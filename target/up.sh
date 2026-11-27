#!/usr/bin/env bash
# Build the api images and start the target stack from known-good config.
# First run pulls nginx/postgres/redis/python images from Docker Hub (a few minutes).
#
#   ./target/up.sh          # build + start + wait for healthy
set -euo pipefail
source "$(dirname "$0")/lib.sh"

log "building ir-target-api:good and ir-target-api:broken"
docker build -q -t ir-target-api:good "$TARGET_DIR/api" >/dev/null
docker build -q -t ir-target-api:broken --build-arg BROKEN=1 "$TARGET_DIR/api" >/dev/null

"$TARGET_DIR/reset.sh"
