"""Integration: each seeded fault, fixed by the REAL Remediator, leaves the stack healthy.

For every fault: reset to known-good -> inject -> check the oracle fails -> mint an approval token
(as the Slack handler will) -> Remediator.remediate(expected fix) -> wait for the oracle to pass.
This proves the remediation plans actually repair the stack, not just that they build the
argv we expect (that's tests/test_remediation.py). Opt-in, needs Docker:

    ./target/up.sh
    RUN_DOCKER_TESTS=1 uv run pytest -m docker -v tests/test_remediation_docker.py
    ./target/down.sh
"""

import asyncio
import os
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from contracts import RemediateRequest
from contracts.approval_token import mint
from executor import CommandRunner
from executor.remediation import IdempotencyStore, Remediator
from security import CommandValidator

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get("RUN_DOCKER_TESTS") != "1",
        reason="needs the target stack; set RUN_DOCKER_TESTS=1",
    ),
]

REPO = Path(__file__).resolve().parents[1]
FAULTS = REPO / "evals" / "faults"
KEY = bytes.fromhex("ab" * 32)

# fault -> the fix the agent should propose (matches each fault.yaml's expected_fix)
FIXES = [
    ("db-stopped-01", "restart_service", {"service": "db"}),
    ("proxy-bad-upstream-01", "restore_config", {"service": "proxy", "file": "nginx.conf"}),
    ("api-bad-db-url-01", "restore_config", {"service": "api", "file": "api.env"}),
    ("cache-maxmemory-01", "restore_config", {"service": "cache", "file": "redis.conf"}),
    ("api-bad-image-01", "rollback_image", {"service": "api", "tag": "good"}),
]


def sh(path: Path) -> int:
    return subprocess.run([str(path)], capture_output=True, timeout=180, check=False).returncode  # noqa: S603 (our own scripts)


def wait_healthy(fault: str, timeout_s: int = 90) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if sh(FAULTS / fault / "oracle.sh") == 0:
            return True
        time.sleep(3)
    return False


@pytest.mark.parametrize(("fault", "action", "params"), FIXES, ids=[f[0] for f in FIXES])
def test_expected_fix_repairs_the_fault(fault, action, params, tmp_path):
    assert sh(FAULTS / fault / "reset.sh") == 0, "reset failed before the test"
    try:
        assert sh(FAULTS / fault / "inject.sh") == 0, "inject failed"
        time.sleep(5)
        assert sh(FAULTS / fault / "oracle.sh") != 0, "fault did not break the stack"

        action_id = str(uuid.uuid4())
        incident_id = f"it-{fault}"
        token = mint(
            KEY,
            incident_id=incident_id,
            action_id=action_id,
            action=action,
            params=params,
            ttl_s=900,
        )
        rem = Remediator(
            CommandRunner(CommandValidator()), KEY, IdempotencyStore(tmp_path / "state.sqlite3")
        )
        req = RemediateRequest(
            incident_id=incident_id,
            action_id=action_id,
            action=action,
            params=params,
            approval_token=token,
            idempotency_key=action_id,
        )
        out = asyncio.run(rem.remediate(req))
        assert out.state == "completed", out.result
        assert out.result.exit_code == 0, f"{out.result.stdout}\n{out.result.stderr}"
        assert wait_healthy(fault), f"stack not healthy after {action} {params}"

        # Retrying the same approved request must not run it again.
        again = asyncio.run(rem.remediate(req))
        assert again == out
    finally:
        sh(FAULTS / fault / "reset.sh")
