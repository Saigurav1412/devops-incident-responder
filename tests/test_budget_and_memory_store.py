"""Unit tests for the two pure safety rules outside the graph: budget caps and memory writes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from config import Settings
from contracts import Budget, IncidentRecord, ProposedAction
from diagnoser.budget import budget_exceeded_reason
from memory.store import InMemoryIncidentMemory, UnverifiedRecordError

T0 = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


def caps() -> Settings:
    return Settings(
        _env_file=None,
        max_iterations=5,
        max_tool_calls=10,
        max_tokens_per_incident=1_000,
        max_wall_clock_s=60,
    )


def test_fresh_budget_is_within_caps():
    assert budget_exceeded_reason(Budget(started_at=T0), caps(), T0) is None


@pytest.mark.parametrize(
    ("budget_fields", "elapsed_s", "reason"),
    [
        ({"iterations": 5}, 0, "max_iterations"),
        ({"tool_calls": 10}, 0, "max_tool_calls"),
        ({"tokens": 1_000}, 0, "max_tokens"),
        ({}, 60, "max_wall_clock"),
    ],
)
def test_each_cap_trips_at_its_limit(budget_fields, elapsed_s, reason):
    budget = Budget(started_at=T0, **budget_fields)
    assert budget_exceeded_reason(budget, caps(), T0 + timedelta(seconds=elapsed_s)) == reason


def test_one_below_every_cap_is_allowed():
    budget = Budget(started_at=T0, iterations=4, tool_calls=9, tokens=999)
    assert budget_exceeded_reason(budget, caps(), T0 + timedelta(seconds=59)) is None


def _record(incident_id: str, symptoms: str, verified: bool = True) -> IncidentRecord:
    return IncidentRecord(
        incident_id=incident_id,
        created_at=T0,
        symptoms=symptoms,
        root_cause="cause",
        fix=ProposedAction(action_id=f"a-{incident_id}", action="restart_service", rationale="r"),
        verified=verified,
    )


def test_memory_refuses_unverified_records():
    memory = InMemoryIncidentMemory()
    with pytest.raises(UnverifiedRecordError):
        memory.save(_record("i1", "api 503", verified=False))
    assert len(memory) == 0


def test_find_similar_ranks_by_overlap_and_limits_k():
    memory = InMemoryIncidentMemory()
    memory.save(_record("redis", "cache redis maxmemory evictions"))
    memory.save(_record("pg", "api 503 postgres connection refused"))
    memory.save(_record("pg2", "api 503 postgres"))

    found = memory.find_similar("api 503 postgres connection refused", k=2)

    assert [r.incident_id for r in found] == ["pg", "pg2"]


def test_find_similar_returns_nothing_for_unrelated_or_empty_queries():
    memory = InMemoryIncidentMemory()
    memory.save(_record("pg", "postgres down"))
    assert memory.find_similar("nginx upstream") == []
    assert memory.find_similar("   ") == []
