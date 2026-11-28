#!/usr/bin/env bash
# Fault: stop the database container (as if it was stopped by hand or by the host).
set -euo pipefail
source "$(dirname "$0")/../../../target/lib.sh"
log "inject db-stopped-01: stopping db"
tc stop db >/dev/null

db_is_stopped() { [ "$(docker inspect -f '{{.State.Running}}' "$(service_container db)")" = "false" ]; }
confirm 20 "db container is not running" db_is_stopped
