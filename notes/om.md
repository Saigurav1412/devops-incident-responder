# Om — process notes (feeds my PROCESS_LOG.md)

## 2026-11-22/23 — Phase 0
- Planning in Claude chat (claude.ai Project): picked the incident responder idea, topology B, Slack approvals, custom eval harness. Phase plan, interface contracts, AGENTS.md and the handoff protocol were drafted by Claude; I reviewed and pushed them myself (PR #1).
- A2A v1.0 check (Claude, cloud session): read the v1.0 "what's new" doc, installed a2a-sdk 1.3.1 and inspected its types. Found my draft used v0.3 names (`message/send`, lowercase states, `kind` on parts). INTERFACES §2 rewritten; committed to PR #1.
- Repo skeleton + CI (Claude, cloud session, branch `chore/skeleton-ci`): pyproject/uv.lock, `src/contracts` (models, approval token, A2A codec), config, JSON logging, 42 tests, GitHub Actions. All AI-generated.
  - Verified: `uv run pytest` 42 passed, `ruff check` + `ruff format --check` clean, wheel builds with all packages (checked in the session). CI result: see PR.
  - Went wrong: `ruff format` also reformatted Python code blocks inside the markdown docs; reverted and excluded `*.md` from ruff.
  - Still to do myself: read docs/walkthroughs/phase0-skeleton.md, run the tests locally, try the "break it on purpose" exercise.
- Target stack + 5 faults (Claude, cloud session, branch `feat/target-stack`): compose stack (nginx, FastAPI api, Postgres, Redis), known-good/runtime config split, lib/up/reset/oracle/down scripts, 5 faults across dependency/config/resource/deploy, fault-catalog tests, CI shellcheck + compose validation. All AI-generated.
  - Verified in session: shellcheck clean, `docker compose config` valid, 50 tests pass, API returns 503 with short errors when deps are down and compact `{"status":"ok"...}` when up (TestClient with stubbed checks), broken image exits 1.
  - NOT verified: real container run (Docker Hub blocked in the cloud session). I need to run docs/walkthroughs/phase1-target-stack.md §6 in WSL.
  - Caught during build: redis.conf doesn't allow end-of-line comments; `sed -i` would break single-file bind mounts (added `edit_in_place`).
