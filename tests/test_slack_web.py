"""Tests for the Slack HTTP endpoint (approval/web.py) and the outbound poster (slack_client.py).

The endpoint tests use FastAPI's TestClient, which runs background tasks before returning, so
after `client.post(...)` the graph has already been resumed (or not) and we can assert on it.
Setup/helpers come from test_slack_approval.py: a real graph paused at request_approval.
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from approval.messages import APPROVE_ACTION
from approval.slack_client import HttpxSlackPoster, SlackApiError, request_approval_in_slack
from approval.web import MAX_BODY_BYTES, SLACK_INTERACTIONS_PATH, create_app
from test_slack_approval import APPROVER, INCIDENT, Setup, click_body, signed

# ---------------------------------------------------------------------------------------------
# POST /slack/interactions
# ---------------------------------------------------------------------------------------------


def client_for(s: Setup) -> TestClient:
    return TestClient(create_app(s.handler))


def post_click(client: TestClient, s: Setup, body: bytes, headers=None):
    return client.post(
        SLACK_INTERACTIONS_PATH,
        content=body,
        headers=headers if headers is not None else signed(body, s.clock),
    )


def test_signed_approve_click_is_acked_then_resumes_the_graph():
    s = Setup()
    response = post_click(client_for(s), s, click_body(APPROVE_ACTION, s.value()))

    assert response.status_code == 200
    assert response.content == b""  # plain ack; Slack shows nothing extra
    assert not s.still_paused()
    assert len(s.executor.remediations) == 1


def test_unsigned_request_gets_401_and_nothing_runs():
    s = Setup()
    response = post_click(client_for(s), s, click_body(APPROVE_ACTION, s.value()), headers={})

    assert response.status_code == 401
    assert response.json() == {"error": "missing_signature_headers"}
    assert s.still_paused()
    assert s.executor.remediations == []


def test_oversized_body_is_refused_before_verifying():
    s = Setup()
    response = post_click(client_for(s), s, b"x" * (MAX_BODY_BYTES + 1))
    assert response.status_code == 413
    assert s.still_paused()


def test_ignored_click_is_still_acked_so_slack_does_not_retry():
    s = Setup()
    body = click_body(APPROVE_ACTION, s.value(), user="U_STRANGER")
    response = post_click(client_for(s), s, body)

    assert response.status_code == 200
    assert s.still_paused()


def test_slack_retry_of_the_same_click_runs_the_fix_once():
    s = Setup()
    client = client_for(s)
    body = click_body(APPROVE_ACTION, s.value())

    assert post_click(client, s, body).status_code == 200
    assert post_click(client, s, body).status_code == 200  # Slack's retry delivery

    assert len(s.executor.remediations) == 1


def test_background_failure_is_logged_not_raised(caplog):
    s = Setup(approval_signing_key="")  # minting will raise inside the background task
    with caplog.at_level(logging.ERROR):
        response = post_click(client_for(s), s, click_body(APPROVE_ACTION, s.value()))

    assert response.status_code == 200
    assert any(r.getMessage() == "slack interaction failed" for r in caplog.records)
    assert s.still_paused()


def test_healthz():
    assert client_for(Setup()).get("/healthz").json() == {"status": "ok"}


# ---------------------------------------------------------------------------------------------
# Outbound: HttpxSlackPoster against a mocked Slack Web API
# ---------------------------------------------------------------------------------------------

BOT_TOKEN = "xoxb-test-only-token"


def poster_with(handler) -> tuple[HttpxSlackPoster, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    return HttpxSlackPoster(BOT_TOKEN, client=client), seen


def test_poster_sends_bearer_token_and_channel():
    poster, seen = poster_with(lambda r: httpx.Response(200, json={"ok": True}))

    asyncio.run(poster.post_message("C123", {"text": "hi", "blocks": []}))

    (request,) = seen
    assert request.url == "https://slack.com/api/chat.postMessage"
    assert request.headers["authorization"] == f"Bearer {BOT_TOKEN}"
    assert json.loads(request.content) == {"channel": "C123", "text": "hi", "blocks": []}


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        (
            httpx.Response(200, json={"ok": False, "error": "channel_not_found"}),
            "channel_not_found",
        ),
        (httpx.Response(500, text="boom"), "transport_error: HTTPStatusError"),
        (httpx.Response(200, text="<html>not json"), "invalid_json_response"),
        (httpx.Response(200, json=["not", "a", "dict"]), "bad_body"),
    ],
)
def test_poster_turns_slack_failures_into_slack_api_error(reply, error):
    poster, _ = poster_with(lambda r: reply)
    with pytest.raises(SlackApiError) as exc:
        asyncio.run(poster.post_message("C123", {"text": "hi"}))
    assert str(exc.value) == error
    assert BOT_TOKEN not in str(exc.value)


def test_poster_refuses_to_start_without_a_token():
    with pytest.raises(ValueError, match="SLACK_BOT_TOKEN"):
        HttpxSlackPoster("")


# ---------------------------------------------------------------------------------------------
# request_approval_in_slack + full loop
# ---------------------------------------------------------------------------------------------


class RecordingPoster:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def post_message(self, channel, message) -> None:
        self.sent.append((channel, message))


def test_request_approval_posts_to_the_configured_channel():
    s = Setup(slack_channel_id="C_INCIDENTS")
    poster = RecordingPoster()

    asyncio.run(request_approval_in_slack(poster, s.settings, s.pause))

    ((channel, message),) = poster.sent
    assert channel == "C_INCIDENTS"
    assert s.proposed.action in message["text"]


@pytest.mark.parametrize(
    ("overrides", "pause_edit", "match"),
    [
        ({}, {"kind": "question_for_human"}, "approval_request"),
        ({"slack_channel_id": ""}, {}, "SLACK_CHANNEL_ID"),
    ],
)
def test_request_approval_refuses_bad_input(overrides, pause_edit, match):
    s = Setup(**({"slack_channel_id": "C1"} | overrides))
    with pytest.raises(ValueError, match=match):
        asyncio.run(request_approval_in_slack(RecordingPoster(), s.settings, s.pause | pause_edit))


def test_full_loop_post_message_then_click_its_button_over_http():
    """Outbound and inbound together: the button value the human clicks is taken from the
    message we actually posted, and sent back through the HTTP endpoint."""
    s = Setup(slack_channel_id="C_INCIDENTS")
    poster = RecordingPoster()
    asyncio.run(request_approval_in_slack(poster, s.settings, s.pause))
    (_, message) = poster.sent[0]
    approve_button = next(
        el for el in message["blocks"][-1]["elements"] if el["action_id"] == APPROVE_ACTION
    )

    body = click_body(APPROVE_ACTION, approve_button["value"], user=APPROVER)
    response = post_click(client_for(s), s, body)

    assert response.status_code == 200
    assert json.loads(approve_button["value"])["incident_id"] == INCIDENT
    assert len(s.executor.remediations) == 1
    assert not s.still_paused()
