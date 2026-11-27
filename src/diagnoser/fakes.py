"""Scripted stand-ins for the reasoner and the executor.

WHAT: deterministic implementations of the ports in ports.py.
WHY:  so the graph's control flow (loops, caps, approval gate) can be tested and run locally
      with no LLM, no VM and no network (AGENTS.md rule 9). They will also let the eval harness
      dry-run the graph before the real executor exists.
NOT FOR PRODUCTION: nothing here talks to a real system, and the fake executor runs nothing.

Scripts are lists consumed in order. When a list runs out, its LAST entry repeats; that makes
"the reasoner keeps saying the same thing" easy to write, which is exactly the case the budget
caps exist for.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from config import Settings
from contracts import (
    CommandResult,
    DiagnoseRequest,
    FailureInfo,
    Hypothesis,
    IncidentState,
    Observation,
    RejectionInfo,
    RemediateRequest,
)
from diagnoser.llm import ChatMessage, ChatModelError, ChatResult
from diagnoser.nodes import health_check_argv
from diagnoser.ports import ExecutorClientError, FixProposal, PolicySummaries, Reasoned

ExecutorResult = CommandResult | RejectionInfo | FailureInfo

# The field default, not Settings(): Settings() would read a developer's real .env.
DEFAULT_HEALTH_URL: str = Settings.model_fields["health_url"].default

_HEALTHY = CommandResult(
    exit_code=0, stdout='{"status":"ok"}', stderr="", truncated=False, duration_ms=4
)
# curl -f exits 22 on an HTTP error status (e.g. the proxy's 503).
_UNHEALTHY = CommandResult(
    exit_code=22,
    stdout="",
    stderr="curl: (22) The requested URL returned error: 503",
    truncated=False,
    duration_ms=4,
)


class _Script[T]:
    """A list consumed in order whose last element repeats once the list is used up."""

    def __init__(self, name: str, items: Sequence[T]) -> None:
        self.name = name
        self.items = list(items)
        self.calls = 0

    def next(self) -> T:
        if not self.items:
            raise AssertionError(f"ScriptedReasoner.{self.name} was called but has no script")
        item = self.items[min(self.calls, len(self.items) - 1)]
        self.calls += 1
        return item


class ScriptedReasoner:
    """A Reasoner that replays fixed answers. `tokens_per_call` feeds the token budget."""

    def __init__(
        self,
        *,
        plans: Sequence[list[Hypothesis]] = (),
        commands: Sequence[list[str] | None] = (),
        assessments: Sequence[list[Hypothesis]] = (),
        proposals: Sequence[FixProposal] = (),
        tokens_per_call: int = 0,
    ) -> None:
        self.plans = _Script("plan", plans)
        self.commands = _Script("next_command", commands)
        self.assessments = _Script("assess", assessments)
        self.proposals = _Script("propose", proposals)
        self.tokens_per_call = tokens_per_call

    async def plan(self, state: IncidentState) -> Reasoned[list[Hypothesis]]:
        return Reasoned(self.plans.next(), self.tokens_per_call)

    async def next_command(self, state: IncidentState) -> Reasoned[list[str] | None]:
        return Reasoned(self.commands.next(), self.tokens_per_call)

    async def assess(self, state: IncidentState) -> Reasoned[list[Hypothesis]]:
        return Reasoned(self.assessments.next(), self.tokens_per_call)

    async def propose(self, state: IncidentState) -> Reasoned[FixProposal]:
        return Reasoned(self.proposals.next(), self.tokens_per_call)


def _task_state(result: ExecutorResult) -> str:
    """Map a result model to the Observation state, mirroring INTERFACES §2.3."""
    if isinstance(result, CommandResult):
        return "completed"
    if isinstance(result, RejectionInfo):
        return "rejected"
    return "failed"


class FakeExecutorClient:
    """An ExecutorClient that answers from tables instead of running anything.

    * `diagnose_results`: exact argv tuple -> result, or a list of results returned in order
      (last one repeats). Unknown argv -> REJECTED, like the real executor's allowlist would
      for anything it wasn't told about.
    * `health`: what check_health's fixed curl returns on each call, in order (last repeats).
      True = exit 0, False = exit 22 (curl -f on a 503). Default: always healthy.
    * `remediate_results`: action name -> result. Unknown action -> a successful CommandResult.
    * `unreachable=True` raises ExecutorClientError on every call (transport failure).
    Every request is recorded in `.calls` so tests can assert what was sent.
    """

    def __init__(
        self,
        *,
        diagnose_results: dict[tuple[str, ...], ExecutorResult | Sequence[ExecutorResult]]
        | None = None,
        remediate_results: dict[str, ExecutorResult] | None = None,
        health: Sequence[bool] = (True,),
        health_url: str = DEFAULT_HEALTH_URL,
        unreachable: bool = False,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.health_argv = tuple(health_check_argv(health_url))
        scripted = dict(diagnose_results or {})
        scripted.setdefault(self.health_argv, [_HEALTHY if ok else _UNHEALTHY for ok in health])
        # Every entry becomes a _Script, so single results and sequences behave the same way.
        self._diagnose_scripts = {
            argv: _Script(
                f"diagnose{list(argv)}", result if isinstance(result, list | tuple) else [result]
            )
            for argv, result in scripted.items()
        }
        self.remediate_results = remediate_results or {}
        self.unreachable = unreachable
        self.clock = clock
        self.calls: list[DiagnoseRequest | RemediateRequest] = []

    def _observe(
        self, request: DiagnoseRequest | RemediateRequest, result: ExecutorResult
    ) -> Observation:
        return Observation(
            task_id=f"fake-task-{len(self.calls)}",
            skill=request.skill,
            request=request,
            state=_task_state(result),
            result=result,
            observed_at=self.clock(),
        )

    async def diagnose(self, request: DiagnoseRequest) -> Observation:
        self.calls.append(request)
        if self.unreachable:
            raise ExecutorClientError("connection refused (fake)")
        script = self._diagnose_scripts.get(tuple(request.command))
        result = script.next() if script else RejectionInfo(reason="not_in_fake_allowlist")
        return self._observe(request, result)

    async def remediate(self, request: RemediateRequest) -> Observation:
        self.calls.append(request)
        if self.unreachable:
            raise ExecutorClientError("connection refused (fake)")
        result = self.remediate_results.get(
            request.action,
            CommandResult(exit_code=0, stdout="ok", stderr="", truncated=False, duration_ms=5),
        )
        return self._observe(request, result)

    @property
    def remediations(self) -> list[RemediateRequest]:
        return [c for c in self.calls if isinstance(c, RemediateRequest)]

    @property
    def diagnostics(self) -> list[DiagnoseRequest]:
        """Diagnostics the reasoner chose (the health check's fixed curl is excluded)."""
        return [
            c
            for c in self.calls
            if isinstance(c, DiagnoseRequest) and tuple(c.command) != self.health_argv
        ]

    @property
    def health_checks(self) -> int:
        return sum(
            1
            for c in self.calls
            if isinstance(c, DiagnoseRequest) and tuple(c.command) == self.health_argv
        )


# ---------------------------------------------------------------------------------------------
# LLM seam fakes (for LLMReasoner tests and dry runs without an API key)
# ---------------------------------------------------------------------------------------------


class FakeChatModel:
    """A ChatModel that answers from a script, in order, and records every call.

    Script items: a reply string, or a ChatModelError instance to raise. Each reply costs
    `input_tokens` + `output_tokens`. Running past the end of the script is a test bug.
    """

    def __init__(
        self,
        script: Sequence[str | ChatModelError],
        input_tokens: int = 100,
        output_tokens: int = 20,
    ) -> None:
        self.script = list(script)
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        schema: dict[str, Any],
        schema_name: str,
        max_output_tokens: int,
    ) -> ChatResult:
        self.calls.append(
            {"messages": list(messages), "schema": schema, "schema_name": schema_name}
        )
        if not self.script:
            raise AssertionError("FakeChatModel script exhausted")
        item = self.script.pop(0)
        if isinstance(item, ChatModelError):
            raise item
        return ChatResult(item, self.input_tokens, self.output_tokens)


class StaticPolicy:
    """A PolicySource with fixed text, or one that fails like an unreachable executor."""

    def __init__(
        self,
        diagnose: str = "DIAGNOSE-POLICY",
        remediate: str = "REMEDIATE-POLICY",
        *,
        fail: bool = False,
    ) -> None:
        self.summaries = PolicySummaries(diagnose, remediate)
        self.fail = fail
        self.calls = 0

    async def policy_summaries(self) -> PolicySummaries:
        self.calls += 1
        if self.fail:
            raise ExecutorClientError("agent_card_unavailable: ConnectError (fake)")
        return self.summaries
