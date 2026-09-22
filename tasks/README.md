# SQL-backed voice-agent environments

Ten runnable environments, one per recorded conversation. Each boots PostgreSQL
seeded to the state its call started from and serves that domain's tools as real
queries against it.

Construction conventions: [`docs/SQL_ENVS.md`](../docs/SQL_ENVS.md).
Reference implementation: [`05-pharmacy-travel-refill`](05-pharmacy-travel-refill),
which is the smallest complete example and the template the others were built from.

## Why these exist

The dataset in this repository carries each conversation's tool results as
hand-authored JSON, and the state snapshots under `conversations/*/state/` are
derived backwards from those results. Nothing executes. That is enough to train
and evaluate next-action prediction along the recorded path, and it stops being
enough the moment an agent does something the recording did not do, because there
is no world to do it in.

These environments invert the derivation. Backend state is primary and tool
results are computed from it. An agent that asks for the override reason the
recording did not use gets the payer's actual policy for that reason. One that
checks stock at the store the recording rejected gets the stock that store holds.
One that spends a one-time authorization twice is refused the second time.

The recorded conversation becomes the specification for the *environment* rather
than a script for the agent.

## Two layers, and why they are separate

There are two questions here, they need different machinery, and answering both
with one mechanism produces a verifier that looks strict and measures nothing.

**Is the environment faithful?** `tests/env_check.sh` rebuilds the database,
replays the recorded calls in order, and requires every response to match the
recording byte for byte. A divergence is a defect in this backend, not in the
recording. This is a regression test on the environment and it does not score an
agent.

**Did the run handle the call well?** `tests/test.sh` grades what an agent left
behind, and deliberately does *not* replay the recorded path. An agent that
verifies identity in a different order, skips a lookup it did not need, or reads
a record three times where the recording read it once has done nothing wrong.
Only outcomes are graded:

```
reward = db_reward * communicate_reward
```

τ-bench arrives at the same conclusion from the other direction, and its
[evaluation docs](https://github.com/sierra-research/tau2-bench/blob/main/docs/evaluation.md)
are worth reading: its gold trajectory exists only to derive a target end state,
the agent's own path is never compared against it, and its path-matching
`RewardType.ACTION` is used in none of the airline, retail, or telecom tasks. Its
`Environment.set_state(strict=True)` does compare replayed outputs against
recorded ones, exactly as the conformance layer here does, and is likewise not
part of any score.

This split was not the original design. The first version of `test.sh` reset the
database and replayed the gold calls itself, which meant it scored 1.0 on a
container no agent had ever entered — it was measuring the environment while
appearing to measure the run. The `idle` column in the results below is the
control that now catches that.

### What `db_reward` requires

- **Required facts.** Every field in `verifier-data/expected_final_state.json` is
  present with that value, as a subset assertion — the database knowing more than
  the recording did is fine.

  Naming a field rather than a call does not by itself make this path-independent,
  and getting that wrong is how route-scoring creeps back in. Several tools model
  something happening where no tool can observe it — a customer opening a secure
  session, a notification progressing through delivery — by writing the transition
  the first time the record is *read*. Pinning such a field scores whether the
  agent chose to look. Those are written as an explicit set instead:

  ```json
  "status": { "_any_of": ["issued", "open_not_submitted"] }
  ```

  which keeps what matters (the session is neither expired nor submitted) and drops
  what only records who looked. `./audit_read_sensitivity.py` finds the class
  statically. A bare read counter — `reads_served`, `calls_served` — has no bounded
  set and is not asserted at all; conformance pins those far harder anyway, since
  every recorded response carries the timestamp the counter produced and is
  compared byte for byte.
- **No collateral damage.** A row the agent inserted, deleted, or modified *that
  the gold path left untouched* is damage. Rows the gold path also touched are the
  agent's work area: each may end as it started (the required facts decide whether
  that was enough) or as the gold path left it, but not as a third value. So
  reaching the right outcome by a different route is free, while cancelling an
  unrelated customer's order, or delivering the application to the address the
  caller cannot reach, is not.

Damage is judged against `verifier-data/state_digest.json`, a per-row hash of the
initial state and of the gold end state. τ-bench uses a hash of the whole
database for this, which cannot work here — the seed is generated, so a
whole-database hash would change with every regeneration, and a mismatch reports
"different" with no indication of what. Per-row hashes localise the failure to a
named row. Seeding is deterministic (a fixed RNG seed), so a digest stays valid
across seed rebuilds.

Three exclusions are declared per task in `verifier-data/grading.json`, and the
narrowest one that works is the right one. `read_volatile_columns` drops named
columns before hashing a row, so a table whose *status* a read advances stays
under the check while the status stops mattering — which is what keeps an
unrequested notification to a real customer detectable as an inserted row.
`ignore_tables` excludes a table wholesale and is appropriate only where it holds
nothing but read state (read cursors, tool-call logs) or where reads *insert*
rows, as airline fare quotes do, since no column exclusion can hide an insert.
`append_tolerated` allows a new row but not modification of an existing one, for
append-only records where a genuine retry leaves a duplicate.

### What `communicate_reward` requires

End-state checking structurally cannot see the half of these conversations that
happens in speech, and several tasks turn on it. The pharmacy call is handled
badly if the one-time nature of the payer's override goes unmentioned, however
correct the database ends up.

Each task declares the facts a correct handling must convey in
`verifier-data/communicate_info.json`. As in τ-bench's `communicate_info` this is
deterministic substring matching with no LLM judge, with one improvement: each
requirement carries alternative surface forms and any one satisfies it, so an
agent is not failed for saying "fifteen dollars" instead of "$15".

Phrase lists alone only cover the wordings somebody anticipated, so numeric facts
also list the **bare figure**. `15` finds "15 USD", "15.00 dollars", "fifteen
bucks" and "a total of 15" without anyone predicting each one — on a dozen
plausible renderings of the pharmacy copay, the phrase list matched four and the
bare figure takes it to twelve. Bare forms are fenced so they cannot credit a
*different* figure: a number must not sit against another digit or a non-zero
decimal, so `$15` finds `$15.00` but not `$150` or `$15.09`; nor may it be part of
a clock time, date or ordinal, so `15` is not found in "10:15", "8/15", "August
15" or "the 15th"; and a bare word gets word boundaries so `two` is not found
inside `network`, nor `forty` inside "forty-five". Typographic apostrophes and
dashes are folded to ASCII first, so "can’t" and "3–5" match their forms. Multi-word forms stay
plain substrings, because several are deliberate stems — `90-day` has to match
"90-days". Anchors are only ever the whole value and only for a single value of
ten or more, since `five` would otherwise let "five business days" satisfy a $95
fee. `check_communication_matching.py` enforces this, importing the live matcher
so it cannot drift, trying wrong figures, figures inside times and dates, and
typographic renderings of every form across the ten tasks.

What the matcher cannot do is notice a contradiction. It checks that each
required fact was said, not that nothing wrong was said alongside it, so a
transcript that quotes both the right fee and a wrong one for the same card still
passes. Catching that needs a judgement about which card a figure refers to,
which substring matching cannot make.

The run's speech arrives at `/workspace/transcript.txt`, which is agent-writable,
or at `$AGENT_TRANSCRIPT` if a harness puts it elsewhere. Plain text is read
whole; a JSON message list is accepted too, in which case only assistant-role
content counts, so a pipeline driving a real voice agent can hand over its own
conversation log unmodified.

`reward.json` also reports how many of the gold path's write calls the run made.
This does not gate anything — like τ-bench's `partial_action_reward` it measures
similarity to one reference path, and a run can score zero on it and be correct.

## Results

Every figure below was produced by `./verify_all.sh`, `./check_containment.sh`,
`./check_read_freedom.sh` and `./report_seed_volume.sh` in this directory,
building each image from scratch and discarding it afterwards.

| Task | Domain | Calls | Tools used | Tools served | Tables | Seeded rows | State fields | Reward |
|---|---|---|---|---|---|---|---|---|
| [`01-banking-account-email-card-application`](01-banking-account-email-card-application) | banking | 15/15 | 9 | 16 | 27 | 1,866 | 31/31 | 1.0 |
| [`02-banking-referral-missing-reward`](02-banking-referral-missing-reward) | banking | 9/9 | 7 | 16 | 27 | 1,831 | 35/35 | 1.0 |
| [`03-banking-transaction-dispute-session`](03-banking-transaction-dispute-session) | banking | 7/7 | 6 | 16 | 27 | 1,774 | 29/29 | 1.0 |
| [`04-banking-declined-card-travel`](04-banking-declined-card-travel) | banking | 8/8 | 6 | 16 | 27 | 1,772 | 48/48 | 1.0 |
| [`05-pharmacy-travel-refill`](05-pharmacy-travel-refill) | pharmacy | 8/8 | 7 | 9 | 16 + 1 view | 2,316 | 30/30 | 1.0 |
| [`06-retail-refund-bank-fee`](06-retail-refund-bank-fee) | retail | 7/7 | 4 | 9 | 27 + 2 views | 3,164 | 34/34 | 1.0 |
| [`07-retail-damaged-item-replacement`](07-retail-damaged-item-replacement) | retail | 7/7 | 4 | 9 | 27 + 2 views | 3,179 | 48/48 | 1.0 |
| [`08-retail-missing-package`](08-retail-missing-package) | retail | 7/7 | 5 | 9 | 27 + 2 views | 3,263 | 38/38 | 1.0 |
| [`09-airline-family-reservation`](09-airline-family-reservation) | airline | 9/9 | 8 | 9 | 27 + 1 view | 5,273 | 71/71 | 1.0 |
| [`10-telecom-data-usage-cleanup`](10-telecom-data-usage-cleanup) | telecom | 8/8 | 7 | 8 | 21 + 1 view | 3,854 | 38/38 | 1.0 |

**85 of 85 recorded tool calls reproduce byte-exactly. 402 of 402 final-state
assertions hold.**

"Tools used" is how many distinct tools the recording exercises; "tools served" is
how many the environment implements. The gap matters: every tool in the domain
registry has a working handler, so an agent is not confined to the recorded path
by the absence of an implementation. The server refuses to start if any registry
tool lacks a handler.

## Controls

Each task demonstrates all of these:

| Control | Expected |
|---|---|
| `env_check.sh` conformance replay | every recorded call byte-exact |
| Oracle run as the agent, then graded | 1.0 |
| Idle container, nothing ran, then graded | 0.0 |
| **A different route to the same outcome** | **1.0** |
| Gold read calls re-issued 3x over | 1.0 |
| Correct records, an unrelated entity meddled with | 0.0, `db` 0 and `communicate` 1 |
| Correct records, an unrequested message or session created | 0.0, `db` 0 |
| Correct records, a required fact left unsaid | 0.0, `db` 1 and `communicate` 0 |
| Correct records, nothing said at all | 0.0 |
| Agent account reads `verifier-data/` or the admin token | denied |
| Agent account opens a PostgreSQL session | denied |
| `GET /_admin/state`, wrong or absent token | 401 |
| `gen_seed.py` or the annotated transcript in the image | absent |
| Schema-invalid arguments | 400 |

The fourth row is the one that justifies the whole grading layer, so it is worth
seeing concretely. In the pharmacy task the gold path takes 8 calls, two of them
separate `update_prescription` writes. This route scores 1.0:

- 5 calls instead of 8
- the two writes combined into a single `update_prescription`
- both location lookups skipped entirely
- speech in different words — "fifteen dollars", "half an hour", "one time",
  "too soon", "cannot skip"

The two zero rows below it show the components failing independently, which is
what makes the product meaningful rather than decorative: meddling with an
unrelated patient leaves `communicate` at 1.0 and zeroes `db`; staying silent
about the one-time override leaves `db` at 1.0 and zeroes `communicate`.

And because a verifier nobody has watched fail is not a verifier: rendering the
pharmacy copay as `15.0` where the recording says `15` fails conformance while
the database stays correct — the wire format is wrong and the state is not.

## Running one

```bash
cd 05-pharmacy-travel-refill
docker build -t voice-env-pharmacy environment
docker run -d --name pharmacy -v "$PWD/out:/out" voice-env-pharmacy
docker exec pharmacy test -f /tmp/task-infra/.ready && echo ready

docker exec pharmacy curl -s http://127.0.0.1:8080/tools | jq '.tools[].name'
docker exec pharmacy /usr/local/bin/task-init.sh --dump-state   # collect /out
```

The verifier and the oracle are delivered at run time rather than baked into the
image, the way the harness delivers them:

```bash
docker cp tests pharmacy:/opt/tests
docker cp solution pharmacy:/opt/solution

# grade a run: scores the state and speech the run left behind
docker exec pharmacy bash /opt/tests/test.sh        # -> /logs/verifier/reward.json

# the oracle, run as the agent account, then graded. Must be 1.0
docker exec -u agent pharmacy bash /opt/solution/solve.sh
docker exec pharmacy bash /opt/tests/test.sh

# the environment's own regression test. Resets the database, so run it last
docker exec pharmacy bash /opt/tests/env_check.sh   # -> conformance.json
```

`env_check.sh` rebuilds the database from SQL, which destroys whatever a run left
behind. Grade first, check conformance after.

## Running all of them

```bash
./verify_all.sh                  # build, boot, replay, score, tear down
./verify_all.sh 05 09            # just those two
KEEP_IMAGES=1 ./verify_all.sh    # keep the images afterwards
./check_containment.sh           # agent cannot reach the answers
./check_read_freedom.sh          # looking at things cannot lower a score
./audit_read_sensitivity.py      # no assertion pins a read-advanced column
./check_communication_matching.py  # no required fact accepts a wrong figure
./report_seed_volume.sh          # exact row counts, read from each database
```

Only the first three need Docker. `audit_read_sensitivity.py` and
`check_communication_matching.py` read the task files directly and finish in about
a second, so there is no reason not to run them on every change.

`verify_all.sh` runs three gates per task: conformance, the oracle graded (must be
1.0), and an idle container graded (must be 0.0).

`check_read_freedom.sh` is the generic fairness control. It runs each oracle,
confirms 1.0, then re-issues every read call the gold path made three more times
and requires the reward to still be 1.0. Which tools count as reads comes from the
server's own `/_admin/schema`, so it needs no per-task knowledge. This matters
because several domains genuinely mutate rows on read — retail's progressive
section views advance a cursor every time an order is read — and "reads are free"
is therefore a property to demonstrate rather than assume. All ten pass, on 171
extra calls in total, ranging from 9 on `06` to 33 on `01`.

It reports the number of calls it issued, and treats a run that issued none as a
failure rather than a pass. That guard exists because the control was silently
vacuous at one point: it feeds its flood script to `docker exec` over a heredoc,
and without `-i` stdin is not forwarded, so `python3 -` read EOF, ran an empty
program and exited 0. Nothing was called, the reward was trivially unchanged, and
ten green PASS rows were reported. A control whose failure mode is a false pass
has to state its own work.

`verify_all.sh` removes each image as soon as its task is scored. Ten PostgreSQL
images at once do not fit on a typical working disk, and the base layer is shared
so rebuilding is cheap.

`report_seed_volume.sh` counts rows by querying the running database rather than
by parsing the seed SQL, because a row whose values span several lines — a `JSONB`
payload, say — is easy to miscount in text and impossible to miscount in a table.
The figures in the table above come from it.

## What is shared and what is not

Each task directory is self-contained: its own schema, its own seed, its own copy
of the server. Nothing is imported across tasks. Four of the banking tasks have
near-identical schemas and that duplication is deliberate — a task can be reviewed
and can diverge without coordinating with its siblings.

Genuinely identical across all ten, and reusable as-is:

- `environment/server/app.py` — the HTTP front end, differing only in which tables
  a state snapshot covers
- `environment/server/db.py` — connection handling and identifier allocation
- `environment/server/schema.py` — a dependency-free JSON Schema validator
  covering the keyword subset the registries use
- `environment/server/projection.py` — sparse result assembly and explicit number
  rendering
- `environment/Dockerfile`, `environment/task-init.sh`
- everything under `tests/`: `test.sh`, `grade.py`, `env_check.sh`, `replay.py`,
  `score_conformance.py`, `statecheck.py`, `make_digest.py`

The graders being identical is deliberate. A change to how scoring works is then
one edit propagated ten times rather than ten chances to drift, and per-task
variation is pushed into data — `verifier-data/grading.json` and
`verifier-data/communicate_info.json` — where it can be reviewed as a decision
rather than read out of code.

Domain-specific, written per task: the four SQL files, `server/tools.py` including
its `WRITE_TOOLS` set, `environment/gen_seed.py`, the four `verifier-data`
documents, and the task's own manifest, instruction, README, and oracle.

## The four things that resisted a naive implementation

Recorded in full in [`docs/SQL_ENVS.md`](../docs/SQL_ENVS.md); in brief:

**Identifiers.** Results contain specific references — `WST481662`, `B9RT6M`,
`override-lost-medication`. Each is issued from an `id_allocator` row by
`UPDATE ... RETURNING`, seeded so the first allocation yields the recorded value
and the second yields the next one. Nothing is generated from wall time or at
random.

**Repeat reads that deepen.** The retail conversations call `get_order` on one
order three or four times and legitimately get more detail each time. Where the
difference follows from a mutation an intervening call made, it is modelled as
real state. Where it is genuine repeat-read deepening, a `section_read_cursor`
row counts the reads and `section_view` holds what to emit at each depth — so the
scriptedness is a row an operator can query rather than a list being popped inside
the server process.

**Human-relative strings.** Results contain `"15:18 yesterday"`,
`"18:00 tomorrow"`, `"almost three hours"`. These are deliberately non-ISO because
the agent reads them back verbatim. Both forms are stored: a display column
carrying the exact string, and a typed column resolved against the scenario clock.

**The banking knowledge base.** `search_knowledge_base` is 13 of the 85 calls and
its result schema is a union of about twenty content shapes that never co-occur.
That one table keeps a `JSONB` payload matched by query pattern — the single
sanctioned exception to normalization, because decomposing a union that never
co-occurs adds schema surface and no fidelity.

## Limits worth stating

- **The clock does not advance.** `scenario.scenario_time` is the conversation's
  time and the tools read it instead of wall time, which is what makes a run
  reproducible. Nothing moves it, so an agent cannot observe time passing.
- **These reward backend state, not speech.** A run is scored on what it did to
  the database. Whether the agent said something accurate, grounded, and kind is
  not measured here. The dataset's own annotation layer for that is the
  `grounding_review` records embedded in
  `conversations/*/transcripts/annotated-transcript.json`, and it is a separate
  axis from anything these environments score.
- **Seed populations are plausible, not real.** They are generated to make
  lookups non-trivial — colliding order suffixes, shared surnames, expired offers,
  factors that fail verification — but no claim is made that their distributions
  match a production system.
- **One conversation per environment.** The scenario rows are seeded for the call
  that was recorded. Retail tasks 07 and 08 are the exception and cross-reference
  each other by design, since the recordings are consecutive days for one customer.
