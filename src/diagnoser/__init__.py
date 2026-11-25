"""Diagnoser agent: LangGraph graph, nodes, A2A client. Owner: Sai. See docs/INTERFACES.md §1.

Layout:
  ports.py   the interfaces the graph depends on (reasoner = LLM, executor = A2A, health check)
  budget.py  the budget caps, as one pure function
  nodes.py   one method per graph node + the routers for the conditional edges
  graph.py   wires nodes/edges, the checkpoint serializer, start/resume helpers
  fakes.py   scripted reasoner/executor/health check for tests and local dry runs

Not built yet: the OpenAI-backed reasoner (waits on the LLM decision) and the real A2A client.

Shared shapes come from `contracts`; never redefine them here.
"""
