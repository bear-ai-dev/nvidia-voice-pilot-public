# Runnable tau2 environments

Ten of the recorded conversations can be run, not just read. Each one has a
JSON database, a set of tools that read and write it, and a task file in the
shape tau2-bench uses. A simulated agent can call the tools, the database
changes as it would in the real backend, and the run is scored the way
Tau-voice scores a call: did the records end in the right state, and did the
agent say the required facts.

## Layout

It follows tau2's domain layout: a policy, typed tools, and a task file per
domain.

| Path | What it is |
|---|---|
| `domains/<domain>/policy.md` | The policy the agent works under (unchanged). |
| `domains/<domain>/tool_registry.json` | Tool names and JSON-Schema argument and result contracts (unchanged). Every argument is validated against it before a tool runs. |
| `domains/<domain>/tools.py` | The tools. Each is a function `(db, args) -> result` that reads and writes the JSON database. |
| `domains/<domain>/tasks.json` | One tau2 task per conversation. |
| `conversations/<id>/state/db.json` | The backend at the start of the call: every table, keyed by row. |
| `conversations/<id>/state/db_after.json` | The backend after the recorded calls and the call's outside events. |
| `conversations/<id>/state/events.json` | Things other people do during the call, each at its own time: the customer entering a texted code, a merchant retrying a charge, an email being delivered. |
| `conversations/<id>/state/ids.json` | The seed for the ID generator, so a replay issues the IDs the recording used. |
| `env/` | The runtime, the tau2-style scorer, and the checks. Standard library only. |

Domains covered: banking (4 calls), retail (3), pharmacy, airline and telecom
(1 each). The other four conversations in the dataset do not have a runnable
backend yet.

## The database

`db.json` is `{table: {row_key: row}}`. A row key is the row's key columns
joined with `|` (each domain's `tools.py` lists them in `KEY_COLUMNS`). The
databases are realistic in size, from about 1,800 to 5,300 rows across 15 to 26
tables. They include the other customers a real register holds, such as a
second person with the caller's name, so an agent that trusts a name alone can
pick the wrong record.

The data is synthetic. The records the recorded call touches match what the
tools returned in the recording, field for field; everything else is generated
to be consistent with it.

The backend behaves like a system rather than a script
([docs/BACKEND_REALISM.md](../docs/BACKEND_REALISM.md)):

- **Reads never change data.** Looking at a record returns the whole record,
  the same way every time.
- **Time moves.** The runtime's clock is the call's start time
  (`call_started_at` in the `scenario` table) plus how far into the call a tool
  call happens. Tools read it with `toolkit.now(db)` and stamp writes with it.
- **Other people act on their own schedule.** `events.json` lists what happens
  outside the agent's tools. Before each tool call the runtime applies every
  event that has come due and whose conditions hold; an event that has to wait
  (a hotel retrying a charge only once the card is unblocked) happens, and is
  stamped, when its conditions first hold.
- **New records get new IDs.** A record exists only once the action that
  creates it happens, and its ID comes from `toolkit.new_id`, seeded from
  `ids.json`.

## The task file

Each task has the fields tau2 uses:

- `description`: what a correct agent achieves, and what makes it hard.
- `user_scenario`: instructions for an LLM playing the caller (`reason_for_call`, `known_info`, `unknown_info`, `task_instructions`, plus a `persona` taken from the recording).
- `evaluation_criteria.actions`: the recorded tool calls, as one reference path. As in tau2, these set the target end state; the agent does not have to repeat them.
- `evaluation_criteria.communicate_info`: short facts the agent must say, such as an amount or a deadline.
- `evaluation_criteria.nl_assertions`: behaviour an LLM judge can check, such as "Agent tells the caller that approval is not guaranteed."
- `evaluation_criteria.reward_basis`: `["DB", "COMMUNICATE"]`.
- `annotations`: pointers to this conversation's database, transcript and audio.

## Scoring

`env/evaluate.py` scores a run as tau2 does: reward = DB x COMMUNICATE.

- **DB.** Replay the reference actions on a fresh copy of `db.json` at their
  recorded times, let the call's outside events play out, hash the result, and
  compare it with the hash of the run's end state. Any route to the same end
  state passes. Columns that only record when something happened are listed in
  `CLOCK_COLUMNS` in each `tools.py` and left out of the hash, so an agent is
  judged on what it did, not the second it did it.
- **COMMUNICATE.** Each `communicate_info` string must appear in an agent
  message, lowercased and with commas removed, exactly as tau2 checks it.

## Running it

```bash
python3 env/replay.py
```

For each conversation this checks that:

1. every recorded tool call, made at its recorded time, returns exactly the recorded output, which also fits the tool's result schema,
2. no read tool writes anything,
3. the database afterwards, with the outside events applied, equals `db_after.json`,
4. the task's actions are the recorded calls,
5. the recorded conversation scores 1.0,
6. repeating every read still scores 1.0, and leaving out any write fails the DB check.

To drive an environment from your own agent loop:

```python
import sys; sys.path.insert(0, "env")
from runtime import Environment
from evaluate import evaluate, find_task

env = Environment.for_conversation("pharmacy-travel-refill")
step = env.call("lookup_patient", {"full_name": "Miles Carter", "date_of_birth": "1988-06-14"},
                at=24)          # seconds into the call; omit it and the clock moves 20 s per call
step.status, step.output   # 200 and the result the agent sees
step.reads, step.writes    # the records the call read and changed
step.events                # outside events that happened before this call
env.finish()               # let the rest of the call's events play out

task = find_task("pharmacy-travel-refill")
evaluate(task, [
    {"kind": "tool", "name": "lookup_patient", "arguments": {...}},
    {"kind": "say", "role": "assistant", "text": "Your copay is $15."},
])
```

A failed call returns the error the recorded backend would have returned (a
`409` refusal, a `404` for an unknown record, or a `400` for arguments that do
not fit the registry) and leaves the database unchanged.

## Where the tools came from

The tools were first written as a PostgreSQL service with one Docker image per
task, then ported to plain Python over JSON. The port was checked against the
original service by sending both the same calls, including refusals, repeated
reads and alternative routes, and requiring the same status, output and row
changes. Where the original service had a bug that the recordings do not
depend on, such as returning a server error for an unknown id, the port returns
the error the code intended instead.

The tools were then revised so they return records rather than the agent's
conclusions: no recommendations, verdicts, answers keyed to the caller's
question, disclaimers or speech-ready times. The recorded outputs in the
annotated transcripts were updated to match, so they are still exactly what the
tools return. [docs/TOOL_OUTPUT_REVISIONS.md](../docs/TOOL_OUTPUT_REVISIONS.md)
lists every change.
