"""The diagnoser's HTTP service: incident intake, incident status, and the Slack webhook.

Endpoints (one FastAPI app, one Cloud Run service):
  POST /incidents              start an incident from an Alert           (bearer token)
  GET  /incidents/{incident_id} where an incident is: outcome, waiting?   (bearer token)
  POST /slack/interactions     Approve/Reject clicks                     (Slack signature)
  GET  /healthz                liveness probe                            (open)

HOW AN INCIDENT FLOWS THROUGH IT:
  1. POST /incidents validates the Alert, picks a fresh incident_id (= LangGraph thread_id),
     answers 202 right away, and runs the graph in a background task, because a full diagnosis
     makes many LLM and executor calls and can take minutes.
  2. If the graph pauses for approval, `_notify_if_paused` posts the approval message to Slack.
  3. A click arrives at /slack/interactions; SlackApprovalHandler resumes the graph. If that
     resume ends in ANOTHER approval pause (the fix failed its health check and the graph
     proposed a new one), the handler calls the same `_notify_if_paused`, so the human is asked
     again instead of the incident waiting silently.
  4. The caller polls GET /incidents/{id} until `outcome` is not "open".

WHY intake has its own token: Slack must be able to reach /slack/interactions, so the service
can't sit behind Cloud Run IAM. Without a check on /incidents, anyone on the internet could start
incidents (spending LLM tokens) and put text of their choice in front of the agent.

NOT here yet: building real DiagnoserDeps from config (the LLM reasoner and the A2A client don't
exist yet), so there is no module-level `app` for uvicorn; `create_service()` takes deps.
"""

from __future__ import annotations

import hmac
import uuid
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph

from approval.handler import SlackApprovalHandler
from approval.slack_client import SlackApiError, SlackPoster, request_approval_in_slack
from approval.web import create_app
from config import Settings
from contracts import Alert
from diagnoser.graph import build_graph, new_incident_state, thread_config
from diagnoser.ports import DiagnoserDeps
from obs.logging import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------------------------
# Auth for the incident endpoints
# ---------------------------------------------------------------------------------------------


def _token_checker(settings: Settings):
    """A FastAPI dependency that enforces `Authorization: Bearer <INCIDENT_API_TOKEN>`."""

    def require_incident_token(authorization: str | None = Header(default=None)) -> None:
        configured = settings.incident_api_token
        if configured is None or not configured.get_secret_value():
            # Fail closed, and say why: a misconfigured service should be obvious, not open.
            raise HTTPException(status_code=503, detail="incident_api_token_not_configured")
        scheme, _, presented = (authorization or "").partition(" ")
        # compare_digest: constant time, so response timing doesn't leak the token prefix.
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            presented.encode(), configured.get_secret_value().encode()
        ):
            raise HTTPException(
                status_code=401,
                detail="invalid_token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return require_incident_token


# ---------------------------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------------------------


class DiagnoserService:
    """Owns the compiled graph and everything that drives it, so the endpoints stay thin."""

    def __init__(
        self,
        deps: DiagnoserDeps,
        poster: SlackPoster,
        checkpointer: BaseCheckpointSaver | None = None,
    ) -> None:
        self.deps = deps
        self.settings = deps.settings
        self.poster = poster
        self.graph: CompiledStateGraph = build_graph(deps, checkpointer)
        self.approvals = SlackApprovalHandler(
            self.graph, self.settings, deps.clock, on_pause=self._notify_if_paused
        )

    async def _notify_if_paused(self, pause: dict[str, Any]) -> None:
        """Ask a human in Slack. A Slack failure is logged loudly, not raised: the incident stays
        safely paused (nothing runs without approval), but nobody knows it's waiting.
        TODO(sai): retry, or escalate when the approval message can't be posted."""
        try:
            await request_approval_in_slack(self.poster, self.settings, pause)
            log.info("approval requested", extra={"incident_id": pause.get("incident_id")})
        except (SlackApiError, ValueError) as exc:
            log.error(
                "approval request could not be posted",
                extra={"incident_id": pause.get("incident_id"), "error": str(exc)},
            )

    async def run_incident(self, alert: Alert, incident_id: str) -> None:
        """Run a new incident until it ends or pauses (background task; never raises)."""
        state = new_incident_state(alert, now=self.deps.clock(), incident_id=incident_id)
        try:
            result = await self.graph.ainvoke(state, thread_config(incident_id))
        except Exception as exc:  # noqa: BLE001 — background task: log, don't crash the server
            log.error(
                "incident run failed",
                extra={"incident_id": incident_id, "error": type(exc).__name__},
            )
            return
        if "__interrupt__" in result:
            await self._notify_if_paused(result["__interrupt__"][0].value)

    async def status(self, incident_id: str) -> dict[str, Any] | None:
        """A summary safe to return to API callers, or None for an unknown incident.

        Deliberately leaves out `observations` (raw command output: large, and untrusted) and
        anything token-like. `waiting_for_approval` is true while a human decision is pending.
        """
        snapshot = await self.graph.aget_state(thread_config(incident_id))
        values = snapshot.values
        if not values:
            return None
        proposed = values.get("proposed")
        budget = values["budget"]
        return {
            "incident_id": incident_id,
            "outcome": values["outcome"],
            "waiting_for_approval": bool(snapshot.interrupts),
            "hypotheses": [
                {"id": h.id, "statement": h.statement, "status": h.status}
                for h in values["hypotheses"]
            ],
            "proposed": (
                {"action": proposed.action, "params": proposed.params} if proposed else None
            ),
            "budget": {
                "iterations": budget.iterations,
                "replans": budget.replans,
                "tool_calls": budget.tool_calls,
                "tokens": budget.tokens,
                "tokens_in": budget.tokens_in,
                "tokens_out": budget.tokens_out,
            },
        }


def create_service(
    deps: DiagnoserDeps,
    poster: SlackPoster,
    checkpointer: BaseCheckpointSaver | None = None,
) -> FastAPI:
    """Build the full diagnoser app around injected deps (fakes in tests, real ones later)."""
    service = DiagnoserService(deps, poster, checkpointer)
    app = create_app(service.approvals)  # /slack/interactions + /healthz
    app.state.service = service  # handy for tests and debugging
    require_token = _token_checker(deps.settings)

    @app.post("/incidents", status_code=202, dependencies=[Depends(require_token)])
    async def start_incident(alert: Alert, background: BackgroundTasks) -> dict[str, str]:
        # FastAPI has already validated the body as an Alert (unknown fields -> 422).
        incident_id = str(uuid.uuid4())
        log.info(
            "incident accepted",
            extra={"incident_id": incident_id, "alert_id": alert.alert_id, "source": alert.source},
        )
        background.add_task(service.run_incident, alert, incident_id)
        return {"incident_id": incident_id, "status_url": f"/incidents/{incident_id}"}

    @app.get("/incidents/{incident_id}", dependencies=[Depends(require_token)])
    async def get_incident(incident_id: str) -> dict[str, Any]:
        status = await service.status(incident_id)
        if status is None:
            raise HTTPException(status_code=404, detail="unknown_incident")
        return status

    return app
