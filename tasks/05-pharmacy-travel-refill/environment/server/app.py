#!/usr/bin/env python3
"""HTTP front end for the pharmacy tool server.

Routes:

  GET  /health              readiness, used by task-init.sh
  GET  /tools               the tool registry, for agent discovery
  POST /tools/{tool_name}   execute a tool; body is arguments, response is result
  GET  /_admin/state        canonical JSON of every table
  POST /_admin/snapshot     write that JSON to /out/final_state.json

The two admin routes require a bearer token that is generated at startup and
written to a root-only path, so an agent can observe the world only through the
tools.

Argument validation runs against the registry before any SQL, so a malformed
call is refused at the boundary instead of quietly returning no rows.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import traceback
from decimal import Decimal
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg2

import schema
from db import NotFound, ToolRefusal, all_rows, transaction, unkeyed_snapshot_tables
from tools import HANDLERS, WRITE_TOOLS

HERE = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(HERE, "registry.json")) as fh:
    REGISTRY = json.load(fh)

TOOL_SCHEMAS = {t["name"]: t for t in REGISTRY["tools"]}

# Tables included in a state snapshot, with the ordering that makes the dump
# byte-stable across runs.
SNAPSHOT_TABLES = [
    ("scenario", "key"),
    ("id_allocator", "entity_type, scope"),
    ("medications", "medication_id"),
    ("stores", "store_id"),
    ("store_inventory", "store_id, medication_id"),
    ("insurance_plans", "plan_id"),
    ("plan_override_rules", "plan_id, reason"),
    ("patients", "patient_id"),
    ("notification_destinations", "destination_id"),
    ("prescriptions", "prescription_id"),
    ("claims", "claim_seq"),
    ("latest_claims", "prescription_id"),
    ("claim_overrides", "override_id"),
    ("fill_queue", "prescription_id"),
    ("transfer_requests", "transfer_id"),
    ("specialist_transfers", "transfer_id"),
]


def json_default(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"cannot serialize {type(value).__name__}")


def dumps(payload) -> bytes:
    return json.dumps(payload, default=json_default).encode()


class ToolHandler(BaseHTTPRequestHandler):
    server_version = "voice-tool-server/1.0"
    admin_token = ""

    # Keep the access log out of stdout; task-init.sh captures stderr to a file.
    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- helpers ----------------------------------------------------------

    def _send(self, status: int, payload) -> None:
        body = dumps(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, kind: str, message: str, detail: dict | None = None):
        payload = {"error": {"type": kind, "message": message}}
        if detail:
            payload["error"]["detail"] = detail
        self._send(status, payload)

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        expected = f"Bearer {self.admin_token}"
        return bool(self.admin_token) and secrets.compare_digest(header, expected)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw)

    # -- routes -----------------------------------------------------------

    def do_GET(self):
        if self.path == "/health":
            try:
                with transaction() as cur:
                    cur.execute("SELECT 1")
            except psycopg2.Error as exc:
                return self._error(503, "database_unavailable", str(exc).strip())
            return self._send(200, {"status": "ok"})

        if self.path == "/tools":
            return self._send(200, REGISTRY)

        if self.path == "/_admin/state":
            if not self._authorized():
                return self._error(401, "unauthorized", "admin token required")
            return self._send(200, self._snapshot())

        if self.path == "/_admin/schema":
            if not self._authorized():
                return self._error(401, "unauthorized", "admin token required")
            # Served rather than restated in verifier data: the grading layer
            # needs to address rows by key and to know which tools mutate, and
            # both facts already live here.
            return self._send(200, {
                "key_columns": {
                    table: [c.strip() for c in order_by.split(",")]
                    for table, order_by in SNAPSHOT_TABLES
                },
                "write_tools": sorted(WRITE_TOOLS),
                "read_tools": sorted(set(HANDLERS) - WRITE_TOOLS),
            })

        if self.path == "/_admin/calls":
            if not self._authorized():
                return self._error(401, "unauthorized", "admin token required")
            with transaction() as cur:
                calls = all_rows(cur, """
                    SELECT call_seq, tool_name, arguments, http_status
                      FROM tool_call_log ORDER BY call_seq
                """)
            return self._send(200, {"calls": calls})

        return self._error(404, "not_found", f"no route for GET {self.path}")

    def do_POST(self):
        if self.path == "/_admin/snapshot":
            if not self._authorized():
                return self._error(401, "unauthorized", "admin token required")
            state = self._snapshot()
            out_dir = os.environ.get("STATE_OUT_DIR", "/out")
            os.makedirs(out_dir, exist_ok=True)
            target = os.path.join(out_dir, "final_state.json")
            with open(target, "w") as fh:
                # Sorted keys and fixed table order make two equal states equal
                # byte for byte, so final states can be diffed directly.
                json.dump(state, fh, default=json_default, indent=2, sort_keys=True)
                fh.write("\n")
            return self._send(200, {"status": "written", "path": target})

        if not self.path.startswith("/tools/"):
            return self._error(404, "not_found", f"no route for POST {self.path}")

        tool_name = self.path[len("/tools/"):]
        tool = TOOL_SCHEMAS.get(tool_name)
        if tool is None or tool_name not in HANDLERS:
            return self._error(404, "unknown_tool", f"no tool named {tool_name!r}")

        try:
            args = self._read_body()
        except json.JSONDecodeError as exc:
            return self._error(400, "malformed_body", f"body is not valid JSON: {exc}")
        if not isinstance(args, dict):
            return self._error(400, "malformed_body", "body must be a JSON object")

        errors = schema.validate(args, tool["parameters"])
        if errors:
            self._log_call(tool_name, args, None, 400)
            return self._error(400, "invalid_arguments",
                               "arguments do not satisfy the tool schema",
                               {"violations": errors})

        try:
            with transaction() as cur:
                result = HANDLERS[tool_name](cur, args)
                # Logged inside the same transaction as the mutation, so the
                # audit trail cannot disagree with the state it describes.
                self._log_call(tool_name, args, result, 200, cur=cur)
        except ToolRefusal as exc:
            self._log_call(tool_name, args, None, 409)
            return self._error(409, "refused", exc.message, exc.detail)
        except NotFound as exc:
            self._log_call(tool_name, args, None, 404)
            return self._error(404, "not_found", str(exc))
        except psycopg2.Error as exc:
            self._log_call(tool_name, args, None, 500)
            return self._error(500, "database_error", str(exc).strip())
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            traceback.print_exc(file=sys.stderr)
            self._log_call(tool_name, args, None, 500)
            return self._error(500, "handler_error", f"{type(exc).__name__}: {exc}")

        return self._send(200, result)

    # -- state ------------------------------------------------------------

    def _snapshot(self) -> dict:
        state: dict = {}
        with transaction() as cur:
            for table, order_by in SNAPSHOT_TABLES:
                state[table] = all_rows(cur, f"SELECT * FROM {table} ORDER BY {order_by}")
        return state

    def _log_call(self, tool_name, args, result, status, cur=None) -> None:
        statement = """
            INSERT INTO tool_call_log (tool_name, arguments, result, http_status)
            VALUES (%s, %s, %s, %s)
        """
        params = (tool_name, json.dumps(args),
                  json.dumps(result, default=json_default) if result is not None else None,
                  status)
        if cur is not None:
            cur.execute(statement, params)
            return
        try:
            with transaction() as own:
                own.execute(statement, params)
        except psycopg2.Error:
            pass  # An audit write must never mask the response it describes.


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--admin-token-file", default="/var/lib/task-data/admin_token")
    args = parser.parse_args()

    ignored = schema.validate_registry(REGISTRY)
    if ignored:
        print(f"warning: registry uses unenforced schema keywords: {ignored}",
              file=sys.stderr)

    missing = sorted(set(TOOL_SCHEMAS) - set(HANDLERS))
    if missing:
        print(f"fatal: registry tools without handlers: {missing}", file=sys.stderr)
        raise SystemExit(1)

    unkeyed = unkeyed_snapshot_tables(SNAPSHOT_TABLES)
    if unkeyed:
        print(f"fatal: snapshot keys no unique index guarantees: {unkeyed}",
              file=sys.stderr)
        raise SystemExit(1)

    token = secrets.token_hex(24)
    os.makedirs(os.path.dirname(args.admin_token_file), exist_ok=True)
    with open(args.admin_token_file, "w") as fh:
        fh.write(token + "\n")
    os.chmod(args.admin_token_file, 0o600)
    ToolHandler.admin_token = token

    server = ThreadingHTTPServer((args.host, args.port), ToolHandler)
    print(f"pharmacy tool server listening on {args.host}:{args.port} "
          f"({len(HANDLERS)} tools)", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
