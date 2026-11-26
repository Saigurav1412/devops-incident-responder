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
- 2026-11-26 — Sai's PR #2 review (Claude, cloud session): added `Settings.approval_key_bytes()` (hex decode, ≥32 bytes, whitespace stripped, error never echoes the value), shared `MIN_KEY_BYTES`, 10 tests, `.env.example` generation comment, INTERFACES §4.3 line.
  - Verified: 52 tests pass with `-W error`, ruff clean; mutation check (swapping hex decode for `.encode()`) makes 6 tests fail.
- 2026-11-26 — First real run of the target stack + 5 faults (me, WSL2, Docker Engine): `up.sh` pulled images and came up healthy in ~80 s; every fault made the oracle fail (503 for db-stopped / bad-db-url / cache-maxmemory, 502 for bad-upstream / bad-image) and `reset.sh` restored health in 15–21 s each. Clears the "never run against real containers" item from the 11-23 handoff.
  - Open: cache-maxmemory's inject was still restarting Redis when the check ran, so the 503 could partly be the restart, not the OOM. Inject scripts should verify their own fault (e.g. a SET returns OOM) before the harness starts timing.
