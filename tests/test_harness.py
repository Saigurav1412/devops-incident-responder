"""The episode loop and suite runner, against a FAKE target (no Docker): a flag that inject sets,
agents clear, and the oracle reads. Real runs: `python -m evals.harness` (evals/README.md)."""

import asyncio
import json

import pytest

from contracts import EpisodeRecord
from evals.agents import AgentReport, NoopAgent
from evals.faults import load_faults
from evals.harness import Timing, parse_config, run_episode, run_suite

FAULTS = load_faults("all")
FAULT = FAULTS[0]
FAST = Timing(poll_s=0.0, inject_check_s=0.05, settle_s=0.05, timeout_s=1.0)


class FakeOps:
    """broken=True after inject (unless inject_breaks=False); healthy() = not broken."""

    def __init__(self, inject_ok=True, inject_breaks=True, reset_ok=True):
        self.broken = False
        self.inject_ok, self.inject_breaks, self.reset_ok = inject_ok, inject_breaks, reset_ok
        self.log: list[str] = []

    async def reset(self, fault):
        self.log.append("reset")
        self.broken = False
        return self.reset_ok

    async def inject(self, fault):
        self.log.append("inject")
        self.broken = self.inject_breaks
        return self.inject_ok

    async def healthy(self, fault):
        return not self.broken


class FixingAgent:
    name = "fixer"

    def __init__(self, ops, claim="resolved", fix=True, delay=0.0):
        self.ops, self.claim, self.fix, self.delay = ops, claim, fix, delay
        self.alerts = []

    async def handle(self, alert, fault, config):
        self.alerts.append(alert)
        await asyncio.sleep(self.delay)
        if self.fix:
            self.ops.broken = False
        return AgentReport(
            outcome=self.claim,
            tool_calls=4,
            replans=1,
            tokens_in=1000,
            tokens_out=100,
            cost_usd=0.01,
            approval_wait_s=0.5,
        )


def episode(agent, ops, timing=FAST, config=None):
    return asyncio.run(
        run_episode(FAULT, agent, ops, run_id="r1", repeat=1, config=config or {}, timing=timing)
    )


def test_fixing_agent_is_resolved_and_record_is_complete():
    ops = FakeOps()
    agent = FixingAgent(ops)
    res = episode(agent, ops, config={"memory": True})
    r = res.record
    assert res.invalid_reason is None
    assert r.resolved and r.outcome == "resolved" and r.agent_outcome == "resolved"
    assert r.time_to_resolve_s is not None and r.time_to_resolve_s >= 0
    assert (r.tool_calls, r.replans, r.tokens_in, r.cost_usd, r.approval_wait_s) == (
        4,
        1,
        1000,
        0.01,
        0.5,
    )
    assert r.config == {"memory": True}
    assert ops.log == ["reset", "inject", "reset"]  # always reset afterwards
    EpisodeRecord.model_validate_json(r.model_dump_json())  # contract-valid


def test_alert_carries_no_hint_of_the_fault():
    ops = FakeOps()
    agent = FixingAgent(ops)
    episode(agent, ops)
    alert = agent.alerts[0]
    text = alert.model_dump_json()
    assert FAULT.spec.expected_fix not in text and FAULT.spec.category not in text
    assert alert.source == "eval_harness"


def test_noop_agent_is_not_resolved():
    res = episode(NoopAgent(), FakeOps())
    assert (
        not res.record.resolved
        and res.record.outcome == "escalated"
        and res.record.time_to_resolve_s is None
    )


def test_claimed_but_not_fixed_is_a_false_resolution():
    ops = FakeOps()
    res = episode(FixingAgent(ops, claim="resolved", fix=False), ops)
    assert not res.record.resolved
    assert res.record.agent_outcome == "resolved" and res.record.outcome == "escalated"


@pytest.mark.parametrize("claim", ["rejected", "budget_exceeded", "escalated"])
def test_agent_stopping_on_its_own_keeps_its_outcome(claim):
    ops = FakeOps()
    assert episode(FixingAgent(ops, claim=claim, fix=False), ops).record.outcome == claim


def test_agent_timeout():
    ops = FakeOps()
    res = episode(
        FixingAgent(ops, delay=0.5),
        ops,
        timing=Timing(poll_s=0, inject_check_s=0.05, settle_s=0.05, timeout_s=0.1),
    )
    assert (
        res.record.outcome == "timeout"
        and not res.record.resolved
        and res.record.agent_outcome is None
    )
    assert ops.log[-1] == "reset"


def test_agent_crash_is_recorded_and_stack_reset():
    class Crasher:
        name = "crasher"

        async def handle(self, *a):
            raise RuntimeError("boom")

    ops = FakeOps()
    res = episode(Crasher(), ops)
    assert res.record.outcome == "error"
    assert ops.log[-1] == "reset"


@pytest.mark.parametrize(
    ("ops", "reason"),
    [
        (FakeOps(reset_ok=False), "reset_failed"),
        (FakeOps(inject_ok=False), "inject_failed"),
        (FakeOps(inject_breaks=False), "inject_did_not_break_stack"),
    ],
)
def test_invalid_episodes_are_not_records(ops, reason):
    res = episode(NoopAgent(), ops)
    assert res.record is None and res.invalid_reason == reason


def test_suite_writes_jsonl_summary_and_counts_invalids(tmp_path):
    class Ops(FakeOps):
        async def inject(self, fault):  # second fault never breaks -> invalid
            self.log.append("inject")
            self.broken = fault.id != FAULTS[1].id
            return True

    ops = Ops()
    lines: list[str] = []
    summary = asyncio.run(
        run_suite(
            FAULTS[:3],
            FixingAgent(ops),
            ops,
            runs=2,
            out_dir=tmp_path,
            run_id="r1",
            config={"memory": False},
            timing=FAST,
            progress=lines.append,
        )
    )
    episodes = [
        EpisodeRecord.model_validate_json(x)
        for x in (tmp_path / "episodes.jsonl").read_text().splitlines()
    ]
    invalid = [json.loads(x) for x in (tmp_path / "invalid.jsonl").read_text().splitlines()]
    assert len(episodes) == 4 and len(invalid) == 2
    assert {i["reason"] for i in invalid} == {"inject_did_not_break_stack"}
    assert [e.repeat for e in episodes] == [1, 1, 2, 2]  # repeat-major order
    assert summary["meta"]["invalid"] == 2 and summary["overall"]["n"] == 4
    assert (tmp_path / "summary.md").read_text().count("|") > 10
    assert json.loads((tmp_path / "summary.json").read_text())["overall"]["resolved"] == 4
    assert len(lines) == 6 and "INVALID" in lines[1]


def test_parse_config():
    assert parse_config("memory=false,max_hypotheses=1,model=gpt-x") == {
        "memory": False,
        "max_hypotheses": 1,
        "model": "gpt-x",
    }
    assert parse_config("") == {}
    with pytest.raises(ValueError):
        parse_config("memory")


def test_load_faults_selection():
    assert [f.id for f in load_faults("db-stopped-01")] == ["db-stopped-01"]
    assert {f.spec.category for f in load_faults("cat:config")} == {"config"}
    with pytest.raises(ValueError, match="unknown fault ids"):
        load_faults("nope-01")


def test_every_fault_has_expected_params_for_the_scripted_baseline():
    from executor.remediation import load_policy

    enabled = set(load_policy().enabled_actions)
    for f in FAULTS:
        assert f.spec.expected_fix in enabled, f.id
        assert f.spec.expected_params, f.id
