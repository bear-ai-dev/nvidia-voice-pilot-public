#!/usr/bin/env python3
"""Generate state_digest.json: per-row hashes of the initial and gold end states.

Run inside a container, as root, against a database that is not being used for
anything else. It rebuilds the database twice — once to digest the initial state,
once more to replay the gold calls and digest the result — and leaves the
database rebuilt at the end.

The digest is what makes the damage check possible: the grading layer needs to
know which rows the gold path touched, so it can hold everything else to the
initial state without caring how the agent reached the rows it was allowed to
change.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

import statecheck

TASK_INIT = "/usr/local/bin/task-init.sh"


def reset_database() -> None:
    subprocess.run([TASK_INIT, "--reset-db"], check=True, capture_output=True)


def wait_for_server(base: str) -> None:
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"{base}/health", timeout=5):
                return
        except OSError:
            time.sleep(0.5)
    raise SystemExit("tool server never became healthy")


def replay_gold(base: str, gold_path: str) -> None:
    with open(gold_path) as fh:
        gold = json.load(fh)
    for call in gold["calls"]:
        request = urllib.request.Request(
            f"{base}/tools/{call['name']}",
            data=json.dumps(call["arguments"]).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=30).read()
        except OSError as exc:
            print(f"  warning: gold call {call['call_id']} failed: {exc}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=os.environ.get("TOOL_SERVER_URL", statecheck.DEFAULT_BASE))
    parser.add_argument("--verifier-dir", default="/var/lib/task-data/verifier")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    token = statecheck.read_token()
    wait_for_server(args.base)
    key_columns = statecheck.fetch_schema(args.base, token)["key_columns"]

    # Columns excluded from the row hash, taken from the task's damage policy and
    # recorded in the digest below so the grading layer excludes exactly the same
    # ones. Reading them from two places independently is how the two halves of
    # the damage check would drift apart.
    with open(os.path.join(args.verifier_dir, "grading.json")) as fh:
        volatile = json.load(fh).get("damage", {}).get("read_volatile_columns", {})
    if volatile:
        listed = sum(len(v) for v in volatile.values())
        print(f"excluding {listed} read-volatile column(s) across "
              f"{len(volatile)} table(s) from the row hash")

    print("digesting the initial state")
    reset_database()
    initial = statecheck.digest_state(
        statecheck.fetch_state(args.base, token), key_columns, volatile
    )

    print("replaying the gold calls")
    replay_gold(args.base, os.path.join(args.verifier_dir, "gold_calls.json"))
    gold_final = statecheck.digest_state(
        statecheck.fetch_state(args.base, token), key_columns, volatile
    )

    touched = sum(
        1
        for table in set(initial) | set(gold_final)
        for key in set(initial.get(table, {})) | set(gold_final.get(table, {}))
        if initial.get(table, {}).get(key) != gold_final.get(table, {}).get(key)
    )
    rows = sum(len(v) for v in initial.values())
    print(f"{rows} rows digested across {len(initial)} tables; "
          f"the gold path touched {touched}")

    payload = {
        "_comment": [
            "Per-row content hashes of the initial state and of the state the",
            "recorded call sequence leaves behind. Used by the grading layer to",
            "tell the agent's legitimate work area (rows the gold path touched)",
            "from collateral damage (rows it did not).",
            "Regenerate with tests/make_digest.py after changing the seed.",
        ],
        "key_columns": key_columns,
        "read_volatile_columns": volatile,
        "gold_touched_rows": touched,
        "initial": initial,
        "gold_final": gold_final,
    }
    with open(args.out, "w") as fh:
        json.dump(payload, fh, indent=1, sort_keys=True)
        fh.write("\n")
    print(f"wrote {args.out}")

    print("leaving the database rebuilt")
    reset_database()


if __name__ == "__main__":
    main()
