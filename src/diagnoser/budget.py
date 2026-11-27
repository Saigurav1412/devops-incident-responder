"""Budget caps for one incident (INTERFACES §1, AGENTS.md rule 5).

WHAT: one pure function that says whether an incident has used up any of its budgets.
WHY a separate pure function: the caps are a safety rule, so they must hold in code and be
      provable by a unit test (AGENTS.md rule 1). Keeping the check free of LangGraph means the
      test is a plain function call, and the graph calls this same function on every routing
      decision, so there is exactly one definition of "over budget".

What counts:
  * iterations   - one per verify_hypothesis pass (one "look at evidence, re-score" round)
  * tool_calls   - one per A2A call to the executor (diagnose or remediate)
  * tokens       - summed from every Reasoned.tokens the reasoner reports
  * wall clock   - seconds since Budget.started_at
`replans` is NOT checked here: running out of replans means "escalate to a human", which is a
different outcome from "budget_exceeded" (see graph.py).
"""

from __future__ import annotations

from datetime import datetime

from config import Settings
from contracts import Budget


def budget_exceeded_reason(budget: Budget, settings: Settings, now: datetime) -> str | None:
    """Return a short machine-readable reason if any cap is hit, else None.

    Uses >= so a cap of N allows exactly N units of work: the check runs *after* a unit is
    counted and stops the graph before it can start unit N+1.
    """
    if budget.iterations >= settings.max_iterations:
        return "max_iterations"
    if budget.tool_calls >= settings.max_tool_calls:
        return "max_tool_calls"
    if budget.tokens >= settings.max_tokens_per_incident:
        return "max_tokens"
    if (now - budget.started_at).total_seconds() >= settings.max_wall_clock_s:
        return "max_wall_clock"
    return None
