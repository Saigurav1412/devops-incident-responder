#!/usr/bin/env bash
# Fault: shrink Redis memory to 1mb with noeviction, restart it, then fill it until writes fail.
set -euo pipefail
source "$(dirname "$0")/../../../target/lib.sh"
log "inject cache-maxmemory-01: maxmemory 1mb, noeviction"
edit_in_place "$RUNTIME/redis.conf" 's/^maxmemory .*/maxmemory 1mb/; s/^maxmemory-policy .*/maxmemory-policy noeviction/'
tc restart cache >/dev/null
# Wait for Redis to accept connections again before filling it.
for _ in $(seq 1 20); do
  tc exec -T cache redis-cli ping >/dev/null 2>&1 && break
  sleep 1
done
# Fill with 1KB values until Redis refuses writes. The EVAL aborts with an OOM error part-way,
# which is the point, so its exit code is ignored.
tc exec -T cache redis-cli EVAL \
  "for i=1,5000 do redis.call('SET','filler:'..i,string.rep('x',1024)) end return 1" 0 >/dev/null 2>&1 || true
