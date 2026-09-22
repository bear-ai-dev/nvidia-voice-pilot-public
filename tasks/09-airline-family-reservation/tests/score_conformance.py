#!/usr/bin/env python3
"""Score the conformance layer and write conformance.json.

This asks whether the environment is faithful, not whether an agent did well: the
recorded call sequence is the specification, and a divergence is a defect in this
backend rather than in the recording. Two conditions, both required:

  1. Every recorded call was reproduced exactly (from the replay report).
  2. Every field the expected-final-state document asserts holds afterwards.

A report with no calls in it satisfies "every call matched" vacuously, so a run
that reproduced nothing is not a pass regardless of what the database says.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error

import statecheck

DEFAULT_EXPECTED = "/var/lib/task-data/verifier/expected_final_state.json"
DEFAULT_BASE = os.environ.get("TOOL_SERVER_URL", statecheck.DEFAULT_BASE)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--replay-report", required=True)
    parser.add_argument("--expected", default=DEFAULT_EXPECTED)
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--token-file", default=statecheck.DEFAULT_TOKEN_FILE)
    parser.add_argument("--out", required=True)
    parser.add_argument("--report", default=None)
    args = parser.parse_args()

    with open(args.replay_report) as fh:
        replay = json.load(fh)

    lines: list[str] = []
    calls_total = replay["calls_total"]
    calls_matched = replay["calls_matched"]
    lines.append(f"tool calls reproduced: {calls_matched}/{calls_total}")
    for call in replay["calls"]:
        if not call["matched"]:
            lines.append(f"  MISMATCH {call['call_id']} ({call['tool']}): {call['detail']}")

    checked = matched = 0
    state_errors: list[str] = []
    try:
        token = statecheck.read_token(args.token_file)
        schema = statecheck.fetch_schema(args.base, token)
        state = statecheck.fetch_state(args.base, token)
        with open(args.expected) as fh:
            expected = json.load(fh)
        state_errors, checked, matched = statecheck.assert_required_facts(
            state, expected, schema["key_columns"]
        )
    except (OSError, urllib.error.URLError, json.JSONDecodeError, KeyError) as exc:
        state_errors = [f"could not evaluate final state: {type(exc).__name__}: {exc}"]

    lines.append(f"final-state fields matched: {matched}/{checked}")
    for error in state_errors:
        lines.append(f"  STATE {error}")

    if calls_total == 0:
        lines.append("  no tool calls were replayed, so nothing was reproduced")
    passed = (
        calls_total > 0
        and replay["all_matched"]
        and not state_errors
        and checked > 0
    )

    payload = {
        "conformant": passed,
        "calls_total": calls_total,
        "calls_matched": calls_matched,
        "state_fields_checked": checked,
        "state_fields_matched": max(matched, 0),
    }
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")

    lines.append("")
    lines.append(f"conformant: {str(passed).lower()}")
    text = "\n".join(lines) + "\n"
    if args.report:
        with open(args.report, "w") as fh:
            fh.write(text)
    print(text, end="")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
