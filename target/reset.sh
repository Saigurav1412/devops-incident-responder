#!/usr/bin/env bash
# Put the target stack back to known-good, from any state. Used between eval episodes and by
# every fault's reset.sh. Idempotent: running it twice is the same as running it once.
#
# 1. copy known-good/* over runtime/ (undoes every config fault)
# 2. recreate all containers (undoes stopped/crashed/limited containers and clears Redis,
#    which has no persistence)
# 3. wait until the oracle passes
set -euo pipefail
source "$(dirname "$0")/lib.sh"

mkdir -p "$RUNTIME"
cp "$KNOWN_GOOD"/* "$RUNTIME"/

if ! docker image inspect ir-target-api:good >/dev/null 2>&1; then
  log "api images missing; run target/up.sh first"
  exit 1
fi

log "recreating target stack from known-good"
tc up -d --force-recreate --remove-orphans --wait --wait-timeout 120 >/dev/null

if wait_healthy 90; then
  log "target healthy"
else
  log "target NOT healthy after reset"
  tc ps >&2
  exit 1
fi
