"""Handle a human's Approve/Reject click from Slack and resume the paused incident.

This is the ONLY place in the system that mints approval tokens (AGENTS.md rule 4,
INTERFACES §4.3). Every check below runs before minting, in this order, and any failure means
"no decision": the graph stays paused and the Slack message stays pending.

  1. Slack signature on the raw body    - the click really came from our Slack app
  2. payload shape                      - a block_actions click on one of our two buttons
  3. approver allowlist                 - this Slack user may approve fixes (fails closed)
  4. incident is paused for approval    - looked up in the graph's checkpoint, not in the click
  5. click is for the CURRENT proposal  - action_id matches (a stale click on an older
                                          proposal can't approve a newer one)
  6. proposal not older than the TTL    - APPROVAL_TTL_S (INTERFACES §4.2)

Only then: on Approve, mint the token over the action and params FROM THE CHECKPOINTED STATE
(never from text in the click), and resume the graph with Command(resume=ApprovalDecision).

The diagnoser re-checks action_id, allowlist and token presence itself (defense in depth), and
the executor verifies the token against the request it actually receives.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal
from urllib.parse import parse_qs

from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from approval.messages import APPROVE_ACTION, REJECT_ACTION
from approval.slack_signature import SlackSignatureError, verify_slack_request
from config import Settings
from contracts import ApprovalDecision, ProposedAction
from contracts.approval_token import mint
from diagnoser.graph import resume_with, thread_config
from diagnoser.nodes import APPROVAL_INTERRUPT_KIND
from obs.logging import get_logger

log = get_logger(__name__)

# Same character rules as contracts' Identifier: these values become a LangGraph thread_id and
# appear in logs, so anything odd is refused before it's used.
_ID = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")


class _ButtonValue(BaseModel):
    """What our buttons carry (messages.button_value). Unknown fields are an error: if someone
    edits the value to smuggle in an action or params, the click is refused, not half-used."""

    model_config = ConfigDict(extra="forbid")
    incident_id: str = _ID
    action_id: str = _ID


@dataclass(frozen=True)
class ApprovalClick:
    """A parsed, not-yet-authorised click."""

    user_id: str
    decision: Literal["approve", "reject"]
    incident_id: str
    action_id: str


@dataclass(frozen=True)
class InteractionResult:
    """What happened. The web layer maps `status` to an HTTP response:
    unauthorized -> 401 (not from Slack); ignored / resumed -> 200 (Slack only needs an ack)."""

    status: Literal["unauthorized", "ignored", "resumed"]
    reason: str
    outcome: str | None = None  # the incident outcome after resuming, if it reached one


class MalformedClickError(ValueError):
    """The (correctly signed) payload isn't a click on one of our approval buttons."""


def parse_click(body: bytes) -> ApprovalClick:
    """Parse Slack's interaction body: form-encoded `payload=<json>`. Raises MalformedClickError.

    Only call this AFTER verify_slack_request has accepted the same raw bytes.
    """
    try:
        form = parse_qs(body.decode("utf-8"), strict_parsing=True)
        (raw_payload,) = form["payload"]
        payload = json.loads(raw_payload)
        if payload.get("type") != "block_actions":
            raise MalformedClickError("not_a_block_action")
        (action,) = payload["actions"]  # exactly one button per click
        user_id = payload["user"]["id"]
        action_name = action["action_id"]
        value = _ButtonValue.model_validate_json(action["value"])
    except MalformedClickError:
        raise
    except (KeyError, ValueError, TypeError, UnicodeDecodeError, ValidationError) as exc:
        # ValueError also covers json.JSONDecodeError and the 1-element unpacking above.
        raise MalformedClickError(f"malformed_payload: {type(exc).__name__}") from None

    decisions = {APPROVE_ACTION: "approve", REJECT_ACTION: "reject"}
    if action_name not in decisions or not isinstance(user_id, str):
        raise MalformedClickError("unknown_action")
    return ApprovalClick(
        user_id=user_id,
        decision=decisions[action_name],
        incident_id=value.incident_id,
        action_id=value.action_id,
    )


class SlackApprovalHandler:
    """Turns a Slack click into ApprovalDecision + resumed graph. One instance per process."""

    def __init__(
        self,
        graph: CompiledStateGraph,
        settings: Settings,
        clock: Any,  # Callable[[], datetime]; Any keeps the tests' FakeClock simple
        on_pause: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self.graph = graph
        self.settings = settings
        self.clock = clock
        # Called with the new pause payload when a resume ends in ANOTHER approval pause (the fix
        # failed its health check, the graph replanned and proposed a new fix). Without it the
        # incident would wait for an approval nobody was asked for. The service passes a
        # function that posts the new approval message to Slack.
        self.on_pause = on_pause
        # Serialises check-then-resume, so two approvers clicking at the same moment can't both
        # pass check 4 and resume the same incident twice. NOTE: only within one process; with
        # several Cloud Run instances this needs a Firestore transaction (not built yet).
        self._lock = asyncio.Lock()

    async def handle_interaction(self, headers: dict[str, str], body: bytes) -> InteractionResult:
        """Run checks 1-6, mint on approve, resume the graph. Never raises for bad input."""
        secret = self.settings.slack_signing_secret
        try:
            verify_slack_request(
                secret.get_secret_value() if secret else "",
                headers,
                body,
                now=self.clock().timestamp(),
            )
        except SlackSignatureError as exc:
            log.warning("slack request refused", extra={"reason": exc.reason})
            return InteractionResult("unauthorized", exc.reason)

        try:
            click = parse_click(body)
        except MalformedClickError as exc:
            return self._ignored(str(exc))

        if click.user_id not in self.settings.approver_ids:
            # Empty allowlist = nobody may approve (fails closed).
            return self._ignored("approver_not_allowed", click)

        async with self._lock:
            proposed, problem = await self._pending_proposal(click)
            if proposed is None:
                return self._ignored(problem, click)
            decision = self._decision(click, proposed)
            log.info(
                "approval accepted",
                extra={
                    "incident_id": click.incident_id,
                    "action_id": click.action_id,
                    "decision": decision.decision,
                    "approver": click.user_id,
                },
            )
            final = await self.graph.ainvoke(
                resume_with(decision), thread_config(click.incident_id)
            )
        # Outside the lock: posting to Slack is slow and doesn't touch the graph.
        if "__interrupt__" in final and self.on_pause is not None:
            await self.on_pause(final["__interrupt__"][0].value)
        return InteractionResult("resumed", decision.decision, outcome=final.get("outcome"))

    async def _pending_proposal(self, click: ApprovalClick) -> tuple[ProposedAction | None, str]:
        """Checks 4-6, against the checkpoint. Returns (proposal, "") or (None, reason)."""
        snapshot = await self.graph.aget_state(thread_config(click.incident_id))
        # `snapshot.interrupts` lists the interrupts the thread is paused on right now. It is
        # empty for an unknown incident, a running one, and a finished one, so "has a pending
        # interrupt of our kind" is the whole "is it waiting for approval?" check. (Also
        # comparing snapshot.next to request_approval was tried and is redundant: a mutation
        # test removing it changed no result.)
        pause = snapshot.interrupts[0].value if snapshot.interrupts else None
        if not isinstance(pause, dict) or pause.get("kind") != APPROVAL_INTERRUPT_KIND:
            return None, "not_waiting_for_approval"

        proposed = ProposedAction.model_validate(pause["proposed"])
        if proposed.action_id != click.action_id:
            return None, "stale_action_id"

        requested_at = datetime.fromisoformat(pause["requested_at"])
        if self.clock() - requested_at > timedelta(seconds=self.settings.approval_ttl_s):
            # TODO(sai): a sweeper should also escalate expired approvals without a click
            # (INTERFACES §4.2, outcome="escalated"); today they just stay paused.
            return None, "approval_expired"
        return proposed, ""

    def _decision(self, click: ApprovalClick, proposed: ProposedAction) -> ApprovalDecision:
        """Build the decision; on approve, mint a token bound to the checkpointed proposal."""
        token = None
        if click.decision == "approve":
            token = mint(
                self.settings.approval_key_bytes(),
                incident_id=click.incident_id,
                action_id=proposed.action_id,
                action=proposed.action,  # from the checkpoint, never from the click
                params=proposed.params,
                ttl_s=self.settings.approval_ttl_s,
                now=self.clock().timestamp(),
            )
        return ApprovalDecision(
            action_id=proposed.action_id,
            decision=click.decision,
            approver=click.user_id,
            decided_at=self.clock(),
            token=token,
        )

    @staticmethod
    def _ignored(reason: str, click: ApprovalClick | None = None) -> InteractionResult:
        extra: dict[str, Any] = {"reason": reason}
        if click:
            extra |= {"incident_id": click.incident_id, "approver": click.user_id}
        log.info("approval click ignored", extra=extra)
        return InteractionResult("ignored", reason)
