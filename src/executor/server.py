"""The executor's A2A v1.0 server: exposes `diagnose` and `remediate` to the diagnoser (INTERFACES §2).

WHAT runs where:
  GET  /.well-known/agent-card.json   Agent Card (a2a-sdk route)            auth required
  POST /                              JSON-RPC: SendMessage, GetTask, ...    auth + A2A-Version 1.0
  GET  /healthz                       liveness for the VM's process manager  open

One SendMessage = one command or one fix. `IncidentAgentExecutor.execute()`:
  1. enqueue a Task first (a2a-sdk 1.3 refuses status updates before that; Sai found this)
  2. parse exactly one JSON data part into contracts.ExecutorRequest
  3. check message.context_id == incident_id (Sai's client groups an incident's tasks by it)
  4. dispatch: diagnose -> CommandRunner.diagnose, remediate -> Remediator.remediate
  5. publish the RunOutcome:
       completed -> artifact "command_result" (one data part: CommandResult), TASK_STATE_COMPLETED
       rejected  -> status.message (one data part: RejectionInfo),           TASK_STATE_REJECTED
       failed    -> status.message (one data part: FailureInfo),             TASK_STATE_FAILED
  Any exception anywhere becomes FAILED("internal_error"). WHY: a2a-sdk turns an uncaught
  exception into a JSON-RPC error that includes the exception's message, which could leak paths or
  values (Sai's finding). The details go to our logs only.

AUTH (the middleware below, before the SDK sees anything):
  * `Authorization: Bearer <Google-signed ID token>`; the token's audience must be this executor
    and its email must be ALLOWED_DIAGNOSER_SA. Anything else: 401/403, no body details.
  * `A2A-Version: 1.0` on JSON-RPC calls; missing or other: 400.
  * request bodies over MAX_BODY_BYTES: 413 (a DiagnoseRequest is a few hundred bytes).
The VM also has no external IP (INTERFACES §0); this is the second lock, not the only one.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, Protocol

from a2a.helpers.proto_helpers import new_task
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    HTTPAuthSecurityScheme,
    SecurityRequirement,
    SecurityScheme,
    StringList,
    TaskState,
)
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH, VERSION_HEADER
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from contracts import CommandResult, DiagnoseRequest, ExecutorRequest, FailureInfo, RejectionInfo
from contracts.a2a_codec import CodecError, from_data_part_as, to_data_part
from executor.remediation import Remediator
from executor.runner import CommandRunner, RunOutcome
from obs.logging import get_logger

log = get_logger(__name__)

COMMAND_RESULT_ARTIFACT = "command_result"  # INTERFACES §2.3; Sai's client requires this name
PROTOCOL_VERSION = "1.0"
MAX_BODY_BYTES = 64 * 1024
SECURITY_SCHEME = "google-id-token"
OPEN_PATHS = frozenset({"/healthz"})


# ---------------------------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------------------------


class AuthError(Exception):
    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


class TokenVerifier(Protocol):
    def __call__(self, token: str) -> Awaitable[str]:
        """Return the caller's identity (service-account email) or raise AuthError."""
        ...


def google_id_token_verifier(audience: str, allowed_email: str) -> TokenVerifier:
    """Verify a Google-signed ID token (as minted for a Cloud Run service account).

    Checks signature (Google's public certs, fetched and cached by google-auth), expiry, issuer,
    audience == this executor, email_verified, and email == the one allowed caller.
    """
    import anyio
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    if not audience or not allowed_email:
        raise ValueError(
            "executor auth needs EXECUTOR_AUDIENCE/EXECUTOR_URL and ALLOWED_DIAGNOSER_SA"
        )
    session = google_requests.Request()

    async def verify(token: str) -> str:
        try:
            # google-auth is synchronous (it may fetch certs); keep it off the event loop.
            claims = await anyio.to_thread.run_sync(
                lambda: id_token.verify_oauth2_token(token, session, audience=audience)
            )
        except Exception:  # noqa: BLE001 — any verification failure is "not authenticated"
            raise AuthError(401, "invalid_token") from None
        email = claims.get("email")
        if not claims.get("email_verified") or email != allowed_email:
            raise AuthError(403, "caller_not_allowed")
        return email

    return verify


class GuardMiddleware:
    """Pure ASGI middleware: auth, A2A-Version and body size, before any route runs.

    Pure ASGI (not BaseHTTPMiddleware) so the body-size limit applies while the body streams in,
    not after it has been read into memory.
    """

    def __init__(self, app: ASGIApp, verify: TokenVerifier) -> None:
        self.app = app
        self.verify = verify

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in OPEN_PATHS:
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        try:
            caller = await self._authenticate(headers)
            if (
                scope["method"] == "POST"
                and headers.get(VERSION_HEADER.lower()) != PROTOCOL_VERSION
            ):
                raise AuthError(400, "unsupported_or_missing_a2a_version")
            declared = headers.get("content-length")
            if declared is not None and (not declared.isdigit() or int(declared) > MAX_BODY_BYTES):
                raise AuthError(413, "payload_too_large")
            body = await self._read_body(receive)
        except AuthError as exc:
            log.warning(
                "request refused",
                extra={"path": scope["path"], "status": exc.status, "reason": exc.reason},
            )
            await JSONResponse({"error": exc.reason}, status_code=exc.status)(scope, receive, send)
            return
        scope.setdefault("state", {})["caller"] = caller
        await self.app(scope, self._replay(body, receive), send)

    async def _authenticate(self, headers: dict[str, str]) -> str:
        scheme, _, token = headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise AuthError(401, "missing_bearer_token")
        return await self.verify(token.strip())

    @staticmethod
    async def _read_body(receive: Receive) -> bytes:
        """Read the whole body, refusing it as soon as it passes MAX_BODY_BYTES.

        Also covers chunked bodies that have no Content-Length to check up front. Reading here
        (instead of limiting inside the app's receive) means an oversized body is answered 413,
        not turned into an exception halfway through the SDK's handler.
        """
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > MAX_BODY_BYTES:
                raise AuthError(413, "payload_too_large")
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        return b"".join(chunks)

    @staticmethod
    def _replay(body: bytes, original: Receive) -> Receive:
        """Hand the already-read body to the app once; after that, wait on the real connection
        (so a handler checking for client disconnects sees the true state, not a fake one)."""
        sent = False

        async def receive() -> dict[str, Any]:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await original()

        return receive


# ---------------------------------------------------------------------------------------------
# The A2A executor
# ---------------------------------------------------------------------------------------------


def _reject(rule_id: str, reason: str) -> RunOutcome:
    return RunOutcome("rejected", RejectionInfo(reason=reason, rule_id=rule_id), rule_id)


class IncidentAgentExecutor(AgentExecutor):
    def __init__(self, runner: CommandRunner, remediator: Remediator) -> None:
        self.runner = runner
        self.remediator = remediator

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        # Step 1: the Task must exist before any status update.
        await event_queue.enqueue_event(
            new_task(context.task_id, context.context_id, TaskState.TASK_STATE_SUBMITTED)
        )
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        try:
            outcome = await self._handle(context)
        except Exception:  # noqa: BLE001 — never let exception text reach the caller
            log.exception("executor error", extra={"task_id": context.task_id})
            outcome = RunOutcome("failed", FailureInfo(reason="internal_error"), "a2a.internal")
        await self._publish(updater, outcome, context)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        # Commands are short and remediation must not stop half-way; cancelling isn't offered.
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.reject(
            updater.new_agent_message(
                [to_data_part(RejectionInfo(reason="cancel_not_supported", rule_id="a2a.cancel"))]
            )
        )

    async def _handle(self, context: RequestContext) -> RunOutcome:
        message = context.message
        if message is None or len(message.parts) != 1:
            return _reject("a2a.bad_request", "send exactly one JSON data part")
        # Steps 2–3.
        try:
            request = from_data_part_as(message.parts[0], ExecutorRequest)
        except CodecError as exc:
            return _reject("a2a.bad_request", str(exc))
        except ValidationError as exc:
            # Field locations and messages only; never echo the values back.
            problems = "; ".join(
                f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5]
            )
            return _reject("a2a.bad_request", f"request doesn't match the contract: {problems}")
        if context.context_id != request.incident_id:
            return _reject("a2a.context_mismatch", "message context_id must equal incident_id")
        # Step 4.
        if isinstance(request, DiagnoseRequest):
            return await self.runner.diagnose(request)
        return await self.remediator.remediate(request)

    @staticmethod
    async def _publish(updater: TaskUpdater, outcome: RunOutcome, context: RequestContext) -> None:
        log.info(
            "task finished",
            extra={
                "task_id": context.task_id,
                "incident_id": context.context_id,
                "state": outcome.state,
                "rule_id": outcome.rule_id,
            },
        )
        if outcome.state == "completed" and isinstance(outcome.result, CommandResult):
            await updater.add_artifact([to_data_part(outcome.result)], name=COMMAND_RESULT_ARTIFACT)
            await updater.complete()
        elif outcome.state == "rejected":
            await updater.reject(updater.new_agent_message([to_data_part(outcome.result)]))
        else:
            await updater.failed(updater.new_agent_message([to_data_part(outcome.result)]))


# ---------------------------------------------------------------------------------------------
# Agent Card and app
# ---------------------------------------------------------------------------------------------


def build_agent_card(
    url: str, diagnose_summary: str, remediate_summary: str, version: str = "0.1.0"
) -> AgentCard:
    """INTERFACES §2.1. The skill descriptions ARE the policy summaries, so the diagnoser can build
    its prompt from the card it already fetches, and the prompt always matches what's enforced."""
    return AgentCard(
        name="incident-executor",
        version=version,
        description="Runs read-only diagnostics and human-approved fixes on the target stack.",
        supported_interfaces=[
            AgentInterface(
                url=url.rstrip("/") + "/",
                protocol_binding="JSONRPC",
                protocol_version=PROTOCOL_VERSION,
            )
        ],
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
        security_schemes={
            SECURITY_SCHEME: SecurityScheme(
                http_auth_security_scheme=HTTPAuthSecurityScheme(
                    scheme="Bearer", bearer_format="JWT"
                )
            )
        },
        security_requirements=[SecurityRequirement(schemes={SECURITY_SCHEME: StringList()})],
        skills=[
            AgentSkill(
                id="diagnose",
                name="diagnose",
                description=diagnose_summary,
                tags=["diagnostics", "read-only"],
                input_modes=["application/json"],
                output_modes=["application/json"],
            ),
            AgentSkill(
                id="remediate",
                name="remediate",
                description=remediate_summary,
                tags=["remediation", "requires-approval"],
                input_modes=["application/json"],
                output_modes=["application/json"],
            ),
        ],
    )


def create_app(
    runner: CommandRunner,
    remediator: Remediator,
    verify: TokenVerifier,
    url: str,
    diagnose_summary: str,
) -> Starlette:
    card = build_agent_card(url, diagnose_summary, remediator.summary())
    handler = DefaultRequestHandler(
        IncidentAgentExecutor(runner, remediator), InMemoryTaskStore(), card
    )

    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    routes = [
        *create_agent_card_routes(card, card_url=AGENT_CARD_WELL_KNOWN_PATH),
        *create_jsonrpc_routes(handler, rpc_url="/"),
        Route("/healthz", healthz),
    ]
    app = Starlette(routes=routes)
    app.add_middleware(GuardMiddleware, verify=verify)
    return app
