"""The diagnoser's graph nodes and routing functions (INTERFACES §1).

HOW LangGraph uses these:
  * A *node* is an async function `state -> partial update`. It returns only the keys it changed;
    LangGraph merges that dict into the IncidentState and saves a checkpoint.
  * A *router* is a plain function `state -> next node name`. graph.py attaches routers to
    conditional edges. Routers never change state; they only read it and choose where to go.

Node names are fixed by INTERFACES §1 because they appear in LangSmith traces and in the README's
code map. graph.py imports the NODE_* constants below, so a name can't be typo'd in one place.

Every router that leaves a "work" node checks the budget first (see `_guarded`). That is the
code-level guarantee behind AGENTS.md rule 5: no path through the graph can loop forever.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from langgraph.types import interrupt

from contracts import (
    ApprovalDecision,
    Budget,
    CommandResult,
    DiagnoseRequest,
    FailureInfo,
    Hypothesis,
    IncidentRecord,
    IncidentState,
    Observation,
    ProposedAction,
    RemediateRequest,
)
from diagnoser.budget import budget_exceeded_reason
from diagnoser.ports import DiagnoserDeps, ExecutorClientError
from obs.logging import get_logger

log = get_logger(__name__)

# Fixed node names (INTERFACES §1) plus the three terminal nodes that set the outcome.
NODE_INGEST = "ingest_alert"
NODE_RECALL = "recall_memory"
NODE_PLAN = "plan_hypotheses"
NODE_DIAGNOSE = "diagnose"
NODE_VERIFY = "verify_hypothesis"
NODE_PROPOSE = "propose_fix"
NODE_APPROVAL = "request_approval"
NODE_EXECUTE = "execute_fix"
NODE_HEALTH = "check_health"
NODE_SAVE = "save_memory"
NODE_ESCALATE = "escalate"
NODE_BUDGET = "budget_exceeded"
# request_approval routes here on "reject"; it's END, but named so the router's return type says so.
ROUTE_REJECTED = "rejected"

# Payload type marker so the Slack side (approval/) can recognise what an interrupt is asking for.
APPROVAL_INTERRUPT_KIND = "approval_request"

# What replaces the approval token once execute_fix has used it (see execute_fix).
REDACTED = "redacted"

# check_health's curl timeout. The argv must match the executor's allowlist exactly
# (src/security/allowlist.yaml, curl.max_time_s >= this; Om's test pins this same argv).
HEALTH_MAX_TIME_S = 5


def health_check_argv(health_url: str) -> list[str]:
    """The ONE command check_health runs. Built by code from config, never by the LLM.

    -f: HTTP status >= 400 becomes a non-zero exit code, so "unhealthy" = exit code != 0.
    -sS: no progress bar, but still print errors (useful evidence in the observation).
    """
    return ["curl", "-fsS", "--max-time", str(HEALTH_MAX_TIME_S), health_url]


def apply_assessment(
    current: list[Hypothesis], assessed: list[Hypothesis]
) -> tuple[list[Hypothesis], list[str]]:
    """Merge the reasoner's re-scoring into the current hypotheses, under rules code enforces.

    WHY: verify_hypothesis used to take the reasoner's list as-is. An LLM (or a prompt injection
    in a log line it read) could then flip a refuted hypothesis back to open, invent new ids,
    drop hypotheses, or reword a statement, quietly undoing the "refuted stays refuted" rule
    that plan_hypotheses enforces. (Raised by Om on the diagnoser PR.)

    Rules:
      * only hypotheses that already exist (matched by id) can change; unknown ids are ignored
      * only `status` and `confidence` change; `statement` / `evidence_needed` are kept
      * a refuted hypothesis stays refuted, whatever the reasoner says
      * a hypothesis the reasoner left out is kept unchanged (dropping is not a way to forget)

    Returns (merged list in the original order, human-readable list of ignored changes).
    """
    by_id = {h.id: h for h in assessed}
    known_ids = {h.id for h in current}
    ignored = [f"unknown id {h.id}" for h in assessed if h.id not in known_ids]

    merged: list[Hypothesis] = []
    for hypothesis in current:
        update = by_id.get(hypothesis.id)
        if update is None:
            merged.append(hypothesis)
            continue
        if hypothesis.status == "refuted" and update.status != "refuted":
            ignored.append(f"un-refute {hypothesis.id}")
            merged.append(hypothesis)
            continue
        if update.statement != hypothesis.statement:
            ignored.append(f"reworded {hypothesis.id}")
        merged.append(
            hypothesis.model_copy(update={"status": update.status, "confidence": update.confidence})
        )
    return merged, ignored


class DiagnoserNodes:
    """All nodes and routers, sharing one DiagnoserDeps.

    WHY a class instead of free functions: every node needs the reasoner/executor/settings.
    Binding them once here keeps each node's signature to just `(state)`, which is what
    LangGraph's `add_node` expects.
    """

    def __init__(self, deps: DiagnoserDeps) -> None:
        self.deps = deps

    # -----------------------------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------------------------

    def _log(self, state: IncidentState, message: str, **fields: Any) -> None:
        # incident_id on every line so Cloud Logging can filter one incident's whole story.
        log.info(message, extra={"incident_id": state["incident_id"], **fields})

    @staticmethod
    def _add_budget(state: IncidentState, **increments: int) -> Budget:
        """Return a NEW Budget with counters increased. Never mutate the model in the state:
        LangGraph checkpoints by value, and mutating in place makes checkpoints lie."""
        budget = state["budget"]
        return budget.model_copy(
            update={name: getattr(budget, name) + amount for name, amount in increments.items()}
        )

    async def _call_executor(
        self, state: IncidentState, request: DiagnoseRequest | RemediateRequest
    ) -> Observation:
        """Send one request; turn an ExecutorClientError into a 'failed' Observation.

        ExecutorClientError covers both "couldn't talk to the executor" (down, timeout, auth) and
        "the executor's answer was malformed" (see diagnoser/a2a_client.py). The reason text
        says which, e.g. "executor_error: a2a_call_failed: ..." or "executor_error:
        malformed_task: ...".

        WHY not let the exception propagate: an executor problem is something the reasoner
        should see (it may explain the incident, or argue for escalation). Crashing the graph
        would lose the incident's checkpointed progress for a transient network error.
        """
        try:
            if isinstance(request, DiagnoseRequest):
                return await self.deps.executor.diagnose(request)
            return await self.deps.executor.remediate(request)
        except ExecutorClientError as exc:
            self._log(state, "executor call failed", error=str(exc), skill=request.skill)
            return Observation(
                task_id="none",  # there is no A2A task: the request never got a Task back
                skill=request.skill,
                request=request,
                state="failed",
                result=FailureInfo(reason=f"executor_error: {exc}"),
                observed_at=self.deps.clock(),
            )

    # -----------------------------------------------------------------------------------------
    # Nodes, in graph order
    # -----------------------------------------------------------------------------------------

    async def ingest_alert(self, state: IncidentState) -> dict[str, Any]:
        """Entry point. The alert was already validated as an `Alert` model by new_incident_state,
        so this node only marks the incident open and logs it (the first line of its trace)."""
        alert = state["alert"]
        self._log(state, "incident opened", service=alert.service, alert_id=alert.alert_id)
        return {"outcome": "open"}

    async def recall_memory(self, state: IncidentState) -> dict[str, Any]:
        """Look up similar past incidents. MEMORY_ENABLED=false (the ablation switch) skips it."""
        if not self.deps.settings.memory_enabled:
            return {"recalled": []}
        alert = state["alert"]
        recalled = self.deps.memory.find_similar(f"{alert.service} {alert.summary}", k=3)
        self._log(state, "memory recalled", count=len(recalled))
        return {"recalled": recalled}

    async def plan_hypotheses(self, state: IncidentState) -> dict[str, Any]:
        """Ask the reasoner for hypotheses. Also the *replan* step when evidence disagreed.

        Self-correction rules enforced here, in code:
          * refuted hypotheses are kept, so the reasoner sees what was already ruled out and the
            trace shows the correction;
          * new hypotheses get fresh code-assigned ids (h1, h2, ...), so an id never refers to two
            different statements;
          * at most MAX_HYPOTHESES new ones are kept (an ablation switch, INTERFACES §5);
          * any previous proposal/approval is cleared: a new plan needs a new approval.
        """
        is_replan = bool(state["hypotheses"])
        answer = await self.deps.reasoner.plan(state)

        kept = [h for h in state["hypotheses"] if h.status == "refuted"]
        new = answer.value[: self.deps.settings.max_hypotheses]
        renumbered = [
            h.model_copy(update={"id": f"h{len(kept) + i + 1}", "status": "open"})
            for i, h in enumerate(new)
        ]

        increments = {"tokens": answer.tokens}
        if is_replan:
            increments["replans"] = 1
        self._log(state, "hypotheses planned", count=len(renumbered), replan=is_replan)
        return {
            "hypotheses": kept + renumbered,
            "proposed": None,
            "approval": None,
            "budget": self._add_budget(state, **increments),
        }

    async def diagnose(self, state: IncidentState) -> dict[str, Any]:
        """Run ONE read-only diagnostic the reasoner picks, and record what came back.

        The argv goes to the executor as data in a DiagnoseRequest. This node does not judge
        whether the command is safe: the executor's allowlist does that (INTERFACES §2.4), so a
        prompt-injected reasoner here can at worst get a REJECTED observation back.
        """
        answer = await self.deps.reasoner.next_command(state)
        if answer.value is None:
            # Nothing more worth checking; verify_hypothesis decides with what we have.
            return {"budget": self._add_budget(state, tokens=answer.tokens)}

        request = DiagnoseRequest(incident_id=state["incident_id"], command=answer.value)
        observation = await self._call_executor(state, request)
        self._log(state, "diagnostic ran", argv=request.command, task_state=observation.state)
        return {
            # Append-only: build a new list rather than .append(), for the same checkpoint reason
            # as _add_budget.
            "observations": [*state["observations"], observation],
            "budget": self._add_budget(state, tokens=answer.tokens, tool_calls=1),
        }

    async def verify_hypothesis(self, state: IncidentState) -> dict[str, Any]:
        """Re-score hypotheses against the evidence. One call = one iteration of the budget.

        The reasoner only *suggests* new statuses; apply_assessment decides what is accepted.
        """
        answer = await self.deps.reasoner.assess(state)
        merged, ignored = apply_assessment(state["hypotheses"], answer.value)
        if ignored:
            # Worth a log line: repeated attempts to un-refute may be a sign of prompt injection.
            self._log(state, "assessment changes ignored", ignored=ignored)
        self._log(state, "hypotheses assessed", statuses={h.id: h.status for h in merged})
        return {
            "hypotheses": merged,
            "budget": self._add_budget(state, tokens=answer.tokens, iterations=1),
        }

    async def propose_fix(self, state: IncidentState) -> dict[str, Any]:
        """Turn the reasoner's suggestion into a ProposedAction with a code-generated action_id."""
        answer = await self.deps.reasoner.propose(state)
        proposal = answer.value
        proposed = ProposedAction(
            action_id=str(uuid.uuid4()),
            action=proposal.action,
            params=proposal.params,
            rationale=proposal.rationale,
            rollback=proposal.rollback,
        )
        self._log(state, "fix proposed", action=proposed.action, action_id=proposed.action_id)
        return {"proposed": proposed, "budget": self._add_budget(state, tokens=answer.tokens)}

    async def request_approval(self, state: IncidentState) -> dict[str, Any]:
        """Pause the graph until a human decides (LangGraph `interrupt`).

        HOW interrupt works: the first time this node runs, `interrupt(payload)` stops the graph
        and the checkpointer saves the state. The caller gets `payload` back under
        `__interrupt__`. Later, the Slack handler resumes the same thread with
        `Command(resume=ApprovalDecision(...))`; LangGraph re-runs this node from the top and
        this time `interrupt(...)` *returns* that decision instead of pausing.

        The diagnoser never mints a token (AGENTS.md rule 4). It only checks that the decision
        is for THIS proposal and from an allowed approver; the executor checks the token itself.
        A decision that fails those checks is ignored and we ask again, so a stale click on an
        older proposal can never approve the current one.
        """
        proposed = state["proposed"]
        if proposed is None:  # unreachable by graph construction; fail loudly if that changes
            raise RuntimeError("request_approval reached without a proposed action")

        payload = {
            "kind": APPROVAL_INTERRUPT_KIND,
            "incident_id": state["incident_id"],
            "proposed": proposed.model_dump(mode="json"),
            # When the human was asked. The Slack handler refuses clicks older than
            # APPROVAL_TTL_S (INTERFACES §4.2). LangGraph stores the payload of the interrupt
            # that actually paused the graph, so later re-runs of this node don't move it.
            "requested_at": self.deps.clock().isoformat(),
        }
        while True:
            decision = ApprovalDecision.model_validate(interrupt(payload))
            problem = self._approval_problem(decision, proposed)
            if problem is None:
                break
            self._log(state, "approval ignored", reason=problem, approver=decision.approver)

        self._log(state, "approval decided", decision=decision.decision, approver=decision.approver)
        update: dict[str, Any] = {"approval": decision}
        if decision.decision == "reject":
            update["outcome"] = "rejected"
        return update

    def _approval_problem(self, decision: ApprovalDecision, proposed: ProposedAction) -> str | None:
        """Why a decision must be ignored, or None if it's acceptable. Fails closed."""
        if decision.action_id != proposed.action_id:
            return "stale_action_id"
        # Defense in depth: approval/ already checks the allowlist before resuming the graph,
        # but if that check is ever skipped, the graph still refuses. An empty allowlist means
        # nobody can approve, not everybody.
        if decision.approver not in self.deps.settings.approver_ids:
            return "approver_not_allowed"
        if decision.decision == "approve" and not decision.token:
            return "approve_without_token"
        return None

    async def execute_fix(self, state: IncidentState) -> dict[str, Any]:
        """Send the approved action to the executor, with the token the human's click produced."""
        proposed, approval = state["proposed"], state["approval"]
        if proposed is None or approval is None or approval.token is None:
            raise RuntimeError("execute_fix reached without an approved, tokened proposal")

        request = RemediateRequest(
            incident_id=state["incident_id"],
            action_id=proposed.action_id,
            action=proposed.action,
            params=proposed.params,
            approval_token=approval.token,
            # Same as action_id (INTERFACES §2.2): if this node is retried after a crash, the
            # executor sees the same key and won't run the fix twice.
            idempotency_key=proposed.action_id,
        )
        observation = await self._call_executor(state, request)
        self._log(state, "fix executed", action=proposed.action, task_state=observation.state)
        return {
            "observations": [*state["observations"], self._redact_token(observation)],
            # The token has done its job. Redact it so it doesn't ride along in every later
            # checkpoint, trace and prompt. (Raised by Om on the diagnoser PR.) Note: the
            # checkpoints saved *before* this node still hold it; that's unavoidable, because the
            # token must survive a restart between approval and execution.
            "approval": approval.model_copy(update={"token": REDACTED}),
            "budget": self._add_budget(state, tool_calls=1),
        }

    @staticmethod
    def _redact_token(observation: Observation) -> Observation:
        """Copy of the observation with the RemediateRequest's approval_token replaced."""
        if not isinstance(observation.request, RemediateRequest):
            return observation
        request = observation.request.model_copy(update={"approval_token": REDACTED})
        return observation.model_copy(update={"request": request})

    async def check_health(self, state: IncidentState) -> dict[str, Any]:
        """Decide, deterministically, whether the fix worked.

        HOW: the graph sends one FIXED diagnostic, `curl -fsS --max-time 5 <HEALTH_URL>`,
        through the normal executor `diagnose` skill. Healthy = the command completed with exit
        code 0. The LLM can't influence the command or read the verdict differently.
        WHY not oracle.sh: that is the eval's grader (it also writes to the stack). Keeping the
        agent's check separate lets the eval measure "agent said resolved, grader disagrees".

        If unhealthy, the hypothesis we acted on was wrong (or incomplete), so it is marked
        refuted here; the replan that follows sees it as ruled out and won't propose the same fix.
        """
        request = DiagnoseRequest(
            incident_id=state["incident_id"],
            command=health_check_argv(self.deps.settings.health_url),
        )
        observation = await self._call_executor(state, request)
        healthy = (
            observation.state == "completed"
            and isinstance(observation.result, CommandResult)
            and observation.result.exit_code == 0
        )
        self._log(state, "health checked", healthy=healthy, task_state=observation.state)

        update: dict[str, Any] = {
            "observations": [*state["observations"], observation],
            "budget": self._add_budget(state, tool_calls=1),
        }
        if not healthy:
            update["hypotheses"] = [
                h.model_copy(update={"status": "refuted"}) if h.status == "confirmed" else h
                for h in state["hypotheses"]
            ]
        return update

    async def save_memory(self, state: IncidentState) -> dict[str, Any]:
        """Store the verified fix for future recall, then mark the incident resolved.

        Only reachable after check_health passed, so `verified=True` is a fact, not a claim
        (INTERFACES §3 write rule). The record is built by code from the state; nothing the LLM
        wrote goes in except the hypothesis statement it confirmed.
        """
        proposed = state["proposed"]
        confirmed = [h for h in state["hypotheses"] if h.status == "confirmed"]
        if proposed is None or not confirmed:
            raise RuntimeError("save_memory reached without a confirmed hypothesis and fix")
        alert = state["alert"]
        record = IncidentRecord(
            incident_id=state["incident_id"],
            created_at=self.deps.clock(),
            symptoms=f"{alert.service} {alert.summary}",
            root_cause=confirmed[0].statement,
            fix=proposed,
            verified=True,
        )
        self.deps.memory.save(record)
        self._log(state, "incident resolved", action=proposed.action)
        return {"outcome": "resolved"}

    async def escalate(self, state: IncidentState) -> dict[str, Any]:
        """Out of replans: hand the incident to a human. TODO(sai): Slack notice (Phase 1)."""
        self._log(state, "incident escalated", replans=state["budget"].replans)
        return {"outcome": "escalated"}

    async def budget_exceeded(self, state: IncidentState) -> dict[str, Any]:
        """A budget cap was hit. TODO(sai): Slack notice (INTERFACES §1), Phase 1."""
        reason = budget_exceeded_reason(state["budget"], self.deps.settings, self.deps.clock())
        self._log(state, "budget exceeded", reason=reason)
        return {"outcome": "budget_exceeded"}

    # -----------------------------------------------------------------------------------------
    # Routers (conditional edges)
    # -----------------------------------------------------------------------------------------

    def _guarded(self, state: IncidentState, next_node: str) -> str:
        """Go to `next_node` unless a budget cap is hit; then go to budget_exceeded instead."""
        if budget_exceeded_reason(state["budget"], self.deps.settings, self.deps.clock()):
            return NODE_BUDGET
        return next_node

    def _replan_or_escalate(self, state: IncidentState) -> str:
        """Replan if we still have replans left, otherwise escalate to a human."""
        if state["budget"].replans >= self.deps.settings.max_replans:
            return NODE_ESCALATE
        return self._guarded(state, NODE_PLAN)

    def route_after_plan(self, state: IncidentState) -> str:
        # A plan with no hypotheses gives us nothing to test: let a human take it.
        if not state["hypotheses"] or all(h.status == "refuted" for h in state["hypotheses"]):
            return NODE_ESCALATE
        return self._guarded(state, NODE_DIAGNOSE)

    def route_after_diagnose(self, state: IncidentState) -> str:
        return self._guarded(state, NODE_VERIFY)

    def route_after_verify(self, state: IncidentState) -> str:
        """The self-correction branch point.

        confirmed      -> propose_fix
        all refuted    -> plan_hypotheses (replan) or escalate if out of replans
        still open     -> diagnose again (gather more evidence)

        "still open -> diagnose" is not drawn in INTERFACES §1 but is needed: one diagnostic is
        rarely enough to decide. The iteration/tool-call caps bound this loop.
        """
        hypotheses = state["hypotheses"]
        if any(h.status == "confirmed" for h in hypotheses):
            return self._guarded(state, NODE_PROPOSE)
        if all(h.status == "refuted" for h in hypotheses):
            return self._replan_or_escalate(state)
        return self._guarded(state, NODE_DIAGNOSE)

    def route_after_approval(self, state: IncidentState) -> Literal["execute_fix", "rejected"]:
        # No budget check here on purpose: waiting for a human counts toward wall-clock time,
        # and an approved fix must not be thrown away because the human took a few minutes.
        approval = state["approval"]
        if approval is not None and approval.decision == "approve":
            return NODE_EXECUTE
        return ROUTE_REJECTED

    def route_after_health(self, state: IncidentState) -> str:
        if not any(h.status == "confirmed" for h in state["hypotheses"]):
            # check_health refuted the hypothesis we acted on: the fix didn't work.
            return self._replan_or_escalate(state)
        return NODE_SAVE
