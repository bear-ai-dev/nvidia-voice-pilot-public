#!/bin/bash
# Report each task's seeded row counts, read from the running database.
#
# Counted from the database rather than by parsing the seed SQL, because a row
# whose values span several lines — a JSONB payload, say — is easy to miscount in
# text and impossible to miscount in a table.
#
# Writes a TSV of task, table, rows to stdout and a per-task total to stderr.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"

select_tasks() {
    if [ "$#" -eq 0 ]; then
        find "$HERE" -maxdepth 1 -mindepth 1 -type d -name '[0-9][0-9]-*' | sort
        return
    fi
    for prefix in "$@"; do
        find "$HERE" -maxdepth 1 -mindepth 1 -type d -name "${prefix}-*" | sort
    done
}

printf 'task\ttable\trows\n'

for task_dir in $(select_tasks "$@"); do
    slug=$(basename "$task_dir")
    image="voice-env-${slug}:volume"
    container="volume-${slug}"

    docker build -q -t "$image" "$task_dir/environment" > /dev/null || {
        echo "$slug: build failed" >&2; continue
    }
    docker rm -f "$container" > /dev/null 2>&1
    docker run -d --name "$container" "$image" > /dev/null
    ready=no
    for _ in $(seq 1 90); do
        docker exec "$container" test -f /tmp/task-infra/.ready 2>/dev/null && { ready=yes; break; }
        sleep 1
    done
    if [ "$ready" != yes ]; then
        echo "$slug: never became ready" >&2
        docker rm -f "$container" > /dev/null 2>&1
        docker rmi "$image" > /dev/null 2>&1
        continue
    fi

    # Count every base table in the public schema. Views are excluded: they
    # restate rows that are already counted in the tables behind them.

    # Exact counts, one query per table.
    tables=$(docker exec "$container" su postgres -c "psql -q -At -d voiceenv -c \"
        SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename;\"")
    total=0
    for table in $tables; do
        n=$(docker exec "$container" su postgres -c \
            "psql -q -At -d voiceenv -c 'SELECT count(*) FROM $table;'")
        printf '%s\t%s\t%s\n' "$slug" "$table" "$n"
        total=$((total + n))
    done
    echo "$slug: ${total} rows across $(echo "$tables" | wc -w | tr -d ' ') tables" >&2

    docker rm -f "$container" > /dev/null 2>&1
    docker rmi "$image" > /dev/null 2>&1
done
