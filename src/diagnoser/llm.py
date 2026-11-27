"""The chat-model seam under the LLM reasoner: one async call, messages in, text + usage out.

WHAT: `ChatModel` protocol, `ChatMessage`, `ChatResult`, `ChatModelError`.
WHY:  the LLM provider is still an open decision (DECISIONS.md). Everything above this seam
      (prompts, rendering untrusted data, parsing and validating output, retries, token
      accounting) is provider-independent and tested with a scripted fake. A provider adapter
      only has to turn messages into one API call and report token usage.
HOW:  `LLMReasoner` calls `complete()` with the system + user messages and the JSON schema of
      the answer it expects. An adapter MAY pass the schema to the provider's structured-output
      feature; the reasoner validates the reply itself either way, so a provider without that
      feature (or one that ignores it) is still safe.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class ChatMessage:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True)
class ChatResult:
    text: str
    input_tokens: int
    output_tokens: int

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class ChatModelError(Exception):
    """The provider call failed (network, auth, rate limit, refusal). Message must not contain
    the API key or the prompt: it is logged."""


class ChatModel(Protocol):
    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        schema: dict[str, Any],
        schema_name: str,
        max_output_tokens: int,
    ) -> ChatResult: ...
