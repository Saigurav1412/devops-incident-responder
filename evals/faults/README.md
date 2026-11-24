# Fault catalog

Each folder is one seeded fault for the eval harness (format: `docs/INTERFACES.md` §5.1).

```
<fault-id>/
  fault.yaml   id, category, description, expected_fix (for analysis only, never shown to the agent)
  inject.sh    breaks the target stack in one specific way
  oracle.sh    exit 0 = healthy (calls target/oracle.sh: /health + a write through the proxy)
  reset.sh     back to known-good (calls target/reset.sh)
```

| Fault | Category | What breaks | Looks like | Expected fix |
|---|---|---|---|---|
| `db-stopped-01` | dependency | Postgres container stopped | 503, db connection error | `restart_service db` |
| `proxy-bad-upstream-01` | config | nginx upstream port 8000 → 8001 | 502 at the proxy, api fine | `restore_config proxy nginx.conf` |
| `api-bad-db-url-01` | config | DATABASE_URL host → `db-primary` | 503, name-resolution error, db container fine | `restore_config api api.env` |
| `cache-maxmemory-01` | resource | Redis 1mb + noeviction, filled | 503 on cache writes, Redis still answers PING | `restore_config cache redis.conf` |
| `api-bad-image-01` | deploy | api switched to `broken` image tag | 502, api restart loop, startup error in logs | `rollback_image api good` |

Several faults are built to have a tempting wrong answer (restart the api, restart the db), so
the eval measures diagnosis, not just "restart whatever is red".

## Run one by hand

```bash
./target/up.sh                                    # once: build images, start, wait healthy
./evals/faults/db-stopped-01/inject.sh
./evals/faults/db-stopped-01/oracle.sh; echo $?   # expect non-zero
./evals/faults/db-stopped-01/reset.sh             # back to healthy
./evals/faults/db-stopped-01/oracle.sh; echo $?   # expect 0
```

## Add a fault

1. New folder named exactly like its `id`.
2. `fault.yaml` with a `category` and an `expected_fix` from `contracts.catalog.REMEDIATION_ACTIONS`.
3. `inject.sh` that sources `target/lib.sh` and edits **runtime** files only (never `known-good/`).
   Use `edit_in_place` / `set_kv`, not `sed -i` (bind-mounted files would not see the change).
4. Copy `oracle.sh` and `reset.sh` from another fault. `chmod +x *.sh`.
5. `uv run pytest tests/test_faults.py` checks the structure; then run it by hand as above.
