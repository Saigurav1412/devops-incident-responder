"""Tests for the A2A executor client (src/diagnoser/a2a_client.py).

Most tests run the client against a real in-process A2A v1.0 server (tests/a2a_stub.py) whose
executor is scripted, so the full protocol (Agent Card, JSON-RPC SendMessage, Task, parts) is
exercised with no network. The last test runs a whole incident through the diagnoser graph with
this client as its executor.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from a2a.helpers.proto_helpers import new_task
from a2a.types import TaskState

from a2a_stub import (
    URL,
    StubExecutor,
    agent_card,
    answers_with_bare_message,
    build_app,
    completes_with,
    completes_with_off_contract_data,
    completes_with_two_parts,
    completes_with_two_results,
    completes_without_artifact,
    crashes,
    fails_with,
    http_client_for,
    rejects_with,
    rejects_without_reason,
    stays_working,
)
from contracts import (
    Alert,
    CommandResult,
    DiagnoseRequest,
    FailureInfo,
    Hypothesis,
    RejectionInfo,
    RemediateRequest,
)
from diagnoser.a2a_client import A2AExecutorClient, task_to_observation
from diagnoser.ports import ExecutorClientError

T0 = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
OK = CommandResult(exit_code=0, stdout="up 3 days", stderr="", truncated=False, duration_ms=12)
DIAG = DiagnoseRequest(incident_id="inc-1", command=["docker", "ps"])
FIX = RemediateRequest(
    incident_id="inc-1",
    action_id="act-1",
    action="restart_service",
    params={"service": "db"},
    approval_token="1800000000.abc",
    idempotency_key="act-1",
)


def call(respond, request=DIAG, *, token_provider=None, card=None, **client_kwargs):
    """Send one request to a stub executor. Returns (observation, stub, seen_headers)."""
    stub = StubExecutor(respond)
    app, seen = build_app(stub, card)

    async def go():
        client = A2AExecutorClient(
            URL,
            http_client=http_client_for(app),
            token_provider=token_provider,
            clock=lambda: T0,
            **client_kwargs,
        )
        try:
            if isinstance(request, RemediateRequest):
                return await client.remediate(request)
            return await client.diagnose(request)
        finally:
            await client.aclose()

    return asyncio.run(go()), stub, seen


# ---------------------------------------------------------------------------------------------
# Normal answers become Observations
# ---------------------------------------------------------------------------------------------


def test_completed_task_becomes_completed_observation():
    obs, stub, _ = call(completes_with(OK))

    assert (obs.state, obs.skill, obs.result) == ("completed", "diagnose", OK)
    assert obs.task_id and obs.observed_at == T0
    ((received, context_id),) = stub.received
    assert received == DIAG  # the executor got exactly our contract model
    assert context_id == "inc-1"  # context_id = incident_id (§2.2)


def test_non_zero_exit_is_still_completed_evidence():
    failing = OK.model_copy(update={"exit_code": 7, "stderr": "curl: (7) refused"})
    obs, _, _ = call(completes_with(failing))
    assert obs.state == "completed" and obs.result.exit_code == 7


def test_remediate_round_trips_the_token_and_idempotency_key():
    obs, stub, _ = call(completes_with(OK), request=FIX)
    ((received, _),) = stub.received
    assert received == FIX
    assert obs.skill == "remediate" and obs.state == "completed"


def test_rejected_task_becomes_rejected_observation():
    info = RejectionInfo(reason="docker exec flags are not allowed", rule_id="docker.exec_flags")
    obs, _, _ = call(rejects_with(info))
    assert (obs.state, obs.result) == ("rejected", info)


def test_failed_task_becomes_failed_observation():
    obs, _, _ = call(fails_with(FailureInfo(reason="timeout after 20s")))
    assert (obs.state, obs.result.reason) == ("failed", "timeout after 20s")


def test_rejection_without_a_reason_keeps_the_rejection():
    obs, _, _ = call(rejects_without_reason)
    assert (obs.state, obs.result.reason) == ("rejected", "rejected_without_reason")


def test_executor_crash_raises_executor_client_error():
    """Observed with a2a-sdk 1.3.1: an uncaught exception in the server's AgentExecutor comes back
    as a JSON-RPC InternalError (-32603), not as a FAILED task (despite the SDK docstring), and
    the server includes the exception's message in the error. The client maps it to
    ExecutorClientError; the graph then records a 'failed' observation."""
    with pytest.raises(ExecutorClientError, match="a2a_call_failed: InternalError"):
        call(crashes)


def test_oversized_output_is_capped_client_side_keeping_the_tail():
    big = OK.model_copy(update={"stdout": "x" * 50_000 + "LAST LINE"})
    obs, _, _ = call(completes_with(big), max_output_bytes=1_000)
    assert len(obs.result.stdout.encode()) <= 1_000
    assert obs.result.stdout.endswith("LAST LINE") and obs.result.truncated is True


# ---------------------------------------------------------------------------------------------
# Malformed or unexpected answers raise instead of being guessed at
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("respond", "reason"),
    [
        (completes_without_artifact, "malformed_task: expected one 'command_result' artifact"),
        (completes_with_two_results, "malformed_task: expected one 'command_result' artifact"),
        (completes_with_two_parts, "malformed_task: expected one 'command_result' artifact"),
        (completes_with_off_contract_data, "malformed_task: command_result doesn't match"),
        (stays_working, "unexpected_task_state: TASK_STATE_WORKING"),
        (answers_with_bare_message, "malformed_response: expected exactly one Task"),
    ],
)
def test_malformed_answers_raise_executor_client_error(respond, reason):
    with pytest.raises(ExecutorClientError, match=reason):
        call(respond)


def test_task_for_another_incident_is_refused():
    task = new_task("t-1", "some-other-incident", TaskState.TASK_STATE_COMPLETED)
    with pytest.raises(ExecutorClientError, match="context_id does not match"):
        task_to_observation(task, DIAG, T0)


@pytest.mark.parametrize(
    "state",
    [
        TaskState.TASK_STATE_SUBMITTED,
        TaskState.TASK_STATE_INPUT_REQUIRED,
        TaskState.TASK_STATE_AUTH_REQUIRED,
        TaskState.TASK_STATE_CANCELED,
        TaskState.TASK_STATE_UNSPECIFIED,
    ],
)
def test_every_non_final_state_is_refused(state):
    with pytest.raises(ExecutorClientError, match="unexpected_task_state"):
        task_to_observation(new_task("t-1", "inc-1", state), DIAG, T0)


# ---------------------------------------------------------------------------------------------
# Transport failures, auth, protocol details
# ---------------------------------------------------------------------------------------------


def _client_with_transport(handler) -> A2AExecutorClient:
    return A2AExecutorClient(
        URL, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=URL)
    )


def test_unreachable_executor_raises_executor_client_error():
    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(ExecutorClientError, match="agent_card_unavailable"):
        asyncio.run(_client_with_transport(refuse).diagnose(DIAG))


def test_timeout_on_send_raises_executor_client_error():
    """The card loads, then SendMessage times out (simulated by the transport)."""
    stub = StubExecutor(completes_with(OK))
    app, _ = build_app(stub)
    asgi = httpx.ASGITransport(app=app)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            raise httpx.ReadTimeout("timed out", request=request)
        return await asgi.handle_async_request(request)

    async def go():
        client = A2AExecutorClient(
            URL, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=URL)
        )
        return await client.diagnose(DIAG)

    with pytest.raises(ExecutorClientError, match="a2a_call_failed"):
        asyncio.run(go())
    assert stub.received == []


def test_card_without_a_jsonrpc_interface_is_refused():
    with pytest.raises(ExecutorClientError, match="agent_card_unavailable"):
        call(completes_with(OK), card=agent_card(bindings=("GRPC",)))


def test_bearer_token_is_fetched_per_request_and_version_header_sent():
    tokens = iter(["token-1", "token-2", "token-3", "token-4"])

    async def provider() -> str:
        return next(tokens)

    _, _, seen = call(completes_with(OK), token_provider=provider)

    # One GET for the Agent Card, one POST for SendMessage; each asked for a fresh token.
    assert [h.get("authorization") for h in seen] == ["Bearer token-1", "Bearer token-2"]
    assert all(h.get("a2a-version") == "1.0" for h in seen)


def test_the_agent_card_is_fetched_once_and_reused():
    stub = StubExecutor(completes_with(OK))
    app, seen = build_app(stub)

    async def go():
        client = A2AExecutorClient(URL, http_client=http_client_for(app), clock=lambda: T0)
        await asyncio.gather(client.diagnose(DIAG), client.diagnose(DIAG))
        await client.diagnose(DIAG)

    asyncio.run(go())
    methods = [h["_method"] for h in seen]
    assert len(stub.received) == 3
    assert methods.count("GET") == 1  # the Agent Card
    assert methods.count("POST") == 3  # one SendMessage per call


# ---------------------------------------------------------------------------------------------
# End to end: the diagnoser graph using the A2A client as its executor
# ---------------------------------------------------------------------------------------------


def test_incident_runs_through_the_graph_over_a2a():
    from config import Settings
    from diagnoser.fakes import ScriptedReasoner
    from diagnoser.graph import build_graph, new_incident_state, resume_with, thread_config
    from diagnoser.nodes import health_check_argv
    from diagnoser.ports import DiagnoserDeps, FixProposal
    from memory.store import InMemoryIncidentMemory

    settings = Settings(_env_file=None, approver_allowlist="U_SAI")
    health_argv = health_check_argv(settings.health_url)
    logs_argv = ["docker", "logs", "--tail", "50", "ir-target-db-1"]

    async def executor(request, context, queue):
        if isinstance(request, DiagnoseRequest) and request.command == logs_argv:
            await completes_with(OK.model_copy(update={"stdout": "FATAL: shutting down"}))(
                request, context, queue
            )
        elif isinstance(request, DiagnoseRequest) and request.command == health_argv:
            await completes_with(OK.model_copy(update={"stdout": '{"status":"ok"}'}))(
                request, context, queue
            )
        elif isinstance(request, RemediateRequest):
            await completes_with(OK)(request, context, queue)
        else:
            await rejects_with(RejectionInfo(reason="not allowed", rule_id="argv.binary"))(
                request, context, queue
            )

    stub = StubExecutor(executor)
    app, _ = build_app(stub)
    reasoner = ScriptedReasoner(
        plans=[[Hypothesis(id="h1", statement="postgres is down")]],
        commands=[logs_argv],
        assessments=[[Hypothesis(id="h1", statement="postgres is down", status="confirmed")]],
        proposals=[FixProposal("restart_service", {"service": "db"}, "db shut down")],
    )

    async def go():
        client = A2AExecutorClient(URL, http_client=http_client_for(app), clock=lambda: T0)
        deps = DiagnoserDeps(reasoner, client, InMemoryIncidentMemory(), settings, lambda: T0)
        graph = build_graph(deps)
        alert = Alert(alert_id="a1", source="manual", service="db", summary="503", fired_at=T0)
        paused = await graph.ainvoke(new_incident_state(alert, T0, "inc-9"), thread_config("inc-9"))
        action_id = paused["__interrupt__"][0].value["proposed"]["action_id"]
        from contracts import ApprovalDecision

        decision = ApprovalDecision(
            action_id=action_id, decision="approve", approver="U_SAI", decided_at=T0, token="tok"
        )
        return await graph.ainvoke(resume_with(decision), thread_config("inc-9"))

    done = asyncio.run(go())

    assert done["outcome"] == "resolved"
    kinds = [type(r).__name__ for r, _ in stub.received]
    assert kinds == ["DiagnoseRequest", "RemediateRequest", "DiagnoseRequest"]  # logs, fix, health
    assert {ctx for _, ctx in stub.received} == {"inc-9"}
    assert [o.state for o in done["observations"]] == ["completed"] * 3


# ---------------------------------------------------------------------------------------------
# Policy summaries from the Agent Card (for the LLM reasoner's prompt)
# ---------------------------------------------------------------------------------------------


def test_policy_summaries_come_from_the_card_skills_and_share_its_fetch():
    stub = StubExecutor(completes_with(OK))
    app, seen = build_app(stub)

    async def go():
        client = A2AExecutorClient(URL, http_client=http_client_for(app), clock=lambda: T0)
        try:
            policy = await client.policy_summaries()
            await client.diagnose(DIAG)
            return policy
        finally:
            await client.aclose()

    policy = asyncio.run(go())
    assert (policy.diagnose, policy.remediate) == ("read-only", "catalog")
    assert [h["_method"] for h in seen] == ["GET", "POST"]  # one card fetch for both uses


def test_card_without_a_policy_skill_is_refused():
    card = agent_card()
    del card.skills[1]  # drop "remediate"
    stub = StubExecutor(completes_with(OK))
    app, _ = build_app(stub, card)

    async def go():
        client = A2AExecutorClient(URL, http_client=http_client_for(app), clock=lambda: T0)
        try:
            return await client.policy_summaries()
        finally:
            await client.aclose()

    with pytest.raises(ExecutorClientError, match="agent_card_missing_skills: remediate"):
        asyncio.run(go())
