"""The environment a simulated agent talks to: one conversation's JSON database
behind one domain's tools.

    env = Environment.for_conversation("banking-account-email-card-application")
    step = env.call("lookup_customer", {"account_id": "SF204771"})
    step.status, step.output, step.reads, step.writes

Every call is validated against the domain's tool registry before it runs, and a
call that fails leaves the database exactly as it was. Each step also records
which rows the tool read and which it changed, which makes a run inspectable
after the fact.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import schema  # noqa: E402
from toolkit import IdGenerator, ToolError, call_context, call_started, row_key  # noqa: E402

# When a caller does not say when a tool call happens, the clock moves on by
# this much, so scheduled events still unfold over a simulated conversation.
DEFAULT_STEP_SECONDS = 20


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
            if old is None:
                kind, columns = "inserted", []
            elif new is None:
                kind, columns = "deleted", []
            else:
                kind = "updated"
                columns = sorted(c for c in set(old) | set(new) if old.get(c) != new.get(c))
            # Snapshots, not references: the live row keeps changing on later
            # calls, and a step must record the row as this call left it.
            changes.append({"table": table, "key": key, "kind": kind, "columns": columns,
                            "before": copy.deepcopy(old), "after": copy.deepcopy(new)})
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
    at: int = 0
    events: list[dict] = field(default_factory=list)

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
    """One conversation's backend: its database, its tools, a clock, the things
    other people do during the call, and a seeded identifier generator.

    The clock starts at the scenario's call_started_at and is measured in
    seconds into the call. Scheduled events (the customer entering a code sent
    to their phone, a merchant retrying a charge) are applied when the clock
    reaches them, before the next tool call, and only if their conditions hold.
    """

    def __init__(self, domain: str, db: dict, events: list[dict] | None = None,
                 ids: IdGenerator | None = None):
        self.domain = domain
        self.db = db
        self.tools = load_domain(domain)
        registry = load_json(os.path.join(ROOT, "domains", domain, "tool_registry.json"))
        self.schemas = {tool["name"]: tool for tool in registry["tools"]}
        unsupported = schema.validate_registry(registry)
        if unsupported:
            raise ValueError(f"{domain} registry uses unsupported keywords: {unsupported}")
        self.started = call_started(db)
        self.elapsed = 0
        self.pending = sorted((dict(e) for e in events or []), key=lambda e: (e["at_seconds"], e["event_id"]))
        # The events that played out after the last tool call, once finish() runs.
        self.after_call: list[dict] = []
        self.ids = ids or IdGenerator(domain)

    @classmethod
    def for_conversation(cls, conversation_id: str, which: str = "db.json") -> "Environment":
        state = os.path.join(ROOT, "conversations", conversation_id, "state")
        events_path, ids_path = os.path.join(state, "events.json"), os.path.join(state, "ids.json")
        events = load_json(events_path) if os.path.exists(events_path) else []
        sequences = load_json(ids_path) if os.path.exists(ids_path) else {}
        return cls(conversation_domain(conversation_id), load_json(os.path.join(state, which)),
                   events, IdGenerator(conversation_id, sequences))

    # -- time and the world outside the call ---------------------------------

    def clock(self, seconds: int | None = None) -> datetime:
        return self.started + timedelta(seconds=self.elapsed if seconds is None else seconds)

    def advance_to(self, seconds: float | None) -> list[dict]:
        """Move the clock forward and apply every event now due."""
        if seconds is None:
            self.elapsed += DEFAULT_STEP_SECONDS
        else:
            self.elapsed = max(self.elapsed, int(seconds))
        return self._apply_due(self.elapsed)

    def finish(self) -> list[dict]:
        """Let the rest of the scenario play out once the call is over."""
        self.after_call = self._apply_due(None)
        return self.after_call

    def _apply_due(self, upto: int | None) -> list[dict]:
        """Apply due events whose conditions hold.

        An event that fell due while its conditions did not hold (a merchant
        retrying a charge only once the card is unblocked) happens when they
        first do, and is stamped with that time rather than its scheduled one,
        so the record never shows it before the thing it waited for.
        """
        applied, waiting = [], []
        for event in self.pending:
            due = upto is None or event["at_seconds"] <= upto
            if due and self._conditions_hold(event):
                when = self.elapsed if event.get("_waited") else event["at_seconds"]
                before = copy.deepcopy(self.db)
                self._apply(event, when)
                applied.append({"event_id": event["event_id"], "at_seconds": when,
                                "actor": event["actor"], "description": event["description"],
                                "writes": row_changes(before, self.db)})
            else:
                if due:
                    event["_waited"] = True
                waiting.append(event)
        self.pending = waiting
        return applied

    def _conditions_hold(self, event: dict) -> bool:
        checks = list(event.get("when", []))
        # An update needs its row to exist: the customer cannot open a link that
        # has not been sent yet, so the event waits until it has.
        checks += [{"table": c["table"], "key": c["key"]}
                   for c in event["changes"] if c["op"] == "update"]
        for check in checks:
            row = self.db.get(check["table"], {}).get(check["key"])
            if row is None:
                return False
            if any(row.get(column) != value for column, value in check.get("equals", {}).items()):
                return False
        return True

    def _apply(self, event: dict, when: int) -> None:
        at = self.clock(when).isoformat(timespec="seconds")

        def resolve(value):
            return at if value == "$now" else value

        for change in event["changes"]:
            if change["op"] == "update":
                row = self.db[change["table"]][change["key"]]
                row.update({column: resolve(value) for column, value in change["set"].items()})
            elif change["op"] == "insert":
                row = {column: resolve(value) for column, value in change["row"].items()}
                key = row_key(row, self.tools.KEY_COLUMNS[change["table"]])
                self.db[change["table"]][key] = row
            else:
                raise ValueError(f"unknown event change {change['op']!r} in {event['event_id']}")

    @property
    def write_tools(self) -> set[str]:
        return set(self.tools.WRITE_TOOLS)

    def call(self, name: str, arguments: dict, at: float | None = None) -> Step:
        """Run one tool call `at` seconds into the call (or one step after the last)."""
        events = self.advance_to(at)
        step = self._call(name, arguments)
        # A write can be what an event was waiting for; let it happen now.
        events += self._apply_due(self.elapsed)
        step.at, step.events = self.elapsed, events
        return step

    def _call(self, name: str, arguments: dict) -> Step:
        tool = self.schemas.get(name)
        handler = self.tools.TOOLS.get(name)
        if tool is None or handler is None:
            return Step(name, arguments, 404, _error("unknown_tool", f"no tool named {name!r}"))
        violations = schema.validate(arguments, tool["parameters"])
        if violations:
            return Step(name, arguments, 400, _error(
                "invalid_arguments", "arguments do not satisfy the tool schema",
                {"violations": violations}))

        before, issued = copy.deepcopy(self.db), self.ids.state()
        log: dict[str, dict] = {}
        traced = {table: TracedTable(table, rows, log) for table, rows in self.db.items()}
        try:
            with call_context(self.clock(), self.ids):
                output = plain(handler(traced, arguments))
        except ToolError as exc:
            self.db = before
            self.ids.restore(issued)
            return Step(name, arguments, exc.status, _error(exc.kind, exc.message, exc.detail),
                        reads=_reads(log))
        except Exception as exc:  # noqa: BLE001 - reported as the recorded backend did
            self.db = before
            self.ids.restore(issued)
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
