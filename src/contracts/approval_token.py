"""§4.3 Approval token: mint and verify.

WHAT: an HMAC-SHA256 tag over (incident_id | action_id | action | canonical params | expiry).
      The Slack webhook handler mints it when a human clicks Approve; the executor verifies it
      before running any remediation.

WHY it lives in contracts/ (shared) instead of security/ (Om) or approval/ (Sai):
      both sides must build the *exact same* bytes to sign. If the minting side sorted params one
      way and the verifying side another way, every approval would fail, or worse, someone would
      "fix" it by loosening verification. One function, used by both, removes that risk.

WHY it binds the params, not just the action_id:
      the threat is a prompt-injected diagnoser that gets "restart_service api" approved and then
      sends "prune_path /" with the same action_id. Because the executor recomputes the tag from
      the request it actually received, any change to action or params breaks the tag.

WHAT IT DOES NOT DO: decide who may approve (approval/ checks APPROVER_ALLOWLIST), or prevent
      replays of the *same* approved action (the executor's idempotency_key check does that).

Token format: "<expiry_unix_seconds>.<hex hmac>". The expiry travels in the clear so the verifier
knows what to recompute; changing it changes the HMAC, so it can't be extended by an attacker.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

# Field separator for the signed message. A byte that can't appear in our identifiers
# (contracts.models.Identifier only allows [A-Za-z0-9._:-]), so "a|b" + "c" can't be confused
# with "a" + "b|c".
_SEP = b"\x1f"


class ApprovalTokenError(ValueError):
    """Raised by verify() with a short machine-readable reason (used as RejectionInfo.reason)."""


def _canonical_params(params: dict[str, str]) -> bytes:
    # sort_keys + fixed separators = the same bytes on every machine and Python version.
    return json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _message(
    incident_id: str, action_id: str, action: str, params: dict[str, str], expiry: int
) -> bytes:
    return _SEP.join(
        [
            b"approval-v1",  # domain separation: this key signs nothing else
            incident_id.encode(),
            action_id.encode(),
            action.encode(),
            _canonical_params(params),
            str(expiry).encode(),
        ]
    )


def mint(
    key: bytes,
    *,
    incident_id: str,
    action_id: str,
    action: str,
    params: dict[str, str],
    ttl_s: int,
    now: float | None = None,
) -> str:
    """Create a token. Call ONLY from the Slack approval handler or the eval's scripted approver,
    never from code an LLM can reach (AGENTS.md rule 4)."""
    if len(key) < 32:
        # A short key makes the HMAC guessable; refuse rather than run insecurely.
        raise ValueError("approval signing key must be at least 32 bytes")
    expiry = int((now if now is not None else time.time()) + ttl_s)
    tag = hmac.new(key, _message(incident_id, action_id, action, params, expiry), hashlib.sha256)
    return f"{expiry}.{tag.hexdigest()}"


def verify(
    key: bytes,
    token: str,
    *,
    incident_id: str,
    action_id: str,
    action: str,
    params: dict[str, str],
    now: float | None = None,
) -> None:
    """Return None if the token is valid for exactly this request; raise ApprovalTokenError otherwise."""
    try:
        expiry_str, tag_hex = token.split(".", 1)
        expiry = int(expiry_str)
    except ValueError:
        raise ApprovalTokenError("approval_malformed") from None

    expected = hmac.new(
        key, _message(incident_id, action_id, action, params, expiry), hashlib.sha256
    )
    # compare_digest: constant-time comparison, so timing doesn't leak how many hex chars matched.
    if not hmac.compare_digest(expected.hexdigest(), tag_hex):
        raise ApprovalTokenError("approval_invalid")
    # Check expiry only after the signature, so an attacker can't learn anything from the
    # difference between "expired" and "invalid" on a forged token.
    if (now if now is not None else time.time()) > expiry:
        raise ApprovalTokenError("approval_expired")
