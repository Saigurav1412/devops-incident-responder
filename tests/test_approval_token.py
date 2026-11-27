"""The approval token (INTERFACES §4.3) is the strongest guardrail in the design, so every
property we rely on has a test: valid tokens pass, and *any* change after approval fails."""

import pytest

from contracts.approval_token import ApprovalTokenError, mint, verify

KEY = b"k" * 32
REQ = {
    "incident_id": "inc-1",
    "action_id": "act-1",
    "action": "restart_service",
    "params": {"service": "api"},
}
T0 = 1_800_000_000.0


def _mint(**overrides):
    return mint(KEY, **{**REQ, **overrides}, ttl_s=900, now=T0)


def test_valid_token_verifies():
    verify(KEY, _mint(), **REQ, now=T0 + 10)


@pytest.mark.parametrize(
    "changed",
    [
        {"action": "prune_path"},  # bait-and-switch: different action, same action_id
        {"params": {"service": "db"}},  # same action, different target
        {"params": {"service": "api", "extra": "1"}},  # added param
        {"params": {}},  # removed param
        {"action_id": "act-2"},
        {"incident_id": "inc-2"},
    ],
)
def test_any_change_after_approval_is_rejected(changed):
    token = _mint()
    with pytest.raises(ApprovalTokenError, match="approval_invalid"):
        verify(KEY, token, **{**REQ, **changed}, now=T0 + 10)


def test_param_order_does_not_matter():
    token = mint(KEY, **{**REQ, "params": {"a": "1", "b": "2"}}, ttl_s=900, now=T0)
    verify(KEY, token, **{**REQ, "params": {"b": "2", "a": "1"}}, now=T0)


def test_expired_token_is_rejected():
    with pytest.raises(ApprovalTokenError, match="approval_expired"):
        verify(KEY, _mint(), **REQ, now=T0 + 901)


def test_extending_expiry_breaks_signature():
    token = _mint()
    expiry, tag = token.split(".")
    forged = f"{int(expiry) + 3600}.{tag}"
    with pytest.raises(ApprovalTokenError, match="approval_invalid"):
        verify(KEY, forged, **REQ, now=T0 + 10)


def test_wrong_key_is_rejected():
    with pytest.raises(ApprovalTokenError, match="approval_invalid"):
        verify(b"x" * 32, _mint(), **REQ, now=T0)


@pytest.mark.parametrize("garbage", ["", "nodot", "abc.def", ".", "1.2.3"])
def test_malformed_tokens_are_rejected(garbage):
    with pytest.raises(ApprovalTokenError):
        verify(KEY, garbage, **REQ, now=T0)


def test_short_key_refused():
    with pytest.raises(ValueError):
        mint(b"short", **REQ, ttl_s=900)
