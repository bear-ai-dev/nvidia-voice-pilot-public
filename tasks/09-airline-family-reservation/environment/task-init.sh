#!/bin/bash
# Build the world, then idle.
#
# Creates the PostgreSQL cluster, applies the schema and the four seed files in
# order, starts the tool server, waits for it to answer, and touches the readiness
# marker the harness healthcheck tests.
#
# Subcommands:
#   --wait          idle after startup (the default; what the harness uses)
#   --reset-db      rebuild the database from SQL without restarting the server
#   --dump-state    write final_state.json and a pg_dump to the output directory
set -euo pipefail

READY_DIR=/tmp/task-infra
SQL_DIR=/opt/sql
SERVER_DIR=/opt/tool-server
TOOL_PORT=${TOOL_PORT:-8080}
OUT_DIR=${STATE_OUT_DIR:-/out}
# Separate from READY_DIR because pg_ctl writes here as the postgres user, while
# the readiness marker stays under /tmp where the harness healthcheck looks.
LOG_DIR=/var/log/task-infra
# The tool server's connection string, password included. Root-only, beside the
# admin token, so the agent account can reach the database only through the tools.
DSN_FILE=/var/lib/task-data/db_dsn

apply_sql() {
    # Applied in filename order: 001 schema, 002 catalogs, 003 population,
    # 004 the entities this conversation touches.
    for file in "$SQL_DIR"/[0-9][0-9][0-9]_*.sql; do
        echo "  applying $(basename "$file")"
        su postgres -c "psql --quiet --no-psqlrc -v ON_ERROR_STOP=1 \
            -d ${POSTGRES_DB} -f $file" > /dev/null
    done
}

build_database() {
    echo "building database ${POSTGRES_DB}"
    su postgres -c "psql --quiet --no-psqlrc -v ON_ERROR_STOP=1 -d postgres -c \
        \"DROP DATABASE IF EXISTS ${POSTGRES_DB};\"" > /dev/null
    su postgres -c "psql --quiet --no-psqlrc -v ON_ERROR_STOP=1 -d postgres -c \
        \"CREATE DATABASE ${POSTGRES_DB} OWNER ${POSTGRES_USER};\"" > /dev/null
    apply_sql
    su postgres -c "psql --quiet --no-psqlrc -v ON_ERROR_STOP=1 -d ${POSTGRES_DB} -c \
        \"GRANT ALL ON ALL TABLES IN SCHEMA public TO ${POSTGRES_USER}; \
          GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO ${POSTGRES_USER};\"" > /dev/null
}

start_postgres() {
    # Idempotent: the verifier calls --reset-db inside an already-running
    # container, where the cluster is up and starting it again would fail.
    if su postgres -c "pg_isready -q -h 127.0.0.1" 2>/dev/null; then
        return 0
    fi

    mkdir -p "$PGDATA"
    chown -R postgres:postgres "$PGDATA"
    chmod 0700 "$PGDATA"

    if [ ! -s "$PGDATA/PG_VERSION" ]; then
        echo "initialising cluster"
        # Peer auth on the socket lets only the postgres OS user in as the
        # superuser; TCP needs a password the agent account cannot read. The
        # agent runs in this container, so trust auth would hand it the database.
        su postgres -c "initdb --username=postgres --auth-local=peer \
            --auth-host=scram-sha-256 --encoding=UTF8 -D $PGDATA" > "$LOG_DIR/initdb.log" 2>&1
        # Loopback only. Nothing outside the container talks to the database
        # directly; the tools are the interface.
        echo "listen_addresses = '127.0.0.1'" >> "$PGDATA/postgresql.conf"
        echo "fsync = off" >> "$PGDATA/postgresql.conf"
    fi

    su postgres -c "pg_ctl -D $PGDATA -l $LOG_DIR/postgres.log -w -t 60 start" \
        > /dev/null

    su postgres -c "psql --quiet --no-psqlrc -d postgres -tAc \
        \"SELECT 1 FROM pg_roles WHERE rolname='${POSTGRES_USER}'\"" \
        | grep -q 1 || su postgres -c "psql --quiet --no-psqlrc -d postgres -c \
            \"CREATE ROLE ${POSTGRES_USER} LOGIN;\"" > /dev/null

    # A fresh password per container, sent over stdin so it never appears in a
    # process listing, and kept only in the root-only DSN file.
    if [ ! -s "$DSN_FILE" ]; then
        local password
        password=$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')
        printf "ALTER ROLE %s PASSWORD '%s';\n" "$POSTGRES_USER" "$password" \
            | su postgres -c "psql --quiet --no-psqlrc -v ON_ERROR_STOP=1 -d postgres" > /dev/null
        ( umask 077 && printf 'host=127.0.0.1 port=5432 dbname=%s user=%s password=%s\n' \
            "$POSTGRES_DB" "$POSTGRES_USER" "$password" > "$DSN_FILE" )
    fi
}

start_tool_server() {
    echo "starting tool server on 127.0.0.1:${TOOL_PORT}"
    ( cd "$SERVER_DIR" && exec python3 app.py --host 127.0.0.1 --port "$TOOL_PORT" ) \
        > "$LOG_DIR/tool-server.log" 2>&1 &
    echo $! > "$READY_DIR/tool-server.pid"

    for _ in $(seq 1 60); do
        if curl -sf -o /dev/null "http://127.0.0.1:${TOOL_PORT}/health"; then
            return 0
        fi
        sleep 0.5
    done
    echo "tool server failed to become healthy; log follows" >&2
    cat "$LOG_DIR/tool-server.log" >&2 || true
    return 1
}

dump_state() {
    mkdir -p "$OUT_DIR"
    local token
    token=$(cat /var/lib/task-data/admin_token)
    curl -sf -X POST -H "Authorization: Bearer ${token}" \
        "http://127.0.0.1:${TOOL_PORT}/_admin/snapshot" > /dev/null
    su postgres -c "pg_dump --data-only --column-inserts -d ${POSTGRES_DB}" \
        > "$OUT_DIR/final_state.sql"
    echo "wrote $OUT_DIR/final_state.json and $OUT_DIR/final_state.sql"
}

mkdir -p "$READY_DIR" "$LOG_DIR"
chown postgres:postgres "$LOG_DIR"
chmod 0755 "$LOG_DIR"
rm -f "$READY_DIR/.ready"

# With no network namespace of its own the container hostname resolves nowhere,
# and sudo prints a resolver warning over the top of every command.
if ! getent hosts "$(hostname)" > /dev/null 2>&1; then
    printf '127.0.1.1\t%s\n' "$(hostname)" >> /etc/hosts 2>/dev/null || true
fi

case "${1:-}" in
    --reset-db)
        start_postgres
        build_database
        echo "database rebuilt"
        exit 0
        ;;
    --dump-state)
        dump_state
        exit 0
        ;;
esac

start_postgres
build_database
start_tool_server
touch "$READY_DIR/.ready"
echo "environment ready"

if [ "${1:-}" = "--wait" ]; then
    exec tail -f /dev/null
fi

if [ "$#" -gt 0 ]; then
    exec "$@"
fi
