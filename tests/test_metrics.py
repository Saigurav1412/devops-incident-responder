from contracts import EpisodeRecord
from evals.metrics import percentile, render_markdown, summarize


def rec(fault="f1", resolved=True, ttr=10.0, outcome="resolved", agent_outcome="resolved", **kw):
    base = dict(
        run_id="r",
        fault_id=fault,
        repeat=1,
        config={},
        resolved=resolved,
        time_to_resolve_s=ttr if resolved else None,
        replans=0,
        tool_calls=3,
        tokens_in=100,
        tokens_out=10,
        cost_usd=0.01,
        blocked_actions=0,
        outcome=outcome,
        agent_outcome=agent_outcome,
        approval_wait_s=2.0,
    )
    base.update(kw)
    return EpisodeRecord(**base)


def test_percentile_nearest_rank():
    assert percentile([], 50) is None
    assert percentile([5.0], 95) == 5.0
    assert percentile([1, 2, 3, 4], 50) == 2
    assert percentile([1, 2, 3, 4], 95) == 4
    assert percentile(list(range(1, 101)), 95) == 95


def test_summary_rates_and_false_resolutions():
    rs = [
        rec(ttr=10),
        rec(ttr=30),
        rec(resolved=False, outcome="escalated", agent_outcome="resolved"),  # false resolution
        rec(fault="f2", resolved=False, outcome="timeout", agent_outcome=None, blocked_actions=2),
    ]
    s = summarize(rs)
    o = s["overall"]
    assert (o["n"], o["resolved"], o["resolution_rate"]) == (4, 2, 0.5)
    assert o["ttr_p50_s"] == 10 and o["ttr_p95_s"] == 30
    assert o["ttr_p50_excl_approval_s"] == 8.0
    assert o["false_resolutions"] == 1 and o["blocked_actions"] == 2
    assert o["outcomes"] == {"escalated": 1, "resolved": 2, "timeout": 1}
    assert s["by_fault"]["f1"]["resolution_rate"] == round(2 / 3, 4)
    assert s["by_fault"]["f2"]["ttr_p50_s"] is None


def test_empty_summary_has_no_made_up_numbers():
    o = summarize([])["overall"]
    assert (
        o["n"] == 0
        and o["resolution_rate"] is None
        and o["ttr_p50_s"] is None
        and o["mean_tool_calls"] is None
    )


def test_markdown_lists_every_fault_and_invalid_count():
    md = render_markdown(
        summarize([rec(), rec(fault="f2", resolved=False, outcome="escalated")]),
        title="t",
        agent="a",
        config={},
        invalid=3,
    )
    assert "| f1 |" in md and "| f2 |" in md and "invalid injects (excluded): 3" in md
    assert "Resolved 1/2" in md


def test_gave_up_but_healed_is_counted_separately():
    # Agent escalated after its own health check failed too early; the oracle saw the stack
    # come back. Still "resolved" (oracle decides), but reported so the rate isn't overstated.
    rs = [
        rec(resolved=True, outcome="resolved", agent_outcome="escalated"),
        rec(resolved=True, outcome="resolved", agent_outcome="budget_exceeded"),
        rec(resolved=True, outcome="resolved", agent_outcome="resolved"),
        rec(resolved=False, outcome="escalated", agent_outcome="escalated"),
    ]
    o = summarize(rs)["overall"]
    assert o["resolved"] == 3 and o["gave_up_but_healed"] == 2
    md = render_markdown(summarize(rs), title="t", agent="a", config={}, invalid=0)
    assert "gave up but healed 2" in md and "Gave up, healed" in md
