"""The environment a simulated agent talks to: one conversation's JSON database
behind one domain's tools.

    env = Environment.for_conversation("banking-account-email-card-application")
    step = env.call("lookup_customer", {"account_id": "SF204771"})
    step.status, step.output, step.reads, step.writes

Every call is validated against the domain's tool registry before it runs, and a
call that fails leaves the database exactly as it was. Each step also records
which rows the tool read and which it changed, which is what the demo draws and
what makes a run inspectable after the fact.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import schema  # noqa: E402
from toolkit import ToolError  # noqa: E402


def json_default(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"cannot serialize {type(value).__name__}")


def plain(value):
    """What the value looks like on the wire, as the recorded backend sent it."""
    return json.loads(json.dumps(value, default=json_default))


def load_json(path: str):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# -- read tracing -------------------------------------------------------------

class TracedTable(dict):
    """A table that notes what a tool read from it.

    Tools see an ordinary dict. Lookups by key are logged per row, a filter run
    through toolkit.rows() is logged with its predicate and the rows it matched,
    and iterating the table by hand is logged as a scan of every row.
    """

    def __init__(self, name: str, rows: dict, log: dict):
        super().__init__(rows)
        self._name, self._log = name, log

    def _entry(self) -> dict:
        return self._log.setdefault(self._name, {"keys": set(), "queries": [], "scanned": False})

    def _row(self, key):
        if isinstance(key, str):  # a lookup of a null reference reads nothing
            self._entry()["keys"].add(key)

    def _scan(self):
        self._entry()["scanned"] = True

    def note_query(self, where: dict, matched: list[str]):
        self._entry()["queries"].append({"where": where, "matched": matched})

    def __getitem__(self, key):
        self._row(key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        self._row(key)
        return super().get(key, default)

    def __contains__(self, key):
        self._row(key)
        return super().__contains__(key)

    def __iter__(self):
        self._scan()
        return super().__iter__()

    def items(self):
        self._scan()
        return super().items()

    def values(self):
        self._scan()
        return super().values()

    def keys(self):
        self._scan()
        return super().keys()


def row_changes(before: dict, after: dict) -> list[dict]:
    """Rows that differ between two databases, with the columns that changed."""
    changes = []
    for table in sorted(set(before) | set(after)):
        old_rows, new_rows = before.get(table, {}), after.get(table, {})
        if old_rows == new_rows:
            continue
        for key in sorted(set(old_rows) | set(new_rows)):
            old, new = old_rows.get(key), new_rows.get(key)
            if old == new:
                continue
            kind = "inserted" if old is None else "deleted" if new is None else "updated"
            columns = sorted(
                c for c in set(old or {}) | set(new or {})
                if (old or {}).get(c) != (new or {}).get(c)
            ) if kind == "updated" else []
            changes.append({"table": table, "key": key, "kind": kind,
                            "columns": columns, "before": old, "after": new})
    return changes


# -- the environment ----------------------------------------------------------

@dataclass
class Step:
    name: str
    arguments: dict
    status: int
    output: dict
    reads: dict[str, dict] = field(default_factory=dict)
    writes: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == 200


def load_domain(domain: str):
    path = os.path.join(ROOT, "domains", domain, "tools.py")
    spec = importlib.util.spec_from_file_location(f"domain_tools_{domain}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def conversation_domain(conversation_id: str) -> str:
    manifest = load_json(os.path.join(ROOT, "conversation_manifest.json"))
    for entry in manifest["conversations"]:
        if entry["conversation_id"] == conversation_id:
            return entry["domain"]
    raise KeyError(f"no conversation {conversation_id!r} in conversation_manifest.json")


class Environment:
    def __init__(self, domain: str, db: dict):
        self.domain = domain
        self.db = db
        self.tools = load_domain(domain)
        registry = load_json(os.path.join(ROOT, "domains", domain, "tool_registry.json"))
        self.schemas = {tool["name"]: tool for tool in registry["tools"]}
        unsupported = schema.validate_registry(registry)
        if unsupported:
            raise ValueError(f"{domain} registry uses unsupported keywords: {unsupported}")

    @classmethod
    def for_conversation(cls, conversation_id: str, which: str = "db.json") -> "Environment":
        path = os.path.join(ROOT, "conversations", conversation_id, "state", which)
        return cls(conversation_domain(conversation_id), load_json(path))

    @property
    def write_tools(self) -> set[str]:
        return set(self.tools.WRITE_TOOLS)

    def call(self, name: str, arguments: dict) -> Step:
        tool = self.schemas.get(name)
        handler = self.tools.TOOLS.get(name)
        if tool is None or handler is None:
            return Step(name, arguments, 404, _error("unknown_tool", f"no tool named {name!r}"))
        violations = schema.validate(arguments, tool["parameters"])
        if violations:
            return Step(name, arguments, 400, _error(
                "invalid_arguments", "arguments do not satisfy the tool schema",
                {"violations": violations}))

        before = copy.deepcopy(self.db)
        log: dict[str, dict] = {}
        traced = {table: TracedTable(table, rows, log) for table, rows in self.db.items()}
        try:
            output = plain(handler(traced, arguments))
        except ToolError as exc:
            self.db = before
            return Step(name, arguments, exc.status, _error(exc.kind, exc.message, exc.detail),
                        reads=_reads(log))
        except Exception as exc:  # noqa: BLE001 - reported as the recorded backend did
            self.db = before
            return Step(name, arguments, 500, _error("handler_error", f"{type(exc).__name__}: {exc}"))
        reads = _reads(log)
        self.db = {table: dict(dict.items(rows)) for table, rows in traced.items()}
        return Step(name, arguments, 200, output, reads=reads,
                    writes=row_changes(before, self.db))


def _reads(log: dict) -> dict[str, dict]:
    # A scan reads every row, so the per-row lookups made while walking the
    # table add nothing and are dropped; filtered queries keep their matches.
    return {table: {"keys": [] if entry["scanned"] else sorted(entry["keys"]),
                    "queries": entry["queries"], "scanned": entry["scanned"]}
            for table, entry in sorted(log.items())}


def _error(kind: str, message: str, detail: dict | None = None) -> dict:
    payload = {"error": {"type": kind, "message": message}}
    if detail:
        payload["error"]["detail"] = detail
    return payload
