"""FastAPI endpoint for Slack's interactive-component callbacks (button clicks).

THE 3-SECOND PROBLEM: Slack expects an HTTP 200 within 3 seconds of a click, or it shows the user
an error and retries the request. Resuming the graph after an approval runs the fix and the
health check, which can take much longer. So the endpoint splits the work:

  1. synchronously: read the RAW body, check size, verify Slack's signature.
     Not from Slack -> 401 right away. (Cheap, and an attacker gets a real "no".)
  2. respond 200 immediately (empty body = "received", Slack shows nothing extra).
  3. after the response: FastAPI `BackgroundTasks` runs `SlackApprovalHandler.handle_interaction`,
     which re-runs every check (including the signature, which costs microseconds) and resumes
     the graph. Duplicate deliveries from Slack retries are harmless: the handler ignores a click
     for an incident that is no longer paused.

DEPLOYMENT NOTE (Cloud Run): by default Cloud Run throttles CPU once a response is sent, which can
stall work running after the response. The diagnoser service needs CPU always allocated
(`--no-cpu-throttling`) or this background resume may crawl. Not verified yet (GCP on hold).

WHY read the raw body ourselves: the signature covers the exact bytes Slack sent. Letting FastAPI
parse the form first would both break the signature and parse untrusted input before checking it.
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, FastAPI, Request, Response
from fastapi.responses import JSONResponse

from approval.handler import SlackApprovalHandler
from approval.slack_signature import SlackSignatureError, verify_slack_request
from obs.logging import get_logger

log = get_logger(__name__)

SLACK_INTERACTIONS_PATH = "/slack/interactions"
# Slack interaction payloads are a few KB. Refuse anything far bigger before doing HMAC over it.
MAX_BODY_BYTES = 64 * 1024


async def _resume_in_background(
    handler: SlackApprovalHandler, headers: dict[str, str], body: bytes
) -> None:
    """Run the full handler after the 200 has gone out. Never lets an exception escape: there is
    no client left to report it to, so it is logged (type only, no payload) and dropped."""
    try:
        result = await handler.handle_interaction(headers, body)
        log.info(
            "slack interaction processed",
            extra={"status": result.status, "reason": result.reason, "outcome": result.outcome},
        )
    except Exception as exc:  # noqa: BLE001 — last-resort guard for a background task
        log.error("slack interaction failed", extra={"error": type(exc).__name__})


def create_slack_router(handler: SlackApprovalHandler) -> APIRouter:
    """The router holding POST /slack/interactions, bound to one handler instance."""
    router = APIRouter()

    @router.post(SLACK_INTERACTIONS_PATH)
    async def slack_interactions(request: Request, background: BackgroundTasks) -> Response:
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            return JSONResponse({"error": "payload_too_large"}, status_code=413)

        headers = dict(
            request.headers
        )  # Starlette lower-cases names; the verifier accepts any case
        secret = handler.settings.slack_signing_secret
        try:
            verify_slack_request(
                secret.get_secret_value() if secret else "",
                headers,
                body,
                now=handler.clock().timestamp(),
            )
        except SlackSignatureError as exc:
            log.warning("slack request refused", extra={"reason": exc.reason})
            return JSONResponse({"error": exc.reason}, status_code=401)

        background.add_task(_resume_in_background, handler, headers, body)
        return Response(status_code=200)

    return router


def create_app(handler: SlackApprovalHandler) -> FastAPI:
    """A minimal app: the Slack webhook plus a liveness probe. The full diagnoser service
    (incident intake, graph construction from real deps) will include this router."""
    app = FastAPI(title="incident-responder diagnoser", docs_url=None, redoc_url=None)
    app.include_router(create_slack_router(handler))

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
