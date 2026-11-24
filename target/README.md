# Target stack

The system the incident responder keeps healthy: **proxy (nginx) → api (FastAPI) → db (Postgres) + cache (Redis)**.

```bash
./target/up.sh        # build api images (good + broken), start from known-good, wait healthy
./target/oracle.sh    # exit 0 if healthy (GET /health + POST /visits through the proxy)
./target/reset.sh     # back to known-good from any state
./target/down.sh      # stop and remove containers
source target/lib.sh && tc ps   # docker compose for this stack
```

Needs Docker with compose v2, bash and curl. Runs on WSL2 now and on the GCE VM later; the proxy
is published on `127.0.0.1:8088` only.

| Path | What |
|---|---|
| `docker-compose.yml` | the four services; every breakable value comes from `runtime/` |
| `known-good/` | committed, correct config: `stack.env`, `api.env`, `nginx.conf`, `redis.conf` |
| `runtime/` | gitignored working copy that faults edit; recreated by `reset.sh` |
| `api/` | the target API and its Dockerfile (`BROKEN=1` build arg makes the bad-release image) |
| `lib.sh` | shared helpers: `tc`, `set_kv`, `edit_in_place`, `wait_healthy` |

`known-good/` doubles as the source for the executor's `restore_config` action: restoring a file
means copying it from `known-good/` into `runtime/` (with `cp`, which keeps the inode, so
bind-mounted files update inside the container).
