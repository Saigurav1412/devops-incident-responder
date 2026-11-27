# Walkthrough — the eval harness

**Built:** 2026-11-26 (Claude, cloud session, for Om) · **Branch:** `feat/eval-harness` ·
**Status:** 287 tests pass; mutation checks pass; **live runs not done yet** (they need Docker: step 6 is yours).

---

## 1. What it's for

The report's main claim is a number like "resolved 17/20 faults, median 90 s". The rubric
asks for that number to be real, repeated and explainable. The harness produces it, and it is
built so the number can't flatter us by accident:

| Ways an eval lies | What the harness does |
|---|---|
| a fault didn't take, so "resolved in 0 s" | each `inject.sh` confirms its own fault, then the oracle must fail; else **invalid**, excluded |
| trusting the agent's "I fixed it" | the **oracle** decides; the agent's claim is kept separately (false resolutions are counted) |
| one lucky run | `--runs 3`, repeats interleaved; every rate printed with its n |
| one broken episode poisoning the next | reset before and **always** after, even on crash/Ctrl-C |
| a crash losing the whole run | each episode appended to `episodes.jsonl` the moment it finishes |
| approval wait hiding in TTR | `approval_wait_s` recorded; TTR shown with and without it |

**Look at:** `evals/harness.py` docstring (the 7 steps), then `run_episode()`.

## 2. One episode

```
reset ─► inject (+ confirm) ─► oracle must FAIL ─► Alert ─► agent.handle() ─► oracle polls ─► record ─► reset
           │ fails                 │ still healthy           (timeout)            (settle window)
           ▼                       ▼
        INVALID                 INVALID
```

The agent gets only an `Alert`: "GET /health through the proxy is failing", from `eval_harness`,
service `proxy`. That's what a real monitor on the front door would page with. No fault id, no
category, no hint (`test_alert_carries_no_hint_of_the_fault`).

After the agent returns, the harness polls the oracle for up to `--settle-s` (default 60 s; a
recreated container needs a few seconds), never past the fault's timeout.

## 3. Outcomes

| `outcome` | Meaning |
|---|---|
| `resolved` | oracle healthy in time, whatever the agent said |
| `escalated` / `rejected` / `budget_exceeded` | the agent stopped on its own and the stack is still broken |
| `escalated` (with `agent_outcome=resolved`) | **false resolution**: the agent said fixed, the oracle disagrees |
| `timeout` | the agent didn't finish within the timeout |
| `error` | the agent raised |

## 4. Two baselines, and why both matter

- **noop** must score **0**. It's the control: if it ever "resolves" something, the oracle or a
  reset is broken, and every other number is meaningless.
- **scripted** applies each fault's `expected_fix` + `expected_params` (new in `fault.yaml`; the real
  agent never sees them) through the **real** `Remediator`, approved by `ScriptedApprover` (the
  eval's stand-in for the Slack click, the only other place allowed to mint tokens). It should
  score **100%**. That proves approval → five gates → steps → oracle works end to end, and gives the
  upper bound for TTR (how fast a perfect agent could be).

Sai's diagnoser plugs in behind the same `Agent.handle(alert, fault, config)` once both stacks are
merged; its adapter must pass only the alert on.

## 5. Inject self-checks (the Redis issue from your first run)

During the first real run, `cache-maxmemory-01` was still restarting Redis when the oracle ran, so
the 503 might have come from the restart, not the OOM. Each `inject.sh` now ends with
`confirm <seconds> "<what>" <check>` (`target/lib.sh`), retried once a second:

| Fault | Confirms |
|---|---|
| db-stopped-01 | db container `State.Running` is false |
| proxy-bad-upstream-01 | `/health` through the proxy is 502 |
| api-bad-db-url-01 | `/health` is 503 *from the api* and the db check isn't ok |
| cache-maxmemory-01 | `redis-cli SET` answers **OOM** (the actual fault, not "Redis is restarting") |
| api-bad-image-01 | the api container's image is `ir-target-api:broken` |

One shell trap avoided: with `set -o pipefail`, `cmd | grep -q x` can fail even when grep matches
(grep exits early, cmd gets SIGPIPE). The checks capture output first, then grep it.

## 6. Run it (yours, ~20 minutes)

```bash
cd ~/CSCI-599/Capstone_Project
git fetch origin && git switch feat/eval-harness && uv sync
./target/up.sh

# control: expect 0/5 resolved, 0 invalid (~5 min)
uv run python -m evals.harness --agent noop --faults all --runs 1 --timeout-s 20 --settle-s 10

# upper bound: expect 15/15 resolved, 0 invalid (~15 min)
uv run python -m evals.harness --agent scripted --faults all --runs 3

./target/down.sh
cat evals/results/*/*-scripted/summary.md
```

Paste both summaries. Any **invalid** episode means an inject check needs fixing (it'll say which
in `invalid.jsonl`); any noop "resolved" means the oracle is wrong.

## 7. Study

| Topic | Read | Then look at |
|---|---|---|
| Percentiles on small samples | Wikipedia: "Percentile", nearest-rank method (not link-checked this session) | `metrics.percentile` |
| Why a do-nothing control | any intro to A/B testing or ablation studies (not link-checked this session) | `NoopAgent` |
| bash `pipefail` + SIGPIPE | Bash manual: "Pipelines" (not link-checked this session) | `cache-maxmemory-01/inject.sh` |
