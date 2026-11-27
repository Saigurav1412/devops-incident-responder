"""Run the executor: `uv run python -m executor --host 0.0.0.0 --port 8080`.

Reads config from the environment / .env (config.Settings). Refuses to start without the pieces
security depends on: an approval signing key, the allowed caller, and an audience. On the VM this
runs under systemd (deploy/vm/, later); locally it can run next to the target stack.

For local development without Google ID tokens, set EXECUTOR_DEV_TOKEN to a long random string
and send `Authorization: Bearer <that>`. It only works when bound to 127.0.0.1.
"""

from __future__ import annotations

import argparse
import hmac
import os
from pathlib import Path

import uvicorn

from config import get_settings
from executor.remediation import IdempotencyStore, Remediator
from executor.runner import CommandRunner
from executor.server import AuthError, create_app, google_id_token_verifier
from obs.logging import configure_logging, get_logger
from security import CommandValidator

log = get_logger("executor")


def main() -> None:
    parser = argparse.ArgumentParser(description="incident executor (A2A v1.0)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    configure_logging()
    settings = get_settings()
    key = settings.approval_key_bytes()  # raises if missing/short: refuse to start

    state = Path(settings.executor_state_path)
    state.parent.mkdir(parents=True, exist_ok=True)

    dev_token = os.environ.get("EXECUTOR_DEV_TOKEN", "")
    if dev_token:
        if args.host not in ("127.0.0.1", "localhost") or len(dev_token) < 32:
            raise SystemExit("EXECUTOR_DEV_TOKEN needs --host 127.0.0.1 and at least 32 characters")
        log.warning(
            "DEV auth: accepting EXECUTOR_DEV_TOKEN instead of Google ID tokens (localhost only)"
        )

        async def verify(token: str) -> str:
            if not hmac.compare_digest(token, dev_token):
                raise AuthError(401, "invalid_token")
            return "dev@localhost"
    else:
        verify = google_id_token_verifier(
            settings.executor_audience or settings.executor_url, settings.allowed_diagnoser_sa
        )

    validator = CommandValidator()
    runner = CommandRunner(validator, max_output_bytes=settings.max_output_bytes)
    remediator = Remediator(runner, key, IdempotencyStore(state))
    app = create_app(runner, remediator, verify, settings.executor_url, validator.summary())
    log.info(
        "executor starting",
        extra={"host": args.host, "port": args.port, "url": settings.executor_url},
    )
    uvicorn.run(app, host=args.host, port=args.port, log_config=None)


if __name__ == "__main__":
    main()
