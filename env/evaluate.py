"""Score a run the way tau2-bench does: reward = DB x COMMUNICATE.

DB           Replay the task's reference actions on a fresh copy of the
             conversation's database and hash the result; the run passes when
             its own end state hashes the same. Any route to an equivalent end
             state passes, and the reference actions are never compared to the
             run's calls. Columns a read tool writes as a side effect (read
             cursors, lazy status transitions, listed as READ_SIDE_EFFECTS in the
             domain's tools.py) are left out of the hash, so looking is free.

COMMUNICATE  Every string in communicate_info must appear in some assistant
             message, compared as tau2 does: lowercased, commas removed from the
             message, plain substring.

nl_assertions are carried in the task files for an LLM judge, as in tau2, and do
not gate the reward here.

A trajectory is the list of events a run produced, in order:
    {"kind": "say", "role": "assistant" | "user", "text": "..."}
    {"kind": "tool", "name": "...", "arguments": {...}}
"""
from __future__ import annotations

import hashlib
import json
import os

from runtime import ROOT, Environment, load_json

def load_tasks(domain: str | None = None) -> list[dict]:
    domains = [domain] if domain else sorted(os.listdir(os.path.join(ROOT, "domains")))
    tasks = []
    for name in domains:
        path = os.path.join(ROOT, "domains", name, "tasks.json")
        if os.path.exists(path):
            tasks.extend(load_json(path))
    return tasks


def find_task(conversation_id: str) -> dict:
    for task in load_tasks():
        if task["id"] == conversation_id:
            return task
    raise KeyError(f"no task for {conversation_id!r}")


# -- the recorded conversation as a trajectory --------------------------------

def recorded_trajectory(conversation_id: str) -> list[dict]:
    """The recorded call as events, with audio times from the event metadata."""
    path = os.path.join(ROOT, "conversations", conversation_id, "transcripts",
                        "annotated-transcript.json")
    recording = load_json(path)
    inputs = recording["responses_create_params"]["input"]
    items = [i for i in inputs
             if i.get("role") != "system" and i.get("type") != "function_call_output"]
    outputs = {i["call_id"]: json.loads(i["output"])
               for i in inputs if i.get("type") == "function_call_output"}
    metadata = recording["event_metadata"]
    if len(items) != len(metadata):
        raise ValueError(f"{conversation_id}: {len(items)} events but {len(metadata)} metadata rows")

    events = []
    for item, meta in zip(items, metadata):
        if item.get("type") == "function_call":
            events.append({
                "kind": "tool", "call_id": item["call_id"], "name": item["name"],
                "arguments": json.loads(item["arguments"]),
                "recorded_output": outputs[item["call_id"]],
                "label": meta.get("source_label"), "at": meta.get("placement_seconds"),
            })
        else:
            content = item["content"]
            text = content if isinstance(content, str) else " ".join(
                part.get("text", "") for part in content)
            audio = meta.get("audio_reference") or {}
            events.append({
                "kind": "say", "role": item["role"], "speaker": meta.get("speaker"),
                "text": text, "start": audio.get("start_seconds"), "end": audio.get("end_seconds"),
                "emotions": [a["label"] for a in meta.get("annotations", []) if a.get("type") == "emotion"],
            })
    return events


# -- running and scoring -------------------------------------------------------

def run(conversation_id: str, trajectory: list[dict]) -> tuple[Environment, list]:
    """Execute a trajectory's tool calls in order against a fresh database."""
    env = Environment.for_conversation(conversation_id)
    steps = [env.call(event["name"], event["arguments"])
             for event in trajectory if event["kind"] == "tool"]
    return env, steps


def db_hash(db: dict, read_side_effects: dict) -> str:
    kept = {}
    for table, rows in db.items():
        excluded = read_side_effects.get(table, ())
        if excluded == "*":
            continue
        kept[table] = {key: {c: v for c, v in row.items() if c not in excluded}
                       for key, row in rows.items()}
    canonical = json.dumps(kept, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def communicate_checks(trajectory: list[dict], required: list[str]) -> list[dict]:
    said = [event["text"].lower().replace(",", "") for event in trajectory
            if event["kind"] == "say" and event["role"] == "assistant"]
    return [{"info": info, "met": any(info.lower() in text for text in said)} for info in required]


def evaluate(task: dict, trajectory: list[dict]) -> dict:
    conversation_id = task["annotations"]["conversation_id"]
    criteria = task["evaluation_criteria"]

    gold, _ = run(conversation_id, [{"kind": "tool", **action} for action in criteria["actions"]])
    predicted, steps = run(conversation_id, trajectory)
    side_effects = getattr(predicted.tools, "READ_SIDE_EFFECTS", {})
    target_hash = db_hash(gold.db, side_effects)
    predicted_hash = db_hash(predicted.db, side_effects)
    checks = communicate_checks(trajectory, criteria.get("communicate_info") or [])

    breakdown = {
        "DB": 1.0 if predicted_hash == target_hash else 0.0,
        "COMMUNICATE": 1.0 if all(check["met"] for check in checks) else 0.0,
    }
    reward = 1.0
    for kind in criteria["reward_basis"]:
        reward *= breakdown[kind]
    return {
        "reward": reward,
        "reward_breakdown": breakdown,
        "db": {"target_hash": target_hash, "predicted_hash": predicted_hash},
        "communicate_checks": checks,
        "tool_calls": len(steps),
        "failed_calls": sum(1 for step in steps if not step.ok),
    }
