# Phase Plan — Incident Responder (CSCI 599 A4)

**Team:** Om (Legion / WSL2), Sai (MacBook Air M4)
**Target submit:** Fri Dec 5, 2026 · **Hard deadline:** Tue Dec 9, 2026 (no grace days left for Om)
**Rule for every phase:** a phase is done only when its exit criteria pass *on GCP*, not just on a laptop.

Ownership is by vertical slice. Each owner builds, tests and documents their slice end to end;
the other person reviews every PR into `main`, so both can explain the whole system in the video
and in their own PROCESS_LOG.

| Slice | Owner | Folders |
|---|---|---|
| Executor agent (A2A server, sandbox, action catalog), GCE VM, target stack, fault injector | Om | `src/executor/`, `src/security/`, `deploy/vm/`, `target/` |
| Eval harness, fault catalog, ablations, adversarial runs | Om | `evals/` |
| Diagnoser agent (LangGraph graph, planning + self-correction) | Sai | `src/diagnoser/`, `src/prompts/` |
| Long-term memory + Firestore checkpointer | Sai | `src/memory/` |
| Slack approval surface, Cloud Run deploy | Sai | `src/approval/`, `deploy/cloudrun/` |
| Shared contracts, config, logging | Both (PR + review from the other) | `src/contracts/`, `src/config/`, `src/obs/` |

---

## Phase 0 — Contracts and skeleton · Nov 23–24 · together

Goal: both of you can work in parallel for four days without blocking each other.

- [ ] Repo created, `main` protected, PRs need one review
- [ ] `.gitattributes` (`* text=auto eol=lf`), `.gitignore`, `.env.example`, `.python-version` (3.12), `uv` lockfile
- [ ] `AGENTS.md` + `CLAUDE.md` (one-line import) committed
- [ ] `docs/INTERFACES.md` reviewed and agreed by both; the Pydantic models in `src/contracts/` written from it
- [ ] GitHub Actions: `uv sync && pytest` on every PR
- [ ] GCP: project, Firestore (Native mode), Artifact Registry, two service accounts (`diagnoser-sa`, `executor-sa`), budget alert. Credit request sent if needed.
- [ ] Hello-world Cloud Run service deployed (proves the deploy path on day 1)
- [ ] GitHub Project board with one issue per feature, guardrail, eval item and deliverable
- [ ] Open decisions in `docs/DECISIONS.md` (section "Open") resolved

**Exit:** contracts merged; `pytest` green in CI; hello-world reachable on Cloud Run.

## Phase 1 — Both agents standing, talking over A2A · Nov 25–28

Thanksgiving is Thu Nov 26. Plan for 2.5 working days, not 4.

**Om**
- [ ] GCE VM (e2-standard-2 or similar, no external IP) with Docker; target stack runs there (stack written: `target/`, runs locally first; GCP on hold until hosting account is decided)
- [x] Executor A2A server: Agent Card (skill descriptions = policy summaries), two skills, task lifecycle, ID-token auth (`src/executor/server.py`; local smoke test + Sai's client in a scratch merge; on the VM: pending GCP)
- [x] Diagnostic command validator (allowlist parser, no shell) + unit tests (`src/security/`, PR #4)
- [x] Diagnostic runner: fixed PATH, minimal env, timeout kills process group, output cap, redaction (`src/executor/runner.py`; live-stack test 2026-11-26 WSL2: all 28 allowed commands exit 0, no secret leaked)
- [ ] Sandbox container + read-only Docker socket proxy for the runner
- [x] Remediation actions with approval-token verification, param policy, idempotency + unit tests (`src/executor/remediation.py`; 4 of 6 catalog actions enabled, `set_env`/`prune_path` deliberately off; live test 2026-11-26 WSL2: each of the 5 faults repaired by its expected fix, retries don't rerun)
- [x] Fault injector: first 5 faults with `inject.sh` / `oracle.sh` / `reset.sh` (first local run 2026-11-26, WSL2 Docker Engine: all 5 break the oracle, reset restores health in 15–21 s)

**Sai**
- [ ] Diagnoser LangGraph graph: nodes from INTERFACES §1, A2A client to the executor
- [ ] Firestore checkpointer: state survives a Cloud Run redeploy mid-incident
- [ ] Slack app: approval message, signed interaction webhook, `Command(resume=...)` back into the graph
- [ ] Cloud Run service with Direct VPC egress to the VM's internal IP

**Exit:** one seeded fault goes alert → diagnosis → Slack approval → executor fix → oracle passes, on GCP.

## Phase 2 — Depth: memory, self-correction, eval · Nov 29 – Dec 1

**Om**
- [ ] Fault catalog to 20 (spread across categories in INTERFACES §5)
- [x] `evals/harness.py`: reset → inject (self-confirmed) → run → oracle → record JSONL; scripted approver; noop + scripted baselines (live 2026-11-26, WSL2: noop 0/5, scripted 15/15, 0 invalid, TTR p50 4.2 s / p95 5.94 s)
- [x] `--agent diagnoser-scripted`: harness drives Sai's real service + graph + signed Slack click + A2A → executor (`evals/diagnoser_agent.py`); live 2026-11-26 WSL2: 5/5, 15/15, memory=false 5/5 resolved by the oracle, BUT the agent itself escalated 10/25 (api-bad-db-url, db-stopped) and replanned needlessly on 2 more faults: `check_health` curls once, ~20 ms after the fix. Fixed by #16 (+ #17, stale config copy): live 2026-11-27 run 15/15, gave up but healed 0, 0 replans, 1 fix per incident, TTR p50 3.37 s / p95 5.18 s
- [x] Ablation switches wired for the diagnoser agent: `--config memory=false`, `--config max_hypotheses=N` (live: memory=false run 5/5)
- [ ] First full 20 × 1 run; triage failures with Sai

**Sai**
- [ ] Incident memory: write-after-verified-resolution, vector recall (INTERFACES §3)
- [ ] Self-correction: hypothesis verification node, replan on mismatch, bounded loop
- [ ] Health re-check after remediation → replan or escalate on failure
- [ ] Budget caps (iterations, tool calls, tokens, wall-clock) enforced in graph

**Exit:** 20 × 1 run completes unattended; resolution rate recorded; each feature visible in a LangSmith trace.

## Phase 3 — Attacks, full eval, freeze · Dec 2–3 · together

- [ ] ≥3 live prompt-injection attacks (malicious log line, poisoned memory entry, fake error / config comment), each run against the deployed system, trace saved to `evals/adversarial/`
- [ ] Fixes or documented "known limitation" for each
- [ ] Full eval: 20 faults × 3 runs + ablations (memory off, single hypothesis)
- [ ] Failure-mode list from the runs
- [ ] Cost numbers: tokens/incident from traces, VM + Cloud Run + Firestore per week
- [ ] **Code freeze end of Dec 3.** After this, only bug fixes needed for the video.

**Exit:** all numbers for the report exist in `evals/results/<date>/`.

## Phase 4 — Deliverables · Dec 4–5 · together

- [ ] README: setup, run, deploy, architecture diagram, **Capability-to-Code Map with line numbers (after freeze)**
- [ ] Evaluation report (metrics with n and date, attacks, failure modes, cost)
- [ ] Slides (≥10, all five topics, plain wording)
- [ ] Demo video 5–10 min: every claimed feature triggers, a guardrail engages, a redeploy shows memory surviving
- [ ] Om's `PROCESS_LOG.md` and Sai's `PROCESS_LOG.md` (200–300 words each, individual)
- [ ] Submit Dec 5

## Cut list (in order) if Phase 1 slips

1. Fault catalog 20 → 12 (still × 3 runs)
2. Remediation catalog trimmed to the actions the faults need
3. Drop the p95 latency metric (keep p50)

Never cut: ablations, the 3 live attacks, the approval-token check, the redeploy-survives demo.
