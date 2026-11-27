#!/usr/bin/env bash
# Stop and remove the target stack's containers and network (images are kept).
set -euo pipefail
source "$(dirname "$0")/lib.sh"
[[ -f "$RUNTIME/stack.env" ]] || { mkdir -p "$RUNTIME"; cp "$KNOWN_GOOD"/* "$RUNTIME"/; }
tc down --remove-orphans
