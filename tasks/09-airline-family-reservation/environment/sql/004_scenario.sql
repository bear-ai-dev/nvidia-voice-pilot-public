-- The entities this conversation touches.
--
-- Every value here is either read back verbatim by a recorded tool result or is
-- a field the recorded results never revealed. The former are exact; the latter
-- are plausible and consistent with the call, and are marked as such.
--
-- The catalog side of the conversation -- the four airports, the National Mall
-- destination area, the two nonstop flights and their fare components, the
-- connecting comparison, the accessibility tariff, and the insurance bands --
-- is declared explicitly at the top of environment/gen_seed.py and lands in
-- 002_reference.sql, because those rows are catalog entries that happen to be
-- read during the call rather than entities belonging to it.
--
-- Scenario clock: 2026-08-26T12:30:00-07:00. Linda Carver is booking two
-- nonstop seats from Phoenix to Washington for herself and her grandson, with a
-- folding walker and a 200-dollar travel certificate.

BEGIN;

-- Date of birth and email are read back by the verification result. The phone's
-- last four digits and the account creation date are never returned by any
-- recorded result; they are consistent filler so the profile is a complete
-- record rather than a stub. elevated_verification is FALSE because the recorded
-- verification cleared on three factors.
INSERT INTO customers
    (customer_id, slug, full_name, date_of_birth, email, phone_last4,
     elevated_verification, created_at)
VALUES
    ('7fe38f6f-b7c5-4f50-a56a-e2d68d6b11d0', 'linda-carver', 'Linda Marie Carver', '1954-03-08',
     'linda.carver9@outlook.com', '7715', FALSE, '2019-04-22T10:15:00-07:00');

-- The card the caller says is already on the account. The token and the last
-- four digits are both read back, in the profile result and again in the
-- booking's tender allocation.
INSERT INTO payment_methods
    (token, customer_id, brand, last4, active, added_at)
VALUES
    ('c1200f87-9c48-41f4-a6f9-04d89f28b2b2', '7fe38f6f-b7c5-4f50-a56a-e2d68d6b11d0', 'Visa', '1182', TRUE,
     '2023-11-08T16:40:00-07:00');

-- The certificate the caller reads out, plus two more on the same account that
-- the call never reaches. The masked form is read back over the phone, so it is
-- stored as the backend renders it. The two extras are why the profile reports
-- that certificate input is required and are what an off-path validation lands
-- on: one is valid but too small to cover this itinerary, the other has value
-- left on it and has expired.
INSERT INTO travel_certificates
    (certificate_id, code, customer_id, masked_code, status, original_amount,
     available_balance, currency, expires_at)
VALUES
    ('18e422e6-a2c3-4cb8-93fa-daf7a28c328b', 'CT-449108', '7fe38f6f-b7c5-4f50-a56a-e2d68d6b11d0', 'CT-***108',
     'valid', '200.00', '200.00', 'USD', '2026-12-31'),
    ('certificate-CT-118240', 'CT-118240', '7fe38f6f-b7c5-4f50-a56a-e2d68d6b11d0', 'CT-***240',
     'valid', '75.00', '45.00', 'USD', '2027-05-31'),
    ('certificate-CT-990031', 'CT-990031', '7fe38f6f-b7c5-4f50-a56a-e2d68d6b11d0', 'CT-***031',
     'expired', '120.00', '120.00', 'USD', '2026-04-30');

-- The two availability checks the call runs, one per stop profile. The search
-- identifiers and the check timestamps are read back by the search results; both
-- are properties of the cached check, which is how a fixed-clock environment
-- reports when availability was last looked at without inventing a monotonic
-- now. A route and date pair that has never been searched gets a fresh row and
-- an allocated identifier instead.
INSERT INTO flight_searches
    (search_id, origin_code, destination_code, departure_date, return_date,
     stop_profile, availability_checked_at, expires_at)
VALUES
    ('96a87cf6-ba8d-4705-995b-8805e877ab0b', 'PHX', 'DCA', '2026-10-14', '2026-10-19',
     'nonstop', '2026-08-26T12:31:33-07:00', 'quote_required_before_booking'),
    ('b1e28687-a8fe-44c9-a18c-66b5dcc0dedb', 'PHX', 'DCA', '2026-10-14', '2026-10-19',
     'one_stop', '2026-08-26T12:32:17-07:00', 'quote_required_before_booking');

-- The quote the pricing call returns. The row carries identity and expiry only:
-- the recording reveals the quote identifier and that the price is held until
-- 12:33 the next day, and neither can be derived from a fixed clock. The four
-- amount columns are deliberately NULL here -- they are computed from fares,
-- taxes, the bag tariff, the accessibility tariff, and the insurance band on
-- every pricing call and written back, so a booking is checked against a number
-- the fare rows can still reproduce rather than against a number typed in here.
INSERT INTO fare_quotes
    (quote_id, outbound_flight_id, return_flight_id, departure_date, return_date,
     fare_class, traveler_count, checked_bag_count, mobility_device_count,
     include_insurance, insurance_plan_id, fare_taxes_and_checked_bags,
     mobility_device_charge, trip_insurance, total_with_insurance, currency,
     expires_at, last_priced_at)
VALUES
    ('b28a0bbf-614c-4616-862c-fbeef88f6495', '2133fbc8-ed10-42aa-baa2-12e3d15a6a05', '3ac31d55-0dbc-4f79-892e-743257ec9f13',
     '2026-10-14', '2026-10-19', 'standard_economy', 2, 2, 1, TRUE,
     NULL, NULL, NULL, NULL, NULL, 'USD', '2026-08-27T12:33:00-07:00', NULL);

-- The record locator the booking issues. Sequence 381 is the first unissued
-- entry in the pool: 1 through 380 are spent by the existing estate in
-- 003_population.sql and 382 onward are spares, so the first booking of a run
-- allocates B9RT6M and a second booking cannot allocate it again.
INSERT INTO confirmation_code_pool (pool_seq, code, issued_at)
VALUES (381, 'B9RT6M', NULL);

INSERT INTO scenario (key, value) VALUES
    ('next_identity_verification_id', '661f8b5c-f3d8-4612-8645-00f9a5ecded8'),
    ('next_reservation_id', 'e27bce0a-c8ac-4102-9a62-e6d3f9bfdaa7'),
    ('next_traveler_1_id', '1a7ef9d5-196f-456c-a780-1acafefd1fd1'),
    ('next_traveler_2_id', 'b0b29fec-eb5c-469e-bee0-046f8c861822')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;

COMMIT;
