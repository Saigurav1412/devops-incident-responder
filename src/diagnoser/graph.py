"""Build the diagnoser's LangGraph graph (INTERFACES §1).

The shape, with the loops that make this an agent rather than a pipeline:

    START -> ingest_alert -> recall_memory -> plan_hypotheses -> diagnose -> verify_hypothesis
                                                  ^    ^            ^              |
                                                  |    |            +-- still open-+
                                                  |    +----------- all refuted ---+   (replan)
                                                  |                                |
                                                  |                           confirmed
                                                  |                                v
                     check_health <- execute_fix <- request_approval <- propose_fix
                          |   (fail: replan)           (interrupt)
                          +--------------^            reject -> END
                          | pass
                          v
                     save_memory -> END

  Any routing step can divert to `budget_exceeded -> END`; running out of replans goes to
  `escalate -> END`. Both are bounded by caps in config, enforced by the routers in nodes.py.

Memory across turns: LangGraph saves a checkpoint after every node, keyed by `thread_id`. We use
the incident_id as the thread_id (INTERFACES §1), so each incident is its own isolated thread,
and an incident paused at request_approval can be resumed later, even by a new process that
builds a new graph on the same checkpointer (that's how it will survive a Cloud Run redeploy
once the Firestore checkpointer replaces InMemorySaver).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from contracts import (
    Alert,
    ApprovalDecision,
    Budget,
    CommandResult,
    DiagnoseRequest,
    FailureInfo,
    Hypothesis,
    IncidentRecord,
    IncidentState,
    Observation,
    ProposedAction,
    RejectionInfo,
    RemediateRequest,
)
from diagnoser import nodes as n
from diagnoser.ports import DiagnoserDeps

# Every Pydantic class that can appear inside an IncidentState checkpoint.
_CHECKPOINTED_MODELS = (
    Alert,
    ApprovalDecision,
    Budget,
    CommandResult,
    DiagnoseRequest,
    FailureInfo,
    Hypothesis,
    IncidentRecord,
    Observation,
    ProposedAction,
    RejectionInfo,
    RemediateRequest,
)


def contract_serializer() -> JsonPlusSerializer:
    """Checkpoint serializer that may only rebuild our own contract classes.

    WHY an allowlist: when LangGraph loads a checkpoint it re-creates Python objects by
    module + class name. Without a list, a tampered checkpoint (e.g. in Firestore) could name
    any importable class. LangGraph 1.2 already warns about unregistered types and says it will
    block them in a future version; listing ours explicitly is both safer and future-proof.
    Reuse this for the Firestore checkpointer so both stores accept the same types.
    """
    allowed = [(model.__module__, model.__name__) for model in _CHECKPOINTED_MODELS]
    return JsonPlusSerializer(allowed_msgpack_modules=allowed)


def build_graph(
    deps: DiagnoserDeps, checkpointer: BaseCheckpointSaver | None = None
) -> CompiledStateGraph:
    """Wire the nodes and edges and compile the graph.

    `checkpointer` defaults to an in-process InMemorySaver (tests, local runs). A checkpointer is
    required, not optional: `interrupt()` in request_approval only works if the paused state can
    be saved and loaded again.
    """
    nodes = n.DiagnoserNodes(deps)
    graph = StateGraph(IncidentState)

    # --- nodes -------------------------------------------------------------------------------
    graph.add_node(n.NODE_INGEST, nodes.ingest_alert)
    graph.add_node(n.NODE_RECALL, nodes.recall_memory)
    graph.add_node(n.NODE_PLAN, nodes.plan_hypotheses)
    graph.add_node(n.NODE_DIAGNOSE, nodes.diagnose)
    graph.add_node(n.NODE_VERIFY, nodes.verify_hypothesis)
    graph.add_node(n.NODE_PROPOSE, nodes.propose_fix)
    graph.add_node(n.NODE_APPROVAL, nodes.request_approval)
    graph.add_node(n.NODE_EXECUTE, nodes.execute_fix)
    graph.add_node(n.NODE_HEALTH, nodes.check_health)
    graph.add_node(n.NODE_SAVE, nodes.save_memory)
    graph.add_node(n.NODE_ESCALATE, nodes.escalate)
    graph.add_node(n.NODE_BUDGET, nodes.budget_exceeded)

    # --- fixed edges: steps that always lead to the same next step -------------------------------
    graph.add_edge(START, n.NODE_INGEST)
    graph.add_edge(n.NODE_INGEST, n.NODE_RECALL)
    graph.add_edge(n.NODE_RECALL, n.NODE_PLAN)
    graph.add_edge(n.NODE_PROPOSE, n.NODE_APPROVAL)
    graph.add_edge(n.NODE_SAVE, END)
    graph.add_edge(n.NODE_ESCALATE, END)
    graph.add_edge(n.NODE_BUDGET, END)

    # --- conditional edges: where the agent decides (and where the loops are) ------------------
    # The dict maps each value a router can return to a node, which also lets LangGraph draw
    # the graph correctly (graph.get_graph().draw_mermaid()).
    graph.add_conditional_edges(
        n.NODE_PLAN,
        nodes.route_after_plan,
        {
            n.NODE_DIAGNOSE: n.NODE_DIAGNOSE,
            n.NODE_ESCALATE: n.NODE_ESCALATE,
            n.NODE_BUDGET: n.NODE_BUDGET,
        },
    )
    graph.add_conditional_edges(
        n.NODE_DIAGNOSE,
        nodes.route_after_diagnose,
        {n.NODE_VERIFY: n.NODE_VERIFY, n.NODE_BUDGET: n.NODE_BUDGET},
    )
    graph.add_conditional_edges(
        n.NODE_VERIFY,
        nodes.route_after_verify,
        {
            n.NODE_PROPOSE: n.NODE_PROPOSE,  # confirmed
            n.NODE_PLAN: n.NODE_PLAN,  # all refuted -> self-correct
            n.NODE_DIAGNOSE: n.NODE_DIAGNOSE,  # still open -> more evidence
            n.NODE_ESCALATE: n.NODE_ESCALATE,  # out of replans
            n.NODE_BUDGET: n.NODE_BUDGET,
        },
    )
    graph.add_conditional_edges(
        n.NODE_APPROVAL,
        nodes.route_after_approval,
        {n.NODE_EXECUTE: n.NODE_EXECUTE, n.ROUTE_REJECTED: END},
    )
    graph.add_conditional_edges(
        n.NODE_EXECUTE,
        nodes.route_after_execute,
        {
            n.NODE_HEALTH: n.NODE_HEALTH,  # the executor applied the fix: did it work?
            n.NODE_ESCALATE: n.NODE_ESCALATE,  # refused / failed / non-zero exit / unreachable
        },
    )
    graph.add_conditional_edges(
        n.NODE_HEALTH,
        nodes.route_after_health,
        {
            n.NODE_SAVE: n.NODE_SAVE,  # healthy
            n.NODE_PLAN: n.NODE_PLAN,  # fix didn't work -> self-correct
            n.NODE_ESCALATE: n.NODE_ESCALATE,
            n.NODE_BUDGET: n.NODE_BUDGET,
        },
    )

    return graph.compile(checkpointer=checkpointer or InMemorySaver(serde=contract_serializer()))


# ---------------------------------------------------------------------------------------------
# Helpers for callers (the HTTP layer, the Slack handler, the eval harness, tests)
# ---------------------------------------------------------------------------------------------


def new_incident_state(
    alert: Alert, now: datetime, incident_id: str | None = None
) -> IncidentState:
    """The initial state for a new incident. The budget clock starts now."""
    return IncidentState(
        incident_id=incident_id or str(uuid.uuid4()),
        alert=alert,
        recalled=[],
        hypotheses=[],
        observations=[],
        proposed=None,
        approval=None,
        outcome="open",
        budget=Budget(started_at=now),
    )


# Hard ceiling on graph steps per invocation: the backstop BEHIND the budget caps.
# WHY: LangGraph 1.2's default recursion_limit is 10,007 steps, effectively no limit. If a bug
# ever bypassed the budget routers, the graph would spin through thousands of LLM calls. The
# largest legitimate run (config allows <= 100 iterations x 2 steps + <= 20 replans, each with a
# plan/propose/approve/execute/health chain) stays well under 500, so hitting this means a bug,
# and LangGraph raises GraphRecursionError instead of looping.
GRAPH_STEP_LIMIT = 500


def thread_config(incident_id: str) -> dict[str, Any]:
    """LangGraph run config: incident_id IS the thread_id, so each incident's checkpoints are
    separate and a resume always lands on the right incident."""
    return {"configurable": {"thread_id": incident_id}, "recursion_limit": GRAPH_STEP_LIMIT}


def resume_with(decision: ApprovalDecision) -> Command:
    """The value the Slack handler passes to `graph.ainvoke(...)` to continue a paused incident."""
    return Command(resume=decision)
