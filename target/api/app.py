"""Target API: the service the incident responder keeps healthy.

WHAT: two endpoints.
  GET  /health   checks Postgres (SELECT 1) and Redis (a real SET with a short TTL).
                 200 {"status": "ok", ...} when both work, 503 with per-dependency errors otherwise.
  POST /visits   writes a row to Postgres and increments a Redis counter: a realistic request that
                 touches both dependencies (the oracle calls it as a write round-trip).

WHY the health check does a SET, not just PING: Redis still answers PING when it is out of memory
with `noeviction`, but rejects writes. A PING-only check would report "healthy" during the
cache-maxmemory fault, and the oracle would lie.

WHY errors are short and sanitized: the agent reads these responses (through the proxy and in
`docker logs`). They should carry the error *type* and a short message, never connection strings.

BROKEN_BUILD: the `broken` image tag is built with BROKEN=1 (see Dockerfile). It exits at startup,
which is how the deploy fault (bad release) is simulated.
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager

import psycopg
import redis
from fastapi import FastAPI
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("target-api")

if os.environ.get("BROKEN_BUILD") == "1":
    # Simulates a bad release: the process dies before serving anything, so Docker restarts it
    # in a loop and the proxy returns 502.
    log.error("startup failed: missing required module 'billing_v2' (release 2026.11.23-rc1)")
    sys.exit(1)

DATABASE_URL = os.environ["DATABASE_URL"]
REDIS_URL = os.environ["REDIS_URL"]


def _short_error(exc: Exception) -> str:
    # First line only, capped. Drops multi-line driver output and keeps logs readable.
    return f"{type(exc).__name__}: {str(exc).splitlines()[0][:200] if str(exc) else ''}"


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Create the table once. If the DB is down at startup the app still starts, and /health
    # reports the problem: a dependency outage should look like a failing check, not a crash.
    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=3) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS visits (id serial PRIMARY KEY, at timestamptz DEFAULT now())"
            )
    except Exception as exc:
        log.warning("db init skipped: %s", _short_error(exc))
    yield


app = FastAPI(lifespan=lifespan)
cache = redis.Redis.from_url(REDIS_URL, socket_timeout=2, socket_connect_timeout=2)


def _check_db() -> str | None:
    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
        return None
    except Exception as exc:
        return _short_error(exc)


def _check_cache() -> str | None:
    try:
        cache.set("healthcheck", "1", ex=30)
        return None
    except Exception as exc:
        return _short_error(exc)


@app.get("/health")
def health():
    db_err, cache_err = _check_db(), _check_cache()
    body = {
        "status": "ok" if not (db_err or cache_err) else "degraded",
        "db": db_err or "ok",
        "cache": cache_err or "ok",
    }
    if db_err or cache_err:
        log.error("health check failed db=%s cache=%s", body["db"], body["cache"])
        return JSONResponse(body, status_code=503)
    return body


@app.post("/visits")
def visit():
    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=3) as conn:
            conn.execute("INSERT INTO visits DEFAULT VALUES")
        count = cache.incr("visits")
    except Exception as exc:
        log.error("visit failed: %s", _short_error(exc))
        return JSONResponse({"error": _short_error(exc)}, status_code=503)
    return {"visits": count}
