"""Diagnoser agent: LangGraph graph, nodes, A2A client. Owner: Sai. See docs/INTERFACES.md §1.

Layout:
  ports.py   the interfaces the graph depends on (reasoner = LLM, executor = A2A)
  budget.py  the budget caps, as one pure function
  nodes.py   one method per graph node + the routers; apply_assessment and the fixed
             health-check argv live here too
  graph.py   wires nodes/edges, the checkpoint serializer, start/resume helpers
  fakes.py   scripted reasoner and executor (incl. health-check answers) for tests and dry runs
  a2a_client.py  the real ExecutorClient: A2A v1.0 SendMessage to the executor, Task -> Observation;
             also serves the executor's policy summaries from its Agent Card
  llm.py     the ChatModel seam (provider-independent): messages in, text + token usage out
  reasoner.py  the LLM-backed Reasoner: fenced prompt rendering, strict answer schemas, repair
             attempt, safe defaults (prompts in src/prompts/)
  service.py the HTTP service: POST/GET /incidents + the Slack webhook (FastAPI)

Not built yet: a ChatModel adapter for a real provider (waits on the LLM decision in DECISIONS.md).

Shared shapes come from `contracts`; never redefine them here.
"""
