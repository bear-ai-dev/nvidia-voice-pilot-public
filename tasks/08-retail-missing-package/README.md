# 08 — retail-missing-package

A pair of blue noise-canceling headphones was scanned delivered to an apartment
building's front entrance yesterday afternoon. The front desk has nothing, there
is no driver signature and no delivery photo, and the customer flies out Friday
around noon.

Recorded conversation: `conversations/retail-missing-package/`. Domain policy and
tool contracts: `domains/retail/`. Construction conventions shared by every task
here: `docs/SQL_ENVS.md`.

| | |
|---|---|
| Domain | retail |
| Scenario time | 2026-08-25T15:40:00-04:00 |
| Tools | 9 |
| Recorded tool calls | 7 |
| Database | PostgreSQL 16, 26 tables and 2 views |

## What this environment is

The published dataset carries this conversation's tool results as authored JSON.
Nothing executes, so an agent that departs from the recorded path has nothing to
depart into.

Here Westline's records are a real database and the nine tools are real queries
against it. The recorded results are reproduced because the data reproduces
them, not because they are stored. Ask for the neighbouring order whose reference
ends in the same three digits and the resolver tells you why it lost; create a
replacement against an order the desk has actually unlocked and a real order row
appears with a real allocated reference.

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

**Progressive reads are a row, not a popped list.** `get_order` is called on this
order three times. The first two both ask for `carrier_scans` and return
different things: the first returns the delivery scan the customer already saw,
the second returns the evidence panel — the geofence the scan landed in, the
absence of a unit number and locker, and the mis-scan flag. No call between them
changed anything, so that difference is a repeat-read deepening and is modelled
as one: `section_read_cursor` counts reads served per (order, section) and
`section_view` holds what to emit at each depth. View 0 is a `NULL` payload,
which omits the section rather than returning it empty, and that is how the first
look legitimately shows no evidence panel. Past the deepest view the last one
repeats. The `section_view` payload for view 1 is not hand-written JSON: it is
projected in SQL from the `carrier_scans` row, so the evidence a repeat read
discloses cannot drift away from the scan it claims to describe.

**The third read is not the cursor.** Its `cases` and `notifications` sections
appear because `open_delivery_trace` inserted a case and `send_case_notification`
inserted a notification. Those are real mutations and are modelled as rows; using
the cursor for them would have made a read appear to conjure a case. The
notification's move from `sent` to `delivered` between the send result and the
later read is also real: `notification_templates.delivery_progression` is the
receipt sequence for that template and `notifications.status_index` advances one
step per read, so a later look reports a later state without a clock that
advances.

**Case identifiers are allocated.** `id_allocator` is seeded so the first support
case opened in this conversation issues `8cf648a4-ca60-4387-bc11-ec38f426123a` and a second issues
`WST481663`. The same mechanism hands out replacement-order references, so the
recorded reference is allocated rather than asserted.

**A partial reference is resolved, not matched.** The caller reads out four
digits. `orders` carries, on this caller's own account, three references ending
in `1319`, `9319`, and `7019`, and a fourth on the other Patel account ending in
`3319`. Resolution takes the longest trailing digit match within the verified
account and refuses when two candidates tie, with `scenario` holding the minimum
suffix length the desk will accept. Nothing in the seed may collide with the
scenario order on four digits, and `gen_seed.py` enforces that rather than hoping.

**Human-relative strings are stored beside their typed values.** The recorded
results say `15:18 yesterday` and `18:00 tomorrow`. `carrier_scans` carries both
`scanned_at` and `scanned_at_display`, and `cases` carries both `deadline_at` and
`deadline_display`. The tools emit the display string; the typed column is what
any future arithmetic would use. Deriving the phrasing from the clock would have
put the exact recorded wording in Python, which is what storing it avoids.

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
| orders | 366 |
| order_items | 736 |
| payments | 432 |
| refunds | 458 |
| returns | 166 |
| carrier_scans | 597 |
| cases | 60 |
| case_notes | 25 |
| eligible_resolutions | 26 |
| products | 81 |
| product_variants | 110 |

The population is there so the lookups have work to do:

- **Two Patels and a Patell.** The caller is resolved by the verified email on
  the order; the surname resolves to several people and is not a lookup key.
- **Colliding digit suffixes.** Three of the caller's own orders end in digits
  close to `7319`, and one pair of orders on different accounts shares all four
  trailing digits, so an unscoped reference has a genuine ambiguity to refuse.
- **Twelve of the unrelated cases are closed and eight more resolved**, so an
  agent that treats any case it finds as actionable is wrong a third of the time.
- **Twenty-six resolution rows on other people's traces**, roughly a third of
  which unlock a refund and nothing else. Reading "this case has eligible
  resolutions" as "a replacement can be created" is wrong often enough to notice.
- **Out-of-stock variants**, so an inventory check can fail, and variants whose
  availability the catalog does not know, which is what the scenario product is.
- **Refunds outnumber returns.** A completed return refunds to every tender the
  order was settled on, and money also goes back with no return at all for a
  price adjustment or a reversed shipping charge, so a refund lookup that assumes
  one refund per return picks the wrong row.

## Verification

Two layers, answering two different questions. See
[`docs/SQL_ENVS.md`](../../docs/SQL_ENVS.md) for the full contract.

### Conformance: is this backend faithful?

`tests/env_check.sh` rebuilds the database, replays the seven recorded calls, and
requires every result to match the recording byte for byte after canonical JSON
normalization. Three of the seven are reads of the same order, so this is also the
layer that establishes the evidence panel appears on the second look and not the
first, and that the recorded `15:18 yesterday` and `18:00 tomorrow` come back
exactly as phrased. A divergence is a defect here, not in the recording. It scores
no agent, and it destroys whatever a run left behind, so it runs last.

```
  [ok  ]   rm-001  get_order
  [ok  ]   rm-002  get_order
  [ok  ]   rm-003  get_product
  [ok  ]   rm-004  open_delivery_trace
  [ok  ]   rm-005  update_case
  [ok  ]   rm-006  send_case_notification
  [ok  ]   rm-007  get_order
  7/7 calls reproduced exactly
final-state fields matched: 39/39

conformant: true
```

### Grading: did the run handle the call well?

`tests/test.sh` grades what an agent left behind. It does not reset the database
and does not replay the recorded calls, because the recorded path is one correct
route and not the only one.

```
required facts:  39/39
collateral damage: 0 row(s) the gold path never touched
transcript: 2226 characters of plain text
communicated:    6/6
[diagnostic] tool calls made: 7; gold write tools used: 3/3 (similarity to one reference path, not gating)

reward: 1.0  (db 1.0 x communicate 1.0)
```

The gold path touches **9 rows out of 3,284**, three of them bookkeeping the damage
policy excludes: the carrier-scans read cursor, the view row over it, and the
support-case allocator. The graded work area is the remaining six — the case, the
item on it, its note and the `case_note_log` row addressing that note, the pickup
preference, and the trace confirmation — and those are governed by the required
facts. The other 3,275 rows are held to the initial state, which is what makes
meddling with an unrelated customer's case detectable.

The confirmation's delivery status is asserted as the set
`["sent", "delivered"]` rather than as one value. The row is created `sent` and
each read of it collects the next receipt, so a route that reads its own
confirmation back leaves `delivered` and one that does not leaves `sent`. Both were
observed on correct handlings of this call — the oracle reads it and the alternate
route below does not — and both score 1.0. Pinning either would score whether the
agent looked rather than whether it sent. `status` and `status_index` are also
`notifications`' `read_volatile_columns`, so the table stays under the damage check
and an unrequested notification is still caught as an inserted row.

Speech is graded from `/workspace/transcript.txt` against six requirements: that
the scan evidence is genuinely ambiguous between a misscan and another entrance,
that a trace has to be opened first and nothing ships on its own, the 18:00
tomorrow carrier deadline, that a replacement still needs his approval through the
link or the case number, that the pickup counter is recorded as a preference and
not a promise, and that his original price carries over. Each accepts several
surface forms.

### Controls

| Control | Result |
|---|---|
| Conformance replay | 7/7 byte-exact, 39/39 fields |
| Oracle as the agent account, then graded | 1.0 |
| Idle container, nothing ran | 0.0 — 34/39 facts, 0/6 said |
| **Different route: 8 calls, caller resolved first, evidence read three times, confirmation sent before the preference and never read back** | **1.0** |
| 24 extra read-only calls after a correct handling, nine of them of the notification | 1.0, 31 calls logged |
| Unrequested notification on `WST358772`, an unrelated open case | 0.0 — `db` 0.0, `communicate` 1.0 |
| Carrier deadline left unsaid | 0.0 — `db` 1.0, `communicate` 0.0 |
| Nothing said at all | 0.0 — `db` 1.0, `communicate` 0.0 |
| Agent account reading `verifier-data/` or the admin token | permission denied |
| `GET /_admin/state` with a wrong token | 401 |
| `create_replacement_order` twice on an unlocked order | two orders, references `8821` then `8822` |
| `create_replacement_order` on the disputed order | refused: not eligible for a replacement |
| Four-digit reference scoped to the wrong account | not found |
| Two-digit reference | refused: four candidates tie at two digits |
| `include` carrying a section name the registry does not declare | 400, the enum listed |

The fourth row is the point of the grading layer. That route resolves the caller
with `lookup_customer`, which the recording never calls on this conversation,
addresses the order by its full ten-digit reference rather than the four digits he
read out, splits the carrier evidence across three reads with different section
sets, and sends the trace confirmation before recording the pickup preference
rather than after it. None of that is a different outcome and it scores 1.0. Under
the previous single-layer verifier it was not measurable at all.

The meddling control is the one the required-facts assertion cannot catch on its
own. All 39 facts still hold and all six things were still said; the run scores
zero because it inserted `notifications[notification-WST358772]` against a case
this conversation has nothing to do with, and that row is named in the report. Had
`notifications` been excluded from the damage check wholesale rather than by
column, an unrequested message to a real customer would have been invisible.

The idle container reporting 34 of 39 fields is the intended reading: those
thirty-four describe the order, its disagreeing carrier scan, and the customer,
all of which exist before the call is handled. The five it misses are the case,
its note-log row, its pickup preference, the notification, and the allocator's
advance — the whole of what the call has to earn.

## Running it

```bash
docker build -t voice-env-08-retail environment
docker run -d --name retail08 -v "$PWD/out:/out" voice-env-08-retail
docker exec retail08 test -f /tmp/task-infra/.ready && echo ready

curl -s http://127.0.0.1:8080/tools | jq '.tools[].name'   # from inside

docker cp tests retail08:/opt/tests
docker cp solution retail08:/opt/solution
docker exec -u agent retail08 bash /opt/solution/solve.sh  # the oracle
docker exec retail08 bash /opt/tests/test.sh               # grade it -> 1.0
docker exec retail08 bash /opt/tests/env_check.sh          # conformance (resets)
```

The oracle runs as the `agent` account, which is also what proves
`/workspace/transcript.txt` is writable by the account that will need to write it.

To collect the final state after a run:

```bash
docker exec retail08 /usr/local/bin/task-init.sh --dump-state
# writes /out/final_state.json (canonical, diffable) and /out/final_state.sql
```

`--reset-db` rebuilds the world from SQL without restarting the server, which is
what the verifier uses to guarantee a clean start.

## Known limits

- **The clock is fixed.** `scenario.scenario_time` is the conversation's time and
  the tools read it instead of wall time, which is what makes runs reproducible.
  Nothing advances it, so an agent cannot observe the carrier's deadline pass.
- **Notification delivery advances on read, not on time.** With a fixed clock the
  only honest way to reproduce a receipt that changed between two looks is to
  make the look the thing that advances it.
- **The headphones carry no display name.** The recorded results disclose the
  item only by reference and colour, so `order_items.name` is null rather than a
  name the recording never revealed.
- **`transfer_to_specialist` records the handoff and ends nothing.** There is no
  specialist behind it.
