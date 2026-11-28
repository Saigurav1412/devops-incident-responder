"""Run the diagnoser: `uv run python -m diagnoser [--host 127.0.0.1] [--port 8000]`.

Reads config from the environment / .env (config.Settings) and EXECUTOR_DEV_TOKEN from the
environment (see diagnoser/wiring.py). If anything is missing it prints every problem and exits
with status 2, before binding a port. On Cloud Run, PORT is set by the platform and used as the
default port.
"""

from __future__ import annotations

import argparse
import os
import sys

import uvicorn

from config import get_settings
from diagnoser.wiring import StartupError, create_app_from_settings
from obs.logging import configure_logging, get_logger

log = get_logger("diagnoser")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m diagnoser", description="incident diagnoser")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = parser.parse_args(argv)

    configure_logging()
    try:
        app = create_app_from_settings(get_settings())
    except StartupError as exc:
        # Plain text on stderr: this is for the person starting the service.
        print(exc, file=sys.stderr)
        return 2
    log.info("diagnoser starting", extra={"host": args.host, "port": args.port})
    uvicorn.run(app, host=args.host, port=args.port, log_config=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
