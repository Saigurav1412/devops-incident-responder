#!/usr/bin/env bash
# Fault: point the api at a database host that doesn't exist, and recreate the api so it picks
# up the new environment (env_file is only read when a container is created).
set -euo pipefail
source "$(dirname "$0")/../../../target/lib.sh"
log "inject api-bad-db-url-01: DATABASE_URL host db -> db-primary"
set_kv "$RUNTIME/api.env" DATABASE_URL "postgresql://app:app-local-only@db-primary:5432/app"
tc up -d --no-deps --force-recreate api >/dev/null

# The api is up (503 from the app itself, not 502 from nginx) and reports the db check as failing.
api_reports_db_error() {
  local body
  [ "$(health_code)" = "503" ] || return 1
  body="$(health_body)"
  ! grep -q '"db":"ok"' <<<"$body"
}
confirm 30 "api up, /health 503 with a db error" api_reports_db_error
