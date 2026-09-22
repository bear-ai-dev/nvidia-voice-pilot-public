# 03 — banking-transaction-dispute-session

A customer sees a 243.18 charge from a merchant he has never heard of, with a
descriptor that reads `MRKTPLC*8472` and no city attached. He has the card in his
wallet. The charge turns out to be his daughter's patio set, bought through a
marketplace wallet that had his card saved in it, and the right outcome is that no
dispute is ever filed.

Recorded conversation: `conversations/banking-transaction-dispute-session/`. Domain
policy and tool contracts: `domains/banking/`. Construction conventions shared by
every task here: `docs/SQL_ENVS.md`.

| | |
|---|---|
| Domain | banking |
| Scenario time | 2026-02-25T16:20:00-05:00 |
| Tools | 16 |
| Recorded tool calls | 7 |
| Database | PostgreSQL 16, 24 tables |

## What this environment is

The published dataset carries this conversation's tool results as authored JSON.
Nothing executes, so an agent that departs from the recorded path has nothing to
depart into.

Here the bank's records are a real database and the sixteen tools are real queries
against it. The recorded results are reproduced because the data reproduces them,
not because they are stored. Drop the amount from the statement search and the
second charge from the same marketplace order comes back with it; drop the
descriptor and a hardware-store charge for the same amount on the same day comes
back; open a dispute against a transaction that is not on the profile and there is
nothing to open one against.

## Schema

Twenty-four tables, the same design as the other three banking tasks in this set;
each task carries its own copy rather than importing one, so a schema change made
for one conversation cannot silently alter another. Shapes come from the
`result_schema` definitions in `domains/banking/tool_registry.json`; the lifecycle
vocabularies come from `domains/banking/policy.md` and are `CHECK` constraints, so
an illegal state fails in the database rather than only in the tool layer.

**Catalogs** — `card_products`, `welcome_offers`, `kb_records`,
`workflow_profiles`, `delivery_channels`, `notification_templates`.

**People** — `customers`, `trusted_channels`, `service_cases`,
`identity_verifications`, `channel_confirmations`.

**Card accounts** — `card_accounts`, `transactions`, `card_restrictions`,
`restriction_transactions`, `travel_notices`, and the three card-section read
model tables.

**Referrals and self-service** — `referrals`, `self_service_sessions`,
`session_deliveries`, `notifications`, `specialist_transfers`.

**Infrastructure** — `scenario` (the clock), `id_allocator`, `tool_call_log`.

Four design points are worth calling out, because each replaces something a naive
implementation would hard-code.

**The one-dollar pre-authorization is a column on the charge, not a ledger row.**
`transactions.preceded_by_authorization_amount` holds it, because it describes the
posted charge rather than being a second piece of activity. Modelling it as its
own row would have put a one-dollar charge into every statement search that found
the real one, which the recorded result does not show — and it would have made the
detail that actually turns the conversation an artifact of the query rather than a
property of the charge.

**The dispute session's identifier and label come from the transaction.** The
recorded session is `0ae36057-bbbb-42e3-994f-55f064bc374c` and its label is
`Review transaction ending 8472`. `transactions.short_ref` and `resource_label`
hold `8472` and `transaction ending 8472`, and the workflow profile holds the
naming rule (`resource_suffix_source = 'resource_short_ref'`) and the label
template. The alternative is string surgery on the transaction id, which happens
to work on this row and would break on any transaction not named after its own
suffix.

**A session nobody opens keeps reading `issued`.** Opening happens in online
banking, outside every tool, so `self_service_sessions.customer_opens` records
whether this session's owner opens it. The first read after delivery writes the
`open_not_submitted` transition it observes; 16 of the 56 generated sessions are
never opened and stay at `issued` no matter how often they are polled.

**Submission is a state, not a flag the agent sets.** `submitted` is constrained
to agree with `status`, and `claim_id` may only be non-null where the workflow
tracks claims. There is no tool that submits: the customer does that in online
banking. That is what makes "the session exists but the claim does not" a fact the
database can hold rather than a distinction the agent has to remember.

## Seed

`environment/gen_seed.py` runs at author time with a fixed RNG seed and writes
`sql/002_reference.sql` and `sql/003_population.sql`. It is excluded from the
image by `.dockerignore`, so the container carries the world but not the machine
that made it. `sql/004_scenario.sql` is hand-written and holds the entities this
conversation touches, with comments marking which values the recorded results
revealed and which are filler.

| Table | Rows |
|---|---|
| customers | 102 |
| trusted_channels | 110 |
| service_cases | 69 |
| identity_verifications | 30 |
| channel_confirmations | 25 |
| card_accounts | 272 |
| transactions | 572 |
| card_restrictions | 46 |
| restriction_transactions | 80 |
| travel_notices | 49 |
| referrals | 122 |
| self_service_sessions | 56 |
| session_deliveries | 80 |
| notifications | 47 |
| card_products | 12 |
| welcome_offers | 15 |
| kb_records | 53 |
| workflow_profiles | 3 |

The generated ledger sits in February 2026 rather than in whatever month the other
tasks use, because a statement search is scoped by posting date and an estate whose
activity all fell in a different month would make the recorded search unique for
the wrong reason. The knowledge base's effective dates are carried back for the
same reason: a record cannot be in force in February under a July effective date.

The population is there so the lookups have work to do:

- **Three customers named Justin Porter**, one of them seeded deliberately beside
  the target. The recorded lookup carries the address as well as the name; the
  name alone is refused. Nine customers share the Porter surname.
- **Three decoys on the target's own card**, one per filter the recorded search
  uses: the same descriptor and date at a different amount, the same amount and
  date under a different descriptor, and the same amount and descriptor family on
  an earlier date. Removing any one narrowing changes the result, which the
  off-path probes below show directly.
- **42 marketplace charges across the estate** and 60 posted charges preceded by a
  small authorization, so the card-on-file signature is a pattern rather than a
  single planted row.
- **17 dispute sessions already in the estate**, across `issued`,
  `open_not_submitted`, `saved`, `submitted`, `expired`, and `closed`, and 16
  sessions whose owners never open them.
- **The descriptor suffix `8472` is reserved out of the generator**, because
  generated marketplace descriptors take a random four-digit suffix and a second
  charge carrying it could satisfy the recorded search too.
- **Two knowledge records answer this conversation's queries and 51 do not.**
  Fourteen of those 51 sit deliberately next to a retrieved record, which bites
  here because the dispute records have close neighbours: a chargeback-timeline
  record and a provisional-credit record both brush a dispute question, and the
  unsubmitted-session record has to win against a secure-session delivery record.
  Retrieval orders by `priority`, then pattern length, then identifier, so the
  outcome is deterministic wherever more than one pattern matches. Every record
  this task carries is in force before its February 2026 clock, and effective
  dates span 36 distinct days, because a record cannot answer a question under a
  date it has not reached.

## Verification

Two layers, answering two different questions. See
[`docs/SQL_ENVS.md`](../../docs/SQL_ENVS.md) for the full contract.

### Conformance: is this backend faithful?

`tests/env_check.sh` rebuilds the database from `001` through `004`, replays the
seven recorded calls in order, and requires every response to match the recording
byte for byte after canonical JSON normalization. A divergence is a defect in this
backend, not in the recording. It involves no agent, and because it destroys
whatever a run left behind it runs last.

```
== replaying the recorded call sequence
  [ok  ]   bd-001  lookup_customer
  [ok  ]   bd-002  verify_customer_identity
  [ok  ]   bd-003  get_credit_card_transactions
  [ok  ]   bd-004  search_knowledge_base
  [ok  ]   bd-005  create_secure_self_service_session
  [ok  ]   bd-006  get_secure_self_service_session
  [ok  ]   bd-007  search_knowledge_base
  7/7 calls reproduced exactly
== scoring conformance
tool calls reproduced: 7/7
final-state fields matched: 29/29

conformant: true
```

### Grading: did the run handle the call well?

`tests/test.sh` scores whatever an agent left behind. It does not reset the
database and does not replay the recorded calls, because the recorded path is one
correct route through this call and not the only one.

```
required facts:  29/29
collateral damage: 0 row(s) the gold path never touched
transcript: 1867 characters of plain text
communicated:    5/5
[diagnostic] tool calls made: 7; gold write tools used: 2/2 (similarity to one reference path, not gating)

reward: 1.0  (db 1.0 x communicate 1.0)
```

The gold path touches **3 rows out of 1,772** — the verification record scoped to
the open dispute case, the dispute session against the charge, and that session's
secure-message delivery. It is the smallest work area of the four banking tasks,
which follows from the call: the right outcome here is that almost nothing
happens. Those three rows are the agent's legitimate work area and are governed by
the required facts; the other 1,769 are held to the initial state, which is what
makes meddling with an uninvolved customer detectable.

Nine of the 29 required facts describe the disputed charge, and they assert it
**unchanged** — the amount, the descriptor, the posting date, the one-dollar
pre-authorization and the posted status all stay exactly as they were. The session
is asserted with `submitted` false and `claim_id` null, because the customer
decided the charge was his daughter's and closed the page. No tool submits a
dispute, so those two are there to fix what a correct handling must leave alone
rather than to catch a mutation the tools make available.

Speech is graded from `/workspace/transcript.txt` against five requirements: that
a posted charge cannot simply be erased, that provisional credit is not
guaranteed, that no claim exists until he presses submit, that a purchase his own
household made does not go in as fraud, and that leaving the form unsubmitted
waives none of his future dispute rights. Each accepts several surface forms, so
"can't erase the charge" passes as readily as "cannot erase a posted charge". This
conversation ends with nothing filed, so nearly all of its value is in what was
said.

### Session status is asserted as a set

`expected_final_state.json` writes the dispute session's status as

```json
"status": { "_any_of": ["issued", "open_not_submitted"] }
```

because nothing about handling this call correctly requires reading the session
back. Reading one is what moves it from `issued` to `open_not_submitted`, since
the opening happens in online banking where no tool can watch. The assertion used
to pin `open_not_submitted`, the value the recording happens to leave behind, so
an agent that issued the dispute form, told the caller how to use it and never
polled it failed on a field describing nothing it had done wrong. The
alternate-route control below is exactly that agent, and it now scores 1.0 with
the session sitting at `issued`. Task 02's identical defect pointed the other way,
which is what makes this a property of the assertion rather than of any one
recording.

What the assertion keeps is the part that matters: the session is neither expired
nor `submitted`, and it carries no claim.

### Reads are free, inserts are not

`verifier-data/grading.json` declares

```json
"read_volatile_columns": {
  "self_service_sessions": ["status", "opened_at"],
  "channel_confirmations": ["status", "verified_at"]
}
```

Those columns are dropped before a row is hashed, so both tables stay under the
damage check while the transitions a read writes stop mattering. This replaced
excluding the two tables wholesale, and in this task that is the sharpest
difference of the four. The whole call is about *not* filing something, so a
second dispute form — against another charge, or against another customer's — is
precisely the over-eager behaviour the conversation tests for, and under the
wholesale exclusion the session row itself was invisible. A trusted-channel
challenge started against an uninvolved customer had no backstop at all, because
`channel_confirmations` has no child table. Only `card_section_read_cursor` —
plus `scenario`, `id_allocator` and `tool_call_log` — is still excluded whole,
because it holds nothing but read state and so has no insert worth catching.

### Controls

| Control | Result |
|---|---|
| Conformance replay | 7/7 byte-exact, 29/29 fields |
| Oracle as the agent account, then graded | 1.0 |
| Idle container, nothing ran | 0.0 — 27/29 facts, 0/5 said |
| **Different route: name-and-ZIP lookup, clock read, broad search first, form never read back** | **1.0** |
| 24 extra read-only calls after a correct handling | 1.0, 31 calls logged |
| A second dispute form on another of the caller's own charges | 0.0 — 2 damaged rows |
| Unrequested travel notice on the caller's card | 0.0 — `db` 0.0, `communicate` 1.0 |
| Card-application session opened on a workflow this call never involved | 0.0 — 2 damaged rows |
| Trusted-channel challenge started against an uninvolved customer | 0.0 — 2 damaged rows |
| Household-purchase-is-not-fraud warning left unsaid | 0.0 — `db` 1.0, `communicate` 0.0 |
| Nothing said at all | 0.0 — `db` 1.0, `communicate` 0.0 |
| Agent account reading `/var/lib/task-data/` or the admin token | permission denied |
| `GET /_admin/state` with a wrong token | 401 |
| `lookup_customer` on the name alone | 409, `candidate_count: 3` |
| Statement search on amount, descriptor and date | 200, one row |
| Statement search without the amount | 200, two rows |
| Statement search on the amount alone | 200, three rows |
| Statement search on amount and date, no descriptor | 200, two rows |
| Dispute session against a transaction on another profile | 404 |
| `session_id` passed as a number | 400, schema violation |

The idle control reporting 27 of 29 facts is the intended reading: the nine
asserted facts about the charge are true before the call is handled, because the
charge already exists and the call does not change it. The two that fail are the
verification and the session, each reported once as a missing row.

The fourth row is the point of the grading layer. That route calls
`get_current_time`, which the recording never does, resolves the caller by name
and billing ZIP instead of by email address, searches the ledger broadly before
narrowing it, reverses its two knowledge-base questions, and never reads the
dispute form back — leaving the session at `issued`. It scores 1.0. Under the
previous single-layer verifier this route was not measurable at all, and under the
old fixed-string assertion it failed on that one word.

The four statement-search rows are the off-path probes that show the recorded
single-row result is a property of the data rather than of a planted row. Each
narrowing is load-bearing: drop the amount and the second charge from the same
marketplace order comes back with it, drop the descriptor and a hardware-store
charge for the same amount on the same day comes back, and search on the amount
alone and all three appear.

The read-freedom control is the one that proves the column-level exclusion is
doing work rather than the check being switched off. Five of the 24 extra calls
genuinely move a row belonging to another customer: three strangers' sessions go
`issued` → `open_not_submitted`, and two strangers' confirmations move off
`sent` — to `expired`, since their challenges had already lapsed against the
frozen February clock. Every one of those writes is invisible
to the damage check, and the run stays at 1.0 with 31 calls logged.

The damage rows are the converse. A second dispute form is caught along with its
delivery child, which is the fault this conversation is most likely to produce:

```
required facts:  29/29
collateral damage: 2 row(s) the gold path never touched
  DAMAGE self_service_sessions[session-dispute-8473] inserted
  DAMAGE session_deliveries[session-dispute-8473|secure_message] inserted
transcript: 1867 characters of plain text
communicated:    5/5
[diagnostic] tool calls made: 8; gold write tools used: 2/2 (similarity to one reference path, not gating)

reward: 0.0  (db 0.0 x communicate 1.0)
failed because: 2 damaged row(s)
```

All 29 required facts still hold and all five things were still said. The run
correctly scores zero because it filed a form nobody asked for, which is exactly
what the required-facts assertion cannot see on its own.

## Running it

```bash
docker build -t voice-env-03-banking environment
docker run -d --name banking03 -v "$PWD/out:/out" voice-env-03-banking
docker exec banking03 test -f /tmp/task-infra/.ready && echo ready

curl -s http://127.0.0.1:8080/tools | jq '.tools[].name'   # from inside

docker cp tests banking03:/opt/tests
docker cp solution banking03:/opt/solution
docker exec -u agent banking03 bash /opt/solution/solve.sh  # the oracle
docker exec banking03 bash /opt/tests/test.sh               # grade it -> 1.0
docker exec banking03 bash /opt/tests/env_check.sh          # conformance (resets)
```

The oracle runs as the `agent` account, which is also what proves
`/workspace/transcript.txt` is writable by the account that will need to write it.

To collect the final state after a run:

```bash
docker exec banking03 /usr/local/bin/task-init.sh --dump-state
# writes /out/final_state.json (canonical, diffable) and /out/final_state.sql
```

`--reset-db` rebuilds the world from SQL without restarting the server, which is
what the verifier uses to guarantee a clean start.

## Known limits

- **The clock is fixed.** `scenario.scenario_time` is the time every mutation
  stamps, and the tools read it instead of wall time. This conversation never
  reads it back, so no recorded result depends on its value; it is what keeps the
  verification record's timestamps reproducible.
- **The daughter is not a modelled customer.** The household use that resolves the
  call is established in conversation, not in the database, and no tool can
  confirm it. That is the point: the agent cannot certify the purchase, and the
  procedure is what tells it to ask before filing.
- **There is no tool that submits a dispute.** Submission happens in online
  banking, so an environment run cannot reach the submitted state, and the
  recorded outcome — a session that stays open and unsubmitted — is the only one
  reachable through the tools.
- **`transfer_to_specialist` records the handoff and ends nothing.** There is no
  specialist behind it, which matters here because the recorded agent offers one
  if the form fails.
