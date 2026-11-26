"""Executor agent: A2A v1.0 server, action catalog, command runner. Owner: Om. See docs/INTERFACES.md §2.

Layout:
  runner.py  run one validated diagnostic command -> CommandResult / RejectionInfo / FailureInfo
  redact.py  mask secret-looking values in command output before it reaches the LLM

Not built yet: remediation actions (catalog + approval-token check + idempotency) and the A2A server.

Shared shapes come from `contracts`; never redefine them here.
"""

from executor.runner import CommandRunner, RunOutcome

__all__ = ["CommandRunner", "RunOutcome"]
