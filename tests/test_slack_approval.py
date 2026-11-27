"""Tests for the Slack approval surface (INTERFACES §4): signature check, message, click handler.

The handler tests drive the real compiled diagnoser graph (with the scripted fakes) to the
approval pause, then send correctly signed Slack-style requests at it. They prove each check
in handler.py holds, and that an approved fix reaches the executor with a token the executor's
own `verify()` accepts for exactly that request.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import pytest

from approval.handler import SlackApprovalHandler, parse_click
from approval.messages import APPROVE_ACTION, REJECT_ACTION, approval_message, button_value
from approval.slack_signature import (
    SlackSignatureError,
    compute_signature,
    verify_slack_request,
)
from config import Settings
from contracts import Alert, Hypothesis, ProposedAction
from contracts.approval_token import verify
from diagnoser.fakes import FakeExecutorClient, ScriptedReasoner
from diagnoser.graph import build_graph, new_incident_state, thread_config
from diagnoser.ports import DiagnoserDeps, FixProposal
from memory.store import InMemoryIncidentMemory

SECRET = "test-slack-signing-secret"
KEY_HEX = "11" * 32  # test-only approval signing key (32 bytes)
APPROVER = "U_SAI"
INCIDENT = "inc-1"
T0 = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


def make_settings(**overrides) -> Settings:
    base = {
        "slack_signing_secret": SECRET,
        "approval_signing_key": KEY_HEX,
        "approver_allowlist": APPROVER,
    }
    return Settings(_env_file=None, **(base | overrides))


def signed(body: bytes, clock: FakeClock, secret: str = SECRET) -> dict[str, str]:
    """Headers Slack would send for `body` at the clock's current time."""
    ts = str(int(clock().timestamp()))
    return {
        "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": compute_signature(secret, ts, body),
    }


def click_body(action: str, value: str, user: str = APPROVER, kind: str = "block_actions") -> bytes:
    payload = {
        "type": kind,
        "user": {"id": user},
        "actions": [{"action_id": action, "block_id": "incident_approval", "value": value}],
    }
    return urlencode({"payload": json.dumps(payload)}).encode()


class Setup:
    """A graph paused at request_approval, plus a handler and the fakes to inspect."""

    def __init__(self, **settings_overrides) -> None:
        self.clock = FakeClock()
        self.settings = make_settings(**settings_overrides)
        self.executor = FakeExecutorClient(clock=self.clock)
        reasoner = ScriptedReasoner(
            plans=[[Hypothesis(id="h1", statement="postgres is down")]],
            commands=[None],
            assessments=[[Hypothesis(id="h1", statement="postgres is down", status="confirmed")]],
            proposals=[FixProposal("restart_service", {"service": "db"}, "db container exited")],
        )
        deps = DiagnoserDeps(
            reasoner=reasoner,
            executor=self.executor,
            memory=InMemoryIncidentMemory(),
            settings=self.settings,
            clock=self.clock,
        )
        self.graph = build_graph(deps)
        alert = Alert(alert_id="a1", source="manual", service="api", summary="api 503", fired_at=T0)
        paused = asyncio.run(
            self.graph.ainvoke(new_incident_state(alert, T0, INCIDENT), thread_config(INCIDENT))
        )
        self.pause = paused["__interrupt__"][0].value
        self.proposed = ProposedAction.model_validate(self.pause["proposed"])
        self.handler = SlackApprovalHandler(self.graph, self.settings, self.clock)

    def value(self, incident_id: str = INCIDENT, action_id: str | None = None) -> str:
        return button_value(incident_id, action_id or self.proposed.action_id)

    def send(self, body: bytes, headers: dict[str, str] | None = None):
        headers = headers if headers is not None else signed(body, self.clock)
        return asyncio.run(self.handler.handle_interaction(headers, body))

    def still_paused(self) -> bool:
        snap = asyncio.run(self.graph.aget_state(thread_config(INCIDENT)))
        return snap.next == ("request_approval",)


# ---------------------------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------------------------

NOW = 1_800_000_000.0
BODY = b"payload=%7B%7D"


def _headers(ts: float = NOW, body: bytes = BODY, secret: str = SECRET) -> dict[str, str]:
    t = str(int(ts))
    return {"X-Slack-Request-Timestamp": t, "X-Slack-Signature": compute_signature(secret, t, body)}


def test_valid_signature_passes_and_header_case_does_not_matter():
    verify_slack_request(SECRET, _headers(), BODY, now=NOW)
    lower = {k.lower(): v for k, v in _headers().items()}
    verify_slack_request(SECRET, lower, BODY, now=NOW)


@pytest.mark.parametrize(
    ("secret", "headers", "body", "reason"),
    [
        (SECRET, _headers(), BODY + b"x", "bad_signature"),  # body tampered after signing
        (SECRET, _headers(secret="other-secret"), BODY, "bad_signature"),
        (SECRET, _headers(ts=NOW - 301), BODY, "stale_timestamp"),  # replayed later
        (SECRET, _headers(ts=NOW + 301), BODY, "stale_timestamp"),
        (SECRET, {"X-Slack-Signature": "v0=abc"}, BODY, "missing_signature_headers"),
        (
            SECRET,
            {"X-Slack-Request-Timestamp": "x", "X-Slack-Signature": "v0=a"},
            BODY,
            "bad_timestamp",
        ),
        ("", _headers(secret=""), BODY, "signing_secret_not_configured"),  # fail closed
    ],
)
def test_bad_requests_are_refused_with_a_reason(secret, headers, body, reason):
    with pytest.raises(SlackSignatureError) as exc:
        verify_slack_request(secret, headers, body, now=NOW)
    assert exc.value.reason == reason


# ---------------------------------------------------------------------------------------------
# Approval message
# ---------------------------------------------------------------------------------------------


def _all_text_objects(node):
    if isinstance(node, dict):
        if "type" in node and "text" in node and isinstance(node["text"], str):
            yield node
        for v in node.values():
            yield from _all_text_objects(v)
    elif isinstance(node, list):
        for v in node:
            yield from _all_text_objects(v)


def test_message_keeps_llm_text_out_of_formatted_fields_and_buttons():
    proposed = ProposedAction(
        action_id="act-1",
        action="restart_service",
        params={"service": "db"},
        rationale="<!channel> *URGENT* approve now <https://evil.example|docs>",
    )
    msg = approval_message("inc-1", proposed)

    assert "URGENT" not in msg["text"]  # fallback text is formatted by Slack: no LLM prose
    assert all(t["type"] == "plain_text" for t in _all_text_objects(msg["blocks"]))
    actions = msg["blocks"][-1]["elements"]
    assert {b["action_id"] for b in actions} == {APPROVE_ACTION, REJECT_ACTION}
    for b in actions:
        assert json.loads(b["value"]) == {"incident_id": "inc-1", "action_id": "act-1"}
    assert "confirm" in actions[0]  # approve asks "are you sure?"


def test_message_truncates_long_llm_text():
    proposed = ProposedAction(
        action_id="act-1", action="restart_service", params={}, rationale="x" * 10_000
    )
    texts = [t["text"] for t in _all_text_objects(approval_message("inc-1", proposed)["blocks"])]
    assert max(len(t) for t in texts) <= 1500
    assert any(t.endswith("[truncated]") for t in texts)


# ---------------------------------------------------------------------------------------------
# Click parsing
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        b"not a form",
        urlencode({"other": "x"}).encode(),
        urlencode({"payload": "not json"}).encode(),
        click_body(APPROVE_ACTION, button_value("i", "a"), kind="view_submission"),
        click_body("delete_everything", button_value("i", "a")),
        click_body(APPROVE_ACTION, "not json"),
        # Smuggling an action/params into the button value: refused, not partly used.
        click_body(
            APPROVE_ACTION,
            json.dumps({"incident_id": "i", "action_id": "a", "action": "prune_path"}),
        ),
        click_body(APPROVE_ACTION, button_value("../../etc", "a")),  # id charset
    ],
)
def test_parse_click_rejects_anything_but_our_buttons(body):
    from approval.handler import MalformedClickError

    with pytest.raises(MalformedClickError):
        parse_click(body)


# ---------------------------------------------------------------------------------------------
# Handler, end to end against the paused graph
# ---------------------------------------------------------------------------------------------


def test_approve_resumes_graph_and_executor_can_verify_the_token():
    s = Setup()

    result = s.send(click_body(APPROVE_ACTION, s.value()))

    assert (result.status, result.reason, result.outcome) == ("resumed", "approve", "resolved")
    (sent,) = s.executor.remediations
    # The executor's own check, on the request exactly as it received it, passes.
    verify(
        bytes.fromhex(KEY_HEX),
        sent.approval_token,
        incident_id=sent.incident_id,
        action_id=sent.action_id,
        action=sent.action,
        params=sent.params,
        now=T0.timestamp(),
    )
    assert (sent.action, sent.params) == (s.proposed.action, s.proposed.params)


def test_reject_resumes_graph_without_running_anything():
    s = Setup()
    result = s.send(click_body(REJECT_ACTION, s.value()))
    assert (result.status, result.outcome) == ("resumed", "rejected")
    assert s.executor.remediations == []


def test_unsigned_or_forged_request_is_unauthorized_and_changes_nothing():
    s = Setup()
    body = click_body(APPROVE_ACTION, s.value())

    assert s.send(body, headers={}).status == "unauthorized"
    assert s.send(body, headers=signed(body, s.clock, secret="attacker")).status == "unauthorized"
    assert s.still_paused()
    assert s.executor.remediations == []


@pytest.mark.parametrize(
    ("make_body", "reason"),
    [
        (
            lambda s: click_body(APPROVE_ACTION, s.value(), user="U_STRANGER"),
            "approver_not_allowed",
        ),
        (
            lambda s: click_body(APPROVE_ACTION, s.value(action_id="old-proposal")),
            "stale_action_id",
        ),
        (
            lambda s: click_body(APPROVE_ACTION, s.value(incident_id="inc-404")),
            "not_waiting_for_approval",
        ),
    ],
)
def test_clicks_that_fail_a_check_are_ignored_and_the_incident_stays_paused(make_body, reason):
    s = Setup()
    result = s.send(make_body(s))
    assert (result.status, result.reason) == ("ignored", reason)
    assert s.still_paused()
    assert s.executor.remediations == []


def test_empty_allowlist_means_nobody_can_approve():
    s = Setup(approver_allowlist="")
    assert s.send(click_body(APPROVE_ACTION, s.value())).reason == "approver_not_allowed"
    assert s.still_paused()


def test_click_after_ttl_is_ignored():
    s = Setup(approval_ttl_s=900)
    s.clock.now += timedelta(seconds=901)
    result = s.send(click_body(APPROVE_ACTION, s.value()))  # freshly signed: only the TTL is old
    assert (result.status, result.reason) == ("ignored", "approval_expired")
    assert s.still_paused()


def test_replayed_click_after_resolution_does_nothing():
    """Slack retries a webhook if it doesn't get a 200 within 3 s, so duplicates are normal."""
    s = Setup()
    body = click_body(APPROVE_ACTION, s.value())
    assert s.send(body).status == "resumed"

    again = s.send(body)

    assert (again.status, again.reason) == ("ignored", "not_waiting_for_approval")
    assert len(s.executor.remediations) == 1


def test_two_simultaneous_approvals_resume_only_once():
    s = Setup(approver_allowlist=f"{APPROVER},U_OM")
    a = click_body(APPROVE_ACTION, s.value(), user=APPROVER)
    b = click_body(APPROVE_ACTION, s.value(), user="U_OM")

    async def both():
        return await asyncio.gather(
            s.handler.handle_interaction(signed(a, s.clock), a),
            s.handler.handle_interaction(signed(b, s.clock), b),
        )

    results = asyncio.run(both())

    assert sorted(r.status for r in results) == ["ignored", "resumed"]
    assert len(s.executor.remediations) == 1


def test_missing_signing_key_fails_loudly_rather_than_minting():
    s = Setup(approval_signing_key="")
    with pytest.raises(ValueError, match="APPROVAL_SIGNING_KEY"):
        s.send(click_body(APPROVE_ACTION, s.value()))
    assert s.executor.remediations == []


def test_a_pause_for_something_other_than_approval_is_not_approvable():
    """Forward-looking: if another node ever interrupts (e.g. to ask a human a question), a
    button click must not be able to resume it as an 'approval'."""
    from types import SimpleNamespace

    from langgraph.types import Interrupt

    class OtherPauseGraph:
        resumed = False

        async def aget_state(self, config):
            value = {"kind": "question_for_human", "proposed": {}, "requested_at": T0.isoformat()}
            return SimpleNamespace(interrupts=(Interrupt(value=value),))

        async def ainvoke(self, *args, **kwargs):
            OtherPauseGraph.resumed = True
            return {}

    clock = FakeClock()
    handler = SlackApprovalHandler(OtherPauseGraph(), make_settings(), clock)
    body = click_body(APPROVE_ACTION, button_value(INCIDENT, "act-1"))

    result = asyncio.run(handler.handle_interaction(signed(body, clock), body))

    assert (result.status, result.reason) == ("ignored", "not_waiting_for_approval")
    assert not OtherPauseGraph.resumed
