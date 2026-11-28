"""The eval harness: run seeded faults against an agent and record what happened (INTERFACES §5).

One episode, in order:
  1. reset           target back to known-good (fault's reset.sh). Fails -> episode INVALID.
  2. inject          break it (inject.sh). Fails -> INVALID.
  3. confirm broken  the oracle must FAIL within --inject-check-s. If the stack still looks
                     healthy, the fault didn't take: INVALID, not "resolved in 0 s". (This is the
                     generic guard; each inject.sh also checks its own fault, see evals/README.md.)
  4. agent           gets an Alert, nothing else, with --timeout-s (default: the fault's timeout_s).
  5. judge           the ORACLE decides resolution, never the agent: poll it until healthy, for up
                     to --settle-s after the agent returns (bounded by the timeout).
  6. record          one EpisodeRecord line in episodes.jsonl, written immediately.
  7. reset           always, even if the agent crashed or Ctrl-C.

Invalid episodes go to invalid.jsonl and are excluded from every metric, with their count shown
in the summary (so a broken fault can't silently inflate or deflate the numbers).

Outcome per episode (EpisodeRecord.outcome):
  resolved                         oracle healthy in time (whatever the agent said)
  escalated/rejected/budget_exceeded  agent stopped on its own without a healthy stack
  timeout                          agent didn't finish within the timeout
  error                            agent raised
A run where the agent claims "resolved" but the oracle disagrees is outcome escalated with
agent_outcome resolved: counted as a FALSE RESOLUTION.

    uv run python -m evals.harness --agent noop --faults all --runs 1 --timeout-s 30
    uv run python -m evals.harness --agent scripted --faults all --runs 3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import secrets
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from contracts import Alert, EpisodeRecord
from evals.agents import Agent, AgentReport, NoopAgent, ScriptedAgent, ScriptedApprover
from evals.faults import REPO, Fault, FaultOps, ScriptFaultOps, load_faults
from evals.metrics import render_markdown, summarize
from obs.logging import configure_logging, get_logger

log = get_logger("evals.harness")

DEFAULT_RESULTS_DIR = REPO / "evals" / "results"


@dataclass(frozen=True)
class EpisodeResult:
    record: EpisodeRecord | None
    invalid_reason: str | None = None


@dataclass(frozen=True)
class Timing:
    """All waits in one place so tests can shrink them."""

    poll_s: float = 3.0
    inject_check_s: float = 20.0
    settle_s: float = 60.0
    timeout_s: float | None = None  # None = fault.spec.timeout_s


async def run_episode(
    fault: Fault,
    agent: Agent,
    ops: FaultOps,
    *,
    run_id: str,
    repeat: int,
    config: dict[str, bool | int | str],
    timing: Timing = Timing(),
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> EpisodeResult:
    ctx = {"run_id": run_id, "fault_id": fault.id, "repeat": repeat}
    timeout = timing.timeout_s or fault.spec.timeout_s
    try:
        # 1–3: set up and prove the fault is real.
        if not await ops.reset(fault):
            return EpisodeResult(None, "reset_failed")
        if not await ops.inject(fault):
            return EpisodeResult(None, "inject_failed")
        deadline = clock() + timing.inject_check_s
        while await ops.healthy(fault):
            if clock() >= deadline:
                return EpisodeResult(None, "inject_did_not_break_stack")
            await sleep(timing.poll_s)

        # 4: the agent sees only an alert, like a real page.
        alert = Alert(
            alert_id=f"{run_id}-{fault.id}-r{repeat}",
            source="eval_harness",
            service="proxy",
            summary="GET /health through the proxy is failing",
            fired_at=datetime.now(UTC),
        )
        started = clock()
        report: AgentReport | None = None
        failure: str | None = None
        try:
            report = await asyncio.wait_for(agent.handle(alert, fault, config), timeout)
        except TimeoutError:
            failure = "timeout"
        except Exception:  # noqa: BLE001 — an agent crash is a result, not a harness crash
            log.exception("agent raised", extra=ctx)
            failure = "error"

        # 5: the oracle decides.
        resolved, ttr = False, None
        stop = min(started + timeout, clock() + timing.settle_s)
        while True:
            if await ops.healthy(fault):
                resolved, ttr = True, round(clock() - started, 2)
                break
            if clock() >= stop or failure is not None:
                break
            await sleep(timing.poll_s)

        if resolved:
            outcome = "resolved"
        elif failure is not None:
            outcome = failure
        elif report is not None and report.outcome in ("rejected", "budget_exceeded", "escalated"):
            outcome = report.outcome
        else:
            outcome = (
                "escalated"  # agent finished (claimed resolved, or gave up) but stack unhealthy
            )

        r = report or AgentReport(outcome="open")
        record = EpisodeRecord(
            run_id=run_id,
            fault_id=fault.id,
            repeat=repeat,
            config=config,
            resolved=resolved,
            time_to_resolve_s=ttr,
            replans=r.replans,
            tool_calls=r.tool_calls,
            tokens_in=r.tokens_in,
            tokens_out=r.tokens_out,
            cost_usd=r.cost_usd,
            blocked_actions=r.blocked_actions,
            outcome=outcome,
            langsmith_run_url=r.langsmith_run_url,
            approval_wait_s=round(r.approval_wait_s, 3),
            agent_outcome=report.outcome if report is not None else None,
        )
        log.info(
            "episode finished",
            extra={**ctx, "outcome": outcome, "resolved": resolved, "ttr_s": ttr},
        )
        return EpisodeResult(record)
    finally:
        # 7: always leave the stack healthy for the next episode.
        if not await ops.reset(fault):
            log.error("reset after episode failed", extra=ctx)


async def run_suite(
    faults: list[Fault],
    agent: Agent,
    ops: FaultOps,
    *,
    runs: int,
    out_dir: Path,
    run_id: str,
    config: dict[str, bool | int | str],
    timing: Timing = Timing(),
    progress: Callable[[str], None] = lambda s: print(s, file=sys.stderr, flush=True),
) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    episodes_path, invalid_path = out_dir / "episodes.jsonl", out_dir / "invalid.jsonl"
    records: list[EpisodeRecord] = []
    invalid = 0
    total = runs * len(faults)
    n = 0
    # Repeat-major order (all faults once, then all again) spreads repeats over time, so a slow
    # drift in the machine doesn't land on one fault's three runs.
    for repeat in range(1, runs + 1):
        for fault in faults:
            n += 1
            result = await run_episode(
                fault, agent, ops, run_id=run_id, repeat=repeat, config=config, timing=timing
            )
            if result.record is None:
                invalid += 1
                with invalid_path.open("a") as f:
                    f.write(
                        json.dumps(
                            {
                                "run_id": run_id,
                                "fault_id": fault.id,
                                "repeat": repeat,
                                "reason": result.invalid_reason,
                            }
                        )
                        + "\n"
                    )
                progress(f"[{n}/{total}] {fault.id} r{repeat}: INVALID ({result.invalid_reason})")
                continue
            records.append(result.record)
            # Append and flush per episode: a crash or Ctrl-C keeps every finished episode.
            with episodes_path.open("a") as f:
                f.write(result.record.model_dump_json() + "\n")
            rec = result.record
            progress(
                f"[{n}/{total}] {fault.id} r{repeat}: {rec.outcome}"
                + (f" in {rec.time_to_resolve_s} s" if rec.resolved else "")
            )

    summary = summarize(records)
    meta = {
        "run_id": run_id,
        "agent": agent.name,
        "config": config,
        "runs": runs,
        "faults": [f.id for f in faults],
        "invalid": invalid,
    }
    (out_dir / "summary.json").write_text(json.dumps({"meta": meta, **summary}, indent=2) + "\n")
    (out_dir / "summary.md").write_text(
        render_markdown(
            summary, title=f"Eval run {run_id}", agent=agent.name, config=config, invalid=invalid
        )
    )
    return {"meta": meta, **summary}


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------


def parse_config(text: str) -> dict[str, bool | int | str]:
    """`memory=false,max_hypotheses=1` -> {"memory": False, "max_hypotheses": 1}."""
    out: dict[str, bool | int | str] = {}
    for pair in filter(None, (p.strip() for p in text.split(","))):
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise ValueError(f"config entries look like key=value, got {pair!r}")
        if value.lower() in ("true", "false"):
            out[key] = value.lower() == "true"
        elif value.lstrip("-").isdigit():
            out[key] = int(value)
        else:
            out[key] = value
    return out


def build_agent(name: str) -> Agent:
    if name == "noop":
        return NoopAgent()
    if name == "scripted":
        from config import get_settings
        from executor import CommandRunner, IdempotencyStore, Remediator
        from security import CommandValidator

        settings = get_settings()
        try:
            key = settings.approval_key_bytes()
        except ValueError:
            key = secrets.token_bytes(32)  # harness-only key; the approver and remediator share it
        runner = CommandRunner(CommandValidator(), max_output_bytes=settings.max_output_bytes)
        return ScriptedAgent(
            runner, Remediator(runner, key, IdempotencyStore()), ScriptedApprover(key)
        )
    raise ValueError(
        f"unknown agent {name!r}; available: noop, scripted (diagnoser: after the stacks merge)"
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m evals.harness", description=__doc__.split("\n")[0])
    p.add_argument("--agent", default="scripted", help="noop | scripted")
    p.add_argument("--faults", default="all", help='"all", ids "a,b", or "cat:config"')
    p.add_argument("--runs", type=int, default=1)
    p.add_argument(
        "--config",
        default="",
        help="ablation switches recorded with each episode, e.g. memory=false",
    )
    p.add_argument("--timeout-s", type=float, default=None, help="override every fault's timeout_s")
    p.add_argument(
        "--settle-s",
        type=float,
        default=60.0,
        help="how long to wait for the oracle after the agent returns",
    )
    p.add_argument("--out", type=Path, default=DEFAULT_RESULTS_DIR)
    args = p.parse_args(argv)

    configure_logging("WARNING")  # progress lines go to stderr; JSON logs only for problems
    logging.getLogger("evals.harness").setLevel(logging.INFO)
    faults = load_faults(args.faults)
    agent = build_agent(args.agent)
    config = parse_config(args.config)
    now = datetime.now(UTC)
    run_id = f"{now:%Y%m%d-%H%M%S}-{agent.name}"
    out_dir = args.out / f"{now:%Y-%m-%d}" / run_id
    timing = Timing(timeout_s=args.timeout_s, settle_s=args.settle_s)

    result = asyncio.run(
        run_suite(
            faults,
            agent,
            ScriptFaultOps(),
            runs=args.runs,
            out_dir=out_dir,
            run_id=run_id,
            config=config,
            timing=timing,
        )
    )
    o = result["overall"]
    print(
        f"\n{o['resolved']}/{o['n']} resolved · invalid {result['meta']['invalid']} · results in {out_dir.relative_to(REPO) if out_dir.is_relative_to(REPO) else out_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
