# Evaluation

The harness injects seeded faults into the target stack, hands an agent an alert, and lets the
**oracle** (`target/oracle.sh`: `/health` + a write through the proxy) decide whether the incident
was resolved. Format and fields: `docs/INTERFACES.md` §5.

```bash
./target/up.sh                                                     # once
uv run python -m evals.harness --agent noop --faults all --runs 1 --timeout-s 20 --settle-s 10
uv run python -m evals.harness --agent scripted --faults all --runs 3
uv run python -m evals.harness --agent scripted --faults cat:config --runs 3 --config memory=false
./target/down.sh
```

Each run writes `evals/results/<date>/<run_id>/`:

| File | What |
|---|---|
| `episodes.jsonl` | one `contracts.EpisodeRecord` per valid episode, appended as it finishes |
| `invalid.jsonl` | episodes that didn't count (reset/inject failed, or the fault didn't take) |
| `summary.json` | overall + per-fault numbers (`evals/metrics.py`), each rate with its n |
| `summary.md` | the same as a table, ready for the report |

`evals/results/*/*/` is gitignored; commit a run's folder deliberately (`git add -f`) when it's the
one you report on, so every number in the report points at its raw data.

## Agents

| `--agent` | What it is | Expected |
|---|---|---|
| `noop` | does nothing | **0 resolved.** If not, the oracle or a reset is broken and nothing else is trustworthy |
| `scripted` | applies each fault's `expected_fix`/`expected_params` (which the real agent never sees) through the real `Remediator`, approved by `ScriptedApprover` | **all resolved.** Upper bound; proves approval → gates → steps → oracle works end to end |
| diagnoser | Sai's LangGraph agent | the real numbers; adapter lands after both stacks are merged |

The real agent receives only the `Alert` ("GET /health through the proxy is failing"). It must not
read `fault`; the adapter for the diagnoser will pass only the alert.

## Episode rules (why the numbers are honest)

1. **The fault must be real.** Each `inject.sh` ends with `confirm ...`, which checks its own fault
   (db container stopped, proxy 502, api 503 with a db error, Redis refusing writes with OOM, api on
   the broken image). The harness then also requires the oracle to fail. Otherwise the episode is
   **invalid** and excluded, never counted as "resolved in 0 s".
2. **The oracle decides**, not the agent. The agent's own conclusion is kept in `agent_outcome`;
   `agent_outcome=resolved` with `resolved=false` is a **false resolution** and is reported.
3. **Always reset**, even after a crash or Ctrl-C.
4. **Approval wait** is recorded (`approval_wait_s`) and TTR is reported with and without it.
5. Repeats are interleaved (all faults once, then again), so machine drift doesn't land on one fault.
