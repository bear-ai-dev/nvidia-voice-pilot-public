# 06 — retail-refund-bank-fee

A black standing desk converter was returned to a store nine days ago. The
return completed and the item is back in inventory. Forty dollars came back to a
gift card; the hundred and forty-six dollars and forty-two cents on the debit
card left the register and was never confirmed by the processor. The customer
has been charged an overdraft fee waiting for it.

Recorded conversation: `conversations/retail-refund-bank-fee/`. Domain policy and
tool contracts: `domains/retail/`. Construction conventions shared by every task
here: `docs/SQL_ENVS.md`.

| | |
|---|---|
| Domain | retail |
| Scenario time | 2026-08-27T13:05:00-04:00 |
| Tools | 9 |
| Recorded tool calls | 7 |
| Database | PostgreSQL 16, 26 tables and 2 views |

## What this environment is

The published dataset carries this conversation's tool results as authored JSON.
Nothing executes, so an agent that departs from the recorded path has nothing to
depart into.

Here Westline's records are a real database and the nine tools are real queries
against it. The recorded results are reproduced because the data reproduces
them, not because they are stored. Open the trace for an amount the tender never
carried and the refund row refuses it; open it against the replacement card the
customer offers and the absence of a refund on that token refuses it; read the
same order a fourth time and the read model says what it says at that depth.

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

**The dispute** — `cases`, `case_items`, `case_notes`, `case_preferences`,
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

**Every difference between the three reads is a repeat read.** `get_order` is
called three times on this order and no call between them changes a record — the
trace is not opened until afterwards. So all of the deepening is modelled as
read counts, not as mutations. `section_read_cursor` counts reads served per
(order, section) and `section_view` holds what to emit at each depth, with a
`NULL` payload omitting a section entirely rather than returning it empty. Both
sections carry three views:

| Read | `payments` | `refunds` |
|---|---|---|
| First | tenders, card token withheld | what the store did with the item |
| Second | the same tenders with the token disclosed | where each refund stands with the processor |
| Third | withheld | the gift-card ledger |

The tender panel's third view is `NULL` deliberately: a card token is disclosed
once per enquiry, and re-reading it is how a token gets harvested rather than
read. Past the deepest view the last one repeats, so a fourth look returns the
gift-card ledger and no tenders at all.

**The panels are projected from the rows, not written out.** Each `section_view`
payload is built in SQL from `payments`, `returns`, and `refunds`, so a panel
cannot drift away from the record it claims to describe. `accepted_age_days` is
computed there against the scenario clock rather than stored, so the nine days
the first read reports cannot disagree with the 18 August acceptance date. The
masked return reference is rendered through the same `scenario` setting the tool
layer reads.

**A materialized panel renders money the way the tool layer does.** A whole
amount is an integer and the rest are decimals — forty dollars is `40`, not
`40.0` — and a read model that emitted `to_jsonb(40.00)` would contradict a
directly projected section on the same order. `money_json()` in the schema is the
single rule both go through.

**A gift-card refund has two statuses that are not the same fact.**
`refunds.status` is where the refund stands with the payment processor and
`refunds.ledger_status` is where the card it created stands on the gift-card
ledger. The second read reports `issued_available` and the third reports
`active`; both are true at the same time, of different things. Modelling this as
one column would have forced a status to change with no event to change it.

**Notes are classified against a policy table.** `note_topics` holds patterns
and whether a matching note obliges the desk to restate its standing decision.
The card-replacement note matches nothing and the result is a bare
acknowledgement; the overdraft note matches `%overdraft fee%` and the result
carries `fee_reimbursement_approved: false`, read from the case rather than
written by the handler. The difference between the two recorded `update_case`
results therefore comes from the note's content meeting a policy row.

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
| orders | 362 |
| order_items | 714 |
| payments | 416 |
| refunds | 431 |
| returns | 176 |
| carrier_scans | 585 |
| cases | 60 |
| case_notes | 18 |
| eligible_resolutions | 24 |
| products | 80 |
| product_variants | 107 |

The population is there so the lookups have work to do:

- **Two Torrezes.** The caller is resolved by the verified email on the order;
  the surname is not a lookup key, and both Torrez accounts mask to `t***@`.
- **Colliding digit suffixes.** Three of the caller's own orders end in digits
  close to `5624`, so `24` alone ties four ways and is refused, and one pair of
  orders on different accounts shares all four trailing digits.
- **Refunds outnumber returns.** A completed return refunds to every tender the
  order was settled on, and money also goes back with no return at all for a
  price adjustment or a reversed shipping charge. An agent that assumes one
  refund per return, or that the only refund on an order is for the returned
  item, picks the wrong row — which is precisely the mistake this conversation
  is about not making.
- **Both case types, not one.** About two in five of the unrelated cases are
  refund traces against a refund already on the ledger, so the caller's case is
  not findable by being the only one of its kind, and about a third of all cases
  are already closed.
- **Out-of-stock variants**, so a replacement path that looks open on the
  eligibility row can still fail at the shelf.

## Verification

Two layers, answering two different questions. See
[`docs/SQL_ENVS.md`](../../docs/SQL_ENVS.md) for the full contract.

### Conformance: is this backend faithful?

`tests/env_check.sh` rebuilds the database, replays the seven recorded calls, and
requires every result to match the recording byte for byte after canonical JSON
normalization. Three of the seven are reads of the same order, so this is also the
layer that establishes the read cursor deepens in the recorded order rather than
happening to land on the right panel. A divergence is a defect here, not in the
recording. It scores no agent, and it destroys whatever a run left behind, so it
runs last.

```
  [ok  ]   rr-001  get_order
  [ok  ]   rr-002  get_order
  [ok  ]   rr-003  get_order
  [ok  ]   rr-004  open_refund_trace
  [ok  ]   rr-005  update_case
  [ok  ]   rr-006  send_case_notification
  [ok  ]   rr-007  update_case
  7/7 calls reproduced exactly
final-state fields matched: 35/35

conformant: true
```

### Grading: did the run handle the call well?

`tests/test.sh` grades what an agent left behind. It does not reset the database
and does not replay the recorded calls, because the recorded path is one correct
route and not the only one.

```
required facts:  35/35
collateral damage: 0 row(s) the gold path never touched
transcript: 2162 characters of plain text
communicated:    5/5
[diagnostic] tool calls made: 7; gold write tools used: 3/3 (similarity to one reference path, not gating)

reward: 1.0  (db 1.0 x communicate 1.0)
```

The gold path touches **11 rows out of 3,179**. Five of those eleven are
bookkeeping the damage policy excludes — the two section read cursors, the view
rows over them, and the support-case allocator — so the graded work area is the
six rows a handling actually creates: the case, its two notes, the two
`case_note_log` rows addressing them, and the confirmation. Those six are
governed by the required facts. The other 3,168 rows are held to the initial
state, which is what makes meddling with an unrelated customer detectable.

The confirmation's delivery status is asserted as the set
`["sent", "delivered"]` rather than as one value, because reading a notification
is what advances it: `get_order` with `notifications` in `include` collects the
next receipt the mail provider has and bumps `status_index`. A correct route may
leave the row at either point in that progression, and pinning one of them would
score whether the agent looked at its own email rather than whether it sent one.
The same two columns are `notifications`' `read_volatile_columns`, which keeps the
table under the damage check while making a read of it free — an unrequested
notification is still an inserted row and still caught.

Speech is graded from `/workspace/transcript.txt` against five requirements: that
the register raised the card refund and the processor never confirmed it, that a
second refund cannot go to the replacement card while the first is open, the
three-to-five business day review window, that the $40 on the gift card is
available and unused, and that the overdraft fee is the bank's charge and can be
documented but not approved or promised. Each accepts several surface forms.

### Controls

| Control | Result |
|---|---|
| Conformance replay | 7/7 byte-exact, 35/35 fields |
| Oracle as the agent account, then graded | 1.0 |
| Idle container, nothing ran | 0.0 — 30/35 facts, 0/5 said |
| **Different route: 9 calls, caller resolved first, order read four times, own wording** | **1.0** |
| 24 extra read-only calls after a correct handling, six of them of the notification | 1.0, 31 calls logged |
| Unrequested notification on `WST211550`, an unrelated open case | 0.0 — `db` 0.0, `communicate` 1.0 |
| Gift-card ledger answer left unsaid | 0.0 — `db` 1.0, `communicate` 0.0 |
| Nothing said at all | 0.0 — `db` 1.0, `communicate` 0.0 |
| Agent account reading `verifier-data/` or the admin token | permission denied |
| `GET /_admin/state` with a wrong token or none | 401 |
| Trace opened for an amount the tender never carried | refused, with the real amount |
| Trace opened against the replacement card | refused: no refund on that token |
| `amount` sent as a string | 400 |
| Two-digit reference on this account | refused: four candidates tie |
| `create_replacement_order` twice | two orders, `ending-8821` then `ending-8822` |
| A fourth read of the order | gift-card ledger, no tender panel |

The fourth row is the point of the grading layer. That route resolves the caller
with `lookup_customer` — which the recording never calls — addresses the order by
its full ten-digit reference rather than the last four, asks for the sections in a
different order and reads the order a fourth time so the cursor ends somewhere
else entirely, and sends the confirmation after both notes rather than between
them. It still scores 1.0. Under the previous single-layer verifier it was not
measurable at all.

The meddling control is the one the required-facts assertion cannot catch on its
own. All 35 facts still hold and all five things were still said; the run scores
zero because it inserted `notifications[notification-WST211550]` against a case
this conversation has nothing to do with, and that row is named in the report.
This is the exclusion choice paying off: had `notifications` been excluded
wholesale rather than by column, an unrequested message to a real customer would
have been invisible.

The idle container reporting 30 of 35 fields is the intended reading: the order,
the customer, and both refunds exist before the call is handled and are asserted
unchanged, so those fields come out true with nothing having happened. The five it
misses are the case, its two note-log rows, the confirmation, and the allocator's
advance — the whole of what the call has to earn.

## Running it

```bash
docker build -t voice-env-06-retail environment
docker run -d --name retail06 -v "$PWD/out:/out" voice-env-06-retail
docker exec retail06 test -f /tmp/task-infra/.ready && echo ready

curl -s http://127.0.0.1:8080/tools | jq '.tools[].name'   # from inside

docker cp tests retail06:/opt/tests
docker cp solution retail06:/opt/solution
docker exec -u agent retail06 bash /opt/solution/solve.sh  # the oracle
docker exec retail06 bash /opt/tests/test.sh               # grade it -> 1.0
docker exec retail06 bash /opt/tests/env_check.sh          # conformance (resets)
```

The oracle runs as the `agent` account, which is also what proves
`/workspace/transcript.txt` is writable by the account that will need to write it.

To collect the final state after a run:

```bash
docker exec retail06 /usr/local/bin/task-init.sh --dump-state
# writes /out/final_state.json (canonical, diffable) and /out/final_state.sql
```

`--reset-db` rebuilds the world from SQL without restarting the server, which is
what the verifier uses to guarantee a clean start.

## Known limits

- **The clock is fixed.** `scenario.scenario_time` is the conversation's time and
  the tools read it instead of wall time, which is what makes runs reproducible.
  Nothing advances it, so the three-to-five business day review window cannot be
  observed to run out.
- **The card token is disclosed on the second read, not on demand.** The desk's
  rule is modelled as a read model rather than as an authorization check,
  because the recording shows a depth and not a permission decision.
- **The payments team is not simulated.** A trace opens and stays open; nothing
  behind it responds.
- **`transfer_to_specialist` records the handoff and ends nothing.** There is no
  specialist behind it.
