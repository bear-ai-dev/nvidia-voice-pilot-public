#!/usr/bin/env python3
"""Replay the recorded tool calls against the running tool server.

The recorded conversation is this environment's specification: if the server is a
faithful backend, issuing the calls the enacted agent issued, in the order it
issued them, must return exactly what it received. A divergence is a defect in
the environment, not in the recording.

Comparison canonicalizes with sorted keys, so property order in a result is free,
but every field, value, and JSON number form must match.

Writes a JSON report to stdout, or to --report if given. Exit status is 0 when
every call matched.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_GOLD = "/var/lib/task-data/verifier/gold_calls.json"
DEFAULT_BASE = os.environ.get("TOOL_SERVER_URL", "http://127.0.0.1:8080")


def canonical(payload) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def post(base: str, tool: str, arguments: dict, timeout: float = 30.0):
    request = urllib.request.Request(
        f"{base}/tools/{tool}",
        data=json.dumps(arguments).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"error": {"type": "non_json", "body": body.decode("utf-8", "replace")}}


def first_difference(got: str, want: str) -> str:
    limit = min(len(got), len(want))
    index = next((i for i in range(limit) if got[i] != want[i]), limit)
    lo = max(0, index - 60)
    return (f"diverges at character {index}\n"
            f"      got:  ...{got[lo:index + 90]}\n"
            f"      want: ...{want[lo:index + 90]}")


def replay(gold_path: str, base: str, verbose: bool = True) -> dict:
    with open(gold_path) as fh:
        gold = json.load(fh)

    results = []
    for call in gold["calls"]:
        status, body = post(base, call["name"], call["arguments"])
        want = canonical(call["expected_output"])
        got = canonical(body)
        matched = status == 200 and got == want
        results.append({
            "call_id": call["call_id"],
            "tool": call["name"],
            "state_effect": call.get("state_effect"),
            "http_status": status,
            "matched": matched,
            "detail": None if matched else first_difference(got, want),
        })
        if verbose:
            mark = "ok  " if matched else "FAIL"
            print(f"  [{mark}] {call['call_id']:>8}  {call['name']}")
            if not matched:
                print(f"      http {status}")
                print(f"      {results[-1]['detail']}")

    matched = sum(1 for r in results if r["matched"])
    report = {
        "conversation_id": gold["conversation_id"],
        "domain": gold["domain"],
        "calls_total": len(results),
        "calls_matched": matched,
        "all_matched": matched == len(results),
        "calls": results,
    }
    if verbose:
        print(f"  {matched}/{len(results)} calls reproduced exactly")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gold", default=DEFAULT_GOLD)
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--report", default=None)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    report = replay(args.gold, args.base, verbose=not args.quiet)
    if args.report:
        with open(args.report, "w") as fh:
            json.dump(report, fh, indent=2)
            fh.write("\n")
    else:
        print(json.dumps(report, indent=2))

    sys.exit(0 if report["all_matched"] else 1)


if __name__ == "__main__":
    main()
