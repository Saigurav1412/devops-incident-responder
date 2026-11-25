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

## Open (resolve in Phase 0)

| Question | Options | Leaning |
|---|---|---|
| Remediation: typed action catalog vs. free-form commands behind an allowlist | catalog (safer, easier to verify) / free-form (stronger "terminal control" story) | catalog for remediation, free-form-but-validated argv for diagnostics |
| Firestore checkpointer | custom `BaseCheckpointSaver` / community package | try community package, fall back to custom |
| LLM | own OpenAI key / ChatGPT Edu OAuth (no API key) / Gemini free tier | confirm on Piazza whether A3's "no NIM" guidance still applies |
| Target stack contents | app + Postgres + Redis + nginx / something smaller | smallest stack that supports 20 distinct faults |
| GCP credits | current coupon / request extra | estimate VM hours first; stop the VM when idle |
| Diagnoser graph additions to INTERFACES §1 (Sai, 2026-11-25) | accept / change | `verify --still open--> diagnose` (one diagnostic is rarely enough; caps bound it), explicit `escalate` and `budget_exceeded` end nodes, and a hard 500-step LangGraph `recursion_limit` behind the budget caps (LangGraph's default is 10,007, i.e. no backstop). Implemented in `feat/diagnoser-skeleton`; needs Om's OK |
| Wall-clock cap vs. waiting for approval: `MAX_WALL_CLOCK_S` (600) < `APPROVAL_TTL_S` (900) | don't count the approval wait / raise `MAX_WALL_CLOCK_S` above the TTL / keep both and accept that slow approvals end as `budget_exceeded` | don't check caps between approval and `execute_fix`, so an approved fix is never discarded (implemented). Om: does the eval's time-to-resolve need the wait excluded too? |
| How `check_health` decides the fix worked | executor runs a fixed health command via `diagnose` (e.g. curl the nginx `/health`) / new executor skill wrapping `target/oracle.sh` / something else | undecided; graph currently takes a pluggable `HealthCheck` (scripted in tests). Needs Om, since the oracle lives on the VM |
