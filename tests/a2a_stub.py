"""An in-process A2A v1.0 server that stands in for Om's executor in tests.

It is a REAL a2a-sdk server (Agent Card route + JSON-RPC route + DefaultRequestHandler), so the
client under test speaks the real protocol. Only the AgentExecutor is scripted: each test passes a
`respond(request, context, queue)` coroutine choosing what the "executor" answers.

Wired to the client through `httpx.ASGITransport`, so no port is opened and nothing touches the
network.

Note for whoever builds the real executor server: the SDK requires the executor to enqueue a
`Task` before any status update (`new_task(...)` below), otherwise it answers with
"Agent should enqueue Task before TaskStatusUpdateEvent event".
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from a2a.helpers.proto_helpers import new_task
from a2a.server.agent_execution import AgentExecutor
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Message,
    Role,
    TaskState,
)
from fastapi import FastAPI

from contracts import CommandResult, ExecutorRequest, FailureInfo, RejectionInfo
from contracts.a2a_codec import from_data_part_as, to_data_part
from diagnoser.a2a_client import COMMAND_RESULT_ARTIFACT

URL = "http://executor.test"

Respond = Callable[[Any, Any, Any], Awaitable[None]]


def agent_card(bindings: tuple[str, ...] = ("JSONRPC",)) -> AgentCard:
    """Card shaped like INTERFACES §2.1."""
    return AgentCard(
        name="incident-executor",
        version="0.1.0",
        description="test stand-in for the executor",
        supported_interfaces=[
            AgentInterface(url=URL + "/", protocol_binding=b, protocol_version="1.0")
            for b in bindings
        ],
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
        skills=[
            AgentSkill(id="diagnose", name="diagnose", description="read-only", tags=["diag"]),
            AgentSkill(id="remediate", name="remediate", description="catalog", tags=["fix"]),
        ],
    )


class StubExecutor(AgentExecutor):
    def __init__(self, respond: Respond) -> None:
        self.respond = respond
        self.received: list[tuple[Any, str]] = []  # (parsed request, context_id)

    async def execute(self, context, event_queue) -> None:
        request = from_data_part_as(context.message.parts[0], ExecutorRequest)
        self.received.append((request, context.context_id))
        await self.respond(request, context, event_queue)

    async def cancel(self, context, event_queue) -> None:  # not used
        return None


def build_app(executor: StubExecutor, card: AgentCard | None = None) -> tuple[FastAPI, list[dict]]:
    """The server app, plus a list that records each HTTP request's headers."""
    card = card or agent_card()
    app = FastAPI()
    seen_headers: list[dict] = []

    @app.middleware("http")
    async def record_headers(request, call_next):
        seen_headers.append({"_method": request.method, **request.headers})
        return await call_next(request)

    handler = DefaultRequestHandler(executor, InMemoryTaskStore(), card)
    for route in create_agent_card_routes(card) + create_jsonrpc_routes(handler, rpc_url="/"):
        app.router.routes.append(route)
    return app, seen_headers


def http_client_for(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=URL)


# --- scripted answers -------------------------------------------------------------------------


async def _start(context, queue) -> TaskUpdater:
    await queue.enqueue_event(
        new_task(context.task_id, context.context_id, TaskState.TASK_STATE_SUBMITTED)
    )
    return TaskUpdater(queue, context.task_id, context.context_id)


def completes_with(result: CommandResult) -> Respond:
    async def respond(request, context, queue):
        updater = await _start(context, queue)
        await updater.add_artifact([to_data_part(result)], name=COMMAND_RESULT_ARTIFACT)
        await updater.complete()

    return respond


def rejects_with(info: RejectionInfo) -> Respond:
    async def respond(request, context, queue):
        updater = await _start(context, queue)
        await updater.reject(updater.new_agent_message([to_data_part(info)]))

    return respond


def fails_with(info: FailureInfo) -> Respond:
    async def respond(request, context, queue):
        updater = await _start(context, queue)
        await updater.failed(updater.new_agent_message([to_data_part(info)]))

    return respond


async def completes_without_artifact(request, context, queue):
    updater = await _start(context, queue)
    await updater.complete()


def _result(stdout: str) -> CommandResult:
    return CommandResult(exit_code=0, stdout=stdout, stderr="", truncated=False, duration_ms=1)


async def completes_with_two_results(request, context, queue):
    """Ambiguous: which one is the answer? The client must refuse, not pick one."""
    updater = await _start(context, queue)
    for text in ("first", "second"):
        await updater.add_artifact([to_data_part(_result(text))], name=COMMAND_RESULT_ARTIFACT)
    await updater.complete()


async def completes_with_two_parts(request, context, queue):
    updater = await _start(context, queue)
    parts = [to_data_part(_result("a")), to_data_part(_result("b"))]
    await updater.add_artifact(parts, name=COMMAND_RESULT_ARTIFACT)
    await updater.complete()


async def completes_with_off_contract_data(request, context, queue):
    updater = await _start(context, queue)
    bad = CommandResult(exit_code=0, stdout="", stderr="", truncated=False, duration_ms=1)
    part = to_data_part(bad)
    part.data.struct_value.fields["exit_code"].string_value = "zero"  # wrong type
    await updater.add_artifact([part], name=COMMAND_RESULT_ARTIFACT)
    await updater.complete()


async def rejects_without_reason(request, context, queue):
    updater = await _start(context, queue)
    await updater.reject()


async def stays_working(request, context, queue):
    updater = await _start(context, queue)
    await updater.start_work()  # returns without reaching a terminal state


async def answers_with_bare_message(request, context, queue):
    await queue.enqueue_event(
        Message(message_id="m1", context_id=context.context_id, role=Role.ROLE_AGENT, parts=[])
    )


async def crashes(request, context, queue):
    raise RuntimeError("executor bug")
