# Decisions

One line per decision: what, why. These feed the "architecture and design decisions" slides.
Newest at the bottom. Move an item from **Open** to **Decided** when both agree.

## Decided

| Date | Decision | Why |
|---|---|---|
| 2026-11-22 | App: DevOps incident responder | Every rubric item fits the problem itself: sandboxing is required anyway, log lines are a realistic injection surface, and success is checkable by a health-check script |
| 2026-11-22 | Features: planning + self-correction, terminal control (sandboxed), HITL approval, long-term memory, A2A | Four connected features plus A2A meets the two-person scope bar; each one is exercised in every incident |
| 2026-11-22 | No hybrid cloud-local routing | Too close to Om's A3 (originality cap risk); small local models aren't reliable enough at tool calls to drive a shell |
| 2026-11-22 | A2A sits on the privilege boundary: read-only diagnoser on Cloud Run, executor on a GCE VM is the only thing that changes the target | A prompt-injected diagnoser still can't do damage; A2A is there for a reason, not a checkbox |
| 2026-11-22 | Topology B on GCP: diagnoser Cloud Run + Firestore; executor + target stack on a GCE VM, no external IP, reached via Direct VPC egress | Cloud Run has no Docker daemon, so the target stack can't live there; internal-only VM keeps the executor off the internet |
| 2026-11-22 | Approval surface: Slack | Reachable by a non-developer; not the Telegram surface Om used in A3 |
| 2026-11-22 | Eval: custom fault-injection harness, 20 faults × 3 runs, plus ablations; LangSmith traces as evidence | No named benchmark fits incident response; ablations show each feature changes the result |
| 2026-11-22 | `AGENTS.md` is the canonical agent context; `CLAUDE.md` only imports it | One file any coding agent reads |
| 2026-11-22 | Status tracked on the GitHub Project board, not a `STATUS.md` | A file both people edit every session causes daily merge conflicts |
| 2026-11-22 | Handoff protocol (`docs/HANDOFF.md`, `/handoff`): durable knowledge goes into contracts/decisions/tests first; one note file per session with a required "NOT verified" section; readers verify notes against code | In A1/A2, summaries (and a memory-service bridge) silently dropped details that later caused bugs; the repo is also the only channel both teammates' agents can read |
| 2026-11-26 | Diagnoser graph additions to INTERFACES §1: `verify --still open--> diagnose`, explicit `escalate` and `budget_exceeded` end nodes, and a hard 500-step LangGraph `recursion_limit` behind the budget caps | One diagnostic is rarely enough to decide; LangGraph's default limit (10,007) is no backstop if a bug ever bypasses the caps. Agreed by Om on PR #2 |
| 2026-11-26 | Budget caps are not checked between approval and `execute_fix`; the eval records `approval_wait_s` and reports time-to-resolve with and without it | `MAX_WALL_CLOCK_S` (600) < `APPROVAL_TTL_S` (900): counting the human's wait would discard approved fixes. Agreed by Om on PR #2 |
| 2026-11-26 | `check_health` = the graph's own fixed `curl -fsS --max-time 5 <HEALTH_URL>` through the normal `diagnose` skill; healthy = completed with exit 0. `oracle.sh` stays the eval's grader only | No new skill or contract; the LLM can't influence the check; keeping the agent's check separate from the grader lets the eval measure false "resolved" claims. Agreed by Om on PR #2 |
| 2026-11-26 | `verify_hypothesis` applies only `status`/`confidence` changes to existing hypothesis ids; refuted never reverts; unknown ids, rewording and omissions are ignored. The approval token is redacted from state after `execute_fix` | The LLM (or an injected log line) must not be able to undo self-correction; the token shouldn't ride along in later checkpoints, traces and prompts. Raised by Om on PR #2 |
| 2026-11-26 | An approved fix the executor didn't apply (rejected, failed, unreachable, non-zero exit) escalates: no health check, no refutation, no memory record | A healthy check after a refused fix would otherwise read as "resolved" and save a verified record for a fix that never ran (found running the diagnoser against the PR #12 server). Replanning would likely re-propose the same fix and cost another approval click. Proposed by Sai on PR #7; Om reviews |

## Open (resolve in Phase 0)

| Question | Options | Leaning |
|---|---|---|
| Remediation: typed action catalog vs. free-form commands behind an allowlist | catalog (safer, easier to verify) / free-form (stronger "terminal control" story) | catalog for remediation, free-form-but-validated argv for diagnostics |
| Firestore checkpointer | custom `BaseCheckpointSaver` / community package | try community package, fall back to custom |
| LLM | own OpenAI key / ChatGPT Edu OAuth (no API key) / Gemini free tier | confirm on Piazza whether A3's "no NIM" guidance still applies |
| Target stack contents | app + Postgres + Redis + nginx / something smaller | smallest stack that supports 20 distinct faults |
| GCP credits | current coupon / request extra | estimate VM hours first; stop the VM when idle |
| Escalating approvals that expire with no click (INTERFACES §4.2 → `outcome="escalated"`) (Sai, 2026-11-26) | Cloud Scheduler job that resumes expired incidents / in-process sweeper task / check on the next alert | Cloud Scheduler (survives instance restarts; Cloud Run may scale to zero, killing an in-process timer). Today the TTL is enforced only at click time via `requested_at`. Blocked on GCP being back on |
| How the diagnoser finishes work after Slack's 3-second ack (Sai, 2026-11-26) | FastAPI background task in the same instance + Cloud Run `--no-cpu-throttling` / push the click to Cloud Tasks and resume in a separate request / keep CPU throttling and hope | Background task + `--no-cpu-throttling` (implemented in `approval/web.py`; simplest, no extra service). Cost: the instance is billed for CPU while it's up, so set `min-instances=0`. Revisit Cloud Tasks if resumes get lost on scale-down. Om: OK given the credit budget? |
| Auth for the diagnoser's `/incidents` API (Sai, 2026-11-26) | shared bearer token (`INCIDENT_API_TOKEN`) / Google-signed ID token for an allowed caller service account / Cloud Run IAM (impossible: Slack's webhook needs the service public) | Shared bearer token, implemented (constant-time compare, fails closed when unset); simplest for the eval harness and local runs. Move to ID tokens if a caller runs on GCP. Om: does the eval harness / monitor have somewhere safe to keep the token? |
