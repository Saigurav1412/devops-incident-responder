"""Run one validated diagnostic command and turn the result into a contract model.

WHAT: `CommandRunner.diagnose(request)` -> `RunOutcome(state, result)`, where state/result are
exactly what INTERFACES §2.3 puts on the A2A task:

    completed  -> CommandResult   the command ran (ANY exit code: a failing curl is evidence)
    rejected   -> RejectionInfo   the validator refused it; never executed
    failed     -> FailureInfo     we tried and couldn't (binary missing, timeout, OS error)

HOW, step by step (each step is a guardrail; the order matters):
  1. `CommandValidator.check(argv)`. Refused -> REJECTED, nothing runs.
  2. Resolve the binary on a FIXED search path (`SAFE_PATH`), never the inherited $PATH, so an
     attacker who managed to drop an executable called `docker` somewhere on $PATH can't get it run.
  3. Start it with `create_subprocess_exec` (argv, no shell, AGENTS.md rule 3), stdin closed,
     a minimal environment, cwd = the target directory, in its own process group.
  4. Read stdout/stderr through rolling buffers that keep only the last `max_output_bytes` of each:
     memory stays bounded even if a command spews gigabytes before the timeout.
  5. Kill the whole process group on timeout (docker CLI can spawn children).
  6. Redact secret-looking values, then return.

WHY async: the A2A server (next PR) is an async web app; a blocking subprocess call would stall
every other request while one `docker logs` runs.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from contracts import CommandResult, DiagnoseRequest, FailureInfo, RejectionInfo
from executor.redact import redact
from obs.logging import get_logger
from security.allowlist import Decision

log = get_logger(__name__)

# Where allowed binaries may live. Deliberately short and system-owned.
SAFE_PATH = "/usr/local/bin:/usr/bin:/bin"

# Environment variables the executor passes through to commands, if set on the executor itself.
# Set by whoever deploys the executor, never by the agent: e.g. DOCKER_HOST to point at a
# read-only socket proxy (INTERFACES §2.4 sandbox), HOME so the docker CLI finds its config.
PASSTHROUGH_ENV = ("HOME", "DOCKER_HOST", "DOCKER_CONFIG")

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TARGET_DIR = REPO_ROOT / "target"


class Validator(Protocol):
    def check(self, argv: list[str] | tuple[str, ...]) -> Decision: ...


@dataclass(frozen=True)
class RunOutcome:
    state: Literal["completed", "rejected", "failed"]
    result: CommandResult | RejectionInfo | FailureInfo
    rule_id: str | None = None  # the validator's rule id, for logs and the eval


class _TailBuffer:
    """Keeps the last `limit` bytes written to it and remembers whether anything was dropped."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.data = bytearray()
        self.dropped = False

    def write(self, chunk: bytes) -> None:
        self.data += chunk
        if len(self.data) > self.limit:
            del self.data[: len(self.data) - self.limit]
            self.dropped = True

    def text(self) -> str:
        text = self.data.decode("utf-8", errors="replace")
        if self.dropped:
            # The buffer now starts mid-line. Drop that partial first line: a secret cut in half
            # (e.g. "SWORD=hunter2") would slip past the redaction patterns.
            _, _, rest = text.partition("\n")
            text = rest
        return text


async def _pump(stream: asyncio.StreamReader, buf: _TailBuffer) -> None:
    while chunk := await stream.read(65536):
        buf.write(chunk)


class CommandRunner:
    def __init__(
        self,
        validator: Validator,
        *,
        target_dir: Path = DEFAULT_TARGET_DIR,
        max_output_bytes: int = 16_384,
        env: Mapping[str, str] | None = None,
        search_path: str = SAFE_PATH,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.validator = validator
        self.target_dir = target_dir
        self.max_output_bytes = max_output_bytes
        self.search_path = search_path
        self.clock = clock
        source = os.environ if env is None else env
        self.env = {
            "PATH": search_path,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            **{k: source[k] for k in PASSTHROUGH_ENV if k in source},
        }

    async def diagnose(self, request: DiagnoseRequest) -> RunOutcome:
        argv = list(request.command)
        ctx = {"incident_id": request.incident_id, "argv": argv}

        # 1. Policy. A refusal is a normal, expected outcome: logged as a blocked action.
        decision = self.validator.check(argv)
        if not decision.allowed:
            log.warning("command rejected", extra={**ctx, "rule_id": decision.rule_id})
            return RunOutcome(
                "rejected",
                RejectionInfo(reason=decision.reason, rule_id=decision.rule_id),
                decision.rule_id,
            )

        # 2. Binary from the fixed path only.
        binary = shutil.which(argv[0], path=self.search_path)
        if binary is None:
            log.error("binary not found", extra=ctx)
            return RunOutcome(
                "failed", FailureInfo(reason=f"binary_not_found: {argv[0]}"), decision.rule_id
            )

        # 3–5. Run.
        started = self.clock()
        try:
            proc = await asyncio.create_subprocess_exec(
                binary,
                *argv[1:],
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.target_dir,
                env=self.env,
                start_new_session=True,  # own process group, so a timeout kills children too
            )
        except OSError as exc:
            log.error("command failed to start", extra={**ctx, "error": str(exc)})
            return RunOutcome(
                "failed",
                FailureInfo(reason=f"start_failed: {type(exc).__name__}"),
                decision.rule_id,
            )

        out, err = _TailBuffer(self.max_output_bytes), _TailBuffer(self.max_output_bytes)
        if proc.stdout is None or proc.stderr is None:  # can't happen with PIPE; keeps mypy honest
            raise RuntimeError("subprocess pipes missing")
        try:
            await asyncio.wait_for(
                asyncio.gather(_pump(proc.stdout, out), _pump(proc.stderr, err), proc.wait()),
                timeout=request.timeout_s,
            )
        except TimeoutError:
            self._kill(proc)
            await proc.wait()
            log.warning("command timed out", extra={**ctx, "timeout_s": request.timeout_s})
            return RunOutcome(
                "failed",
                FailureInfo(reason=f"timeout after {request.timeout_s}s"),
                decision.rule_id,
            )

        duration_ms = int((self.clock() - started) * 1000)
        result = CommandResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=redact(out.text()),
            stderr=redact(err.text()),
            truncated=out.dropped or err.dropped,
            duration_ms=duration_ms,
        )
        log.info(
            "command completed",
            extra={
                **ctx,
                "exit_code": result.exit_code,
                "duration_ms": duration_ms,
                "truncated": result.truncated,
            },
        )
        return RunOutcome("completed", result, decision.rule_id)

    @staticmethod
    def _kill(proc: asyncio.subprocess.Process) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # already exited between the timeout and the kill
