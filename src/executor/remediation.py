"""Remediation: perform ONE approved catalog action on the target (INTERFACES §2.2, §2.4, §4.3).

WHAT: `Remediator.remediate(RemediateRequest) -> RunOutcome`, same outcome shapes as diagnostics:
      completed (the action's steps ran; exit_code says whether the last one succeeded),
      rejected (refused before anything changed), failed (tried and broke, or interrupted earlier).

WHY each gate exists, in the order they run:
  1. idempotency_key must equal action_id   (INTERFACES §2.2: one key per approved proposal)
  2. action known and ENABLED here           remediation.yaml decides; set_env/prune_path are off
  3. approval token valid for EXACTLY this   contracts.approval_token.verify recomputes the HMAC from
     incident/action/params                  the request as received, so any change after the human
                                             clicked Approve (bait-and-switch) fails here
  4. params valid for this action            a valid approval does NOT bypass policy: a human can
                                             approve something this executor still refuses
  5. not already run                         sqlite-backed store; a retry gets the stored outcome back
                                             instead of running the fix twice; an attempt that was
                                             interrupted mid-way is NOT re-run automatically

Only after all five does anything change. The steps themselves are built by THIS code from the
validated params (e.g. ["docker", "compose", ..., "restart", "db"]); the LLM never supplies a
command here. They run through `CommandRunner.run_argv`, i.e. the same fixed-PATH, no-shell,
timeout-bounded, output-capped, redacted path as diagnostics.

One remediation at a time (a global asyncio lock): two concurrent fixes to the same small stack
would make each other's results meaningless, and serialising also closes the race where two
identical retries both pass the "not already run" check.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from contracts import CommandResult, FailureInfo, RejectionInfo, RemediateRequest
from contracts.approval_token import ApprovalTokenError, verify
from contracts.catalog import REMEDIATION_ACTIONS
from executor.runner import DEFAULT_TARGET_DIR, CommandRunner, RunOutcome
from obs.logging import get_logger

log = get_logger(__name__)

DEFAULT_POLICY_PATH = Path(__file__).with_name("remediation.yaml")

# ---------------------------------------------------------------------------------------------
# Policy (remediation.yaml)
# ---------------------------------------------------------------------------------------------


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ConfigFileRule(_Model):
    file: str = Field(pattern=r"^[A-Za-z0-9._-]+$")  # a bare file name, never a path
    apply: Literal["reload", "recreate", "restart"]


class MemoryRule(_Model):
    var: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    min_mb: int = Field(ge=16)
    max_mb: int = Field(le=8192)


class ImageRule(_Model):
    var: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    allowed: tuple[str, ...]


class Timeouts(_Model):
    step: int = Field(ge=5, le=600)


class RemediationPolicy(_Model):
    version: Literal[1]
    compose_project: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    services: tuple[str, ...]
    enabled_actions: tuple[str, ...]
    config_files: dict[str, ConfigFileRule]
    memory_limits: dict[str, MemoryRule]
    image_tags: dict[str, ImageRule]
    timeouts_s: Timeouts


def load_policy(path: Path = DEFAULT_POLICY_PATH) -> RemediationPolicy:
    policy = RemediationPolicy.model_validate(yaml.safe_load(path.read_text()))
    unknown = set(policy.enabled_actions) - REMEDIATION_ACTIONS
    if unknown:
        raise ValueError(f"enabled_actions not in contracts.catalog: {sorted(unknown)}")
    for section in (policy.config_files, policy.memory_limits, policy.image_tags):
        stray = set(section) - set(policy.services)
        if stray:
            raise ValueError(f"policy names services not in `services`: {sorted(stray)}")
    return policy


# ---------------------------------------------------------------------------------------------
# Per-action parameter models. All params arrive as strings (contracts: dict[str, str]).
# ---------------------------------------------------------------------------------------------


class RestartParams(_Model):
    service: str


class RestoreConfigParams(_Model):
    service: str
    file: str


class ScaleMemoryParams(_Model):
    service: str
    mb: str = Field(pattern=r"^[0-9]{1,5}$")


class RollbackImageParams(_Model):
    service: str
    tag: str


class _ParamError(Exception):
    pass


# ---------------------------------------------------------------------------------------------
# Plan steps: what an action does, as data, so tests can check the plan without running it.
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CopyFile:
    src: Path
    dst: Path

    def describe(self) -> str:
        return (
            f"copy {self.src.parent.name}/{self.src.name} -> {self.dst.parent.name}/{self.dst.name}"
        )


@dataclass(frozen=True)
class SetEnvFileVar:
    path: Path
    key: str
    value: str

    def describe(self) -> str:
        return f"set {self.key}={self.value} in {self.path.parent.name}/{self.path.name}"


@dataclass(frozen=True)
class RunArgv:
    argv: tuple[str, ...]

    def describe(self) -> str:
        return "$ " + " ".join(self.argv)


Step = CopyFile | SetEnvFileVar | RunArgv


# ---------------------------------------------------------------------------------------------
# Idempotency store
# ---------------------------------------------------------------------------------------------


class IdempotencyStore:
    """Remembers every remediation by idempotency_key, in sqlite so it survives an executor restart.

    status "started" is written BEFORE any step runs and "done" (with the outcome) after. If the
    executor crashes in between, the key stays "started": the next attempt is refused rather than
    re-run, because we can't know how far the first attempt got. A human (or a new proposal with
    a new action_id) decides what to do next.
    """

    def __init__(self, path: Path | str = ":memory:") -> None:
        self.db = sqlite3.connect(str(path), isolation_level=None)  # autocommit; one row per write
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS runs (key TEXT PRIMARY KEY, request_hash TEXT NOT NULL,"
            " status TEXT NOT NULL, outcome TEXT, started_at REAL NOT NULL, finished_at REAL)"
        )

    def get(self, key: str) -> tuple[str, str, str | None] | None:
        row = self.db.execute(
            "SELECT request_hash, status, outcome FROM runs WHERE key = ?", (key,)
        ).fetchone()
        return tuple(row) if row else None  # type: ignore[return-value]

    def begin(self, key: str, request_hash: str) -> None:
        self.db.execute(
            "INSERT INTO runs (key, request_hash, status, started_at) VALUES (?, ?, 'started', ?)",
            (key, request_hash, time.time()),
        )

    def finish(self, key: str, outcome: RunOutcome) -> None:
        self.db.execute(
            "UPDATE runs SET status = 'done', outcome = ?, finished_at = ? WHERE key = ?",
            (_dump_outcome(outcome), time.time(), key),
        )


def _dump_outcome(o: RunOutcome) -> str:
    return json.dumps(
        {
            "state": o.state,
            "rule_id": o.rule_id,
            "type": type(o.result).__name__,
            "result": o.result.model_dump(),
        }
    )


def _load_outcome(raw: str) -> RunOutcome:
    d = json.loads(raw)
    model = {
        "CommandResult": CommandResult,
        "RejectionInfo": RejectionInfo,
        "FailureInfo": FailureInfo,
    }[d["type"]]
    return RunOutcome(d["state"], model.model_validate(d["result"]), d["rule_id"])


def _request_hash(req: RemediateRequest) -> str:
    canonical = json.dumps(
        {"incident_id": req.incident_id, "action": req.action, "params": req.params}, sort_keys=True
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


# ---------------------------------------------------------------------------------------------
# The remediator
# ---------------------------------------------------------------------------------------------


def _reject(rule_id: str | None, reason: str) -> RunOutcome:
    return RunOutcome("rejected", RejectionInfo(reason=reason, rule_id=rule_id), rule_id)


class Remediator:
    def __init__(
        self,
        runner: CommandRunner,
        approval_key: bytes,
        store: IdempotencyStore,
        *,
        policy: RemediationPolicy | None = None,
        target_dir: Path = DEFAULT_TARGET_DIR,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.runner = runner
        self.key = approval_key
        self.store = store
        self.policy = policy or load_policy()
        self.target_dir = target_dir
        self.clock = clock  # None -> real time inside verify(); tests pass a fixed clock
        self._lock = asyncio.Lock()

    # -- entry point ---------------------------------------------------------------------------

    async def remediate(self, req: RemediateRequest) -> RunOutcome:
        ctx = {"incident_id": req.incident_id, "action": req.action, "action_id": req.action_id}

        # Gate 1: one key per approved proposal.
        if req.idempotency_key != req.action_id:
            return self._blocked(
                ctx, "remediate.idempotency_key", "idempotency_key must equal action_id"
            )

        # Gate 2: known and enabled on this executor.
        if req.action not in REMEDIATION_ACTIONS:
            return self._blocked(ctx, "remediate.unknown_action", f"unknown action {req.action}")
        if req.action not in self.policy.enabled_actions:
            return self._blocked(
                ctx,
                "remediate.action_disabled",
                f"{req.action} is not enabled on this executor; enabled: {', '.join(self.policy.enabled_actions)}",
            )

        # Gate 3: the human approved exactly this.
        try:
            verify(
                self.key,
                req.approval_token,
                incident_id=req.incident_id,
                action_id=req.action_id,
                action=req.action,
                params=req.params,
                now=self.clock() if self.clock else None,
            )
        except ApprovalTokenError as exc:
            # rule_id None for approval failures (INTERFACES §2.3); the reason says which.
            return self._blocked(ctx, None, str(exc))

        # Gate 4: an approval doesn't bypass policy.
        try:
            steps = self._plan(req.action, req.params)
        except (_ParamError, ValidationError) as exc:
            reason = exc.errors()[0]["msg"] if isinstance(exc, ValidationError) else str(exc)
            return self._blocked(
                ctx, "remediate.params", f"invalid params for {req.action}: {reason}"
            )

        # Gate 5 + run, serialised.
        async with self._lock:
            req_hash = _request_hash(req)
            existing = self.store.get(req.idempotency_key)
            if existing is not None:
                stored_hash, status, raw = existing
                if stored_hash != req_hash:
                    return self._blocked(
                        ctx,
                        "remediate.idempotency_conflict",
                        "this action_id was used for a different request",
                    )
                if status == "done" and raw is not None:
                    log.info("remediation already done; returning stored outcome", extra=ctx)
                    return _load_outcome(raw)
                log.error("previous attempt interrupted; not re-running", extra=ctx)
                return RunOutcome(
                    "failed",
                    FailureInfo(
                        reason="previous attempt was interrupted; not re-run automatically (propose a new fix)"
                    ),
                    "remediate.interrupted",
                )

            self.store.begin(req.idempotency_key, req_hash)
            outcome = await self._execute(steps, ctx)
            self.store.finish(req.idempotency_key, outcome)
            log.info("remediation finished", extra={**ctx, "state": outcome.state})
            return outcome

    def _blocked(self, ctx: dict[str, str], rule_id: str | None, reason: str) -> RunOutcome:
        log.warning("remediation rejected", extra={**ctx, "rule_id": rule_id, "reason": reason})
        return _reject(rule_id, reason)

    # -- planning: params -> steps -------------------------------------------------------------

    def _compose(self, *args: str) -> RunArgv:
        p = self.policy
        return RunArgv(
            (
                "docker",
                "compose",
                "-p",
                p.compose_project,
                "-f",
                "docker-compose.yml",
                "--env-file",
                "runtime/stack.env",
                *args,
            )
        )

    def _service(self, name: str, allowed: dict | tuple) -> str:
        if name not in self.policy.services or name not in allowed:
            raise _ParamError(f"service must be one of {', '.join(sorted(allowed))}")
        return name

    def _plan(self, action: str, params: dict[str, str]) -> list[Step]:
        runtime, known_good = self.target_dir / "runtime", self.target_dir / "known-good"
        stack_env = runtime / "stack.env"

        if action == "restart_service":
            p = RestartParams.model_validate(params)
            svc = self._service(p.service, self.policy.services)
            return [self._compose("restart", svc)]

        if action == "restore_config":
            p = RestoreConfigParams.model_validate(params)
            svc = self._service(p.service, self.policy.config_files)
            rule = self.policy.config_files[svc]
            if p.file != rule.file:
                raise _ParamError(f"the config file for {svc} is {rule.file}")
            steps: list[Step] = [CopyFile(known_good / rule.file, runtime / rule.file)]
            if rule.apply == "reload":
                steps.append(self._compose("exec", "-T", svc, "nginx", "-s", "reload"))
            elif rule.apply == "recreate":
                steps.append(self._compose("up", "-d", "--no-deps", "--force-recreate", svc))
            else:
                steps.append(self._compose("restart", svc))
            return steps

        if action == "scale_memory_limit":
            p = ScaleMemoryParams.model_validate(params)
            svc = self._service(p.service, self.policy.memory_limits)
            rule = self.policy.memory_limits[svc]
            mb = int(p.mb)
            if not rule.min_mb <= mb <= rule.max_mb:
                raise _ParamError(f"mb for {svc} must be {rule.min_mb}..{rule.max_mb}")
            return [
                SetEnvFileVar(stack_env, rule.var, f"{mb}m"),
                self._compose("up", "-d", "--no-deps", svc),
            ]

        if action == "rollback_image":
            p = RollbackImageParams.model_validate(params)
            svc = self._service(p.service, self.policy.image_tags)
            rule = self.policy.image_tags[svc]
            if p.tag not in rule.allowed:
                raise _ParamError(f"tag for {svc} must be one of {', '.join(rule.allowed)}")
            return [
                SetEnvFileVar(stack_env, rule.var, p.tag),
                self._compose("up", "-d", "--no-deps", svc),
            ]

        # Enabled in policy but no implementation: refuse rather than guess.
        raise _ParamError(f"{action} has no implementation on this executor")

    # -- execution -----------------------------------------------------------------------------

    async def _execute(self, steps: list[Step], ctx: dict[str, str]) -> RunOutcome:
        """Run steps in order, stop at the first that fails, report all of them in one result."""
        lines_out: list[str] = []
        lines_err: list[str] = []
        exit_code, truncated, duration_ms = 0, False, 0
        for step in steps:
            lines_out.append(step.describe())
            if isinstance(step, RunArgv):
                o = await self.runner.run_argv(
                    list(step.argv),
                    self.policy.timeouts_s.step,
                    ctx={**ctx, "argv": list(step.argv)},
                )
                if o.state != "completed" or not isinstance(o.result, CommandResult):
                    return RunOutcome(
                        "failed",
                        FailureInfo(
                            reason=f"step failed: {step.describe()}: {getattr(o.result, 'reason', '')}"
                        ),
                        "remediate.step",
                    )
                r = o.result
                if r.stdout:
                    lines_out.append(r.stdout.rstrip("\n"))
                if r.stderr:
                    lines_err.append(r.stderr.rstrip("\n"))
                exit_code, truncated, duration_ms = (
                    r.exit_code,
                    truncated or r.truncated,
                    duration_ms + r.duration_ms,
                )
                if exit_code != 0:
                    break  # later steps assume this one worked
            else:
                try:
                    self._apply_file_step(step)
                except OSError as exc:
                    return RunOutcome(
                        "failed",
                        FailureInfo(reason=f"step failed: {step.describe()}: {type(exc).__name__}"),
                        "remediate.step",
                    )
        return RunOutcome(
            "completed",
            CommandResult(
                exit_code=exit_code,
                stdout="\n".join(lines_out) + "\n",
                stderr="\n".join(lines_err) + ("\n" if lines_err else ""),
                truncated=truncated,
                duration_ms=duration_ms,
            ),
            "remediate.done",
        )

    @staticmethod
    def _apply_file_step(step: CopyFile | SetEnvFileVar) -> None:
        # Both write INTO the existing file (same inode), so bind-mounted files update inside the
        # running container. shutil.copyfile and write_text both truncate-and-write the target.
        if isinstance(step, CopyFile):
            shutil.copyfile(step.src, step.dst)
            return
        lines = step.path.read_text().splitlines()
        prefix = f"{step.key}="
        replaced = False
        for i, line in enumerate(lines):
            if line.startswith(prefix):
                lines[i] = prefix + step.value
                replaced = True
        if not replaced:
            lines.append(prefix + step.value)
        step.path.write_text("\n".join(lines) + "\n")
