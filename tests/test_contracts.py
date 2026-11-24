"""Contract tests: the shapes in src/contracts match docs/INTERFACES.md and refuse bad input.

These run without an LLM, network or Docker (AGENTS.md rule 9). If one fails after a pull, the
other person changed a contract: read docs/INTERFACES.md before "fixing" the test.
"""

from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter, ValidationError

from contracts import (
    CONTRACT_VERSION,
    Alert,
    DiagnoseRequest,
    EpisodeRecord,
    ExecutorRequest,
    FaultSpec,
    ProposedAction,
    RemediateRequest,
)

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)


def test_contract_version_is_set():
    assert CONTRACT_VERSION


def test_executor_request_dispatches_on_skill():
    # The executor receives a dict and must turn it into exactly the right model.
    adapter = TypeAdapter(ExecutorRequest)
    diag = adapter.validate_python(
        {"skill": "diagnose", "incident_id": "inc-1", "command": ["docker", "ps"]}
    )
    assert isinstance(diag, DiagnoseRequest)
    rem = adapter.validate_python(
        {
            "skill": "remediate",
            "incident_id": "inc-1",
            "action_id": "act-1",
            "action": "restart_service",
            "params": {"service": "api"},
            "approval_token": "1.abc",
            "idempotency_key": "act-1",
        }
    )
    assert isinstance(rem, RemediateRequest)


def test_unknown_skill_is_rejected():
    with pytest.raises(ValidationError):
        TypeAdapter(ExecutorRequest).validate_python({"skill": "shell", "incident_id": "inc-1"})


def test_extra_fields_are_rejected():
    # Smuggling an extra field (e.g. "shell": true) must fail validation, not be ignored.
    with pytest.raises(ValidationError):
        DiagnoseRequest.model_validate(
            {"incident_id": "inc-1", "command": ["docker", "ps"], "shell": True}
        )


def test_command_must_be_argv_list_not_string():
    with pytest.raises(ValidationError):
        DiagnoseRequest.model_validate({"incident_id": "inc-1", "command": "docker ps; rm -rf /"})


def test_command_length_is_bounded():
    with pytest.raises(ValidationError):
        DiagnoseRequest(incident_id="inc-1", command=["x"] * 33)
    with pytest.raises(ValidationError):
        DiagnoseRequest(incident_id="inc-1", command=[])


@pytest.mark.parametrize("bad_id", ["", "a b", "../etc/passwd", "x" * 200, "id;rm"])
def test_identifiers_are_restricted(bad_id):
    with pytest.raises(ValidationError):
        ProposedAction(action_id=bad_id, action="restart_service", rationale="r")


def test_remediation_requires_approval_token():
    with pytest.raises(ValidationError):
        RemediateRequest.model_validate(
            {
                "incident_id": "inc-1",
                "action_id": "a",
                "action": "restart_service",
                "idempotency_key": "a",
            }
        )


def test_timestamps_must_be_timezone_aware():
    with pytest.raises(ValidationError):
        Alert(
            alert_id="a1",
            source="manual",
            service="api",
            summary="down",
            fired_at=datetime(2026, 11, 23, 12, 0),  # naive: no tzinfo
        )
    assert Alert(alert_id="a1", source="manual", service="api", summary="down", fired_at=NOW)


def test_fault_spec_defaults_match_interfaces():
    spec = FaultSpec(
        id="db-oom-01", category="resource", description="d", expected_fix="scale_memory_limit"
    )
    assert (spec.inject, spec.oracle, spec.reset, spec.timeout_s) == (
        "inject.sh",
        "oracle.sh",
        "reset.sh",
        600,
    )


def test_episode_record_round_trips_through_json():
    rec = EpisodeRecord(
        run_id="r1",
        fault_id="db-oom-01",
        repeat=1,
        config={"memory": True, "max_hypotheses": 3},
        resolved=True,
        time_to_resolve_s=142.3,
        replans=1,
        tool_calls=14,
        tokens_in=38210,
        tokens_out=2120,
        cost_usd=0.041,
        blocked_actions=0,
        outcome="resolved",
    )
    assert EpisodeRecord.model_validate_json(rec.model_dump_json()) == rec
