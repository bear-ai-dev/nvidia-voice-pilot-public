# 07 — retail-damaged-item-replacement

A 12-cup coffee maker arrived with a cracked water tank and a damp base. The
customer is the same one who called yesterday about headphones that were scanned
delivered and never arrived, and he is worried the two orders will get mixed up.
They must not.

Recorded conversation: `conversations/retail-damaged-item-replacement/`. Domain
policy and tool contracts: `domains/retail/`. Construction conventions shared by
every task here: `docs/SQL_ENVS.md`.

| | |
|---|---|
| Domain | retail |
| Scenario time | 2026-08-26T11:20:00-04:00 |
| Tools | 9 |
| Recorded tool calls | 7 |
| Database | PostgreSQL 16, 26 tables and 2 views |

## What this environment is

The published dataset carries this conversation's tool results as authored JSON.
Nothing executes, so an agent that departs from the recorded path has nothing to
depart into.

Here Westline's records are a real database and the nine tools are real queries
against it. The recorded results are reproduced because the data reproduces
them, not because they are stored. Create the replacement twice and you get two
real orders with two allocated references rather than a crash; try to create one
against the headphones order and the absence of an eligibility row refuses it.

## Yesterday, as data

Per `docs/DATA_QUALITY.md` this conversation and the missing-package one are
consecutive days on one account, and this one's recorded results read the other
one's outcome back. Two of the seven calls do it: the first read reports the
headphones case sitting on the account with its pickup preference attached, and
the last reports that case's type, status, absent carrier response, and 18:00
deadline.

`sql/004_scenario.sql` therefore seeds the headphones order, its item, its
disagreeing carrier scan, the `8cf648a4-ca60-4387-bc11-ec38f426123a` delivery trace, the note and pickup
preference recorded on it, and the confirmation email that was sent, all as
pre-existing rows with yesterday's timestamps. The deadline that was
`18:00 tomorrow` in yesterday's recording is `18:00 today` in this one, because
it is the same instant a day later. Both recordings are reproduced from a row
rather than from a string in a handler, which is the point of seeding it at all.

## Schema

Twenty-six tables. Shapes come from the `result_schema` definitions in
`domains/retail/tool_registry.json`; the lifecycle vocabularies come from
`domains/retail/policy.md` and are `CHECK` constraints, so an illegal transition
fails in the database rather than only in the tool layer.

**Catalogs** — `products`, `product_variants`, `distribution_centers`,
`case_type_policy`, `notification_templates`, `note_topics`,
`pickup_site_suffixes`.

**People and orders** — `customers`, `orders`, `order_items`, `payments`,
`returns`, `refunds`, `carrier_scans`.

**The claim** — `cases`, `case_items`, `case_notes`, `case_preferences`,
`eligible_resolutions`, `replacement_orders`, `notifications`,
`specialist_transfers`.

**Read models** — `section_read_cursor`, `section_view`.

**Infrastructure** — `scenario` (the clock and the desk's rendering settings),
`id_allocator`, `tool_call_log`, and two views, `case_note_log` and
`section_read_log`, which expose composite-keyed rows under a single stable key
so the verifier can address them without depending on a sequence number that
shifts when the population changes.

Five design points are worth calling out, because each replaces something a
naive implementation would hard-code.

**One repeat read needed the cursor; the rest are mutations.** `get_order` is
called four times. Calls one and two both ask for `eligible_resolutions` and
only the second returns them: the agent opened the record, asked what was wrong
with the item, and then went back for the options. No call between them changed
anything, so that difference is a repeat-read deepening and is modelled as one —
`section_read_cursor` counts reads served per (order, section) and
`section_view` holds what to emit at each depth, with a `NULL` payload at depth
zero omitting the section rather than returning it empty. The other two reads
need no cursor: the notifications on `ending-8821` exist because
`create_replacement_order` created that order and raised its confirmation, and
the case on `ending-7319` was seeded a day earlier. Using the cursor for either
would have made a read appear to conjure an order.

**The resolutions panel is projected from the rows the handler acts on.** The
`section_view` payload for depth one is built in SQL by aggregating the
`eligible_resolutions` rows, the same rows `create_replacement_order` reads to
decide that the original price carries over, that no return is required, and
what estimate to quote. Hand-writing the panel would have let the quoted
estimate and the created order drift apart. `jsonb_strip_nulls` is what makes
the refund option disclose only that it exists: the recording never revealed its
terms, so the row is nulls and the panel emits a single key.

**Replacement creation is real.** It allocates a reference from `id_allocator`,
copies the original's lines into a new order, computes the balance due from the
eligibility's price basis, assigns a distribution centre from the customer's
fulfilment region, writes a `replacement_orders` row, raises the confirmation
from its template, and flags any open case on the original. Called a second time
it does all of that again and issues `ending-8822`. The unmerged `runnable-env`
branch pops a pre-authored template off a dict and raises `KeyError` on the
second call; this is what replaces that.

**What a case discloses depends on which order you are reading.**
`case_type_policy` carries two field lists per case type: one for a case on the
order in front of you and one for a case carried over from another order on the
same account. The first read here is of the coffee maker and reports the
headphones case as context — which order it belongs to, what it is about, and
the preference already on it, so the preference is visibly not attached to the
order being worked. The last read is of the headphones order itself and reports
the trace's operational state. Both come from the same projection reading
different rows. The field lists are seed data because the two retail
conversations that read a delivery trace disclose different parts of it, and a
hard-coded projection would have had to contradict one of them.

**Notification delivery advances on refresh.** The confirmation is created
`queued` because the order pipeline builds it asynchronously, and the customer
asks about it a minute later.  `notification_templates.delivery_progression`
holds the receipt sequence and `notifications.status_index` advances one step per
read, so the refresh reports `sent` without a clock that moves.

## Seed

`environment/gen_seed.py` runs at author time with a fixed RNG seed and writes
`sql/002_reference.sql` and `sql/003_population.sql`. It is excluded from the
image by `.dockerignore`, so the container carries the world but not the machine
that made it. `sql/004_scenario.sql` is hand-written and holds the entities this
conversation touches, with each value marked as either read back by a recorded
result or as filler the recording never revealed.

| Table | Rows |
|---|---|
| customers | 102 |
| orders | 359 |
| order_items | 711 |
| payments | 439 |
| refunds | 445 |
| returns | 170 |
| carrier_scans | 544 |
| cases | 61 |
| case_notes | 30 |
| eligible_resolutions | 26 |
| products | 81 |
| product_variants | 107 |

The population is there so the lookups have work to do:

- **Two Patels and a Patell.** The caller is resolved by the verified email on
  the order; the surname resolves to several people and is not a lookup key.
- **Colliding digit suffixes.** Three of the caller's own orders end in digits
  close to `4086`, so `86` alone ties three ways and is refused, and one pair of
  orders on different accounts shares all four trailing digits.
- **Twenty-six resolution rows on other people's cases**, roughly a third of
  which unlock a refund and nothing else, so the presence of eligibility is not
  the same as the presence of a replacement.
- **Out-of-stock variants**, so an inventory check can fail, and a scenario
  variant whose catalog knows its stock but not whether the identical unit can
  be reserved — which is exactly the asymmetry the recorded inventory result
  shows.
- **Forty-eight other cases still open**, alongside twelve closed and eight
  resolved, so an agent that treats any case it finds as actionable is often
  wrong.

## Verification

Two layers, answering two different questions. See
[`docs/SQL_ENVS.md`](../../docs/SQL_ENVS.md) for the full contract.

### Conformance: is this backend faithful?

`tests/env_check.sh` rebuilds the database, replays the seven recorded calls, and
requires every result to match the recording byte for byte after canonical JSON
normalization. Two of the seven read yesterday's case back, so this is also the
layer that establishes the seeded prior conversation is reproduced from its rows
rather than from strings in a handler. A divergence is a defect here, not in the
recording. It scores no agent, and it destroys whatever a run left behind, so it
runs last.

```
  [ok  ]   rd-001  get_order
  [ok  ]   rd-002  get_order
  [ok  ]   rd-003  get_product
  [ok  ]   rd-004  create_replacement_order
  [ok  ]   rd-005  get_order
  [ok  ]   rd-006  lookup_customer
  [ok  ]   rd-007  get_order
  7/7 calls reproduced exactly
final-state fields matched: 48/48

conformant: true
```

### Grading: did the run handle the call well?

`tests/test.sh` grades what an agent left behind. It does not reset the database
and does not replay the recorded calls, because the recorded path is one correct
route and not the only one. Eleven of the 48 required facts describe yesterday's
headphones case and the pickup preference recorded on it, which this call must
leave exactly as it found them — asserting the absence of interference is how "the
two orders must not get mixed up" becomes something a grader can check.

```
required facts:  48/48
collateral damage: 0 row(s) the gold path never touched
transcript: 1836 characters of plain text
communicated:    6/6
[diagnostic] tool calls made: 7; gold write tools used: 1/1 (similarity to one reference path, not gating)

reward: 1.0  (db 1.0 x communicate 1.0)
```

The gold path touches **7 rows out of 3,208**, and three of the seven are
bookkeeping the damage policy excludes: the resolutions read cursor, the view row
over it, and the replacement-order allocator. The graded work area is therefore
four rows — the replacement order, its single line, its `replacement_orders` row,
and the confirmation — and those four are governed by the required facts. The
other 3,201 rows are held to the initial state, which is what makes creating a
replacement for an unrelated customer detectable.

The confirmation's delivery status is asserted as the set
`["queued", "sent", "delivered"]` rather than as one value. This task has the
widest set of the three retail environments because the confirmation is created
`queued` and each read of it collects one more receipt, so the row's value records
how many times somebody looked rather than anything the run achieved. All three
values were observed on correct handlings of this call: the alternate route below
never reads the confirmation back and leaves it `queued`, the oracle reads it once
and leaves it `sent`, and the oracle followed by six more reads of it leaves it
`delivered`. All three score 1.0. `status` and `status_index` are also
`notifications`' `read_volatile_columns`, so the table stays under the damage
check and an inserted notification is still caught.

Speech is graded from `/workspace/transcript.txt` against six requirements: that
the replacement goes out at the price he already paid, that the return is waived
and nothing has to go back, that the damp unit must not be plugged in, that
Thursday is an estimate and not a guaranteed window, that the replacement ships to
his home rather than to the pickup counter belonging to yesterday's case, and that
the headphones trace is still open with no carrier response before its 18:00
deadline. Each accepts several surface forms.

### Controls

| Control | Result |
|---|---|
| Conformance replay | 7/7 byte-exact, 48/48 fields |
| Oracle as the agent account, then graded | 1.0 |
| Idle container, nothing ran | 0.0 — 43/48 facts, 0/6 said |
| **Different route: caller resolved by `customer_id` first, order read three times, confirmation never read back, own wording** | **1.0** |
| 24 extra read-only calls after a correct handling, nine of them of a notification | 1.0, 31 calls logged |
| Replacement created for `1407942574`, an unrelated customer | 0.0 — 47/48 facts, 4 damaged rows |
| Unrequested notification on `WST230618`, an unrelated open case | 0.0 — `db` 0.0, `communicate` 1.0 |
| "Do not plug it in" left unsaid | 0.0 — `db` 1.0, `communicate` 0.0 |
| Nothing said at all | 0.0 — `db` 1.0, `communicate` 0.0 |
| Agent account reading `verifier-data/` or the admin token | permission denied |
| `GET /_admin/state` with a wrong token | 401 |
| `create_replacement_order` twice | two orders, `ending-8821` then `ending-8822` |
| `create_replacement_order` on the headphones order | refused: not eligible for a replacement |
| Two-digit reference on this account | refused: three candidates tie |
| Four-digit reference scoped to the wrong account | not found |
| `include` carrying a section name the registry does not declare | 400, the enum listed |

The fourth row is the point of the grading layer. That route resolves the caller
by `customer_id` before doing anything else, addresses the order by its full
ten-digit reference, splits the two recorded reads into three with different
section sets, asks the catalog about the product rather than the variant, and never
reads its own confirmation back. Every one of those is a different route and none
of them is a different outcome, and it scores 1.0. Under the previous single-layer
verifier it was not measurable at all.

The two meddling controls show the damage check from both sides. Creating a
replacement for `1407942574` — a real order belonging to Sana Ilyin, eligible, with
the item in stock, so nothing refuses it — leaves four rows behind and is caught on
all four, and the extra reference it took also drops the allocator fact to 47 of
48. Sending an unrequested confirmation on an unrelated open case leaves exactly
one row and moves nothing else: that run still passes 48/48 facts and 6/6
utterances, so the required-facts assertion cannot see it at all, and the reward is
zero purely because the damage check can.

One thing the damage check deliberately does not catch: an extra note on
yesterday's headphones case scores 1.0, because `case_notes` is
`append_tolerated`. `update_case` has no idempotency key, so a retried call
genuinely leaves a second note, and what the case must end up saying is asserted
through `case_note_log` instead. Changing that case's status, its carrier
response, or its pickup preference is a different matter and fails on the required
facts.

The idle container reporting 43 of 48 fields is the intended reading: the coffee
maker, the customer, and the whole of yesterday's case exist before the call is
handled and are asserted unchanged. The five it misses are the replacement order,
its line, its `replacement_orders` row, the confirmation, and the allocator's
advance — the whole of what the call has to earn.

## Running it

```bash
docker build -t voice-env-07-retail environment
docker run -d --name retail07 -v "$PWD/out:/out" voice-env-07-retail
docker exec retail07 test -f /tmp/task-infra/.ready && echo ready

curl -s http://127.0.0.1:8080/tools | jq '.tools[].name'   # from inside

docker cp tests retail07:/opt/tests
docker cp solution retail07:/opt/solution
docker exec -u agent retail07 bash /opt/solution/solve.sh  # the oracle
docker exec retail07 bash /opt/tests/test.sh               # grade it -> 1.0
docker exec retail07 bash /opt/tests/env_check.sh          # conformance (resets)
```

The oracle runs as the `agent` account, which is also what proves
`/workspace/transcript.txt` is writable by the account that will need to write it.

To collect the final state after a run:

```bash
docker exec retail07 /usr/local/bin/task-init.sh --dump-state
# writes /out/final_state.json (canonical, diffable) and /out/final_state.sql
```

`--reset-db` rebuilds the world from SQL without restarting the server, which is
what the verifier uses to guarantee a clean start.

## Known limits

- **The clock is fixed.** `scenario.scenario_time` is the conversation's time and
  the tools read it instead of wall time, which is what makes runs reproducible.
  Nothing advances it, so an agent cannot observe the carrier's 18:00 deadline
  pass.
- **Notification delivery advances on read, not on time**, for the same reason.
- **The coffee maker's item carries no colour and the headphones carry no name.**
  Each recorded result discloses one identification and not the other, so the
  column the recording never revealed is null rather than filled in.
- **Yesterday's conversation is seeded, not replayed.** This task starts from the
  state that one ends in; it does not run it.
- **`transfer_to_specialist` records the handoff and ends nothing.** There is no
  specialist behind it.
