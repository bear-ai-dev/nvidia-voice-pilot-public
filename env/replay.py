#!/usr/bin/env python3
"""Check that each conversation's JSON backend reproduces its recording, and
that its task file scores the recording the way tau2 would.

For every conversation with a db.json, replay the tool calls recorded in its
annotated transcript against the domain's tools and require:

  1. every call succeeds and returns exactly the recorded output,
  2. the database afterwards equals state/db_after.json, the end state the
     original PostgreSQL backend reached on the same calls,

and for its task in domains/<domain>/tasks.json:

  3. the reference actions are exactly the recorded calls,
  4. the recorded conversation scores 1.0 (DB x COMMUNICATE),
  5. issuing every read call again still scores 1.0, and leaving out any one
     state-changing call fails the DB check.

    python3 env/replay.py                      # every conversation
    python3 env/replay.py banking-declined-card-travel

Exit status is 0 only when every conversation passes.
"""
from __future__ import annotations

import json
import os
import sys

from evaluate import evaluate, find_task, recorded_trajectory
from runtime import ROOT, Environment, load_json, row_changes


def canonical(payload) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def recorded_calls(conversation_id: str) -> list[dict]:
    """The recorded function calls, each with the output the backend returned."""
    path = os.path.join(ROOT, "conversations", conversation_id, "transcripts",
                        "annotated-transcript.json")
    items = load_json(path)["responses_create_params"]["input"]
    outputs = {i["call_id"]: i["output"] for i in items if i.get("type") == "function_call_output"}
    return [
        {"call_id": i["call_id"], "name": i["name"],
         "arguments": json.loads(i["arguments"]), "output": json.loads(outputs[i["call_id"]])}
        for i in items if i.get("type") == "function_call"
    ]


def conversations_with_db() -> list[str]:
    base = os.path.join(ROOT, "conversations")
    return sorted(c for c in os.listdir(base)
                  if os.path.exists(os.path.join(base, c, "state", "db.json")))


def check_conversation(conversation_id: str) -> list[str]:
    env = Environment.for_conversation(conversation_id)
    problems = []
    for call in recorded_calls(conversation_id):
        step = env.call(call["name"], call["arguments"])
        if not step.ok:
            problems.append(f"{call['call_id']} {call['name']}: {step.status} {step.output}")
        elif canonical(step.output) != canonical(call["output"]):
            problems.append(f"{call['call_id']} {call['name']}: output differs\n"
                            f"      recorded {canonical(call['output'])[:400]}\n"
                            f"      got      {canonical(step.output)[:400]}")
    expected = load_json(os.path.join(ROOT, "conversations", conversation_id, "state", "db_after.json"))
    for change in row_changes(expected, env.db)[:10]:
        problems.append(f"end state: {change['table']}[{change['key']}] {change['kind']} "
                        f"{change['columns'] or ''} vs db_after.json")
    return problems


def check_task(conversation_id: str) -> list[str]:
    try:
        task = find_task(conversation_id)
    except KeyError:
        return ["no task in domains/<domain>/tasks.json"]
    trajectory = recorded_trajectory(conversation_id)
    calls = [(e["name"], e["arguments"]) for e in trajectory if e["kind"] == "tool"]
    actions = [(a["name"], a["arguments"]) for a in task["evaluation_criteria"]["actions"]]
    problems = [] if actions == calls else ["task actions differ from the recorded calls"]

    scored = evaluate(task, trajectory)
    if scored["reward"] != 1.0:
        unmet = [c["info"] for c in scored["communicate_checks"] if not c["met"]]
        problems.append(f"recorded conversation scores {scored['reward_breakdown']}"
                        + (f", unsaid: {unmet}" if unmet else ""))

    write_tools = Environment.for_conversation(conversation_id).write_tools
    reads = [e for e in trajectory if e["kind"] == "tool" and e["name"] not in write_tools]
    if evaluate(task, trajectory + reads)["reward"] != 1.0:
        problems.append("issuing the reads again changes the score")
    for i, event in enumerate(trajectory):
        if event["kind"] == "tool" and event["name"] in write_tools:
            if evaluate(task, trajectory[:i] + trajectory[i + 1:])["reward_breakdown"]["DB"] != 0.0:
                problems.append(f"leaving out {event['call_id']} {event['name']} still passes DB")
    return problems


def main(argv: list[str]) -> int:
    failed = 0
    for conversation_id in argv or conversations_with_db():
        problems = check_conversation(conversation_id)
        problems += check_task(conversation_id) if not problems else []
        calls = len(recorded_calls(conversation_id))
        print(f"{conversation_id:44} {'PASS' if not problems else 'FAIL'} ({calls} calls)")
        for problem in problems:
            print(f"    {problem}")
        failed += bool(problems)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
