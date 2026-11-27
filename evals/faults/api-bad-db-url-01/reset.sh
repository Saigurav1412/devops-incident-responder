#!/usr/bin/env bash
# Undo this fault: put the whole target stack back to known-good. See target/reset.sh.
exec "$(dirname "$0")/../../../target/reset.sh"
