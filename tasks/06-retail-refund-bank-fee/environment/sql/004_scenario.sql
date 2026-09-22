-- The entities this conversation touches.
--
-- Every value here is either read back verbatim by a recorded tool result or is
-- a field the recorded results never revealed. The former are exact; the latter
-- are plausible, consistent with the call, and marked as such.
--
-- Scenario clock: 2026-08-27T13:05:00-04:00. Teddy Torrez returned a standing
-- desk converter to a store nine days ago. The return completed and the item is
-- back in inventory, the gift-card portion of his money came back, and the card
-- portion left the register and was never confirmed by the processor. Nothing
-- has been disputed yet: there is no case and no notification, because the
-- trace this call opens is the first record of the problem.

BEGIN;

-- The masked email is read aloud, so it is stored exactly as the recorded
-- results render it. The masked phone is filler: no recorded result disclosed
-- one, and it exists so an sms notification has a destination to name.
INSERT INTO customers
    (customer_id, display_name, email, masked_email, masked_phone,
     fulfillment_region, address_label)
VALUES
    ('customer-teddy-torrez', 'Teddy Torrez', 'teddy.torrez@harbormail.com',
     't***@harbormail.com', '***-***-3390', 'new-england',
     'home_address_on_order');

-- A second Torrez on the register. The caller is resolved by the verified email
-- on the order, never by surname, and this row is what makes that a real
-- constraint rather than a stated one.
INSERT INTO customers
    (customer_id, display_name, email, masked_email, masked_phone,
     fulfillment_region, address_label)
VALUES
    ('customer-tamsin-torrez', 'Tamsin Torrez', 'tamsin.torrez@harbormail.com',
     't***@harbormail.com', '***-***-1187', 'ny-metro', 'home_address_on_order');

-- The returned order plus three of Teddy's own orders whose references end in
-- digits close enough to matter. The caller reads out four digits and the desk
-- resolves them by the longest trailing match, so 5624 must beat 1624 and 9624
-- (three digits) and 5024 (two). A fourth reference on the other Torrez account
-- is unreachable from this caller's email at all.
INSERT INTO orders
    (order_reference, customer_id, placed_on, fulfillment_status,
     destination_label, replaces_order_reference, representative_item)
VALUES
    -- Placed date and destination label are filler: the recorded results never
    -- disclosed either. The delivered status is implied by the item having been
    -- received and returned to a store.
    ('5820465624', 'customer-teddy-torrez', '2026-08-05', 'delivered',
     'home_address_on_order', NULL, 'black standing desk converter'),
    ('5820461624', 'customer-teddy-torrez', '2026-07-29', 'delivered',
     'home_address_on_order', NULL, 'desk lamp'),
    ('5820469624', 'customer-teddy-torrez', '2026-07-16', 'delivered',
     'home_address_on_order', NULL, 'wool socks'),
    ('5820465024', 'customer-teddy-torrez', '2026-06-30', 'delivered',
     'home_address_on_order', NULL, 'cutting board'),
    ('5820463182', 'customer-tamsin-torrez', '2026-08-09', 'delivered',
     'home_address_on_order', NULL, 'yoga mat');

-- The returned item. The recorded read discloses the item reference, the name,
-- the money, and the currency; no separate variant label or colour was ever
-- returned for it, so those columns carry what the catalog knows and the name
-- is the exact phrase the result renders.
INSERT INTO order_items
    (item_reference, order_reference, line_no, variant_reference,
     product_reference, name, variant_label, color, total_after_tax, currency)
VALUES
    ('standing-desk-converter-black', '5820465624', 1,
     'standing-desk-converter-black', 'standing-desk-converter',
     'black standing desk converter', NULL, 'black', 186.42, 'USD');

INSERT INTO order_items
    (item_reference, order_reference, line_no, variant_reference,
     product_reference, name, variant_label, color, total_after_tax, currency)
VALUES
    ('item-scenario-desk-lamp', '5820461624', 1, 'desk-lamp-graphite',
     'desk-lamp', 'desk lamp', 'graphite', 'graphite', 48.30, 'USD'),
    ('item-scenario-wool-socks', '5820469624', 1, 'wool-socks-olive',
     'wool-socks', 'wool socks', 'olive', 'olive', 24.75, 'USD'),
    ('item-scenario-cutting-board', '5820465024', 1, 'cutting-board-sand',
     'cutting-board', 'cutting board', 'sand', 'sand', 39.10, 'USD'),
    ('item-tamsin-yoga-mat', '5820463182', 1, 'yoga-mat-navy', 'yoga-mat',
     'yoga mat', 'navy', 'navy', 61.00, 'USD');

-- The split tender. Both rows are read back verbatim, and the card token on the
-- debit row is the reference the trace is opened against.
INSERT INTO payments
    (order_reference, tender_type, amount, currency, original_card_last4)
VALUES
    ('5820465624', 'gift_card', 40.00, 'USD', NULL),
    ('5820465624', 'debit', 146.42, 'USD', '2047');

INSERT INTO payments
    (order_reference, tender_type, amount, currency, original_card_last4)
VALUES
    ('5820461624', 'credit', 48.30, 'USD', '8163'),
    ('5820469624', 'debit', 24.75, 'USD', '2047'),
    ('5820465024', 'credit', 39.10, 'USD', '8163'),
    ('5820463182', 'debit', 61.00, 'USD', '5514');

-- Delivery evidence for the returned order. Nothing in this conversation reads
-- it; it exists so a delivered order is a complete record rather than a status
-- word.
INSERT INTO carrier_scans
    (order_reference, scanned_at, scanned_at_display, location,
     evidence_location, unit_number, locker, photo_reference, possible_misscan)
VALUES
    ('5820465624', '2026-08-08T14:22:00-04:00', '14:22 on August 8',
     'front desk', 'front desk', '2C', NULL, 'photo-5820465624', FALSE),
    ('5820461624', '2026-08-01T10:11:00-04:00', '10:11 on August 1',
     'front desk', 'front desk', '2C', NULL, NULL, FALSE),
    ('5820469624', '2026-07-19T12:37:00-04:00', '12:37 on July 19',
     'package room', 'package room', '2C', 'locker-4', NULL, FALSE);

-- The store return. The reference, the completed status, the store name, and
-- the restocked disposition are read back verbatim. `accepted_on` is typed and
-- the age the result reports is computed from it against the scenario clock
-- rather than stored, so the two can never disagree: 18 August is nine days
-- before 27 August.
INSERT INTO returns
    (return_reference, order_reference, item_reference, return_status,
     accepted_at, accepted_on, inventory_disposition)
VALUES
    ('5820469182', '5820465624', 'standing-desk-converter-black', 'complete',
     'Eastwood store', '2026-08-18', 'restocked');

-- The two refunds the register raised against that return.
--
-- The gift-card portion carries two statuses that are not the same fact: the
-- refund is `issued_available` to the payment processor, and the card it
-- created is `active` on the gift-card ledger with its balance unspent. The
-- recorded results report the first when the agent reviews the refunds and the
-- second when he checks the balance, which is why both are columns.
--
-- The card portion is the whole problem: the register submitted it and the
-- processor never confirmed settlement. `submitted_no_settlement_confirmation`
-- is a lifecycle state the schema allows, so an agent cannot read it as either
-- paid or rejected.
INSERT INTO refunds
    (order_reference, return_reference, tender_type, amount, currency, status,
     ledger_status, available_balance, used, delivery, original_card_last4,
     initiation_source)
VALUES
    -- No initiation source on the gift-card row: the recorded result names one
    -- for the card refund and not for this one, so the column is null rather
    -- than filled in with the register the recording never attributed it to.
    ('5820465624', '5820469182', 'gift_card', 40.00, 'USD', 'issued_available',
     'active', 40.00, FALSE, 'digital_gift_card_number_in_email', NULL, NULL),
    ('5820465624', '5820469182', 'debit', 146.42, 'USD',
     'submitted_no_settlement_confirmation', NULL, NULL, NULL, NULL, '2047',
     'store_register');

-- Progressive section reads on the returned order.
--
-- The order is read three times and the tender and refund panels answer a
-- different question each time: first what the return did, then what the money
-- did, then what the reissued gift card is worth. Nothing between those reads
-- changes a record -- the trace is not opened until afterwards -- so the
-- deepening is a read count rather than a mutation, and it is modelled as one.
INSERT INTO section_read_cursor (order_reference, section, reads_served)
VALUES
    ('5820465624', 'payments', 0),
    ('5820465624', 'refunds', 0);

-- The tender panel withholds card tokens on a first look and discloses them
-- once. A third look gets nothing at all: a card number is disclosed one time
-- per enquiry, and repeating the request is how a token gets harvested rather
-- than read.
INSERT INTO section_view (order_reference, section, view_index, payload, note)
SELECT '5820465624', 'payments', 0,
       jsonb_agg(jsonb_build_object(
           'type', tender_type,
           'amount', money_json(amount),
           'currency', currency) ORDER BY payment_seq),
       'First look: the tenders and what each carried, with the card token withheld.'
  FROM payments WHERE order_reference = '5820465624';

INSERT INTO section_view (order_reference, section, view_index, payload, note)
SELECT '5820465624', 'payments', 1,
       jsonb_agg(jsonb_strip_nulls(jsonb_build_object(
           'type', tender_type,
           'amount', money_json(amount),
           'currency', currency,
           'original_card_last4', original_card_last4)) ORDER BY payment_seq),
       'Second look: the same tenders with the original card token disclosed, which is what a payment trace has to be opened against.'
  FROM payments WHERE order_reference = '5820465624';

INSERT INTO section_view (order_reference, section, view_index, payload, note)
VALUES ('5820465624', 'payments', 2, NULL,
        'Third and later looks: the token has already been disclosed once and the panel is withheld.');

-- The refund panel answers the return question first, the money question
-- second, and the gift-card ledger question third. Every payload is projected
-- from the rows above rather than written out, so what a panel discloses cannot
-- drift away from the record it claims to describe, and the amounts are
-- rendered through money_json so a materialized panel and a directly projected
-- one cannot disagree about whether forty dollars is 40 or 40.0.
INSERT INTO section_view (order_reference, section, view_index, payload, note)
SELECT '5820465624', 'refunds', 0,
       jsonb_agg(jsonb_build_object(
           -- Masked through the same setting the tool layer reads, so a change
           -- of redaction format moves both together.
           'return_reference',
           replace((SELECT value FROM scenario WHERE key = 'order_reference_mask'),
                   '{last4}', right(r.return_reference, 4)),
           'return_status', r.return_status,
           'accepted_at', r.accepted_at,
           'accepted_age_days',
           to_jsonb((SELECT value::timestamptz::date FROM scenario
                      WHERE key = 'scenario_time') - r.accepted_on),
           'inventory_disposition', r.inventory_disposition)
           ORDER BY r.return_reference),
       'First look: what the store did with the item, which is what a customer asking where the money is has to be told before the money is discussed.'
  FROM returns r WHERE r.order_reference = '5820465624';

INSERT INTO section_view (order_reference, section, view_index, payload, note)
SELECT '5820465624', 'refunds', 1,
       jsonb_agg(jsonb_strip_nulls(jsonb_build_object(
           'tender_type', tender_type,
           'amount', money_json(amount),
           'currency', currency,
           'status', status,
           'original_card_last4', original_card_last4,
           'initiation_source', initiation_source)) ORDER BY refund_seq),
       'Second look: where each refund stands with the payment processor.'
  FROM refunds WHERE order_reference = '5820465624';

INSERT INTO section_view (order_reference, section, view_index, payload, note)
SELECT '5820465624', 'refunds', 2,
       jsonb_agg(jsonb_build_object(
           'tender_type', tender_type,
           'amount', money_json(amount),
           'available_balance', money_json(available_balance),
           'currency', currency,
           'status', ledger_status,
           'used', used,
           'delivery', delivery) ORDER BY refund_seq),
       'Third look: the gift-card ledger. Only the tender that has a ledger is on this panel, and its status is the card status rather than the processing status.'
  FROM refunds
 WHERE order_reference = '5820465624' AND ledger_status IS NOT NULL;

INSERT INTO scenario (key, value) VALUES
    ('next_support_case_id', 'b0fee5fa-0534-4c47-a42a-b3a252106888'),
    ('next_support_case_number', 'WST735204'),
    ('target_case_id', 'b0fee5fa-0534-4c47-a42a-b3a252106888'),
    ('target_case_number', 'WST735204'),
    ('next_notification_id', '43e020c5-a9da-4175-aec7-5989435d95a6')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;

COMMIT;
