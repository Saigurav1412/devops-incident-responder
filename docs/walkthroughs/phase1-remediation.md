# Walkthrough — remediation: performing an approved fix

**Built:** 2026-11-26 (Claude, cloud session, for Om) · **Branch:** `feat/executor-remediation` ·
**Status:** 245 tests pass; mutation checks pass; **5 live fix-each-fault tests written, not yet run**
(they need Docker: step 6 below is yours).

---

## 1. Where this sits

```
Slack Approve ──► Sai's handler mints token (contracts.approval_token.mint)
                         │
diagnoser.execute_fix ──A2A── RemediateRequest{action, params, approval_token, idempotency_key}
                         ▼
             executor.Remediator.remediate()   ← this PR
                 5 gates, then code-built steps via CommandRunner.run_argv
```

Diagnostics (PR #4/#5) are read-only and need no approval. Remediation **changes** the system, so
it needs a human's approval, and the executor checks that approval itself instead of trusting
the diagnoser to have asked.

**Look at:** `src/executor/remediation.py` top docstring (the five gates), then `remediate()`.

## 2. The five gates

| # | Gate | Refusal | Stops |
|---|---|---|---|
| 1 | `idempotency_key == action_id` | `remediate.idempotency_key` | a caller inventing its own keys to dodge gate 5 |
| 2 | action known + enabled in `remediation.yaml` | `remediate.unknown_action` / `action_disabled` | anything outside the catalog; `set_env` and `prune_path` here |
| 3 | `verify(key, token, ...)` on the request **as received** | reason `approval_invalid` / `expired` / `malformed` | bait-and-switch: approve "restart db", send "restart api" |
| 4 | params valid for the action | `remediate.params` | a human approving something unsafe (`tag=latest`, `mb=99999`, a file path) |
| 5 | not already run (sqlite store) | returns stored outcome / `remediate.interrupted` | running a fix twice on a retry or after a crash |

Gate 3 before gate 4 is deliberate: an unapproved caller learns nothing about which params would
be valid. Gate 4 after gate 3 is the point of "**approval doesn't bypass policy**": the test
`test_bad_params_are_refused_even_with_a_valid_approval` mints a correct token for bad params and
checks it's still refused, with nothing changed on disk.

## 3. Why some actions are off

`set_env` would need the agent to write `DATABASE_URL=postgresql://app:<password>@db/...`, but the
runner redacts the password to `***` before the agent sees it. The agent *can't* produce a correct
value, so enabling it only invites a broken fix. `restore_config` undoes env drift safely.
`prune_path` (deleting files) isn't needed by any fault and is the riskiest action, so it's off.
Both stay in the catalog: enabling one later is a YAML change plus tests, not a contract change.

## 4. Steps are data, built by our code

`_plan()` turns validated params into a list of steps: `CopyFile`, `SetEnvFileVar`, `RunArgv`. The
LLM never supplies a command. That also makes plans testable without Docker:
`test_restore_config_proxy_copies_in_place_and_reloads` checks the exact argv and that the file
keeps its inode (the bind-mount gotcha from the target-stack walkthrough).

`RunArgv` steps go through `CommandRunner.run_argv`, the same fixed-PATH, no-shell, bounded,
redacted path as diagnostics. `diagnose()` = validator + `run_argv`; remediation = five gates +
`run_argv`. The validator isn't used for remediation steps on purpose: they're built by our code,
and some (`docker compose restart`) are exactly what diagnostics must never allow.

## 5. Idempotency and crashes

`IdempotencyStore` writes `started` before the first step and `done` + outcome after the last.

- Retry after success → stored outcome returned, nothing runs (`test_retry_returns_stored_outcome_without_rerunning`).
- Executor restarted → same, because it's sqlite on disk (`test_idempotency_survives_an_executor_restart`).
- Crash between `started` and `done` → refused, not re-run: we can't know how far it got, and
  running half a fix twice can be worse than running it once (`test_interrupted_attempt_is_not_rerun`).
- Two identical requests at the same moment → one global lock, so it runs once (`test_concurrent_identical_requests_run_once`).

Mutation check done in the session: removing the token check, the tag check, the lock or the interrupted-attempt
check each made tests fail.

## 6. Run the live fix-each-fault test (yours, ~3 minutes)

`tests/test_remediation_docker.py` does, for each of the 5 faults: reset → inject → confirm broken
→ mint a token the way Sai's Slack handler will → `Remediator.remediate(expected fix)` → wait for the
oracle → retry the same request and check it doesn't run again → reset.

```bash
cd ~/CSCI-599/Capstone_Project
git fetch origin && git switch feat/executor-remediation && uv sync
./target/up.sh
RUN_DOCKER_TESTS=1 uv run pytest -m docker -v
./target/down.sh
```

Expected: `33 passed` (28 runner + 5 remediation). The one I'm least sure of is `db-stopped-01`:
it relies on `docker compose restart` starting a *stopped* container. If that one fails, paste it
and I'll switch that action to `docker compose start`.

## 7. Not done yet

- A2A server exposing `diagnose()` and `remediate()` (next).
- Where the sqlite file lives on the VM (`EXECUTOR_STATE_PATH`, with the A2A server).
- Rollbacks: the `rollback` field on a proposal isn't executed automatically; a rollback is just
  another proposal that needs its own approval.

## 8. Study

| Topic | Read | Then look at |
|---|---|---|
| Idempotency keys | Stripe API docs: "Idempotent requests" (search stripe.com/docs; not link-checked this session) | `IdempotencyStore`, gate 5 |
| HMAC verification | [Python `hmac` docs](https://docs.python.org/3/library/hmac.html) | `contracts/approval_token.py` (gate 3) |
| docker compose restart/up semantics | Docker docs: `docker compose restart`, `docker compose up` (not link-checked this session) | `_plan()` |
