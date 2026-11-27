"""Guardrails enforced in code. Owner: Om. See docs/INTERFACES.md §2.4.

allowlist.py + allowlist.yaml: the diagnostic command validator (default-deny argv grammar).
Approval-token verification uses contracts.approval_token (shared with the Slack side).
"""

from security.allowlist import CommandValidator, Decision, Policy, load_policy

__all__ = ["CommandValidator", "Decision", "Policy", "load_policy"]
