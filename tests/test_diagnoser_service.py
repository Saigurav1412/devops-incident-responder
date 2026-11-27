"""Tests for the diagnoser HTTP service (src/diagnoser/service.py).

Everything goes through real HTTP routes (FastAPI TestClient) and the real compiled graph; only
the reasoner, executor and Slack poster are fakes. TestClient runs background tasks before it
returns, so after POST /incidents the graph has already run to its pause or its end.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from approval.messages import APPROVE_ACTION, REJECT_ACTION
from approval.slack_client import SlackApiError
from approval.web import SLACK_INTERACTIONS_PATH
from contracts import Hypothesis
from diagnoser.fakes import FakeExecutorClient, ScriptedReasoner
from diagnoser.ports import DiagnoserDeps, FixProposal
from diagnoser.service import create_service
from memory.store import InMemoryIncidentMemory
from test_slack_approval import FakeClock, click_body, make_settings, signed

API_TOKEN = "test-incident-api-token"
AUTH = {"Authorization": f"Bearer {API_TOKEN}"}
ALERT = {
    "alert_id": "alert-1",
    "source": "manual",
    "service": "api",
    "summary": "api returns 503",
    "fired_at": "2026-11-26T12:00:00Z",
}


def hyp(statement, status="open", hid="h1"):
    return Hypothesis(id=hid, statement=statement, status=status)


def one_fix_reasoner() -> ScriptedReasoner:
    return ScriptedReasoner(
        plans=[[hyp("postgres is down")]],
        commands=[None],
        assessments=[[hyp("postgres is down", "confirmed")]],
        proposals=[FixProposal("restart_service", {"service": "db"}, "db container exited")],
    )


class RecordingPoster:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[str, dict]] = []
        self.fail = fail

    async def post_message(self, channel, message) -> None:
        if self.fail:
            raise SlackApiError("channel_not_found")
        self.sent.append((channel, message))

    def approve_value(self, index: int = -1) -> str:
        """The Approve button's value from the index-th posted message."""
        _, message = self.sent[index]
        buttons = message["blocks"][-1]["elements"]
        return next(b["value"] for b in buttons if b["action_id"] == APPROVE_ACTION)


class Service:
    def __init__(self, reasoner=None, executor=None, poster=None, **settings_overrides):
        self.clock = FakeClock()
        self.settings = make_settings(
            **({"incident_api_token": API_TOKEN, "slack_channel_id": "C_INC"} | settings_overrides)
        )
        self.executor = executor or FakeExecutorClient(clock=self.clock)
        self.poster = poster or RecordingPoster()
        deps = DiagnoserDeps(
            reasoner=reasoner or one_fix_reasoner(),
            executor=self.executor,
            memory=InMemoryIncidentMemory(),
            settings=self.settings,
            clock=self.clock,
        )
        self.client = TestClient(create_service(deps, self.poster))

    def start(self, alert=ALERT, headers=AUTH):
        return self.client.post("/incidents", json=alert, headers=headers)

    def status(self, incident_id, headers=AUTH):
        return self.client.get(f"/incidents/{incident_id}", headers=headers)

    def click(self, action, value, user="U_SAI"):
        body = click_body(action, value, user=user)
        return self.client.post(
            SLACK_INTERACTIONS_PATH, content=body, headers=signed(body, self.clock)
        )


# ---------------------------------------------------------------------------------------------
# Auth and validation
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-token"},
        {"Authorization": API_TOKEN},  # missing "Bearer"
        {"Authorization": f"Basic {API_TOKEN}"},
    ],
)
def test_intake_and_status_require_the_bearer_token(headers):
    s = Service()
    assert s.start(headers=headers).status_code == 401
    assert s.status("anything", headers=headers).status_code == 401
    assert s.executor.calls == [] and s.poster.sent == []


def test_unset_token_refuses_everything():
    s = Service(incident_api_token="")
    response = s.start()
    assert response.status_code == 503
    assert response.json()["detail"] == "incident_api_token_not_configured"


@pytest.mark.parametrize(
    "alert",
    [
        ALERT | {"source": "the-internet"},  # not an allowed source
        ALERT | {"summary": "x" * 501},  # over the 500-char cap
        ALERT | {"shell": "true"},  # unknown field (contracts use extra="forbid")
        {k: v for k, v in ALERT.items() if k != "fired_at"},
        ALERT | {"fired_at": "2026-11-26T12:00:00"},  # naive datetime, no timezone
    ],
)
def test_invalid_alerts_are_rejected_with_422(alert):
    s = Service()
    assert s.start(alert=alert).status_code == 422
    assert s.poster.sent == []


# ---------------------------------------------------------------------------------------------
# The flow
# ---------------------------------------------------------------------------------------------


def test_intake_runs_graph_posts_approval_and_status_shows_waiting():
    s = Service()

    response = s.start()

    assert response.status_code == 202
    incident_id = response.json()["incident_id"]
    assert response.json()["status_url"] == f"/incidents/{incident_id}"
    ((channel, message),) = s.poster.sent
    assert channel == "C_INC" and "restart_service" in message["text"]

    status = s.status(incident_id).json()
    assert status["outcome"] == "open"
    assert status["waiting_for_approval"] is True
    assert status["proposed"] == {"action": "restart_service", "params": {"service": "db"}}
    assert "observations" not in status  # raw command output is never returned
    # The token split is exposed for the eval's cost numbers (0 here: the scripted reasoner
    # reports no split).
    assert set(status["budget"]) == {
        "iterations",
        "replans",
        "tool_calls",
        "tokens",
        "tokens_in",
        "tokens_out",
    }


def test_full_incident_over_http_intake_slack_click_status():
    s = Service()
    incident_id = s.start().json()["incident_id"]

    assert s.click(APPROVE_ACTION, s.poster.approve_value()).status_code == 200

    status = s.status(incident_id).json()
    assert status["outcome"] == "resolved"
    assert status["waiting_for_approval"] is False
    assert len(s.executor.remediations) == 1


def test_reject_over_http_ends_incident():
    s = Service()
    incident_id = s.start().json()["incident_id"]
    s.click(REJECT_ACTION, s.poster.approve_value())
    assert s.status(incident_id).json()["outcome"] == "rejected"
    assert s.executor.remediations == []


def test_second_approval_is_posted_when_the_first_fix_fails_its_health_check():
    """The case the on_pause hook exists for: without it the incident would wait for an
    approval nobody was asked for."""
    reasoner = ScriptedReasoner(
        plans=[[hyp("postgres is down")], [hyp("db password changed")]],
        commands=[None],
        assessments=[
            [hyp("postgres is down", "confirmed")],
            [hyp("postgres is down", "refuted"), hyp("db password changed", "confirmed", "h2")],
        ],
        proposals=[
            FixProposal("restart_service", {"service": "db"}, "db exited"),
            FixProposal("restore_config", {"service": "api"}, "bad url"),
        ],
    )
    # The executor's clock only timestamps observations, so a separate FakeClock is fine here.
    s = Service(
        reasoner=reasoner, executor=FakeExecutorClient(clock=FakeClock(), health=[False, True])
    )
    incident_id = s.start().json()["incident_id"]

    s.click(APPROVE_ACTION, s.poster.approve_value(0))  # first fix runs, health fails, replan

    assert len(s.poster.sent) == 2
    assert "restore_config" in s.poster.sent[1][1]["text"]
    assert s.status(incident_id).json()["waiting_for_approval"] is True

    s.click(APPROVE_ACTION, s.poster.approve_value(1))
    assert s.status(incident_id).json()["outcome"] == "resolved"


def test_incident_that_needs_no_approval_posts_nothing():
    s = Service(reasoner=ScriptedReasoner(plans=[[]]))  # nothing to test -> escalated
    incident_id = s.start().json()["incident_id"]
    assert s.status(incident_id).json()["outcome"] == "escalated"
    assert s.poster.sent == []


def test_each_alert_gets_its_own_incident():
    s = Service(reasoner=ScriptedReasoner(plans=[[]]))
    ids = {s.start().json()["incident_id"] for _ in range(3)}
    assert len(ids) == 3


def test_unknown_incident_is_404():
    assert Service().status("no-such-incident").status_code == 404


# ---------------------------------------------------------------------------------------------
# Failures in the background
# ---------------------------------------------------------------------------------------------


def test_slack_post_failure_is_logged_and_incident_stays_safely_paused(caplog):
    s = Service(poster=RecordingPoster(fail=True))
    with caplog.at_level(logging.ERROR):
        incident_id = s.start().json()["incident_id"]

    assert any(r.getMessage() == "approval request could not be posted" for r in caplog.records)
    status = s.status(incident_id).json()
    assert status["waiting_for_approval"] is True
    assert s.executor.remediations == []


def test_crash_inside_the_graph_is_logged_not_raised(caplog):
    class BrokenReasoner(ScriptedReasoner):
        async def plan(self, state):
            raise RuntimeError("model API down")

    s = Service(reasoner=BrokenReasoner())
    with caplog.at_level(logging.ERROR):
        response = s.start()

    assert response.status_code == 202
    assert any(r.getMessage() == "incident run failed" for r in caplog.records)
    assert s.poster.sent == []
