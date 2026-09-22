#!/bin/bash
# Build, boot, and verify every task in this directory, across both layers.
#
# For each task:
#   conformance  reset the database, replay the recorded calls, require every
#                response byte-exact. Asks whether the environment is faithful.
#   oracle       reset, run solution/solve.sh as the agent account, then grade.
#                Must be 1.0. Asks whether a correct run scores.
#   idle         reset, grade a container nothing has touched. Must be 0.0.
#                Asks whether an incorrect run fails. This is the control the
#                earlier single-layer verifier could not express: it replayed the
#                gold calls itself, so it scored 1.0 with no agent present.
#
# Containers and images are removed as soon as a task is scored, because ten
# PostgreSQL images at once do not fit on a typical working disk.
#
# Usage:
#   ./verify_all.sh              every task
#   ./verify_all.sh 05 09        only the named tasks, by number prefix
#
# Exit status is 0 only when every task is conformant, scores 1.0 on its oracle,
# and scores 0.0 when idle.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
RESULTS="$HERE/.verify-results.tsv"
KEEP=${KEEP_IMAGES:-0}

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

field_of() {
    docker exec "$1" python3 -c \
        "import json;print(json.load(open('/logs/verifier/reward.json'))['$2'])" \
        2>/dev/null || echo "-"
}

printf 'task\tconformance\tcalls\toracle\tfacts\tsaid\tdamage\tidle\n' > "$RESULTS"
overall=0

# A grader fixed in some tasks and not others would score them differently.
"$HERE/check_shared_files.sh" || overall=1

for task_dir in $(select_tasks "$@"); do
    slug=$(basename "$task_dir")
    image="voice-env-${slug}:verify"
    container="verify-${slug}"
    printf '\n======== %s ========\n' "$slug"

    if [ -f "$task_dir/environment/gen_seed.py" ]; then
        ( cd "$task_dir" && python3 environment/gen_seed.py ) || {
            printf '%s\tseed-failed\t-\t-\t-\t-\t-\t-\n' "$slug" >> "$RESULTS"
            overall=1; continue
        }
    fi

    if ! docker build -q -t "$image" "$task_dir/environment" > /dev/null; then
        echo "build failed" >&2
        printf '%s\tbuild-failed\t-\t-\t-\t-\t-\t-\n' "$slug" >> "$RESULTS"
        overall=1; continue
    fi

    docker rm -f "$container" > /dev/null 2>&1
    docker run -d --name "$container" "$image" > /dev/null

    ready=no
    for _ in $(seq 1 90); do
        if docker exec "$container" test -f /tmp/task-infra/.ready 2>/dev/null; then
            ready=yes; break
        fi
        sleep 1
    done
    if [ "$ready" != yes ]; then
        echo "container never became ready; init log follows" >&2
        docker logs "$container" 2>&1 | tail -30 >&2
        docker rm -f "$container" > /dev/null 2>&1
        printf '%s\tnever-ready\t-\t-\t-\t-\t-\t-\n' "$slug" >> "$RESULTS"
        overall=1; continue
    fi

    # tests/ and solution/ are delivered by the harness at run time rather than
    # baked into the image, so they are copied in here the same way.
    docker cp "$task_dir/tests" "$container:/opt/tests" > /dev/null
    docker cp "$task_dir/solution" "$container:/opt/solution" > /dev/null 2>&1 || true

    echo "-- conformance"
    if docker exec "$container" bash /opt/tests/env_check.sh > /dev/null 2>&1; then
        conformance=ok
    else
        conformance=FAIL; overall=1
        docker exec "$container" bash /opt/tests/env_check.sh 2>&1 | tail -20 >&2
    fi
    calls=$(docker exec "$container" python3 -c \
        "import json;r=json.load(open('/logs/verifier/conformance.json'));print(f\"{r['calls_matched']}/{r['calls_total']}\")" \
        2>/dev/null || echo "-")

    echo "-- oracle"
    docker exec "$container" /usr/local/bin/task-init.sh --reset-db > /dev/null 2>&1
    docker exec -u agent "$container" bash /opt/solution/solve.sh > /dev/null 2>&1
    docker exec "$container" bash /opt/tests/test.sh > /dev/null 2>&1
    oracle=$(reward_of "$container")
    facts="$(field_of "$container" state_fields_matched)/$(field_of "$container" state_fields_checked)"
    said="$(field_of "$container" communicate_met)/$(field_of "$container" communicate_required)"
    damage=$(field_of "$container" damage_rows)
    [ "$oracle" = "1.0" ] || overall=1

    echo "-- idle"
    docker exec "$container" /usr/local/bin/task-init.sh --reset-db > /dev/null 2>&1
    docker exec "$container" bash /opt/tests/test.sh > /dev/null 2>&1
    idle=$(reward_of "$container")
    [ "$idle" = "0.0" ] || overall=1

    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$slug" "$conformance" "$calls" "$oracle" "$facts" "$said" "$damage" "$idle" \
        >> "$RESULTS"
    printf '   conformance %s (%s), oracle %s, facts %s, said %s, damage %s, idle %s\n' \
        "$conformance" "$calls" "$oracle" "$facts" "$said" "$damage" "$idle"

    docker rm -f "$container" > /dev/null 2>&1
    [ "$KEEP" = "1" ] || docker rmi "$image" > /dev/null 2>&1
done

printf '\n======== summary ========\n'
column -t -s $'\t' "$RESULTS"
printf '\nconformance: recorded calls reproduced byte-exact\n'
printf 'oracle: solve.sh graded, must be 1.0    idle: nothing ran, must be 0.0\n'
exit "$overall"
