#!/usr/bin/env python3
"""Find state assertions whose value depends on whether an optional read happened.

Some tools model "the customer did something in a channel no tool can observe" by
writing the transition the first time the record is read. A self-service session
nobody looked at reads back 'issued'; read once, the same session reads
'open_not_submitted'. Both describe the same outcome.

That makes an assertion on such a field path-dependent in disguise, which is the
easiest way to reintroduce the route-scoring the grading layer exists to remove.
An agent that sensibly confirms the tracker it just issued must not fail, and
neither must one that does not bother.

The audit reports three classes, and only the first is a failure:

  certain     The assertion lands on a column the task itself declares as
              read-volatile in grading.json, and is neither written as
              {"_any_of": [...]} nor declared forced. The task is asserting a
              value it has already said changes on read.

  forced      The assertion lands on a read-volatile column, but a later write
              refuses to proceed unless the value is already there, so every
              correct route has necessarily performed the read and the fixed
              value is a real precondition rather than a stray look. This cannot
              be detected mechanically, so the task declares it and states why,
              in expected_final_state.json:

                  "_forced_read_fields": {
                    "channel_confirmations.status":
                      "update_customer_email refuses unless the confirmation is
                       already verified, and only reading it moves it there."
                  }

              A justification is required, not optional: the point is to make
              this a reviewed decision that a reader can check and disagree with,
              rather than a way to silence the audit.

  unverified  The assertion lands on a table excluded from the damage check
              wholesale, where no per-column information exists. Most of these
              are fine — a session's workflow does not change on read — so they
              print for a human to judge and do not fail the run.

Usage:
  ./audit_read_sensitivity.py            every task
  ./audit_read_sensitivity.py 01 02      only the named tasks, by number prefix
"""
from __future__ import annotations

import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent

# Bookkeeping excluded for reasons other than read effects, and which no
# expected-state document should be asserting against anyway.
NOT_READ_EFFECTS = {"scenario", "tool_call_log"}


def leaf_paths(node, prefix=""):
    """Yield (dotted path, value, is_any_of) for each asserted leaf."""
    if isinstance(node, dict):
        if set(node) == {"_any_of"}:
            yield prefix, node, True
            return
        for key, value in node.items():
            if key.startswith("_"):
                continue
            yield from leaf_paths(value, f"{prefix}.{key}" if prefix else key)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from leaf_paths(value, f"{prefix}[{index}]")
    else:
        yield prefix, node, False


def audit(task: pathlib.Path):
    data = task / "environment" / "verifier-data"
    try:
        damage = json.loads((data / "grading.json").read_text()).get("damage", {})
        expected = json.loads((data / "expected_final_state.json").read_text())
    except FileNotFoundError as exc:
        return [], [], 0, [f"  missing {pathlib.Path(exc.filename).name}"]

    volatile = damage.get("read_volatile_columns", {})
    ignored = set(damage.get("ignore_tables", [])) - NOT_READ_EFFECTS
    asserted = {k for k in expected if not k.startswith("_")}
    forced = expected.get("_forced_read_fields", {})

    certain, unverified, resolved, lines = [], [], 0, []

    # Certain: the task declares the column volatile and asserts a value anyway.
    for table in sorted(set(volatile) & asserted):
        columns = set(volatile[table])
        for path, value, is_any_of in leaf_paths(expected[table], table):
            # path is table.row.column, or table.row.column[i] for a list field.
            parts = path.split(".")
            column = parts[2].split("[")[0] if len(parts) > 2 else ""
            if column not in columns:
                continue
            if is_any_of:
                resolved += 1
                lines.append(f"  [resolved   ] {path} = any of {value['_any_of']!r}")
                continue
            reason = str(forced.get(f"{table}.{column}", "")).strip()
            if reason:
                lines.append(f"  [forced     ] {path} = {value!r}")
                lines.append(f"                 {' '.join(reason.split())}")
            else:
                certain.append(path)
                lines.append(f"  [DEFECT     ] {path} = {value!r}")
                lines.append(f"                 {table}.{column} is declared "
                             f"read-volatile by this task")

    # Unverified: whole table excluded, so no column-level information exists.
    for table in sorted((ignored - set(volatile)) & asserted):
        for path, value, is_any_of in leaf_paths(expected[table], table):
            if is_any_of:
                resolved += 1
                lines.append(f"  [resolved   ] {path} = any of {value['_any_of']!r}")
            else:
                unverified.append(path)

    return certain, unverified, resolved, lines


def main() -> None:
    prefixes = sys.argv[1:]
    tasks = sorted(d for d in HERE.glob("[0-9][0-9]-*") if d.is_dir())
    if prefixes:
        tasks = [t for t in tasks if any(t.name.startswith(p + "-") for p in prefixes)]

    defects = 0
    for task in tasks:
        certain, unverified, resolved, lines = audit(task)
        defects += len(certain)
        parts = []
        if certain:
            parts.append(f"{len(certain)} DEFECT(S)")
        if resolved:
            parts.append(f"{resolved} resolved")
        if unverified:
            parts.append(f"{len(unverified)} unverified")
        print(f"{task.name:46s} {', '.join(parts) or 'clean'}")
        for line in lines:
            print(line)

    print()
    if defects:
        print(f"{defects} assertion(s) pin a column the task itself declares")
        print("read-volatile. For each, either write it as {\"_any_of\": [...]} listing")
        print("the values a correct run may leave, drop it if it is a bare read counter")
        print("with no bounded set, or — if a later write refuses to proceed unless the")
        print("value is already there — declare it under _forced_read_fields with a")
        print("justification saying which write forces it.")
        sys.exit(1)
    print("No assertion pins a declared read-volatile column.")
    print("Unverified counts above land on wholesale-excluded tables and need a human;")
    print("they are not failures.")


if __name__ == "__main__":
    main()
