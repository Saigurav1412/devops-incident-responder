"""JSON structured logging, stdlib only.

WHAT: every log line is one JSON object with timestamp, level, logger, message, and any
      context passed via `extra=` (e.g. incident_id, task_id, run_id).
WHY:  Cloud Logging parses JSON lines from stdout into searchable fields, so on GCP we can filter
      "all executor lines for incident X" with no extra agent. Locally, the same lines are easy
      to grep and to attach to the eval report. AGENTS.md: no print().
HOW:  call configure_logging() once at process start, then

    log = get_logger(__name__)
    log.info("command rejected", extra={"incident_id": iid, "rule_id": "docker.subcommand"})

`severity` is the field name Cloud Logging looks for to set the log level.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

# Attributes every LogRecord has; anything else on the record came from `extra=`.
_STANDARD_ATTRS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "severity": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # default=str: never let an odd value (a Path, a datetime) crash logging itself.
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]  # replace, so calling twice doesn't double every line
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
