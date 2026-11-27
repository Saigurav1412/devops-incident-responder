#!/usr/bin/env bash
# Fault: change nginx's upstream port to one nothing listens on, then reload nginx.
set -euo pipefail
source "$(dirname "$0")/../../../target/lib.sh"
log "inject proxy-bad-upstream-01: upstream api:8000 -> api:8001"
edit_in_place "$RUNTIME/nginx.conf" 's/server api:8000;/server api:8001;/'
grep -q 'server api:8001;' "$RUNTIME/nginx.conf" || { log "edit did not apply"; exit 1; }
tc exec -T proxy nginx -s reload >/dev/null
