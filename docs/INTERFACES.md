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
    budget: Budget               # counters: iterations, tool_calls, tokens, started_at
```

**Nodes (fixed names, used in traces and the code map):**
`ingest_alert → recall_memory → plan_hypotheses → diagnose → verify_hypothesis`
`verify_hypothesis --refuted--> plan_hypotheses` (replan, bounded by `MAX_REPLANS`)
`verify_hypothesis --confirmed--> propose_fix → request_approval (interrupt) → execute_fix → check_health`
`check_health --fail--> plan_hypotheses` · `check_health --pass--> save_memory → END`
`verify_hypothesis --still open--> diagnose` (gather more evidence; bounded by the caps below)
`request_approval --reject--> END` (`outcome="rejected"`)
Out of replans → `escalate → END` (`outcome="escalated"`). Any cap hit → `budget_exceeded → END`.

Implemented in `src/diagnoser/` (graph.py wires these names; nodes.py has the routers).
`incident_id` is the LangGraph `thread_id`. Approval pauses with `interrupt()`; the interrupt value is
`{"kind": "approval_request", "incident_id", "proposed": ProposedAction as JSON}` and the graph resumes
with `Command(resume=ApprovalDecision)`. A decision for a different `action_id`, from an approver not in
`APPROVER_ALLOWLIST`, or an approve without a token is ignored and the graph keeps waiting. The
diagnoser never mints tokens. Budget caps are not checked between approval and `execute_fix`, so time
spent waiting for a human never discards an approved fix.

**Budget caps (enforced in code, not the prompt):** `MAX_ITERATIONS`, `MAX_TOOL_CALLS`,
`MAX_TOKENS_PER_INCIDENT`, `MAX_WALL_CLOCK_S`. Hitting any cap ends with `outcome="budget_exceeded"`
and a Slack notice.

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

### 2.4 Rules the executor enforces

**Diagnostics (`diagnose`):**
- argv only, executed with `subprocess.run(..., shell=False)` inside a sandbox container with the
  Docker socket proxied read-only and the target's config mounted read-only
- binary allowlist + per-binary argument rules in `src/security/allowlist.yaml`
  (e.g. `docker` only with `ps | logs | inspect | stats --no-stream`; `curl` only to target-stack hosts)
- any shell metacharacter, path outside the allowed roots, or unknown flag → `rejected`

**Remediation (`remediate`) — catalog only, never free-form:** **[proposed list]**

| Action | Params | Rollback |
|---|---|---|
| `restart_service` | `service` | — |
| `restore_config` | `service`, `file` (from known-good snapshot) | `restore_config` to previous |
| `set_env` | `service`, `key`, `value` (key allowlist) | `set_env` previous value |
| `scale_memory_limit` | `service`, `mb` (bounded range) | previous limit |
| `rollback_image` | `service`, `tag` (from a pinned list) | previous tag |
| `prune_path` | `path` (under an allowlisted dir) | — |

Each action validates its own params with a Pydantic model; anything else → `rejected`.

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
  32 bytes (64 hex chars; `python -c "import secrets; print(secrets.token_hex(32))"`); the helper and
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
| `OPENAI_API_KEY`, `LLM_MODEL` | diagnoser | model name never hard-coded |
| `EMBEDDING_MODEL` | diagnoser | |
| `EXECUTOR_URL` | diagnoser | internal IP / DNS of the VM |
| `GCP_PROJECT`, `FIRESTORE_DATABASE` | diagnoser | |
| `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `SLACK_CHANNEL_ID` | diagnoser | |
| `APPROVER_ALLOWLIST` | diagnoser | comma-separated Slack user ids |
| `APPROVAL_SIGNING_KEY` | both | Secret Manager in cloud |
| `APPROVAL_TTL_S`, `MAX_ITERATIONS`, `MAX_REPLANS`, `MAX_TOOL_CALLS`, `MAX_TOKENS_PER_INCIDENT`, `MAX_WALL_CLOCK_S` | diagnoser | |
| `MEMORY_ENABLED`, `MAX_HYPOTHESES` | diagnoser | ablation switches |
| `ALLOWED_DIAGNOSER_SA`, `MAX_OUTPUT_BYTES` | executor | |
| `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT` | both | |
