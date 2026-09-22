# 05 — pharmacy-travel-refill

A patient's albuterol inhaler refill is stuck. The pharmacy app says "processing";
the real reason is that the payer rejected the claim as a refill too soon. He lost
last month's inhaler in a hotel room, he leaves town in the morning, and the
counter closes in 48 minutes.

Recorded conversation: `conversations/pharmacy-travel-refill/`. Domain policy and
tool contracts: `domains/pharmacy/`. Construction conventions shared by every
task here: `docs/SQL_ENVS.md`.

| | |
|---|---|
| Domain | pharmacy |
| Scenario time | 2026-08-27T18:12:00-05:00 |
| Tools | 9 |
| Recorded tool calls | 8 |
| Database | PostgreSQL 16, 17 tables and 1 view |

## What this environment is

The published dataset carries this conversation's tool results as authored JSON.
Nothing executes, so an agent that departs from the recorded path has nothing to
depart into.

Here the pharmacy's records are a real database and the nine tools are real
queries against it. The recorded results are reproduced because the data
reproduces them, not because they are stored. Ask for the override reason the
recording did not use and the payer's actual policy for that reason answers; ask
for stock at the location the recording rejected and you get the stock it holds.

## Schema

Seventeen tables. Shapes come from the `result_schema` definitions in
`domains/pharmacy/tool_registry.json`; the lifecycle vocabularies come from
`domains/pharmacy/policy.md` and are `CHECK` constraints, so an illegal
transition fails in the database rather than only in the tool layer.

**Catalogs** — `medications`, `stores`, `store_inventory`, `insurance_plans`,
`plan_override_rules`.

**People** — `patients`, `notification_destinations`.

**The fill** — `prescriptions`, `claims`, `claim_overrides`, `fill_queue`.

**Requests** — `transfer_requests`, `specialist_transfers`.

**Infrastructure** — `scenario` (the clock), `id_allocator`, `tool_call_log`, and
the `latest_claims` view, which exposes the current payer position keyed by
prescription so the verifier can address it without depending on a sequence
number that shifts when the population changes.

Three design points are worth calling out, because each replaces something a
naive implementation would hard-code.

**The payer's decisions are data.** `plan_override_rules` holds, per plan and per
override reason, the identifier to issue and the decision to return. The recorded
call asks for `lost_medication` on Midwest Choice PPO and gets a one-time
approval because that is what the row says. `vacation_supply` on the same plan
returns `pending_patient_participation`, and `other` returns `denied`, from the
same lookup.

**A one-time override is spent once.** `claim_overrides.consumed_at` is written
when a claim pays against an `approved_one_time` decision and is checked before
the next one. The published `runnable-env` branch records consumption and never
reads it back, so the same one-time approval can pay indefinitely; here the second
submission is rejected.

**Queue position comes from the store.** `stores.queue_next_position` is
incremented by `UPDATE ... RETURNING` when a claim activates a fill, so the
recorded position of 1 is allocated rather than asserted, and a second fill at
that counter gets 2. The seed hands positions out from the same per-store
counter, so every store's occupied positions are 1..k with no duplicates and its
`queue_next_position` lands on k+1. The scenario store's queue is seeded empty,
which is why position 1 is available: it is 18:12 and the counter closes at 19:00,
so the recorded fill is the next one it takes.

## Seed

`environment/gen_seed.py` runs at author time with a fixed RNG seed and writes
`sql/002_reference.sql` and `sql/003_population.sql`. It is excluded from the
image by `.dockerignore`, so the container carries the world but not the machine
that made it. `sql/004_scenario.sql` is hand-written and holds the entities this
conversation touches.

| Table | Rows |
|---|---|
| patients | 105 |
| notification_destinations | 191 |
| prescriptions | 308 |
| claims | 308 |
| fill_queue | 308 |
| store_inventory | 1000 |
| stores | 25 |
| medications | 40 |
| insurance_plans | 5 |
| plan_override_rules | 20 |

The population is there so the lookups have work to do:

- **Two patients named Miles Carter.** Resolving on the name alone is ambiguous;
  the date of birth is what disambiguates. Ten patients share the Carter surname.
- **Three stores in the fill store's district**, of which only Park Avenue is
  still open after 19:00. Maple Grove closes at 18:00 and must be filtered out,
  and it is also out of stock on the inhaler.
- **31 unverified notification destinations.** A ready alert pointed at one is
  refused, so "verified" is a property that has to be checked.
- **Nine override rules that deny** and several that require patient
  participation, across five plans.
- **174 out-of-stock inventory rows**, so a stock check can fail.

A nearby-location search is scoped to the origin store's `district`. Without
that, a 25-store chain would surface as "nearby" and the recorded single-result
search would be unreproducible for the wrong reason.

## Verification

Two layers, answering two different questions. See
[`docs/SQL_ENVS.md`](../../docs/SQL_ENVS.md) for the full contract.

### Conformance: is this backend faithful?

`tests/env_check.sh` rebuilds the database, replays the eight recorded calls, and
requires every result to match the recording byte for byte after canonical JSON
normalization. A divergence is a defect here, not in the recording. It scores no
agent, and it destroys whatever a run left behind, so it runs last.

```
  [ok  ]   ph-001  lookup_patient
  [ok  ]   ph-002  get_prescription
  [ok  ]   ph-003  request_claim_override
  [ok  ]   ph-004  submit_prescription_claim
  [ok  ]   ph-005  update_prescription
  [ok  ]   ph-006  search_pharmacy_locations
  [ok  ]   ph-007  get_store_inventory
  [ok  ]   ph-008  update_prescription
  8/8 calls reproduced exactly
final-state fields matched: 30/30

conformant: true
```

### Grading: did the run handle the call well?

`tests/test.sh` grades what an agent left behind. It does not reset the database
and does not replay the recorded calls, because the recorded path is one correct
route and not the only one.

```
required facts:  30/30
collateral damage: 0 row(s) the gold path never touched
transcript: 796 characters of plain text
communicated:    5/5
[diagnostic] tool calls made: 8; gold write tools used: 3/3

reward: 1.0  (db 1.0 x communicate 1.0)
```

The gold path touches **6 rows out of 2,624** — the prescription, its claim and
the `latest_claims` view over it, the override, the queue entry, and the store's
position counter. Those six are the agent's work area, governed by the required
facts. The other 2,618 are held to the initial state, which is what makes
meddling with another patient detectable.

Speech is graded from `/workspace/transcript.txt` against five requirements: the
refill-too-soon rejection, the one-time nature of the override, the $15 copay,
that pharmacist verification cannot be skipped, and the queue estimate. Each
accepts several surface forms.

### Controls

| Control | Result |
|---|---|
| Conformance replay | 8/8 byte-exact, 30/30 fields |
| Oracle as the agent account, then graded | 1.0 |
| Idle container, nothing ran | 0.0 — 26/30 facts, 0/5 said |
| **Different route: 5 calls, writes merged, 2 reads skipped, own wording** | **1.0** |
| 24 extra read-only calls after a correct handling | 1.0, 40 calls logged |
| Meddling with `prescription-0001`, an unrelated patient | 0.0 — `db` 0.0, `communicate` 1.0 |
| One-time override left unmentioned | 0.0 — `db` 1.0, `communicate` 0.0 |
| Nothing said at all | 0.0 — `db` 1.0, `communicate` 0.0 |
| Agent account reading `verifier-data/` or the admin token | permission denied |
| `GET /_admin/state` with a wrong token | 401 |
| One-time override submitted twice | second submission rejected |
| Copay deliberately rendered `15.0` instead of `15` | conformance fails, state stays 30/30 |

The fourth row is the point of the grading layer. That route reaches the same
outcome by combining the two `update_prescription` writes into one call and
skipping both location lookups, and it says "fifteen dollars" and "half an hour"
rather than "$15" and "30 minutes". Under the previous single-layer verifier this
route was simply not measurable.

The meddling control is the one the required-facts assertion cannot catch on its
own: all 30 facts still hold and all 5 things were still said, and the run
correctly scores zero because it modified `prescriptions[prescription-0001]` and
that patient's queue entry. Both damaged rows are named in the report.

The last control establishes the conformance layer is worth running: a copay of
`15.0` where the recording says `15` fails it while the database stays correct.

Also checked: every store's `queue_next_position` agrees with the fills queued
there, and no two active fills at one store hold the same position.

## Running it

```bash
docker build -t voice-env-05-pharmacy environment
docker run -d --name pharmacy -v "$PWD/out:/out" voice-env-05-pharmacy
docker exec pharmacy test -f /tmp/task-infra/.ready && echo ready

curl -s http://127.0.0.1:8080/tools | jq '.tools[].name'   # from inside

docker cp tests pharmacy:/opt/tests
docker cp solution pharmacy:/opt/solution
docker exec -u agent pharmacy bash /opt/solution/solve.sh  # the oracle
docker exec pharmacy bash /opt/tests/test.sh               # grade it -> 1.0
docker exec pharmacy bash /opt/tests/env_check.sh          # conformance (resets)
```

The oracle runs as the `agent` account, which is also what proves
`/workspace/transcript.txt` is writable by the account that will need to write it.

To collect the final state after a run:

```bash
docker exec pharmacy /usr/local/bin/task-init.sh --dump-state
# writes /out/final_state.json (canonical, diffable) and /out/final_state.sql
```

`--reset-db` rebuilds the world from SQL without restarting the server, which is
what the verifier uses to guarantee a clean start.

## Known limits

- **The clock is fixed.** `scenario.scenario_time` is the conversation's time and
  the tools read it instead of wall time, which is what makes runs reproducible.
  Nothing advances it, so an agent cannot observe time passing during a call.
- **Human-relative strings are stored, not derived.** This conversation happens to
  carry none, but the pattern the other domains need is in `docs/SQL_ENVS.md`.
- **`transfer_to_specialist` records the handoff and ends nothing.** There is no
  specialist behind it.
- **There is no caller.** The environment serves tools and grades outcomes, but
  nothing simulates the patient's side of the conversation. τ-bench drives one
  with a user LLM against a task instruction; here the caller's intent is stated
  in `instruction.md` and the run supplies its own utterances. Grading a live
  agent end to end needs something generating the customer's turns.
- **Communication is matched, not judged.** The five requirements are substring
  matches over alternative surface forms. This is deterministic and needs no
  judge, but it cannot tell a fact stated correctly from the same words used to
  say something false — "the override is not one-time" contains "one-time". A
  paraphrase nobody anticipated fails, and a hostile run could satisfy a
  requirement without conveying it. τ-bench has the same property with
  `communicate_info` and offers `nl_assertions` under an LLM judge as the
  alternative.
