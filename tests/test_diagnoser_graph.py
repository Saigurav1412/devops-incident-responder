"""Graph-level tests for the diagnoser (INTERFACES §1), using the scripted fakes.

Each test drives the real compiled LangGraph graph end to end. Only the reasoner and the
executor are fakes, so these tests prove the *control flow*: the self-correction loops, the
approval gate, and the budget caps. That's where the safety rules live, and it must hold
whatever the LLM says (AGENTS.md rules 1, 4, 5).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from config import Settings
from contracts import (
    Alert,
    ApprovalDecision,
    CommandResult,
    FailureInfo,
    Hypothesis,
    IncidentRecord,
    Observation,
    ProposedAction,
    RejectionInfo,
    RemediateRequest,
)
from diagnoser.fakes import FakeExecutorClient, ScriptedReasoner
from diagnoser.graph import build_graph, new_incident_state, resume_with, thread_config
from diagnoser.nodes import (
    APPROVAL_INTERRUPT_KIND,
    REDACTED,
    DiagnoserNodes,
    apply_assessment,
    fix_applied,
    last_remediation,
)
from diagnoser.ports import DiagnoserDeps, ExecutorClientError, FixProposal, Reasoned
from memory.store import InMemoryIncidentMemory

APPROVER = "U_SAI"
T0 = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
LOGS_CMD = ["docker", "logs", "--tail", "200", "api"]
# Stands in for the HMAC token the Slack handler would mint; the graph only passes it through.
FAKE_TOKEN = "fake-approval-token"


class FakeClock:
    """A clock tests can move forward, to hit the wall-clock cap without sleeping."""

    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


def hyp(statement: str, status: str = "open", hid: str = "h1") -> Hypothesis:
    return Hypothesis(id=hid, statement=statement, status=status)


def settings(**overrides) -> Settings:
    # _env_file=None: never read a developer's real .env in tests.
    # health_settle_s=0: one curl per health check, so `health=[False, True]` in these tests
    # still means "the first fix failed, the replanned one worked". The grace period has its
    # own tests (test_health_grace_*).
    base = {"approver_allowlist": APPROVER, "health_settle_s": 0}
    return Settings(_env_file=None, **(base | overrides))


async def no_sleep(_seconds: float) -> None:
    """check_health's wait between retries, without waiting."""


def make_deps(reasoner, *, executor=None, memory=None, clock=None, **cfg):
    clock = clock or FakeClock()
    return DiagnoserDeps(
        reasoner=reasoner,
        executor=executor or FakeExecutorClient(clock=clock),
        memory=memory if memory is not None else InMemoryIncidentMemory(),
        settings=settings(**cfg),
        clock=clock,
        sleep=no_sleep,
    )


def alert() -> Alert:
    return Alert(
        alert_id="a1",
        source="manual",
        service="api",
        summary="api returns 503, cannot reach postgres",
        fired_at=T0,
    )


def start(deps, incident_id="inc-1", graph=None):
    """Run a new incident until it ends or pauses. Returns (graph, result)."""
    graph = graph or build_graph(deps)
    state = new_incident_state(alert(), now=deps.clock(), incident_id=incident_id)
    return graph, asyncio.run(graph.ainvoke(state, thread_config(incident_id)))


def decide(graph, action_id, decision="approve", approver=APPROVER, token=FAKE_TOKEN, inc="inc-1"):
    """Simulate the Slack handler resuming the paused graph."""
    value = ApprovalDecision(
        action_id=action_id,
        decision=decision,
        approver=approver,
        decided_at=T0,
        token=token if decision == "approve" else None,
    )
    return asyncio.run(graph.ainvoke(resume_with(value), thread_config(inc)))


def happy_reasoner(**kw) -> ScriptedReasoner:
    """Plan one hypothesis, run one diagnostic, confirm it, propose a restart."""
    return ScriptedReasoner(
        plans=[[hyp("postgres is down")]],
        commands=[LOGS_CMD],
        assessments=[[hyp("postgres is down", "confirmed")]],
        proposals=[FixProposal("restart_service", {"service": "db"}, "db container exited")],
        **kw,
    )


def logs_executor(clock=None, health=(True,)) -> FakeExecutorClient:
    result = CommandResult(
        exit_code=0, stdout="connection refused", stderr="", truncated=False, duration_ms=3
    )
    return FakeExecutorClient(
        diagnose_results={tuple(LOGS_CMD): result}, health=health, clock=clock or FakeClock()
    )


# ---------------------------------------------------------------------------------------------
# Happy path + approval gate
# ---------------------------------------------------------------------------------------------


def test_happy_path_pauses_for_approval_then_resolves_and_saves_memory():
    executor, memory = logs_executor(), InMemoryIncidentMemory()
    deps = make_deps(happy_reasoner(), executor=executor, memory=memory)

    graph, paused = start(deps)

    # The graph stops at request_approval and hands the proposal out via __interrupt__.
    (interrupt,) = paused["__interrupt__"]
    assert interrupt.value["kind"] == APPROVAL_INTERRUPT_KIND
    proposed = ProposedAction.model_validate(interrupt.value["proposed"])
    assert proposed.action == "restart_service"
    assert executor.remediations == []  # nothing ran before a human said yes
    snapshot = asyncio.run(graph.aget_state(thread_config("inc-1")))
    assert snapshot.next == ("request_approval",)

    done = decide(graph, proposed.action_id, token="signed-token")

    assert done["outcome"] == "resolved"
    (remediation,) = executor.remediations
    assert remediation.approval_token == "signed-token"
    assert remediation.idempotency_key == proposed.action_id == remediation.action_id
    assert remediation.params == {"service": "db"}
    assert len(memory) == 1
    record = memory.find_similar("api postgres")[0]
    assert record.verified and record.fix.action_id == proposed.action_id


def test_action_id_is_generated_by_code_not_reasoner():
    deps = make_deps(happy_reasoner(), executor=logs_executor())
    _, first = start(deps, incident_id="inc-a")
    deps2 = make_deps(happy_reasoner(), executor=logs_executor())
    _, second = start(deps2, incident_id="inc-b")
    ids = {r["__interrupt__"][0].value["proposed"]["action_id"] for r in (first, second)}
    assert len(ids) == 2  # same reasoner output, different fresh uuids


def test_reject_ends_incident_without_running_anything():
    executor = logs_executor()
    graph, paused = start(make_deps(happy_reasoner(), executor=executor))
    action_id = paused["__interrupt__"][0].value["proposed"]["action_id"]

    done = decide(graph, action_id, decision="reject")

    assert done["outcome"] == "rejected"
    assert executor.remediations == []


@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        ({"action_id": "some-older-proposal"}, "stale click on a different proposal"),
        ({"approver": "U_STRANGER"}, "approver not on the allowlist"),
        ({"token": None}, "approve without a token"),
    ],
)
def test_invalid_approval_is_ignored_and_graph_keeps_waiting(kwargs, why):
    executor = logs_executor()
    graph, paused = start(make_deps(happy_reasoner(), executor=executor))
    real_id = paused["__interrupt__"][0].value["proposed"]["action_id"]

    args = {"action_id": real_id} | kwargs
    still_paused = decide(graph, **args)

    assert "__interrupt__" in still_paused, why
    assert executor.remediations == [], why
    # A valid decision afterwards still works.
    assert decide(graph, real_id)["outcome"] == "resolved"


def test_empty_allowlist_means_nobody_can_approve():
    executor = logs_executor()
    deps = make_deps(happy_reasoner(), executor=executor, approver_allowlist="")
    graph, paused = start(deps)
    action_id = paused["__interrupt__"][0].value["proposed"]["action_id"]

    assert "__interrupt__" in decide(graph, action_id)
    assert executor.remediations == []


def test_paused_incident_resumes_in_a_new_graph_sharing_the_checkpointer():
    """Simulates a restart: a fresh graph object, same checkpoint store, same incident_id."""
    deps = make_deps(happy_reasoner(), executor=logs_executor())
    first_graph, paused = start(deps)
    action_id = paused["__interrupt__"][0].value["proposed"]["action_id"]

    second_graph = build_graph(deps, checkpointer=first_graph.checkpointer)
    assert decide(second_graph, action_id)["outcome"] == "resolved"


def test_incidents_are_isolated_by_thread():
    deps = make_deps(happy_reasoner(), executor=logs_executor())
    graph, _ = start(deps, incident_id="inc-1")
    other = asyncio.run(graph.aget_state(thread_config("inc-2")))
    assert other.values == {}  # nothing from inc-1 leaks into another incident's thread


# ---------------------------------------------------------------------------------------------
# Self-correction
# ---------------------------------------------------------------------------------------------


def test_refuted_plan_triggers_replan_and_keeps_refuted_history():
    reasoner = ScriptedReasoner(
        plans=[[hyp("redis is full")], [hyp("postgres is down")]],
        commands=[LOGS_CMD],
        assessments=[
            [hyp("redis is full", "refuted")],
            [hyp("redis is full", "refuted"), hyp("postgres is down", "confirmed", "h2")],
        ],
        proposals=[FixProposal("restart_service", {"service": "db"}, "db exited")],
    )
    graph, paused = start(make_deps(reasoner, executor=logs_executor()))

    assert reasoner.plans.calls == 2
    values = asyncio.run(graph.aget_state(thread_config("inc-1"))).values
    assert values["budget"].replans == 1
    assert [h.status for h in values["hypotheses"]] == ["refuted", "confirmed"]
    assert "__interrupt__" in paused


class RefuteEverything(ScriptedReasoner):
    """A reasoner whose every guess is wrong: assess() refutes every hypothesis it is shown.

    (A fixed `[h1: refuted]` script isn't enough: after a replan the new hypothesis is h2, and
    apply_assessment keeps a hypothesis the reasoner didn't mention unchanged, i.e. still open.)
    """

    async def assess(self, state):
        self.assessments.calls += 1
        refuted = [h.model_copy(update={"status": "refuted"}) for h in state["hypotheses"]]
        return Reasoned(refuted, self.tokens_per_call)


def test_replans_are_capped_then_escalated():
    reasoner = RefuteEverything(plans=[[hyp("guess")]], commands=[LOGS_CMD])
    _, done = start(make_deps(reasoner, executor=logs_executor(), max_replans=2))

    assert done["outcome"] == "escalated"
    assert done["budget"].replans == 2
    assert reasoner.plans.calls == 3  # the first plan + 2 replans


def test_failed_health_check_refutes_the_fix_and_replans():
    reasoner = ScriptedReasoner(
        plans=[[hyp("postgres is down")], [hyp("db password changed")]],
        commands=[LOGS_CMD],
        assessments=[
            [hyp("postgres is down", "confirmed")],
            [hyp("postgres is down", "refuted"), hyp("db password changed", "confirmed", "h2")],
        ],
        proposals=[
            FixProposal("restart_service", {"service": "db"}, "db exited"),
            FixProposal("restore_config", {"service": "api", "file": "api.env"}, "bad url"),
        ],
    )
    executor = logs_executor(health=[False, True])
    graph, paused = start(make_deps(reasoner, executor=executor))

    first_id = paused["__interrupt__"][0].value["proposed"]["action_id"]
    second_pause = decide(graph, first_id)  # fix runs, health fails, graph replans + pauses again
    second = second_pause["__interrupt__"][0].value["proposed"]
    assert second["action"] == "restore_config"
    assert second["action_id"] != first_id

    done = decide(graph, second["action_id"])
    assert done["outcome"] == "resolved"
    assert done["hypotheses"][0].status == "refuted"  # the fix that didn't work stays ruled out
    assert executor.health_checks == 2


def test_max_hypotheses_limits_the_plan_and_ids_are_reassigned():
    reasoner = ScriptedReasoner(
        plans=[[hyp("a", hid="x"), hyp("b", hid="x"), hyp("c", hid="x")]],
        commands=[None],
        assessments=[[hyp("a", "refuted"), hyp("b", "refuted", "h2")]],
    )
    _, done = start(make_deps(reasoner, max_hypotheses=2, max_replans=0))
    assert [h.statement for h in done["hypotheses"]] == ["a", "b"]
    assert done["outcome"] == "escalated"


def test_empty_plan_escalates_instead_of_looping():
    _, done = start(make_deps(ScriptedReasoner(plans=[[]])))
    assert done["outcome"] == "escalated"


# ---------------------------------------------------------------------------------------------
# Budget caps (AGENTS.md rule 5): undecided reasoner, every cap must stop it
# ---------------------------------------------------------------------------------------------


def undecided_reasoner(tokens: int = 0) -> ScriptedReasoner:
    """Never confirms or refutes, so only a budget cap can end the incident."""
    return ScriptedReasoner(
        plans=[[hyp("maybe")]],
        commands=[LOGS_CMD],
        assessments=[[hyp("maybe")]],
        tokens_per_call=tokens,
    )


def test_tool_call_cap_stops_the_loop():
    executor = logs_executor()
    _, done = start(make_deps(undecided_reasoner(), executor=executor, max_tool_calls=3))
    assert done["outcome"] == "budget_exceeded"
    assert len(executor.diagnostics) == 3


def test_iteration_cap_stops_the_loop():
    reasoner = ScriptedReasoner(
        plans=[[hyp("maybe")]], commands=[None], assessments=[[hyp("maybe")]]
    )
    _, done = start(make_deps(reasoner, max_iterations=4))
    assert done["outcome"] == "budget_exceeded"
    assert reasoner.assessments.calls == 4


def test_token_cap_stops_the_loop():
    _, done = start(
        make_deps(
            undecided_reasoner(tokens=400), executor=logs_executor(), max_tokens_per_incident=1_000
        )
    )
    assert done["outcome"] == "budget_exceeded"
    assert done["budget"].tokens >= 1_000


def test_wall_clock_cap_stops_the_loop():
    clock = FakeClock()

    class SlowReasoner(ScriptedReasoner):
        async def assess(self, state):
            clock.now += timedelta(seconds=40)  # each round "takes" 40s
            return await super().assess(state)

    reasoner = SlowReasoner(plans=[[hyp("maybe")]], commands=[None], assessments=[[hyp("maybe")]])
    _, done = start(make_deps(reasoner, clock=clock, max_wall_clock_s=100))
    assert done["outcome"] == "budget_exceeded"
    assert reasoner.assessments.calls == 3  # 40s, 80s, 120s -> stop


def test_step_limit_is_a_backstop_if_budget_checks_are_bypassed(monkeypatch):
    """Simulate a bug that disables every budget cap: the run must fail fast, not spin."""
    from langgraph.errors import GraphRecursionError

    monkeypatch.setattr("diagnoser.nodes.budget_exceeded_reason", lambda *a: None)
    reasoner = ScriptedReasoner(
        plans=[[hyp("maybe")]], commands=[None], assessments=[[hyp("maybe")]]
    )
    with pytest.raises(GraphRecursionError):
        start(make_deps(reasoner))


def test_waiting_for_approval_does_not_trip_the_wall_clock_cap():
    clock = FakeClock()
    executor = logs_executor(clock)
    graph, paused = start(
        make_deps(happy_reasoner(), executor=executor, clock=clock, max_wall_clock_s=60)
    )
    clock.now += timedelta(minutes=10)  # the human took 10 minutes to click Approve
    action_id = paused["__interrupt__"][0].value["proposed"]["action_id"]

    assert decide(graph, action_id)["outcome"] == "resolved"
    assert len(executor.remediations) == 1


# ---------------------------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------------------------


def test_unreachable_executor_is_recorded_not_raised():
    executor = FakeExecutorClient(unreachable=True)
    _, done = start(make_deps(undecided_reasoner(), executor=executor, max_tool_calls=2))
    assert done["outcome"] == "budget_exceeded"
    failed = done["observations"][0]
    assert failed.state == "failed"
    assert failed.result.reason.startswith("executor_error")


def test_rejected_diagnostic_is_evidence_not_a_crash():
    # Fake executor has no entry for this argv, so it answers REJECTED like the allowlist would.
    reasoner = ScriptedReasoner(
        plans=[[hyp("maybe")]],
        commands=[["rm", "-rf", "/"]],
        assessments=[[hyp("maybe", "refuted")]],
    )
    _, done = start(make_deps(reasoner, max_replans=0))
    assert done["observations"][0].state == "rejected"
    assert done["outcome"] == "escalated"


# ---------------------------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------------------------


def _past_record() -> IncidentRecord:
    return IncidentRecord(
        incident_id="old-1",
        created_at=T0,
        symptoms="api cannot reach postgres 503",
        root_cause="postgres container stopped",
        fix=ProposedAction(action_id="old-a", action="restart_service", params={}, rationale="r"),
        verified=True,
    )


@pytest.mark.parametrize(("enabled", "expected"), [(True, 1), (False, 0)])
def test_recall_respects_memory_enabled_switch(enabled, expected):
    memory = InMemoryIncidentMemory()
    memory.save(_past_record())
    graph, _ = start(
        make_deps(happy_reasoner(), executor=logs_executor(), memory=memory, memory_enabled=enabled)
    )
    values = asyncio.run(graph.aget_state(thread_config("inc-1"))).values
    assert len(values["recalled"]) == expected


# ---------------------------------------------------------------------------------------------
# Follow-ups from Om's review: assessment merge rules, token redaction, fixed health check
# ---------------------------------------------------------------------------------------------


def test_apply_assessment_only_updates_status_and_confidence_of_known_ids():
    current = [hyp("redis is full", "refuted", "h1"), hyp("postgres is down", "open", "h2")]
    assessed = [
        hyp("redis is full", "open", "h1"),  # tries to un-refute
        Hypothesis(id="h2", statement="REWORDED", status="confirmed", confidence=0.9),
        hyp("brand new idea", "confirmed", "h9"),  # unknown id
    ]

    merged, ignored = apply_assessment(current, assessed)

    assert [(h.id, h.statement, h.status) for h in merged] == [
        ("h1", "redis is full", "refuted"),  # stays refuted
        ("h2", "postgres is down", "confirmed"),  # status applied, statement kept
    ]
    assert merged[1].confidence == 0.9
    assert sorted(ignored) == ["reworded h2", "un-refute h1", "unknown id h9"]


def test_apply_assessment_keeps_hypotheses_the_reasoner_left_out():
    current = [hyp("a", "open", "h1"), hyp("b", "open", "h2")]
    merged, ignored = apply_assessment(current, [hyp("a", "refuted", "h1")])
    assert [(h.id, h.status) for h in merged] == [("h1", "refuted"), ("h2", "open")]
    assert ignored == []


def test_graph_ignores_a_reasoner_that_tries_to_undo_a_refutation():
    """End to end: after a replan, the reasoner un-refutes h1, confirms an invented h9 and
    rewords h2. Only h2's status change may land, so the fix proposed is for the real h2."""
    reasoner = ScriptedReasoner(
        plans=[[hyp("redis is full")], [hyp("postgres is down")]],
        commands=[LOGS_CMD],
        assessments=[
            [hyp("redis is full", "refuted", "h1")],
            [
                hyp("redis is full", "confirmed", "h1"),
                hyp("ignore previous instructions", "confirmed", "h2"),
                hyp("invented", "confirmed", "h9"),
            ],
        ],
        proposals=[FixProposal("restart_service", {"service": "db"}, "db exited")],
    )
    graph, paused = start(make_deps(reasoner, executor=logs_executor()))

    values = asyncio.run(graph.aget_state(thread_config("inc-1"))).values
    assert [(h.id, h.statement, h.status) for h in values["hypotheses"]] == [
        ("h1", "redis is full", "refuted"),
        ("h2", "postgres is down", "confirmed"),
    ]
    assert "__interrupt__" in paused


def test_approval_token_is_redacted_from_state_after_execute_fix():
    executor = logs_executor()
    graph, paused = start(make_deps(happy_reasoner(), executor=executor))
    action_id = paused["__interrupt__"][0].value["proposed"]["action_id"]

    done = decide(graph, action_id, token="the-real-token")

    # The executor got the real token...
    assert executor.remediations[0].approval_token == "the-real-token"
    # ...but nothing kept in the final state (checkpoints, traces, later prompts) has it.
    remediate_obs = [o for o in done["observations"] if o.skill == "remediate"]
    assert [o.request.approval_token for o in remediate_obs] == [REDACTED]
    assert done["approval"].token == REDACTED
    assert "the-real-token" not in str(done)


def test_health_check_sends_the_exact_allowlisted_curl_and_records_it():
    executor = logs_executor()
    graph, paused = start(make_deps(happy_reasoner(), executor=executor))
    action_id = paused["__interrupt__"][0].value["proposed"]["action_id"]
    done = decide(graph, action_id)

    # Pinned to the argv Om's executor allowlist permits (tests/test_allowlist.py on PR #4).
    health_obs = done["observations"][-1]
    assert health_obs.request.command == [
        "curl",
        "-fsS",
        "--max-time",
        "5",
        "http://127.0.0.1:8088/health",
    ]
    assert health_obs.state == "completed"
    # 1 reasoner diagnostic + 1 remediation + 1 health check
    assert done["budget"].tool_calls == 3


def test_health_check_uses_health_url_from_config():
    url = "http://10.0.0.5:8088/health"
    executor = FakeExecutorClient(health_url=url, clock=FakeClock())
    reasoner = ScriptedReasoner(
        plans=[[hyp("postgres is down")]],
        commands=[None],  # no diagnostic needed; this test is about the health check
        assessments=[[hyp("postgres is down", "confirmed")]],
        proposals=[FixProposal("restart_service", {"service": "db"}, "db exited")],
    )
    graph, paused = start(make_deps(reasoner, executor=executor, health_url=url))
    action_id = paused["__interrupt__"][0].value["proposed"]["action_id"]

    assert decide(graph, action_id)["outcome"] == "resolved"
    assert executor.health_checks == 1


class HealthUnreachable(FakeExecutorClient):
    """The fix goes through, then the executor drops off before the health check.

    (Making the whole fake unreachable would stop at the fix: an unapplied fix escalates before
    any health check, see route_after_execute.)
    """

    async def diagnose(self, request):
        if tuple(request.command) == self.health_argv:
            self.calls.append(request)
            raise ExecutorClientError("connection refused (fake)")
        return await super().diagnose(request)


@pytest.mark.parametrize(
    ("executor_kwargs", "why"),
    [
        ({"health": [False]}, "curl -f exits 22 on a 503"),
        ({"health_url": "http://not-the-configured-url/health"}, "REJECTED by the executor"),
        ({"health_unreachable": True}, "executor unreachable: a failed check is not a pass"),
    ],
)
def test_anything_but_exit_0_counts_as_unhealthy(executor_kwargs, why):
    result = CommandResult(exit_code=0, stdout="x", stderr="", truncated=False, duration_ms=1)
    cls = (
        HealthUnreachable
        if executor_kwargs.pop("health_unreachable", False)
        else FakeExecutorClient
    )
    executor = cls(diagnose_results={tuple(LOGS_CMD): result}, clock=FakeClock(), **executor_kwargs)
    graph, paused = start(make_deps(happy_reasoner(), executor=executor, max_replans=0))
    action_id = paused["__interrupt__"][0].value["proposed"]["action_id"]

    done = decide(graph, action_id)

    assert done["outcome"] == "escalated", why
    assert done["hypotheses"][0].status == "refuted", why


# ---------------------------------------------------------------------------------------------
# A fix the executor didn't apply is never "resolved"
# ---------------------------------------------------------------------------------------------
# Found by running the graph against Om's real executor server (PR #12) with a token minted over
# the wrong params: the executor correctly refused the fix, but the graph still ran check_health,
# the service answered, and the incident ended "resolved" with a verified memory record for a fix
# that never ran. A service that recovers by itself would do exactly that in production.


@pytest.mark.parametrize(
    ("result", "why"),
    [
        (RejectionInfo(reason="approval_invalid", rule_id="approval"), "refused by policy"),
        (FailureInfo(reason="timeout after 120s"), "failed half-way"),
        (
            CommandResult(
                exit_code=1, stdout="", stderr="no such service", truncated=False, duration_ms=4
            ),
            "completed, but the step exited non-zero",
        ),
    ],
)
def test_fix_not_applied_escalates_even_if_the_service_looks_healthy(result, why):
    memory = InMemoryIncidentMemory()
    executor = logs_executor(health=(True,))  # the service answers: the trap
    executor.remediate_results = {"restart_service": result}
    graph, paused = start(make_deps(happy_reasoner(), executor=executor, memory=memory))
    action_id = paused["__interrupt__"][0].value["proposed"]["action_id"]

    done = decide(graph, action_id)

    assert done["outcome"] == "escalated", why
    assert executor.health_checks == 0, why  # no health check: nothing was applied to check
    assert len(memory) == 0, why  # no "verified" record
    # The hypothesis isn't refuted: the fix never ran, so this says nothing about the diagnosis.
    assert done["hypotheses"][0].status == "confirmed", why


def test_unreachable_executor_during_the_fix_escalates():
    # Same rule via _call_executor: a transport failure becomes a "failed" observation.
    obs = Observation(
        task_id="",
        skill="remediate",
        request=RemediateRequest(
            incident_id="inc-1",
            action_id="act-1",
            action="restart_service",
            params={"service": "db"},
            approval_token=FAKE_TOKEN,
            idempotency_key="act-1",
        ),
        state="failed",
        result=FailureInfo(reason="executor_error: connection refused"),
        observed_at=T0,
    )
    assert not fix_applied(obs)


def _remediation(action_id: str, result) -> Observation:
    return Observation(
        task_id=f"t-{action_id}",
        skill="remediate",
        request=RemediateRequest(
            incident_id="inc-1",
            action_id=action_id,
            action="restart_service",
            params={"service": "db"},
            approval_token=REDACTED,
            idempotency_key=action_id,
        ),
        state="completed" if isinstance(result, CommandResult) else "rejected",
        result=result,
        observed_at=T0,
    )


OK = CommandResult(exit_code=0, stdout="ok", stderr="", truncated=False, duration_ms=1)


def test_an_earlier_fix_does_not_count_for_the_current_proposal():
    state = new_incident_state(alert(), now=T0, incident_id="inc-1")
    state["proposed"] = ProposedAction(
        action_id="act-2",
        action="restart_service",
        params={"service": "db"},
        rationale="r",
    )
    state["observations"] = [_remediation("act-1", OK)]  # applied, but for an older proposal
    assert last_remediation(state) is None

    state["observations"].append(_remediation("act-2", RejectionInfo(reason="x", rule_id="y")))
    assert last_remediation(state).request.action_id == "act-2"
    assert not fix_applied(last_remediation(state))


def test_save_memory_refuses_a_fix_that_was_not_applied():
    # Defence in depth behind route_after_execute: even if routing were wrong, memory stays clean.
    memory = InMemoryIncidentMemory()
    nodes = DiagnoserNodes(make_deps(happy_reasoner(), memory=memory))
    state = new_incident_state(alert(), now=T0, incident_id="inc-1")
    state["hypotheses"] = [hyp("postgres is down", "confirmed")]
    state["proposed"] = ProposedAction(
        action_id="act-1",
        action="restart_service",
        params={"service": "db"},
        rationale="r",
    )
    state["observations"] = [_remediation("act-1", RejectionInfo(reason="x", rule_id="y"))]

    with pytest.raises(RuntimeError, match="didn't apply"):
        asyncio.run(nodes.save_memory(state))
    assert len(memory) == 0


def test_fix_applied_needs_completed_state_and_remediate_skill():
    # The Observation contract doesn't tie `state` to the result type, so check both.
    applied = _remediation("act-1", OK)
    assert fix_applied(applied)
    assert not fix_applied(applied.model_copy(update={"state": "failed"}))
    assert not fix_applied(applied.model_copy(update={"skill": "diagnose"}))


# --- check_health grace period ------------------------------------------------------------------
# Found in the first live eval run (2026-11-26): one curl ~20 ms after the fix refuted correct
# fixes, because a restarted service needs a few seconds to answer.


class RecordingSleep:
    def __init__(self):
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def grace_deps(health, *, sleep=None, **cfg):
    deps = make_deps(happy_reasoner(), executor=logs_executor(health=health), **cfg)
    deps.sleep = sleep or RecordingSleep()
    return deps


def test_health_grace_a_slow_service_is_not_refuted():
    # Not answering for the first 3 curls, then healthy: ONE approval, resolved, no replan.
    deps = grace_deps([False, False, False, True], health_settle_s=20, health_retry_interval_s=2)
    graph, paused = start(deps)
    done = decide(graph, paused["__interrupt__"][0].value["proposed"]["action_id"])
    assert done["outcome"] == "resolved"
    assert done["budget"].replans == 0
    assert deps.executor.health_checks == 4
    assert deps.sleep.waits == [2, 2, 2]  # waited between tries, not after the passing one


def test_health_grace_is_bounded_by_the_settle_window():
    # Never healthy: 1 + 20 // 2 = 11 curls, then refuted and replanned as before.
    deps = grace_deps([False], health_settle_s=20, health_retry_interval_s=2, max_replans=0)
    graph, paused = start(deps)
    done = decide(graph, paused["__interrupt__"][0].value["proposed"]["action_id"])
    assert deps.executor.health_checks == 11
    assert len(deps.sleep.waits) == 10
    assert done["outcome"] == "escalated"
    assert done["hypotheses"][0].status == "refuted"


def test_health_grace_counts_every_curl_and_respects_max_tool_calls():
    # The tool-call cap wins over the settle window: logs (1) + fix (1) leave 3 of 5 for curls.
    # max_replans=0: the incident stops right after the check, so the budget shows exactly what
    # check_health charged (with replans, later steps would fill the cap either way).
    deps = grace_deps(
        [False], health_settle_s=60, health_retry_interval_s=1, max_tool_calls=5, max_replans=0
    )
    graph, paused = start(deps)
    done = decide(graph, paused["__interrupt__"][0].value["proposed"]["action_id"])
    assert deps.executor.health_checks == 3
    assert done["budget"].tool_calls == 5  # logs + fix + 3 curls, every curl counted
    assert done["outcome"] == "escalated"


def test_health_grace_does_not_retry_a_rejected_health_check():
    # A REJECTED curl (HEALTH_URL not on the executor's allowlist) won't change on retry.
    deps = grace_deps([True], health_settle_s=20, health_url="http://127.0.0.1:8088/other")
    graph, paused = start(deps)
    decide(graph, paused["__interrupt__"][0].value["proposed"]["action_id"])
    curls = [c for c in deps.executor.calls if getattr(c, "command", [""])[0] == "curl"]
    assert len(curls) == 1
    assert deps.sleep.waits == []


def test_health_grace_zero_means_one_curl():
    deps = grace_deps([False, True], health_settle_s=0, max_replans=0)
    graph, paused = start(deps)
    done = decide(graph, paused["__interrupt__"][0].value["proposed"]["action_id"])
    assert deps.executor.health_checks == 1 and done["outcome"] == "escalated"
