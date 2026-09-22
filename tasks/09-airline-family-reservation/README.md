# 09 — airline-family-reservation

A grandmother is booking two nonstop seats from Phoenix to Washington for herself
and her twelve-year-old grandson. She is travelling with a folding walker, the
website offered her three Washington airports and she does not know which one she
needs, and she has a travel certificate she thinks is worth $200. Nothing may be
quoted to her that the backend has not computed, and nothing may be charged that
she has not authorized.

Recorded conversation: `conversations/airline-family-reservation/`. Domain policy
and tool contracts: `domains/airline/`. Construction conventions shared by every
task here: `docs/SQL_ENVS.md`.

| | |
|---|---|
| Domain | airline |
| Scenario time | 2026-08-26T12:30:00-07:00 |
| Tools | 9 |
| Recorded tool calls | 9 |
| Database | PostgreSQL 16, 27 tables and 1 view |

## What this environment is

The published dataset carries this conversation's tool results as authored JSON.
Nothing executes, so an agent that departs from the recorded path has nothing to
depart into.

Here the airline's records are a real database and the nine tools are real
queries against it. The recorded results are reproduced because the data
reproduces them, not because they are stored. Ask which airports serve the Inner
Harbor and the airport catalog answers with Baltimore first; price basic economy
instead of standard and the fare rows produce a different total; book against the
certificate a second time and the balance the first booking drew down refuses it.

## Schema

Twenty-seven tables. Shapes come from the `result_schema` definitions in
`domains/airline/tool_registry.json`; the lifecycle vocabularies come from
`domains/airline/policy.md` and are `CHECK` constraints, so an illegal state
fails in the database rather than only in the tool layer.

**Catalogs** — `airports`, `destination_areas`, `airport_area_links`, `flights`,
`fare_options`, `flight_availability`, `connecting_itineraries`,
`connecting_itinerary_segments`, `baggage_fees`, `mobility_device_rules`,
`insurance_plans`, `confirmation_code_pool`.

**People and money on file** — `customers`, `payment_methods`,
`travel_certificates`, `identity_verifications`.

**Shopping** — `flight_searches`, `fare_quotes`.

**Reservations** — `reservations`, `travelers`, `reservation_mobility_devices`,
`payment_allocations`, `certificate_redemptions`, `specialist_transfers`.

**Infrastructure** — `scenario` (the clock), `id_allocator`, `tool_call_log`, and
the `reservation_payment_totals` view, which exposes what each reservation's
tenders add up to keyed by reservation, so the verifier can assert that a split
payment reconciles without summing allocation rows itself.

Five design points are worth calling out, because each replaces something a
naive implementation would hard-code.

**No price is stored as a total.** `fare_options` holds a base fare and a tax per
flight per fare family, `baggage_fees` holds a per-bag tariff per fare family,
`mobility_device_rules` holds an accessibility tariff, and `insurance_plans`
holds a per-traveller premium inside a trip-cost band. The three figures the
recording reads aloud are sums over those rows, computed on every pricing call:

```
fare and taxes, per traveller   (241.90 + 37.20) + (241.90 + 37.20)  =  558.20
                              x 2 travellers                         = 1116.40
two checked bags, standard        2 x 35.00                          =   70.00
                                                                       -------
fare, taxes, and checked bags                                          1186.40

folding walker, tariff in effect 2026-07-01, does not count as a bag       0.00

insurance: 558.20 per traveller falls in the standard band 500.00 <
           trip cost <= 750.00, which prices at 47.30 x 2            =    94.60
                                                                       -------
total with insurance                                                   1281.00
```

`fare_quotes` caches what a pricing call computed, which is what a later booking
is checked against, but the cached amounts are recomputed from the same rows
rather than trusted: `004_scenario.sql` seeds the quote with its identity and
expiry and leaves all four amount columns `NULL`, so the recorded total cannot be
reproduced by a number somebody typed into the seed. The generator asserts the
sums at author time, which is how a fare edit that breaks the recording is caught
before the container is built rather than during replay.

**A travel certificate is a balance, not a coupon.** `travel_certificates`
carries `original_amount` and `available_balance` with
`CHECK (available_balance <= original_amount)`, `book_reservation` locks the row
`FOR UPDATE` and draws it down with an `UPDATE`, and `certificate_redemptions` is
the ledger behind the running figure. Booking twice cannot spend the same $200
twice: after the recorded booking the balance is `0.00`, the status is `redeemed`,
and a second booking that offers the same certificate is refused.

**A split payment is rows, not a sentence.** `payment_allocations` holds one row
per tender, and the handler verifies the rows sum to `charged_total` before it
commits. The recording's $200 certificate and $1,081 card are two rows whose
`allocated_total` the view reports as `1281.00` against a `charged_total` of
`1281.00`; the recorded response's `payment_allocation` array is a projection of
those rows in tender order rather than an assembled pair of numbers.

**Ticketing and payment are separate columns because the policy separates
them.** `reservations.status` runs
`draft/quoted/pending_payment/confirmed/ticketed`, `ticketing_status` runs
`pending/ticketed/failed`, and `payment_status` runs
`authorized/captured/failed`. A `CHECK` constraint makes a ticket impossible
against money that was only authorized:

```
CHECK (ticketing_status <> 'ticketed'
       OR (status IN ('confirmed', 'ticketed') AND payment_status = 'captured'))
```

The estate exercises all of it: 194 reservations ticketed against captured money,
40 confirmed with an authorization that has not been captured and so are not
ticketed, 45 awaiting payment, and 101 in `draft` or `quoted` with no payment
position at all.

**The record locator comes out of a pool, not a counter.** Airlines issue
six-character locators from pre-generated stock rather than by counting, so
`confirmation_code_pool` is a table and allocation is an `UPDATE` that stamps
`issued_at` on the lowest unissued row. The recorded `B9RT6M` sits at sequence
381, immediately after the 380 codes the existing estate has spent, so the first
booking of a run allocates it and a second booking gets `ELGJYP` instead of it.
Searches, quotes, and specialist transfers are genuinely sequential and come from
`id_allocator` with pre-seeded `next_value` and `template`; verification records
are named after the customer they clear.

## Two searches, two answers

`search_flights` is called twice with different arguments, so the difference is
data rather than a progressive read. The first call caps stops at zero and gets
the nonstop pair with both fare families. The second allows one stop and tolerates
five hours of layover, and the cheapest itinerary that satisfies it is a
connection through Chicago, so the result is a comparison: `best_connection` with
$62 of total savings and 170 additional minutes each way, and no `outbound` or
`return` block, because the direct flights were already returned and the caller is
being asked about the connection.

Both figures are computed. The saving is the difference between the cheapest fare
family available on the direct pair and the cheapest available on the connection,
summed over four real segments in `connecting_itinerary_segments`; the additional
duration is the larger of the two directions' elapsed times minus the direct
flight's. Tighten the layover tolerance to two hours and the connection's
145-minute Chicago wait puts it out of range, so the same call falls back to the
direct pair.

## `list_supported_airports` resolves an area, not a string

The caller says she is staying near the National Mall. `destination_areas` holds
landmarks, districts, and metros with the phrases a caller might use, and
`airport_area_links` holds distance, ground-access minutes, and a proximity rank
per airport. A query matches an area when it contains one of that area's search
terms; a landmark outranks the metro that contains it, which is what makes
"Washington, DC National Mall" resolve to the Mall rather than to the DC metro
area even though the metro's term is the longer match.

The `recommendation_basis` the policy requires before calling an airport closest
or easiest is a column on the area, because it is read aloud and has to be a
sentence rather than fragments assembled at call time. Fifteen areas are seeded,
so asking about the Inner Harbor returns Baltimore first with the basis that
belongs to it, and asking about the metro rather than the landmark returns Reagan
with a different basis.

## Seed

`environment/gen_seed.py` runs at author time with a fixed RNG seed and writes
`sql/002_reference.sql` and `sql/003_population.sql`. It is excluded from the
image by `.dockerignore`, so the container carries the world but not the machine
that made it. `sql/004_scenario.sql` is hand-written and holds the entities this
conversation touches, with each value marked as either something a recorded
result revealed or plausible filler the recording never exposed.

| Table | Rows |
|---|---|
| airports | 40 |
| destination_areas / airport_area_links | 15 / 33 |
| flights | 200 |
| fare_options | 400 |
| flight_availability | 2000 |
| connecting_itineraries / segments | 7 / 28 |
| insurance_plans | 15 |
| mobility_device_rules | 13 |
| customers | 106 |
| payment_methods | 197 |
| travel_certificates | 66 |
| identity_verifications | 50 |
| flight_searches | 32 |
| fare_quotes | 51 |
| reservations | 380 |
| travelers | 702 |
| reservation_mobility_devices | 44 |
| payment_allocations | 423 |
| certificate_redemptions | 23 |
| confirmation_code_pool | 430 |

The population is there so the lookups have work to do:

- **Two customers named Linda Marie Carver**, with different dates of birth and
  different emails, plus four more Carvers. Verification takes three factors and
  a contradiction on any of them fails rather than falling through to the other
  profile.
- **A Marcus Carver holding the same itinerary.** Price the recorded flights and
  read his profile and `duplicate_reservation` comes back `true`, which is the
  same check that returns `false` for the caller.
- **Six profiles carrying an elevated-verification hold.** Every supplied factor
  matches and the answer is still `needs_more_factors`, so a cleared verification
  is a property of the record rather than of the arithmetic on factors.
- **66 certificates: 9 expired, 10 void, 27 already redeemed, 20 valid**, one of
  them the caller's second certificate with $45 left on it, which is valid and
  too small to cover this trip. A code belonging to another account answers 404
  rather than disclosing that it exists.
- **238 sold-out fare classes** across the availability grid, including the second
  Phoenix–Reagan nonstop on the caller's outbound date, which is why the recorded
  search returns one outbound flight rather than two. It is available on other
  dates and searching them returns it.
- **13 mobility-device tariff lines, two of them versions of the same device.**
  The handler takes the newest version in effect at the scenario clock, so the
  folding walker's 2026-07-01 rule applies and its 2025-06-01 predecessor does
  not.
- **Routes with no service.** A search for a pair the schedule does not fly, such
  as Boston to San Diego, returns the availability check with no flights rather
  than inventing one, and an airport code the catalog does not serve answers 404.

## Verification

Two layers, answering two different questions. See
[`docs/SQL_ENVS.md`](../../docs/SQL_ENVS.md) for the full contract.

### Conformance: is this backend faithful?

`tests/env_check.sh` rebuilds the database, replays the nine recorded calls, and
requires every result to match the recording byte for byte after canonical JSON
normalization. This is the layer the pricing arithmetic answers to: the three
figures read aloud on the call are sums over fare, baggage, mobility and insurance
rows recomputed on every pricing call, so any drift between those rows and the
recorded totals surfaces here rather than as a quiet difference in a score. A
divergence is a defect here, not in the recording. It scores no agent, and it
destroys whatever a run left behind, so it runs last.

```
  [ok  ]   af-001  list_supported_airports
  [ok  ]   af-002  search_flights
  [ok  ]   af-003  search_flights
  [ok  ]  af-003b  check_mobility_device_requirements
  [ok  ]   af-004  calculate_itinerary_price
  [ok  ]  af-004b  verify_customer_identity
  [ok  ]   af-005  get_customer_profile
  [ok  ]  af-005b  validate_travel_certificate
  [ok  ]   af-006  book_reservation
  9/9 calls reproduced exactly
final-state fields matched: 71/71

conformant: true
```

### Grading: did the run handle the call well?

`tests/test.sh` grades what an agent left behind. It does not reset the database
and does not replay the recorded calls, because the recorded path is one correct
route and not the only one.

```
required facts:  71/71
collateral damage: 0 row(s) the gold path never touched
transcript: 2529 characters of plain text
communicated:    6/6
[diagnostic] tool calls made: 9; gold write tools used: 2/2 (similarity to one reference path, not gating)

reward: 1.0  (db 1.0 x communicate 1.0)
```

The gold path touches **14 rows out of 5,649**: the reservation, its two
travellers, the walker entry, the two tenders and the view that reconciles them,
the certificate and its redemption row, the verification, the issued pool code,
the two `flight_availability` rows the seats came out of, and the priced quote.
Those are the agent's work area and are governed by the required facts rather than
by the damage check. The other 5,635 rows are held to the initial state, which is
what makes booking a second trip for somebody who never called detectable.

This domain needs `ignore_tables` rather than `read_volatile_columns`, and the
reason is worth stating: two of its reads persist rows. `search_flights` files a
`flight_searches` row for any route and date pair the cache has not seen, so an
agent that checks a second Washington airport leaves searches behind, and
`calculate_itinerary_price` inserts a `fare_quotes` row the first time an itinerary
is priced and rewrites the amounts on every later call. Column exclusion cannot
help with an *insert*, so both tables are excluded wholesale. What the gold quote
must end up holding is still asserted field by field in
`expected_final_state.json`, which is where the five priced amounts come back
under the required facts rather than under the damage check.
`identity_verifications` is `append_tolerated` instead: an agent that mistypes a
date of birth and retries genuinely leaves a second record.

Speech is graded from `/workspace/transcript.txt` against six requirements: the
$62 the one-stop comparison actually saves, that the insurance has exclusions she
must read the plan document for, that $200 came off the trip from the certificate,
that the $1,081 remainder went to the Visa, that a confirmed reservation does not
confirm seats, and that her grandson's documentation is a question for official
government guidance rather than for the agent. Each accepts several surface forms.

### Controls

| Control | Result |
|---|---|
| Conformance replay | 9/9 byte-exact, 71/71 fields |
| Oracle as the agent account, then graded | 1.0 |
| Idle container, nothing ran | 0.0 — 54/71 facts, 0/6 said |
| **Different route: identity verified first and fumbled once, certificate checked before anything was priced, one-stop search before the nonstop, two lookups skipped, itinerary priced twice** | **1.0** |
| 24 extra read-only calls after a correct handling | 1.0, 33 calls logged |
| An unrelated customer booked onto the same two flights | 0.0 — 71/71 facts, 6/6 said, 5 damaged rows |
| Insurance exclusions left unsaid | 0.0 — `db` 1.0, `communicate` 0.0 |
| Nothing said at all | 0.0 — `db` 1.0, `communicate` 0.0 |
| Agent account reading `verifier-data/` or the admin token | permission denied |
| `GET /_admin/state` with no token or a wrong one | 401 |
| Same certificate offered to a second booking | refused, balance still `0.00`, one redemption row |
| Same itinerary booked again for the same customer | refused, the existing locator named |
| `max_stops` above the registry maximum, a lower-case airport code, a fare class outside the enum, `certificate_id` omitted | 400 with the violated constraint named |
| `total_savings` deliberately rendered `62` instead of `62.0` | conformance 8/9, state still 71/71, grading still 1.0 |

The fourth row is the point of the grading layer. That route verifies identity
before anything else rather than sixth, mis-hears the date of birth once and
retries, validates the certificate before an itinerary exists, runs the one-stop
comparison ahead of the nonstop search, never calls `list_supported_airports` or
`get_customer_profile` at all, asks about the walker by a different name, and
prices the itinerary twice. It ends with the same reservation, the same split
payment and the same drawn-down certificate, and it scores 1.0. Under the previous
single-layer verifier it was not measurable at all.

The meddling control is the one the required-facts assertion cannot catch on its
own. Booking Aiko Achebe — a real customer with a real card on file, on the same
Phoenix–Reagan pair — leaves all 71 facts true and all six things still said, and
scores zero on seven damaged rows: the reservation, its traveller, its tender, the
view over that tender, the pool code it consumed, and the two `flight_availability`
rows. Those two are inside the work area, because the gold path decremented them
too, but the extra booking leaves them at a count that is neither the initial one
nor the gold path's, which the damage check treats as damage. A side effect
confined entirely to shared rows is therefore caught as well.

The last control is the one that establishes the conformance layer is worth
running, and it also shows the two layers are independent in the direction that
matters. Rendering the connection's saving as `62` where the recording says `62.0`
fails conformance at 8 of 9 calls while the final-state check still passes 71/71
and a graded oracle run still scores 1.0. The wire format is wrong and the state
is not, and each layer says exactly that.

The idle container reporting 54 of 71 fields is the intended reading: the
certificate, the quote, and the pool entry exist before the call is handled, so
the fields describing what they already are come out true. The seventeen the call
has to earn — the reservation, its travellers, the walker entry, the two tenders
and their reconciliation, the drawn-down balance and status, the redemption, the
verification, the five priced quote amounts, and the issued locator — are the ones
the reward turns on.

Also checked: no reservation's allocations disagree with its `charged_total`, a
ticketed reservation against uncaptured money is rejected by the schema, and a
certificate balance cannot be raised above what was issued.

## Running it

```bash
docker build -t voice-env-09-airline environment
docker run -d --name airline -v "$PWD/out:/out" voice-env-09-airline
docker exec airline test -f /tmp/task-infra/.ready && echo ready

curl -s http://127.0.0.1:8080/tools | jq '.tools[].name'   # from inside

docker cp tests airline:/opt/tests
docker cp solution airline:/opt/solution
docker exec -u agent airline bash /opt/solution/solve.sh  # the oracle
docker exec airline bash /opt/tests/test.sh               # grade it -> 1.0
docker exec airline bash /opt/tests/env_check.sh          # conformance (resets)
```

The oracle runs as the `agent` account, which is also what proves
`/workspace/transcript.txt` is writable by the account that will need to write it.

To collect the final state after a run:

```bash
docker exec airline /usr/local/bin/task-init.sh --dump-state
# writes /out/final_state.json (canonical, diffable) and /out/final_state.sql
```

`--reset-db` rebuilds the world from SQL without restarting the server, which is
what the verifier uses to guarantee a clean start.

## Known limits

- **The clock is fixed.** `scenario.scenario_time` is the conversation's time and
  the tools read it instead of wall time, which is what makes runs reproducible.
  Cache-freshness timestamps the recording reads back — when an availability
  check ran, when an airport list was refreshed — are therefore columns on the
  cached thing rather than a monotonic `now`, and an off-path search stamps its
  new cache row with the scenario clock instead.
- **A quote names flights, not dates.** The registry's pricing arguments carry no
  dates, so a quote raised off the recorded path takes its dates from the most
  recent availability check on the same route, and failing that from the next
  seeded departure. The recorded quote carries its own.
- **Seat selection is not a tool.** The recording confirms standard economy allows
  advance seats and sends the caller to the reservation screen, so
  `seat_selection_available` is recorded and `confirmed_seats` is empty; there is
  no endpoint that would fill it.
- **`transfer_to_specialist` records the handoff and ends nothing.** There is no
  specialist behind it.
