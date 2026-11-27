"""Executor agent: A2A v1.0 server, action catalog, command runner. Owner: Om. See docs/INTERFACES.md §2.

Layout:
  runner.py          run one validated diagnostic command (and run_argv for code-built steps)
  redact.py          mask secret-looking values in command output before it reaches the LLM
  remediation.py     perform one approved catalog action: 5 gates, then code-built steps
  remediation.yaml   which actions are enabled here and what each may touch

Not built yet: the A2A server that exposes diagnose() and remediate().

Shared shapes come from `contracts`; never redefine them here.
"""

from executor.remediation import IdempotencyStore, Remediator
from executor.runner import CommandRunner, RunOutcome

__all__ = ["CommandRunner", "IdempotencyStore", "Remediator", "RunOutcome"]
