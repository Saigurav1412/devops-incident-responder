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


# --- approval key decoding (INTERFACES §4.3; Sai's review on PR #2) ---------------------------

GOOD_HEX = "ab" * 32  # 64 hex chars -> 32 bytes


def _settings_with_key(monkeypatch, tmp_path, value):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APPROVAL_SIGNING_KEY", value)
    return Settings()


def test_approval_key_decodes_hex_to_32_bytes(monkeypatch, tmp_path):
    key = _settings_with_key(monkeypatch, tmp_path, GOOD_HEX).approval_key_bytes()
    assert key == bytes.fromhex(GOOD_HEX)
    assert len(key) == 32
    # The bug this prevents: .encode() would give 64 *different* bytes.
    assert key != GOOD_HEX.encode()


def test_approval_key_ignores_surrounding_whitespace(monkeypatch, tmp_path):
    s = _settings_with_key(monkeypatch, tmp_path, f"  {GOOD_HEX}\n")
    assert s.approval_key_bytes() == bytes.fromhex(GOOD_HEX)


def test_both_sides_agree_on_a_token(monkeypatch, tmp_path):
    # Slack handler (mint) and executor (verify) each build Settings from the same env var.
    from contracts.approval_token import mint, verify

    minter = _settings_with_key(monkeypatch, tmp_path, GOOD_HEX)
    verifier = _settings_with_key(monkeypatch, tmp_path, GOOD_HEX)
    req = {"incident_id": "i1", "action_id": "a1", "action": "restart_service", "params": {}}
    token = mint(minter.approval_key_bytes(), **req, ttl_s=900)
    verify(verifier.approval_key_bytes(), token, **req)


@pytest.mark.parametrize(
    ("value", "match"),
    [
        ("", "not set"),
        ("   ", "not set"),
        ("zz" * 32, "must be hex"),
        ("ab" * 31, "31 bytes"),  # 62 hex chars: one byte short
        ("change-me-to-64-hex-chars", "must be hex"),  # the old placeholder
    ],
)
def test_bad_approval_keys_are_refused(monkeypatch, tmp_path, value, match):
    with pytest.raises(ValueError, match=match):
        _settings_with_key(monkeypatch, tmp_path, value).approval_key_bytes()


def test_unset_approval_key_is_refused(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("APPROVAL_SIGNING_KEY", raising=False)
    with pytest.raises(ValueError, match="not set"):
        Settings().approval_key_bytes()


def test_key_errors_never_contain_the_value(monkeypatch, tmp_path):
    secret_ish = "zz" * 40
    with pytest.raises(ValueError) as exc:
        _settings_with_key(monkeypatch, tmp_path, secret_ish).approval_key_bytes()
    assert secret_ish not in str(exc.value)


def test_chatty_third_party_loggers_are_quiet_but_keep_warnings(capsys):
    import logging

    from obs.logging import configure_logging

    configure_logging("INFO")
    try:
        logging.getLogger("a2a.client.card_resolver").info("Successfully fetched agent card data")
        logging.getLogger("httpx").info("HTTP Request: GET http://x")
        logging.getLogger("httpx").warning("httpx warning still shown")
        logging.getLogger("diagnoser.a2a_client").info("a2a call done")
        out = capsys.readouterr().out
    finally:
        logging.getLogger().handlers.clear()
    assert "agent card" not in out and "HTTP Request" not in out
    assert "httpx warning still shown" in out and "a2a call done" in out
