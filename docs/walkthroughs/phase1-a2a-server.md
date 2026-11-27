# Walkthrough — the executor's A2A server

**Built:** 2026-11-26 (Claude, cloud session, for Om) · **Branch:** `feat/executor-a2a-server` ·
**Status:** 267 tests pass; mutation checks pass; local smoke test over a real socket passes;
Sai's `A2AExecutorClient` checked against it in a scratch merge (5/5). Not yet on the VM (GCP on hold).

---

## 1. Where this sits

```
diagnoser (Sai)                                   executor (you)
A2AExecutorClient ── HTTPS, Bearer ID token ──►  GuardMiddleware   auth, A2A-Version, body size
   SendMessage(JSON-RPC)                            │
                                                    ▼
                                    a2a-sdk DefaultRequestHandler
                                                    │
                                                    ▼
                                    IncidentAgentExecutor.execute()
                                    ├─ diagnose  → CommandRunner.diagnose   (PR #4/#5)
                                    └─ remediate → Remediator.remediate     (PR #6)
```

Everything below the SDK was already built. This PR is the network boundary: who may call, in
what format, and how results go back as A2A Tasks.

**Look at:** `src/executor/server.py` top docstring, then `GuardMiddleware.__call__`, then
`IncidentAgentExecutor.execute`.

## 2. The guard middleware (the front door)

Runs before the SDK sees a byte, on every route except `/healthz`:

| Check | Status | Why |
|---|---|---|
| `Authorization: Bearer <token>` present and valid | 401 | only an authenticated caller gets anything, even the Agent Card |
| token's email == `ALLOWED_DIAGNOSER_SA` | 403 | a valid Google token for *another* service account is still refused |
| `A2A-Version: 1.0` on POST | 400 | refuse v0.3 clients rather than misread them |
| body ≤ 64 KB (incl. chunked) | 413 | a request is a few hundred bytes; this caps memory per request |

Two implementation choices worth understanding:

- **Pure ASGI middleware, not `BaseHTTPMiddleware`.** It reads the body itself, with the size limit
  applied while it streams in, then replays it to the app. An oversized chunked body is refused
  with 413 instead of blowing up inside the SDK. After the replay, further reads wait on the real
  connection so disconnect detection still works.
- **ID-token verification is injected** (`TokenVerifier`). Production uses
  `google_id_token_verifier` (google-auth checks signature, expiry, issuer, audience; then we check
  the email). Tests use a fake. Locally, `EXECUTOR_DEV_TOKEN` stands in, and `__main__` refuses to
  start with it unless bound to `127.0.0.1`, so it can't accidentally protect a real deployment.

The VM will have no external IP (INTERFACES §0). That's the first lock; this is the second.

## 3. One SendMessage → one Task

`execute()` does five things, in an order the SDK and Sai's client both depend on:

1. **Enqueue the Task first** (SUBMITTED). a2a-sdk 1.3 rejects status updates before that (Sai found this).
2. **Exactly one JSON data part** → `contracts.ExecutorRequest`. A text part ("ignore previous
   instructions…"), two parts, or an off-contract field → `REJECTED a2a.bad_request`. The reason
   lists field *locations* only; values are never echoed (`test_off_contract_payload_is_rejected_without_echoing_values`).
3. **`context_id == incident_id`**, or `REJECTED a2a.context_mismatch`. Sai's client refuses a Task
   whose context doesn't match, so the server enforces the same rule on the way in.
4. **Dispatch** to the runner or the remediator.
5. **Publish** exactly one terminal state: COMPLETED with one `command_result` artifact, or
   REJECTED/FAILED with the info in `status.message`.

**Any exception → `FAILED internal_error`.** The SDK would otherwise turn it into a JSON-RPC error
containing the exception text (paths, values: Sai's second finding). The test raises an exception
containing `DB_PASSWORD=hunter2` and checks it never reaches the caller.

## 4. Sai's three asks, done here

The Agent Card's skill descriptions *are* the policy summaries:

- `diagnose` = `CommandValidator.summary()`, now with "short boolean flags may be combined" and a
  container → service map ("Diagnostics take CONTAINER names; remediation params take the SERVICE name").
- `remediate` = `Remediator.summary()`: enabled actions, which services have a config file, MB
  ranges, allowed tags, and which actions are refused even if approved.

Sai's client already fetches the card, so the diagnoser's prompt can be built from it and always
matches what the executor enforces. `test_agent_card_shape` pins that.

## 5. How it was checked

- `tests/test_executor_server.py` (22): a real a2a-sdk client over httpx's in-process ASGI
  transport, against the real app. Covers every task state, malformed input, the card, and all
  middleware statuses.
- Mutation checks done in the session: removing auth, the version check, the context check or the exception catch
  each made tests fail.
- **Cross-check with Sai's client** (not committed: it needs both stacks merged). In a scratch merge of
  `feat/a2a-client` + this branch: 401 tests pass, lint clean, and 5 extra tests drove Sai's
  `A2AExecutorClient` against this server: diagnose completed, diagnose rejected (`curl.url`), the
  exact `check_health` argv, remediate completed, bait-and-switch refused. The merge conflicts were
  only `DECISIONS.md` (keep both), `pyproject.toml` (union of deps) and `uv.lock` (re-lock).
- **Smoke test of the real entry point** (`python -m executor`, dev token, real socket): healthz 200,
  card 401 without token / served with it, JSON-RPC without version 400, `SendMessage uptime` → COMPLETED,
  logs are JSON.

## 6. Try it yourself (local, ~2 minutes)

```bash
cd ~/CSCI-599/Capstone_Project
git fetch origin && git switch feat/executor-a2a-server && uv sync
# `uv run python`, not `python`: WSL/Ubuntu has no bare `python` command, and an empty key
# makes the executor refuse to start ("APPROVAL_SIGNING_KEY is not set"), which is by design.
export APPROVAL_SIGNING_KEY=$(uv run python -c "import secrets;print(secrets.token_hex(32))")
export EXECUTOR_DEV_TOKEN=$(uv run python -c "import secrets;print(secrets.token_hex(24))")
export EXECUTOR_URL=http://127.0.0.1:8080
uv run python -m executor --port 8080 &
sleep 3
curl -s -H "Authorization: Bearer $EXECUTOR_DEV_TOKEN" localhost:8080/.well-known/agent-card.json | uv run python -m json.tool
kill %1
```

Read the two skill descriptions in the card: that's exactly what Sai's prompt will be built from.

## 7. Not done yet

- Running on the GCE VM with real ID tokens (GCP on hold): systemd unit, firewall, Direct VPC egress.
- Sandbox container + read-only Docker socket proxy for the runner.
- The committed cross-test with Sai's client lands once both stacks are merged into `main`.

## 8. Study

| Topic | Read | Then look at |
|---|---|---|
| A2A v1.0 spec: Task lifecycle, Agent Card | [A2A specification](https://a2a-protocol.org/latest/specification/) (checked 2026-11-23) | `execute()`, `build_agent_card()` |
| Google ID tokens for service-to-service auth | Google Cloud docs: "Authenticating service-to-service" (not link-checked this session) | `google_id_token_verifier` |
| ASGI middleware | Starlette docs: "Middleware" → pure ASGI middleware (not link-checked this session) | `GuardMiddleware` |
