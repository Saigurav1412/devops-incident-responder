"""The interface the harness drives, and two baseline agents.

An agent receives one `Alert` and does whatever it does (diagnose, propose, get approval, fix),
then returns an `AgentReport` describing its own run. The HARNESS, not the agent, decides whether
the incident was resolved, by running the oracle. That's why the report has the agent's
`outcome` only as a claim.

Baselines (they bracket the real agent's numbers in the eval report):
  * NoopAgent      does nothing. Its resolution rate must be 0%: if not, the oracle or a reset is
                   broken and every other number is meaningless.
  * ScriptedAgent  reads the fault's expected fix (which the real agent never sees), gets it
                   approved by the scripted approver, and applies it through the REAL executor
                   code (Remediator + CommandRunner). An upper bound, and proof that the whole
                   pipeline (approval token, gates, steps, oracle) works end to end.
The real diagnoser (Sai's graph) plugs in behind the same `Agent` protocol once both stacks are
merged; see evals/README.md.
"""

from __future__ import annotations

import time
import uuid
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from contracts import Alert, DiagnoseRequest, RemediateRequest
from contracts.approval_token import mint
from evals.faults import Fault
from executor import CommandRunner, Remediator


class AgentReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal["resolved", "escalated", "rejected", "budget_exceeded", "open"]
    replans: int = 0
    tool_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    blocked_actions: int = 0  # REJECTED diagnostics/remediations seen during the run
    approval_wait_s: float = 0.0
    langsmith_run_url: str | None = None


class Agent(Protocol):
    name: str

    async def handle(
        self, alert: Alert, fault: Fault, config: dict[str, bool | int | str]
    ) -> AgentReport:
        """Handle one incident. `fault` is passed so baselines can cheat; a real agent must
        only use `alert` (the harness does not enforce this; the adapter must not read it)."""
        ...


class NoopAgent:
    name = "noop"

    async def handle(self, alert, fault, config) -> AgentReport:
        return AgentReport(outcome="open")


class ScriptedApprover:
    """Stands in for the human clicking Approve in Slack (INTERFACES §4.3: the only other place
    allowed to mint tokens). Approves immediately; the wait is recorded so reports can show it."""

    def __init__(self, key: bytes, ttl_s: int = 900) -> None:
        self.key = key
        self.ttl_s = ttl_s

    def approve(
        self, *, incident_id: str, action_id: str, action: str, params: dict[str, str]
    ) -> tuple[str, float]:
        started = time.monotonic()
        token = mint(
            self.key,
            incident_id=incident_id,
            action_id=action_id,
            action=action,
            params=params,
            ttl_s=self.ttl_s,
        )
        return token, time.monotonic() - started


class ScriptedAgent:
    name = "scripted"

    def __init__(
        self, runner: CommandRunner, remediator: Remediator, approver: ScriptedApprover
    ) -> None:
        self.runner = runner
        self.remediator = remediator
        self.approver = approver

    async def handle(self, alert, fault, config) -> AgentReport:
        blocked = 0
        # One diagnostic, so the runner path is exercised like a real agent's first step.
        diag = await self.runner.diagnose(
            DiagnoseRequest(incident_id=alert.alert_id, command=["docker", "ps", "-a"])
        )
        blocked += diag.state == "rejected"

        action_id = str(uuid.uuid4())
        params = dict(fault.spec.expected_params)
        token, waited = self.approver.approve(
            incident_id=alert.alert_id,
            action_id=action_id,
            action=fault.spec.expected_fix,
            params=params,
        )
        fix = await self.remediator.remediate(
            RemediateRequest(
                incident_id=alert.alert_id,
                action_id=action_id,
                action=fault.spec.expected_fix,
                params=params,
                approval_token=token,
                idempotency_key=action_id,
            )
        )
        blocked += fix.state == "rejected"
        ok = fix.state == "completed" and getattr(fix.result, "exit_code", 1) == 0
        return AgentReport(
            outcome="resolved" if ok else "escalated",
            tool_calls=2,
            blocked_actions=blocked,
            approval_wait_s=waited,
        )
