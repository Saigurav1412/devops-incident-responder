"""The harness's DiagnoserAgent, end to end in one process, through BOTH real stacks.

Real: Sai's diagnoser service (intake auth, LangGraph graph, Slack signature check, approval
handler that mints the token), Sai's A2AExecutorClient over the a2a-sdk, and our executor server
(guard middleware, CommandValidator allowlist, Remediator gates, approval token verify).
Fake: only process execution (`run_argv`) and the network hop to Slack.

So these tests prove the harness can drive a whole incident the way production would, and that
the harness's approver goes through the same checks as a human click. No Docker, no LLM.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from pydantic import SecretStr

from approval.handler import parse_click
from approval.messages import approval_message
from approval.slack_signature import verify_slack_request
from config import Settings
from contracts import CommandResult, ProposedAction
from diagnoser.a2a_client import A2AExecutorClient
from evals.diagnoser_agent import (
    HARNESS_APPROVER,
    DiagnoserAgent,
    FaultScriptedReasoner,
    approve_button_values,
    harness_settings,
    signed_click,
)
from evals.faults import load_faults
from evals.harness import build_agent
from executor import CommandRunner, IdempotencyStore, Remediator
from executor.runner import RunOutcome
from executor.server import create_app
from memory.store import InMemoryIncidentMemory
from security import CommandValidator
from test_executor_server import GOOD, URL, fake_verify

from test_remediation import RecordingRunner, target  # noqa: F401 (pytest fixture)

KEY_HEX = "cd" * 32
FAULTS = {f.id: f for f in load_faults("all")}


class FakeProcessRunner(CommandRunner):
    """The real CommandRunner (so diagnose() still runs the allowlist), with process execution
    replaced. `health` scripts check_health's curl: True = exit 0, False = exit 22."""

    def __init__(self, health=(True,)):
        super().__init__(CommandValidator())
        self.health = list(health)
        self.argvs: list[list[str]] = []

    async def run_argv(self, argv, timeout_s, *, ctx=None, rule_id=None):
        self.argvs.append(list(argv))
        code = 0
        if argv[0] == "curl":
            ok = self.health.pop(0) if len(self.health) > 1 else self.health[0]
            code = 0 if ok else 22
        return RunOutcome(
            "completed",
            CommandResult(exit_code=code, stdout="ok\n", stderr="", truncated=False, duration_ms=1),
        )


def base_settings(**over) -> Settings:
    # health_settle_s=0: one health curl per check (PR #16's grace period off), so the scripted
    # `health=(False, True)` means "first fix refuted, second approved", and no test sleeps.
    # Before #16 is merged the key is simply ignored (Settings ignores unknown fields).
    fields = {
        "approval_signing_key": SecretStr(KEY_HEX),
        "max_wall_clock_s": 600,
        "health_settle_s": 0,
        **over,
    }
    return Settings.model_validate(fields)


class Rig:
    """One executor server + one DiagnoserAgent wired to it, all in-process."""

    def __init__(self, target_dir, *, health=(True,), executor_key: bytes | None = None):
        self.runner = FakeProcessRunner(health)
        self.remediation_runner = RecordingRunner()
        validator = CommandValidator()
        remediator = Remediator(
            self.remediation_runner,
            executor_key or bytes.fromhex(KEY_HEX),
            IdempotencyStore(),
            target_dir=target_dir,
        )
        app = create_app(self.runner, remediator, fake_verify, URL, validator.summary())
        http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=URL,
            headers={"Authorization": f"Bearer {GOOD}"},
        )
        self.memory = InMemoryIncidentMemory()
        self.agent = DiagnoserAgent(
            name="diagnoser-scripted",
            settings=harness_settings(base_settings()),
            executor=A2AExecutorClient(URL, http_client=http),
            reasoner_factory=FaultScriptedReasoner,
            memory=self.memory,
            poll_s=0.01,
        )

    def handle(self, fault_id: str, config=None):
        from datetime import UTC, datetime

        from contracts import Alert

        alert = Alert(
            alert_id=f"test-{fault_id}",
            source="eval_harness",
            service="proxy",
            summary="GET /health through the proxy is failing",
            fired_at=datetime.now(UTC),
        )
        return asyncio.run(self.agent.handle(alert, FAULTS[fault_id], config or {}))


# --- whole incidents ---------------------------------------------------------------------------


def test_incident_resolves_through_both_real_stacks(target):  # noqa: F811
    rig = Rig(target)
    report = rig.handle("db-stopped-01")
    assert report.outcome == "resolved"
    assert report.replans == 0 and report.blocked_actions == 0
    assert report.tool_calls == 3  # docker ps -a, the remediation, the health curl
    assert report.approval_wait_s >= 0.0
    # The fix really went through the executor's remediation path, with the approved params.
    assert rig.remediation_runner.calls[-1][-2:] == ["restart", "db"]
    assert rig.runner.argvs[0] == ["docker", "ps", "-a"]
    assert rig.runner.argvs[-1][0] == "curl"


@pytest.mark.parametrize("fault_id", sorted(FAULTS))
def test_every_fault_expected_fix_is_accepted_by_the_executor(target, fault_id):  # noqa: F811
    # Each fault's expected fix must survive the real param validation + token check.
    report = Rig(target).handle(fault_id)
    assert report.outcome == "resolved", fault_id
    assert report.blocked_actions == 0


def test_failed_health_check_replans_and_asks_again(target):  # noqa: F811
    # First curl after the fix fails (service still starting), second passes: the graph refutes,
    # replans, proposes again, and the harness approves the NEW proposal.
    rig = Rig(target, health=(False, True))
    report = rig.handle("db-stopped-01")
    assert report.outcome == "resolved"
    assert report.replans == 1
    assert len(rig.remediation_runner.calls) == 2  # two approved fixes, two runs


def test_executor_refusing_the_token_escalates_and_saves_nothing(target):  # noqa: F811
    # Diagnoser and executor hold different keys: the executor refuses the approved fix
    # (approval_invalid). Sai's PR #7 rule: escalate, no health check, no memory record.
    rig = Rig(target, executor_key=bytes.fromhex("ef" * 32))
    report = rig.handle("db-stopped-01")
    assert report.outcome == "escalated"
    assert report.blocked_actions == 1
    assert rig.remediation_runner.calls == []
    assert not any(a[0] == "curl" for a in rig.runner.argvs)


def test_memory_is_shared_across_episodes_unless_ablated(target):  # noqa: F811
    rig = Rig(target)
    rig.handle("db-stopped-01")
    rig.handle("db-stopped-01", {"memory": False})
    rig.handle("db-stopped-01")
    # Two records: the ablated episode wrote to a throwaway store, not the shared one.
    assert len(rig.memory) == 2


# --- the pieces --------------------------------------------------------------------------------


def test_signed_click_passes_slack_verification_and_parsing():
    secret = "s" * 40
    value = {"incident_id": "inc-1", "action_id": "act-1"}
    body, headers = signed_click(secret, value, user_id=HARNESS_APPROVER, now=1_700_000_000)
    verify_slack_request(secret, headers, body, now=1_700_000_000)  # raises if wrong
    click = parse_click(body)
    assert (click.decision, click.incident_id, click.action_id, click.user_id) == (
        "approve",
        "inc-1",
        "act-1",
        HARNESS_APPROVER,
    )


def test_approve_button_value_is_found_in_the_real_message():
    proposed = ProposedAction(
        action_id="act-9", action="restart_service", params={"service": "db"}, rationale="r"
    )
    values = approve_button_values(approval_message("inc-9", proposed))
    assert values == [{"incident_id": "inc-9", "action_id": "act-9"}]


def test_harness_settings_fresh_secrets_and_approver():
    base = base_settings(approver_allowlist="U123")
    a, b = harness_settings(base), harness_settings(base)
    assert a.approver_ids == {"U123", HARNESS_APPROVER}
    assert a.incident_api_token.get_secret_value() != b.incident_api_token.get_secret_value()
    assert a.slack_signing_secret.get_secret_value() != b.slack_signing_secret.get_secret_value()
    assert a.approval_key_bytes() == base.approval_key_bytes()  # NOT replaced: executor shares it


def test_harness_settings_refuses_a_missing_key():
    with pytest.raises(ValueError):
        harness_settings(Settings.model_validate({"approval_signing_key": None}))


def test_build_agent_needs_the_executor_dev_token(monkeypatch):
    monkeypatch.delenv("EXECUTOR_DEV_TOKEN", raising=False)
    with pytest.raises(ValueError, match="EXECUTOR_DEV_TOKEN"):
        build_agent("diagnoser-scripted")


def test_an_ignored_click_fails_fast_instead_of_timing_out(target):  # noqa: F811
    # The harness's approver is NOT on the allowlist: the handler ignores its click, and the
    # agent must say so quickly rather than poll until the episode timeout.
    from evals.diagnoser_agent import ApprovalClickIgnoredError

    rig = Rig(target)
    rig.agent.settings = Settings.model_validate(
        {**rig.agent.settings.model_dump(), "approver_allowlist": "USOMEONEELSE"}
    )
    rig.agent.click_ignored_after_s = 0.05
    with pytest.raises(ApprovalClickIgnoredError):
        rig.handle("db-stopped-01")
    assert rig.remediation_runner.calls == []  # nothing ran without a valid approval


def test_max_hypotheses_ablation_reaches_the_graph(target):  # noqa: F811
    seen = []

    class Wide(FaultScriptedReasoner):
        async def plan(self, state):
            r = await super().plan(state)
            return type(r)(r.value * 3, r.tokens)  # offer 3 hypotheses

        async def assess(self, state):
            seen.append(len(state["hypotheses"]))
            return await super().assess(state)

    rig = Rig(target)
    rig.agent.reasoner_factory = Wide
    rig.handle("db-stopped-01", {"max_hypotheses": 1})
    rig.handle("db-stopped-01", {"max_hypotheses": 3})
    assert seen == [1, 3]  # the graph kept 1, then 3


def test_bad_max_hypotheses_is_refused(target):  # noqa: F811
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Rig(target).handle("db-stopped-01", {"max_hypotheses": 0})


class ExplodingMemory(InMemoryIncidentMemory):
    """Raises in save(): the graph's last node, i.e. AFTER the approval click and the fix."""

    def save(self, record):
        raise AttributeError("boom")


def test_a_graph_crash_after_approval_fails_fast(target):  # noqa: F811
    # 2026-11-27: the resume after the click raised, the service logged only the type, and the
    # harness waited out the 300 s timeout. Now it's an immediate GraphRunError.
    from evals.diagnoser_agent import GraphRunError

    rig = Rig(target)
    rig.agent.memory = ExplodingMemory()
    with pytest.raises(GraphRunError, match="after approving"):
        rig.handle("db-stopped-01")
    assert rig.remediation_runner.calls  # the fix did run before the crash


def test_a_graph_crash_at_intake_fails_fast(target):  # noqa: F811
    from evals.diagnoser_agent import GraphRunError

    class ExplodingPlan(FaultScriptedReasoner):
        async def plan(self, state):
            raise AttributeError("boom")

    rig = Rig(target)
    rig.agent.reasoner_factory = ExplodingPlan
    with pytest.raises(GraphRunError, match="after intake"):
        rig.handle("db-stopped-01")
