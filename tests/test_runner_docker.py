"""Integration: every command the allowlist permits runs successfully against the REAL target stack.

This closes the gap test_allowlist.py can't: it proves the real docker / redis-cli / pg_isready /
nginx / curl binaries accept exactly the argv shapes we allow (e.g. that `docker logs -n` and
`redis-cli MEMORY STATS` are real). Skipped unless RUN_DOCKER_TESTS=1, because it needs Docker
and a healthy stack:

    ./target/up.sh
    RUN_DOCKER_TESTS=1 uv run pytest -m docker -v
    ./target/down.sh
"""

import asyncio
import os

import pytest

from contracts import DiagnoseRequest
from executor import CommandRunner
from security import CommandValidator

from test_allowlist import ALLOWED  # same list: what we allow is exactly what we test for real

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get("RUN_DOCKER_TESTS") != "1",
        reason="needs the target stack; set RUN_DOCKER_TESTS=1",
    ),
]

RUNNER = CommandRunner(CommandValidator())


@pytest.mark.parametrize(
    ("argv", "rule_id"), ALLOWED, ids=lambda x: " ".join(x) if isinstance(x, list) else x
)
def test_allowed_command_runs_against_the_live_stack(argv, rule_id):
    out = asyncio.run(
        RUNNER.diagnose(DiagnoseRequest(incident_id="it-1", command=argv, timeout_s=30))
    )
    assert out.state == "completed", out.result
    assert out.rule_id == rule_id
    assert out.result.exit_code == 0, (
        f"exit {out.result.exit_code}\nstdout: {out.result.stdout[-500:]}\nstderr: {out.result.stderr[-500:]}"
    )
    # Output is redacted: the dummy DB password never comes back, even from `docker inspect`.
    assert "app-local-only" not in out.result.stdout + out.result.stderr
