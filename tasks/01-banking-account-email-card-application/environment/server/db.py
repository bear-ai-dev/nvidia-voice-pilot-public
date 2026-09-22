"""Database access for the tool server.

One connection per request, autocommit off, so a handler that raises leaves no
partial mutation behind. Rows come back as dicts.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from functools import cache

import psycopg2
import psycopg2.extras

# Written by task-init.sh with a per-container password. Read from a root-only
# file rather than the environment, which the agent's processes inherit.
DSN_FILE = os.environ.get("TOOL_DB_DSN_FILE", "/var/lib/task-data/db_dsn")


@cache
def dsn() -> str:
    with open(DSN_FILE) as fh:
        return fh.read().strip()


@contextmanager
def transaction():
    """Yield a dict cursor inside a transaction, committing on clean exit."""
    conn = psycopg2.connect(dsn())
    try:
        conn.autocommit = False
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def one(cur, sql: str, params: tuple = ()) -> dict | None:
    cur.execute(sql, params)
    row = cur.fetchone()
    return dict(row) if row else None


def all_rows(cur, sql: str, params: tuple = ()) -> list[dict]:
    cur.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def scalar(cur, sql: str, params: tuple = ()):
    cur.execute(sql, params)
    row = cur.fetchone()
    if row is None:
        return None
    return list(row.values())[0]


def scenario_value(cur, key: str) -> str | None:
    return scalar(cur, "SELECT value FROM scenario WHERE key = %s", (key,))


def scenario_id(cur, key: str, table: str, column: str,
                owner: dict | None = None) -> str | None:
    """The scenario's seeded identifier for a new record, while it is still free.

    Seeded identifiers let a replay reproduce the recorded ones, but each names
    one record. A second create that reused it would collide with the first, or
    an upsert would silently rewrite it, so once the identifier is taken it is
    only reused when the existing row has the same `owner` values (a re-send to
    the same customer, say). Otherwise this returns None and the caller issues
    an ordinary identifier.
    """
    fixed = scenario_value(cur, key)
    if not fixed:
        return None
    row = one(cur, f"SELECT * FROM {table} WHERE {column} = %s", (fixed,))
    if row is None or (owner and all(row[k] == v for k, v in owner.items())):
        return fixed
    return None


def allocate_id(cur, entity_type: str, scope: str = "") -> str:
    """Issue the next identifier for an entity type, advancing the allocator.

    Identifiers that appear in tool results come from here so that results are
    reproducible and a second allocation cannot repeat the first.
    """
    row = one(
        cur,
        """
        UPDATE id_allocator
           SET next_value = next_value + 1
         WHERE entity_type = %s AND scope = %s
        RETURNING next_value - 1 AS issued, template
        """,
        (entity_type, scope),
    )
    if row is None:
        raise KeyError(f"no allocator for entity_type={entity_type!r} scope={scope!r}")
    return row["template"].format(n=row["issued"])


def unkeyed_snapshot_tables(snapshot_tables: list[tuple[str, str]]) -> list[str]:
    """Snapshot tables whose key columns no primary key or unique index guarantees.

    The grading layer addresses snapshot rows by these columns and digests them
    into a dict, so a key two rows can share would silently merge them, and a
    damaged row could hide behind its twin. Views carry no indexes of their own
    and are keyed on their base table's primary key, so only tables are checked.
    """
    with transaction() as cur:
        tables = {row["relname"] for row in all_rows(cur, """
            SELECT c.relname
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
             WHERE c.relkind IN ('r', 'p')
        """)}
        rows = all_rows(cur, """
            SELECT c.relname AS table_name,
                   array_agg(a.attname::text) AS columns
              FROM pg_index i
              JOIN pg_class c ON c.oid = i.indrelid
              JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
              CROSS JOIN LATERAL unnest(i.indkey) AS k(attnum)
              JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum
             WHERE i.indisunique
             GROUP BY c.relname, i.indexrelid
        """)
    unique: dict[str, list[set[str]]] = {}
    for row in rows:
        unique.setdefault(row["table_name"], []).append(set(row["columns"]))
    unkeyed = []
    for table, order_by in snapshot_tables:
        if table not in tables:
            continue
        key = {column.strip() for column in order_by.split(",")}
        if not any(columns <= key for columns in unique.get(table, [])):
            unkeyed.append(f"{table}({order_by})")
    return unkeyed


class ToolRefusal(Exception):
    """A domain precondition was not met. Surfaces as HTTP 409."""

    def __init__(self, message: str, detail: dict | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail or {}


class NotFound(Exception):
    """A referenced entity does not exist. Surfaces as HTTP 404."""
