#!/usr/bin/env bash
# The success check for every fault: exit 0 = healthy, non-zero = broken.
#
# Healthy means, through the proxy (the path a real user takes):
#   1. GET  /health  returns 200 with "status": "ok"  (Postgres SELECT 1 + Redis SET both work)
#   2. POST /visits  returns 200                      (a real write touching both dependencies)
#
# It deliberately checks behaviour from the outside, not container states: "all containers are
# running" can be true while users get 502s, and the eval must measure what users see.
# Never shown to the agent (it is the grader, not a tool).
set -uo pipefail
source "$(dirname "$0")/lib.sh"

port="$(stack_var PROXY_PORT)"
base="http://127.0.0.1:${port:-8088}"

body="$(curl -fsS --max-time 5 "$base/health")" || { echo "health request failed"; exit 1; }
grep -q '"status":"ok"' <<<"$body" || { echo "health not ok: $body"; exit 1; }
curl -fsS --max-time 5 -X POST "$base/visits" >/dev/null || { echo "write round-trip failed"; exit 1; }
echo "ok"
