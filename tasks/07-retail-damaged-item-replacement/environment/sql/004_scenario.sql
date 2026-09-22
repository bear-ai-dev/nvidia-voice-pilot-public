-- The entities this conversation touches.
--
-- Every value here is either read back verbatim by a recorded tool result or is
-- a field the recorded results never revealed. The former are exact; the latter
-- are plausible, consistent with the call, and marked as such.
--
-- Scenario clock: 2026-08-26T11:20:00-04:00. This is the morning after the
-- missing-package call. Ethan Patel's coffee maker arrived with a cracked water
-- tank; separately, yesterday's delivery trace on his headphones is still open
-- and its carrier deadline falls at 18:00 this evening.
--
-- The headphones order, its scan, its trace, and the pickup preference recorded
-- on that trace are seeded here as pre-existing rows because two recorded
-- results in this conversation read them back. Per docs/DATA_QUALITY.md the two
-- retail conversations are consecutive days on one account, so this task's
-- starting state is the other task's ending state, restated as data rather than
-- as strings in a handler.

BEGIN;

-- The masked email is read aloud, so it is stored exactly as the recorded
-- results render it. The masked phone is filler: no recorded result disclosed
-- one, and it exists so an sms notification has a destination to name.
INSERT INTO customers
    (customer_id, display_name, email, masked_email, masked_phone,
     fulfillment_region, address_label)
VALUES
    ('c51d2171-c10b-489b-9186-a93ebd98613d', 'Ethan Patel', 'ethan.patel@northmail.com',
     'e***@northmail.com', '***-***-4471', 'nj-metro', 'home_address_on_order');

-- A second Patel on the register. The caller is resolved by the verified email
-- on the order, never by surname, and this row is what makes that a real
-- constraint rather than a stated one.
INSERT INTO customers
    (customer_id, display_name, email, masked_email, masked_phone,
     fulfillment_region, address_label)
VALUES
    ('customer-nisha-patel', 'Nisha Patel', 'nisha.patel@northmail.com',
     'n***@northmail.com', '***-***-9024', 'ny-metro', 'home_address_on_order');

-- The coffee-maker order, yesterday's headphones order, and three near misses.
-- The caller reads out four digits for the coffee maker and the desk resolves
-- them by the longest trailing match, so 4086 must beat 1086 and 9086 (three
-- digits) and 4886 (two). The headphones order is reached the same way later in
-- the call, against 1319, 9319 and 7019 on this same account.
INSERT INTO orders
    (order_reference, customer_id, placed_on, fulfillment_status,
     destination_label, replaces_order_reference, representative_item)
VALUES
    -- Placed date and destination label are filler: the recorded results never
    -- disclosed either for this order. The delivered status is implied by the
    -- customer having opened the box.
    ('5820454086', 'c51d2171-c10b-489b-9186-a93ebd98613d', '2026-08-22', 'delivered',
     'home_address_on_order', NULL, '12-cup coffee maker'),
    -- Carried over from the missing-package call. The representative item is
    -- the exact phrase the account summary reads back.
    ('5820447319', 'c51d2171-c10b-489b-9186-a93ebd98613d', '2026-08-21', 'delivered',
     'home_address_on_order', NULL, 'blue noise-canceling headphones'),
    ('5820451086', 'c51d2171-c10b-489b-9186-a93ebd98613d', '2026-08-10', 'delivered',
     'home_address_on_order', NULL, 'desk lamp'),
    ('5820459086', 'c51d2171-c10b-489b-9186-a93ebd98613d', '2026-07-28', 'delivered',
     'home_address_on_order', NULL, 'wool socks'),
    ('5820454886', 'customer-nisha-patel', '2026-08-18', 'delivered',
     'home_address_on_order', NULL, 'yoga mat');

-- The coffee maker. The recorded read discloses the item reference, the name,
-- and the variant label; no colour field and no price were ever returned for
-- it, so `color` is null and the price is filler consistent with a replacement
-- that carries the original price and therefore leaves nothing due.
INSERT INTO order_items
    (item_reference, order_reference, line_no, variant_reference,
     product_reference, name, variant_label, color, total_after_tax, currency)
VALUES
    ('coffee-maker-matte-black-12-cup', '5820454086', 1,
     'coffee-maker-matte-black-12-cup', '12-cup-coffee-maker',
     '12-cup coffee maker', 'matte black', NULL, 118.40, 'USD');

-- The headphones line, restated from the missing-package task so the trace
-- below hangs off a real item rather than a dangling reference.
INSERT INTO order_items
    (item_reference, order_reference, line_no, variant_reference,
     product_reference, name, variant_label, color, total_after_tax, currency)
VALUES
    ('blue-noise-canceling-headphones', '5820447319', 1,
     'blue-noise-canceling-headphones', 'blue-noise-canceling-headphones',
     NULL, NULL, 'blue', 214.99, 'USD'),
    ('item-scenario-desk-lamp', '5820451086', 1, 'desk-lamp-graphite',
     'desk-lamp', 'desk lamp', 'graphite', 'graphite', 48.30, 'USD'),
    ('item-scenario-wool-socks', '5820459086', 1, 'wool-socks-olive',
     'wool-socks', 'wool socks', 'olive', 'olive', 24.75, 'USD'),
    ('item-nisha-yoga-mat', '5820454886', 1, 'yoga-mat-navy', 'yoga-mat',
     'yoga mat', 'navy', 'navy', 61.00, 'USD');

-- Tenders. The recorded results never disclosed how any of these orders were
-- paid for; the rows exist so an order is a complete record rather than a stub.
INSERT INTO payments
    (order_reference, tender_type, amount, currency, original_card_last4)
VALUES
    ('5820454086', 'credit', 118.40, 'USD', '6142'),
    ('5820447319', 'credit', 214.99, 'USD', '6142'),
    ('5820451086', 'credit', 48.30, 'USD', '6142'),
    ('5820459086', 'debit', 24.75, 'USD', '3308'),
    ('5820454886', 'debit', 61.00, 'USD', '7751');

-- Carrier evidence. The headphones scan is the row the missing-package task
-- turns on: `location` is what the courier entered, `evidence_location` is the
-- geofence the scan landed in, and the pair disagreeing is why the trace exists.
-- Nothing in this conversation reads it, but the trace below would be a claim
-- without evidence if it were dropped.
INSERT INTO carrier_scans
    (order_reference, scanned_at, scanned_at_display, location,
     evidence_location, unit_number, locker, photo_reference, possible_misscan)
VALUES
    ('5820447319', '2026-08-24T15:18:00-04:00', '15:18 on Monday',
     'front entrance', 'near building', NULL, NULL, NULL, TRUE),
    ('5820454086', '2026-08-24T09:41:00-04:00', '09:41 on August 24',
     'front desk', 'front desk', '4B', NULL, 'photo-5820454086', FALSE),
    ('5820451086', '2026-08-12T11:02:00-04:00', '11:02 on August 12',
     'front desk', 'front desk', '4B', NULL, NULL, FALSE),
    ('5820459086', '2026-07-31T13:44:00-04:00', '13:44 on July 31',
     'package room', 'package room', '4B', 'locker-12', NULL, FALSE);

-- Yesterday's delivery trace, carried over. The case id, its type, its open
-- status, the absent carrier response, and the deadline string are all read
-- back verbatim by a recorded result in this conversation. The item description
-- is the phrase the account panel repeats. `carrier_may_contact_customer` and
-- the eligibility triggers are restated from the policy row rather than
-- invented, so the case is internally consistent with a trace opened under the
-- same policy the day before.
INSERT INTO cases
    (case_number, order_reference, customer_id, case_type, status, reason,
     item_description, carrier_response, deadline_at, deadline_display,
     carrier_may_contact_customer, replacement_created, requested_resolution,
     needed_by, approval_required, approval_channel, next_action,
     eligibility_triggers, fee_reimbursement_approved, pickup_guaranteed,
     opened_at)
VALUES
    ('8cf648a4-ca60-4387-bc11-ec38f426123a', '5820447319', 'c51d2171-c10b-489b-9186-a93ebd98613d', 'delivery_trace',
     'open', 'delivered_not_received', 'blue noise-canceling headphones',
     'none', '2026-08-26T18:00:00-04:00', '18:00 today',
     TRUE, FALSE, 'replacement', '2026-08-27', TRUE, 'trace_notification',
     'review requested resolution and fulfillment after an eligibility trigger',
     ARRAY['carrier_confirms_missing', 'carrier_response_deadline_expires'],
     FALSE, FALSE, '2026-08-25T15:52:00-04:00');

INSERT INTO case_items (case_number, item_reference)
VALUES ('8cf648a4-ca60-4387-bc11-ec38f426123a', 'blue-noise-canceling-headphones');

-- The note the agent left on yesterday's call. Not read back here; it is the
-- reason the pickup preference below exists and is kept so the case reads as a
-- case rather than as a header.
INSERT INTO case_notes (case_number, note_no, note, topic, visible_to_next_reviewer,
                        created_at)
VALUES
    ('8cf648a4-ca60-4387-bc11-ec38f426123a', 1,
     'Preserve exact blue variant and original price if replacement becomes eligible.',
     NULL, TRUE, '2026-08-25T15:52:00-04:00');

-- The pickup preference recorded on yesterday's trace. The location string is
-- read back verbatim by this conversation's first recorded result, where it
-- appears under the headphones case and pointedly not under the coffee maker.
INSERT INTO case_preferences
    (case_number, pickup_location, pickup_site, review_instruction,
     visible_to_next_reviewer, recorded_at)
VALUES
    ('8cf648a4-ca60-4387-bc11-ec38f426123a', 'West 23rd Street pickup counter', 'West 23rd Street',
     'Check West 23rd Street pickup availability first after replacement eligibility.',
     TRUE, '2026-08-25T15:52:00-04:00');

-- The confirmation email yesterday's call sent. Already delivered: it is a day
-- old and the customer told that agent he had received it. It is here so the
-- notification the coffee-maker replacement raises is not the only message on
-- the account, which is what makes the notifications read on the replacement
-- order a scoped query rather than a table scan.
INSERT INTO notifications
    (notification_id, case_number, order_reference, channel, template, message_type,
     masked_destination, status, status_index, status_progression,
     subject_prefix, optional_photo_link, photo_link_section, included_fields,
     sent_at, sent_at_display, created_at)
VALUES
    ('notification-8cf648a4-ca60-4387-bc11-ec38f426123a', '8cf648a4-ca60-4387-bc11-ec38f426123a', '5820447319', 'email',
     'delivery_trace_confirmation', 'delivery_trace_confirmation',
     'e***@northmail.com', 'delivered', 1, ARRAY['sent', 'delivered'],
     'Your Westline delivery trace', NULL, NULL,
     ARRAY['case_number', 'status', 'carrier_response_deadline', 'approval_link'],
     '2026-08-25T15:53:00-04:00', NULL, '2026-08-25T15:53:00-04:00');

-- The resolutions the damage claim has already unlocked on the coffee maker.
-- Every field here is read back verbatim by the second recorded result, and the
-- replacement row is also what the replacement handler reads to decide that the
-- original price carries over, that no return is required, and what estimate to
-- quote. Storing it once and reading it from both places is what keeps the
-- quoted estimate and the created order from disagreeing.
INSERT INTO eligible_resolutions
    (order_reference, resolution_type, position, preserves_original_price,
     return_required, photo_required, optional_photo_upload_available,
     photo_upload_blocks_fulfillment, estimated_delivery_on,
     estimated_delivery_display, default_fulfillment)
VALUES
    ('5820454086', 'replacement', 1, TRUE, FALSE, FALSE, TRUE, FALSE,
     '2026-08-27', 'Thursday end of day', 'home_address_on_order'),
    -- The refund option is offered and nothing about it is disclosed. A row of
    -- nulls is the honest representation of that: the option exists, its terms
    -- were never returned, and inventing them would be inventing policy.
    ('5820454086', 'refund', 2, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL);

-- Progressive section reads on the coffee-maker order.
--
-- The resolutions panel is priced by the returns service and is not populated
-- on the first look at an order; the agent opened the record, asked the
-- customer what was wrong, and only then went back for the options. Nothing
-- between those two reads changed any record, so the difference is a repeat
-- read deepening rather than a mutation, and it is modelled as a read count.
INSERT INTO section_read_cursor (order_reference, section, reads_served)
VALUES ('5820454086', 'eligible_resolutions', 0);

INSERT INTO section_view (order_reference, section, view_index, payload, note)
VALUES
    ('5820454086', 'eligible_resolutions', 0, NULL,
     'First look: the returns service has not priced the options yet, so the section is omitted entirely rather than returned empty.');

-- The disclosed payload is not written by hand. It is projected here from the
-- eligible_resolutions rows above, so what the panel discloses cannot drift away
-- from what the replacement handler will act on. Keys absent from a row are
-- dropped rather than emitted as null, which is how the refund option discloses
-- only that it exists.
INSERT INTO section_view (order_reference, section, view_index, payload, note)
SELECT '5820454086', 'eligible_resolutions', 1,
       jsonb_agg(entry ORDER BY position),
       'Second and later looks disclose the priced panel: what each resolution preserves, what it requires, and what it estimates.'
  FROM (
        SELECT position,
               jsonb_strip_nulls(jsonb_build_object(
                   'type', resolution_type,
                   'preserves_original_price', preserves_original_price,
                   'return_required', return_required,
                   'photo_required', photo_required,
                   'optional_photo_upload_available', optional_photo_upload_available,
                   'photo_upload_blocks_fulfillment', photo_upload_blocks_fulfillment,
                   'estimated_delivery', estimated_delivery_display,
                   'default_fulfillment', default_fulfillment)) AS entry
          FROM eligible_resolutions
         WHERE order_reference = '5820454086'
       ) AS priced;

INSERT INTO scenario (key, value) VALUES
    ('target_case_id', '8cf648a4-ca60-4387-bc11-ec38f426123a'),
    ('target_case_number', 'WST481662')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;

COMMIT;
