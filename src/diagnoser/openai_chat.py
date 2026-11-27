"""ChatModel adapter for any OpenAI-compatible chat-completions API (OpenAI, Ollama, ...).

WHAT: `OpenAIChatModel.complete()` sends the reasoner's messages with the answer's JSON schema in
      strict structured-output mode and returns the text plus token usage. `strict_schema()`
      turns a Pydantic JSON schema into the shape strict mode requires.
WHY:  decided with Om on PR #14: one adapter on the `openai` SDK, pointed at a provider by
      `LLM_BASE_URL` (empty = OpenAI), so the main eval model and a local dev model are an .env
      change, not a code change. Everything provider-independent (prompts, fencing, validation,
      repair, safe defaults, token accounting) stays in reasoner.py and is not repeated here.
HOW:  1. `response_format={"type": "json_schema", ..., "strict": True}` with the strict schema.
      2. If the server answers 400 (some OpenAI-compatible servers don't support json_schema),
         retry the same call once with `{"type": "json_object"}`. The reasoner validates every
         reply against the full Pydantic model either way, so no bound is lost.
      3. Every SDK error becomes ChatModelError("<status> <ErrorType>"): no key, no prompt, no
         response body, because the reasoner logs the message.

The model name, endpoint, key, timeout and retries all come from Settings (AGENTS.md rule 6).
"""

from __future__ import annotations

import copy
from typing import Any

import httpx
import openai
from openai import AsyncOpenAI

from config import Settings
from diagnoser.llm import ChatMessage, ChatModelError, ChatResult
from obs.logging import get_logger

log = get_logger(__name__)

# Sent instead of a key to an OpenAI-compatible server that needs none (e.g. local Ollama).
# Only used when LLM_BASE_URL is set; the OpenAI API itself always needs a real key.
NO_KEY_PLACEHOLDER = "not-needed"  # noqa: S105 (not a secret: a fixed placeholder)


# Adapted from https://github.com/openai/openai-python/blob/main/src/openai/lib/_pydantic.py
# (`_ensure_strict_json_schema`) — that module is private API, so the rules are copied here and
# tested against our own answer models instead of importing it.
def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """A copy of `schema` that strict structured outputs accept.

    Strict mode needs, for every object: `additionalProperties: false` and every property listed
    in `required` (optional fields become required-but-nullable/defaulted in the reply; our
    Pydantic models then apply the real rules). `None` defaults are dropped and a `$ref` that has
    sibling keys is inlined.
    """
    root = copy.deepcopy(schema)
    return _strict(root, root)


def _strict(node: Any, root: dict[str, Any]) -> Any:
    if not isinstance(node, dict):
        return node
    for defs_key in ("$defs", "definitions"):
        defs = node.get(defs_key)
        if isinstance(defs, dict):
            for name, sub in defs.items():
                defs[name] = _strict(sub, root)
    if node.get("type") == "object" and "additionalProperties" not in node:
        node["additionalProperties"] = False
    props = node.get("properties")
    if isinstance(props, dict):
        node["required"] = list(props)
        node["properties"] = {k: _strict(v, root) for k, v in props.items()}
    if isinstance(node.get("items"), dict):
        node["items"] = _strict(node["items"], root)
    for key in ("anyOf", "allOf"):
        if isinstance(node.get(key), list):
            node[key] = [_strict(v, root) for v in node[key]]
    if key_all := node.get("allOf"):
        if len(key_all) == 1:
            node.update(key_all[0])
            node.pop("allOf")
    if "default" in node and node["default"] is None:
        node.pop("default")
    ref = node.get("$ref")
    if isinstance(ref, str) and len(node) > 1:
        target = _resolve(root, ref)
        node.pop("$ref")
        merged = {**copy.deepcopy(target), **node}
        return _strict(merged, root)
    return node


def _resolve(root: dict[str, Any], ref: str) -> dict[str, Any]:
    if not ref.startswith("#/"):
        raise ValueError(f"only local $refs are supported, got {ref!r}")
    node: Any = root
    for part in ref[2:].split("/"):
        node = node[part]
    if not isinstance(node, dict):
        raise ValueError(f"$ref {ref!r} doesn't point at an object")
    return node


def _error(exc: openai.OpenAIError) -> ChatModelError:
    """Status and error type only. SDK exception messages can quote the response body."""
    status = getattr(exc, "status_code", None)
    label = f"{status} {type(exc).__name__}" if status else type(exc).__name__
    return ChatModelError(label)


class OpenAIChatModel:
    """ChatModel over `AsyncOpenAI`. One instance per process (it reuses its connection pool).

    `http_client` is for tests (an httpx.AsyncClient with a MockTransport); leave it None.
    """

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str | None = None,
        timeout_s: float = 60.0,
        max_retries: int = 2,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not model:
            raise ValueError("LLM_MODEL is not set")
        self._model = model
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url or None,
            timeout=timeout_s,
            max_retries=max_retries,
            http_client=http_client,
        )

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        schema: dict[str, Any],
        schema_name: str,
        max_output_tokens: int,
    ) -> ChatResult:
        payload = [{"role": m.role, "content": m.content} for m in messages]
        strict = {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "schema": strict_schema(schema), "strict": True},
        }
        try:
            return await self._create(payload, strict, max_output_tokens)
        except openai.BadRequestError as exc:
            # Some OpenAI-compatible servers reject json_schema. JSON mode still gets a JSON
            # object back (our system prompt says "JSON", which JSON mode requires), and the
            # reasoner validates it. A 400 for another reason will fail again and be reported.
            log.warning(
                "json_schema refused, retrying with json_object",
                extra={"schema_name": schema_name, "error": str(_error(exc))},
            )
        try:
            return await self._create(payload, {"type": "json_object"}, max_output_tokens)
        except openai.OpenAIError as exc:
            raise _error(exc) from None

    async def _create(
        self, messages: list[dict[str, str]], response_format: dict[str, Any], max_tokens: int
    ) -> ChatResult:
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=messages,  # type: ignore[arg-type]
                response_format=response_format,  # type: ignore[arg-type]
                max_completion_tokens=max_tokens,
            )
        except openai.BadRequestError:
            raise  # complete() decides whether to fall back
        except openai.OpenAIError as exc:
            raise _error(exc) from None

        if not response.choices:
            raise ChatModelError("empty_response: no choices")
        message = response.choices[0].message
        if getattr(message, "refusal", None):
            raise ChatModelError("refusal")  # the text is not logged: it may quote the prompt
        if not message.content:
            raise ChatModelError("empty_response: no content")
        usage = response.usage
        if usage is None:
            log.warning("llm response without usage; counting 0 tokens", extra={})
        return ChatResult(
            text=message.content,
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
        )

    async def aclose(self) -> None:
        await self._client.close()


def chat_model_from_settings(
    settings: Settings, *, http_client: httpx.AsyncClient | None = None
) -> OpenAIChatModel:
    """Build the adapter from config. Fails loudly on a missing model or (for OpenAI) key."""
    key = settings.openai_api_key.get_secret_value() if settings.openai_api_key else ""
    if not key:
        if not settings.llm_base_url:
            raise ValueError("OPENAI_API_KEY is not set (needed unless LLM_BASE_URL is set)")
        key = NO_KEY_PLACEHOLDER
    return OpenAIChatModel(
        model=settings.llm_model,
        api_key=key,
        base_url=settings.llm_base_url or None,
        timeout_s=settings.llm_timeout_s,
        max_retries=settings.llm_max_retries,
        http_client=http_client,
    )
