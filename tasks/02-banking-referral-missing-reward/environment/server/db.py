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


class ToolRefusal(Exception):
    """A domain precondition was not met. Surfaces as HTTP 409."""

    def __init__(self, message: str, detail: dict | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail or {}


class NotFound(Exception):
    """A referenced entity does not exist. Surfaces as HTTP 404."""
