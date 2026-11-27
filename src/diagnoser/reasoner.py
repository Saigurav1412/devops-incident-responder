"""The LLM-backed Reasoner: prompts in, validated answers out. Provider-independent.

WHAT: `LLMReasoner` implements the four Reasoner methods (plan, next_command, assess, propose) on
      top of any `ChatModel` (diagnoser/llm.py), plus `render_incident()`, which turns the graph
      state into the user message.
WHY:  this is where the LLM meets attacker-controlled text (log lines, config files, alert text,
      memory). Three rules hold here in code, whatever the model does (AGENTS.md rules 1 and 2):
        1. Untrusted text only reaches the model inside fenced data blocks with a random nonce,
           JSON-encoded, so it can't close the fence or pose as our instructions.
        2. Every answer is parsed into a strict Pydantic model (unknown fields, wrong types and
           oversized strings are errors). One repair attempt, then a safe default.
        3. Every call's token usage is returned, so the graph's token cap sees it.
      What this module does NOT decide: whether a command or fix is allowed (the executor's
      allowlist and approval token), and which status changes are accepted (apply_assessment).
HOW:  system message = common prompt (with the executor's policy summaries from its Agent Card)
      + the role prompt; user message = render_incident(state).

Safe defaults when the model fails (unreachable, or invalid twice):
  plan -> []            the graph escalates on an empty plan
  next_command -> None  verify decides with the evidence it has
  assess -> []          omitted hypotheses stay unchanged (apply_assessment)
  propose -> ReasonerError   no safe default; propose_fix escalates
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Callable
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

import prompts
from contracts import (
    CommandResult,
    DiagnoseRequest,
    FailureInfo,
    Hypothesis,
    IncidentRecord,
    IncidentState,
    Observation,
    RejectionInfo,
)
from diagnoser.llm import ChatMessage, ChatModel, ChatModelError
from diagnoser.ports import (
    ExecutorClientError,
    FixProposal,
    PolicySource,
    PolicySummaries,
    Reasoned,
    ReasonerError,
)
from obs.logging import get_logger

log = get_logger(__name__)

PROMPT_VERSION = "v1"

# Prompt-size caps. WHY: a log dump must not crowd out the instructions or blow the token cap in
# one call. Tails, because the newest lines of a log are usually the ones that matter.
OUTPUT_TAIL_CHARS = 2_000  # per stdout/stderr, per observation
RECENT_OBSERVATIONS = 12  # entries shown with output; older ones are listed without it
TEXT_CHARS = 300  # recalled-memory fields, hypothesis statements
POLICY_CHARS = 8_000  # each policy summary (Om's are ~1 KB today)
BAD_REPLY_CHARS = 2_000  # how much of an invalid reply is echoed back in the repair turn


# ---------------------------------------------------------------------------------------------
# Answer schemas: what the model must return. Strict, bounded, no free-form dicts.
# ---------------------------------------------------------------------------------------------
# WHY lists of {name, value} instead of dicts for params: strict structured-output modes (e.g.
# OpenAI's) can't express arbitrary-key objects, and a list makes duplicate names detectable.

_Id = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")]
_Short = Annotated[str, Field(min_length=1, max_length=TEXT_CHARS)]


class _Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlannedHypothesis(_Answer):
    statement: _Short
    evidence_needed: list[Annotated[str, Field(max_length=200)]] = Field(
        default_factory=list, max_length=5
    )


class PlanAnswer(_Answer):
    hypotheses: list[PlannedHypothesis] = Field(max_length=10)


_Arg = Annotated[str, Field(min_length=1, max_length=512)]


class CommandAnswer(_Answer):
    # Required, so "nothing useful left" has to be said explicitly as null.
    command: Annotated[list[_Arg], Field(min_length=1, max_length=32)] | None
    reason: Annotated[str, Field(max_length=TEXT_CHARS)] = ""

    @field_validator("command")
    @classmethod
    def _no_nul(cls, argv: list[str] | None) -> list[str] | None:
        # A NUL can't be passed to exec and has no business in an argv; reject it here so the
        # model gets a clear repair message instead of an executor error.
        if argv is not None and any("\x00" in a for a in argv):
            raise ValueError("argv elements must not contain NUL")
        return argv


class Assessment(_Answer):
    id: _Id
    status: Literal["open", "confirmed", "refuted"]
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    reason: Annotated[str, Field(max_length=TEXT_CHARS)] = ""


class AssessAnswer(_Answer):
    assessments: list[Assessment] = Field(max_length=20)


class Param(_Answer):
    name: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")]
    value: Annotated[str, Field(min_length=1, max_length=256)]


class ProposeAnswer(_Answer):
    action: _Id
    params: list[Param] = Field(max_length=8)
    rationale: Annotated[str, Field(min_length=1, max_length=500)]
    rollback: _Id | None = None

    @field_validator("params")
    @classmethod
    def _unique_names(cls, params: list[Param]) -> list[Param]:
        names = [p.name for p in params]
        if len(names) != len(set(names)):
            raise ValueError("parameter names must be unique")
        return params


def parse_answer[A: _Answer](text: str, model: type[A]) -> A:
    """Parse one reply into `model`. Raises ValueError with a message safe to show the model
    again (field locations and rule names only, never the input values)."""
    body = text.strip()
    # Tolerate a Markdown fence around the JSON: common, harmless, and cheaper than a repair turn.
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", body, flags=re.S)
    if fenced:
        body = fenced.group(1)
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        raise ValueError("the reply is not a single valid JSON object") from None
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(map(str, e['loc'])) or '(root)'}: {e['msg']}" for e in exc.errors()[:8]
        )
        raise ValueError(f"the JSON doesn't match the schema: {problems}") from None


# ---------------------------------------------------------------------------------------------
# Rendering the incident (the user message)
# ---------------------------------------------------------------------------------------------


def _tail(text: str, limit: int = OUTPUT_TAIL_CHARS) -> str:
    return text if len(text) <= limit else "[...cut...]" + text[-limit:]


def _clip(text: str, limit: int = TEXT_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + "[...cut...]"


# U+0085 NEXT LINE, U+2028 LINE SEPARATOR, U+2029 PARAGRAPH SEPARATOR -> JSON \u escapes.
_LINE_SEPARATOR_ESCAPES = {0x85: "\\u0085", 0x2028: "\\u2028", 0x2029: "\\u2029"}


class _Fence:
    """Wraps untrusted payloads in nonce-marked blocks.

    WHY a random nonce per message: the data can't contain the closing marker, because it can't
    know it. JSON encoding turns newlines into `\\n`, so a fake marker inside the data can't start
    a line either. Belt and braces: any occurrence of the nonce is stripped from the payload.
    """

    def __init__(self, nonce: str) -> None:
        self.nonce = nonce

    def wrap(self, payload: Any) -> str:
        body = json.dumps(payload, ensure_ascii=False).replace(self.nonce, "")
        # JSON escapes \n and \r, but with ensure_ascii=False it leaves the Unicode line
        # separators raw, and some tokenizers treat them as line breaks. Escape them so "a fake
        # marker can't start a line" holds (Om's review on #14). Same JSON meaning, still parses.
        body = body.translate(_LINE_SEPARATOR_ESCAPES)
        return (
            f"===BEGIN UNTRUSTED DATA {self.nonce}===\n{body}\n"
            f"===END UNTRUSTED DATA {self.nonce}==="
        )


def _observation_payload(n: int, obs: Observation, *, with_output: bool) -> dict[str, Any]:
    request = obs.request
    item: dict[str, Any] = {"n": n, "skill": obs.skill, "state": obs.state}
    if isinstance(request, DiagnoseRequest):
        item["argv"] = list(request.command)
    else:
        # Built field by field: the approval token must never reach a prompt (or a trace).
        item["action"] = request.action
        item["params"] = dict(request.params)
    result = obs.result
    if isinstance(result, CommandResult):
        item["exit_code"] = result.exit_code
        if with_output:
            item["stdout_tail"] = _tail(result.stdout)
            item["stderr_tail"] = _tail(result.stderr)
            item["truncated"] = (
                result.truncated
                or len(result.stdout) > OUTPUT_TAIL_CHARS
                or len(result.stderr) > OUTPUT_TAIL_CHARS
            )
    elif isinstance(result, RejectionInfo):
        item["rejected_reason"] = _clip(result.reason)
        item["rule_id"] = result.rule_id
    elif isinstance(result, FailureInfo):
        item["failed_reason"] = _clip(result.reason)
    return item


def _same_result(a: Observation, b: Observation) -> bool:
    """Same request and an identical answer, so merging them loses nothing.

    WHY: check_health retries its fixed curl (PR #16), so a fix that didn't work leaves up to ~11
    identical observations. Listed one by one they would fill the output window and push the
    diagnostic evidence out of the prompt right when the replan needs it. Only exact repeats are
    merged: a different exit code, state or output starts a new entry.
    """
    return (
        a.skill == b.skill
        and a.request.model_dump(exclude={"approval_token"})
        == b.request.model_dump(exclude={"approval_token"})
        and a.state == b.state
        and a.result == b.result
    )


def _merge_repeats(observations: list[Observation]) -> list[tuple[int, int, Observation]]:
    """Runs of identical consecutive observations -> (first n, last n, observation), 1-based."""
    runs: list[tuple[int, int, Observation]] = []
    for i, o in enumerate(observations, start=1):
        if runs and _same_result(runs[-1][2], o):
            runs[-1] = (runs[-1][0], i, runs[-1][2])
        else:
            runs.append((i, i, o))
    return runs


def _recalled_payload(record: IncidentRecord) -> dict[str, Any]:
    return {
        "symptoms": _clip(record.symptoms),
        "root_cause": _clip(record.root_cause),
        "fix_that_worked": {"action": record.fix.action, "params": dict(record.fix.params)},
    }


def render_incident(state: IncidentState, settings_view: dict[str, int], nonce: str) -> str:
    """The user message: alert, memory, hypotheses and observations, each in a data block.

    Hypothesis statements are fenced too: the model wrote them, but possibly while reading
    injected text, so they get no more trust than the data they came from.
    """
    fence = _Fence(nonce)
    alert = state["alert"]
    budget = state["budget"]
    parts = [
        "## Alert",
        fence.wrap(
            {
                "service": alert.service,
                "source": alert.source,
                "summary": alert.summary,
                "fired_at": alert.fired_at.isoformat(),
            }
        ),
        "",
        "## Past incidents from memory (each fix is only a hypothesis here)",
        fence.wrap([_recalled_payload(r) for r in state["recalled"]])
        if state["recalled"]
        else "none",
        "",
        "## Current hypotheses",
        fence.wrap(
            [
                {
                    "id": h.id,
                    "statement": _clip(h.statement),
                    "status": h.status,
                    "confidence": h.confidence,
                    "evidence_needed": [_clip(e, 200) for e in h.evidence_needed],
                }
                for h in state["hypotheses"]
            ]
        )
        if state["hypotheses"]
        else "none yet",
        "",
    ]
    observations = state["observations"]
    runs = _merge_repeats(observations)
    # The output window counts ENTRIES (after merging), not raw observations.
    first_full = max(0, len(runs) - RECENT_OBSERVATIONS)
    payload = []
    for j, (first, last, o) in enumerate(runs):
        item = _observation_payload(first, o, with_output=j >= first_full)
        if last > first:
            item["n_last"] = last
            item["repeated"] = last - first + 1
        payload.append(item)
    parts += [
        f"## Observations ({len(observations)} in {len(runs)} entries, oldest first; identical "
        f"consecutive results merged with a `repeated` count; output shown for the last "
        f"{min(len(runs), RECENT_OBSERVATIONS)} entries)",
        fence.wrap(payload) if payload else "none yet",
        "",
        "## Budget",
        f"iterations {budget.iterations}/{settings_view['max_iterations']}, "
        f"tool calls {budget.tool_calls}/{settings_view['max_tool_calls']}, "
        f"replans {budget.replans}/{settings_view['max_replans']}",
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------------------------
# The reasoner
# ---------------------------------------------------------------------------------------------

_ROLES = {
    "plan": ("diagnoser_planner", PlanAnswer),
    "next_command": ("diagnoser_commander", CommandAnswer),
    "assess": ("diagnoser_assessor", AssessAnswer),
    "propose": ("diagnoser_proposer", ProposeAnswer),
}


class LLMReasoner:
    """Reasoner over a ChatModel. One instance per process; stateless between incidents
    except for the cached executor policy."""

    def __init__(
        self,
        chat: ChatModel,
        policy: PolicySource,
        *,
        max_hypotheses: int,
        max_iterations: int,
        max_tool_calls: int,
        max_replans: int,
        max_output_tokens: int = 1_500,
        repair_attempts: int = 1,
        nonce: Callable[[], str] = lambda: secrets.token_hex(8),
        prompt_version: str = PROMPT_VERSION,
    ) -> None:
        self._chat = chat
        self._policy_source = policy
        self._policy: PolicySummaries | None = None
        self._max_hypotheses = max_hypotheses
        self._limits = {
            "max_iterations": max_iterations,
            "max_tool_calls": max_tool_calls,
            "max_replans": max_replans,
        }
        self._max_output_tokens = max_output_tokens
        self._repair_attempts = repair_attempts
        self._nonce = nonce
        self._common = prompts.load("diagnoser_common", prompt_version)
        self._role_prompts = {
            role: prompts.load(name, prompt_version) for role, (name, _) in _ROLES.items()
        }

    @property
    def prompt_refs(self) -> dict[str, str]:
        """Which prompt texts are in use (`name.version@sha`), for traces and eval results."""
        return {"common": self._common.ref} | {r: p.ref for r, p in self._role_prompts.items()}

    async def _policy_text(self) -> PolicySummaries:
        # Cached only on success, so a transient card failure is retried on the next call.
        # Cached for the process lifetime: after an executor redeploy with a changed policy,
        # restart the diagnoser too (the executor still enforces the real policy meanwhile, so
        # the worst case is extra rejections, not a bypass). Noted in INTERFACES §1.
        if self._policy is None:
            self._policy = await self._policy_source.policy_summaries()
        return self._policy

    async def _ask[A: _Answer](
        self, role: str, state: IncidentState, model: type[A]
    ) -> tuple[A | None, int]:
        """One question to the model, with up to `repair_attempts` corrections.

        Returns (answer or None, tokens used). Never raises for model or policy failures: the
        caller applies its safe default.
        """
        ctx = {"incident_id": state["incident_id"], "role": role}
        try:
            policy = await self._policy_text()
        except ExecutorClientError as exc:
            log.warning("executor policy unavailable", extra={**ctx, "error": str(exc)})
            return None, 0

        system = (
            self._common.text.format(
                diagnose_policy=_clip(policy.diagnose, POLICY_CHARS),
                remediate_policy=_clip(policy.remediate, POLICY_CHARS),
            )
            + "\n\n"
            + self._role_prompts[role].text.format(max_hypotheses=self._max_hypotheses)
        )
        messages = [
            ChatMessage("system", system),
            ChatMessage("user", render_incident(state, self._limits, self._nonce())),
        ]
        schema = model.model_json_schema()
        tokens = 0
        for attempt in range(1 + self._repair_attempts):
            try:
                result = await self._chat.complete(
                    messages,
                    schema=schema,
                    schema_name=model.__name__,
                    max_output_tokens=self._max_output_tokens,
                )
            except ChatModelError as exc:
                # Not retried here: provider-level retries/backoff are the adapter's job.
                log.warning("llm call failed", extra={**ctx, "attempt": attempt, "error": str(exc)})
                return None, tokens
            tokens += result.tokens
            try:
                answer = parse_answer(result.text, model)
            except ValueError as exc:
                log.warning(
                    "llm answer invalid", extra={**ctx, "attempt": attempt, "error": str(exc)}
                )
                messages = [
                    *messages,
                    ChatMessage("assistant", _clip(result.text, BAD_REPLY_CHARS)),
                    ChatMessage(
                        "user",
                        f"That reply was rejected: {exc}. Reply again with only the JSON object.",
                    ),
                ]
                continue
            log.info("llm answer ok", extra={**ctx, "attempt": attempt, "tokens": tokens})
            return answer, tokens
        return None, tokens

    async def plan(self, state: IncidentState) -> Reasoned[list[Hypothesis]]:
        answer, tokens = await self._ask("plan", state, PlanAnswer)
        if answer is None:
            return Reasoned([], tokens)
        # Placeholder ids: plan_hypotheses renumbers them (h1, h2, ...) in code.
        hypotheses = [
            Hypothesis(id=f"new{i + 1}", statement=h.statement, evidence_needed=h.evidence_needed)
            for i, h in enumerate(answer.hypotheses)
        ]
        return Reasoned(hypotheses, tokens)

    async def next_command(self, state: IncidentState) -> Reasoned[list[str] | None]:
        answer, tokens = await self._ask("next_command", state, CommandAnswer)
        return Reasoned(answer.command if answer else None, tokens)

    async def assess(self, state: IncidentState) -> Reasoned[list[Hypothesis]]:
        answer, tokens = await self._ask("assess", state, AssessAnswer)
        if answer is None:
            return Reasoned([], tokens)  # omitted = unchanged
        current = {h.id: h for h in state["hypotheses"]}
        assessed = []
        for a in answer.assessments:
            known = current.get(a.id)
            if known is None:
                # Passed through so apply_assessment logs it as ignored (a possible injection
                # signal) rather than this function silently dropping it.
                assessed.append(
                    Hypothesis(id=a.id, statement="", status=a.status, confidence=a.confidence)
                )
            else:
                assessed.append(
                    known.model_copy(update={"status": a.status, "confidence": a.confidence})
                )
        return Reasoned(assessed, tokens)

    async def propose(self, state: IncidentState) -> Reasoned[FixProposal]:
        answer, tokens = await self._ask("propose", state, ProposeAnswer)
        if answer is None:
            raise ReasonerError("no valid fix proposal from the model", tokens=tokens)
        proposal = FixProposal(
            action=answer.action,
            params={p.name: p.value for p in answer.params},
            rationale=answer.rationale,
            rollback=answer.rollback,
        )
        return Reasoned(proposal, tokens)
