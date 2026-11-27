#!/usr/bin/env bash
# Grader for this fault: the shared end-to-end check (exit 0 = healthy). See target/oracle.sh.
exec "$(dirname "$0")/../../../target/oracle.sh"
