"""Posting the approval message to Slack (the outbound half of the approval surface).

WHAT: `SlackPoster` is the interface ("post this message to this channel"); `HttpxSlackPoster`
      implements it with one call to Slack's Web API method `chat.postMessage`.
      `request_approval_in_slack()` turns the graph's approval pause into that message.
WHY an interface: tests (and local dry runs) plug in a recording fake, so nothing here needs a
      Slack workspace or network access to be tested. Same idea as the diagnoser's ports.

Slack Web API facts this relies on (https://api.slack.com/methods/chat.postMessage):
  * POST https://slack.com/api/chat.postMessage, `Authorization: Bearer <bot token>`, JSON body.
  * Slack answers HTTP 200 even for many failures and reports them as `{"ok": false,
    "error": "..."}` in the body, so checking the status code alone is not enough.
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx

from approval.messages import approval_message
from config import Settings
from contracts import ProposedAction
from diagnoser.nodes import APPROVAL_INTERRUPT_KIND

SLACK_API_BASE = "https://slack.com/api"


class SlackApiError(Exception):
    """Slack refused or failed the call. The message is Slack's short error code (safe to log;
    it never contains the token)."""


class SlackPoster(Protocol):
    async def post_message(self, channel: str, message: dict[str, Any]) -> None: ...


class HttpxSlackPoster:
    """Real poster. Pass a shared `httpx.AsyncClient` in a long-running service so connections
    are reused; tests pass one with a MockTransport instead of the network."""

    def __init__(
        self,
        bot_token: str,
        client: httpx.AsyncClient | None = None,
        base_url: str = SLACK_API_BASE,
        timeout_s: float = 10.0,
    ) -> None:
        if not bot_token:
            raise ValueError("SLACK_BOT_TOKEN is not set")
        self._token = bot_token
        self._client = client or httpx.AsyncClient()
        self._base_url = base_url
        self._timeout_s = timeout_s

    async def post_message(self, channel: str, message: dict[str, Any]) -> None:
        try:
            response = await self._client.post(
                f"{self._base_url}/chat.postMessage",
                headers={"Authorization": f"Bearer {self._token}"},
                json={"channel": channel, **message},
                timeout=self._timeout_s,
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError as exc:
            # Type name only: some httpx errors include the request, and we never want the
            # Authorization header anywhere near a log line.
            raise SlackApiError(f"transport_error: {type(exc).__name__}") from None
        except ValueError:
            raise SlackApiError("invalid_json_response") from None
        if not isinstance(data, dict) or data.get("ok") is not True:
            error = data.get("error", "unknown_error") if isinstance(data, dict) else "bad_body"
            raise SlackApiError(str(error))


async def request_approval_in_slack(
    poster: SlackPoster, settings: Settings, pause: dict[str, Any]
) -> None:
    """Post the approval message for one approval pause (the graph's `__interrupt__` value).

    Called by whatever runs the graph (the incident endpoint, the eval harness) right after
    `ainvoke` returns with `__interrupt__`. Raises ValueError for anything that isn't an
    approval pause, so a future different kind of pause is never shown as an approval.
    """
    if not isinstance(pause, dict) or pause.get("kind") != APPROVAL_INTERRUPT_KIND:
        raise ValueError("not an approval_request pause")
    if not settings.slack_channel_id:
        raise ValueError("SLACK_CHANNEL_ID is not set")
    proposed = ProposedAction.model_validate(pause["proposed"])
    await poster.post_message(
        settings.slack_channel_id, approval_message(pause["incident_id"], proposed)
    )
