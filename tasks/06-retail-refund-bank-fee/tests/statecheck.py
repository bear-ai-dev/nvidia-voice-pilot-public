#!/usr/bin/env python3
"""State comparison shared by the conformance layer and the grading layer.

Both layers ask questions about the database over the same admin snapshot, so the
subset assertion, the row addressing, and the row digest live here rather than
being written twice with a chance of drifting apart.
"""
from __future__ import annotations

import hashlib
import json
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:8080"
DEFAULT_TOKEN_FILE = "/var/lib/task-data/admin_token"


def read_token(path: str = DEFAULT_TOKEN_FILE) -> str:
    with open(path) as fh:
        return fh.read().strip()


def _get(base: str, path: str, token: str, timeout: float = 60.0):
    request = urllib.request.Request(
        f"{base}{path}", headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def fetch_state(base: str, token: str) -> dict:
    return _get(base, "/_admin/state", token)


def fetch_schema(base: str, token: str) -> dict:
    """Key columns per table and the set of state-mutating tools.

    Served by the tool server rather than restated in verifier data, so a table
    added to the snapshot is addressable without editing anything here.
    """
    return _get(base, "/_admin/schema", token)


def fetch_calls(base: str, token: str) -> list[dict]:
    return _get(base, "/_admin/calls", token)["calls"]


def canonical(payload) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def row_key(row: dict, key_columns: list[str]) -> str:
    return "|".join(str(row[column]) for column in key_columns)


def row_digest(row: dict) -> str:
    """Short content hash of a row.

    Twelve hex characters of SHA-256 over the canonical form. Long enough that a
    collision between two versions of the same row is not a practical concern,
    short enough that ten tasks' worth of digests stay reviewable in git.
    """
    return hashlib.sha256(canonical(row).encode()).hexdigest()[:12]


def digest_state(
    state: dict,
    key_columns: dict[str, list[str]],
    volatile_columns: dict[str, list[str]] | None = None,
) -> dict:
    """Reduce a snapshot to {table: {row key: row digest}}.

    Tables without declared key columns are skipped: without a stable address a
    modified row is indistinguishable from a delete plus an insert, which would
    make every diff unreadable.

    `volatile_columns` names columns whose value records that a row was *read*
    rather than what the run did to it — a session's opened_at, a notification's
    delivery progress. They are dropped before hashing, which is what lets such a
    table stay under the damage check instead of being excluded wholesale: an
    unrequested notification is still an inserted row and still caught, while a
    notification that merely advanced because somebody looked at it is not.
    """
    volatile_columns = volatile_columns or {}
    digested: dict[str, dict[str, str]] = {}
    for table, rows in state.items():
        if table.startswith("_"):
            continue
        columns = key_columns.get(table)
        if not columns:
            continue
        drop = set(volatile_columns.get(table, ()))
        digested[table] = {
            row_key(row, columns): row_digest(
                {k: v for k, v in row.items() if k not in drop} if drop else row
            )
            for row in rows
        }
    return digested


def subset_errors(expected, actual, path: str = "") -> list[str]:
    """Every field in `expected` must exist in `actual` with the same value.

    Lists are compared by exact length rather than as subsets: an extra element
    in an ordered field is a real difference, not extra knowledge.

    A leaf written as {"_any_of": [...]} is satisfied by any listed value. This
    exists for fields whose value records whether an *optional read* happened
    rather than what the run achieved. Some tools model "the customer did
    something in a channel no tool can observe" by writing the transition the
    first time the record is read, so a session nobody looked at reads back
    'issued' and the same session read once reads 'open_not_submitted'. Both are
    the same outcome, and pinning one of them would make the state assertion
    depend on the route after all.
    """
    errors: list[str] = []
    where = path or "(root)"
    if isinstance(expected, dict) and set(expected) == {"_any_of"}:
        if actual not in expected["_any_of"]:
            errors.append(f"{where}: expected one of {expected['_any_of']!r}, "
                          f"got {actual!r}")
        return errors
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [f"{where}: expected an object, got {type(actual).__name__}"]
        for key, value in expected.items():
            if key not in actual:
                errors.append(f"{where}.{key}: missing")
            else:
                errors.extend(subset_errors(value, actual[key], f"{path}.{key}"))
    elif isinstance(expected, list):
        if not isinstance(actual, list):
            errors.append(f"{where}: expected a list, got {type(actual).__name__}")
        elif len(expected) != len(actual):
            errors.append(f"{where}: expected {len(expected)} items, got {len(actual)}")
        else:
            for index, value in enumerate(expected):
                errors.extend(subset_errors(value, actual[index], f"{path}[{index}]"))
    elif expected != actual:
        errors.append(f"{where}: expected {expected!r}, got {actual!r}")
    return errors


def count_leaves(expected) -> int:
    if isinstance(expected, dict):
        if set(expected) == {"_any_of"}:
            return 1
        return sum(count_leaves(v) for v in expected.values())
    if isinstance(expected, list):
        return sum(count_leaves(v) for v in expected)
    return 1


def index_state(state: dict, expected: dict, key_columns: dict) -> dict:
    """Rekey the snapshot's row lists to match how the expected document addresses them."""
    indexed: dict = {}
    for table in expected:
        if table.startswith("_"):
            continue
        declared = expected.get("_key_columns", {}).get(table)
        columns = [declared] if declared else key_columns.get(table)
        rows = state.get(table, [])
        if not columns:
            indexed[table] = rows
            continue
        indexed[table] = {row_key(row, columns): row for row in rows}
    return indexed


def assert_required_facts(state: dict, expected: dict, key_columns: dict):
    """Check the expected-final-state document against a snapshot.

    Returns (errors, fields_checked, fields_matched).
    """
    # Underscore-prefixed keys are directives and prose, not assertions.
    target = {k: v for k, v in expected.items() if not k.startswith("_")}
    actual = index_state(state, expected, key_columns)
    errors = subset_errors(target, actual)
    checked = count_leaves(target)
    return errors, checked, checked - len(errors)
