"""Remediator: the five gates (idempotency key, enabled action, approval token, params, not-run-twice)
and the exact steps each action plans. No Docker: a recording runner stands in for docker compose;
tests/test_remediation_docker.py runs the real fixes against the live stack."""

import asyncio
import shutil
import threading
from pathlib import Path

import pytest

from contracts import CommandResult, FailureInfo, RemediateRequest
from contracts.approval_token import mint
from executor.remediation import IdempotencyStore, Remediator, load_policy
from executor.runner import RunOutcome

REPO = Path(__file__).resolve().parents[1]
KEY = bytes.fromhex("ab" * 32)
T0 = 1_800_000_000.0
COMPOSE = (
    "docker",
    "compose",
    "-p",
    "ir-target",
    "-f",
    "docker-compose.yml",
    "--env-file",
    "runtime/stack.env",
)


class RecordingRunner:
    """Stands in for CommandRunner.run_argv: records argv, returns success (or a scripted result)."""

    def __init__(self, exit_codes=None, delay=0.0, fail=False):
        self.calls: list[list[str]] = []
        self.exit_codes = list(exit_codes or [])
        self.delay = delay
        self.fail = fail

    async def run_argv(self, argv, timeout_s, *, ctx=None, rule_id=None):
        self.calls.append(list(argv))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            return RunOutcome("failed", FailureInfo(reason="timeout after 120s"))
        code = self.exit_codes.pop(0) if self.exit_codes else 0
        return RunOutcome(
            "completed",
            CommandResult(exit_code=code, stdout="ok\n", stderr="", truncated=False, duration_ms=5),
        )


@pytest.fixture
def target(tmp_path):
    """A target dir with known-good/ and a runtime/ copy, like target/reset.sh leaves it."""
    shutil.copytree(REPO / "target" / "known-good", tmp_path / "known-good")
    shutil.copytree(REPO / "target" / "known-good", tmp_path / "runtime")
    return tmp_path


def make(target, runner=None, store=None):
    runner = runner or RecordingRunner()
    rem = Remediator(
        runner,
        KEY,
        store if store is not None else IdempotencyStore(),
        target_dir=target,
        clock=lambda: T0 + 10,
    )
    return rem, runner


def request(
    action="restart_service", params=None, *, action_id="act-1", token=None, key=KEY, **over
):
    params = {"service": "db"} if params is None else params
    token = token or mint(
        key,
        incident_id="inc-1",
        action_id=action_id,
        action=action,
        params=params,
        ttl_s=900,
        now=T0,
    )
    fields = dict(
        incident_id="inc-1",
        action_id=action_id,
        action=action,
        params=params,
        approval_token=token,
        idempotency_key=action_id,
    )
    fields.update(over)
    return RemediateRequest(**fields)


def go(rem, req):
    return asyncio.run(rem.remediate(req))


# --- gate 1: idempotency key ------------------------------------------------------------------


def test_idempotency_key_must_equal_action_id(target):
    rem, runner = make(target)
    out = go(rem, request(idempotency_key="other"))
    assert out.state == "rejected" and out.rule_id == "remediate.idempotency_key"
    assert runner.calls == []


# --- gate 2: enabled actions --------------------------------------------------------------------


@pytest.mark.parametrize("action", ["set_env", "prune_path"])
def test_disabled_actions_are_refused_even_when_approved(target, action):
    rem, runner = make(target)
    out = go(rem, request(action=action, params={"path": "/var/log"}))
    assert out.state == "rejected" and out.rule_id == "remediate.action_disabled"
    assert runner.calls == []


def test_unknown_action_is_refused(target):
    rem, runner = make(target)
    out = go(rem, request(action="drop_database", params={}))
    assert out.rule_id == "remediate.unknown_action" and runner.calls == []


# --- gate 3: approval token ------------------------------------------------------------------------


def test_valid_approval_runs_the_action(target):
    rem, runner = make(target)
    out = go(rem, request())
    assert out.state == "completed", out.result
    assert out.result.exit_code == 0
    assert runner.calls == [[*COMPOSE, "restart", "db"]]


@pytest.mark.parametrize(
    "tamper",
    [
        {"params": {"service": "api"}},  # approved db, asks for api
        {
            "action": "rollback_image",
            "params": {"service": "api", "tag": "good"},
        },  # approved a restart
        {"incident_id": "inc-2"},
    ],
)
def test_changing_anything_after_approval_is_refused(target, tamper):
    rem, runner = make(target)
    token = mint(
        KEY,
        incident_id="inc-1",
        action_id="act-1",
        action="restart_service",
        params={"service": "db"},
        ttl_s=900,
        now=T0,
    )
    out = go(rem, request(token=token, **tamper))
    assert out.state == "rejected"
    assert out.result.reason == "approval_invalid"
    assert out.rule_id is None  # INTERFACES §2.3: rule_id None for approval failures
    assert runner.calls == []


def test_wrong_key_and_expired_and_garbage_tokens(target):
    rem, runner = make(target)
    assert go(rem, request(key=bytes.fromhex("cd" * 32))).result.reason == "approval_invalid"
    late = Remediator(runner, KEY, IdempotencyStore(), target_dir=target, clock=lambda: T0 + 901)
    assert go(late, request()).result.reason == "approval_expired"
    assert go(rem, request(token="not-a-token")).result.reason == "approval_malformed"
    assert runner.calls == []


# --- gate 4: params (approval doesn't bypass policy) -----------------------------------------------


@pytest.mark.parametrize(
    ("action", "params"),
    [
        ("restart_service", {"service": "host"}),
        ("restart_service", {"service": "db", "extra": "x"}),
        ("restart_service", {}),
        ("restore_config", {"service": "proxy", "file": "api.env"}),  # wrong file for the service
        ("restore_config", {"service": "db", "file": "postgresql.conf"}),  # no rule for db
        ("restore_config", {"service": "proxy", "file": "../../etc/passwd"}),
        ("scale_memory_limit", {"service": "db", "mb": "99999"}),
        ("scale_memory_limit", {"service": "db", "mb": "16"}),
        ("scale_memory_limit", {"service": "db", "mb": "1g"}),
        ("rollback_image", {"service": "api", "tag": "latest"}),
        ("rollback_image", {"service": "api", "tag": "broken"}),
        ("rollback_image", {"service": "db", "tag": "good"}),
    ],
)
def test_bad_params_are_refused_even_with_a_valid_approval(target, action, params):
    rem, runner = make(target)
    out = go(rem, request(action=action, params=params))  # token minted for exactly these params
    assert out.state == "rejected" and out.rule_id == "remediate.params", out.result
    assert runner.calls == []
    # nothing on disk changed either
    assert (target / "runtime" / "stack.env").read_text() == (
        target / "known-good" / "stack.env"
    ).read_text()


# --- gate 5: never twice ---------------------------------------------------------------------------


def test_retry_returns_stored_outcome_without_rerunning(target):
    rem, runner = make(target)
    first = go(rem, request())
    second = go(rem, request())
    assert len(runner.calls) == 1
    assert second == first


def test_idempotency_survives_an_executor_restart(target, tmp_path):
    db = tmp_path / "state.sqlite3"
    rem1, runner1 = make(target, store=IdempotencyStore(db))
    go(rem1, request())
    rem2, runner2 = make(target, store=IdempotencyStore(db))  # "restarted" executor
    out = go(rem2, request())
    assert out.state == "completed"
    assert runner2.calls == []


def test_interrupted_attempt_is_not_rerun(target, tmp_path):
    db = tmp_path / "state.sqlite3"
    store = IdempotencyStore(db)
    req = request()
    from executor.remediation import _request_hash

    store.begin(req.idempotency_key, _request_hash(req))  # crashed after begin, before finish
    rem, runner = make(target, store=IdempotencyStore(db))
    out = go(rem, req)
    assert out.state == "failed" and out.rule_id == "remediate.interrupted"
    assert runner.calls == []


def test_concurrent_identical_requests_run_once(target):
    rem, runner = make(target, runner=RecordingRunner(delay=0.2))

    async def both():
        return await asyncio.gather(rem.remediate(request()), rem.remediate(request()))

    a, b = asyncio.run(both())
    assert len(runner.calls) == 1
    assert a == b


# --- plans: what each action actually does ----------------------------------------------------------


def test_restore_config_proxy_copies_in_place_and_reloads(target):
    runtime = target / "runtime" / "nginx.conf"
    runtime.write_text("upstream api_upstream { server api:8001; }\n")  # the fault
    inode = runtime.stat().st_ino
    rem, runner = make(target)
    out = go(rem, request("restore_config", {"service": "proxy", "file": "nginx.conf"}))
    assert out.state == "completed"
    assert runtime.read_text() == (target / "known-good" / "nginx.conf").read_text()
    assert runtime.stat().st_ino == inode  # same inode: the bind mount sees the change
    assert runner.calls == [[*COMPOSE, "exec", "-T", "proxy", "nginx", "-s", "reload"]]


def test_restore_config_api_recreates_and_cache_restarts(target):
    rem, runner = make(target)
    go(rem, request("restore_config", {"service": "api", "file": "api.env"}, action_id="a1"))
    go(rem, request("restore_config", {"service": "cache", "file": "redis.conf"}, action_id="a2"))
    assert runner.calls == [
        [*COMPOSE, "up", "-d", "--no-deps", "--force-recreate", "api"],
        [*COMPOSE, "restart", "cache"],
    ]


def test_rollback_image_sets_tag_and_recreates(target):
    env = target / "runtime" / "stack.env"
    env.write_text(env.read_text().replace("API_IMAGE_TAG=good", "API_IMAGE_TAG=broken"))
    rem, runner = make(target)
    go(rem, request("rollback_image", {"service": "api", "tag": "good"}))
    assert "API_IMAGE_TAG=good" in env.read_text() and "broken" not in env.read_text()
    assert runner.calls == [[*COMPOSE, "up", "-d", "--no-deps", "api"]]


def test_scale_memory_limit_sets_var_and_recreates(target):
    rem, runner = make(target)
    go(rem, request("scale_memory_limit", {"service": "db", "mb": "512"}))
    assert "DB_MEM_LIMIT=512m" in (target / "runtime" / "stack.env").read_text()
    assert runner.calls == [[*COMPOSE, "up", "-d", "--no-deps", "db"]]


def test_failed_step_stops_the_plan_and_is_reported(target):
    rem, runner = make(target, runner=RecordingRunner(fail=True))
    out = go(rem, request("restore_config", {"service": "proxy", "file": "nginx.conf"}))
    assert out.state == "failed" and out.rule_id == "remediate.step"
    assert "nginx -s reload" in out.result.reason


def test_nonzero_exit_is_completed_with_that_exit_code(target):
    rem, _ = make(target, runner=RecordingRunner(exit_codes=[1]))
    out = go(rem, request())
    assert out.state == "completed" and out.result.exit_code == 1


# --- policy file -----------------------------------------------------------------------------------


def test_policy_loads_and_matches_compose():
    import yaml

    compose = yaml.safe_load((REPO / "target" / "docker-compose.yml").read_text())
    policy = load_policy()
    assert set(policy.services) == set(compose["services"])
    assert policy.compose_project == compose["name"]
    for rule in policy.config_files.values():
        assert (REPO / "target" / "known-good" / rule.file).is_file()
    stack_env = (REPO / "target" / "known-good" / "stack.env").read_text()
    for rule in [*policy.memory_limits.values(), *policy.image_tags.values()]:
        assert f"{rule.var}=" in stack_env


def test_policy_refuses_actions_outside_the_catalog(tmp_path):
    import yaml

    data = yaml.safe_load((REPO / "src" / "executor" / "remediation.yaml").read_text())
    data["enabled_actions"].append("run_shell")
    p = tmp_path / "bad.yaml"
    p.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="not in contracts.catalog"):
        load_policy(p)


# --- IdempotencyStore across threads (Sai, #20: TestClient runs the app in a worker thread) ----


def _outcome(n: int) -> RunOutcome:
    return RunOutcome(
        "completed",
        CommandResult(exit_code=0, stdout=f"{n}", stderr="", truncated=False, duration_ms=1),
        "remediate.done",
    )


@pytest.mark.parametrize("where", ["memory", "file"])
def test_store_created_in_one_thread_works_in_another(tmp_path, where):
    store = IdempotencyStore(":memory:" if where == "memory" else tmp_path / "s.sqlite3")
    errors = []

    def use():
        try:
            store.begin("k1", "h1")
            store.finish("k1", _outcome(1))
        except Exception as e:  # noqa: BLE001 (hand any failure to the test thread)
            errors.append(e)

    th = threading.Thread(target=use)
    th.start()
    th.join()
    assert errors == []
    hash_, status, raw = store.get("k1")
    assert (hash_, status) == ("h1", "done") and raw is not None


def test_store_survives_many_threads_at_once():
    store = IdempotencyStore()
    errors = []

    def use(n):
        try:
            store.begin(f"k{n}", f"h{n}")
            store.finish(f"k{n}", _outcome(n))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=use, args=(n,)) for n in range(32)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert errors == []
    assert all(store.get(f"k{n}")[1] == "done" for n in range(32))
