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
| 2026-11-26 | Diagnostics: free-form argv, validated by a default-deny grammar (`src/security/allowlist.py`); remediation: typed catalog only (§2.4) | Diagnosis needs flexible read-only commands (the "terminal control" feature); changes need a fixed, approvable shape. Proposed by Om in PR (security-allowlist); Sai reviews |

## Open (resolve in Phase 0)

| Question | Options | Leaning |
|---|---|---|
| Firestore checkpointer | custom `BaseCheckpointSaver` / community package | try community package, fall back to custom |
| LLM | own OpenAI key / ChatGPT Edu OAuth (no API key) / Gemini free tier | confirm on Piazza whether A3's "no NIM" guidance still applies |
| Target stack contents | app + Postgres + Redis + nginx / something smaller | smallest stack that supports 20 distinct faults |
| GCP credits | current coupon / request extra | estimate VM hours first; stop the VM when idle |
