# 02 — banking-referral-missing-reward

A customer referred his sister, she was approved, and the hundred-dollar bonus has
not arrived. The app says "pending" and has said so for three checks. The answer
turns out to be that approval and qualification are different stages, that the
stage he is stuck on belongs to an account he has no right to see, and that the
banner he read set an expectation the terms never made.

Recorded conversation: `conversations/banking-referral-missing-reward/`. Domain
policy and tool contracts: `domains/banking/`. Construction conventions shared by
every task here: `docs/SQL_ENVS.md`.

| | |
|---|---|
| Domain | banking |
| Scenario time | 2026-08-28T09:09:00-04:00 |
| Tools | 16 |
| Recorded tool calls | 9 |
| Database | PostgreSQL 16, 24 tables |

## What this environment is

The published dataset carries this conversation's tool results as authored JSON.
Nothing executes, so an agent that departs from the recorded path has nothing to
depart into.

Here the bank's records are a real database and the sixteen tools are real queries
against it. The recorded results are reproduced because the data reproduces them,
not because they are stored. Ask for the other Daniel Brooks' referral from this
profile and the read returns nothing, because the query is scoped to the owner and
not to the identifier; ask for the tracker by SMS and the template is not approved
for that channel; look up the caller by name and the lookup refuses, because the
register holds two of him.

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

**The invitation date is stored twice, deliberately.** The recorded result reads
`"invited_at": "August 2"`, which is the string the customer hears, not an ISO
date. `referrals.invited_at_display` holds that string and is emitted verbatim;
`invited_on` holds `2026-08-02` so the record stays queryable and orderable.
Deriving the display string from the date would mean inventing a formatter whose
output the recording only appears to agree with; storing only the string would
leave the table unable to answer a date question. See `docs/SQL_ENVS.md` on
human-relative strings.

**The offer is pinned to the invitation, not to the catalog.** The recorded
knowledge answer is a record whose `effective_at` is `2026-08-02` — the day the
invitation went out — and `referrals.offer_version_record_id` points at it. That
is what makes "your invitation was created under the hundred-dollar offer" a fact
about this referral rather than a restatement of whatever is currently being
advertised. The catalog holds a `$150` offer too, so the two are visibly
different questions.

**The absence of a trusted channel is a filter result.** The recorded lookup
reports no trusted channels at all. The profile does hold a handset, but it was
never enrolled for confirmation challenges, so the enrolment filter excludes it.
Without that row the empty list would be an empty table, and the enrolment check
would not be observable.

**The third-party boundary is a join, not a rule in the handler.** `get_referrals`
filters on `referring_customer_id`, and `send_secure_notification` will only name a
session or referral the caller owns. The other Daniel Brooks' referral `RF8244`
exists with a full record; asking for it from this profile returns an empty list
rather than a refusal, because from this profile it simply is not there.

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
| trusted_channels | 118 |
| service_cases | 68 |
| identity_verifications | 42 |
| channel_confirmations | 25 |
| card_accounts | 283 |
| transactions | 582 |
| card_restrictions | 50 |
| restriction_transactions | 90 |
| travel_notices | 55 |
| referrals | 127 |
| self_service_sessions | 53 |
| session_deliveries | 79 |
| notifications | 40 |
| card_products | 12 |
| welcome_offers | 15 |
| kb_records | 53 |
| workflow_profiles | 3 |

The population is there so the lookups have work to do:

- **Two customers named Daniel Brooks**, at addresses one token apart
  (`daniel.brooks17@gmail.com` and `daniel.brooks@gmail.com`). The recorded lookup
  carries the full address and resolves uniquely; the name alone is refused. Nine
  customers share the Brooks surname.
- **127 referrals across the estate**, 90 of them still `purchase_pending` and 76
  with an application status other than approved, so "approved but pending" is one
  combination among several rather than the only shape the table can hold.
- **The target profile's address is reserved out of the generator**, because
  generated addresses follow the same pattern and a duplicate would make the
  recorded lookup ambiguous for a reason unrelated to the conversation.
- **19 channels on file but not enrolled**, including the caller's own handset, and
  7 that are enrolled but whose owners never complete a challenge.
- **Four lapsed welcome offers**, so an offer question has to look at state.
- **Three knowledge records answer this conversation's queries and 50 do not.**
  Fourteen of those 50 are deliberate near-misses standing next to a retrieved
  record, which matters most here because the pinned `ce7d58b0-b5fc-42cb-a6e0-7a57929e1205` record has to win
  against a general referral record, an offer-eligibility record, and a record for
  an expired previous version of the offer. Retrieval orders by `priority`, then
  pattern length, then identifier, so the outcome is deterministic wherever more
  than one pattern matches. Effective dates span 37 distinct days, so the base
  carries a published history rather than one flat snapshot.

## Verification

Two layers, answering two different questions. See
[`docs/SQL_ENVS.md`](../../docs/SQL_ENVS.md) for the full contract.

### Conformance: is this backend faithful?

`tests/env_check.sh` rebuilds the database from `001` through `004`, replays the
nine recorded calls in order, and requires every response to match the recording
byte for byte after canonical JSON normalization. A divergence is a defect in this
backend, not in the recording. It involves no agent, and because it destroys
whatever a run left behind it runs last.

```
== replaying the recorded call sequence
  [ok  ]   br-001  lookup_customer
  [ok  ]   br-002  get_current_time
  [ok  ]   br-003  verify_customer_identity
  [ok  ]   br-004  get_referrals
  [ok  ]   br-005  search_knowledge_base
  [ok  ]   br-006  search_knowledge_base
  [ok  ]   br-007  create_secure_self_service_session
  [ok  ]   br-008  send_secure_notification
  [ok  ]   br-009  search_knowledge_base
  9/9 calls reproduced exactly
== scoring conformance
tool calls reproduced: 9/9
final-state fields matched: 35/35

conformant: true
```

### Grading: did the run handle the call well?

`tests/test.sh` scores whatever an agent left behind. It does not reset the
database and does not replay the recorded calls, because the recorded path is one
correct route through this call and not the only one.

```
required facts:  35/35
collateral damage: 0 row(s) the gold path never touched
transcript: 2164 characters of plain text
communicated:    5/5
[diagnostic] tool calls made: 9; gold write tools used: 3/3 (similarity to one reference path, not gating)

reward: 1.0  (db 1.0 x communicate 1.0)
```

The gold path touches **4 rows out of 1,826** — the verification record scoped to
the open referral case, the tracker session against ce7d58b0-b5fc-42cb-a6e0-7a57929e1205, its secure-message
delivery, and the email notification telling the customer a secure message is
waiting. Those four are the agent's legitimate work area and are governed by the
required facts; the other 1,822 are held to the initial state, which is what makes
meddling with an uninvolved customer detectable.

Seven of the 35 required facts describe the referral row itself, and they assert
it **unchanged**: still `approved` on the application, still `purchase_pending` on
qualification, still pinned to the $100 offer. The reward this call is about does
not post during it, and no tool in the registry writes to `referrals` at all, so
the assertion is there to fix what a correct handling must leave alone rather than
to catch a mutation the tools make available.

Speech is graded from `/workspace/transcript.txt` against five requirements:
that approval alone is not what earns the bonus, the 90-day qualifying-purchase
window, the up-to-two-billing-cycle posting delay, that the agent cannot see or
disclose the referred customer's spending, and that his sister must not reapply.
Each accepts several surface forms, so "two billing cycles" passes as readily as
"2 billing cycles". None of the five is visible in the database: this is a call
about why nothing has happened yet, so nearly all of its value is in what gets
said.

### Session status is asserted as a set

`expected_final_state.json` writes the tracker session's status as

```json
"status": { "_any_of": ["issued", "open_not_submitted"] }
```

because nothing about handling this call correctly requires reading the session
back. Reading one is what moves it from `issued` to `open_not_submitted`, since
the opening happens in online banking where no tool can watch. This task is the
clearest case in the repo of why that matters. The recording issues the tracker
and never looks at it again, so the assertion used to pin `issued` — and an agent
that sensibly confirmed the thing it had just issued therefore failed, on a field
describing nothing it had done wrong. The alternate-route control below is exactly
that agent, and it now scores 1.0 with the session sitting at
`open_not_submitted`. Tasks 01 and 03 had the same defect pointing the other way,
which is what makes this a property of the assertion rather than of any one
recording.

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
excluding the two tables wholesale, and the difference is not academic. This call
never confirms a channel, so every row in `channel_confirmations` belongs to
somebody else — reading one must be free, and starting a challenge against one of
those profiles must not be, because that is a real code sent to a real phone.
Under the wholesale exclusion such a row scored a clean 1.0; it is now named
directly. Only `card_section_read_cursor` — plus `scenario`, `id_allocator` and
`tool_call_log` — is still excluded whole, because it holds nothing but read state
and so has no insert worth catching.

### Controls

| Control | Result |
|---|---|
| Conformance replay | 9/9 byte-exact, 35/35 fields |
| Oracle as the agent account, then graded | 1.0 |
| Idle container, nothing ran | 0.0 — 32/35 facts, 0/5 said |
| **Different route: name-and-ZIP lookup, referral fetched by id, tracker read back** | **1.0** |
| 24 extra read-only calls after a correct handling | 1.0, 33 calls logged |
| Unrequested travel notice on the caller's card | 0.0 — `db` 0.0, `communicate` 1.0 |
| Card-application session opened on a workflow this call never involved | 0.0 — 2 damaged rows |
| Trusted-channel challenge started against an uninvolved customer | 0.0 — 2 damaged rows |
| Do-not-reapply warning left unsaid | 0.0 — `db` 1.0, `communicate` 0.0 |
| Nothing said at all | 0.0 — `db` 1.0, `communicate` 0.0 |
| Agent account reading `/var/lib/task-data/` or the admin token | permission denied |
| `GET /_admin/state` with a wrong token | 401 |
| `lookup_customer` on the name alone | 409, `candidate_count: 2` |
| The other Brooks' referral `RF8244` read from this profile | 200, empty list |
| Tracker notification requested over SMS | 409, template not approved |
| Notification naming a referral the caller does not own | 404 |
| `billing_zip` passed as a number | 400, schema violation |

The idle control reporting 32 of 35 facts is the intended reading: the referral's
own seven fields are true before the call is handled, because the referral already
exists and the call does not change it. The three that fail are the verification,
the session and the notification, each reported once as a missing row.

The fourth row is the point of the grading layer. That route resolves the caller
by name and billing ZIP instead of by email address, never calls
`get_current_time`, asks `get_referrals` for the one referral by id rather than
for all of them, reverses the order of its knowledge-base questions, and reads the
tracker back after issuing it — which the recording never does. It scores 1.0, and
the session it leaves behind reads

```
b549b0e6-fced-4596-8794-db42a62476a6|open_not_submitted|2026-08-28T09:09:00-04:00
```

Under the previous single-layer verifier this route was not measurable at all, and
under the old fixed-string assertion it failed on that one word.

The read-freedom control is the one that proves the column-level exclusion is
doing work rather than the check being switched off. Five of the 24 extra calls
genuinely move a row belonging to another customer: three strangers' sessions go
`issued` → `open_not_submitted`, and two strangers' confirmations move off
`sent` — to `expired`, since their challenges had already lapsed against the
frozen clock. Every one of those writes is invisible to the
damage check, and the run stays at 1.0 with 33 calls logged.

The next three rows are the converse, and the third is why the policy changed.
An inserted session is caught along with its delivery child; an inserted
confirmation is caught on its own account, and nothing else would have caught
it — `channel_confirmations` has no child table:

```
required facts:  35/35
collateral damage: 2 row(s) the gold path never touched
  DAMAGE channel_confirmations[confirmation-email-change-SF203004] inserted
  DAMAGE identity_verifications[verification-000001] inserted
transcript: 2164 characters of plain text
communicated:    5/5
[diagnostic] tool calls made: 11; gold write tools used: 3/3 (similarity to one reference path, not gating)

reward: 0.0  (db 0.0 x communicate 1.0)
failed because: 2 damaged row(s)
```

The second damaged row is the stranger's own verification record, which starting
the challenge requires; the row that matters is the first.

## Running it

```bash
docker build -t voice-env-02-banking environment
docker run -d --name banking02 -v "$PWD/out:/out" voice-env-02-banking
docker exec banking02 test -f /tmp/task-infra/.ready && echo ready

curl -s http://127.0.0.1:8080/tools | jq '.tools[].name'   # from inside

docker cp tests banking02:/opt/tests
docker cp solution banking02:/opt/solution
docker exec -u agent banking02 bash /opt/solution/solve.sh  # the oracle
docker exec banking02 bash /opt/tests/test.sh               # grade it -> 1.0
docker exec banking02 bash /opt/tests/env_check.sh          # conformance (resets)
```

The oracle runs as the `agent` account, which is also what proves
`/workspace/transcript.txt` is writable by the account that will need to write it.

To collect the final state after a run:

```bash
docker exec banking02 /usr/local/bin/task-init.sh --dump-state
# writes /out/final_state.json (canonical, diffable) and /out/final_state.sql
```

`--reset-db` rebuilds the world from SQL without restarting the server, which is
what the verifier uses to guarantee a clean start.

## Known limits

- **The clock is fixed.** `scenario.scenario_time` is the time
  `get_current_time` reports and every mutation stamps, and the tools read it
  instead of wall time. Nothing advances it, so an agent cannot observe time
  passing during a call.
- **The referred sister is not a modelled customer.** Nothing in the registry can
  reach her account from this profile, so there is no row to leak and no row to
  read. The privacy boundary is enforced by scoping rather than by trusting the
  agent, and it is therefore not a test of restraint.
- **The exact referral deadline is stored but never disclosed.** The recorded
  answer says the deadline has not passed and that the date lives in the tracker,
  so `referrals.deadline_on` is filler that no result depends on.
- **`transfer_to_specialist` records the handoff and ends nothing.** There is no
  specialist behind it.
