#!/bin/bash
# Conformance layer. Runs as root inside the task container.
#
# Asks whether this backend is a faithful stand-in for the tools the recording
# was made against: rebuild the database, replay the recorded call sequence, and
# require every response to match the recorded one byte for byte. A divergence is
# a defect here, not in the recording.
#
# This is a regression test on the environment. It does not score an agent, and
# it deliberately destroys whatever state a run left behind. To grade a run, use
# tests/test.sh instead.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR=${LOG_DIR:-/logs/verifier}
VERIFIER_DIR=/var/lib/task-data/verifier
TOOL_PORT=${TOOL_PORT:-8080}
BASE="http://127.0.0.1:${TOOL_PORT}"

mkdir -p "$LOG_DIR"
printf '{"conformant": false, "calls_total": 0, "calls_matched": 0}\n' \
    > "$LOG_DIR/conformance.json"

echo "== resetting database to the recorded initial state"
/usr/local/bin/task-init.sh --reset-db || {
    echo "database reset failed" >&2
    exit 1
}

echo "== waiting for the tool server"
for _ in $(seq 1 60); do
    curl -sf -o /dev/null "${BASE}/health" && break
    sleep 0.5
done

echo "== replaying the recorded call sequence"
python3 "$HERE/replay.py" \
    --gold "$VERIFIER_DIR/gold_calls.json" \
    --base "$BASE" \
    --report "$LOG_DIR/replay_report.json"
replay_status=$?

if [ ! -s "$LOG_DIR/replay_report.json" ]; then
    echo "replay produced no report; scoring cannot proceed" >&2
    exit 1
fi

echo "== scoring conformance"
python3 "$HERE/score_conformance.py" \
    --replay-report "$LOG_DIR/replay_report.json" \
    --expected "$VERIFIER_DIR/expected_final_state.json" \
    --base "$BASE" \
    --out "$LOG_DIR/conformance.json" \
    --report "$LOG_DIR/conformance.txt"
score_status=$?

if [ "$replay_status" -ne 0 ] || [ "$score_status" -ne 0 ]; then
    exit 1
fi
exit 0
