# Walkthrough — the diagnostic command runner

**Built:** 2026-11-26 (Claude, cloud session, for Om) · **Branch:** `feat/executor-runner` ·
**Status:** 212 tests pass; mutation checks pass; **28 live-stack tests written but not yet run**
(they need Docker: step 5 below is yours).

---

## 1. Where this sits

```
diagnoser ──A2A──► executor
                    ├─ security.CommandValidator.check(argv)   (PR #4)  is this allowed?
                    └─ executor.CommandRunner.diagnose(req)     (this PR) run it, safely, and report
```

The validator says *whether* a command may run. The runner decides *how* it runs, and that has its
own failure modes: a command that never ends, one that prints gigabytes, a planted binary, secrets
in the output, leaked environment variables. Each is handled by one step in `diagnose()`.

**Look at:** `src/executor/runner.py`, top docstring (the six steps), then `diagnose()`.

## 2. The six steps, and the test that proves each

| Step | What | Why | Test |
|---|---|---|---|
| 1 | `validator.check(argv)`; refused → `REJECTED`, nothing runs | policy first | `test_rejected_commands_never_run` |
| 2 | find the binary only on `/usr/local/bin:/usr/bin:/bin` | an attacker who drops a fake `docker` early on `$PATH` doesn't get it run | `test_inherited_path_is_ignored` |
| 3 | `create_subprocess_exec` (argv, no shell), stdin closed, minimal env, cwd = `target/`, own process group | no shell parsing; no secrets in the child's env; `cat runtime/...` paths resolve | `test_environment_is_minimal`, `test_stdin_is_closed`, `test_cwd_is_the_target_dir` |
| 4 | stdout/stderr through `_TailBuffer` (last `MAX_OUTPUT_BYTES` only) | memory stays bounded even if a command spews; the *end* of a log is what matters | `test_output_is_capped_and_keeps_the_tail` |
| 5 | on timeout, `killpg` the whole group | the docker CLI and others start child processes; killing only the parent leaves them running | `test_timeout_kills_the_command_and_its_children` |
| 6 | `redact()` stdout/stderr | `docker inspect` prints every env var; credentials must not reach the prompt | `test_cat_runtime_file_through_real_validator_is_redacted`, `test_redaction*` |

Mutation check done in the session: breaking step 1, 2, 5 or 6 (one at a time) makes the matching test(s) fail.

### Two subtle bits

- **Non-zero exit is `COMPLETED`, not `FAILED`.** `curl -f` returning 22 because the proxy gives 502
  is exactly the evidence the agent needs. `FAILED` means the executor couldn't do its job (binary
  missing, timeout). This matches INTERFACES §2.3 and keeps the eval's numbers honest.
- **The partial first line is dropped when output is truncated.** The tail buffer can start in the
  middle of a line. If that line was `DB_PASSWORD=hunter2` and the cut left `SWORD=hunter2`, no
  redaction pattern would match it. Dropping the partial line removes that leak.
  Test: `test_partial_secret_at_the_cut_is_not_leaked`.

## 3. What redaction keeps

Redaction masks values, not structure: `postgresql://app:***@db-primary:5432/app`. The agent still
sees the wrong host `db-primary` (fault `api-bad-db-url-01`). `test_redaction_keeps_useful_evidence`
checks that normal output (`maxmemory 64mb`, image tags, error messages) passes through unchanged.
It's best-effort: the real protection is that commands are read-only and the target has no real secrets.

## 4. Why async

The A2A server (next PR) is an async web app. A blocking `subprocess.run` inside it would freeze
every other request while one `docker logs` runs. `asyncio.create_subprocess_exec` plus
`asyncio.wait_for` gives the same behaviour without blocking.

## 5. Run the live-stack test (yours, ~3 minutes)

`tests/test_runner_docker.py` runs **every command from the allowlist's `ALLOWED` list** through the
real runner against the real stack, and expects exit code 0 and no leaked DB password. That closes
the "do the real binaries accept our argv?" gap from PR #4.

```bash
cd ~/CSCI-599/Capstone_Project
git fetch origin && git switch feat/executor-runner && uv sync
./target/up.sh
RUN_DOCKER_TESTS=1 uv run pytest -m docker -v
./target/down.sh
```

Expected: `28 passed`. If one fails, paste its output: either our allowlist permits an argv the
real program rejects (fix the allowlist) or the stack isn't healthy (rerun `up.sh`).

## 6. Not done yet

- **Sandbox container** with a read-only Docker socket proxy (`DOCKER_HOST` is already passed
  through for it). The validator + runner are the first line; the sandbox is the second.
- **Remediation actions** (catalog, approval-token check, idempotency): next PR.
- **A2A server** wrapping `diagnose()` / `remediate()`: after that.

## 7. Study (links checked 2026-11-26 unless noted)

| Topic | Read | Then look at |
|---|---|---|
| asyncio subprocesses | Python docs: "Subprocesses" (asyncio) (search docs.python.org; not link-checked this session) | `diagnose()` |
| process groups and `killpg` | Python docs: `os.killpg`, `subprocess` `start_new_session` (not link-checked) | `_kill`, timeout test |
| Why not trust `$PATH` | Python docs: `shutil.which` (`path=` argument) (not link-checked) | `SAFE_PATH` |
