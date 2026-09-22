#!/bin/bash
# Confirm that reading costs an agent nothing.
#
# The grading layer is supposed to score outcomes, not routes. The sharpest
# generic test of that is whether looking at things repeatedly can lower a score:
# an agent that re-reads an order, checks a balance twice, or verifies stock
# before quoting it has done nothing wrong, and several of these domains
# deliberately mutate rows on read (progressive section views, read cursors,
# notification progression), so "reads are free" is a property that has to be
# demonstrated rather than assumed.
#
# Per task: run the oracle, confirm 1.0, then re-issue every read call the gold
# path made three more times with the same arguments, and require the reward to
# still be 1.0. Which tools are reads comes from the server itself via
# /_admin/schema, so this needs no per-task knowledge.
#
# Usage:
#   ./check_read_freedom.sh              every task
#   ./check_read_freedom.sh 06 07        only the named tasks, by number prefix
#
# Exit status is 0 only when every task scores 1.0 both before and after.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPEATS=${REPEATS:-3}
overall=0
results=$'task\tbefore\tgold_reads\textra_calls\tafter\tverdict\n'

select_tasks() {
    if [ "$#" -eq 0 ]; then
        find "$HERE" -maxdepth 1 -mindepth 1 -type d -name '[0-9][0-9]-*' | sort
        return
    fi
    for prefix in "$@"; do
        find "$HERE" -maxdepth 1 -mindepth 1 -type d -name "${prefix}-*" | sort
    done
}

reward_of() {
    docker exec "$1" python3 -c \
        "import json;print(json.load(open('/logs/verifier/reward.json'))['reward'])" \
        2>/dev/null || echo "-"
}

# Re-issues the gold sequence's read-only calls, REPEATS times over. Runs as root
# because the gold call arguments live under verifier-data, which the agent
# account deliberately cannot read; the tool server does not care who calls it,
# and what is being tested is the effect of the reads, not who made them.
read_flood() {
    # -i matters: without it docker exec does not forward stdin, so the heredoc
    # below is discarded, `python3 -` reads EOF, runs an empty program and exits 0
    # silently. That issues no calls at all and leaves the reward untouched, which
    # this control would then report as a pass. The caller checks the issued count
    # for exactly that reason.
    docker exec -i -e REPEATS="$REPEATS" "$1" python3 - <<'PY'
import json, os, urllib.error, urllib.request

BASE = os.environ.get("TOOL_SERVER_URL", "http://127.0.0.1:8080")
repeats = int(os.environ.get("REPEATS", "3"))

with open("/var/lib/task-data/admin_token") as fh:
    token = fh.read().strip()
schema_req = urllib.request.Request(
    f"{BASE}/_admin/schema", headers={"Authorization": f"Bearer {token}"}
)
with urllib.request.urlopen(schema_req, timeout=30) as fh:
    writes = set(json.load(fh)["write_tools"])

with open("/var/lib/task-data/verifier/gold_calls.json") as fh:
    gold = json.load(fh)["calls"]
reads = [c for c in gold if c["name"] not in writes]

issued = 0
for _ in range(repeats):
    for call in reads:
        request = urllib.request.Request(
            f"{BASE}/tools/{call['name']}",
            data=json.dumps(call["arguments"]).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=30).read()
        except urllib.error.HTTPError:
            pass  # a refusal is a legitimate answer; it still changed nothing
        issued += 1

print(f"{len(reads)} {issued}")
PY
}

for task_dir in $(select_tasks "$@"); do
    slug=$(basename "$task_dir")
    image="voice-env-${slug}:readfree"
    container="readfree-${slug}"
    printf '\n======== %s ========\n' "$slug"

    [ -f "$task_dir/environment/gen_seed.py" ] &&
        ( cd "$task_dir" && python3 environment/gen_seed.py > /dev/null )

    if ! docker build -q -t "$image" "$task_dir/environment" > /dev/null; then
        results+="${slug}"$'\tbuild-failed\t-\t-\t-\tFAIL\n'
        overall=1; continue
    fi

    docker rm -f "$container" > /dev/null 2>&1
    docker run -d --name "$container" "$image" > /dev/null
    ready=no
    for _ in $(seq 1 90); do
        docker exec "$container" test -f /tmp/task-infra/.ready 2>/dev/null &&
            { ready=yes; break; }
        sleep 1
    done
    if [ "$ready" != yes ]; then
        results+="${slug}"$'\tnever-ready\t-\t-\t-\tFAIL\n'
        docker rm -f "$container" > /dev/null 2>&1
        overall=1; continue
    fi

    docker cp "$task_dir/tests" "$container:/opt/tests" > /dev/null
    docker cp "$task_dir/solution" "$container:/opt/solution" > /dev/null

    docker exec -u agent "$container" bash /opt/solution/solve.sh > /dev/null 2>&1
    docker exec "$container" bash /opt/tests/test.sh > /dev/null 2>&1
    before=$(reward_of "$container")

    read -r gold_reads extra <<< "$(read_flood "$container" 2>/dev/null | tail -1)"
    docker exec "$container" bash /opt/tests/test.sh > /dev/null 2>&1
    after=$(reward_of "$container")

    # A flood that issued nothing proves nothing, so it is a failure rather than a
    # pass. Reads are free is only demonstrated by reads actually having happened.
    if [ -z "${extra:-}" ] || [ "${extra:-0}" -lt 1 ] 2>/dev/null; then
        verdict=FAIL; overall=1
        echo "   the read flood issued no calls; this control would be vacuous" >&2
    elif [ "$before" = "1.0" ] && [ "$after" = "1.0" ]; then
        verdict=PASS
    else
        verdict=FAIL; overall=1
        docker exec "$container" cat /logs/verifier/report.txt 2>/dev/null | tail -20 >&2
    fi
    results+="${slug}"$'\t'"${before}"$'\t'"${gold_reads:--}"$'\t'"${extra:--}"$'\t'"${after}"$'\t'"${verdict}"$'\n'
    printf '   before %s, %s extra read calls, after %s -> %s\n' \
        "$before" "${extra:--}" "$after" "$verdict"

    docker rm -f "$container" > /dev/null 2>&1
    docker rmi "$image" > /dev/null 2>&1
done

printf '\n======== read freedom ========\n'
printf '%s' "$results" | column -t -s $'\t'
printf '\nEach task ran its oracle, then re-issued the gold path read calls %sx.\n' "$REPEATS"
printf 'A PASS needs 1.0 both before and after: looking costs nothing.\n'
exit "$overall"
