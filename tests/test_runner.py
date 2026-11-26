"""CommandRunner: runs only validated argv, without a shell, bounded in time and output, with a
minimal environment, and maps every outcome to the right contract model (INTERFACES §2.3).

Most tests use a permissive fake validator plus small Python child processes, so they can
exercise exit codes, timeouts and huge output without Docker. The validator itself is tested in
test_allowlist.py; a few tests here use the real one to prove the two are wired together.
"""

import asyncio
import sys
import time
from pathlib import Path

import pytest

from contracts import CommandResult, DiagnoseRequest, FailureInfo, RejectionInfo
from executor import CommandRunner
from executor.redact import redact
from security import CommandValidator
from security.allowlist import Decision

PY_DIR = str(Path(sys.executable).parent)
PY = Path(sys.executable).name


class AllowAll:
    """Test-only validator: allows everything. Never used outside tests."""

    def check(self, argv):
        return Decision(True, "test.allow_all", "allowed", tuple(argv))


def run(runner: CommandRunner, *argv: str, timeout_s: int = 10):
    return asyncio.run(
        runner.diagnose(
            DiagnoseRequest(incident_id="inc-1", command=list(argv), timeout_s=timeout_s)
        )
    )


def py_runner(tmp_path, **kw) -> CommandRunner:
    return CommandRunner(AllowAll(), target_dir=tmp_path, search_path=PY_DIR, **kw)


# --- outcome mapping ---------------------------------------------------------------------------


def test_rejected_commands_never_run(tmp_path):
    marker = tmp_path / "ran"
    runner = CommandRunner(CommandValidator(), target_dir=tmp_path)
    out = run(runner, "touch", str(marker))
    assert out.state == "rejected"
    assert isinstance(out.result, RejectionInfo)
    assert out.result.rule_id == "binary.not_allowed"
    assert not marker.exists()


def test_nonzero_exit_is_still_completed(tmp_path):
    # A failing command is evidence for the diagnoser, not an executor failure.
    out = run(py_runner(tmp_path), PY, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)")
    assert out.state == "completed"
    assert isinstance(out.result, CommandResult)
    assert out.result.exit_code == 3
    assert out.result.stderr == "boom"
    assert out.result.truncated is False


def test_stdout_and_duration(tmp_path):
    out = run(py_runner(tmp_path), PY, "-c", "print('hello')")
    assert out.result.stdout == "hello\n"
    assert out.result.duration_ms >= 0


def test_binary_missing_from_safe_path_fails(tmp_path):
    out = run(CommandRunner(AllowAll(), target_dir=tmp_path), "definitely-not-a-binary-599")
    assert out.state == "failed"
    assert isinstance(out.result, FailureInfo)
    assert "binary_not_found" in out.result.reason


def test_inherited_path_is_ignored(tmp_path, monkeypatch):
    # A planted `docker` early on $PATH must not be what runs.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    planted = fake_bin / "uptime"
    planted.write_text("#!/bin/sh\necho PLANTED\n")
    planted.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:/usr/bin:/bin")
    out = run(CommandRunner(CommandValidator(), target_dir=tmp_path), "uptime")
    assert out.state == "completed"
    assert "PLANTED" not in out.result.stdout


# --- bounds --------------------------------------------------------------------------------------


def test_timeout_kills_the_command_and_its_children(tmp_path):
    child = tmp_path / "child_alive"
    grandchild = f"import time; time.sleep(3); open({str(child)!r}, 'w')"
    script = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}])\n"
        "time.sleep(30)\n"
    )
    started = time.monotonic()
    out = run(py_runner(tmp_path), PY, "-c", script, timeout_s=1)
    assert out.state == "failed"
    assert out.result.reason == "timeout after 1s"
    assert time.monotonic() - started < 5
    time.sleep(3.5)  # the child would have written its file by now if it survived
    assert not child.exists(), "child process survived the timeout"


def test_output_is_capped_and_keeps_the_tail(tmp_path):
    script = (
        "import sys\nfor i in range(200000): sys.stdout.write(f'line {i}\\n')\nprint('LAST LINE')"
    )
    out = run(py_runner(tmp_path, max_output_bytes=4096), PY, "-c", script)
    assert out.state == "completed"
    assert out.result.truncated is True
    assert len(out.result.stdout.encode()) <= 4096
    assert out.result.stdout.endswith("LAST LINE\n")
    assert out.result.stdout.startswith("line ")  # partial first line dropped


def test_partial_secret_at_the_cut_is_not_leaked(tmp_path):
    # The line holding the secret is the one cut in half by the cap: it must be dropped,
    # not returned as "SWORD=hunter2" (which the redaction patterns wouldn't recognise).
    script = "print('x' * 5000 + 'DB_PASSWORD=hunter2'); print('tail')"
    out = run(py_runner(tmp_path, max_output_bytes=1024), PY, "-c", script)
    assert "hunter2" not in out.result.stdout


# --- environment and cwd --------------------------------------------------------------------------


def test_environment_is_minimal(tmp_path):
    # Probe with /usr/bin/env, not Python: on macOS a Python child adds __CF_USER_TEXT_ENCODING to
    # its own environment at startup, which isn't something the runner passed (Sai, PR #6 review).
    # `env` is a plain C program on both macOS and Linux and prints exactly what it was given.
    secrets = {
        "OPENAI_API_KEY": "sk-should-not-leak",
        "SLACK_BOT_TOKEN": "xoxb-no",
        "AWS_SECRET": "x",
    }
    runner = CommandRunner(
        AllowAll(),
        target_dir=tmp_path,
        search_path="/usr/bin:/bin",
        env={"HOME": "/home/x", **secrets},
    )
    out = run(runner, "env")
    assert out.state == "completed", out.result
    keys = {line.split("=", 1)[0] for line in out.result.stdout.splitlines() if "=" in line}
    # The part that matters, checked first so it's always reported: no secret reached the child.
    assert not keys & set(secrets), f"leaked: {keys & set(secrets)}"
    # And nothing beyond what the runner sets or passes through on purpose.
    assert keys <= {"PATH", "LANG", "LC_ALL", "HOME", "DOCKER_HOST", "DOCKER_CONFIG"}, keys
    assert {"PATH", "LANG", "LC_ALL", "HOME"} <= keys


def test_stdin_is_closed(tmp_path):
    out = run(py_runner(tmp_path), PY, "-c", "import sys; print(repr(sys.stdin.read()))")
    assert out.result.stdout.strip() == "''"


def test_cwd_is_the_target_dir(tmp_path):
    out = run(py_runner(tmp_path), PY, "-c", "import os; print(os.getcwd())")
    assert Path(out.result.stdout.strip()).resolve() == tmp_path.resolve()


# --- real validator + real binaries (no Docker needed) ---------------------------------------------


def test_cat_runtime_file_through_real_validator_is_redacted(tmp_path):
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime" / "api.env").write_text(
        "DATABASE_URL=postgresql://app:app-local-only@db-primary:5432/app\nREDIS_URL=redis://cache:6379/0\n"
    )
    out = run(CommandRunner(CommandValidator(), target_dir=tmp_path), "cat", "runtime/api.env")
    assert out.state == "completed", out.result
    assert out.rule_id == "cat"
    assert "app-local-only" not in out.result.stdout
    assert "db-primary" in out.result.stdout  # the clue the agent needs survives


def test_real_validator_rejects_before_any_binary_lookup(tmp_path):
    out = run(
        CommandRunner(CommandValidator(), target_dir=tmp_path), "curl", "http://169.254.169.254/"
    )
    assert out.state == "rejected" and out.rule_id == "curl.url"


# --- redaction -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "must_not_contain", "must_contain"),
    [
        ('"POSTGRES_PASSWORD=app-local-only",', "app-local-only", "POSTGRES_PASSWORD=***"),
        ("postgresql://app:s3cr3t@db:5432/app", "s3cr3t", "postgresql://app:***@db:5432/app"),
        ("redis://default:pw123@cache:6379", "pw123", "@cache:6379"),
        ('{"api_key": "abc123"}', "abc123", '"api_key": "***"'),
        ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.x.y", "eyJhbGci", "Bearer ***"),
        ("key sk-proj-abcdefghijklmnopqrstuvwx", "abcdefghijklmnop", "***"),
        ("xoxb-1234567890-abcdefghij", "1234567890", "***"),
        ("ghp_abcdefghijklmnopqrstuvwxyz0123", "abcdefghijklmnop", "***"),
        ("APPROVAL_SIGNING_KEY=deadbeef", "deadbeef", "APPROVAL_SIGNING_KEY=***"),
    ],
)
def test_redaction(raw, must_not_contain, must_contain):
    out = redact(raw)
    assert must_not_contain not in out
    assert must_contain in out


@pytest.mark.parametrize(
    "safe",
    [
        "REDIS_URL=redis://cache:6379/0",
        "maxmemory 64mb",
        "upstream api_upstream { server api:8000; }",
        '"Image": "ir-target-api:broken"',
        "OperationalError: failed to resolve host 'db-primary'",
        "KEYSPACE_HITS=12",
    ],
)
def test_redaction_keeps_useful_evidence(safe):
    assert redact(safe) == safe
