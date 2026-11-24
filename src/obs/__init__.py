"""Observability: structured logging (and, later, LangSmith setup). Shared by both services."""

from obs.logging import configure_logging, get_logger

__all__ = ["configure_logging", "get_logger"]
