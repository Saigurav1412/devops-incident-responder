"""Pydantic models for every contract in docs/INTERFACES.md.

Section numbers in comments (§1, §2.2, ...) point at docs/INTERFACES.md so a reader can check
the code against the spec line by line.

Design notes that apply to the whole file:

* `extra="forbid"` on anything the executor receives. WHY: an attacker who controls what the
  diagnoser's LLM writes could try to smuggle extra fields (e.g. `"shell": true`) into a request.
  Forbidding unknown fields makes that a validation error instead of something silently ignored
  today and accidentally honoured by a later refactor.
* Models describe *shape*, not *policy*. Whether `["docker", "rm", "-f", "db"]` is allowed is the
  executor's allowlist's job (src/security/), not this file's. Keeping policy out of the contract
  means the rules can tighten without a contract change.
* Timestamps are timezone-aware UTC (`AwareDatetime`), because Firestore, LangSmith and the A2A
  spec all use UTC, and naive datetimes silently mix up local time on two laptops.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------------------------
# Small shared building blocks
# ---------------------------------------------------------------------------------------------

# Identifiers we generate (uuid4 strings) or that come from fixed catalogs. Bounded length and
# a conservative character set so an id can safely appear in logs, file names and Firestore paths.
Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")]


class _Strict(BaseModel):
    """Base for models that cross a trust boundary: unknown fields are an error."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------------------------
# §4.1 Alert: what starts an incident
# ---------------------------------------------------------------------------------------------


class Alert(_Strict):
    alert_id: Identifier
    source: Literal["healthcheck", "eval_harness", "manual"]
    service: Identifier
    # Written by our monitor/harness, not by the target app, so it's semi-trusted.
    # Still capped: an alert summary should never be a place to paste a log dump.
    summary: Annotated[str, Field(max_length=500)]
    fired_at: AwareDatetime


# ---------------------------------------------------------------------------------------------
# §1 Diagnoser graph state pieces
# ---------------------------------------------------------------------------------------------


class Hypothesis(_Strict):
    id: Identifier  # "h1", "h2", ...
    statement: str  # "postgres container is OOM-killed"
    evidence_needed: list[str] = Field(default_factory=list)
    status: Literal["open", "confirmed", "refuted"] = "open"
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0


class ProposedAction(_Strict):
    action_id: Identifier  # uuid4, unique per proposal; also the idempotency key
    action: Identifier  # name from the remediation catalog (§2.4)
    params: dict[str, str] = Field(default_factory=dict)
    rationale: str
    rollback: Identifier | None = None  # catalog action that undoes it, if any


class Observation(_Strict):
    """One diagnostic or remediation call and what came back (append-only in the state).

    Not spelled out field-by-field in INTERFACES §1; this is the minimal shape both sides need:
    which A2A task it was, what was asked, and the outcome. The raw output stays inside
    `result` and is untrusted data (log lines are attacker-controlled).
    """

    task_id: str  # A2A task id returned by the executor
    skill: Literal["diagnose", "remediate"]
    request: DiagnoseRequest | RemediateRequest
    state: Literal["completed", "rejected", "failed"]
    result: CommandResult | RejectionInfo | FailureInfo
    observed_at: AwareDatetime


class Budget(_Strict):
    """Counters the graph checks before every step (§1 budget caps). Limits come from config."""

    iterations: int = 0
    replans: int = 0
    tool_calls: int = 0
    tokens: int = 0
    started_at: AwareDatetime


class ApprovalDecision(_Strict):
    """§4.2 The value passed to LangGraph's Command(resume=...) after a Slack click."""

    action_id: Identifier
    decision: Literal["approve", "reject"]
    approver: Identifier  # Slack user id; must be in APPROVER_ALLOWLIST (checked by approval/)
    decided_at: AwareDatetime
    token: str | None = None  # §4.3, present only when approved


class IncidentState(TypedDict):
    """§1 LangGraph state. A TypedDict (not a BaseModel) because that's what LangGraph's
    StateGraph expects; the values inside are the validated models above."""

    incident_id: str
    alert: Alert
    recalled: list[IncidentRecord]
    hypotheses: list[Hypothesis]
    observations: list[Observation]
    proposed: ProposedAction | None
    approval: ApprovalDecision | None
    outcome: Literal["open", "resolved", "escalated", "rejected", "budget_exceeded"]
    budget: Budget


# ---------------------------------------------------------------------------------------------
# §2.2 A2A request payloads (diagnoser -> executor). Exactly one per A2A Message, in a data part.
# ---------------------------------------------------------------------------------------------


class DiagnoseRequest(_Strict):
    skill: Literal["diagnose"] = "diagnose"
    incident_id: Identifier
    # argv, never a shell string. e.g. ["docker", "logs", "--tail", "200", "api"]
    # Bounded so a runaway LLM can't send a 10,000-element argv.
    command: Annotated[
        list[Annotated[str, Field(max_length=512)]], Field(min_length=1, max_length=32)
    ]
    timeout_s: Annotated[int, Field(ge=1, le=120)] = 20


class RemediateRequest(_Strict):
    skill: Literal["remediate"] = "remediate"
    incident_id: Identifier
    action_id: Identifier  # the id the approval token was minted for
    action: Identifier  # catalog name
    params: dict[Identifier, Annotated[str, Field(max_length=512)]] = Field(default_factory=dict)
    approval_token: Annotated[str, Field(min_length=1, max_length=256)]
    idempotency_key: Identifier  # = action_id; the executor never runs the same key twice


# The executor parses the data part into exactly one of these, chosen by the `skill` field.
ExecutorRequest = Annotated[DiagnoseRequest | RemediateRequest, Field(discriminator="skill")]


# ---------------------------------------------------------------------------------------------
# §2.3 Executor results (executor -> diagnoser)
# ---------------------------------------------------------------------------------------------


class CommandResult(_Strict):
    """TASK_STATE_COMPLETED: the command or action ran (any exit code)."""

    exit_code: int
    stdout: str  # capped at MAX_OUTPUT_BYTES, tail kept; UNTRUSTED
    stderr: str  # same
    truncated: bool
    duration_ms: Annotated[int, Field(ge=0)]


class RejectionInfo(_Strict):
    """TASK_STATE_REJECTED: the executor refused to run it. Counted as a blocked unsafe action."""

    reason: str
    rule_id: str | None = None  # which allowlist rule fired; None for approval_invalid


class FailureInfo(_Strict):
    """TASK_STATE_FAILED: the executor tried and broke (timeout, docker down, bug)."""

    reason: str


# ---------------------------------------------------------------------------------------------
# §3 Long-term memory
# ---------------------------------------------------------------------------------------------


class IncidentRecord(_Strict):
    incident_id: Identifier
    created_at: AwareDatetime
    symptoms: str  # normalized alert + key observations, written by code
    root_cause: str  # the confirmed hypothesis statement
    fix: ProposedAction  # what actually worked
    verified: bool  # True only if check_health passed; save_memory refuses False
    embedding: list[float] = Field(default_factory=list)


# ---------------------------------------------------------------------------------------------
# §5 Eval harness
# ---------------------------------------------------------------------------------------------


class FaultSpec(_Strict):
    """§5.1 evals/faults/<id>/fault.yaml"""

    id: Identifier
    category: Literal["resource", "config", "dependency", "network", "deploy"]
    description: str
    inject: str = "inject.sh"
    oracle: str = "oracle.sh"  # exit 0 = healthy
    reset: str = "reset.sh"
    expected_fix: Identifier  # for analysis only; never shown to the agent
    timeout_s: Annotated[int, Field(ge=10, le=3600)] = 600
    repeat_of: Identifier | None = None  # memory-ablation pairs


class EpisodeRecord(_Strict):
    """§5.2 one line of evals/results/<date>/episodes.jsonl"""

    run_id: str
    fault_id: Identifier
    repeat: Annotated[int, Field(ge=1)]
    config: dict[str, bool | int | str]
    resolved: bool
    time_to_resolve_s: float | None  # None when not resolved
    replans: int
    tool_calls: int
    tokens_in: int
    tokens_out: int
    cost_usd: float
    blocked_actions: int
    outcome: Literal["resolved", "escalated", "rejected", "budget_exceeded", "timeout", "error"]
    langsmith_run_url: str | None = None


# Observation references models defined after it; resolve the forward references now.
Observation.model_rebuild()
