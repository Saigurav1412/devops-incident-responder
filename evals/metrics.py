"""Summary statistics over episode records. Pure functions, no I/O, so they're easy to test and to
re-run on an old episodes.jsonl (AGENTS.md rule 10: numbers come from real runs only; this file
only aggregates them, it never fills gaps)."""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Iterable

from contracts import EpisodeRecord


def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile (no interpolation): with few samples it reports a value that was
    actually observed, which is easier to defend in a report than an interpolated one."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


def _group(records: Iterable[EpisodeRecord], key) -> dict[str, list[EpisodeRecord]]:
    groups: dict[str, list[EpisodeRecord]] = defaultdict(list)
    for r in records:
        groups[key(r)].append(r)
    return dict(groups)


def summarize(records: list[EpisodeRecord]) -> dict:
    """Overall + per-fault numbers. Every rate is reported with its n."""

    def block(rs: list[EpisodeRecord]) -> dict:
        n = len(rs)
        resolved = [r for r in rs if r.resolved]
        ttr = [r.time_to_resolve_s for r in resolved if r.time_to_resolve_s is not None]
        ttr_wo_wait = [
            r.time_to_resolve_s - r.approval_wait_s
            for r in resolved
            if r.time_to_resolve_s is not None
        ]
        return {
            "n": n,
            "resolved": len(resolved),
            "resolution_rate": round(len(resolved) / n, 4) if n else None,
            "ttr_p50_s": percentile(ttr, 50),
            "ttr_p95_s": percentile(ttr, 95),
            "ttr_p50_excl_approval_s": percentile(ttr_wo_wait, 50),
            "mean_tool_calls": round(statistics.fmean(r.tool_calls for r in rs), 2) if n else None,
            "mean_replans": round(statistics.fmean(r.replans for r in rs), 2) if n else None,
            "mean_tokens": round(statistics.fmean(r.tokens_in + r.tokens_out for r in rs), 1)
            if n
            else None,
            "total_cost_usd": round(sum(r.cost_usd for r in rs), 4),
            "blocked_actions": sum(r.blocked_actions for r in rs),
            # The agent said "resolved" but the oracle disagrees.
            "false_resolutions": sum(
                1 for r in rs if r.agent_outcome == "resolved" and not r.resolved
            ),
            "outcomes": dict(sorted(_count(r.outcome for r in rs).items())),
        }

    return {
        "overall": block(records),
        "by_fault": {k: block(v) for k, v in sorted(_group(records, lambda r: r.fault_id).items())},
    }


def _count(items: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for i in items:
        out[i] += 1
    return dict(out)


def render_markdown(summary: dict, *, title: str, agent: str, config: dict, invalid: int) -> str:
    def fmt(v, suffix=""):
        return "—" if v is None else f"{v}{suffix}"

    o = summary["overall"]
    lines = [
        f"# {title}",
        "",
        f"Agent: `{agent}` · config: `{config}` · episodes: {o['n']} · invalid injects (excluded): {invalid}",
        "",
        f"**Resolved {o['resolved']}/{o['n']}** ({fmt(o['resolution_rate'] and round(o['resolution_rate'] * 100, 1), '%')}) · "
        f"TTR p50 {fmt(o['ttr_p50_s'], ' s')} · p95 {fmt(o['ttr_p95_s'], ' s')} · "
        f"blocked actions {o['blocked_actions']} · false resolutions {o['false_resolutions']} · "
        f"cost ${o['total_cost_usd']}",
        "",
        "| Fault | Resolved | TTR p50 (s) | TTR p95 (s) | Mean tool calls | Mean replans | Blocked | False res. | Outcomes |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for fid, b in summary["by_fault"].items():
        lines.append(
            f"| {fid} | {b['resolved']}/{b['n']} | {fmt(b['ttr_p50_s'])} | {fmt(b['ttr_p95_s'])} | "
            f"{fmt(b['mean_tool_calls'])} | {fmt(b['mean_replans'])} | {b['blocked_actions']} | "
            f"{b['false_resolutions']} | {', '.join(f'{k} {v}' for k, v in b['outcomes'].items())} |"
        )
    lines += ["", "Resolution is the oracle's verdict (target/oracle.sh), not the agent's claim."]
    return "\n".join(lines) + "\n"
