# 04 — banking-declined-card-travel

A traveller is standing at a hotel front desk in Portland, Maine with a card that
has just been declined twice for the same 840 dollars. The card is under a
temporary travel review opened by his own activity that morning. Lifting the
review is straightforward; the part that matters is that his available credit
after the hold is 72 dollars, so the room goes through and nothing larger will.

Recorded conversation: `conversations/banking-declined-card-travel/`. Domain policy
and tool contracts: `domains/banking/`. Construction conventions shared by every
task here: `docs/SQL_ENVS.md`.

| | |
|---|---|
| Domain | banking |
| Scenario time | 2026-08-28T14:30:00-04:00 |
| Tools | 16 |
| Recorded tool calls | 8 |
| Database | PostgreSQL 16, 24 tables |

## What this environment is

The published dataset carries this conversation's tool results as authored JSON.
Nothing executes, so an agent that departs from the recorded path has nothing to
depart into.

Here the bank's records are a real database and the sixteen tools are real queries
against it. The recorded results are reproduced because the data reproduces them.
Look up the caller by name and ZIP alone and a second Colin Reeves at the same ZIP
makes the lookup ambiguous; confirm two of the three pieces of activity the review
is holding and the review stays open; lift the review and the available credit
falls to 72 because a real 840-dollar hold now stands against a real credit line.

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

**Two of the three `get_card_account` differences are real state; one is
disclosure depth.** This is the distinction `docs/SQL_ENVS.md` asks to be settled
from the data rather than assumed, and here the recording settles it both ways.
The third read's authorization list differs from the second's because
`resolve_card_restriction` ran in between: it settled the breakfast charge and
re-presented the declined hotel attempt as an approved hold, so a query for
pending authorizations returns a different set for the same reason a real one
would. The two decline reads are different: the same two rows come back both
times, the second carrying where each attempt came from and why each failed and
dropping the status already stated. Nothing changed, so nothing is modelled as
having changed — `card_section_view` holds one row per section per depth,
`declines` has two and every other section has one, and
`card_section_read_cursor` records how many reads a section has served so the
depth is a queryable fact rather than a counter in the handler.

**The two hotel declines fail for different reasons, and that is a column.**
`transactions.reason` holds `travel_review` on the first attempt and
`prior_review_open` on the second. The distinction is the whole content of what
the agent has to tell the caller — the first attempt tripped the review, the
second was refused because the review it tripped was still open — and deriving it
from ordering at read time would have made a fact about the bank's decision into
an artifact of a `SELECT`.

**Available credit is a stored line reduced by real activity, not a number to
report.** `card_accounts.available_credit` reads 912 while the review is open and
72 after, because the re-presented hold is an actual `transactions` row for 840
against that line. The recorded conversation never states 72; it follows from
912 minus 840. Storing the post-resolve figure instead would have made the
warning that a larger hold will not clear unfalsifiable, which is exactly the
thing this call turns on.

**A travel notice is a note, and the tool says so in its own result.**
`travel_notices.authorization_guaranteed` is a stored column pinned to false
rather than a constant the handler emits, and the knowledge base record on hotel
holds carries the same field. Both come out of the database, so an agent that
tells the caller the notice will get him approved is contradicted by the record it
just read rather than only by the policy document.

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
| trusted_channels | 92 |
| service_cases | 61 |
| identity_verifications | 37 |
| channel_confirmations | 25 |
| card_accounts | 268 |
| transactions | 576 |
| card_restrictions | 50 |
| restriction_transactions | 90 |
| travel_notices | 42 |
| referrals | 106 |
| self_service_sessions | 62 |
| session_deliveries | 99 |
| notifications | 46 |
| card_products | 12 |
| welcome_offers | 15 |
| kb_records | 53 |
| card_section_view | 7 |

The generated ledger sits in August 2026 so that the estate's activity is
contemporary with the scenario clock; a card whose only recent transactions were
the four planted ones would make the recorded reads unique for the wrong reason.
The knowledge base's effective dates sit before the scenario time for the same
reason.

The population is there so the lookups have work to do:

- **Two customers named Colin Reeves at billing ZIP 20005.** The recorded lookup
  carries the card's last four as well; name and ZIP alone are refused. Nine
  customers share the Reeves surname.
- **A `lost_card_block` on the second Colin Reeves.** It is the wrong kind of
  restriction to lift by confirming activity, so an agent that reaches for the
  resolve tool on the wrong profile is refused rather than quietly succeeding.
- **Fifty restrictions across four kinds and both lifecycle states**, with 90
  linked activity rows in `restriction_transactions`, so partial confirmation is a
  general behaviour of the tool and not a special case wired for this row.
- **Six distinct decline reasons in the ledger**, so `travel_review` and
  `prior_review_open` are two values among several rather than the only two
  present.
- **Forty-two travel notices, none of which guarantee anything.** A notice on the
  account is not by itself evidence that an authorization will clear, which is the
  distinction this call has to make out loud.
- **One knowledge record answers this conversation's query and 52 do not.** The
  hotel-hold record has three close neighbours by design — a general
  authorization-hold record, a travel-notice record, and a record on what a travel
  notice does when a review is already open — so the recorded question has to
  discriminate rather than merely find something about holds. Retrieval orders by
  `priority`, then pattern length, then identifier, so the outcome is
  deterministic wherever more than one pattern matches. Effective dates span 37
  distinct days from September 2025 to August 2026.

## Verification

Two layers, answering two different questions. See
[`docs/SQL_ENVS.md`](../../docs/SQL_ENVS.md) for the full contract.

### Conformance: is this backend faithful?

`tests/env_check.sh` rebuilds the database from `001` through `004`, replays the
eight recorded calls in order, and requires every response to match the recording
byte for byte after canonical JSON normalization. A divergence is a defect in this
backend, not in the recording. It involves no agent, and because it destroys
whatever a run left behind it runs last.

```
== replaying the recorded call sequence
  [ok  ]   bt-001  lookup_customer
  [ok  ]   bt-002  verify_customer_identity
  [ok  ]   bt-003  get_card_account
  [ok  ]   bt-004  get_card_account
  [ok  ]   bt-005  resolve_card_restriction
  [ok  ]   bt-006  search_knowledge_base
  [ok  ]   bt-007  get_card_account
  [ok  ]   bt-008  create_travel_notice
  8/8 calls reproduced exactly
== scoring conformance
tool calls reproduced: 8/8
final-state fields matched: 48/48

conformant: true
```

Two of the 48 asserted facts are derived rather than quoted, and are marked as
such in the file. `available_credit: 72.0` is 912 minus the 840-dollar hold;
`131fc14b-fc2e-4ec9-ad07-248d01f7d54b.settlement_state: settled` follows from the third read
returning only the hotel hold as pending. Both are consequences of recorded
results, not additions to them.

### Grading: did the run handle the call well?

`tests/test.sh` scores whatever an agent left behind. It does not reset the
database and does not replay the recorded calls, because the recorded path is one
correct route through this call and not the only one.

```
required facts:  48/48
collateral damage: 0 row(s) the gold path never touched
transcript: 1697 characters of plain text
communicated:    5/5
[diagnostic] tool calls made: 8; gold write tools used: 3/3 (similarity to one reference path, not gating)

reward: 1.0  (db 1.0 x communicate 1.0)
```

The gold path touches **15 rows out of 1,769**, against three to six for its
sibling banking tasks. The difference is entirely `resolve_card_restriction`
doing real work.
Lifting one travel review is not a status flip: it settles the activity the review
was holding, re-presents the attempt that tripped it, and moves the credit line.
The fifteen are the card account, the restriction itself, the three
`restriction_transactions` links joining it to the activity being confirmed,
three `transactions` rows — the first hotel attempt, which gains a
`represented_as` pointer, the breakfast authorization, which settles, and the
inserted 840-dollar hold — the travel notice, the verification record, and five
`card_section_read_cursor` rows that exist only because the recording reads the
card three times. Those cursor rows are excluded from the damage check wholesale
anyway, so the work area that is actually load-bearing is ten rows.

Those fifteen are the agent's legitimate work area and are governed by the
required facts; the other 1,754 are held to the initial state, which is what makes
meddling with an uninvolved customer detectable.

Speech is graded from `/workspace/transcript.txt` against five requirements: the
$912 available credit, that a hotel will routinely authorise more than the room
total for incidentals, that the whole hold has to fit inside the remaining line,
that neither the lifted review nor the travel note guarantees any particular
purchase is approved, and that the hold will stay pending for days after checkout.
Each accepts several surface forms, so "nine hundred and twelve dollars" passes as
readily as "$912". None of the five is visible in the database — the card ends up
active with the review lifted and the note on file whether or not the caller was
warned about any of it — which matters here because he is standing at a hotel desk
about to have the card run again.

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
excluding the two tables wholesale, and it matters more in this task than in the
other three: this conversation issues neither a session nor a confirmation, so
every row in both tables belongs to somebody else and nothing an agent adds to
either could be legitimate work. A trusted-channel challenge started against an
uninvolved customer is a real code sent to a real phone; under the wholesale
exclusion it scored a clean 1.0, and the row is now named directly.

`card_section_read_cursor` is the one table still excluded whole, and it carries
the weight of that principle here. `get_card_account` serves each section at a
depth that depends on how many times it has been read, so every card read
rewrites a cursor row by design and a cursor row exists only because something was
read. Excluding its columns rather than the table would buy nothing.

### Controls

| Control | Result |
|---|---|
| Conformance replay | 8/8 byte-exact, 48/48 fields |
| Oracle as the agent account, then graded | 1.0 |
| Idle container, nothing ran | 0.0 — 39/48 facts, 0/5 said |
| **Different route: email lookup, clock read, five card reads one section at a time** | **1.0** |
| 24 extra read-only calls after a correct handling | 1.0, 32 calls logged |
| Unrequested second travel notice on the caller's card | 0.0 — `db` 0.0, `communicate` 1.0 |
| Self-service session opened on a workflow this call never involved | 0.0 — 2 damaged rows |
| Trusted-channel challenge started against an uninvolved customer | 0.0 — 2 damaged rows |
| No-guarantee caveat left unsaid | 0.0 — `db` 1.0, `communicate` 0.0 |
| Nothing said at all | 0.0 — `db` 1.0, `communicate` 0.0 |
| Agent account reading `/var/lib/task-data/` or the admin token | permission denied |
| `GET /_admin/state` with a wrong token | 401 |
| `lookup_customer` on name and ZIP alone | 409, `candidate_count: 2` |
| Resolve with one of three activity rows confirmed | 409, `unconfirmed_count: 2` |
| Resolve the other Colin Reeves' `lost_card_block` | 409, not resolved by confirming activity |
| `travel_notices` read before the notice is created | 200, empty list |
| Knowledge base on what a travel notice does | 200, `authorization_guaranteed: false` |
| `confirmed_transaction_ids` empty | 400, schema violation |
| `create_travel_notice` without `return_date` | 400, schema violation |
| `include: ["balance"]` | 400, not in the enum |

The idle control reporting 39 of 48 facts is the intended reading: the card, the
two declined attempts, the breakfast charge and the open review all exist before
the call is handled. The nine that fail are the verification, the card's status and
available credit, the re-presentation pointer, the settled breakfast charge, the
inserted hold, the review's status and `resolved_at`, and the travel notice.

The fourth row is the point of the grading layer, and this task is where the read
cursor makes it vivid. That route resolves the caller by email address rather than
by name, ZIP and card last four, calls `get_current_time`, which the recording
never does, reads the knowledge base before lifting the review rather than after,
and reads the card five times asking for one section at a time instead of three
times in the recorded groupings. The cursor it leaves behind is in a different
place from gold's — `declines` served twice and every other section once, against
gold's `authorizations` and `declines` twice — and the run still scores 1.0,
because how deeply an agent chose to look is not a property of how well it handled
the call.

The read-freedom control adds 24 more read-only calls on top of a correct
handling, including a `travel_notices` section read that inserts a *sixth* cursor
row the gold path never created. The run stays at 1.0 with 32 calls logged.

The damage rows are the converse. `create_travel_notice` will happily mint a
second notice under a suffixed id, and that is not a harmless duplicate — it puts
a trip on the card the customer never mentioned, and the note is visible to fraud
review:

```
required facts:  48/48
collateral damage: 1 row(s) the gold path never touched
  DAMAGE travel_notices[travel-notice-colin-lisbon] inserted
transcript: 1697 characters of plain text
communicated:    5/5
[diagnostic] tool calls made: 9; gold write tools used: 3/3 (similarity to one reference path, not gating)

reward: 0.0  (db 0.0 x communicate 1.0)
failed because: 1 damaged row(s)
```

All 48 required facts still hold and all five things were still said, because the
travel notice the call was supposed to produce is still there and correct. The run
scores zero on the row nobody asked for, which is exactly what the required-facts
assertion cannot see on its own.

## Running it

```bash
docker build -t voice-env-04-banking environment
docker run -d --name banking04 -v "$PWD/out:/out" voice-env-04-banking
docker exec banking04 test -f /tmp/task-infra/.ready && echo ready

curl -s http://127.0.0.1:8080/tools | jq '.tools[].name'   # from inside

docker cp tests banking04:/opt/tests
docker cp solution banking04:/opt/solution
docker exec -u agent banking04 bash /opt/solution/solve.sh  # the oracle
docker exec banking04 bash /opt/tests/test.sh               # grade it -> 1.0
docker exec banking04 bash /opt/tests/env_check.sh          # conformance (resets)
```

The oracle runs as the `agent` account, which is also what proves
`/workspace/transcript.txt` is writable by the account that will need to write it.

To collect the final state after a run:

```bash
docker exec banking04 /usr/local/bin/task-init.sh --dump-state
# writes /out/final_state.json (canonical, diffable) and /out/final_state.sql
```

`--reset-db` rebuilds the world from SQL without restarting the server, which is
what the verifier uses to guarantee a clean start.

## Known limits

- **The clock is fixed.** `scenario.scenario_time` is the time every mutation
  stamps, and the tools read it instead of wall time. This conversation never
  calls `get_current_time`, so no recorded result depends on the clock's value; it
  is what makes the removed review's `resolved_at` reproducible.
- **The hotel is not a modelled party.** The desk running the card again is
  narrated in conversation, and the environment represents its effect as the
  re-presentation the resolve performs. There is no tool the hotel calls, so the
  hold appearing is a consequence of lifting the review rather than of an external
  event the agent waits for.
- **The re-presented hold carries no merchant location or time.** It is created by
  the resolve rather than submitted by the merchant, and the recorded third read
  shows exactly those fields absent. They stay null until a merchant submission
  would fill them, which no tool here does.
- **Nothing measures whether the caller was told about the 72 dollars.** The
  arithmetic is in the database and the hold rules are in the knowledge base, so
  the facts are available; whether they were said out loud is a transcript
  property, not a state property.
