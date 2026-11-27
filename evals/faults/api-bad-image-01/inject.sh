#!/usr/bin/env bash
# Fault: "deploy" the broken api image (built by target/up.sh) and recreate the api with it.
set -euo pipefail
source "$(dirname "$0")/../../../target/lib.sh"
docker image inspect ir-target-api:broken >/dev/null 2>&1 || { log "ir-target-api:broken missing; run target/up.sh"; exit 1; }
log "inject api-bad-image-01: API_IMAGE_TAG good -> broken"
set_kv "$RUNTIME/stack.env" API_IMAGE_TAG broken
tc up -d --no-deps api >/dev/null
