"""Redact secret-looking values from command output before it reaches the LLM.

WHAT: `redact(text)` replaces
  * the value in `KEY=value` / `"KEY": "value"` when KEY looks secret (PASSWORD, SECRET, TOKEN, ...)
  * the password in URLs with credentials: `scheme://user:password@host` -> `scheme://user:***@host`
  * bearer tokens and a few well-known key formats (OpenAI `sk-...`, Slack `xox?-...`,
    GitHub `gh?_...`, Google `ya29....` access tokens)

WHY: diagnostic output goes into the diagnoser's prompt, its checkpoints, LangSmith traces and the
eval report. `docker inspect` in particular prints every environment variable of a container.
Our target's DB password is a dummy, but the habit matters: an agent should never be handed
credentials it doesn't need, and anything in a prompt can be leaked by a later injection.

WHAT IT KEEPS: hosts, ports, usernames and non-secret keys. The agent needs to see
`postgresql://app:***@db-primary:5432/app` to notice the wrong host in api-bad-db-url-01.

This is a best-effort filter, not a guarantee. The real protection is that the executor's
commands are read-only and the target holds no real secrets.
"""

from __future__ import annotations

import re

MASK = "***"

# Key names that mark a value as secret. Matched case-insensitively anywhere in the key, so
# POSTGRES_PASSWORD, DB_PASS, SLACK_BOT_TOKEN, APPROVAL_SIGNING_KEY and api_key all match.
_SECRET_KEY = r"[A-Za-z0-9_.-]*(?:PASSWORD|PASSWD|PASS|SECRET|TOKEN|API_?KEY|SIGNING_KEY|PRIVATE_KEY|CREDENTIAL)[A-Za-z0-9_.-]*"  # noqa: S105 (a regex of key names, not a password)

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # scheme://user:password@host  (keep user and host)
    (
        re.compile(
            r"(?P<pre>\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s:/@\"']+:)(?P<secret>[^\s@/\"']+)(?P<post>@)"
        ),
        r"\g<pre>***\g<post>",
    ),
    # KEY=value (env files, docker inspect "Env": ["KEY=value"], command lines)
    (re.compile(rf"(?P<pre>\b{_SECRET_KEY}=)(?P<secret>[^\s\"',]+)", re.IGNORECASE), r"\g<pre>***"),
    # "key": "value" (JSON)
    (
        re.compile(
            rf"(?P<pre>\"{_SECRET_KEY}\"\s*:\s*\")(?P<secret>[^\"]*)(?P<post>\")", re.IGNORECASE
        ),
        r"\g<pre>***\g<post>",
    ),
    # Authorization: Bearer xxx
    (re.compile(r"(?P<pre>\b[Bb]earer\s+)(?P<secret>[A-Za-z0-9._~+/=-]{8,})"), r"\g<pre>***"),
    # Well-known token formats, wherever they appear.
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), MASK),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), MASK),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), MASK),
    (re.compile(r"\bya29\.[A-Za-z0-9._-]{20,}"), MASK),
]


def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text
