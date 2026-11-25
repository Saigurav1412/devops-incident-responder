"""Scripted stand-ins for the reasoner, the executor and the health check.

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
from diagnoser.ports import ExecutorClientError, FixProposal, Reasoned

ExecutorResult = CommandResult | RejectionInfo | FailureInfo


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

    * `diagnose_results`: exact argv tuple -> result. Unknown argv -> REJECTED, like the real
      executor's allowlist would for anything it wasn't told about.
    * `remediate_results`: action name -> result. Unknown action -> a successful CommandResult.
    * `unreachable=True` raises ExecutorClientError on every call (transport failure).
    Every request is recorded in `.calls` so tests can assert what was sent.
    """

    def __init__(
        self,
        *,
        diagnose_results: dict[tuple[str, ...], ExecutorResult] | None = None,
        remediate_results: dict[str, ExecutorResult] | None = None,
        unreachable: bool = False,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.diagnose_results = diagnose_results or {}
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
        result = self.diagnose_results.get(
            tuple(request.command), RejectionInfo(reason="not_in_fake_allowlist")
        )
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
        return [c for c in self.calls if isinstance(c, DiagnoseRequest)]


class ScriptedHealthCheck:
    """Returns the scripted booleans in order (last one repeats)."""

    def __init__(self, results: Sequence[bool]) -> None:
        self._script = _Script("health_check", results)

    async def __call__(self, state: IncidentState) -> bool:
        return self._script.next()

    @property
    def calls(self) -> int:
        return self._script.calls
