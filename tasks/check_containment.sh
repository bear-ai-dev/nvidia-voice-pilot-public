#!/bin/bash
# Verify, against the built image rather than against the Dockerfile, that an
# agent running in a task container cannot reach the answers.
#
# Checked per task:
#   1. The agent account cannot list or read /var/lib/task-data.
#   2. The agent account cannot read the admin token.
#   3. The admin plane refuses a wrong bearer token.
#   4. The population generator is absent from the image.
#   5. No annotated transcript reached the agent-visible workspace.
#   6. The agent account cannot connect to PostgreSQL directly, over TCP or the
#      socket, as the app role or the superuser. The grader scores end state, so
#      a direct write would be indistinguishable from doing the work.
#   7. The agent CAN reach the tool server, which is the whole interface.
#
# A COPY line saying mode 0700 proves nothing on its own; a base image that
# already created the parent, or a later layer that widened it, would not show up
# in a diff. So this asks the running container.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
failures=0

select_tasks() {
    if [ "$#" -eq 0 ]; then
        find "$HERE" -maxdepth 1 -mindepth 1 -type d -name '[0-9][0-9]-*' | sort
        return
    fi
    for prefix in "$@"; do
        find "$HERE" -maxdepth 1 -mindepth 1 -type d -name "${prefix}-*" | sort
    done
}

# Reports pass when the command FAILS, which is what a denied read looks like.
expect_denied() {
    local container=$1 label=$2 command=$3
    if docker exec -u agent "$container" sh -c "$command" > /dev/null 2>&1; then
        printf '    FAIL  %s: succeeded but should have been denied\n' "$label"
        failures=$((failures + 1))
    else
        printf '    ok    %s: denied\n' "$label"
    fi
}

expect_ok() {
    local container=$1 label=$2 command=$3
    if docker exec -u agent "$container" sh -c "$command" > /dev/null 2>&1; then
        printf '    ok    %s\n' "$label"
    else
        printf '    FAIL  %s: should have succeeded\n' "$label"
        failures=$((failures + 1))
    fi
}

for task_dir in $(select_tasks "$@"); do
    slug=$(basename "$task_dir")
    image="voice-env-${slug}:containment"
    container="containment-${slug}"
    printf '\n%s\n' "$slug"

    docker build -q -t "$image" "$task_dir/environment" > /dev/null || {
        printf '    FAIL  image did not build\n'; failures=$((failures + 1)); continue
    }
    docker rm -f "$container" > /dev/null 2>&1
    docker run -d --name "$container" "$image" > /dev/null
    for _ in $(seq 1 90); do
        docker exec "$container" test -f /tmp/task-infra/.ready 2>/dev/null && break
        sleep 1
    done

    expect_denied "$container" "list /var/lib/task-data" "ls /var/lib/task-data"
    expect_denied "$container" "read gold_calls.json" \
        "cat /var/lib/task-data/verifier/gold_calls.json"
    expect_denied "$container" "read expected_final_state.json" \
        "cat /var/lib/task-data/verifier/expected_final_state.json"
    expect_denied "$container" "read admin token" "cat /var/lib/task-data/admin_token"
    expect_denied "$container" "read the database DSN" "cat /var/lib/task-data/db_dsn"
    expect_denied "$container" "no database password in the environment" \
        "env | grep -qi -e password -e TOOL_DB_DSN"
    for role in voiceenv postgres agent; do
        expect_denied "$container" "psql over TCP as $role" \
            "psql -h 127.0.0.1 -U $role -d voiceenv -w -c 'SELECT 1'"
        expect_denied "$container" "psql over the socket as $role" \
            "psql -U $role -d voiceenv -w -c 'SELECT 1'"
    done
    expect_denied "$container" "admin plane with a wrong token" \
        "curl -sf -H 'Authorization: Bearer wrong' http://127.0.0.1:8080/_admin/state"
    expect_denied "$container" "admin plane with no token" \
        "curl -sf http://127.0.0.1:8080/_admin/state"

    # The generator that produced the population must not ship.
    if docker exec "$container" sh -c 'find / -name gen_seed.py -not -path "/proc/*" 2>/dev/null | grep -q .'; then
        printf '    FAIL  gen_seed.py is present in the image\n'
        failures=$((failures + 1))
    else
        printf '    ok    gen_seed.py absent from the image\n'
    fi

    # The annotated transcript contains the answers and must not be readable.
    if docker exec "$container" sh -c 'find / -name "annotated-transcript*" -not -path "/proc/*" 2>/dev/null | grep -q .'; then
        printf '    FAIL  an annotated transcript is present in the image\n'
        failures=$((failures + 1))
    else
        printf '    ok    no annotated transcript in the image\n'
    fi

    expect_ok "$container" "agent reaches the tool server" \
        "curl -sf http://127.0.0.1:8080/health"
    expect_ok "$container" "agent reads the governing policy" "cat /workspace/policy.md"

    docker rm -f "$container" > /dev/null 2>&1
    docker rmi "$image" > /dev/null 2>&1
done

printf '\n'
if [ "$failures" -eq 0 ]; then
    echo "containment: all checks passed"
else
    echo "containment: ${failures} check(s) failed"
fi
exit "$failures"
