# Walkthrough — target stack + first 5 faults

**Built:** 2026-11-23 (Claude, cloud session, for Om) · **Branch:** `feat/target-stack` · **Status:**
static checks pass (shellcheck, compose config, 50 tests, API failure-path test); **not yet run
against real containers** (the cloud session can't pull from Docker Hub). First real run is yours,
step 6 below.

---

## 1. What the target is and why it looks like this

```
you / oracle ──► 127.0.0.1:8088 ──► proxy (nginx) ──► api (FastAPI :8000) ──► db (Postgres)
                                                                          └──► cache (Redis)
```

It's the "production system" the agent repairs. Four services is the smallest setup where
**different faults produce similar symptoms**: a stopped db, a wrong DATABASE_URL and a full Redis
all show up as "503 from /health". The agent has to run diagnostics to tell them apart, which is
exactly what the self-correction feature needs (first hypothesis is often wrong).

**Look at:** `target/docker-compose.yml` header comment, then `target/api/app.py`.

Why `/health` does a Redis **SET** and not just PING: Redis answers PING even when it's out of memory
with `noeviction`; only writes fail. A PING-only check would say "healthy" during
`cache-maxmemory-01`. The grader has to measure what users experience.

## 2. known-good vs runtime: how faults and fixes work

Every breakable value lives in a file:

| File | Read by | Faults that edit it |
|---|---|---|
| `stack.env` | compose (`--env-file`): image tag, memory limits, port | `api-bad-image-01` |
| `api.env` | api container (`env_file`) | `api-bad-db-url-01` |
| `nginx.conf` | proxy (bind mount) | `proxy-bad-upstream-01` |
| `redis.conf` | cache (bind mount) | `cache-maxmemory-01` |

`target/known-good/` is committed and correct. `target/runtime/` is a gitignored working copy that
compose actually uses. Faults only ever edit `runtime/`. `reset.sh` copies known-good back and
recreates everything.

This gives the executor a clean, safe `restore_config` action later: "copy this one file from
known-good to runtime and restart/reload that service". The agent never writes config content
itself, so a prompt-injected agent can't write a malicious nginx.conf.

## 3. The bind-mount gotcha (`edit_in_place`)

**Look at:** `target/lib.sh`, `edit_in_place`.

`nginx.conf` and `redis.conf` are mounted as single files. Docker binds the file's **inode**.
`sed -i` writes a new file and renames it over the old one: new inode, so the container keeps
seeing the old config and the fault silently does nothing. `edit_in_place` rewrites the existing
file instead (same inode). `cp` onto an existing file also keeps the inode, which is why `reset.sh`
and `restore_config` can use `cp`. If a new fault "doesn't break anything", check this first.

## 4. The five faults

**Look at:** `evals/faults/README.md` (table), then each `inject.sh`.

| Fault | Tempting wrong fix | Right fix |
|---|---|---|
| `db-stopped-01` | restart api | `restart_service db` |
| `proxy-bad-upstream-01` | restart api (502 "looks like" api is down) | `restore_config proxy nginx.conf` |
| `api-bad-db-url-01` | restart db (error mentions db) | `restore_config api api.env` |
| `cache-maxmemory-01` | nothing (Redis container is "healthy") | `restore_config cache redis.conf` |
| `api-bad-image-01` | restart api (it's already restarting in a loop) | `rollback_image api good` |

The "tempting wrong fix" column is deliberate: the eval should reward diagnosis, not "restart
whatever is red". Categories so far: dependency, config ×2, resource, deploy. Network is missing;
add it in the next batch (e.g. api detached from the compose network).

Each fault's `oracle.sh` and `reset.sh` just call the shared `target/oracle.sh` / `target/reset.sh`.
They're separate files so a future fault can have its own check (e.g. "a specific row exists").

## 5. What's checked automatically now

- `tests/test_faults.py`: every `fault.yaml` parses into `contracts.FaultSpec`, the folder name equals
  the id, `expected_fix` is a real catalog action (`contracts.catalog.REMEDIATION_ACTIONS`), and the
  scripts exist and are executable.
- CI: `shellcheck` on every script, `docker compose config --quiet` on the compose file.
- Done once in the session (not in CI): the API returns 503 with short error text when Postgres and
  Redis are unreachable, returns compact JSON `{"status":"ok",...}` when both checks pass (so the
  oracle's grep matches), and the `BROKEN_BUILD=1` image exits with code 1.

## 6. First real run (do this in WSL, ~10 minutes the first time)

```bash
cd ~/CSCI-599/Capstone_Project
git fetch origin && git switch feat/target-stack
./target/up.sh                     # pulls images, builds api:good + api:broken, waits healthy
./target/oracle.sh                 # expect "ok"

for f in db-stopped-01 proxy-bad-upstream-01 api-bad-db-url-01 cache-maxmemory-01 api-bad-image-01; do
  echo "== $f"
  ./evals/faults/$f/inject.sh
  sleep 5
  ./evals/faults/$f/oracle.sh && echo "!! fault did NOT break the stack" || echo "broken as expected"
  ./evals/faults/$f/reset.sh && ./evals/faults/$f/oracle.sh
done
./target/down.sh
```

Each fault should print "broken as expected", then "target healthy" and "ok" after reset.
If one says "did NOT break the stack", that's a real bug: tell me the fault id and I'll fix it.
Also look at the symptoms once (`curl -s 127.0.0.1:8088/health`, `source target/lib.sh; tc ps`,
`tc logs api --tail 20`), since that's what the agent will see.

## 7. Study (links checked 2026-11-23 unless noted)

| Topic | Read | Then look at |
|---|---|---|
| Compose files, env files, healthchecks, `depends_on` conditions | Docker docs: "Compose file reference" (search for it on docs.docker.com; not link-checked from this session) | `target/docker-compose.yml` |
| uv in Docker / lockfiles (for the executor image later) | [Using uv in Docker](https://docs.astral.sh/uv/guides/integration/docker/) | — |
| Redis `maxmemory` and eviction policies | Redis docs: "Key eviction" (not link-checked from this session) | `target/known-good/redis.conf`, `cache-maxmemory-01` |
| nginx upstreams and reload | nginx docs: `ngx_http_upstream_module` (not link-checked from this session) | `target/known-good/nginx.conf` |
