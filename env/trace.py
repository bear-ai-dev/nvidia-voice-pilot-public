#!/usr/bin/env python3
"""Run a recorded conversation against its JSON backend and write a trace.

The trace is what demo/ draws: every utterance with its audio time, and every
tool call with the output the backend returned, the rows it read and the rows
it changed. It also scores the recorded run and three variations of it with
env/evaluate.py, to show what the reward does and does not tolerate:

  extra reads      every read call issued a second time      -> must stay 1.0
  skipped write    the last state-changing call left out     -> DB fails
  unsaid fact      the agent's lines carrying one required
                   fact removed                              -> COMMUNICATE fails

    python3 env/trace.py <conversation_id> [...] --out demo/traces
"""
from __future__ import annotations

import argparse
import json
import os

from evaluate import communicate_checks, evaluate, find_task, load_tasks, recorded_trajectory, run
from runtime import ROOT, load_json


def touched_rows(db_before: dict, steps: list) -> dict[str, dict]:
    """Initial rows of every table a step read by key or wrote, for display."""
    shown: dict[str, dict] = {}
    for step in steps:
        for table, read in step.reads.items():
            # An unfiltered query is a walk over the whole table: shown as a scan.
            keys = set(read["keys"]).union(*(q["matched"] for q in read["queries"] if q["where"]))
            for key in sorted(keys):
                if key in db_before.get(table, {}):
                    shown.setdefault(table, {})[key] = db_before[table][key]
        for change in step.writes:
            if change["before"] is not None:
                shown.setdefault(change["table"], {})[change["key"]] = change["before"]
    return shown


def variations(trajectory: list[dict], task: dict, write_tools: set[str]) -> list[dict]:
    reads = [e for e in trajectory if e["kind"] == "tool" and e["name"] not in write_tools]
    writes = [i for i, e in enumerate(trajectory) if e["kind"] == "tool" and e["name"] in write_tools]
    out = [{
        "name": "Every read issued twice",
        "expect": "still passes: looking is free",
        "trajectory": trajectory + [dict(e) for e in reads],
    }]
    if writes:
        skipped = trajectory[writes[-1]]
        out.append({
            "name": f"Skip the last write ({skipped['name']})",
            "expect": "DB fails: the end state no longer matches",
            "trajectory": trajectory[:writes[-1]] + trajectory[writes[-1] + 1:],
        })
    required = task["evaluation_criteria"].get("communicate_info") or []
    if required:
        fact = required[0]
        silenced = [e for e in trajectory if not (
            e["kind"] == "say" and e["role"] == "assistant"
            and communicate_checks([e], [fact])[0]["met"])]
        out.append({
            "name": f"Agent never says “{fact}”",
            "expect": "COMMUNICATE fails: a required fact went unsaid",
            "trajectory": silenced,
        })
    return out


def build(conversation_id: str) -> dict:
    task = find_task(conversation_id)
    trajectory = recorded_trajectory(conversation_id)
    env, steps = run(conversation_id, trajectory)
    db_before = load_json(os.path.join(ROOT, "conversations", conversation_id, "state", "db.json"))

    step_iter = iter(steps)
    events = []
    for event in trajectory:
        if event["kind"] == "tool":
            step = next(step_iter)
            event = {**event, "status": step.status, "output": step.output,
                     "matches_recording": json.dumps(step.output, sort_keys=True)
                     == json.dumps(event["recorded_output"], sort_keys=True),
                     "write_tool": step.name in env.write_tools,
                     "reads": step.reads, "writes": step.writes}
            event.pop("recorded_output")
        events.append(event)

    scored = evaluate(task, trajectory)
    return {
        "conversation_id": conversation_id,
        "domain": env.domain,
        "task": {k: task[k] for k in ("description", "user_scenario")}
        | {"communicate_info": task["evaluation_criteria"]["communicate_info"],
           "nl_assertions": task["evaluation_criteria"]["nl_assertions"],
           "reward_basis": task["evaluation_criteria"]["reward_basis"]},
        "tables": {table: len(rows) for table, rows in sorted(db_before.items())},
        "rows": touched_rows(db_before, steps),
        "events": events,
        "evaluation": scored,
        "variations": [
            {"name": v["name"], "expect": v["expect"],
             "result": {k: evaluate(task, v["trajectory"])[k] for k in ("reward", "reward_breakdown")}}
            for v in variations(trajectory, task, env.write_tools)
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("conversations", nargs="*")
    parser.add_argument("--out", default=os.path.join(ROOT, "demo", "traces"))
    args = parser.parse_args()
    ids = args.conversations or [t["id"] for t in load_tasks()]
    os.makedirs(args.out, exist_ok=True)
    for conversation_id in ids:
        trace = build(conversation_id)
        with open(os.path.join(args.out, f"{conversation_id}.json"), "w") as fh:
            json.dump(trace, fh, indent=1, ensure_ascii=False)
            fh.write("\n")
        print(f"{conversation_id:44} reward {trace['evaluation']['reward']}  "
              + "  ".join(f"[{v['name']}: {v['result']['reward']}]" for v in trace["variations"]))
    write_index(args.out)


def write_index(out: str) -> None:
    """List every trace in the output directory for the demo's call picker."""
    entries = []
    for name in sorted(os.listdir(out)):
        if not name.endswith(".json") or name == "index.json":
            continue
        trace = load_json(os.path.join(out, name))
        domain, conversation_id = trace["domain"], trace["conversation_id"]
        title = conversation_id.removeprefix(f"{domain}-").replace("-", " ")
        entries.append({"id": conversation_id, "domain": domain,
                        "title": f"{domain.capitalize()}: {title}"})
    with open(os.path.join(out, "index.json"), "w") as fh:
        json.dump(entries, fh, indent=1)
        fh.write("\n")


if __name__ == "__main__":
    main()
