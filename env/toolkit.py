"""Building blocks for the domain toolkits in domains/<domain>/tools.py.

Each conversation's backend is a JSON document shaped {table: {row_key: row}}.
A row key is the row's key columns joined with "|", in the order the domain's
KEY_COLUMNS lists them, so a composite key such as (entity_type, scope) is
addressed as "identity_verification|". Values are plain JSON: numerics are
numbers, timestamps are ISO 8601 strings, and a missing value is null.

A tool is a function (db, args) -> result that may mutate db in place. It
signals a domain refusal by raising Refusal and an unknown reference by raising
NotFound; the runtime turns both into the error payload the recorded backend
returned and rolls the database back, so a failed call leaves nothing behind.
"""
from __future__ import annotations

import contextlib
import contextvars
import random
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Iterable, Iterator


class ToolError(Exception):
    status = 500
    kind = "handler_error"

    def __init__(self, message: str, detail: dict | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail or {}


class Refusal(ToolError):
    """A domain precondition was not met."""
    status = 409
    kind = "refused"


class NotFound(ToolError):
    """A referenced entity does not exist."""
    status = 404
    kind = "not_found"


# -- rows ---------------------------------------------------------------------

def row_key(row: dict, columns: list[str]) -> str:
    return "|".join(str(row[column]) for column in columns)


def insert(db: dict, table: str, row: dict, key_columns: dict[str, list[str]]) -> dict:
    """Add a row, refusing to overwrite one with the same key as a primary key would."""
    key = row_key(row, key_columns[table])
    if key in db[table]:
        raise ValueError(f"duplicate key {key!r} in {table}")
    db[table][key] = row
    return row


def rows(db: dict, table: str, **where) -> list[dict]:
    """Rows of a table whose columns equal the given values, in key order.

    Prefer this (and first) over iterating a table by hand: the runtime records
    the filter and the rows it matched, which is what a trace shows as a query.
    """
    matches = [
        (key, row) for key, row in sorted(dict.items(db[table]))
        if all(row.get(column) == value for column, value in where.items())
    ]
    note = getattr(db[table], "note_query", None)
    if note:
        note(where, [key for key, _ in matches])
    return [row for _, row in matches]


def first(db: dict, table: str, **where) -> dict | None:
    matches = rows(db, table, **where)
    return matches[0] if matches else None


# -- the call clock and identifier generation ---------------------------------
# The runtime sets both for the duration of each tool call. A tool asks what time
# it is with now(), and for a fresh identifier with new_id(); neither is stored in
# the database, the way a real service reads its clock and its id generator
# rather than a table.

_clock: contextvars.ContextVar = contextvars.ContextVar("clock", default=None)
_ids: contextvars.ContextVar = contextvars.ContextVar("ids", default=None)


def call_started(db: dict) -> datetime:
    """When the call began, in the scenario's own UTC offset."""
    started = scenario_value(db, "call_started_at") or scenario_value(db, "scenario_time")
    return datetime.fromisoformat(started)


def now(db: dict) -> datetime:
    """The current time in the call, in the scenario's own UTC offset."""
    current = _clock.get()
    return call_started(db) if current is None else current


def now_iso(db: dict) -> str:
    return now(db).isoformat(timespec="seconds")


def new_id(db: dict, kind: str, fallback: Callable[[], str] | None = None) -> str:
    """A fresh identifier of the given kind from the environment's generator.

    The generator is seeded per conversation, so a replay issues the same
    identifiers the recording did. Once its seeded values for a kind are used
    up it falls back to `fallback` (for formatted references such as a case
    number) or to a random-looking UUID drawn from the same seed.
    """
    generator = _ids.get()
    if generator is None:
        return fallback() if fallback else str(uuid.uuid4())
    return generator.next(kind, fallback)


class IdGenerator:
    """Seeded identifiers: the listed values for each kind first, then derived ones."""

    def __init__(self, seed: str, sequences: dict[str, list[str]] | None = None):
        self.seed = seed
        self.sequences = sequences or {}
        self.issued: dict[str, int] = {}

    def next(self, kind: str, fallback: Callable[[], str] | None = None) -> str:
        n = self.issued.get(kind, 0)
        self.issued[kind] = n + 1
        seeded = self.sequences.get(kind, [])
        if n < len(seeded):
            return seeded[n]
        if fallback is not None:
            return fallback()
        rng = random.Random(f"{self.seed}:{kind}:{n}")
        return str(uuid.UUID(int=rng.getrandbits(128), version=4))

    def state(self) -> dict:
        return dict(self.issued)

    def restore(self, state: dict) -> None:
        self.issued = dict(state)


@contextlib.contextmanager
def call_context(clock: datetime, ids: IdGenerator) -> Iterator[None]:
    """Set the clock and identifier generator the tools see for one call."""
    clock_token, ids_token = _clock.set(clock), _ids.set(ids)
    try:
        yield
    finally:
        _clock.reset(clock_token)
        _ids.reset(ids_token)


# -- scenario values and database sequences ------------------------------------

def scenario_value(db: dict, key: str) -> Any:
    row = db["scenario"].get(key)
    return None if row is None else row["value"]


def allocate_id(db: dict, entity_type: str, scope: str = "") -> str:
    """Issue the next identifier for an entity type, advancing the allocator."""
    row = db["id_allocator"].get(f"{entity_type}|{scope}")
    if row is None:
        raise KeyError(f"no allocator for entity_type={entity_type!r} scope={scope!r}")
    issued = row["next_value"]
    row["next_value"] = issued + 1
    return row["template"].format(n=issued)


# -- result rendering ---------------------------------------------------------
# Results list candidate fields in registry order and drop the ones the backend
# does not know, and numerics are rendered as int or float explicitly because
# the recorded results use both forms (a $15 copay is 15, a zero fee is 0.0).

def compact(pairs: Iterable[tuple[str, Any]]) -> dict:
    return {key: value for key, value in pairs if value is not None}


def as_int(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, (Decimal, float)) and value != int(value):
        raise ValueError(f"{value} is not integral and cannot render as int")
    return int(value)


def as_float(value) -> float | None:
    return None if value is None else float(value)


def as_list_always(value) -> list:
    return [] if value is None else list(value)
