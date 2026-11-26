"""The real ExecutorClient: talks to Om's executor over A2A v1.0 (INTERFACES §2).

WHAT one call does:
  DiagnoseRequest / RemediateRequest (our Pydantic contract)
    -> one A2A `Message` (role USER, context_id = incident_id, exactly one JSON `data` part,
       built by contracts.a2a_codec so both sides encode it identically)
    -> JSON-RPC `SendMessage` to the executor (a2a-sdk client, non-streaming)
    -> the executor answers with a `Task` (never a bare Message, INTERFACES §2.3)
    -> `task_to_observation()` turns that Task into our Observation:
         TASK_STATE_COMPLETED -> CommandResult from the artifact named "command_result"
         TASK_STATE_REJECTED  -> RejectionInfo from status.message  (the executor refused)
         TASK_STATE_FAILED    -> FailureInfo  from status.message  (the executor broke)

THREE KINDS OF "SOMETHING WENT WRONG", handled differently on purpose:
  * A normal answer that is bad news (non-zero exit code, REJECTED, FAILED): an Observation.
    The reasoner sees it as evidence. Nothing is raised.
  * Transport failure (executor down, timeout, auth refused, JSON-RPC error): ExecutorClientError.
    diagnoser.nodes turns that into a "failed" Observation, so the incident continues.
  * Malformed or unexpected answer (no command_result artifact, a data part that doesn't match the
    contract, a non-terminal task state, a task for a different incident): also
    ExecutorClientError, with a reason saying what was wrong. We never guess at a result: a
    half-parsed answer could make the agent believe a command succeeded when it didn't.

AUTH: the executor wants `Authorization: Bearer <Google-signed ID token for diagnoser-sa>`
(INTERFACES §2.1). The token comes from an injected `TokenProvider` called on EVERY request,
because ID tokens expire after an hour; the provider is expected to cache. The real Google
provider needs GCP (on hold); tests use a fixed string.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import httpx
from a2a.client import Client, ClientCallContext, ClientConfig, ClientFactory
from a2a.types import Message, Role, SendMessageRequest, Task, TaskState
from a2a.utils.errors import A2AError

from contracts import (
    CommandResult,
    DiagnoseRequest,
    FailureInfo,
    Observation,
    RejectionInfo,
    RemediateRequest,
)
from contracts.a2a_codec import from_data_part, to_data_part
from diagnoser.ports import ExecutorClientError
from obs.logging import get_logger

log = get_logger(__name__)

# Name of the artifact that carries a CommandResult (INTERFACES §2.3).
COMMAND_RESULT_ARTIFACT = "command_result"

# Returns a bearer token for the executor. In Cloud Run: a Google ID token for diagnoser-sa.
TokenProvider = Callable[[], Awaitable[str]]

ExecutorRequestModel = DiagnoseRequest | RemediateRequest


class _BearerAuth(httpx.Auth):
    """Adds `Authorization: Bearer <token>` to every request, asking the provider each time."""

    def __init__(self, provider: TokenProvider) -> None:
        self._provider = provider

    async def async_auth_flow(self, request: httpx.Request):  # type: ignore[override]
        request.headers["Authorization"] = f"Bearer {await self._provider()}"
        yield request


# ---------------------------------------------------------------------------------------------
# Task -> Observation (pure; most of the "malformed answer" handling lives here)
# ---------------------------------------------------------------------------------------------


def _cap_tail(text: str, max_bytes: int) -> tuple[str, bool]:
    """Keep the last `max_bytes` of text. The executor already caps output (MAX_OUTPUT_BYTES);
    this is defense in depth so an oversized answer can never flood the reasoner's prompt."""
    raw = text.encode()
    if len(raw) <= max_bytes:
        return text, False
    return raw[-max_bytes:].decode(errors="ignore"), True


def _command_result(task: Task, max_output_bytes: int) -> CommandResult:
    artifacts = [a for a in task.artifacts if a.name == COMMAND_RESULT_ARTIFACT]
    if len(artifacts) != 1 or len(artifacts[0].parts) != 1:
        raise ExecutorClientError(
            f"malformed_task: expected one '{COMMAND_RESULT_ARTIFACT}' artifact with one part, "
            f"got {len(artifacts)} artifact(s)"
        )
    try:
        result = from_data_part(artifacts[0].parts[0], CommandResult)
    except ValueError as exc:  # CodecError and pydantic.ValidationError are both ValueErrors
        raise ExecutorClientError(
            f"malformed_task: command_result doesn't match the contract ({type(exc).__name__})"
        ) from None
    stdout, cut_out = _cap_tail(result.stdout, max_output_bytes)
    stderr, cut_err = _cap_tail(result.stderr, max_output_bytes)
    if cut_out or cut_err:
        result = result.model_copy(update={"stdout": stdout, "stderr": stderr, "truncated": True})
    return result


def _status_info[M: RejectionInfo | FailureInfo](task: Task, model: type[M], fallback: str) -> M:
    """Parse the status.message data part; keep the state even if the reason is unreadable.

    A rejection/failure with a missing or malformed reason is still a rejection/failure: dropping
    it would lose real evidence, so only the reason text falls back.
    """
    parts = task.status.message.parts
    if len(parts) == 1:
        try:
            return from_data_part(parts[0], model)
        except ValueError:
            pass
    return model(reason=fallback)


def task_to_observation(
    task: Task,
    request: ExecutorRequestModel,
    observed_at: datetime,
    max_output_bytes: int = 16_384,
) -> Observation:
    """Map the executor's Task onto our Observation, or raise ExecutorClientError if malformed."""
    if task.context_id and task.context_id != request.incident_id:
        # The executor groups an incident's tasks by context_id = incident_id (§2.2). A different
        # value means the answer isn't for this incident; using it would mix up two incidents.
        raise ExecutorClientError("malformed_task: context_id does not match incident_id")
    if not task.id:
        raise ExecutorClientError("malformed_task: task has no id")

    state = task.status.state
    result: CommandResult | RejectionInfo | FailureInfo
    if state == TaskState.TASK_STATE_COMPLETED:
        observed_state, result = "completed", _command_result(task, max_output_bytes)
    elif state == TaskState.TASK_STATE_REJECTED:
        observed_state = "rejected"
        result = _status_info(task, RejectionInfo, "rejected_without_reason")
    elif state == TaskState.TASK_STATE_FAILED:
        observed_state = "failed"
        result = _status_info(task, FailureInfo, "failed_without_reason")
    else:
        # SUBMITTED/WORKING (executor returned before finishing), INPUT/AUTH_REQUIRED,
        # CANCELED, UNSPECIFIED: none is a valid final answer under our contract.
        raise ExecutorClientError(f"unexpected_task_state: {TaskState.Name(state)}")

    return Observation(
        task_id=task.id,
        skill=request.skill,
        request=request,
        state=observed_state,
        result=result,
        observed_at=observed_at,
    )


# ---------------------------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------------------------


class A2AExecutorClient:
    """ExecutorClient over A2A v1.0 JSON-RPC. One instance per process; reuses its connection.

    `http_client` is for tests (an httpx.AsyncClient with an ASGITransport pointing at an
    in-process A2A server). In production leave it None and pass `token_provider`.
    """

    def __init__(
        self,
        base_url: str,
        *,
        token_provider: TokenProvider | None = None,
        http_client: httpx.AsyncClient | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        remediate_timeout_s: float = 300.0,
        max_output_bytes: int = 16_384,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._http = http_client or httpx.AsyncClient()
        if token_provider is not None:
            self._http.auth = _BearerAuth(token_provider)
        self._clock = clock
        self._remediate_timeout_s = remediate_timeout_s
        self._max_output_bytes = max_output_bytes
        self._client: Client | None = None
        # Two concurrent first calls shouldn't both fetch the Agent Card.
        self._client_lock = asyncio.Lock()

    async def _a2a_client(self) -> Client:
        """Fetch the Agent Card once and build the SDK client from it (JSON-RPC, no streaming)."""
        async with self._client_lock:
            if self._client is None:
                config = ClientConfig(httpx_client=self._http, streaming=False)
                try:
                    self._client = await ClientFactory(config).create_from_url(self._base_url)
                except (A2AError, httpx.HTTPError, ValueError) as exc:
                    # ValueError: the card offers no transport we speak (e.g. no JSONRPC).
                    raise ExecutorClientError(
                        f"agent_card_unavailable: {type(exc).__name__}"
                    ) from None
            return self._client

    async def diagnose(self, request: DiagnoseRequest) -> Observation:
        # A little longer than the command's own timeout, so the executor's FAILED("timeout")
        # answer arrives before our transport gives up.
        return await self._send(request, timeout_s=request.timeout_s + 10)

    async def remediate(self, request: RemediateRequest) -> Observation:
        return await self._send(request, timeout_s=self._remediate_timeout_s)

    async def _send(self, request: ExecutorRequestModel, timeout_s: float) -> Observation:
        client = await self._a2a_client()
        message = Message(
            message_id=str(uuid.uuid4()),
            context_id=request.incident_id,
            role=Role.ROLE_USER,
            parts=[to_data_part(request)],
        )
        try:
            events = [
                event
                async for event in client.send_message(
                    SendMessageRequest(message=message),
                    context=ClientCallContext(timeout=timeout_s),
                )
            ]
        except (A2AError, httpx.HTTPError, ValueError) as exc:
            # Type name only: some exceptions carry the request, and with it the bearer token.
            log.warning(
                "a2a call failed",
                extra={"incident_id": request.incident_id, "error": type(exc).__name__},
            )
            raise ExecutorClientError(f"a2a_call_failed: {type(exc).__name__}") from None

        tasks = [e.task for e in events if e.HasField("task")]
        if len(events) != 1 or len(tasks) != 1:
            raise ExecutorClientError("malformed_response: expected exactly one Task")
        observation = task_to_observation(tasks[0], request, self._clock(), self._max_output_bytes)
        log.info(
            "a2a call done",
            extra={
                "incident_id": request.incident_id,
                "skill": request.skill,
                "task_id": observation.task_id,
                "task_state": observation.state,
            },
        )
        return observation

    async def aclose(self) -> None:
        await self._http.aclose()
