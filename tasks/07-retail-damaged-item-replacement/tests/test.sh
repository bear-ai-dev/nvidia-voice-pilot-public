#!/bin/bash
# Grading layer. Runs as root inside the task container, after the agent.
#
# Scores what the run left behind. It does NOT reset the database and does NOT
# replay the recorded calls: an agent that reached the right outcome by a
# different route through the tools has done nothing wrong, and resetting first
# would score the environment rather than the run.
#
# For the environment's own regression test, use tests/env_check.sh.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR=${LOG_DIR:-/logs/verifier}
VERIFIER_DIR=/var/lib/task-data/verifier
TOOL_PORT=${TOOL_PORT:-8080}
BASE="http://127.0.0.1:${TOOL_PORT}"
TRANSCRIPT=${AGENT_TRANSCRIPT:-/workspace/transcript.txt}

mkdir -p "$LOG_DIR"

# A stale verdict is worse than no verdict, and a missing one is worse than a
# zero. The reward file starts at zero and is only ever raised by a grading run
# that completed, so every exit path from here leaves a well-formed reward.json.
write_zero_reward() {
    printf '{"reward": 0.0, "score": 0.0, "reward_breakdown": {"db": 0.0, "communicate": 0.0}, "state_fields_checked": 0, "state_fields_matched": 0, "damage_rows": 0, "communicate_required": 0, "communicate_met": 0}\n' \
        > "$LOG_DIR/reward.json"
}
rm -f "$LOG_DIR/reward.json"
write_zero_reward

echo "== waiting for the tool server"
ready=no
for _ in $(seq 1 60); do
    if curl -sf -o /dev/null "${BASE}/health"; then
        ready=yes
        break
    fi
    sleep 0.5
done
if [ "$ready" != yes ]; then
    echo "tool server is not answering; cannot grade" >&2
    exit 1
fi

echo "== grading the state the run left behind"
python3 "$HERE/grade.py" \
    --verifier-dir "$VERIFIER_DIR" \
    --base "$BASE" \
    --transcript "$TRANSCRIPT" \
    --out "$LOG_DIR/reward.json" \
    --report "$LOG_DIR/report.txt"
grade_status=$?

if [ ! -s "$LOG_DIR/reward.json" ]; then
    echo "grading did not produce a reward file" >&2
    write_zero_reward
    grade_status=1
fi

# Keep the run's final state alongside the verdict, so a failure can be diffed
# rather than guessed at.
/usr/local/bin/task-init.sh --dump-state > /dev/null 2>&1 || true
cp -f /out/final_state.json "$LOG_DIR/final_state.json" 2>/dev/null || true
cp -f "$TRANSCRIPT" "$LOG_DIR/transcript.txt" 2>/dev/null || true

python3 -c "import json;print('reward:', json.load(open('$LOG_DIR/reward.json'))['reward'])"

exit "$grade_status"
