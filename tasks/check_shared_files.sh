#!/bin/bash
# Keep the files every task carries a copy of byte-identical across tasks.
#
# Each task directory has to be self-contained, because the harness builds and
# grades one task at a time, so the grader, the state comparison and the generic
# server plumbing are copied into all ten. A fix applied to nine of them grades
# the tenth differently without anyone noticing. This fails when any copy drifts.
#
#   ./check_shared_files.sh            report drift, exit 1 if any
#   ./check_shared_files.sh --sync 01  copy task 01's versions over every other task
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"

SHARED=(
    tests/grade.py
    tests/statecheck.py
    tests/replay.py
    tests/make_digest.py
    tests/score_conformance.py
    tests/env_check.sh
    tests/test.sh
    environment/task-init.sh
    environment/server/db.py
    environment/server/projection.py
    environment/server/schema.py
)

tasks=$(find "$HERE" -maxdepth 1 -mindepth 1 -type d -name '[0-9][0-9]-*' | sort)

if [ "${1:-}" = "--sync" ]; then
    source=$(find "$HERE" -maxdepth 1 -mindepth 1 -type d -name "${2:?task prefix}-*")
    [ -d "$source" ] || { echo "no task ${2}" >&2; exit 1; }
    for task in $tasks; do
        [ "$task" = "$source" ] && continue
        for file in "${SHARED[@]}"; do
            cp -p "$source/$file" "$task/$file"
        done
    done
    echo "synced ${#SHARED[@]} shared file(s) from $(basename "$source")"
fi

drift=0
for file in "${SHARED[@]}"; do
    variants=$(for task in $tasks; do shasum -a 256 "$task/$file" | cut -c1-64; done | sort -u | wc -l)
    if [ "$variants" -ne 1 ]; then
        echo "DRIFT  $file has $variants different versions across tasks"
        drift=1
    fi
done
[ "$drift" -eq 0 ] && echo "shared files: all ${#SHARED[@]} identical across tasks"
exit "$drift"
