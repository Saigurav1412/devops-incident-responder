#!/usr/bin/env bash
# Fault: stop the database container (as if it was stopped by hand or by the host).
set -euo pipefail
source "$(dirname "$0")/../../../target/lib.sh"
log "inject db-stopped-01: stopping db"
tc stop db >/dev/null
