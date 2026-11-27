#!/usr/bin/env bash
# shellcheck disable=SC2034  # variables here are used by the scripts that source this file
# Shared helpers for the target stack. Sourced (not run) by up.sh, reset.sh, oracle.sh and every
# evals/faults/*/ script, so paths and the compose invocation are defined in exactly one place.
#
#   source "$(git rev-parse --show-toplevel)/target/lib.sh"   # or a relative path
#   tc ps                                                       # docker compose for the target
#
# Works on WSL2, macOS and the GCE VM: bash + docker compose v2 + curl, nothing else.

# Directory of this file, resolved even when sourced from elsewhere.
TARGET_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KNOWN_GOOD="$TARGET_DIR/known-good"
RUNTIME="$TARGET_DIR/runtime"
TARGET_PROJECT="${TARGET_PROJECT:-ir-target}"

# `tc` = "target compose". Always the same project name, compose file and env file.
tc() {
  docker compose -p "$TARGET_PROJECT" -f "$TARGET_DIR/docker-compose.yml" --env-file "$RUNTIME/stack.env" "$@"
}

# Read one KEY=value from runtime/stack.env (e.g. PROXY_PORT). Prints the value.
stack_var() {
  grep -E "^$1=" "$RUNTIME/stack.env" | tail -n1 | cut -d= -f2-
}

# Apply a sed expression to a file IN PLACE, keeping the same inode.
#
# WHY not `sed -i`: nginx.conf and redis.conf are bind-mounted into containers as single files.
# Docker binds the file's inode, and `sed -i` writes a *new* file and renames it over the old one,
# so the container keeps seeing the old content. Truncating and rewriting the existing file keeps
# the inode, so the change is visible inside the container. (`cp` onto an existing file also keeps
# the inode, which is why reset.sh and the executor's restore_config can use plain cp.)
edit_in_place() {
  local file="$1" expr="$2" content
  content="$(sed -E "$expr" "$file")"
  printf '%s\n' "$content" >"$file"
}

# Replace KEY=... in an env-style file (or append it). Used by inject scripts.
set_kv() {
  local file="$1" key="$2" value="$3"
  if grep -qE "^${key}=" "$file"; then
    # | as the sed delimiter so values containing / (URLs) work.
    edit_in_place "$file" "s|^${key}=.*|${key}=${value}|"
  else
    printf '%s=%s\n' "$key" "$value" >>"$file"
  fi
}

# Wait until target/oracle.sh passes, up to $1 seconds. Returns 0 if healthy in time.
wait_healthy() {
  local timeout="${1:-90}" waited=0
  while ((waited < timeout)); do
    if "$TARGET_DIR/oracle.sh" >/dev/null 2>&1; then return 0; fi
    sleep 3
    waited=$((waited + 3))
  done
  return 1
}

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
