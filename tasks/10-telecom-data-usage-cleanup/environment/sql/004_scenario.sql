-- The entities this conversation touches.
--
-- Every value here is either read back verbatim by a recorded tool result or is
-- a field the recorded results never revealed. The former are exact; the latter
-- are plausible and consistent with the call, and are marked as such.
--
-- Scenario clock: 2026-08-27T19:30:00-05:00. Benjamin Reed woke to an alert
-- saying he had used 85% of his data while he was asleep. The carrier metered
-- 11.8 GB on his line between midnight and four in the morning, which leaves him
-- 2.2 GB of high-speed data and nine days of cycle to spend it in.
--
-- Nothing here stores 11.8, 2.2, or 7.2. The allowance is on the plan, the
-- consumption is in usage_samples, the purchased increment goes into
-- addon_transactions when the call buys it, and every figure the tools report is
-- a sum over those rows.

BEGIN;

INSERT INTO customers
    (customer_id, slug, full_name, date_of_birth, account_status,
     autopay_enabled, identity_hold)
VALUES
    -- customer_id and the name and date of birth are exact: the lookup matched
    -- on all three and the result echoed the id. autopay_enabled is filler, and
    -- FALSE rather than TRUE because it keeps the autopay-gated offers in the
    -- catalog ineligible for this line and so out of the recorded offer read.
    ('6dcb2039-012b-4723-a256-13bb7b6467c2', 'benjamin-reed', 'Benjamin Reed', '1991-11-22',
     'active', FALSE, NULL);

-- Three cycles on the account. Only the current one was disclosed, by the first
-- bill read: 2026-08-06 to 2026-09-05, which is nine calendar days from the
-- scenario date and is what "your cycle resets in nine days" comes from. The two
-- earlier cycles are filler, and they carry usage of their own so that a
-- remaining balance which forgot to scope its sum to the current cycle would
-- come out visibly wrong.
INSERT INTO billing_cycles
    (billing_cycle_id, customer_id, cycle_start, cycle_end, is_current)
VALUES
    ('cycle-jun-benjamin', '6dcb2039-012b-4723-a256-13bb7b6467c2',
     '2026-06-06T00:00:00-05:00', '2026-07-06T00:00:00-05:00', FALSE),
    ('cycle-jul-benjamin', '6dcb2039-012b-4723-a256-13bb7b6467c2',
     '2026-07-06T00:00:00-05:00', '2026-08-06T00:00:00-05:00', FALSE),
    ('305af2ad-04af-420c-9562-a935b04b855b', '6dcb2039-012b-4723-a256-13bb7b6467c2',
     '2026-08-06T00:00:00-05:00', '2026-09-05T00:00:00-05:00', TRUE);

-- bill_id and its cycle are exact. The current bill is open, which is what makes
-- it the bill a next-bill add-on charge lands on: "charged to your next
-- ClearWave bill" and "7af62370-8858-4af6-b2cd-2b702fd15586" are the same bill, because the
-- cycle in progress has not been invoiced yet. due_at is filler.
INSERT INTO bills
    (bill_id, billing_cycle_id, customer_id, status, currency, issued_at, due_at)
VALUES
    ('bill-jun-benjamin', 'cycle-jun-benjamin', '6dcb2039-012b-4723-a256-13bb7b6467c2',
     'paid', 'USD', '2026-07-06T00:00:00-05:00', '2026-07-21T00:00:00-05:00'),
    ('bill-jul-benjamin', 'cycle-jul-benjamin', '6dcb2039-012b-4723-a256-13bb7b6467c2',
     'paid', 'USD', '2026-08-06T00:00:00-05:00', '2026-08-21T00:00:00-05:00'),
    ('7af62370-8858-4af6-b2cd-2b702fd15586', '305af2ad-04af-420c-9562-a935b04b855b', '6dcb2039-012b-4723-a256-13bb7b6467c2',
     'open', 'USD', NULL, '2026-09-20T00:00:00-05:00');

-- The recorded bill read reported an overage charge of 0.0. It is zero because
-- the bill carries no overage line: Unlimited Start reduces speed past the
-- allowance instead of billing for the excess, so there is nothing to charge.
-- The plan and tax lines are filler; the overage sum is what the tool reports.
INSERT INTO bill_charges
    (charge_id, bill_id, kind, description, amount, currency, billing_timing)
VALUES
    ('charge-7af62370-8858-4af6-b2cd-2b702fd15586-plan', '7af62370-8858-4af6-b2cd-2b702fd15586',
     'recurring_plan', 'Unlimited Start monthly charge', '65.00', 'USD', 'next_bill'),
    ('charge-7af62370-8858-4af6-b2cd-2b702fd15586-tax', '7af62370-8858-4af6-b2cd-2b702fd15586',
     'tax', 'Federal and state surcharges', '5.20', 'USD', 'next_bill'),
    ('charge-bill-jul-benjamin-plan', 'bill-jul-benjamin',
     'recurring_plan', 'Unlimited Start monthly charge', '65.00', 'USD', 'next_bill'),
    ('charge-bill-jul-benjamin-tax', 'bill-jul-benjamin',
     'tax', 'Federal and state surcharges', '5.20', 'USD', 'next_bill'),
    ('charge-bill-jun-benjamin-plan', 'bill-jun-benjamin',
     'recurring_plan', 'Unlimited Start monthly charge', '65.00', 'USD', 'next_bill'),
    ('charge-bill-jun-benjamin-tax', 'bill-jun-benjamin',
     'tax', 'Federal and state surcharges', '5.20', 'USD', 'next_bill');

-- line_id, the mobile number, the active status, and the cycle are exact; the
-- masked form the account read discloses is generated from the number. The line
-- is the only one on the account, which is why the recorded account read
-- returned a single-element array without the handler filtering anything.
-- activated_on is filler.
INSERT INTO lines
    (line_id, customer_id, mobile_number, status, plan_id, billing_cycle_id,
     is_primary, autopay_enabled, metering_source, activated_on, ported_out_at)
VALUES
    ('ec8443dc-5fa9-4579-8dbe-5eafc61d53ac', '6dcb2039-012b-4723-a256-13bb7b6467c2', '404-555-0176', 'active',
     '70102739-54a7-4e7b-b251-5040b1fc2f21', '305af2ad-04af-420c-9562-a935b04b855b', TRUE, FALSE,
     'carrier_metering', '2024-03-15', NULL);

-- device_id, model, line, and provisioning status are exact. The manufacturer
-- and the IMEI suffix are filler; no recorded result carried them.
INSERT INTO devices
    (device_id, line_id, manufacturer, model, provisioning_status, imei_suffix,
     activated_at)
VALUES
    ('17c528ea-9b9d-48cd-8268-c6cca19d817e', 'ec8443dc-5fa9-4579-8dbe-5eafc61d53ac', 'Google', 'Pixel 8', 'active', '4471',
     '2024-03-15T14:20:00-05:00');

-- The overnight burst, one row per metered hour. The recorded read of the last
-- 24 hours reported 11.8 GB between 00:00 and 04:00: the amount is the sum of
-- these four rows and the window is their extent, so both the total and the
-- bounds are aggregates. Splitting the burst into hours also means an agent
-- asking for a narrower custom window gets a smaller, correct answer instead of
-- the same 11.8 GB.
INSERT INTO usage_samples
    (sample_id, line_id, billing_cycle_id, window_start, window_end, gigabytes,
     measurement_source)
VALUES
    ('sample-benjamin-2026-08-27-00', 'ec8443dc-5fa9-4579-8dbe-5eafc61d53ac', '305af2ad-04af-420c-9562-a935b04b855b',
     '2026-08-27T00:00:00-05:00', '2026-08-27T01:00:00-05:00', '2.90', 'carrier_metering'),
    ('sample-benjamin-2026-08-27-01', 'ec8443dc-5fa9-4579-8dbe-5eafc61d53ac', '305af2ad-04af-420c-9562-a935b04b855b',
     '2026-08-27T01:00:00-05:00', '2026-08-27T02:00:00-05:00', '3.40', 'carrier_metering'),
    ('sample-benjamin-2026-08-27-02', 'ec8443dc-5fa9-4579-8dbe-5eafc61d53ac', '305af2ad-04af-420c-9562-a935b04b855b',
     '2026-08-27T02:00:00-05:00', '2026-08-27T03:00:00-05:00', '3.10', 'carrier_metering'),
    ('sample-benjamin-2026-08-27-03', 'ec8443dc-5fa9-4579-8dbe-5eafc61d53ac', '305af2ad-04af-420c-9562-a935b04b855b',
     '2026-08-27T03:00:00-05:00', '2026-08-27T04:00:00-05:00', '2.40', 'carrier_metering');

-- The rest of the cycle: twenty-one days of ordinary daytime use, 1.00 GB in
-- total, from a caller who is on home or office Wi-Fi almost all the time. They
-- are the difference between the burst and the cycle's consumption, and so
-- between 11.8 GB used in the window and 12.8 GB used against the allowance:
-- 15.00 - 12.80 leaves the 2.2 GB the call reports, and 12.80 of 15.00 is the
-- 85% the caller's alert quoted.
--
-- Each window closes at 17:00, so none of them overlaps the last-24-hours read,
-- which begins at 19:30 on the 26th. The 27th carries no daytime sample because
-- the caller spent the day at home on Wi-Fi, which is also why he noticed the
-- alert rather than the usage.
INSERT INTO usage_samples
    (sample_id, line_id, billing_cycle_id, window_start, window_end, gigabytes,
     measurement_source)
VALUES
    ('sample-benjamin-2026-08-06', 'ec8443dc-5fa9-4579-8dbe-5eafc61d53ac', '305af2ad-04af-420c-9562-a935b04b855b',
     '2026-08-06T09:00:00-05:00', '2026-08-06T17:00:00-05:00', '0.04', 'carrier_metering'),
    ('sample-benjamin-2026-08-07', 'ec8443dc-5fa9-4579-8dbe-5eafc61d53ac', '305af2ad-04af-420c-9562-a935b04b855b',
     '2026-08-07T09:00:00-05:00', '2026-08-07T17:00:00-05:00', '0.06', 'carrier_metering'),
    ('sample-benjamin-2026-08-08', 'ec8443dc-5fa9-4579-8dbe-5eafc61d53ac', '305af2ad-04af-420c-9562-a935b04b855b',
     '2026-08-08T09:00:00-05:00', '2026-08-08T17:00:00-05:00', '0.03', 'carrier_metering'),
    ('sample-benjamin-2026-08-09', 'ec8443dc-5fa9-4579-8dbe-5eafc61d53ac', '305af2ad-04af-420c-9562-a935b04b855b',
     '2026-08-09T09:00:00-05:00', '2026-08-09T17:00:00-05:00', '0.05', 'carrier_metering'),
    ('sample-benjamin-2026-08-10', 'ec8443dc-5fa9-4579-8dbe-5eafc61d53ac', '305af2ad-04af-420c-9562-a935b04b855b',
     '2026-08-10T09:00:00-05:00', '2026-08-10T17:00:00-05:00', '0.07', 'carrier_metering'),
    ('sample-benjamin-2026-08-11', 'ec8443dc-5fa9-4579-8dbe-5eafc61d53ac', '305af2ad-04af-420c-9562-a935b04b855b',
     '2026-08-11T09:00:00-05:00', '2026-08-11T17:00:00-05:00', '0.02', 'carrier_metering'),
    ('sample-benjamin-2026-08-12', 'ec8443dc-5fa9-4579-8dbe-5eafc61d53ac', '305af2ad-04af-420c-9562-a935b04b855b',
     '2026-08-12T09:00:00-05:00', '2026-08-12T17:00:00-05:00', '0.04', 'carrier_metering'),
    ('sample-benjamin-2026-08-13', 'ec8443dc-5fa9-4579-8dbe-5eafc61d53ac', '305af2ad-04af-420c-9562-a935b04b855b',
     '2026-08-13T09:00:00-05:00', '2026-08-13T17:00:00-05:00', '0.06', 'carrier_metering'),
    ('sample-benjamin-2026-08-14', 'ec8443dc-5fa9-4579-8dbe-5eafc61d53ac', '305af2ad-04af-420c-9562-a935b04b855b',
     '2026-08-14T09:00:00-05:00', '2026-08-14T17:00:00-05:00', '0.05', 'carrier_metering'),
    ('sample-benjamin-2026-08-15', 'ec8443dc-5fa9-4579-8dbe-5eafc61d53ac', '305af2ad-04af-420c-9562-a935b04b855b',
     '2026-08-15T09:00:00-05:00', '2026-08-15T17:00:00-05:00', '0.03', 'carrier_metering'),
    ('sample-benjamin-2026-08-16', 'ec8443dc-5fa9-4579-8dbe-5eafc61d53ac', '305af2ad-04af-420c-9562-a935b04b855b',
     '2026-08-16T09:00:00-05:00', '2026-08-16T17:00:00-05:00', '0.08', 'carrier_metering'),
    ('sample-benjamin-2026-08-17', 'ec8443dc-5fa9-4579-8dbe-5eafc61d53ac', '305af2ad-04af-420c-9562-a935b04b855b',
     '2026-08-17T09:00:00-05:00', '2026-08-17T17:00:00-05:00', '0.04', 'carrier_metering'),
    ('sample-benjamin-2026-08-18', 'ec8443dc-5fa9-4579-8dbe-5eafc61d53ac', '305af2ad-04af-420c-9562-a935b04b855b',
     '2026-08-18T09:00:00-05:00', '2026-08-18T17:00:00-05:00', '0.05', 'carrier_metering'),
    ('sample-benjamin-2026-08-19', 'ec8443dc-5fa9-4579-8dbe-5eafc61d53ac', '305af2ad-04af-420c-9562-a935b04b855b',
     '2026-08-19T09:00:00-05:00', '2026-08-19T17:00:00-05:00', '0.02', 'carrier_metering'),
    ('sample-benjamin-2026-08-20', 'ec8443dc-5fa9-4579-8dbe-5eafc61d53ac', '305af2ad-04af-420c-9562-a935b04b855b',
     '2026-08-20T09:00:00-05:00', '2026-08-20T17:00:00-05:00', '0.06', 'carrier_metering'),
    ('sample-benjamin-2026-08-21', 'ec8443dc-5fa9-4579-8dbe-5eafc61d53ac', '305af2ad-04af-420c-9562-a935b04b855b',
     '2026-08-21T09:00:00-05:00', '2026-08-21T17:00:00-05:00', '0.07', 'carrier_metering'),
    ('sample-benjamin-2026-08-22', 'ec8443dc-5fa9-4579-8dbe-5eafc61d53ac', '305af2ad-04af-420c-9562-a935b04b855b',
     '2026-08-22T09:00:00-05:00', '2026-08-22T17:00:00-05:00', '0.03', 'carrier_metering'),
    ('sample-benjamin-2026-08-23', 'ec8443dc-5fa9-4579-8dbe-5eafc61d53ac', '305af2ad-04af-420c-9562-a935b04b855b',
     '2026-08-23T09:00:00-05:00', '2026-08-23T17:00:00-05:00', '0.05', 'carrier_metering'),
    ('sample-benjamin-2026-08-24', 'ec8443dc-5fa9-4579-8dbe-5eafc61d53ac', '305af2ad-04af-420c-9562-a935b04b855b',
     '2026-08-24T09:00:00-05:00', '2026-08-24T17:00:00-05:00', '0.04', 'carrier_metering'),
    ('sample-benjamin-2026-08-25', 'ec8443dc-5fa9-4579-8dbe-5eafc61d53ac', '305af2ad-04af-420c-9562-a935b04b855b',
     '2026-08-25T09:00:00-05:00', '2026-08-25T17:00:00-05:00', '0.06', 'carrier_metering'),
    ('sample-benjamin-2026-08-26', 'ec8443dc-5fa9-4579-8dbe-5eafc61d53ac', '305af2ad-04af-420c-9562-a935b04b855b',
     '2026-08-26T09:00:00-05:00', '2026-08-26T17:00:00-05:00', '0.05', 'carrier_metering');

-- Earlier cycles. Filler, and deliberately larger than the current cycle's
-- ordinary use: a balance that summed every sample on the line would report a
-- remaining figure far below 2.2 GB, so these rows are what makes the cycle
-- scoping observable rather than incidental.
INSERT INTO usage_samples
    (sample_id, line_id, billing_cycle_id, window_start, window_end, gigabytes,
     measurement_source)
VALUES
    ('sample-benjamin-2026-07-11', 'ec8443dc-5fa9-4579-8dbe-5eafc61d53ac', 'cycle-jul-benjamin',
     '2026-07-11T08:00:00-05:00', '2026-07-11T18:00:00-05:00', '1.70', 'carrier_metering'),
    ('sample-benjamin-2026-07-23', 'ec8443dc-5fa9-4579-8dbe-5eafc61d53ac', 'cycle-jul-benjamin',
     '2026-07-23T08:00:00-05:00', '2026-07-23T18:00:00-05:00', '2.50', 'carrier_metering'),
    ('sample-benjamin-2026-06-18', 'ec8443dc-5fa9-4579-8dbe-5eafc61d53ac', 'cycle-jun-benjamin',
     '2026-06-18T08:00:00-05:00', '2026-06-18T18:00:00-05:00', '3.10', 'carrier_metering');

-- The offer the call quoted. Every field is exact: 5 GB for $40 on the next
-- bill, usable immediately, expiring twenty-four hours and five minutes after
-- the read that returned it. It is the only unexpired offer on Unlimited Start,
-- so the recorded single-offer result is the whole of what the catalog holds for
-- this plan right now rather than the first row of several.
INSERT INTO addon_offers
    (offer_id, plan_id, data_gigabytes, price, currency, billing_timing,
     effective_timing, expires_at, requires_line_status, requires_autopay,
     withdrawn)
VALUES
    ('21298486-3eca-4c2e-8d07-2b13c0a33fcc', '70102739-54a7-4e7b-b251-5040b1fc2f21', '5.00', '40.00', 'USD',
     'next_bill', 'immediate', '2026-08-28T19:35:00-05:00', 'active', FALSE, FALSE);

-- The allocator the add-on transaction identifier is issued from. The template
-- is the customer stem; the handler appends the offer's size, which is where
-- "dfdab773-2580-4908-91cf-99a2b4826547" comes from, and appends the issued ordinal as
-- well once this line has bought before, so a second purchase cannot reuse the
-- first one's identifier.
INSERT INTO id_allocator (entity_type, scope, next_value, template)
VALUES
    ('addon_transaction', 'ec8443dc-5fa9-4579-8dbe-5eafc61d53ac', 1, 'addon-transaction-benjamin');

-- What this caller said about his handset on an earlier contact. Filler, and
-- present to make the boundary concrete: nothing he reads off his phone during
-- this call is written anywhere, because no tool in the registry records a
-- customer report and the carrier must not appear to have observed one. The
-- StreamBox figure he reads out, the download-over-cellular setting he finds,
-- the Data Saver switch he flips, and the speed test he runs therefore leave no
-- row behind.
INSERT INTO customer_reported_device_state
    (report_id, line_id, reported_at, reported_at_display, report_kind, channel,
     app_name, reported_gigabytes, setting_name, setting_value)
VALUES
    ('report-benjamin-cloudphotos', 'ec8443dc-5fa9-4579-8dbe-5eafc61d53ac',
     '2026-07-14T10:05:00-05:00', 'the July 14 call', 'app_usage_screen',
     'support', 'CloudPhotos', '0.40', NULL, NULL);

INSERT INTO scenario (key, value) VALUES
    ('next_identity_verification_id', 'ae1d7bf3-792a-4a70-8673-16c535063f29'),
    ('next_addon_transaction_id', 'dfdab773-2580-4908-91cf-99a2b4826547')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;

COMMIT;
