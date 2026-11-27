# Interface Contracts

These are the seams between Om's slices and Sai's slices. Each section becomes Pydantic models in
`src/contracts/` and both sides import from there; nobody redefines these shapes locally.

**Changing a contract:** edit this file and `src/contracts/` in the same PR, tag the other person,
and bump `CONTRACT_VERSION` in `src/contracts/__init__.py`.

Status markers: **[agreed]** both signed off · **[proposed]** draft, needs the other person's review.

---

## 0. Topology at a glance

```
Slack ──(signed interaction webhook)──▶ Diagnoser (Cloud Run, LangGraph, diagnoser-sa)
                                            │  ▲
                              checkpoints + │  │ incident memory
                                            ▼  │
                                         Firestore
                                            │
                     A2A over Direct VPC egress (internal IP only)
                                            ▼
                              Executor (GCE VM, A2A server, executor-sa)
                                            │  subprocess, no shell, sandbox container
                                            ▼
                              Target stack (docker-compose on the same VM)
```

The diagnoser never touches the VM except through A2A. The executor never calls an LLM's output
directly as a shell string. It enforces every rule below in code, whatever the diagnoser asks.

---

## 1. Diagnoser graph state **[proposed]** — owner: Sai

```python
class Hypothesis(BaseModel):
    id: str                      # "h1", "h2", ...
    statement: str               # "postgres container is OOM-killed"
    evidence_needed: list[str]   # diagnostic checks that would confirm/refute it
    status: Literal["open", "confirmed", "refuted"]
    confidence: float            # 0..1, set by the verify node

class ProposedAction(BaseModel):
    action_id: str               # uuid4, unique per proposal
    action: str                  # name from the remediation catalog (§2.4)
    params: dict[str, str]       # validated against that action's schema
    rationale: str
    rollback: str | None         # catalog action that undoes it, if any

class IncidentState(TypedDict):
    incident_id: str             # uuid4; also the LangGraph thread_id
    alert: Alert                 # §4.1
    recalled: list[IncidentRecord]      # from memory (§3), wrapped as data
    hypotheses: list[Hypothesis]
    observations: list[Observation]     # every A2A call + result, append-only (src/contracts/models.py)
    proposed: ProposedAction | None
    approval: ApprovalDecision | None   # §4.2
    outcome: Literal["open", "resolved", "escalated", "rejected", "budget_exceeded"]
    budget: Budget               # counters: iterations, replans, tool_calls, tokens (+ tokens_in/tokens_out split), started_at
```

**Nodes (fixed names, used in traces and the code map):**
`ingest_alert → recall_memory → plan_hypotheses → diagnose → verify_hypothesis`
`verify_hypothesis --refuted--> plan_hypotheses` (replan, bounded by `MAX_REPLANS`)
`verify_hypothesis --confirmed--> propose_fix → request_approval (interrupt) → execute_fix → check_health`
`check_health --fail--> plan_hypotheses` · `check_health --pass--> save_memory → END`
`execute_fix --fix not applied--> escalate` (remediate task not `completed` with `exit_code == 0`)
`propose_fix --no valid proposal--> escalate` (the reasoner raised `ReasonerError`)
`verify_hypothesis --still open--> diagnose` (gather more evidence; bounded by the caps below)
`request_approval --reject--> END` (`outcome="rejected"`)
Out of replans → `escalate → END` (`outcome="escalated"`). Any cap hit → `budget_exceeded → END`.

Implemented in `src/diagnoser/` (graph.py wires these names; nodes.py has the routers).
`incident_id` is the LangGraph `thread_id`. Approval pauses with `interrupt()`; the interrupt value is
`{"kind": "approval_request", "incident_id", "proposed": ProposedAction as JSON, "requested_at": ISO-8601 UTC}` and the graph resumes
with `Command(resume=ApprovalDecision)`. A decision for a different `action_id`, from an approver not in
`APPROVER_ALLOWLIST`, or an approve without a token is ignored and the graph keeps waiting. The
diagnoser never mints tokens. Budget caps are not checked between approval and `execute_fix`, so time
spent waiting for a human never discards an approved fix. After `execute_fix`, the token is replaced
with `"redacted"` in `approval` and in the stored `RemediateRequest` observation.

`verify_hypothesis` treats the reasoner's assessment as a suggestion: only `status` and `confidence` of
existing ids change; a refuted hypothesis stays refuted; unknown ids, reworded statements and omitted
hypotheses are ignored (omitted = unchanged).

`check_health` sends one fixed `DiagnoseRequest` built by code, `["curl", "-fsS", "--max-time", "5",
HEALTH_URL]`, through the normal `diagnose` skill and records it as an observation (counts as a tool
call). Healthy = `completed` with `exit_code == 0`; anything else (non-zero exit, rejected, failed,
executor unreachable) is unhealthy and refutes the confirmed hypothesis. `HEALTH_URL` must be one of
the executor allowlist's exact curl URLs.

`check_health` only runs if the executor applied the fix: the remediate observation for the current
`action_id` is `completed` with `exit_code == 0`. Otherwise (rejected, failed, unreachable, non-zero
exit) the graph escalates without a health check and without refuting the hypothesis, because an
unapplied fix says nothing about the diagnosis. `save_memory` checks the same thing again and raises
rather than store a `verified` record for a fix that didn't run.

**Reasoner (LLM), implemented in `src/diagnoser/reasoner.py` (`LLMReasoner`) over a provider-independent
`ChatModel` (`src/diagnoser/llm.py`); prompts in `src/prompts/` (versioned, hashed):**
- System message = `diagnoser_common.v1` with the executor's two policy summaries (the `diagnose` and
  `remediate` skill descriptions of its Agent Card, §2.1) + the role prompt. User message = the incident.
- Untrusted text (alert summary, memory records, hypothesis statements, observations) reaches the model
  only JSON-encoded inside `===BEGIN/END UNTRUSTED DATA <nonce>===` blocks, with a fresh random nonce per
  call. Approval tokens are never rendered. Output tails are capped (2,000 chars per stream). Identical
  consecutive observations (same request, same result, e.g. `check_health` retries) are merged into one
  entry with `n`, `n_last`, `repeated`; only the last 12 entries show output.
- Answers must validate against strict schemas (`PlanAnswer`, `CommandAnswer`, `AssessAnswer`,
  `ProposeAnswer`: unknown fields forbidden, bounded strings, params as `[{name, value}]`). One repair
  turn, then a safe default: plan `[]` (→ escalate), command `None`, assess `[]` (no change), propose
  raises `ReasonerError` (→ escalate). Provider errors are not retried by the reasoner. Tokens of every
  attempt, failed ones included, go to the budget.
- If the executor's card can't be fetched, no LLM call is made and the same defaults apply.
- Tokens: `Budget.tokens` (the total the cap checks) plus `tokens_in` / `tokens_out` for cost. The LLM
  reasoner fills all three; reasoners that report only a total (scripted fakes) leave the split at 0.
- **Provider adapter** (`src/diagnoser/openai_chat.py`, `OpenAIChatModel`): any OpenAI-compatible
  chat-completions API via the `openai` SDK; `LLM_BASE_URL` empty = OpenAI. Sends
  `response_format=json_schema` with `strict: true` (schema made strict by `strict_schema()`: every
  object `additionalProperties: false` and all properties `required`; identical to the SDK's own rules,
  checked by a test) and `max_completion_tokens`; no `temperature`. A 400 gets one retry with
  `json_object`. Errors become `ChatModelError("<status> <ErrorType>")` (no key, prompt or body).
  A refusal or empty answer is an error. Missing `usage` counts 0 tokens.
- The policy summaries are cached for the diagnoser's process lifetime. **Deploy note:** after an
  executor redeploy that changes `allowlist.yaml` / `remediation.yaml`, restart the diagnoser too
  (until then the prompt shows the old policy; the executor still enforces the new one).

**Budget caps (enforced in code, not the prompt):** `MAX_ITERATIONS`, `MAX_TOOL_CALLS`,
`MAX_TOKENS_PER_INCIDENT`, `MAX_WALL_CLOCK_S`. Hitting any cap ends with `outcome="budget_exceeded"`
and a Slack notice.

### 1.1 Diagnoser HTTP API **[proposed]** — owner: Sai; callers: monitor, eval harness (Om)

Implemented in `src/diagnoser/service.py` (`create_service(deps, poster)`). One Cloud Run service.

| Endpoint | Auth | Request | Response |
|---|---|---|---|
| `POST /incidents` | `Authorization: Bearer <INCIDENT_API_TOKEN>` | an `Alert` (§4.1) as JSON; unknown fields, naive datetimes, `summary` > 500 chars → 422 | `202 {"incident_id", "status_url"}`; the graph runs in the background |
| `GET /incidents/{incident_id}` | same bearer token | — | `200 {"incident_id", "outcome", "waiting_for_approval", "hypotheses": [{id, statement, status}], "proposed": {action, params} \| null, "budget": {iterations, replans, tool_calls, tokens, tokens_in, tokens_out}}`; unknown id → 404 |
| `POST /slack/interactions` | Slack signature (§4.2) | Slack's form-encoded `payload` | empty 200 (or 401 / 413) |
| `GET /healthz` | none | — | `{"status": "ok"}` |

- Wrong or missing token → 401; `INCIDENT_API_TOKEN` unset → 503 `incident_api_token_not_configured` (fails closed).
- Every alert starts a new incident with a fresh uuid `incident_id` (no de-duplication by `alert_id` yet).
- Status never includes `observations` (raw, untrusted command output) or any token.
- Callers poll `GET /incidents/{id}` until `outcome != "open"`. Whenever the graph pauses for approval,
  including a second time after a failed health check, the service posts the approval message to Slack.

---

## 2. A2A: diagnoser ↔ executor **[proposed]** — owner: Om (server), Sai (client)

**Protocol version: A2A v1.0** (stable since March 2026; spec repo `a2aproject/A2A`).
**SDK: `a2a-sdk` 1.3.x** (official Python SDK, implements v1.0; pin `a2a-sdk[http-server]>=1.3,<2`).
Checked 2026-11-23 against the v1.0 "what's new" doc and by installing `a2a-sdk` 1.3.1 and reading its
types. Things that differ from older (v0.3) tutorials, which most blog posts still show:

| Topic | v1.0 (use this) | v0.3 (don't) |
|---|---|---|
| JSON-RPC method names | `SendMessage`, `GetTask`, `CancelTask`, `ListTasks`, `SendStreamingMessage`, `SubscribeToTask` | `message/send`, `tasks/get`, … |
| Task states | `TASK_STATE_COMPLETED`, `TASK_STATE_FAILED`, `TASK_STATE_REJECTED`, `TASK_STATE_WORKING`, … (SCREAMING_SNAKE) | `completed`, `rejected`, … |
| Part type | no `kind` field: a part is whichever of `text` / `data` / `url` / `raw` it carries, plus `mediaType` | `kind: "data"` |
| Agent endpoint | `supportedInterfaces[{url, protocolBinding: "JSONRPC", protocolVersion: "1.0"}]` | top-level `url` |
| Version header | client sends `A2A-Version: 1.0` on every request | — |
| JSON field names on the wire | camelCase (`messageId`, `contextId`, `mediaType`) | — |

**SDK gotcha:** `a2a-sdk` 1.x types (`Message`, `Part`, `Task`, `AgentCard`) are **protobuf messages,
not Pydantic models**. Our own payload models in `src/contracts/` stay Pydantic; they go into a part as
`Part(data=<protobuf Struct from model.model_dump(mode="json")>, media_type="application/json")`, and
come out with `MessageToDict` + `Model.model_validate(...)`. Keep that conversion in one helper
(`src/contracts/a2a_codec.py`) so neither side hand-rolls it.

### 2.1 Agent Card
Served by the executor at `GET /.well-known/agent-card.json`. JSON-RPC endpoint at `/`.

| Field | Value |
|---|---|
| `name` / `version` | `incident-executor` / package version |
| `supportedInterfaces` | `[{url: EXECUTOR_URL, protocolBinding: "JSONRPC", protocolVersion: "1.0"}]` |
| `capabilities` | `streaming: false`, `pushNotifications: false` (commands are short; plain `SendMessage`) |
| `defaultInputModes` / `defaultOutputModes` | `["application/json"]` |
| `securitySchemes` | `{"google-id-token": httpAuthSecurityScheme{scheme: "Bearer", bearerFormat: "JWT"}}` |
| `securityRequirements` | `[{"google-id-token": []}]` |

Skills (`id`, `name`, `description`, `tags` are all required in v1.0):

| Skill id | Purpose | Needs approval token |
|---|---|---|
| `diagnose` | run one read-only diagnostic command | no |
| `remediate` | run one catalog action that changes the target | **yes** |

Auth: Cloud Run calls with a Google-signed ID token for `diagnoser-sa` in `Authorization: Bearer …`
(A2A carries no credentials inside messages). The executor verifies the token's audience and
service-account email, and rejects a missing or unsupported `A2A-Version`, before reading the payload.
No external IP on the VM.

**Server side, implemented in `src/executor/server.py`** (run: `uv run python -m executor --host … --port …`):

| Route | Auth | Notes |
|---|---|---|
| `GET /.well-known/agent-card.json` | bearer | skill descriptions = `CommandValidator.summary()` (`diagnose`) and `Remediator.summary()` (`remediate`): prompt-ready, generated from the enforced policy |
| `POST /` (JSON-RPC) | bearer + `A2A-Version: 1.0` | `SendMessage` → one Task per command/fix; other methods (e.g. `GetTask`) as the SDK provides |
| `GET /healthz` | none | liveness only |

- Guard middleware runs first on every route except `/healthz`: no/invalid bearer → **401**, valid
  token but not `ALLOWED_DIAGNOSER_SA` (or email not verified) → **403**, JSON-RPC without
  `A2A-Version: 1.0` → **400**, body over 64 KB (also chunked) → **413**. Body is `{"error": "<short reason>"}`.
- ID token checked with `google-auth` (`verify_oauth2_token`): signature, expiry, issuer, audience =
  `EXECUTOR_AUDIENCE` (empty → `EXECUTOR_URL`).
- Every task: `Task` enqueued first (SUBMITTED), then exactly one terminal state. `context_id` must
  equal `incident_id` (`REJECTED`, `a2a.context_mismatch`); not exactly one JSON data part, or off-contract
  payload → `REJECTED`, `a2a.bad_request` (field locations only, values never echoed). Any exception →
  `FAILED`, reason `internal_error`; details only in the executor's logs.
- `CancelTask` → `REJECTED` (`a2a.cancel`): a fix must not stop half-way.
- Local development without GCP: `EXECUTOR_DEV_TOKEN` (≥ 32 chars) replaces ID-token checks, and the
  executor refuses to start with it unless bound to `127.0.0.1`.
- Tasks live in the SDK's in-memory task store (lost on restart; the idempotency store is what must persist).
- Checked against Sai's `A2AExecutorClient` in a scratch merge of both stacks (2026-11-26): diagnose
  completed/rejected, the exact `check_health` argv, remediate completed and bait-and-switch refused.

### 2.2 Request payload (exactly one `data` part per `Message`, `role: ROLE_USER`)

The diagnoser sends one `SendMessage` per command or action. The message's `contextId` is the
`incident_id`, so every task for one incident is grouped together on the executor.

```python
class DiagnoseRequest(BaseModel):
    skill: Literal["diagnose"]
    incident_id: str
    command: list[str]           # argv, e.g. ["docker", "logs", "--tail", "200", "api"]
    timeout_s: int = 20

class RemediateRequest(BaseModel):
    skill: Literal["remediate"]
    incident_id: str
    action_id: str               # same id the approval was issued for
    action: str                  # catalog name
    params: dict[str, str]
    approval_token: str          # §4.3
    idempotency_key: str         # = action_id; executor won't run the same key twice
```

### 2.3 Task outcomes

The executor always answers with a `Task` (never a bare `Message`), so every command has a task id
that shows up in traces and in `ListTasks`. All three end states below are terminal in v1.0.

| Situation | `status.state` | Where the result goes |
|---|---|---|
| Command ran (any exit code) | `TASK_STATE_COMPLETED` | artifact `command_result`, one `data` part: `{exit_code, stdout, stderr, truncated, duration_ms}` |
| Validator or allowlist refused it | `TASK_STATE_REJECTED` | `status.message`, one `data` part: `{reason, rule_id}`; logged as a blocked unsafe action |
| Bad / missing / expired approval token | `TASK_STATE_REJECTED` | `status.message`, one `data` part: `{reason: "approval_invalid"}` |
| Executor error, timeout | `TASK_STATE_FAILED` | `status.message`, one `data` part: `{reason}` |

A non-zero exit code is still `COMPLETED`: the command ran, and a failing `curl` or `docker inspect`
is evidence for the diagnoser, not an executor failure. `REJECTED` is reserved for "the executor
refused to run this", which is what the eval counts as a blocked unsafe action.

`stdout`/`stderr` are capped (`MAX_OUTPUT_BYTES`, default 16 KB, tail kept). The diagnoser must treat
them as untrusted data (they are an injection surface: log lines are attacker-controlled).

**Client side, implemented in `src/diagnoser/a2a_client.py` (`A2AExecutorClient`)**, how it reads your answers:
- Fetches the Agent Card once from `EXECUTOR_URL`, then one non-streaming `SendMessage` per request
  (timeout: `timeout_s + 10` for diagnose, 300 s for remediate). `A2A-Version: 1.0` is added by the SDK;
  `Authorization: Bearer <token>` comes from an injected token provider, asked on every request.
- Exactly one `Task` must come back. `COMPLETED` needs **exactly one** artifact named `command_result`
  with **exactly one** data part that validates as `CommandResult`. `REJECTED`/`FAILED` read
  `RejectionInfo`/`FailureInfo` from `status.message`; if that part is missing or malformed the state is
  kept and the reason becomes `rejected_without_reason` / `failed_without_reason`.
- Anything else raises `ExecutorClientError` (the graph records a `failed` observation with reason
  `executor_error: …`): transport errors and JSON-RPC errors (`a2a_call_failed: <type>`), a missing,
  duplicated or off-contract `command_result`, a bare `Message` instead of a `Task`, a non-terminal or
  unexpected state (e.g. `WORKING`, `INPUT_REQUIRED`), or a task whose `context_id` isn't the incident_id.
- The client also tail-caps stdout/stderr to `MAX_OUTPUT_BYTES` itself (defense in depth).
- `policy_summaries()` returns the card's `diagnose` and `remediate` skill descriptions (same single card
  fetch) for the reasoner's prompt; a card without both raises `agent_card_missing_skills: …`.

**a2a-sdk 1.3.1 server behaviour seen while testing the client** (relevant to the executor server):
- The `AgentExecutor` must enqueue a `Task` (e.g. `a2a.helpers.proto_helpers.new_task(..., SUBMITTED)`)
  before any `TaskUpdater` status update, or the request fails with "Agent should enqueue Task before
  TaskStatusUpdateEvent event".
- An uncaught exception in `execute()` does **not** become a `FAILED` task for a non-streaming
  `SendMessage` (despite the SDK docstring): the caller gets JSON-RPC `InternalError` (-32603) **with
  the exception's message in it**. So catch exceptions in the executor and answer `FAILED` with a
  short reason; otherwise exception text (paths, values) leaks to the caller.

### 2.4 Rules the executor enforces

**Diagnostics (`diagnose`):**
- argv only, executed with `subprocess.run(..., shell=False)` inside a sandbox container with the
  Docker socket proxied read-only and the target's config mounted read-only
- validated by `security.CommandValidator.check(argv)` (`src/security/allowlist.py`): default-deny
  grammar per binary in code, allowed values (containers, URLs, files, limits) in
  `src/security/allowlist.yaml`. A refusal returns `rule_id` + a reason written for the LLM to read
- allowed today: `docker ps | logs | inspect | stats --no-stream`, `docker exec` of only
  `redis-cli` (read commands) in cache, `pg_isready` in db, `nginx -t|-T` in proxy; `curl` GET of
  exact URLs (`http://127.0.0.1:8088/health`); `cat` of the four `target/runtime/` files (cwd = `target/`);
  `df`, `free`, `uptime`. Never: shells, docker global flags, `docker exec` flags, curl headers/output
  files/methods, the GCE metadata server, `target/known-good/`
- every argument: charset `[A-Za-z0-9._:/=-]` only, no `..` segment, ≤ 512 chars, ≤ 32 args
- `validator.summary()` renders the allowed commands as text for the diagnoser's prompt, so the
  prompt can't drift from what the executor accepts
- the graph's `check_health` sends `curl -fsS --max-time 5 http://127.0.0.1:8088/health` (allowed)
- run by `executor.CommandRunner.diagnose()` (`src/executor/runner.py`): binary resolved only on
  `/usr/local/bin:/usr/bin:/bin` (never the inherited `$PATH`), stdin closed, environment reduced to
  `PATH`, `LANG`/`LC_ALL` and the operator-set `HOME`/`DOCKER_HOST`/`DOCKER_CONFIG`, cwd = `target/`,
  own process group killed on timeout (`FAILED`, reason `timeout after Ns`), stdout/stderr each capped
  to the last `MAX_OUTPUT_BYTES` with the partial first line dropped, then secret-looking values
  masked (`executor.redact`: `*PASSWORD*=`, `user:pass@` in URLs, bearer tokens, `sk-`/`xox?-`/`gh?_`/`ya29.`)
- rule ids: `argv.*` (empty, charset, path, too_long, arg_too_long), `binary.*` (not_allowed, path),
  `docker.*` (subcommand, global_flag, flag, flag_value, flag_duplicate, follow, args, container,
  exec_flag, exec_program), `redis.*`, `pg_isready.*`, `nginx.args`, `curl.*` (flag, flag_value, url),
  `cat.*`, `df.args`, `free.args`, `uptime.args`

**Remediation (`remediate`) — catalog only, never free-form:**

The action **names** are code in `src/contracts/catalog.py` (`REMEDIATION_ACTIONS`); the fault
catalog and the approval token both refer to them. Each action's params and behaviour belong to the
executor. `restore_config` copies a file from `target/known-good/` into `target/runtime/` with `cp`
(same inode, so a bind-mounted file updates inside the container), then restarts or reloads the service.

Implemented in `src/executor/remediation.py` (`Remediator.remediate`); what is enabled and what each
action may touch is in `src/executor/remediation.yaml`. All params are strings.

| Action | Params (this target) | What the executor does | Enabled |
|---|---|---|---|
| `restart_service` | `service` ∈ proxy, api, db, cache | `docker compose restart <service>` | yes |
| `restore_config` | `service` + its `file`: proxy `nginx.conf`, api `api.env`, cache `redis.conf` | copy known-good → runtime (same inode), then proxy: `nginx -s reload`; api: recreate; cache: restart | yes |
| `scale_memory_limit` | `service`, `mb` (proxy 32–256, api/db 128–1024, cache 64–512) | set `<SVC>_MEM_LIMIT=<mb>m` in runtime/stack.env, `up -d --no-deps` | yes |
| `rollback_image` | `service` = api, `tag` = good | set `API_IMAGE_TAG`, `up -d --no-deps api` | yes |
| `set_env` | — | refused (`remediate.action_disabled`): the agent only sees redacted values, so it can't write a correct secret-bearing value; `restore_config` undoes env drift | no |
| `prune_path` | — | refused (`remediate.action_disabled`): no fault needs it; deleting is the riskiest action | no |

Gates, in order (each refusal changes nothing): `idempotency_key == action_id`
(`remediate.idempotency_key`) → action known (`remediate.unknown_action`) and enabled
(`remediate.action_disabled`) → approval token verified with `Settings.approval_key_bytes()`
(`rule_id: null`, reason `approval_invalid|approval_expired|approval_malformed`) → params valid
(`remediate.params`; **a valid approval does not bypass policy**) → not already run.

Idempotency: a sqlite store keyed by `idempotency_key`. A retry of a finished request returns the
stored outcome without running again; a key reused for a different request is refused
(`remediate.idempotency_conflict`); an attempt interrupted mid-way is **not** re-run (`FAILED`,
`remediate.interrupted`) and needs a new proposal. One remediation runs at a time.

Steps are built by the executor's code from validated params, never supplied by the LLM, and run
through the same `CommandRunner` path as diagnostics (fixed PATH, no shell, timeout, output cap,
redaction). Result: `COMPLETED` with every step listed in `stdout` and the last step's exit code;
`FAILED` (`remediate.step`) if a step couldn't run.

---

## 3. Long-term memory **[proposed]** — owner: Sai

```python
class IncidentRecord(BaseModel):
    incident_id: str
    created_at: datetime
    symptoms: str                # normalized alert + key observations, written by code
    root_cause: str              # the confirmed hypothesis statement
    fix: ProposedAction          # what actually worked
    verified: bool               # True only if check_health passed
    embedding: list[float]       # of `symptoms`

class IncidentMemory(Protocol):
    def save(self, record: IncidentRecord) -> None: ...
    def find_similar(self, symptoms: str, k: int = 3) -> list[IncidentRecord]: ...
```

- Store: Firestore collection `incidents`, vector search on `embedding`.
- **Write rule:** only `save_memory` writes, only after `check_health` passed, so records are
  `verified=True`. Nothing an LLM says during an incident is written directly.
- **Read rule:** recalled records go into the prompt inside a data block marked untrusted, and a
  recalled fix is a *hypothesis*, never an action to run without the normal approval path.
- Ablation switch: `MEMORY_ENABLED=false` makes `find_similar` return `[]`.
- Checkpoints (graph state) live in a separate collection `checkpoints/`. **[open]** custom
  `BaseCheckpointSaver` vs community Firestore saver — decide in Phase 0.

---

## 4. Alerts and approval **[proposed]** — owner: Sai

### 4.1 Alert (input to the graph)
```python
class Alert(BaseModel):
    alert_id: str
    source: Literal["healthcheck", "eval_harness", "manual"]
    service: str
    summary: str                 # short, written by the monitor, not the target app
    fired_at: datetime
```

### 4.2 Approval decision (value passed to `Command(resume=...)`)
```python
class ApprovalDecision(BaseModel):
    action_id: str
    decision: Literal["approve", "reject"]
    approver: str                # Slack user id, must be in APPROVER_ALLOWLIST
    decided_at: datetime
    token: str | None            # §4.3, only when approved
```

- Slack interaction requests are verified with the Slack signing secret before anything is parsed.
- An approver not in `APPROVER_ALLOWLIST` → treated as no decision; message stays pending.
- Pending approvals expire after `APPROVAL_TTL_S` (default 15 min) → `outcome="escalated"`.

Implemented in `src/approval/` (`slack_signature.py`, `messages.py`, `handler.py`):
- Signature: Slack's v0 scheme (`HMAC-SHA256(secret, "v0:" + timestamp + ":" + raw body)`), constant-time
  compare, timestamps more than 5 min from now refused; an unset secret refuses everything.
- The approval message shows LLM-written text (`rationale`, `params`) only as `plain_text`, capped, and
  never in the notification fallback. Buttons carry only `{"incident_id", "action_id"}`; any extra
  field in a returned value makes the click invalid.
- A click resumes the graph only if: signed, a `block_actions` click on our approve/reject button,
  approver allowlisted, the incident is paused on an `approval_request` interrupt, `action_id` matches
  the current proposal, and `requested_at` is within `APPROVAL_TTL_S`. Otherwise it is ignored and the
  incident stays paused. On approve, the token is minted over the action and params read from the
  checkpoint, never from the click. Check-and-resume is serialised per process.
- HTTP: `POST /slack/interactions` (`approval/web.py`) reads the raw body (≤ 64 KB, else 413), verifies
  the signature synchronously (401 + `{"error": reason}` if not from Slack), answers an empty 200, and
  runs the handler in a FastAPI background task, because Slack needs the ack within 3 s. Every signed
  request gets 200, including ignored clicks, so Slack doesn't retry; retries that do arrive are
  ignored because the incident is no longer paused. Cloud Run needs CPU always allocated
  (`--no-cpu-throttling`) for the background resume. `GET /healthz` for probes.
- Posting: `approval.slack_client.request_approval_in_slack(poster, settings, pause)` sends
  `approval_message()` to `SLACK_CHANNEL_ID` via `chat.postMessage` (`SLACK_BOT_TOKEN`); a Slack
  `{"ok": false}` reply raises `SlackApiError(error)`. The caller that runs the graph posts it when
  `ainvoke` returns `__interrupt__`.
- Not built yet: escalating on expiry *without* a click (today an expired approval just stays paused),
  updating the Slack message after a decision, and cross-instance locking.

### 4.3 Approval token
Implemented in `src/contracts/approval_token.py` (`mint`, `verify`), shared by both sides so the
signed bytes can't drift.

- Signed message: `"approval-v1" ␟ incident_id ␟ action_id ␟ action ␟ canonical_json(params) ␟ expiry`,
  where `␟` is the byte `0x1F` (can't appear in an identifier) and `canonical_json` is sorted keys,
  no spaces. The `approval-v1` prefix means this key's signatures can't be reused for anything else.
- Token string: `"<expiry_unix_seconds>.<hex HMAC-SHA256>"`. The expiry is readable but signed, so
  it can't be extended.
- `APPROVAL_SIGNING_KEY` is **hex text**; both sides turn it into bytes only with
  `Settings.approval_key_bytes()` (`bytes.fromhex`, whitespace stripped). It must decode to at least
  32 bytes (64 hex chars; `uv run python -c "import secrets; print(secrets.token_hex(32))"`); the helper and
  `mint` both refuse shorter keys. Never `.encode()` the env value directly.
- `verify` raises `ApprovalTokenError` with reason `approval_malformed`, `approval_invalid` or
  `approval_expired`; the executor puts that reason in `RejectionInfo.reason`. The signature is
  checked before the expiry, so a forged token always reports `approval_invalid`.

- Minted **only** in the Slack webhook handler (and in the eval harness's scripted approver), never
  in a node the LLM can reach.
- The executor recomputes it from the request it received. If the diagnoser changes the action or a
  single param after approval, the token no longer matches → `rejected`.
- Both services read the key from Secret Manager.

---

## 5. Eval harness **[proposed]** — owner: Om

### 5.1 Fault definition — `evals/faults/<fault_id>/fault.yaml`
```yaml
id: db-oom-01
category: resource        # resource | config | dependency | network | deploy
description: Postgres memory limit cut to 32MB; container OOM-loops
inject: inject.sh
oracle: oracle.sh         # exit 0 = healthy
reset: reset.sh
expected_fix: scale_memory_limit   # for analysis, not given to the agent
timeout_s: 600
repeat_of: null           # set to another fault id for memory-ablation pairs
```

### 5.2 Episode record — one JSON line in `evals/results/<YYYY-MM-DD>/episodes.jsonl`
```json
{"run_id": "...", "fault_id": "db-oom-01", "repeat": 2, "config": {"memory": true, "max_hypotheses": 3},
 "resolved": true, "time_to_resolve_s": 142.3, "replans": 1, "tool_calls": 14,
 "tokens_in": 38210, "tokens_out": 2120, "cost_usd": 0.041,
 "blocked_actions": 0, "outcome": "resolved", "langsmith_run_url": "..."}
```

---

## 6. Configuration (env vars, see `.env.example`)

| Var | Used by | Notes |
|---|---|---|
| `OPENAI_API_KEY`, `LLM_MODEL` | diagnoser | model name never hard-coded; key may be empty when `LLM_BASE_URL` is set (local servers) |
| `LLM_BASE_URL`, `LLM_TIMEOUT_S`, `LLM_MAX_RETRIES` | diagnoser | OpenAI-compatible endpoint (empty = OpenAI); per-call timeout (60); SDK retries for 429/5xx/network (2) |
| `EMBEDDING_MODEL` | diagnoser | |
| `EXECUTOR_URL` | diagnoser | internal IP / DNS of the VM |
| `HEALTH_URL` | diagnoser | what `check_health` curls, as seen from the VM; default `http://127.0.0.1:8088/health`; must match the executor's curl allowlist |
| `INCIDENT_API_TOKEN` | diagnoser; monitor + eval harness send it | bearer token for `/incidents` (§1.1); Secret Manager in cloud; unset = intake refuses all |
| `GCP_PROJECT`, `FIRESTORE_DATABASE` | diagnoser | |
| `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `SLACK_CHANNEL_ID` | diagnoser | |
| `APPROVER_ALLOWLIST` | diagnoser | comma-separated Slack user ids |
| `APPROVAL_SIGNING_KEY` | both | Secret Manager in cloud |
| `APPROVAL_TTL_S`, `MAX_ITERATIONS`, `MAX_REPLANS`, `MAX_TOOL_CALLS`, `MAX_TOKENS_PER_INCIDENT`, `MAX_WALL_CLOCK_S` | diagnoser | |
| `MEMORY_ENABLED`, `MAX_HYPOTHESES` | diagnoser | ablation switches |
| `ALLOWED_DIAGNOSER_SA`, `MAX_OUTPUT_BYTES` | executor | |
| `EXECUTOR_AUDIENCE`, `EXECUTOR_STATE_PATH` | executor | ID-token audience (empty → `EXECUTOR_URL`); sqlite idempotency store on a persistent disk |
| `EXECUTOR_DEV_TOKEN` | executor | local dev only, ≥ 32 chars, refused unless bound to 127.0.0.1; never set on the VM |
| `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT` | both | |
