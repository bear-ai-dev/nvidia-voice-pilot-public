# 01 — banking-account-email-card-application

A customer is locked out of the email address his bank profile points at. Online
banking keeps sending the change code to that same address, so the loop cannot
close from his side. Once it does close he wants a travel card, and he spends the
second half of the call reading the application back to the agent field by field.

Recorded conversation: `conversations/banking-account-email-card-application/`.
Domain policy and tool contracts: `domains/banking/`. Construction conventions
shared by every task here: `docs/SQL_ENVS.md`.

| | |
|---|---|
| Domain | banking |
| Scenario time | 2026-08-27T10:46:31-04:00 |
| Tools | 16 |
| Recorded tool calls | 15 |
| Database | PostgreSQL 16, 24 tables |

## What this environment is

The published dataset carries this conversation's tool results as authored JSON.
Nothing executes, so an agent that departs from the recorded path has nothing to
depart into.

Here the bank's records are a real database and the sixteen tools are real queries
against it. The recorded results are reproduced because the data reproduces them,
not because they are stored. Ask for an SMS confirmation on a profile whose only
handset is on file but no longer enrolled and it is refused; give the wrong billing ZIP and
the verification comes back unverified with only the factor that actually matched;
open a session against the withdrawn Summit Reserve Elite and the product is not
there to open one against.

## Schema

Twenty-four tables. Shapes come from the `result_schema` definitions in
`domains/banking/tool_registry.json`; the lifecycle vocabularies come from
`domains/banking/policy.md` and are `CHECK` constraints, so an illegal state fails
in the database rather than only in the tool layer.

**Catalogs** — `card_products`, `welcome_offers`, `kb_records`,
`workflow_profiles`, `delivery_channels`, `notification_templates`.

**People** — `customers`, `trusted_channels`, `service_cases`,
`identity_verifications`, `channel_confirmations`.

**Card accounts** — `card_accounts`, `transactions`, `card_restrictions`,
`restriction_transactions`, `travel_notices`, plus the three card-section read
model tables (`card_section_policy`, `card_section_view`,
`card_section_read_cursor`).

**Referrals and self-service** — `referrals`, `self_service_sessions`,
`session_deliveries`, `notifications`, `specialist_transfers`.

**Infrastructure** — `scenario` (the clock), `id_allocator`, `tool_call_log`.

The whole registry is served from this one schema even though this conversation
exercises about half of it. An agent that reaches for a card read or a referral
lookup off the recorded path has to get an answer from the same estate the
recorded calls read, so those tables are populated rather than stubbed.

Five design points are worth calling out, because each replaces something a naive
implementation would hard-code.

**The confirmation outcome is a property of the channel, not of the poll.** The
customer completes the SMS challenge on his handset, through a path no tool can
watch. `trusted_channels.confirmation_completes` and
`confirmation_verified_at` record whether this channel's owner completes a
challenge and when the completion is stamped, so the poll writes the transition it
reads rather than assuming success. A channel whose owner never completes leaves a
poll at `sent` indefinitely, and 12 of the 99 generated channels are enrolled but
never complete.

**The verification identifier comes from the reason the customer called.** The
recorded record is `613f42b7-69cd-4767-bf41-9a9db63ff731`, which is the profile's
key and the slug of its open service case. That case is a row in
`service_cases`, with a unique partial index enforcing at most one open case per
customer, so re-verifying inside one piece of work returns the same record instead
of minting a second one — which is what makes the identifier quotable in a later
argument at all.

**Two knowledge records are views of the catalog, not copies of it.**
`kb_records.projection` names an assembly the handler performs:
`travel_card_matches` selects the active travel products and `welcome_offers`
joins live offers to live products. The recorded two-product answer falls out
because exactly two travel products are current. Summit Reserve Elite is a travel
card with no foreign-transaction fee and lounge membership, and it is excluded
only by `active = false`; a lapsed Summit Journey offer and an active offer on the
withdrawn product make both halves of the offer filter load-bearing.

**A workflow's session surface is data.** `workflow_profiles` holds, per workflow,
which fields that workflow's session reports: a `card_application` session reports
`save_and_continue` and `credit_pull_authorized`, a `referral_status` session
reports `visible_stages`, a `transaction_dispute` session reports `claim_id` and
an access location. A `NULL` column means the field is not part of that surface,
so the difference between the three recorded session shapes across tasks 01, 02,
and 03 is rows rather than branches.

**The masked delivery address is derived after the mutation.** The recorded
session delivery masks to `j***@outlook.com`. The profile's pre-change address is
seeded at `relaymail.example` precisely so it masks to something else: the value
in the result can only be right if the delivery read the address the email change
had just written.

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
| trusted_channels | 102 |
| service_cases | 70 |
| identity_verifications | 47 |
| channel_confirmations | 25 |
| card_accounts | 280 |
| transactions | 607 |
| card_restrictions | 55 |
| restriction_transactions | 98 |
| travel_notices | 51 |
| referrals | 125 |
| self_service_sessions | 57 |
| session_deliveries | 86 |
| notifications | 46 |
| card_products | 12 |
| welcome_offers | 15 |
| kb_records | 53 |
| workflow_profiles | 3 |

The population is there so the lookups have work to do:

- **Two customers named Johnny Monroe.** The recorded call resolves on the account
  id, so the collision changes nothing there; resolving by name alone is refused
  as ambiguous. Another 12 customers share the Monroe surname.
- **A retired handset on the target profile**, still on file and no longer
  enrolled. The recorded lookup reports exactly one channel, so this row is what
  the enrolment filter excludes. Note the tool takes a channel *type* rather than
  a channel id and selects the enrolled channel of that type, so the retired row
  cannot be addressed directly — it exists to be skipped, and the refusal it
  demonstrates is reachable only on a profile with no enrolled channel of the
  type asked for.
- **Thirteen knowledge records answer the recorded queries and 40 do not.**
  Patterns are POSIX regular expressions matched case-insensitively, ordered by
  `priority`, then pattern length, then identifier, so retrieval is deterministic
  when several patterns match. Fourteen of the 40 are deliberate near-misses
  placed next to a retrieved record so that matching has to discriminate rather
  than merely find something: the current welcome-offer listing sits beside a
  record for the offer's eligibility exclusions and another for an expired
  previous version of it, the housing-payment field sits beside two further
  application fields, and a foreign-transaction-fee exemption record carries a
  pattern the recorded travel-card question also brushes and which loses to it on
  priority. Effective dates span 37 distinct days from September 2025 to August
  2026, so the base reads as a published history rather than a flat snapshot.
- **A withdrawn travel product and two lapsed offers**, so both catalog filters
  can be seen to matter.
- **Verification profiles that cannot be satisfied by talking.** 25 customers
  require the calling channel to match and 30 percent of those did not call from
  it; the target profile's own factors are checked against its columns, so a wrong
  ZIP yields `unverified` with a partial `matched_methods`.

## Verification

Two layers, answering two different questions. See
[`docs/SQL_ENVS.md`](../../docs/SQL_ENVS.md) for the full contract.

### Conformance: is this backend faithful?

`tests/env_check.sh` rebuilds the database from `001` through `004`, replays the
fifteen recorded calls in order, and requires every response to match the
recording byte for byte after canonical JSON normalization. A divergence is a
defect in this backend, not in the recording. It involves no agent, and because
it destroys whatever a run left behind it runs last.

```
== replaying the recorded call sequence
  [ok  ]   bc-001  lookup_customer
  [ok  ]   bc-002  get_current_time
  [ok  ]   bc-003  verify_customer_identity
  [ok  ]   bc-004  start_trusted_channel_confirmation
  [ok  ]  bc-004b  get_trusted_channel_confirmation
  [ok  ]   bc-005  update_customer_email
  [ok  ]   bc-006  search_knowledge_base
  [ok  ]   bc-007  search_knowledge_base
  [ok  ]   bc-008  search_knowledge_base
  [ok  ]   bc-009  create_secure_self_service_session
  [ok  ]   bc-010  search_knowledge_base
  [ok  ]   bc-011  search_knowledge_base
  [ok  ]   bc-012  search_knowledge_base
  [ok  ]   bc-013  get_secure_self_service_session
  [ok  ]   bc-014  search_knowledge_base
  15/15 calls reproduced exactly
== scoring conformance
tool calls reproduced: 15/15
final-state fields matched: 31/31

conformant: true
```

### Grading: did the run handle the call well?

`tests/test.sh` scores whatever an agent left behind. It does not reset the
database and does not replay the recorded calls, because the recorded path is one
correct route through this call and not the only one.

```
required facts:  31/31
collateral damage: 0 row(s) the gold path never touched
transcript: 2221 characters of plain text
communicated:    6/6
[diagnostic] tool calls made: 15; gold write tools used: 4/4 (similarity to one reference path, not gating)

reward: 1.0  (db 1.0 x communicate 1.0)
```

The gold path touches **6 rows out of 1,863** — the customer's profile, the
verification record scoped to the open email-change case, the trusted-channel
confirmation, the application session, its secure-message delivery and its
email-notification delivery. Those six are the agent's legitimate work area and
are governed by the required facts; the other 1,857 are held to the initial
state, which is what makes meddling with an uninvolved customer detectable.

Speech is graded from `/workspace/transcript.txt` against six requirements: the
$95 annual fee on the card he is applying for, the 40,000-point welcome bonus,
the $3,000 spend condition attached to it, that Summit Reserve carries no
automatic free checked bag, that approval is not guaranteed and no phone agent
can move underwriting, and that the financial fields on the application are his
to fill in rather than the agent's. Each accepts several surface forms, so
"ninety-five dollars" passes as readily as "$95".

### Session status is asserted as a set

`expected_final_state.json` writes the application session's status as

```json
"status": { "_any_of": ["issued", "open_not_submitted"] }
```

because nothing about handling this call correctly requires reading the session
back. Reading one is what moves it from `issued` to `open_not_submitted`, since
the opening happens in online banking where no tool can watch. The assertion used
to pin `open_not_submitted`, the value the recording happens to leave behind, so
an agent that issued the application and never confirmed it failed on a field
describing nothing it had done wrong. The alternate-route control below is exactly
that agent, and it now scores 1.0 with the session sitting at `issued`. Task 02's
identical defect pointed the other way, which is what makes this a property of the
assertion rather than of any one recording.

### Where the two mechanisms deliberately overlap

`channel_confirmations.status` is the exception, and it is asserted as the fixed
string `verified`. That is not an oversight. `update_customer_email` refuses
unless the confirmation is already verified, and only
`get_trusted_channel_confirmation` moves it there, so every route that
successfully changes the address has necessarily polled:

```
### confirmation status before any poll ###
a241c1d7-d6c0-4489-9406-65a28d3bca66|sent|
### update_customer_email with the confirmation unpolled ###
{"error": {"type": "refused", "message": "trusted-channel confirmation is not verified", "detail": {"confirmation_status": "sent"}}}
HTTP 409
### now poll it, then retry ###
{"confirmation_id": "a241c1d7-d6c0-4489-9406-65a28d3bca66", "status": "verified", "verified_at": "2026-08-27T10:48:39-04:00"}
{"status": "updated", "primary_email": "johnny.monroe.travel@outlook.com", "notification_email": "johnny.monroe.travel@outlook.com", "login_identifier_changed": false, "transition_security_notices": ["old_email", "new_email"]}
HTTP 200
```

The same column is simultaneously declared read-volatile for the damage check, so
it is dropped before a confirmation row is hashed. The two rules are answering
different questions and do not conflict: a stranger's confirmation may be read
freely, while this customer's must be verified before the address moves. Because
the combination is indistinguishable from an authoring mistake,
`expected_final_state.json` carries a `_forced_read_fields` directive naming the
write that forces the read and why.

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
excluding the two tables wholesale, and the difference is not academic. A
trusted-channel challenge started against a customer this call has nothing to do
with is a real code sent to a real phone; under the wholesale exclusion it scored
a clean 1.0, and the row is now named directly. Only
`card_section_read_cursor` — plus `scenario`, `id_allocator` and
`tool_call_log` — is still excluded whole, because it holds nothing but read state
and so has no insert worth catching.

### Controls

| Control | Result |
|---|---|
| Conformance replay | 15/15 byte-exact, 31/31 fields |
| Oracle as the agent account, then graded | 1.0 |
| Idle container, nothing ran | 0.0 — 26/31 facts, 0/6 said |
| **Different route: name-and-ZIP lookup, no clock read, session never read back** | **1.0** |
| 24 extra read-only calls after a correct handling | 1.0, 39 calls logged |
| Unrequested secure message to the caller | 0.0 — `db` 0.0, `communicate` 1.0 |
| Dispute session opened on a workflow this call never involved | 0.0 — 2 damaged rows |
| Trusted-channel challenge started against an uninvolved customer | 0.0 — 2 damaged rows |
| Annual fee left unsaid | 0.0 — `db` 1.0, `communicate` 0.0 |
| Nothing said at all | 0.0 — `db` 1.0, `communicate` 0.0 |
| Agent account reading `/var/lib/task-data/` or the admin token | permission denied |
| `GET /_admin/state` with a wrong token | 401 |
| `lookup_customer` on the name alone | 409, `candidate_count: 2` |
| Email change with the confirmation still at `sent` | 409, not verified |
| Confirmation on a profile whose handset is on file but not enrolled | 409, no enrolled sms channel |
| Session against the withdrawn Summit Reserve Elite | 404 |
| `account_id` passed as a number | 400, schema violation |

The idle control reporting 26 of 31 facts is the intended reading: the customer's
name, the factors the profile requires and its login-identifier kind are all true
before the call is handled. The five that fail are the two address fields and the
three records the conversation creates.

The fourth row is the point of the grading layer. That route resolves the caller
by name and billing ZIP instead of by account id, never calls
`get_current_time`, supplies a factor the profile does not require, polls the
confirmation twice, asks the knowledge base different questions in a different
order, delivers the application to the secure message centre only, and never
reads the session back — so the session ends at `issued` rather than
`open_not_submitted`. It scores 1.0. Under the previous single-layer verifier this
route was not measurable at all.

The read-freedom control is the one that proves the column-level exclusion is
doing work rather than the check being switched off. Five of the 24 extra calls
genuinely move a row belonging to another customer: three strangers' sessions go
`issued` → `open_not_submitted`, and two strangers' confirmations move off
`sent` — to `expired`, since their challenges had already lapsed against the
frozen clock. Every one of those writes is invisible to the
damage check, and the run stays at 1.0 with 39 calls logged.

The next three rows are the converse, and the third is why the policy changed.
An inserted session is caught along with its delivery child; an inserted
confirmation is caught on its own account, and nothing else would have caught
it — `channel_confirmations` has no child table:

```
required facts:  31/31
collateral damage: 2 row(s) the gold path never touched
  DAMAGE channel_confirmations[confirmation-email-change-SF287951] inserted
  DAMAGE identity_verifications[verification-000001] inserted
transcript: 2221 characters of plain text
communicated:    6/6
[diagnostic] tool calls made: 17; gold write tools used: 4/4 (similarity to one reference path, not gating)

reward: 0.0  (db 0.0 x communicate 1.0)
failed because: 2 damaged row(s)
```

The second damaged row is the stranger's verification record: reaching the
confirmation at all requires verifying that customer first, because every seeded
verification expired at 10:45 and the scenario clock reads 10:46:31. The row that
matters is the first.

## Running it

```bash
docker build -t voice-env-01-banking environment
docker run -d --name banking01 -v "$PWD/out:/out" voice-env-01-banking
docker exec banking01 test -f /tmp/task-infra/.ready && echo ready

curl -s http://127.0.0.1:8080/tools | jq '.tools[].name'   # from inside

docker cp tests banking01:/opt/tests
docker cp solution banking01:/opt/solution
docker exec -u agent banking01 bash /opt/solution/solve.sh  # the oracle
docker exec banking01 bash /opt/tests/test.sh               # grade it -> 1.0
docker exec banking01 bash /opt/tests/env_check.sh          # conformance (resets)
```

The oracle runs as the `agent` account, which is also what proves
`/workspace/transcript.txt` is writable by the account that will need to write it.

To collect the final state after a run:

```bash
docker exec banking01 /usr/local/bin/task-init.sh --dump-state
# writes /out/final_state.json (canonical, diffable) and /out/final_state.sql
```

`--reset-db` rebuilds the world from SQL without restarting the server, which is
what the verifier uses to guarantee a clean start.

## Known limits

- **The clock is fixed.** `scenario.scenario_time` is the time
  `get_current_time` reports and every mutation stamps, and the tools read it
  instead of wall time, which is what makes runs reproducible. Nothing advances
  it, so an agent cannot observe time passing during a call. The recorded
  conversation opens at 10:45 and the recorded clock read returns 10:46:31;
  `scenario_time` is the latter, and the opening time is kept beside it as
  `conversation_started_at` for reference.
- **The confirmation code is never modelled.** The six digits the caller reads
  aloud are a spoken secret that policy keeps out of tool arguments, so no tool
  accepts one and no column stores one. The completion is recorded on the channel
  instead.
- **`transfer_to_specialist` records the handoff and ends nothing.** There is no
  specialist behind it.
