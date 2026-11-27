"""Shared contracts between the diagnoser (Sai) and the executor/evals (Om).

WHAT: every data shape that crosses a slice boundary lives here, as Pydantic models, generated
      by hand from docs/INTERFACES.md.
WHY:  if each side defined its own copy of, say, RemediateRequest, the two copies would drift and
      the bug would only appear at runtime over A2A. One shared definition makes a mismatch a
      test failure instead.
HOW:  import from here, never redefine locally:

    from contracts import RemediateRequest, CONTRACT_VERSION

Changing anything in this package: update docs/INTERFACES.md in the same PR, bump
CONTRACT_VERSION, and ask the other person to review (AGENTS.md rule 11).
"""

from contracts.models import (
    Alert,
    ApprovalDecision,
    Budget,
    CommandResult,
    DiagnoseRequest,
    EpisodeRecord,
    ExecutorRequest,
    FailureInfo,
    FaultSpec,
    Hypothesis,
    IncidentRecord,
    IncidentState,
    Observation,
    ProposedAction,
    RejectionInfo,
    RemediateRequest,
)

# Bump on every change to a model in this package (semver-ish: MAJOR.MINOR).
# 0.x while the contracts are still [proposed] in docs/INTERFACES.md.
# 0.2: REMEDIATION_ACTIONS names in contracts.catalog; 0.3: eval fields (PR #13);
# 0.4: Budget.tokens_in / tokens_out
CONTRACT_VERSION = "0.4"

__all__ = [
    "CONTRACT_VERSION",
    "Alert",
    "ApprovalDecision",
    "Budget",
    "CommandResult",
    "DiagnoseRequest",
    "EpisodeRecord",
    "ExecutorRequest",
    "FailureInfo",
    "FaultSpec",
    "Hypothesis",
    "IncidentRecord",
    "IncidentState",
    "Observation",
    "ProposedAction",
    "RejectionInfo",
    "RemediateRequest",
]
