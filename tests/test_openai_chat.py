"""OpenAIChatModel tests: the real `openai` SDK over an httpx MockTransport (no network, no key).

What they prove: the request carries the strict schema, model and token limit from config; usage
is parsed; a server that refuses json_schema gets one json_object retry; errors carry status and
type only (never the key, prompt or body); and our four answer schemas pass the strict rules.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from config import Settings
from diagnoser.llm import ChatMessage, ChatModelError
from diagnoser.openai_chat import (
    NO_KEY_PLACEHOLDER,
    OpenAIChatModel,
    chat_model_from_settings,
    strict_schema,
)
from diagnoser.reasoner import AssessAnswer, CommandAnswer, PlanAnswer, ProposeAnswer

BASE = "http://llm.test/v1"
KEY = "sk-test-SECRET-KEY-123"
PROMPT = "SECRET-PROMPT-TEXT reply with JSON"
MESSAGES = [ChatMessage("system", "Reply in JSON."), ChatMessage("user", PROMPT)]


def completion(content: str | None = '{"command": null, "reason": "x"}', **message) -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content, **message},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 321, "completion_tokens": 45, "total_tokens": 366},
    }


class Server:
    """Scripted chat-completions endpoint. Each script item: (status, json body)."""

    def __init__(self, *script: tuple[int, dict]) -> None:
        self.script = list(script)
        self.requests: list[dict] = []
        self.headers: list[httpx.Headers] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        self.requests.append(json.loads(request.content))
        self.headers.append(request.headers)
        status, body = self.script.pop(0)
        return httpx.Response(status, json=body)


def model(server: Server, **kw) -> OpenAIChatModel:
    http = httpx.AsyncClient(transport=httpx.MockTransport(server))
    return OpenAIChatModel(
        model="test-model", api_key=KEY, base_url=BASE, max_retries=0, http_client=http, **kw
    )


def call(m: OpenAIChatModel, answer=CommandAnswer):
    return asyncio.run(
        m.complete(
            MESSAGES,
            schema=answer.model_json_schema(),
            schema_name=answer.__name__,
            max_output_tokens=777,
        )
    )


def test_request_uses_strict_json_schema_model_and_token_limit():
    server = Server((200, completion()))
    result = call(model(server))

    body = server.requests[0]
    assert body["model"] == "test-model"
    assert body["max_completion_tokens"] == 777
    assert body["messages"] == [
        {"role": "system", "content": "Reply in JSON."},
        {"role": "user", "content": PROMPT},
    ]
    rf = body["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["name"] == "CommandAnswer"
    assert rf["json_schema"]["schema"]["additionalProperties"] is False
    assert server.headers[0]["authorization"] == f"Bearer {KEY}"
    assert "temperature" not in body  # some reasoning models reject it; we never send it

    assert result.text == '{"command": null, "reason": "x"}'
    assert (result.input_tokens, result.output_tokens) == (321, 45)


def test_a_server_that_refuses_json_schema_gets_one_json_object_retry():
    refused = {"error": {"message": "response_format json_schema unsupported", "type": "x"}}
    server = Server((400, refused), (200, completion()))
    result = call(model(server))
    assert [r["response_format"]["type"] for r in server.requests] == ["json_schema", "json_object"]
    assert result.input_tokens == 321


def test_a_second_400_is_reported_not_retried_again():
    bad = {"error": {"message": f"context too long: {PROMPT}", "type": "invalid_request_error"}}
    server = Server((400, bad), (400, bad))
    with pytest.raises(ChatModelError) as exc:
        call(model(server))
    assert str(exc.value) == "400 BadRequestError"
    assert len(server.requests) == 2


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, "401 AuthenticationError"),
        (429, "429 RateLimitError"),
        (500, "500 InternalServerError"),
    ],
)
def test_errors_carry_status_and_type_only(status, expected):
    body = {"error": {"message": f"bad key {KEY} for prompt {PROMPT}", "type": "x"}}
    server = Server((status, body))
    with pytest.raises(ChatModelError) as exc:
        call(model(server))
    assert str(exc.value) == expected
    assert KEY not in str(exc.value) and PROMPT not in str(exc.value)
    assert len(server.requests) == 1  # max_retries=0 here; no fallback for non-400s


def test_network_failure_is_a_chat_model_error():
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    m = OpenAIChatModel(
        model="m",
        api_key=KEY,
        base_url=BASE,
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(refuse)),
    )
    with pytest.raises(ChatModelError, match="^APIConnectionError$"):
        call(m)


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (completion(content=None, refusal="I can't help with that"), "refusal"),
        (completion(content=None), "empty_response: no content"),
        ({**completion(), "choices": []}, "empty_response: no choices"),
    ],
)
def test_refusals_and_empty_answers_are_errors(body, reason):
    with pytest.raises(ChatModelError) as exc:
        call(model(Server((200, body))))
    assert str(exc.value) == reason


def test_missing_usage_counts_zero():
    body = {**completion()}
    body.pop("usage")
    result = call(model(Server((200, body))))
    assert (result.input_tokens, result.output_tokens) == (0, 0)


# --- strict schema ---------------------------------------------------------------------------


def _objects(node):
    if isinstance(node, dict):
        if node.get("type") == "object" or "properties" in node:
            yield node
        for v in node.values():
            yield from _objects(v)
    elif isinstance(node, list):
        for v in node:
            yield from _objects(v)


@pytest.mark.parametrize("answer", [PlanAnswer, CommandAnswer, AssessAnswer, ProposeAnswer])
def test_our_answer_schemas_meet_the_strict_rules(answer):
    original = answer.model_json_schema()
    schema = strict_schema(original)
    for obj in _objects(schema):
        assert obj.get("additionalProperties") is False
        assert sorted(obj["required"]) == sorted(obj["properties"])
    assert '"default": null' not in json.dumps(schema)  # None defaults dropped (others stay)
    assert original == answer.model_json_schema()  # the input is not mutated


def test_ref_with_siblings_is_inlined():
    schema = {
        "type": "object",
        "properties": {"p": {"$ref": "#/$defs/P", "description": "d"}},
        "$defs": {"P": {"type": "object", "properties": {"x": {"type": "string"}}}},
    }
    p = strict_schema(schema)["properties"]["p"]
    assert "$ref" not in p and p["description"] == "d"
    assert p["required"] == ["x"] and p["additionalProperties"] is False


# --- settings ---------------------------------------------------------------------------------


def test_settings_build_the_adapter_and_need_a_key_for_openai():
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        chat_model_from_settings(Settings(_env_file=None, llm_model="m"))
    with pytest.raises(ValueError, match="LLM_MODEL"):
        chat_model_from_settings(Settings(_env_file=None, openai_api_key="sk-x"))


def test_a_local_base_url_needs_no_key():
    server = Server((200, completion()))
    s = Settings(_env_file=None, llm_model="gemma", llm_base_url=BASE, llm_max_retries=0)
    m = chat_model_from_settings(
        s, http_client=httpx.AsyncClient(transport=httpx.MockTransport(server))
    )
    call(m)
    assert server.headers[0]["authorization"] == f"Bearer {NO_KEY_PLACEHOLDER}"
    assert server.requests[0]["model"] == "gemma"


@pytest.mark.parametrize("answer", [PlanAnswer, CommandAnswer, AssessAnswer, ProposeAnswer])
def test_strict_schema_matches_the_sdk_helper(answer):
    # Our copy of the SDK's (private) strict-schema rules. If an SDK upgrade changes the rules,
    # this fails and tells us to re-sync, instead of the API rejecting schemas at run time.
    sdk = pytest.importorskip("openai.lib._pydantic")
    assert strict_schema(answer.model_json_schema()) == sdk.to_strict_json_schema(answer)


# --- end to end: LLMReasoner + this adapter + the real graph ------------------------------------


def test_llm_reasoner_over_the_adapter_drives_an_incident_to_approval():
    from datetime import UTC, datetime

    from contracts import Alert, CommandResult
    from diagnoser.fakes import FakeExecutorClient, StaticPolicy
    from diagnoser.graph import build_graph, new_incident_state, thread_config
    from diagnoser.ports import DiagnoserDeps
    from diagnoser.reasoner import LLMReasoner
    from memory.store import InMemoryIncidentMemory

    logs = ["docker", "logs", "--tail", "100", "ir-target-db-1"]
    replies = [
        {"hypotheses": [{"statement": "postgres crashed", "evidence_needed": ["logs"]}]},
        {"command": logs, "reason": "db logs"},
        {"assessments": [{"id": "h1", "status": "confirmed", "confidence": 0.9, "reason": "x"}]},
        {
            "action": "restart_service",
            "params": [{"name": "service", "value": "db"}],
            "rationale": "postgres exited",
            "rollback": None,
        },
    ]
    server = Server(*[(200, completion(json.dumps(r))) for r in replies])
    settings = Settings(_env_file=None, approver_allowlist="U")
    reasoner = LLMReasoner(
        model(server),
        StaticPolicy(),
        max_hypotheses=settings.max_hypotheses,
        max_iterations=settings.max_iterations,
        max_tool_calls=settings.max_tool_calls,
        max_replans=settings.max_replans,
    )
    t0 = datetime(2026, 11, 27, tzinfo=UTC)
    ok = CommandResult(exit_code=0, stdout="FATAL", stderr="", truncated=False, duration_ms=1)
    executor = FakeExecutorClient(diagnose_results={tuple(logs): ok})
    deps = DiagnoserDeps(reasoner, executor, InMemoryIncidentMemory(), settings, lambda: t0)
    alert = Alert(alert_id="a", source="manual", service="db", summary="down", fired_at=t0)

    paused = asyncio.run(
        build_graph(deps).ainvoke(new_incident_state(alert, t0, "inc-1"), thread_config("inc-1"))
    )

    proposed = paused["__interrupt__"][0].value["proposed"]
    assert proposed["action"] == "restart_service" and proposed["params"] == {"service": "db"}
    assert [r["response_format"]["json_schema"]["name"] for r in server.requests] == [
        "PlanAnswer",
        "CommandAnswer",
        "AssessAnswer",
        "ProposeAnswer",
    ]
    b = paused["budget"]
    assert (b.tokens_in, b.tokens_out, b.tokens) == (4 * 321, 4 * 45, 4 * 366)
