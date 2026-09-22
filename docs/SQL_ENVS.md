# SQL-backed voice-agent environments

This document is the construction contract for the environments under `tasks/`.
Each task turns one recorded conversation into a running backend: a PostgreSQL
database seeded to the state the call started from, and an HTTP server that maps
that domain's tools onto real SQL against it.

The recorded conversation is the specification. A task is correct when replaying
its recorded tool calls against the running server reproduces every recorded
tool result byte for byte and leaves the database in the recorded final state.

## Why a database instead of authored payloads

The published dataset carries tool results as hand-authored JSON, and the state
snapshots under `conversations/<id>/state/` are derived backwards from those
results. Nothing executes. That is sufficient for next-action prediction on the
recorded path and useless the moment an agent deviates, because there is no
world to deviate into.

These environments invert the derivation. Backend state is primary, tool results
are computed from it, and an agent that takes an unrecorded action gets an answer
that follows from the same data the recorded actions read.

## Layout

One directory per conversation, numbered to match the trajectory order in
`exports/conversation_manifest.json`:

```
tasks/NN-<conversation-id>/
  task.toml                      Harbor manifest
  instruction.md                 agent-facing prompt: the caller's goal, no answers
  README.md                      schema, seed rationale, verification results
  .gitignore                     .local/
  environment/
    Dockerfile
    task-init.sh                 initdb -> schema -> seed -> tool server -> .ready
    .dockerignore                keeps gen_seed.py out of the image
    sql/
      001_schema.sql             DDL: tables, foreign keys, CHECK-enforced lifecycles
      002_reference.sql          catalogs: products, stores, airports, offers, knowledge base
      003_population.sql         generated bulk population and distractors
      004_scenario.sql           the entities this conversation touches
    server/
      app.py                     HTTP front end
      db.py                      connection handling
      tools.py                   one handler per tool
      projection.py              include-section assembly, result key ordering
      registry.json              copy of domains/<domain>/tool_registry.json
    gen_seed.py                  author-time generator for 003_population.sql
    verifier-data/               root-only, mode 0700
      gold_calls.json            ordered calls with expected outputs
      expected_final_state.json  required facts, asserted as a subset
      state_digest.json          per-row hashes of initial and gold end states
      grading.json               damage policy: volatile columns, ignored
                                 and append-tolerated tables
      communicate_info.json      facts the caller must be told
    workspace/                   agent-visible: policy, plus a writable transcript
  tests/
    test.sh                      grading entry point: scores what a run left behind
    grade.py                     db x communicate reward, damage detection
    env_check.sh                 conformance entry point: resets and replays gold
    replay.py                    drives gold calls, compares outputs byte-exact
    score_conformance.py         conformance verdict
    statecheck.py                shared state comparison and row digests
    make_digest.py               author-time generator for state_digest.json
  solution/
    solve.sh                     oracle: the gold calls plus the required speech
```

Everything under `tests/` is byte-for-byte identical across all ten tasks. The
per-task variation lives in `verifier-data/` instead: the damage policy, the
communication requirements, and the digest. Keeping the graders identical means a
change to how scoring works is one edit propagated ten times rather than ten
opportunities to drift.

Tasks are self-contained. There is no shared library; a task carries its own
copy of the schema and server even where a sibling task in the same domain is
near-identical. This follows the xai repo's rule that two task workspaces from
one extraction must be independently reviewable, and it lets a task diverge
without coordinating with its siblings.

## Container

Base image `postgres:16-bookworm`, which is multi-arch, so an arm64 build is
amd64-compatible. Added at build time: `python3`, `python3-psycopg2`, `curl`,
`jq`, `ripgrep`.

Two listeners, both on loopback only:

- PostgreSQL on `127.0.0.1:5432`
- the tool server on `127.0.0.1:8080`

`task-init.sh` is the `ENTRYPOINT` and `CMD` is `["--wait"]`. It runs `initdb`,
starts PostgreSQL, applies `001` through `004` in order, starts the tool server,
polls `/health`, then touches `/tmp/task-infra/.ready`, which is what the Harbor
healthcheck tests. With `--wait` it hands off to `tail -f /dev/null`; with any
other argument it `exec`s that argument, so the harness can replace the command.

Seeding happens at container start rather than at build time. The image holds the
SQL; the database is constructed fresh on boot. Two runs of the same image
therefore start from identical state, and a run can be reset without a rebuild.

## Tool server contract

- `POST /tools/{tool_name}` — request body is the tool's arguments object,
  response body is the tool's result object. `200` on success. `400` with
  `{"error": {"type": ..., "message": ...}}` when arguments fail the registry
  schema, `404` for an unknown tool, `409` for a domain refusal (a mutation whose
  preconditions are not met).
- `GET /health` — `{"status": "ok"}` once the database is reachable.
- `GET /tools` — the registry, so an agent can discover the tool set.
- `GET /_admin/state` — canonical JSON of every table.
- `POST /_admin/snapshot` — writes that JSON to `/out/final_state.json`.

The two admin routes require `Authorization: Bearer <token>`. The token is
generated at startup and written to `/var/lib/task-data/admin_token`, mode 0600
root-owned. An agent account cannot read it, so an agent can observe the world
only through the tools.

The database itself is closed to the agent account too, which matters because the
agent runs in the same container and the grader scores end state: an agent with a
SQL session could write the expected rows directly and score 1.0 without calling a
tool. PostgreSQL uses peer auth on its socket, so only the `postgres` OS user gets
in there, and scram over TCP. The app role is not a superuser, and its password is
generated per container into `/var/lib/task-data/db_dsn`, mode 0600 root-owned,
where the tool server reads it. It is never in the image environment, which the
agent's processes would inherit.

Argument validation runs before any SQL, against the registry's `parameters`
schema, so `additionalProperties: false`, `required`, and every enum are enforced
at the boundary rather than incidentally by a query returning no rows.

## Reproducing recorded results exactly

Byte-exactness is the binding constraint and four properties of the recorded data
work against it.

### Deterministic identifiers

Recorded results contain specific references: `WST481662`, `ending-8821`,
`B9RT6M`, `override-lost-medication`. Real backends allocate these from a
sequence, so the schema carries an `id_allocator` table keyed by entity type and
scope, holding the next value to issue. It is seeded so that the first delivery
trace opened on the scenario's order allocates the recorded case id. Allocation
is a real `UPDATE ... RETURNING`, so a second trace on the same order gets the
next value rather than the same one.

Never generate an identifier randomly or from a timestamp. If a result contains
an id, that id comes from the database.

### Progressive section reads

The retail conversations call `get_order` on the same order several times and
legitimately receive successively deeper read models, because the enacted agent
was querying a system that serves more detail on repeat lookups.

Model this as state, not as a script: `section_read_cursor(order_reference,
section, reads_served)` incremented on each read, and `section_view(
order_reference, section, view_index, payload)` holding what to emit at each
depth. After the deepest view is reached it repeats. A `NULL` payload omits the
section entirely, which is how a first read can legitimately return no
`eligible_resolutions` at all.

This is deliberately inspectable: the read count is a row an operator can query,
not a Python list being mutated inside the server process.

### Human-relative strings inside results

Recorded results contain values like `"15:18 yesterday"`, `"18:00 tomorrow"`,
`"August 2"`, and `"almost three hours"`. `docs/DATA_QUALITY.md` documents these
as deliberately non-ISO, because the agent must read them back verbatim and a
caller does not hear an ISO timestamp.

Store both representations: a `*_display` column carrying the exact string the
tool emits, and a typed column carrying the resolved value against the
conversation's `scenario_time`. The tool emits the display string. The typed
column exists so the data is queryable and so a future dynamic clock can
regenerate the display form.

### Polymorphic knowledge-base results

Banking's `search_knowledge_base` accounts for 13 of the 85 recorded calls and
its result schema is the typed union of every observed payload — roughly twenty
optional content shapes that never co-occur.

This one table is a deliberate exception to full normalization:
`kb_records(record_id, effective_at, query_pattern, payload JSONB)`. The handler
matches the query against `query_pattern` and merges `payload` into the result.
Decomposing a union that never co-occurs into twenty tables would add schema
surface and no fidelity.

Everywhere else, results are assembled from normalized columns.

### Result key ordering

Handlers build result dictionaries in the order the registry declares the
properties, and comparison canonicalizes with `json.dumps(sort_keys=True)`. Key
order therefore cannot silently drift, and a missing or extra field fails loudly.

### Number form

JSON distinguishes `15` from `15.0`, and the recorded results use both forms in
places where neither is a rounding artifact. A pharmacy copay of fifteen dollars
is recorded as `15`; an airline fee of zero is recorded as `0.0`.

Column type cannot decide this, because both of those are money and both would
naturally be `NUMERIC`. So the choice is made explicitly at each call site with
`projection.as_int` or `projection.as_float`, checked against the recorded output
field by field. A handler that reaches for a bare `float()` or `int()` is a
handler whose number form has not been thought about.

This is worth the tedium: it is the single most common cause of a replay failure,
and it fails loudly rather than drifting.

## Seed volume

Each task's database holds a real population, not just the handful of rows the
conversation touches. `gen_seed.py` runs at author time with a fixed RNG seed and
emits `003_population.sql`; it never enters the image.

Targets per task:

- 80 to 120 customers or patients, one of which is the conversation's
- 250 to 400 primary records: orders, prescriptions, reservations, or lines
- proportionate children: 600 to 1200 line items, 400 to 800 payments and
  refunds, 300 to 600 carrier scans, transactions, or usage samples
- 40 to 80 cases, traces, sessions, or notices unrelated to the scenario
- fully populated catalogs: around 40 airports, 200 flights, 80 products with
  variants, 25 stores, 12 card products, 30 knowledge-base records, 15 add-on
  offers

The population exists to make lookups non-trivial, so it includes deliberate
distractors: customers sharing a surname with the target, order references with
colliding digit suffixes, out-of-stock variants, expired and ineligible offers,
override reasons that deny, and identity factors that fail verification. A
`lookup_customer` that can only ever return one row is not testing anything.

## Two layers: conformance and grading

There are two different questions to answer, they need different machinery, and
conflating them produces a verifier that looks strict and measures nothing.

**Is the environment faithful?** Does this SQL backend reproduce what the
recording says the real tools produced? The recorded call sequence is the
specification, and the answer must be byte-exact. This is a regression test on
the environment, it is run at build time, and it does not score an agent.

**Did the agent handle the call well?** This must not require the gold path. An
agent that verifies identity in a different order, skips a lookup it did not
need, or reads a record twice where the recording read it once has not done
anything wrong. Only outcomes can be graded: what is true in the database
afterwards, and what the caller was told.

τ-bench reaches the same split from the other direction, and its
[evaluation docs](https://github.com/sierra-research/tau2-bench/blob/main/docs/evaluation.md)
are worth reading. Its gold trajectory exists only to *derive* a target end
state; the agent's own path is never compared against it, and its
`RewardType.ACTION` — which does compare paths — is used in none of the airline,
retail, or telecom tasks. Its `Environment.set_state(strict=True)` does compare
replayed tool outputs against recorded ones, exactly as the conformance layer
here does, and it is likewise not part of any score.

### Layer 1: conformance, `tests/env_check.sh`

Rebuilds the database from `001` through `004`, POSTs each gold call in recorded
order through `tests/replay.py`, and compares each response to the recorded
output after canonical normalization. Then checks the resulting state against
`verifier-data/expected_final_state.json`. Writes
`/logs/verifier/conformance.json`. It must pass on every task, and it says
nothing about any agent.

This layer is stricter than τ-bench's equivalent on purpose. τ-bench skips
non-mutating calls during strict replay and compares parsed values, so `15` and
`15.0` compare equal there. Here every call including reads is replayed and the
comparison is byte-exact on canonical JSON, which is what surfaced the
number-form defects while these backends were being written.

### Layer 2: grading, `tests/test.sh`

Runs after an agent has had the container, and **does not reset the database** —
resetting is what made the earlier version of this file score the environment
instead of the run. Reward is a product of independent components, following
τ-bench:

```
reward = db_reward * communicate_reward
```

`db_reward` is 1.0 when both hold:

- **Required facts.** Every field in `expected_final_state.json` is present with
  that value — a subset assertion, so the database knowing more is fine. This
  states outcomes rather than routes, but see the caveat below: stating an
  outcome is not automatic just because the document only names fields.
- **No collateral damage.** Nothing the agent changed, outside what the gold path
  also changed, may differ from the initial state.

`communicate_reward` is 1.0 when every entry in `verifier-data/communicate_info.json`
is satisfied by the agent's utterances.

### Fields that record a read rather than an outcome

Naming a field instead of a call does not by itself make an assertion
route-independent, and this is the easiest way to reintroduce the very
path-dependence the grading layer exists to remove.

Several tools model something happening in a channel no tool can observe — a
customer opening a secure session in online banking, completing a trusted-channel
challenge, a notification progressing through delivery. The handler writes that
transition the first time the record is *read*. So a self-service session nobody
looked at reads back `issued`, and the same session read once reads
`open_not_submitted`.

Asserting either value pins whether the agent made an optional read. Whichever
one the recording happened to leave behind, the other route fails, and it fails
on a field that describes no part of what the run achieved. Write such a field as
an explicit set instead:

```json
"status": { "_any_of": ["issued", "open_not_submitted"] }
```

That keeps the part that matters — the session is neither expired nor submitted —
and drops the part that only records who looked. It counts as one field in
`state_fields_checked`, and it satisfies the conformance layer too, since gold's
own value is one of the listed ones.

A read whose effect is *forced* by a later write is different, and must be
asserted exactly rather than widened. In `01-banking-*`, `update_customer_email`
refuses unless the trusted-channel confirmation is already `verified`, and only
reading the confirmation moves it there — so every route that successfully
changes the address has necessarily polled, and `verified` is a real precondition
rather than a stray look.

Note that such a column is usually *also* read-volatile for the damage check, and
this is not a contradiction: a stranger's confirmation may be read freely, while
this customer's must be verified. The two mechanisms operate on different
questions, one on the assertion and one on the row hash, so they coexist. But the
combination is indistinguishable from an authoring mistake, so the task declares
it and says which write forces it, in `expected_final_state.json`:

```json
"_forced_read_fields": {
  "channel_confirmations.status":
    "update_customer_email refuses unless the confirmation is already verified,
     and only get_trusted_channel_confirmation moves it there."
}
```

Keys are `table.column`, matching the granularity of `read_volatile_columns`. The
justification is required — an empty or whitespace one does not satisfy the audit
— because the point is a reviewed decision a reader can check and disagree with,
not a way to silence a warning. Underscore-prefixed keys are directives, so this
is inert to both the conformance and grading layers.

`tasks/audit_read_sensitivity.py` finds this statically, in two classes:

- **Defects**, which fail the run. The assertion pins a column the task itself
  declares under `read_volatile_columns`, so the task is asserting a value it has
  already stated changes on read. That is always wrong. Fix it with `_any_of`, or
  drop the field if it is a bare read counter with no bounded set — a
  `reads_served` or `calls_served` cannot be widened, only removed.
- **Unverified**, which print for a human. The assertion lands on a table excluded
  from the damage check wholesale, where no per-column information exists to
  judge against. Most are genuinely route-independent, so these are not failures.

The practical consequence: a task that declares its volatile columns narrowly
gets a precise gate, and one that excludes whole tables gets a reading list. That
asymmetry is deliberate, and is a second reason to prefer `read_volatile_columns`.

### Asserting an allocator counter

`id_allocator` is excluded from the damage check but its `next_value` is asserted
by the three retail tasks, which looks contradictory and is worth stating as a
rule. Such a counter is safe to assert only while **every** allocation site for
that entity is a write tool whose extra row is itself damage. Retail satisfies
this: `support_case` and `order` move only through `open_delivery_trace`,
`open_refund_trace` and `create_replacement_order`, and a second case or
replacement order is caught on its own account, so no correct route can move the
counter.

`09-airline-*` deliberately asserts no allocator, and that asymmetry is the point:
there, `search_flights` and `calculate_itinerary_price` allocate
`flight_search` and `fare_quote` identifiers, so an agent that checks one more
route moves a counter without doing anything wrong. Asserting it would have made
"extra reads are free" impossible, in the same way telecom's `calls_served`
assertion did before it was removed.

The cost of keeping it where it is safe is over-determination rather than
unfairness: a control that meddles with an unrelated customer fails both the
damage check and the counter, so its output reports two causes where one would
read more clearly. That is a diagnostic wart, not a wrong verdict. Before adding
an allocator assertion to a task, check every `allocate_id` call site for that
entity; if any of them is reachable from a read or from an append-tolerated retry,
do not assert it.

### Asserting a table the damage check ignores

The two cases above are the whole of what `audit_read_sensitivity.py` prints as
*unverified*, and both have been settled, so a reader meeting that output should
not have to re-derive them. Across the ten tasks the class is exactly
`id_allocator` on the three retail tasks and `fare_quotes` on `09-airline-*`.

`fare_quotes` is the more interesting one and shows how to make such an assertion
safely. The table is damage-excluded because `calculate_itinerary_price` inserts a
row the first time an itinerary is priced and rewrites it on every later call, so
repricing — which the desk does whenever the party size, bags or insurance answer
changes — necessarily modifies an existing row. What the gold quote *holds* is
still asserted field by field, and that is legitimate because the asserted fields
are all functions of the itinerary rather than of the call: the component amounts,
the plan, the currency, and an `expires_at` derived from the frozen scenario clock.
The one field that does record when the pricing happened, `last_priced_at`, is
deliberately not asserted.

This is verified rather than argued. `calculate_itinerary_price` is a read tool, so
`check_read_freedom.sh` reprices the gold itinerary three extra times after the
oracle has finished and requires the reward to stay 1.0 — which is a direct test
that the surviving `fare_quotes` assertions do not depend on how often the quote
was taken. If a future change makes any asserted column advance on read, that
control fails rather than the defect going unnoticed.

### Known limits of the damage check

- **Shared rows are held to two states, not one.** A row inside the work area may
  end as it started or as the gold path left it; any third value is damage. In
  `09-airline-*` a booking made for an unrelated customer on the same two flights
  decrements the same `flight_availability` rows the gold path decremented, and
  the doubled decrement is now flagged on those rows too. What is still not caught
  is an agent that leaves a work-area row as it started when the outcome needed it
  changed, unless a required fact asserts that column.
- **Wholesale exclusion hides inserts.** Covered above; prefer
  `read_volatile_columns`. Where a table must still be excluded, check whether a
  child table catches what the parent cannot, and say so in the task README
  rather than leaving it to be rediscovered.

### Why the damage check exists

A subset assertion alone is too lax in one specific way: an agent that completes
the refill correctly *and* cancels an unrelated patient's prescription satisfies
every asserted field. τ-bench catches this because it compares a hash of the
whole database, which fails on any difference anywhere.

A whole-database hash is not usable here — the seed is generated, so a hash would
change whenever the population is regenerated, and a mismatch reports "different"
with no indication of what. Instead each task ships `verifier-data/state_digest.json`,
holding a per-row hash of the initial state and of the gold end state:

```json
{"key_columns": {"prescriptions": "prescription_id"},
 "initial":    {"prescriptions": {"prescription-albuterol": "3f9c1a2b8e07"}},
 "gold_final": {"prescriptions": {"prescription-albuterol": "b71e04d5c9aa"}}}
```

Damage is then defined per row rather than over the whole database:

> A row is damage when the agent inserted, deleted, or modified it **and the gold
> path left it untouched**, or when the gold path did touch it and the agent left
> it **in neither the initial nor the gold state.**

Rows the gold path also touched are the agent's work area, so an agent that
reaches the right outcome by a different route is not penalised for the route.
But the work area is not a free zone: a column the required facts happen not to
assert, such as the masked destination a secure session was delivered to, still
has to end where the gold path left it. Rows nothing was supposed to touch are
held to the initial state.

Three exclusions are declared in `verifier-data/grading.json`, in decreasing
order of bluntness. Prefer the narrowest one that works.

- **Read-volatile columns** (`damage.read_volatile_columns`), the narrowest.
  Named columns are dropped before the row is hashed, so the table stays under
  the damage check while the columns a read advances stop mattering — a
  notification's delivery progress, a session's `opened_at`. Use this wherever it
  suffices, because excluding the whole table instead makes an *inserted* row
  invisible too, and an unrequested notification to a customer is precisely the
  consequential side effect this check exists to catch. The exclusion list is
  recorded inside `state_digest.json` and read back from there by the grader, so
  both sides of the comparison hash rows the same way; changing it means
  regenerating the digest with `tests/make_digest.py`.
  When declaring these, check the converse as well: every *write* tool that
  touches the table must still change at least one non-volatile column, or that
  tool's effect becomes invisible. Retail's `send_case_notification` re-firing on
  an existing row survives this test only because it moves `sent_at`, which is
  not volatile — so a seed that stamped a notification at `scenario_time` would
  make an unrequested resend undetectable. Prefer to keep such a column out of
  the volatile list rather than to rely on the seed.
- **Read-side-effect tables** (`damage.ignore_tables`), the blunt instrument.
  Appropriate when the table holds nothing *but* read state — `section_read_cursor`,
  `tool_call_log` — so there is no insert worth catching. `id_allocator` too: an
  extra create bumps the counter, but the row that create produced is itself
  caught. A table with real content and one volatile column belongs in the
  category above instead.
- **Append-tolerated tables** (`damage.append_tolerated`). `claims`, `case_notes`,
  and similar append-only records, where a *new* row is acceptable but modifying
  a pre-existing one is not. An agent that submits a claim twice leaves two claim
  rows; the outcome is asserted through the `latest_claims` view, so the extra row
  is a different route to the same place rather than a fault. τ-bench's hash would
  fail this, and that strictness is a known complaint about it.

A table excluded wholesale is sometimes still covered incidentally by a child
table — a spurious self-service session also inserts into `session_deliveries` —
but incidental cover is worth distrusting even when it holds. Banking originally
relied on exactly that and no longer does: `self_service_sessions` and
`channel_confirmations` are now under the damage check with their two
read-advanced columns declared volatile, so the offending row is named directly.
The confirmation case is why this mattered. No child table covered it, and a
trusted-channel challenge sent to an uninvolved customer — a real message to a
real phone — scored a clean 1.0 under the wholesale exclusion and 0.0 once the
column-level rule replaced it.

### The communicate component

End-state checking structurally cannot see the part of these conversations that
happens in speech. Several tasks turn on it: the pharmacy call is handled wrongly
if the one-time nature of the override goes unmentioned, however correct the
database ends up.

τ-bench uses `communicate_info`, a list of strings that must appear in the
agent's messages by substring match. The same idea here, with one improvement —
each required fact carries alternative surface forms and any one satisfies it:

```json
{"required": [
  {"id": "copay",
   "any_of": ["$15", "15 dollars", "fifteen dollars", "15", "fifteen"],
   "why": "the caller must be told what he will pay at the counter"}]}
```

This stays deterministic and needs no LLM judge, while not failing an agent for
saying "fifteen dollars" instead of "$15". Matching is case-insensitive with
collapsed whitespace.

### Bare figures, and the fences that make them safe

Enumerating whole phrases is not enough on its own, because a phrase list only
covers the orderings somebody thought of. Listing `$15`, `15 dollars` and
`fifteen dollars` still misses `15 USD`, `15.00 dollars`, `fifteen bucks`, a
`fifteen-dollar copay` and "a total of 15" — five natural ways to say the same
thing. Measured on a dozen plausible renderings of the pharmacy copay, the phrase
list alone matched four; that is a false-negative rate the agent cannot see and
cannot avoid, and `instruction.md` explicitly invites it to "say these in your own
words", so failing it for words nobody enumerated breaks a promise the task makes.

The fix is to also list the **bare figure**, which collapses the whole family into
one entry. `15` finds every phrasing above regardless of the unit word or its
position. The same twelve renderings then all match.

A bare form cannot be matched as a plain substring, though, because `15` is inside
`$150`, `62` is inside `1.62`, and `two` is inside `network`. Crediting an agent
for quoting a *different* figure is far worse than missing a phrasing, so bare
forms are fenced while multi-word forms stay plain substrings:

| Form shape | Matched as | Reason |
|---|---|---|
| single number, e.g. `15`, `$62`, `11.8` | not adjacent to another digit, and not followed by a decimal carrying a non-zero | `$15` finds `$15.00` but not `$150` or `$15.09` |
| single word, e.g. `fifteen` | word boundaries | `two` must not be found inside `network` |
| two or more words, e.g. `90-day`, `too soon` | plain substring | several are deliberate stems: `90-day` has to find "90-days" |

Two rules govern when a bare figure may be added at all, and both exist because a
loose anchor silently credits a wrong answer. **It must be the whole value, never
a fragment** — `fifteen` is the number 15, but `five` is a piece of "ninety-five"
and would let "five business days" satisfy a $95 annual fee. **It must be a single
value of ten or more** — `40,000 points` keeps its unit word because a bare
`40000` could be dollars, and `2` is too common in prose to carry a fact alone.
Seven of the nineteen numeric requirements therefore have no bare figure, by
design rather than oversight, and `check_communication_matching.py` lists them.

That script is the guard. It imports the real matcher out of `tests/grade.py`
rather than reimplementing it, so it cannot drift from what is actually scored,
and it tries deliberately wrong figures against every numeric requirement in
every task — 184 of them at present, none accepted. It runs in under a second and
needs no Docker.

The agent's speech arrives at `/workspace/transcript.txt`, which is
agent-writable, or at `$AGENT_TRANSCRIPT` when a harness puts it elsewhere. Plain
text is read whole; JSON is accepted as a message list, in which case only
assistant-role content is considered, so a harness driving a real voice agent can
hand over its own conversation log unmodified.

### Diagnostics that do not gate

`reward.json` also reports, without affecting the reward, how many of the gold
path's write calls the agent made, from `tool_call_log`. This mirrors τ-bench's
`partial_action_reward` and carries the same caveat: it measures similarity to
one reference path, not correctness. An agent can score zero on it and be right.

```json
{"reward": 1.0, "score": 1.0,
 "reward_breakdown": {"db": 1.0, "communicate": 1.0},
 "state_fields_checked": 30, "state_fields_matched": 30,
 "damage_rows": 0,
 "communicate_required": 4, "communicate_met": 4,
 "gold_write_calls_made": 4, "gold_write_calls_total": 4}
```

`test.sh` writes a zero `reward.json` before doing anything else, so no exit path
can leave a stale verdict or none at all.

### Controls a task must demonstrate

| Control | Expected |
|---|---|
| `env_check.sh` conformance replay | every call byte-exact |
| `solution/solve.sh` then `test.sh` | 1.0 |
| Container an agent never touched, then `test.sh` | 0.0 |
| Oracle, then damage an unrelated row, then `test.sh` | 0.0 |
| Oracle with a required fact left unsaid | 0.0 |
| Oracle tool calls with no transcript at all | 0.0 |
| A read-only tool called many extra times | still 1.0 |
| Agent account reads `/var/lib/task-data/` | denied |
| Agent account connects to PostgreSQL, any role, TCP or socket | denied |
| A deliberate number-form defect | `env_check.sh` fails |

The last one is worth doing explicitly: a verifier that has never been observed
failing has not been tested. Rendering the pharmacy copay as `15.0` instead of
`15` fails conformance while leaving the database correct, which is the right
outcome — the wire format is wrong and the state is not.

The read-only control is the one that proves the point of this whole section. It
must stay 1.0, because an agent is allowed to look at things.

Gold outputs, digests, and expected final state live only under
`/var/lib/task-data/verifier/`, mode 0700 root-owned. The annotated transcript is
never copied into the agent-visible workspace, because it contains the answers.

## Extracting final state

After a run, `POST /_admin/snapshot` writes canonical JSON to
`/out/final_state.json`. `task-init.sh --dump-state` produces a
`pg_dump --data-only --column-inserts` alongside it. Mount a host directory at
`/out` to collect both.

The canonical JSON is stable: tables in name order, rows ordered by primary key,
keys sorted. Two runs that end in the same state produce identical bytes, so
final states can be diffed directly.

## Hygiene

- No absolute host paths anywhere in a task.
- No `__pycache__`, `.local/`, or generator output committed.
- `gen_seed.py` excluded from the build context by `.dockerignore`.
- Verify containment against the built image, not against `COPY` lines: an agent
  shell must get permission denied on `/var/lib/task-data` and must not be able to
  open a database session.
