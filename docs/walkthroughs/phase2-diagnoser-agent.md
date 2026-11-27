# Walkthrough: the harness drives the real diagnoser (`--agent diagnoser-scripted`)

Branch `feat/harness-diagnoser` (stacked on `feat/eval-harness`, PR #13). Files: `evals/diagnoser_agent.py`,
`evals/harness.py` (`build_agent`), `tests/test_diagnoser_agent.py`.

## 1. What it's for

Until now the harness had two baselines that never touched Sai's code. This adds a third agent that
runs **the whole diagnoser** (the incident API, LangGraph graph, Slack approval handler and A2A client)
against **your executor server**, on the real stack. Only the reasoner is scripted: it reads the fault's
answer, like `scripted`. So:

| Agent | Diagnosis | Approval | Fix path | Tells you |
|---|---|---|---|---|
| `scripted` | none (cheats) | `ScriptedApprover` mints directly | `Remediator` called in-process | executor + oracle work |
| `diagnoser-scripted` | graph, reasoner cheats | signed Slack click → Sai's handler mints | graph → A2A → executor server → `Remediator` | **the whole pipeline works**; the upper bound for the LLM agent |
| `diagnoser` (later) | graph + LLM | same | same | the real numbers |

If `diagnoser-scripted` doesn't resolve a fault that `scripted` does, the bug is in the pipeline
(approval, A2A, health-check timing), not in the LLM. That's why it's worth having before the LLM lands.

## 2. How one episode flows

1. The harness hands `DiagnoserAgent.handle()` the Alert (and the fault, which only the scripted
   reasoner reads).
2. It builds Sai's app with `create_service(deps, poster)` **in this process** and talks to it through
   `httpx.ASGITransport`, i.e. real HTTP requests with no port:
   - `POST /incidents` with a bearer token (random per run, see `harness_settings`)
   - `GET /incidents/{id}` polled until `outcome != "open"`.
3. When the status says `waiting_for_approval`, the app has "posted" the Slack message to
   `CapturingSlackPoster`. The harness finds the Approve button's value (`incident_id` + `action_id`),
   builds Slack's form body, **signs it with the signing secret**, and POSTs `/slack/interactions`.
   Sai's handler then runs every check it runs for a human (signature, approver allowlist, current
   proposal, TTL) and mints the token from the checkpoint. The harness never mints.
4. The graph sends the fix over A2A (`A2AExecutorClient`, dev token) to your executor, which checks the
   token and runs the steps. Then `check_health` curls once.
5. The report goes back to the harness: outcome (the agent's **claim**), replans, tool calls,
   tokens, and blocked actions (rejected observations, read from the checkpoint because the status
   API deliberately doesn't expose observations). The **oracle** still decides "resolved".

Ablations (`--config`): `memory=false` gives the episode an empty memory; `max_hypotheses=N` overrides the
setting for that episode (validated, 1–10). Memory is otherwise shared across episodes, so repeats can
benefit from earlier fixes.

## 3. Things that went wrong while building it (worth knowing)

- **`memory or InMemoryIncidentMemory()` was a bug.** The store defines `__len__`, so an empty one is
  falsy and `or` silently replaced it with a new store. The memory ablation would have compared
  "no memory" with "no memory". The test that saves across 3 episodes caught it; the code now uses
  `is None`.
- **An ignored click hung forever.** If the handler refuses the harness's click (approver not on the
  allowlist, past the TTL), the incident stays paused and the loop polled until the episode timeout.
  That would have been recorded as `timeout`. Now it raises `ApprovalClickIgnoredError` after 10 s,
  so the episode is recorded as `error`, and the log line "slack interaction processed" says why.
- `tool_calls` counts the remediation too: `docker ps -a` + fix + health curl = 3.

## 4. A risk this run will measure: health-check timing

`check_health` curls **once**, right after the fix. Your remediation steps (`docker compose restart` /
`up -d`) return when the container has *started*, not when it's *healthy*. The `scripted` baseline never
noticed, because the harness oracle keeps polling for up to `--settle-s`. The graph doesn't poll. If
Postgres or the API isn't answering yet, the graph refutes a correct hypothesis and replans.

How it shows up: `replans > 0` on `diagnoser-scripted`, or `escalated` with `agent_outcome=escalated` while
the oracle later says resolved. The scripted reasoner then proposes the same fix again, and the harness
approves again (tested: `test_failed_health_check_replans_and_asks_again`).

If it happens, the fix belongs in the executor (yours): a final "wait until the service's Docker
healthcheck says healthy" step, so `COMPLETED, exit 0` really means "applied and up". Don't change it
until the run shows it's needed.

## 5. Run it (yours, ~10 minutes)

```bash
cd ~/CSCI-599/Capstone_Project
git fetch && git switch feat/harness-diagnoser && uv sync
uv run pytest -q -W error                       # expect all pass (Docker tests skipped)
target/up.sh                                     # if the stack isn't up

# Your .env must have APPROVAL_SIGNING_KEY (the executor and the diagnoser's handler both read it).
export EXECUTOR_DEV_TOKEN=$(uv run python -c "import secrets;print(secrets.token_hex(24))")
export EXECUTOR_URL=http://127.0.0.1:8080        # .env.example's 10.0.0.2 is the VM address
uv run python -m executor --port 8080 &
sleep 2

uv run python -m evals.harness --agent diagnoser-scripted --faults all --runs 1
# if that's 5/5:
uv run python -m evals.harness --agent diagnoser-scripted --faults all --runs 3
uv run python -m evals.harness --agent diagnoser-scripted --faults all --runs 1 --config memory=false

kill %1; wait %1 2>/dev/null
```

Paste back: the final `N/M resolved · invalid K` line of each run, and from the newest
`evals/results/<date>/<run>/summary.md` the replans and false-resolution lines. If an episode shows
`error`, also paste the executor's and harness's log lines around it.

## 6. What's verified and what isn't

Verified here (cloud session): 17 tests in `tests/test_diagnoser_agent.py`, all through Sai's real
service and my real executor server in one process (only `run_argv` and Slack's network are fake):
every fault's expected fix resolves, a failed first health check replans and gets approved again, a key
mismatch escalates with nothing run and nothing saved, the ignored-click and ablation cases. Mutation
checks: unsigned click, wrong blocked count, ignored memory/max_hypotheses ablations, approver not
allowed, reasoner never confirming. Each one fails a test.

**Not verified:** anything against real Docker (the health-check timing above is the main unknown), the
LLM reasoner (not merged), and token in/out split: the graph keeps one total, recorded as `tokens_in`,
with cost left at 0 until it's split.

## 7. Study

- `httpx.ASGITransport` runs the app in-process. It waits for the app *including background tasks*,
  so `POST /incidents` returns once the graph has paused or ended. That's why polling is short here.
- Slack request signing: https://api.slack.com/authentication/verifying-requests-from-slack
- LangGraph interrupts and resume: https://langchain-ai.github.io/langgraph/concepts/human_in_the_loop/
- Python truthiness and `__len__`: https://docs.python.org/3/reference/datamodel.html#object.__bool__
