-- BlueMesa Airlines backend schema.
--
-- Shapes follow domains/airline/tool_registry.json result schemas; the
-- reservation lifecycle and the separation of ticketing from payment come from
-- domains/airline/policy.md. Lifecycle vocabularies are CHECK constraints rather
-- than comments, so an illegal combination -- a ticketed reservation whose money
-- was never captured, for instance -- fails in the database and not only in the
-- tool layer.
--
-- Money is NUMERIC throughout and is never stored as a finished total. Fares,
-- taxes, bag tariffs, insurance premiums, and device fees are separate rows, and
-- every amount a tool emits is summed from them at call time. The one exception
-- is fare_quotes, which caches what a pricing call computed so a later booking
-- can be checked against the number the customer authorized.

BEGIN;

-- Scenario clock and other per-conversation constants. The tools read
-- scenario_time from here rather than from wall time, so a container started
-- next year serves the same world.
CREATE TABLE scenario (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

-- Identifier allocation for the entities the airline numbers sequentially:
-- searches, quotes, and specialist transfers. Record locators are not sequential
-- and come from confirmation_code_pool instead.
CREATE TABLE id_allocator (
    entity_type  TEXT NOT NULL,
    scope        TEXT NOT NULL DEFAULT '',
    next_value   INTEGER NOT NULL,
    template     TEXT NOT NULL,
    PRIMARY KEY (entity_type, scope)
);

-- ---------------------------------------------------------------------------
-- catalogs
-- ---------------------------------------------------------------------------

CREATE TABLE airports (
    code                TEXT PRIMARY KEY CHECK (code ~ '^[A-Z]{3}$'),
    name                TEXT NOT NULL,
    city                TEXT NOT NULL,
    state_or_region     TEXT,
    timezone            TEXT NOT NULL,
    -- Offset in effect for the seeded schedule season. Block times on flights
    -- are derived from it at author time, which is why a 09:10 departure and a
    -- 16:30 arrival three time zones east come to 260 minutes and not 440.
    utc_offset_minutes  INTEGER NOT NULL,
    served              BOOLEAN NOT NULL DEFAULT TRUE
);

-- Destination areas a caller names: a metro, a district, or a landmark. The
-- caller in this conversation asks about the National Mall, not about a city, so
-- the catalog has to hold landmarks and rank them ahead of the metro that
-- contains them.
CREATE TABLE destination_areas (
    area_id                TEXT PRIMARY KEY,
    display_name           TEXT NOT NULL,
    -- Short form used when the recommendation basis names the area.
    short_name             TEXT NOT NULL,
    area_kind              TEXT NOT NULL
        CHECK (area_kind IN ('landmark', 'district', 'metro')),
    -- Phrases a caller might use. A query matches an area when it contains one
    -- of these; the most specific area wins, which is how "Washington, DC
    -- National Mall" resolves to the Mall and not to the metro area.
    search_terms           TEXT[] NOT NULL,
    -- The comparison basis the policy requires an agent to have before calling
    -- an airport closest or easiest. Read aloud, so it is stored as the sentence
    -- the tool emits rather than assembled from fragments.
    recommendation_basis   TEXT NOT NULL,
    -- When this area's airport list was last refreshed. Emitted as retrieved_at.
    -- A fixed-clock environment cannot invent a monotonic now, so the freshness
    -- of the cached list is a column on the cached thing.
    retrieved_at           TEXT NOT NULL
);

CREATE TABLE airport_area_links (
    area_id               TEXT NOT NULL REFERENCES destination_areas(area_id),
    airport_code          TEXT NOT NULL REFERENCES airports(code),
    distance_miles        NUMERIC(6, 1) NOT NULL,
    ground_access_minutes INTEGER NOT NULL,
    -- Emission and recommendation order; 1 is the recommended airport.
    proximity_rank        INTEGER NOT NULL CHECK (proximity_rank >= 1),
    PRIMARY KEY (area_id, airport_code),
    -- The snapshot addresses rows by (area_id, proximity_rank), so it must be
    -- unique or two rows would share one digest entry in the damage check.
    UNIQUE (area_id, proximity_rank)
);

-- One row per scheduled segment. Connecting itineraries are built from these in
-- connecting_itinerary_segments, so a connection's price and elapsed time are
-- sums over real flights rather than a stored pair of numbers.
CREATE TABLE flights (
    flight_id         TEXT PRIMARY KEY,
    carrier           TEXT NOT NULL DEFAULT 'BM',
    flight_number     TEXT NOT NULL,
    origin_code       TEXT NOT NULL REFERENCES airports(code),
    destination_code  TEXT NOT NULL REFERENCES airports(code),
    departure_time    TEXT NOT NULL
        CHECK (departure_time ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'),
    arrival_time      TEXT NOT NULL
        CHECK (arrival_time ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'),
    duration_minutes  INTEGER NOT NULL CHECK (duration_minutes >= 1),
    stops             INTEGER NOT NULL DEFAULT 0 CHECK (stops BETWEEN 0 AND 2),
    arrives_next_day  BOOLEAN NOT NULL DEFAULT FALSE,
    CHECK (origin_code <> destination_code)
);

CREATE INDEX flights_route ON flights (origin_code, destination_code, departure_time);

-- Fare families offered on a segment, decomposed so a quoted price is a sum.
CREATE TABLE fare_options (
    flight_id                      TEXT NOT NULL REFERENCES flights(flight_id),
    fare_class                     TEXT NOT NULL
        CHECK (fare_class IN ('basic_economy', 'standard_economy')),
    base_fare                      NUMERIC(10, 2) NOT NULL CHECK (base_fare >= 0),
    tax_amount                     NUMERIC(10, 2) NOT NULL CHECK (tax_amount >= 0),
    advance_seat_selection_allowed BOOLEAN NOT NULL,
    PRIMARY KEY (flight_id, fare_class)
);

-- Seats left per departure date and fare family. A fare family a search would
-- otherwise return is withheld when it is sold out, so availability is a
-- property of the data rather than an assumption in the handler.
CREATE TABLE flight_availability (
    flight_id       TEXT NOT NULL REFERENCES flights(flight_id),
    departure_date  DATE NOT NULL,
    fare_class      TEXT NOT NULL
        CHECK (fare_class IN ('basic_economy', 'standard_economy')),
    seats_remaining INTEGER NOT NULL CHECK (seats_remaining >= 0),
    PRIMARY KEY (flight_id, departure_date, fare_class)
);

-- A connecting alternative offered for price comparison against the direct
-- pair. Savings and added duration are computed from its segments; only the
-- spoken form of the added duration is stored, because the recording reads it
-- back verbatim as "almost three hours".
CREATE TABLE connecting_itineraries (
    itinerary_id                 TEXT PRIMARY KEY,
    origin_code                  TEXT NOT NULL REFERENCES airports(code),
    destination_code             TEXT NOT NULL REFERENCES airports(code),
    departure_date               DATE NOT NULL,
    return_date                  DATE NOT NULL,
    via_airport_code             TEXT NOT NULL REFERENCES airports(code),
    additional_duration_display  TEXT NOT NULL,
    offered                      BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE INDEX connecting_itineraries_route
    ON connecting_itineraries (origin_code, destination_code, departure_date, return_date);

CREATE TABLE connecting_itinerary_segments (
    itinerary_id           TEXT NOT NULL REFERENCES connecting_itineraries(itinerary_id),
    direction              TEXT NOT NULL CHECK (direction IN ('outbound', 'return')),
    segment_index          INTEGER NOT NULL CHECK (segment_index >= 1),
    flight_id              TEXT NOT NULL REFERENCES flights(flight_id),
    -- Wait after this segment before the next one boards; 0 on the last segment
    -- of a direction. The layover limit a caller states is checked against the
    -- largest of these.
    layover_after_minutes  INTEGER NOT NULL DEFAULT 0
        CHECK (layover_after_minutes >= 0),
    PRIMARY KEY (itinerary_id, direction, segment_index)
);

-- Paid-bag tariff per fare family, per bag, for a round trip.
CREATE TABLE baggage_fees (
    fare_class      TEXT PRIMARY KEY
        CHECK (fare_class IN ('basic_economy', 'standard_economy')),
    per_bag_amount  NUMERIC(10, 2) NOT NULL CHECK (per_bag_amount >= 0),
    currency        TEXT NOT NULL DEFAULT 'USD' CHECK (currency = 'USD')
);

-- Accessibility tariff, versioned by effective date. A lookup takes the newest
-- version in effect at the scenario clock, so the fee and the labeling guidance
-- an agent reads out are the current rule and not the only rule ever written.
CREATE TABLE mobility_device_rules (
    device_type                   TEXT NOT NULL,
    effective_at                  DATE NOT NULL,
    aliases                       TEXT[] NOT NULL DEFAULT '{}',
    counts_as_paid_bag            BOOLEAN NOT NULL,
    fee                           NUMERIC(10, 2) NOT NULL CHECK (fee >= 0),
    currency                      TEXT NOT NULL DEFAULT 'USD' CHECK (currency = 'USD'),
    serial_number_required        BOOLEAN NOT NULL,
    labeling_guidance             TEXT NOT NULL,
    airport_notification_required BOOLEAN NOT NULL,
    -- The tariff line a quote uses when the caller has not yet named a device
    -- type, and the line an unrecognized device type falls back to.
    is_quote_default              BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (device_type, effective_at)
);

-- Trip-insurance tariff. A premium is a per-traveler amount within a trip-cost
-- band, so the quoted insurance for two travelers is a multiplication and not a
-- number typed into a row.
CREATE TABLE insurance_plans (
    plan_id             TEXT PRIMARY KEY,
    display_name        TEXT NOT NULL,
    tier                TEXT NOT NULL CHECK (tier IN ('standard', 'plus', 'legacy')),
    -- Half-open band on the per-traveler fare and taxes: min exclusive, max
    -- inclusive. The bands partition the range, so exactly one plan per tier
    -- applies to any fare.
    min_trip_cost       NUMERIC(10, 2) NOT NULL CHECK (min_trip_cost >= 0),
    max_trip_cost       NUMERIC(10, 2) NOT NULL,
    price_per_traveler  NUMERIC(10, 2) NOT NULL CHECK (price_per_traveler >= 0),
    currency            TEXT NOT NULL DEFAULT 'USD' CHECK (currency = 'USD'),
    document_reference  TEXT NOT NULL,
    active              BOOLEAN NOT NULL DEFAULT TRUE,
    CHECK (max_trip_cost > min_trip_cost)
);

-- Record locators. Airlines issue these from a pre-generated pool rather than
-- from a counter, so the pool is a table and allocation is an UPDATE that marks
-- a code spent. The next unissued code is the one a booking gets, which is what
-- makes the recorded confirmation reproducible without hard-coding it.
CREATE TABLE confirmation_code_pool (
    pool_seq   INTEGER PRIMARY KEY,
    code       TEXT NOT NULL UNIQUE CHECK (code ~ '^[A-Z0-9]{6}$'),
    issued_at  TEXT
);

CREATE INDEX confirmation_code_pool_unissued
    ON confirmation_code_pool (pool_seq) WHERE issued_at IS NULL;

-- ---------------------------------------------------------------------------
-- people and money on file
-- ---------------------------------------------------------------------------

CREATE TABLE customers (
    customer_id            TEXT PRIMARY KEY,
    -- Stable name stem the airline uses when it mints per-customer identifiers,
    -- so a verification record is addressable without a random suffix.
    slug                   TEXT NOT NULL UNIQUE,
    full_name              TEXT NOT NULL,
    date_of_birth          DATE NOT NULL,
    email                  TEXT NOT NULL,
    phone_last4            TEXT CHECK (phone_last4 ~ '^[0-9]{4}$'),
    -- Set when the profile carries a hold that name, date of birth, and email
    -- alone do not clear. Verification of such a customer returns
    -- needs_more_factors even though every supplied factor matched.
    elevated_verification  BOOLEAN NOT NULL DEFAULT FALSE,
    created_at             TEXT NOT NULL
);

CREATE INDEX customers_email ON customers (lower(email));
CREATE INDEX customers_name_dob ON customers (lower(full_name), date_of_birth);

CREATE TABLE payment_methods (
    token        TEXT PRIMARY KEY,
    customer_id  TEXT NOT NULL REFERENCES customers(customer_id),
    brand        TEXT NOT NULL,
    last4        TEXT NOT NULL CHECK (last4 ~ '^[0-9]{4}$'),
    active       BOOLEAN NOT NULL DEFAULT TRUE,
    added_at     TEXT NOT NULL
);

CREATE INDEX payment_methods_customer ON payment_methods (customer_id, token);

-- A certificate is a balance, not a coupon. book_reservation draws it down with
-- an UPDATE, so the same certificate value cannot be spent by two bookings.
CREATE TABLE travel_certificates (
    certificate_id     TEXT PRIMARY KEY,
    code               TEXT NOT NULL UNIQUE,
    customer_id        TEXT NOT NULL REFERENCES customers(customer_id),
    -- Masked form is read back over the phone, so it is stored as the backend
    -- renders it rather than derived from digits a result never discloses.
    masked_code        TEXT NOT NULL,
    status             TEXT NOT NULL
        CHECK (status IN ('valid', 'expired', 'redeemed', 'void')),
    original_amount    NUMERIC(10, 2) NOT NULL CHECK (original_amount >= 0),
    available_balance  NUMERIC(10, 2) NOT NULL CHECK (available_balance >= 0),
    currency           TEXT NOT NULL DEFAULT 'USD' CHECK (currency = 'USD'),
    expires_at         DATE,
    CHECK (available_balance <= original_amount)
);

CREATE INDEX travel_certificates_customer ON travel_certificates (customer_id);

CREATE TABLE identity_verifications (
    verification_id  TEXT PRIMARY KEY,
    customer_id      TEXT REFERENCES customers(customer_id),
    purpose          TEXT NOT NULL DEFAULT 'booking',
    status           TEXT NOT NULL
        CHECK (status IN ('verified', 'failed', 'needs_more_factors')),
    matched_factors  TEXT[] NOT NULL DEFAULT '{}',
    -- Sentinel or timestamp, per the registry: this tier of verification lives
    -- for the duration of the call.
    expires_at       TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    -- Only a cleared verification names a customer.
    CHECK ((status = 'verified') = (customer_id IS NOT NULL))
);

-- ---------------------------------------------------------------------------
-- shopping: searches and quotes
-- ---------------------------------------------------------------------------

-- Availability-check cache, one row per route, date pair, and stop profile. The
-- search identifier and the check timestamp a result reports are properties of
-- the cached check; a route that has never been searched gets a fresh row with
-- an allocated identifier.
CREATE TABLE flight_searches (
    search_id               TEXT PRIMARY KEY,
    origin_code             TEXT NOT NULL REFERENCES airports(code),
    destination_code        TEXT NOT NULL REFERENCES airports(code),
    departure_date          DATE NOT NULL,
    return_date             DATE NOT NULL,
    stop_profile            TEXT NOT NULL
        CHECK (stop_profile IN ('nonstop', 'one_stop')),
    availability_checked_at TEXT NOT NULL,
    -- Timestamp or the registry's documented sentinel for "a quote is required
    -- before the price is held".
    expires_at              TEXT NOT NULL,
    UNIQUE (origin_code, destination_code, departure_date, return_date, stop_profile)
);

-- Quote cache keyed by everything that changes the price. Identity and expiry
-- are the row's own; the amounts are recomputed from fares, tariffs, and rules
-- on every pricing call and written back here so a later booking can be held to
-- the number the customer authorized.
CREATE TABLE fare_quotes (
    quote_id                    TEXT PRIMARY KEY,
    outbound_flight_id          TEXT NOT NULL REFERENCES flights(flight_id),
    return_flight_id            TEXT NOT NULL REFERENCES flights(flight_id),
    departure_date              DATE NOT NULL,
    return_date                 DATE NOT NULL,
    fare_class                  TEXT NOT NULL
        CHECK (fare_class IN ('basic_economy', 'standard_economy')),
    traveler_count              INTEGER NOT NULL CHECK (traveler_count >= 1),
    checked_bag_count           INTEGER NOT NULL CHECK (checked_bag_count >= 0),
    mobility_device_count       INTEGER NOT NULL CHECK (mobility_device_count >= 0),
    include_insurance           BOOLEAN NOT NULL,
    insurance_plan_id           TEXT REFERENCES insurance_plans(plan_id),
    fare_taxes_and_checked_bags NUMERIC(10, 2) CHECK (fare_taxes_and_checked_bags >= 0),
    mobility_device_charge      NUMERIC(10, 2) CHECK (mobility_device_charge >= 0),
    trip_insurance              NUMERIC(10, 2) CHECK (trip_insurance >= 0),
    total_with_insurance        NUMERIC(10, 2) CHECK (total_with_insurance >= 0),
    currency                    TEXT NOT NULL DEFAULT 'USD' CHECK (currency = 'USD'),
    expires_at                  TEXT NOT NULL,
    -- Set when a pricing call last computed this quote. The desk's current
    -- pricing context is the most recently priced quote, which is how a profile
    -- read knows which itinerary to run a duplicate check against.
    last_priced_at              TEXT,
    UNIQUE (outbound_flight_id, return_flight_id, fare_class, traveler_count,
            checked_bag_count, mobility_device_count, include_insurance)
);

-- ---------------------------------------------------------------------------
-- reservations
-- ---------------------------------------------------------------------------

CREATE TABLE reservations (
    reservation_id          TEXT PRIMARY KEY,
    confirmation_code       TEXT NOT NULL UNIQUE
        REFERENCES confirmation_code_pool(code),
    customer_id             TEXT NOT NULL REFERENCES customers(customer_id),
    quote_id                TEXT REFERENCES fare_quotes(quote_id),
    outbound_flight_id      TEXT NOT NULL REFERENCES flights(flight_id),
    return_flight_id        TEXT NOT NULL REFERENCES flights(flight_id),
    departure_date          DATE NOT NULL,
    return_date             DATE NOT NULL,
    contact_email           TEXT NOT NULL,
    fare_class              TEXT NOT NULL
        CHECK (fare_class IN ('basic_economy', 'standard_economy')),
    checked_bag_count       INTEGER NOT NULL CHECK (checked_bag_count >= 0),
    -- The lifecycle from the policy. status and ticketing_status are separate
    -- columns because the policy forbids collapsing Confirmed and Ticketed, and
    -- payment_status is separate again because authorized is not captured.
    status                  TEXT NOT NULL
        CHECK (status IN ('draft', 'quoted', 'pending_payment', 'confirmed', 'ticketed')),
    ticketing_status        TEXT NOT NULL
        CHECK (ticketing_status IN ('pending', 'ticketed', 'failed')),
    payment_status          TEXT
        CHECK (payment_status IN ('authorized', 'captured', 'failed')),
    seat_selection_available BOOLEAN NOT NULL,
    insurance_included      BOOLEAN NOT NULL,
    insurance_plan_id       TEXT REFERENCES insurance_plans(plan_id),
    insurance_price         NUMERIC(10, 2) CHECK (insurance_price >= 0),
    charged_total           NUMERIC(10, 2) NOT NULL CHECK (charged_total >= 0),
    currency                TEXT NOT NULL DEFAULT 'USD' CHECK (currency = 'USD'),
    created_at              TEXT NOT NULL,
    -- A ticket exists only against captured money.
    CHECK (ticketing_status <> 'ticketed'
           OR (status IN ('confirmed', 'ticketed') AND payment_status = 'captured')),
    -- A confirmed reservation has a payment position of some kind.
    CHECK (status NOT IN ('confirmed', 'ticketed') OR payment_status IS NOT NULL),
    -- Insurance that is included has a price and a plan.
    CHECK (NOT insurance_included
           OR (insurance_price IS NOT NULL AND insurance_plan_id IS NOT NULL))
);

CREATE INDEX reservations_customer ON reservations (customer_id, reservation_id);
CREATE INDEX reservations_itinerary
    ON reservations (outbound_flight_id, return_flight_id, departure_date, return_date);

CREATE TABLE travelers (
    traveler_seq   BIGSERIAL PRIMARY KEY,
    reservation_id TEXT NOT NULL REFERENCES reservations(reservation_id),
    traveler_id    TEXT NOT NULL,
    full_name      TEXT NOT NULL,
    date_of_birth  DATE NOT NULL,
    -- Emission order within the reservation, which is the order the caller gave.
    traveler_index INTEGER NOT NULL CHECK (traveler_index >= 1),
    UNIQUE (reservation_id, traveler_id),
    UNIQUE (reservation_id, traveler_index)
);

-- Mobility devices recorded separately from paid baggage, as the policy
-- requires. The tariff in force at booking is copied onto the entry, so a later
-- tariff change does not rewrite what the customer was told.
CREATE TABLE reservation_mobility_devices (
    device_entry_id        TEXT PRIMARY KEY,
    reservation_id         TEXT NOT NULL REFERENCES reservations(reservation_id),
    device_index           INTEGER NOT NULL CHECK (device_index >= 1),
    device_type            TEXT NOT NULL,
    fee                    NUMERIC(10, 2) NOT NULL CHECK (fee >= 0),
    counts_as_paid_bag     BOOLEAN NOT NULL,
    serial_number_required BOOLEAN NOT NULL,
    rule_effective_at      DATE NOT NULL,
    UNIQUE (reservation_id, device_index)
);

-- One row per tender. The split is data, so a booking paid partly by
-- certificate cannot report an allocation that does not add up to what was
-- charged; the handler checks the sum before it commits.
CREATE TABLE payment_allocations (
    allocation_id    TEXT PRIMARY KEY,
    reservation_id   TEXT NOT NULL REFERENCES reservations(reservation_id),
    allocation_index INTEGER NOT NULL CHECK (allocation_index >= 1),
    tender           TEXT NOT NULL,
    tender_kind      TEXT NOT NULL
        CHECK (tender_kind IN ('travel_certificate', 'card')),
    amount           NUMERIC(10, 2) NOT NULL CHECK (amount >= 0),
    currency         TEXT NOT NULL DEFAULT 'USD' CHECK (currency = 'USD'),
    UNIQUE (reservation_id, allocation_index)
);

CREATE INDEX payment_allocations_reservation ON payment_allocations (reservation_id);

-- Certificate drawdowns, one row per application. The balance on
-- travel_certificates is the running figure; this is the ledger behind it.
CREATE TABLE certificate_redemptions (
    redemption_id   TEXT PRIMARY KEY,
    certificate_id  TEXT NOT NULL REFERENCES travel_certificates(certificate_id),
    reservation_id  TEXT NOT NULL REFERENCES reservations(reservation_id),
    amount          NUMERIC(10, 2) NOT NULL CHECK (amount > 0),
    currency        TEXT NOT NULL DEFAULT 'USD' CHECK (currency = 'USD'),
    redeemed_at     TEXT NOT NULL
);

CREATE INDEX certificate_redemptions_certificate
    ON certificate_redemptions (certificate_id);

CREATE TABLE specialist_transfers (
    transfer_id  TEXT PRIMARY KEY,
    reason       TEXT NOT NULL,
    summary      TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('initiated', 'completed', 'failed')),
    created_at   TEXT NOT NULL
);

-- What each reservation's tenders add up to, keyed by reservation. Exists so the
-- verifier can assert that the split matches the charged total without summing
-- rows itself, and so an operator can spot an allocation that drifted.
CREATE VIEW reservation_payment_totals AS
SELECT r.reservation_id,
       r.charged_total,
       coalesce(sum(p.amount), 0)::numeric(10, 2) AS allocated_total,
       count(p.allocation_id)::int                AS tender_count
  FROM reservations r
  LEFT JOIN payment_allocations p ON p.reservation_id = r.reservation_id
 GROUP BY r.reservation_id, r.charged_total;

-- Append-only record of every tool call served, so a run can be audited and so
-- read-only calls leave a trace even though they change nothing else.
CREATE TABLE tool_call_log (
    call_seq    BIGSERIAL PRIMARY KEY,
    tool_name   TEXT NOT NULL,
    arguments   JSONB NOT NULL,
    result      JSONB,
    http_status INTEGER NOT NULL,
    called_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMIT;
