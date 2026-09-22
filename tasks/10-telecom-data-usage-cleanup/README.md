# 10 — telecom-data-usage-cleanup

A caller wakes to an alert saying he has used 85% of his data and he was asleep
while it happened. The carrier metered 11.8 GB on his line between midnight and
four in the morning. He has 2.2 GB of high-speed data left, nine days of cycle to
spend it in, and a work trip before then.

Recorded conversation: `conversations/telecom-data-usage-cleanup/`. Domain policy
and tool contracts: `domains/telecom/`. Construction conventions shared by every
task here: `docs/SQL_ENVS.md`.

| | |
|---|---|
| Domain | telecom |
| Scenario time | 2026-08-27T19:30:00-05:00 |
| Tools | 8 |
| Recorded tool calls | 8 |
| Database | PostgreSQL 16, 21 tables, 1 view, 4 functions |

## What this environment is

The published dataset carries this conversation's tool results as authored JSON.
Nothing executes, so an agent that departs from the recorded path has nothing to
depart into.

Here the carrier's records are a real database and the eight tools are real
queries against it. The recorded results are reproduced because the data
reproduces them. Ask for the current cycle instead of the last twenty-four hours
and you get 12.8 GB rather than 11.8, because a different set of samples is in
scope. Ask for a two-hour slice of the overnight burst and you get 6.5 GB. Buy
the add-on twice and the balance moves twice.

## The arithmetic, and why none of it is stored

This is the part of the task worth reading closely, because three numbers the
call turns on — 11.8, 2.2, and 7.2 — are nowhere in the schema.

**The allowance is on the plan.** `plans.high_speed_allowance_gigabytes` is 15.00
for Unlimited Start. The recording revealed the plan's identifier and display
name but not its allowance; 15 GB is an inference, and the call checks it. The
caller quotes an alert saying he had used 85%, and 12.80 metered against 15.00 is
85.3%.

**The consumption is in `usage_samples`.** Twenty-five rows in the current cycle:
four hourly samples across the overnight burst (2.90, 3.40, 3.10, 2.40) and
twenty-one daytime samples from the 6th to the 26th totalling 1.00 GB. Nothing
holds 11.8 or 12.8.

**The purchased increment is in `addon_transactions`.** One row per purchase,
never an increment to a column.

Every figure the tools report is then a sum:

| Recorded value | Where it comes from |
|---|---|
| `used_gigabytes` 11.8 | `SUM(gigabytes)` over the four samples overlapping the last-24-hours interval: 2.90 + 3.40 + 3.10 + 2.40 |
| `window_start` 00:00, `window_end` 04:00 | `MIN(window_start)` and `MAX(window_end)` over those same four rows. The request was for the last twenty-four hours; the reported window is where the metered records actually are |
| `remaining_high_speed_gigabytes` 2.2 | `line_high_speed_balance`: 15.00 allowance + 0.00 added − 12.80 consumed in the current cycle |
| `remaining_high_speed_gigabytes` 7.2 after the add-on | the same view, after the purchase inserted a 5.00 row: 15.00 + 5.00 − 12.80 |
| `overage_charge` 0.0 | `SUM(amount)` over `bill_charges` of kind `overage` on the open bill. There are none, because Unlimited Start reduces speed instead of billing overage, so this is an empty sum rather than a stored zero |
| `cycle_resets_in_days` 9 | the calendar-date difference between the cycle end and the scenario date in the account's zone: 5 September minus 27 August |

`line_high_speed_balance` is the view both the usage tool and the add-on tool read
their balance from, and it is included in the state snapshot as its component
parts — allowance, consumed, added, remaining — so a run can be diffed as the four
separate quantities rather than only as the number they produce.

The daytime samples earn their place twice. They are the difference between 11.8
GB used in the window and 12.8 GB used against the allowance, which is why the
window figure and the balance figure are not the same number read twice. And the
earlier cycles carry larger samples than the current one, so a balance that summed
every sample on the line instead of every sample in the line's current cycle would
report a visibly wrong answer rather than an almost-right one.

## Schema

Twenty-one tables and one view. Shapes come from the `result_schema` definitions in
`domains/telecom/tool_registry.json`; the lifecycle vocabularies come from
`domains/telecom/policy.md` and are `CHECK` constraints, so an illegal status
fails in the database rather than only in the tool layer.

**Catalogs** — `plans`, `addon_offers`, `measurement_sources`,
`verification_policies`.

**Account** — `customers`, `lines`, `devices`, `identity_verifications`.

**Billing** — `billing_cycles`, `bills`, `bill_charges`.

**Usage and add-ons** — `usage_samples`, `addon_transactions`.

**Customer-observed** — `customer_reported_device_state`.

**Requests** — `specialist_transfers`.

**Infrastructure** — `scenario` (the clock), `id_allocator`, `tool_clock`,
`tool_clock_cursor`, `tool_access_requirements`, `tool_call_log`, and the
`line_high_speed_balance` view.

Six design points are worth calling out, because each replaces something a naive
implementation would hard-code.

**Cycles and bills are separate tables.** A cycle exists whether or not it has
been billed. That separation is what lets the current cycle's usage be attributed
while its bill is still open, and it is also what makes `bill_reference` correct:
the call says the $40 goes on "your next ClearWave bill" and the result says
`7af62370-8858-4af6-b2cd-2b702fd15586`, because the bill for the cycle in progress is the next one
the customer will receive. A next-bill charge resolves to the open bill of the
current cycle, and if there is no open bill the mutation is refused rather than
inventing somewhere to put the charge.

**The access gate is data.** `verification_policies` holds, per intake channel,
the identity factors verification requires and the account scope it grants;
`tool_access_requirements` holds the scope each tool needs. The recorded call
arrives on the support channel, so its verification grants exactly
`["lines", "usage", "billing"]` — the recorded scope — and the account read is
gated on `lines` rather than on each requested section, because a line's device
and plan are attributes of the line. The retail channel grants
`["lines", "devices", "plans"]` instead, from the same lookup, so the other enum
values mean something.

**The verification identifier is derived, not counted.** A verification is one
record per caller per channel, so `ae1d7bf3-792a-4a70-8673-16c535063f29` is
`customers.slug` and the channel, and re-verifying the same caller refreshes the
record instead of accumulating identical ones. A failed or inconclusive record
carries an empty `access_scope`, and the gate reads the scope rather than the
status, so a record that did not succeed grants nothing by construction.

**The add-on transaction identifier is allocated.** `id_allocator` holds one row
per line whose template is the account stem; the handler completes it with the
offer's size, which is where `dfdab773-2580-4908-91cf-99a2b4826547` comes from, and
appends the issued ordinal from the second purchase onward. A second purchase on
the same line is therefore `dfdab773-2580-4908-91cf-99a2b4826547-2`, not a collision.

**Offer eligibility is computed per line.** `eligibility_status` is not a column
on the offer. The same catalog row is eligible for one line and not for another,
depending on the line's status, whether the plan carries add-ons at all, whether
the offer is autopay-only, and whether the account has an overdue bill. Unlimited
Start deliberately holds exactly one unexpired offer, so the recorded single-offer
result is the whole of what the catalog currently has for that plan rather than
the first row of several.

**Carrier telemetry and customer reports are different tables, and one of them is
write-only to history.** `devices.provisioning_status` and `usage_samples` are
what the network observed. `customer_reported_device_state` is what a caller said,
carrying its own `reported_at_display` for the non-ISO way a past contact is
referred to. No tool in this registry writes to it, and that is deliberate: the
registry has no device-telemetry or customer-report operation, so the StreamBox
figure the caller reads off his screen, the download-over-cellular setting he
finds, the Data Saver switch he flips, and the speed test he runs leave no row
behind. The policy forbids presenting a customer report as carrier telemetry, and
the cheapest way to guarantee that is to give the report nowhere in the carrier's
records to land.

## Timestamps

Every recorded result that carries a timestamp carries a different one: the
verification at 19:31:08, the usage read at 19:31:45, the two bill reads at
19:34:10 and 19:34:22, the offer read at 19:34:46, the add-on at 19:37:35. A real
backend takes those from its own clock, which this environment deliberately does
not have.

So the elapsed offsets are data. `tool_clock` holds one row per tool per
invocation ordinal, and `tool_clock_cursor` counts how many times each tool has
been called. This is the shape `docs/SQL_ENVS.md` prescribes for progressive
section reads and for the same reason: the read count is a row an operator can
query, not a counter inside the server process. Past the recorded offsets the
cursor's step keeps the clock moving forward, so an unrecorded second add-on is
stamped later than the first rather than identically.

The strings themselves come from one place. `iso8601(ts, tz)` renders an instant
as local wall time with an explicit numeric offset, deriving the offset from the
named zone rather than pasting `-05:00` in, and `scenario_iso` applies it with the
zone from `scenario`. Mutations also store the exact emitted string in a
`*_display` column beside the typed one, per `docs/SQL_ENVS.md`, so
`identity_verifications.verified_at_display` and
`addon_transactions.effective_at_display` are what the verifier asserts against.

**The two `get_customer_bills` calls do not need the `section_read_cursor`
pattern.** Their outputs differ, but every difference follows from the arguments:
the first asks for `["cycle"]` and gets the cycle dates and the reset count, the
second asks for `["charges", "overages", "plan_behavior"]` and gets the overage
figure, its currency, and the post-allowance behaviour. The only other difference
is `as_of`, which is the read ordinal, not a deeper read model. Splitting the read
is a policy requirement rather than a backend quirk: the agent gives the reset date
before saying he is opening the bill, so reading everything at once would have
disclosed facts before the call disclosed them.

## Seed

`environment/gen_seed.py` runs at author time with a fixed RNG seed and writes
`sql/002_reference.sql` and `sql/003_population.sql`. It is excluded from the image
by `.dockerignore`, so the container carries the world but not the machine that
made it. `sql/004_scenario.sql` is hand-written and holds the entities this
conversation touches, with each value marked as either exact from the recording or
plausible filler.

Actual counts, read from the built database:

| Table | Rows |
|---|---|
| customers | 106 |
| lines | 284 |
| devices | 251 |
| plans | 7 |
| billing_cycles | 528 |
| bills | 528 |
| bill_charges | 1126 |
| usage_samples | 560 |
| addon_offers | 15 |
| addon_transactions | 52 |
| identity_verifications | 51 |
| customer_reported_device_state | 30 |
| id_allocator | 285 |
| verification_policies | 3 |
| measurement_sources | 2 |

The population is there so the lookups have work to do:

- **Two customers named Benjamin Reed** and eleven surnamed Reed. Name alone does
  not resolve; the date of birth does.
- **A duplicate account pair.** Marisol Okafor exists twice with the same name,
  the same date of birth, and the same mobile number on both records, so a lookup
  on the complete factor set legitimately returns `multiple` rather than picking
  one.
- **Eight customers under an identity hold** and sixteen accounts that are not
  active. A hold makes verification `inconclusive` with every factor matching,
  which is the branch the policy's retry-or-transfer rule exists for; a suspended
  account fails.
- **Twenty-seven suspended lines and one ported out.** An offer read on a
  suspended line returns the plan's current offers marked `ineligible`.
- **Two expired offers on the scenario's own plan**, one withdrawn offer, and one
  autopay-only price. Without the expiry filter the recorded single-offer result
  would be unreproducible.
- **Sixty-five overdue bills.** An overdue balance makes a line ineligible for a
  paid add-on.
- **Two hundred and fifty-two usage samples outside their line's current cycle**,
  so cycle scoping is observable rather than incidental.
- **Eleven failed or inconclusive verifications** from earlier contacts, each with
  an empty access scope.

## Verification

Two layers, answering two different questions. See
[`docs/SQL_ENVS.md`](../../docs/SQL_ENVS.md) for the full contract.

### Conformance: is this backend faithful?

`tests/env_check.sh` rebuilds the database, replays the eight recorded calls, and
requires every result to match the recording byte for byte after canonical JSON
normalization. This is the layer the arithmetic above answers to: 11.8, 2.2 and 7.2
are sums recomputed on every call, and each of the six differing timestamps has to
come out of `tool_clock` at the right invocation ordinal. A divergence is a defect
here, not in the recording. It scores no agent, and it destroys whatever a run left
behind, so it runs last.

```
  [ok  ]   td-001  lookup_customer
  [ok  ]  td-001b  verify_customer_identity
  [ok  ]   td-002  get_customer_account
  [ok  ]   td-003  get_line_data_usage
  [ok  ]   td-004  get_customer_bills
  [ok  ]  td-004b  get_customer_bills
  [ok  ]  td-004c  get_data_addon_offers
  [ok  ]   td-005  add_data_addon
  8/8 calls reproduced exactly
final-state fields matched: 38/38

conformant: true
```

### Grading: did the run handle the call well?

`tests/test.sh` grades what an agent left behind. It does not reset the database
and does not replay the recorded calls, because the recorded path is one correct
route and not the only one.

```
required facts:  38/38
collateral damage: 0 row(s) the gold path never touched
transcript: 1723 characters of plain text
communicated:    6/6
[diagnostic] tool calls made: 8; gold write tools used: 2/2 (similarity to one reference path, not gating)

reward: 1.0  (db 1.0 x communicate 1.0)
```

The gold path touches **10 rows out of 4,136**, but six of the ten are bookkeeping
the damage policy excludes: five `tool_clock_cursor` rows, one per timed tool, and
the add-on allocator. The graded work area is therefore four rows — the
verification record, the add-on transaction, the $40 charge on the open bill, and
the line's balance — and those four are governed by the required facts. The other
4,126 rows are held to the initial state, which is what makes charging a stranger's
line detectable.

`tool_clock_cursor` has to be excluded rather than merely hashed without its
counter column, because the row *is* the counter: `get_line_data_usage`,
`get_customer_bills` and `get_data_addon_offers` each bump theirs on every
invocation, and reading the bill twice would otherwise be damage. `tool_clock`
itself, which holds the recorded offsets and is never written, is deliberately not
excluded — a run that changed it would be caught. `identity_verifications` is
`append_tolerated` instead: the register holds a second Benjamin Reed, so an agent
that resolves the wrong record first and then finds the right one legitimately
leaves an extra verification behind.

Speech is graded from `/workspace/transcript.txt` against six requirements: the
11.8 GB the carrier actually metered overnight, the 2.2 GB left at full speed
before the purchase, that metered usage cannot be credited back however many
settings he turns off, that the plan slows him down rather than billing an overage
and the current bill carries none, the $40 price read from a current offer, and
that the $40 is a single charge landing on his next bill rather than a new monthly
cost. Each accepts several surface forms.

### Controls

| Control | Result |
|---|---|
| Conformance replay | 8/8 byte-exact, 38/38 fields |
| Oracle as the agent account, then graded | 1.0 |
| Idle container, nothing ran | 0.0 — 33/38 facts, 0/6 said |
| **Different route: 7 calls, account read skipped, the two bill reads collapsed into one, usage read twice, offers pulled before the bill** | **1.0** |
| 24 extra read-only calls after a correct handling | 1.0, 32 calls logged |
| The same add-on charged to `line-4045555570`, a stranger's line | 0.0 — 38/38 facts, 6/6 said, 3 damaged rows |
| The no-overage answer left unsaid | 0.0 — `db` 1.0, `communicate` 0.0 |
| Nothing said at all | 0.0 — `db` 1.0, `communicate` 0.0 |
| Agent account listing `/var/lib/task-data/` or reading the admin token | permission denied |
| `GET /_admin/state` with a wrong token | 401 |
| Agent account writing `/workspace/policy.md` | permission denied |
| `gen_seed.py` inside the built image | absent |

The fourth row is the point of the grading layer. That route never calls
`get_customer_account`, pulls the offer catalogue before touching the bill instead
of after, collapses the recorded two `get_customer_bills` reads into one call
asking for all four sections, and reads the usage twice — once for the overnight
window and once for the whole cycle. It makes seven calls where the recording made
eight, and it scores 1.0. Under the previous single-layer verifier it was not
measurable at all, and the earlier version of this file recorded the opposite
result: a run that mutated state off-path still scored 1.0, because `test.sh` reset
the database before scoring and therefore graded the environment rather than the
run.

The meddling control is the one the required-facts assertion cannot catch on its
own. Adding the same 5 GB offer to `line-4045555570` — Hugo Sorensen's line, on the
same plan, whose account carries a seeded verification with `billing` scope, so
nothing refuses it — leaves all 38 facts true and all six things still said, and
scores zero on three damaged rows: his add-on transaction, the $40 charge on his
bill, and his line's balance. The access gate blocks the cruder version of this:
the same call against a line whose account holds no verified identity record comes
back 409 rather than charging anyone.

The idle container reporting 33 of 38 fields is the intended reading: the matched
fields describe the line, the bill, and the plan that already exist before the call
is handled. The five it misses are the verification record, the add-on transaction
and its bill charge — each reported once as a missing row rather than field by
field — plus the two balance columns the purchase moves, `added_gigabytes` and
`remaining_gigabytes`.

Off-path probing, all against the state the recorded call leaves behind:

| Probe | Result |
|---|---|
| `lookup_customer` on the duplicate pair | `{"match": "multiple"}` |
| `lookup_customer` on the other Benjamin Reed | `6dcb2039-012b-4723-a256-13bb7b6467c2-1978`, `unique` |
| `get_line_data_usage` for `current_billing_cycle` | 12.8 GB used, window 2026-08-06T09:00 to 2026-08-27T04:00 |
| `get_line_data_usage` for a custom 01:00–03:00 window | 6.5 GB used, from two samples |
| `get_customer_bills` with `status: historical` | `bill-jul-benjamin`, its own cycle dates |
| `verify_customer_identity` on a held customer | `inconclusive`, all three factors matched, empty scope |
| `get_data_addon_offers` on a suspended line | three current offers, all `ineligible` |
| `get_customer_bills` citing another customer's verification | 409 refused |
| `add_data_addon` on an expired offer | 409 refused |
| `add_data_addon` with `customer_authorized: false` | 409 refused |
| `get_line_data_usage` on an unknown line | 404 |
| Unpatterned mobile number, ISO date of birth, window outside the enum, undeclared property, empty `include` | 400 with the violated constraint named |
| **`add_data_addon` a second time** | `dfdab773-2580-4908-91cf-99a2b4826547-2`, remaining 12.2 |
| **`get_line_data_usage` after both add-ons** | remaining 12.2, used still 11.8 |

The last two are the point. After the second purchase the view reports allowance
15.00, consumed 12.80, added 10.00, remaining 12.20, from two `addon_transactions`
rows and twenty-five `usage_samples` rows; the bill carries two separate $40
`addon` charges. Nothing was overwritten, and the usage figure did not move,
because consumption and allowance are different quantities.

## Running it

```bash
python3 environment/gen_seed.py          # only when changing the population
docker build -t voice-env-10-telecom environment
docker run -d --name telecom -v "$PWD/out:/out" voice-env-10-telecom
docker exec telecom test -f /tmp/task-infra/.ready && echo ready

curl -s http://127.0.0.1:8080/tools | jq '.tools[].name'   # from inside

docker cp tests telecom:/opt/tests
docker cp solution telecom:/opt/solution
docker exec -u agent telecom bash /opt/solution/solve.sh  # the oracle
docker exec telecom bash /opt/tests/test.sh               # grade it -> 1.0
docker exec telecom bash /opt/tests/env_check.sh          # conformance (resets)
```

The oracle runs as the `agent` account, which is also what proves
`/workspace/transcript.txt` is writable by the account that will need to write it.

To collect the final state after a run:

```bash
docker exec telecom /usr/local/bin/task-init.sh --dump-state
# writes /out/final_state.json (canonical, diffable) and /out/final_state.sql
```

`--reset-db` rebuilds the world from SQL without restarting the server, which is
what the verifier uses to guarantee a clean start.

## Known limits

- **The clock is fixed.** `scenario.scenario_time` is the conversation's time and
  the tools read it instead of wall time, which is what makes runs reproducible.
  Nothing advances it, so an agent cannot observe time passing during a call, and
  the per-tool `tool_clock` offsets are a recorded schedule rather than a running
  clock: they are monotonic per tool, not globally ordered across tools.
- **`app_attribution_available` is always false in practice.** It is a property of
  `measurement_sources`, and the second source that would report `true` — a
  handset-side agent — is seeded but not provisioned on any line, because no tool
  in this registry can read one. That is the schema stating the policy's
  aggregate-only constraint rather than a handler asserting it.
- **No usage-dispute or credit path exists.** The policy anticipates one and this
  registry does not provide it, so a disputed measurement can only be escalated.
  `transfer_to_specialist` records the handoff and ends nothing; there is no
  specialist behind it.
- **Human-relative time strings are exercised only in seeded data.** No recorded
  result in this conversation contains a non-ISO time phrase, so the
  `*_display` pattern from `docs/SQL_ENVS.md` appears on
  `customer_reported_device_state.reported_at_display` and on the two mutation
  timestamps rather than on anything the replay compares.
- **One statement in the recording is not backed by any tool.** The agent says
  "CloudPhotos on my end is showing 300 megabytes", which carrier metering cannot
  report and no recorded call returned. It is not seeded as carrier data, and no
  tool here would emit it. The recording's own tool sequence is reproduced exactly;
  that spoken claim is a provenance defect in the conversation, not a gap in this
  backend.
