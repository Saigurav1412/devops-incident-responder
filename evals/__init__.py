"""Evaluation harness for the incident responder (docs/INTERFACES.md §5). Owner: Om.

faults.py    load evals/faults/*/fault.yaml; run a fault's inject/oracle/reset scripts
agents.py    the Agent interface + baseline agents (noop, scripted)
metrics.py   summary statistics over episodes.jsonl (pure functions, unit-tested)
harness.py   the episode loop and CLI: `uv run python -m evals.harness --help`
"""
