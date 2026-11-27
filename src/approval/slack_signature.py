"""Verify that an HTTP request really came from Slack (INTERFACES §4.2: "verified ... before
anything is parsed").

WHY this is the first thing the webhook does: the interaction endpoint is public (Slack must be
able to reach it). Without this check, anyone who finds the URL could POST a fake "Approve" click.
The approval token minted afterwards would be genuine, so the executor would run the fix. The
signature is what ties a click to our Slack app.

HOW Slack signs a request:
    basestring = "v0:" + X-Slack-Request-Timestamp + ":" + <raw request body bytes>
    X-Slack-Signature = "v0=" + hex(HMAC-SHA256(signing_secret, basestring))
We recompute that and compare in constant time. We also refuse timestamps more than 5 minutes
from now, so a captured request can't be replayed later.

Adapted from https://api.slack.com/authentication/verifying-requests-from-slack — Slack's
documented v0 signing scheme; implemented with the stdlib instead of slack_sdk to avoid a
dependency for ~20 lines.

IMPORTANT for callers: pass the RAW body bytes exactly as received. Re-serialising a parsed form
or JSON body changes the bytes and breaks the signature (and a framework that parses first has
already broken the "verify before parsing" rule).
"""

from __future__ import annotations

import hashlib
import hmac
import time

# Slack's recommended replay window.
MAX_CLOCK_SKEW_S = 5 * 60

SIGNATURE_HEADER = "x-slack-signature"
TIMESTAMP_HEADER = "x-slack-request-timestamp"


class SlackSignatureError(Exception):
    """The request is not provably from Slack. `reason` is short and safe to log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def compute_signature(signing_secret: str, timestamp: str, body: bytes) -> str:
    """The `v0=<hex>` value Slack would send for this timestamp and body.

    Public so tests (and the eval harness's scripted approver) can build correctly signed requests.
    """
    basestring = b"v0:" + timestamp.encode() + b":" + body
    digest = hmac.new(signing_secret.encode(), basestring, hashlib.sha256).hexdigest()
    return f"v0={digest}"


def verify_slack_request(
    signing_secret: str,
    headers: dict[str, str],
    body: bytes,
    now: float | None = None,
) -> None:
    """Return None if the request is genuinely from Slack; raise SlackSignatureError otherwise.

    `headers` may use any capitalisation (HTTP header names are case-insensitive).
    """
    if not signing_secret:
        # Fail closed: an unset secret must not mean "accept everything".
        raise SlackSignatureError("signing_secret_not_configured")

    lowered = {k.lower(): v for k, v in headers.items()}
    signature = lowered.get(SIGNATURE_HEADER)
    timestamp = lowered.get(TIMESTAMP_HEADER)
    if not signature or not timestamp:
        raise SlackSignatureError("missing_signature_headers")

    try:
        sent_at = int(timestamp)
    except ValueError:
        raise SlackSignatureError("bad_timestamp") from None
    current = now if now is not None else time.time()
    # abs(): a timestamp far in the future is as suspicious as one far in the past.
    if abs(current - sent_at) > MAX_CLOCK_SKEW_S:
        raise SlackSignatureError("stale_timestamp")

    expected = compute_signature(signing_secret, timestamp, body)
    # compare_digest: constant time, so response timing doesn't reveal how much of a forged
    # signature was right.
    if not hmac.compare_digest(expected, signature):
        raise SlackSignatureError("bad_signature")
