import json
import logging

import pytest
from pydantic import ValidationError

from config import Settings
from obs.logging import JsonFormatter


def test_settings_load_without_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no .env here
    s = Settings()
    assert s.max_tool_calls == 30
    assert s.memory_enabled is True


def test_budget_caps_cannot_be_disabled(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MAX_TOOL_CALLS", "0")
    with pytest.raises(ValidationError):
        Settings()


def test_secrets_are_masked(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APPROVAL_SIGNING_KEY", "super-secret-value")
    assert "super-secret-value" not in repr(Settings())


def test_approver_ids_split(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APPROVER_ALLOWLIST", "U1, U2,,U3 ")
    assert Settings().approver_ids == {"U1", "U2", "U3"}


def test_log_lines_are_json_with_extra_fields():
    record = logging.makeLogRecord(
        {"name": "t", "levelname": "INFO", "msg": "hello", "incident_id": "inc-1"}
    )
    line = json.loads(JsonFormatter().format(record))
    assert line["message"] == "hello"
    assert line["severity"] == "INFO"
    assert line["incident_id"] == "inc-1"
