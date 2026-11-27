#!/usr/bin/env bash
# Fault: point the api at a database host that doesn't exist, and recreate the api so it picks
# up the new environment (env_file is only read when a container is created).
set -euo pipefail
source "$(dirname "$0")/../../../target/lib.sh"
log "inject api-bad-db-url-01: DATABASE_URL host db -> db-primary"
set_kv "$RUNTIME/api.env" DATABASE_URL "postgresql://app:app-local-only@db-primary:5432/app"
tc up -d --no-deps --force-recreate api >/dev/null
