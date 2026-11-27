"""Building the real diagnoser from config (diagnoser/wiring.py, `python -m diagnoser`).

The end-to-end test runs the app built ONLY from Settings against Om's real executor server
(allowlist, Remediator, token check), with the LLM and Slack's API behind MockTransports and the
target's processes faked: intake -> LLM plan/command/assess/propose -> Slack message ->
Slack-signed Approve click -> token minted from the checkpoint -> remediation -> health -> resolved.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx
import pytest

from approval.messages import APPROVE_ACTION
from approval.slack_signature import compute_signature
from approval.web import SLACK_INTERACTIONS_PATH
from config import Settings
from contracts import CommandResult
from diagnoser.wiring import (
    DEV_TOKEN_ENV,
    StartupError,
    build_deps_from_settings,
    create_app_from_settings,
    startup_problems,
)
from executor import CommandRunner, IdempotencyStore, Remediator
from executor.runner import RunOutcome
from executor.server import AuthError, create_app
from security import CommandValidator

from test_remediation import RecordingRunner, target  # noqa: F401 (pytest fixture)

EXECUTOR_URL = "http://127.0.0.1:8080"
DEV_TOKEN = "d" * 40
KEY_HEX = "ab" * 32
INTAKE = "intake-token-for-tests-0123456789"
SIGNING = "slack-signing-secret-for-tests"
APPROVER = "U_SAI"
LOGS = ["docker", "logs", "--tail", "100", "ir-target-db-1"]
HEALTH = ["curl", "-fsS", "--max-time", "5", "http://127.0.0.1:8088/health"]


def settings(**over) -> Settings:
    fields = {
        "llm_model": "test-model",
        "openai_api_key": "sk-test",
        "llm_max_retries": 0,
        "incident_api_token": INTAKE,
        "slack_bot_token": "xoxb-test",
        "slack_signing_secret": SIGNING,
        "slack_channel_id": "C_INC",
        "approver_allowlist": APPROVER,
        "approval_signing_key": KEY_HEX,
        "executor_url": EXECUTOR_URL,
        "health_settle_s": 0,  # ignored until #16 merges (Settings ignores unknown keys)
        **over,
    }
    return Settings(_env_file=None, **fields)


ENV = {DEV_TOKEN_ENV: DEV_TOKEN}


# --- startup checks ---------------------------------------------------------------------------


def test_good_config_has_no_problems():
    assert startup_problems(settings(), ENV) == []


def test_empty_config_lists_every_problem_at_once():
    problems = startup_problems(Settings(_env_file=None, executor_url=EXECUTOR_URL), {})
    text = "\n".join(problems)
    for name in (
        "LLM_MODEL",
        "OPENAI_API_KEY",
        "INCIDENT_API_TOKEN",
        "SLACK_BOT_TOKEN",
        "SLACK_SIGNING_SECRET",
        "SLACK_CHANNEL_ID",
        "APPROVER_ALLOWLIST",
        "APPROVAL_SIGNING_KEY",
        DEV_TOKEN_ENV,
    ):
        assert name in text, name
    with pytest.raises(StartupError) as exc:
        build_deps_from_settings(Settings(_env_file=None), env={})
    assert len(exc.value.problems) == len(problems)


def test_problems_never_contain_secret_values():
    s = settings(slack_bot_token="xoxb-PLANTED-SECRET", approval_signing_key="zz-not-hex-SECRET")
    text = "\n".join(startup_problems(s, {DEV_TOKEN_ENV: "short-PLANTED"}))
    assert "APPROVAL_SIGNING_KEY" in text and DEV_TOKEN_ENV in text
    assert "PLANTED" not in text and "SECRET" not in text


@pytest.mark.parametrize(
    ("over", "env", "expected"),
    [
        ({}, {DEV_TOKEN_ENV: "x" * 31}, "at least 32"),
        ({"executor_url": "http://10.0.0.2:8080"}, ENV, "only sent to a localhost executor"),
        ({"openai_api_key": None}, ENV, "OPENAI_API_KEY"),
    ],
)
def test_unsafe_or_incomplete_config_is_refused(over, env, expected):
    assert any(expected in p for p in startup_problems(settings(**over), env))


def test_a_local_llm_needs_no_openai_key():
    s = settings(openai_api_key=None, llm_base_url="http://localhost:11434/v1")
    assert startup_problems(s, ENV) == []


def test_built_reasoner_uses_the_configured_limits():
    built = build_deps_from_settings(settings(max_hypotheses=2, max_replans=1), env=ENV)
    r = built.deps.reasoner
    assert r._max_hypotheses == 2 and r._limits["max_replans"] == 1
    assert len(built.closers) == 3


# --- end to end -------------------------------------------------------------------------------


class FakeProcesses(CommandRunner):
    """Om's real CommandRunner (allowlist check in diagnose()), with processes faked."""

    def __init__(self):
        super().__init__(CommandValidator())
        self.ran: list[list[str]] = []

    async def run_argv(self, argv, timeout_s, *, ctx=None, rule_id=None):
        self.ran.append(list(argv))
        out = "FATAL: the database system is shutting down" if argv == LOGS else "ok"
        return RunOutcome(
            "completed",
            CommandResult(exit_code=0, stdout=out, stderr="", truncated=False, duration_ms=1),
        )


async def dev_verify(token: str) -> str:
    # What `python -m executor` does with EXECUTOR_DEV_TOKEN.
    if not hmac.compare_digest(token, DEV_TOKEN):
        raise AuthError(401, "invalid_token")
    return "dev@localhost"


def llm_reply(payload: dict) -> dict:
    return {
        "id": "c",
        "object": "chat.completion",
        "created": 1,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": json.dumps(payload)},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 500, "completion_tokens": 50, "total_tokens": 550},
    }


LLM_SCRIPT = [
    {"hypotheses": [{"statement": "postgres is shutting down", "evidence_needed": ["db logs"]}]},
    {"command": LOGS, "reason": "read the db logs"},
    {"assessments": [{"id": "h1", "status": "confirmed", "confidence": 0.9, "reason": "FATAL"}]},
    {
        "action": "restart_service",
        "params": [{"name": "service", "value": "db"}],
        "rationale": "postgres stopped; restarting it brings it back",
        "rollback": None,
    },
]


def test_app_built_from_config_runs_an_incident_against_the_real_executor(target):  # noqa: F811
    processes, remediation = FakeProcesses(), RecordingRunner()
    executor_app = create_app(
        processes,
        Remediator(remediation, bytes.fromhex(KEY_HEX), IdempotencyStore(), target_dir=target),
        dev_verify,
        EXECUTOR_URL,
        CommandValidator().summary(),
    )
    executor_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=executor_app))

    llm_requests: list[dict] = []
    llm_script = list(LLM_SCRIPT)

    def llm(request: httpx.Request) -> httpx.Response:
        llm_requests.append(json.loads(request.content))
        return httpx.Response(200, json=llm_reply(llm_script.pop(0)))

    llm_http = httpx.AsyncClient(transport=httpx.MockTransport(llm))

    slack_posts: list[dict] = []

    def slack(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat.postMessage")
        assert request.headers["authorization"] == "Bearer xoxb-test"
        slack_posts.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    slack_http = httpx.AsyncClient(transport=httpx.MockTransport(slack))

    app = create_app_from_settings(
        settings(),
        env=ENV,
        executor_http=executor_http,
        llm_http=llm_http,
        slack_http=slack_http,
    )
    auth = {"Authorization": f"Bearer {INTAKE}"}
    alert = {
        "alert_id": "a1",
        "source": "manual",
        "service": "db",
        "summary": "api returns 503",
        "fired_at": "2026-11-27T12:00:00Z",
    }

    async def scenario() -> dict:
        # One event loop for everything, like uvicorn (the executor's sqlite idempotency store
        # is bound to the thread that created it, so TestClient's worker thread won't do).
        # ASGITransport finishes the app's background tasks before returning each response.
        transport = httpx.ASGITransport(app=app)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=transport, base_url="http://diagnoser") as client,
        ):
            r = await client.post("/incidents", json=alert, headers=auth)
            assert r.status_code == 202
            incident_id = r.json()["incident_id"]

            status = (await client.get(f"/incidents/{incident_id}", headers=auth)).json()
            assert status["waiting_for_approval"] is True
            assert status["proposed"] == {"action": "restart_service", "params": {"service": "db"}}
            assert processes.ran == [LOGS]  # the LLM's diagnostic passed the real allowlist
            assert remediation.calls == []  # nothing changed before a human said yes
            (post,) = slack_posts
            assert post["channel"] == "C_INC"

            # A human clicks Approve: a Slack-signed block_actions request.
            value = next(
                e["value"]
                for b in post["blocks"]
                for e in b.get("elements", [])
                if e.get("action_id") == APPROVE_ACTION
            )
            payload = {
                "type": "block_actions",
                "user": {"id": APPROVER},
                "actions": [{"action_id": APPROVE_ACTION, "value": value}],
            }
            body = urlencode({"payload": json.dumps(payload)}).encode()
            ts = str(int(time.time()))
            click = await client.post(
                SLACK_INTERACTIONS_PATH,
                content=body,
                headers={
                    "content-type": "application/x-www-form-urlencoded",
                    "x-slack-request-timestamp": ts,
                    "x-slack-signature": compute_signature(SIGNING, ts, body),
                },
            )
            assert click.status_code == 200
            return (await client.get(f"/incidents/{incident_id}", headers=auth)).json()

    done = asyncio.run(scenario())

    assert done["outcome"] == "resolved"
    # The executor verified the token the diagnoser minted (same APPROVAL_SIGNING_KEY).
    assert remediation.calls and remediation.calls[0][-2:] == ["restart", "db"]
    assert processes.ran[-1] == HEALTH
    b = done["budget"]
    assert (b["tokens_in"], b["tokens_out"], b["tokens"]) == (2000, 200, 2200)
    assert [r["response_format"]["json_schema"]["name"] for r in llm_requests] == [
        "PlanAnswer",
        "CommandAnswer",
        "AssessAnswer",
        "ProposeAnswer",
    ]
    # The prompt was built from the executor's card: its policy text is in the system message.
    assert "Container -> compose service" in llm_requests[0]["messages"][0]["content"]
    # The lifespan's shutdown ran: every client is closed.
    assert executor_http.is_closed and llm_http.is_closed and slack_http.is_closed


# --- python -m diagnoser ------------------------------------------------------------------------


def test_cli_refuses_to_start_with_bad_config_and_names_every_problem(tmp_path):
    # Fresh interpreter, cwd without a .env, only a planted secret in the environment.
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "SYSTEMROOT")}
    env["SLACK_BOT_TOKEN"] = "xoxb-PLANTED-SECRET"
    src = str(Path(__file__).resolve().parents[1] / "src")
    env["PYTHONPATH"] = src
    out = subprocess.run(  # noqa: S603 (fixed argv)
        [sys.executable, "-m", "diagnoser", "--port", "0"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 2
    assert "diagnoser can't start" in out.stderr
    assert "LLM_MODEL" in out.stderr and DEV_TOKEN_ENV in out.stderr
    assert "PLANTED" not in out.stderr + out.stdout
