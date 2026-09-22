-- The entities this conversation touches.
--
-- Every value here is either read back verbatim by a recorded tool result or is
-- a field the recorded results never revealed. The former are exact; the latter
-- are plausible, consistent with the call, and marked as such.
--
-- Scenario clock: 2026-08-25T15:40:00-04:00. Ethan Patel's headphones were
-- scanned as delivered to his building yesterday afternoon and are not there.
-- Nothing about the order has been disputed yet: no case, no trace, and no
-- resolution eligibility, because policy establishes eligibility only on a
-- trigger and none has fired.

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

-- The scenario order, plus three of Ethan's own orders whose references end in
-- digits close enough to matter. The caller reads out four digits and the desk
-- resolves them by the longest trailing match, so 7319 must beat 1319 and 9319
-- (three digits) and 7019 (two). A fourth reference on the other Patel account
-- ends in 3319 and is unreachable from this caller's email at all.
INSERT INTO orders
    (order_reference, customer_id, placed_on, fulfillment_status,
     destination_label, replaces_order_reference, representative_item)
VALUES
    -- Placed date and destination label are filler: the recorded results never
    -- disclosed either. The delivered status and the scan below are not.
    ('5820447319', 'c51d2171-c10b-489b-9186-a93ebd98613d', '2026-08-21', 'delivered',
     'home_address_on_order', NULL, NULL),
    ('5820441319', 'c51d2171-c10b-489b-9186-a93ebd98613d', '2026-08-14', 'delivered',
     'home_address_on_order', NULL, 'desk lamp'),
    ('5820449319', 'c51d2171-c10b-489b-9186-a93ebd98613d', '2026-07-30', 'delivered',
     'home_address_on_order', NULL, 'wool socks'),
    ('5820447019', 'c51d2171-c10b-489b-9186-a93ebd98613d', '2026-07-11', 'delivered',
     'home_address_on_order', NULL, 'cutting board'),
    ('5820443319', 'customer-nisha-patel', '2026-08-19', 'delivered',
     'home_address_on_order', NULL, 'yoga mat');

-- The headphones. The recorded read discloses the item reference, the product
-- reference, and the colour, and nothing else: no name, no variant label, and
-- no price were ever returned, so those columns are null rather than invented.
INSERT INTO order_items
    (item_reference, order_reference, line_no, variant_reference,
     product_reference, name, variant_label, color, total_after_tax, currency)
VALUES
    ('blue-noise-canceling-headphones', '5820447319', 1,
     'blue-noise-canceling-headphones', 'blue-noise-canceling-headphones',
     NULL, NULL, 'blue', 214.99, 'USD');

INSERT INTO order_items
    (item_reference, order_reference, line_no, variant_reference,
     product_reference, name, variant_label, color, total_after_tax, currency)
VALUES
    ('item-scenario-desk-lamp', '5820441319', 1, 'desk-lamp-graphite',
     'desk-lamp', 'desk lamp', 'graphite', 'graphite', 48.30, 'USD'),
    ('item-scenario-wool-socks', '5820449319', 1, 'wool-socks-olive',
     'wool-socks', 'wool socks', 'olive', 'olive', 24.75, 'USD'),
    ('item-scenario-cutting-board', '5820447019', 1, 'cutting-board-sand',
     'cutting-board', 'cutting board', 'sand', 'sand', 39.10, 'USD'),
    ('item-nisha-yoga-mat', '5820443319', 1, 'yoga-mat-navy', 'yoga-mat',
     'yoga mat', 'navy', 'navy', 61.00, 'USD');

-- Tenders. The recorded results never disclosed how any of these orders were
-- paid for; the rows exist so an order is a complete record rather than a stub.
INSERT INTO payments
    (order_reference, tender_type, amount, currency, original_card_last4)
VALUES
    ('5820447319', 'credit', 214.99, 'USD', '6142'),
    ('5820441319', 'credit', 48.30, 'USD', '6142'),
    ('5820449319', 'debit', 24.75, 'USD', '3308'),
    ('5820447019', 'credit', 39.10, 'USD', '6142'),
    ('5820443319', 'debit', 61.00, 'USD', '7751');

-- The delivery scan. `location` is what the courier entered and what the
-- customer saw in the shipping email; `evidence_location` is the geofence the
-- scan actually landed in. The pair disagreeing is the whole reason
-- possible_misscan is true, and the recorded call turns on exactly that.
INSERT INTO carrier_scans
    (order_reference, scanned_at, scanned_at_display, location,
     evidence_location, unit_number, locker, photo_reference, possible_misscan)
VALUES
    ('5820447319', '2026-08-24T15:18:00-04:00', '15:18 yesterday',
     'front entrance', 'near building', NULL, NULL, NULL, TRUE);

INSERT INTO carrier_scans
    (order_reference, scanned_at, scanned_at_display, location,
     evidence_location, unit_number, locker, photo_reference, possible_misscan)
VALUES
    ('5820441319', '2026-08-16T11:02:00-04:00', '11:02 on August 16',
     'front desk', 'front desk', '4B', NULL, 'photo-5820441319', FALSE),
    ('5820449319', '2026-08-02T13:44:00-04:00', '13:44 on August 2',
     'package room', 'package room', '4B', 'locker-12', NULL, FALSE),
    ('5820447019', '2026-07-14T16:20:00-04:00', '16:20 on July 14',
     'front desk', 'front desk', '4B', NULL, NULL, FALSE);

-- One of Ethan's earlier orders arrived damaged and its eligibility has already
-- been determined. Nothing in this conversation touches it. It is here so that
-- a replacement path exists on this account that does not run through the
-- disputed order, whose eligibility policy says is not established until a
-- trace trigger fires.
INSERT INTO eligible_resolutions
    (order_reference, resolution_type, position, preserves_original_price,
     return_required, photo_required, optional_photo_upload_available,
     photo_upload_blocks_fulfillment, estimated_delivery_on,
     estimated_delivery_display, default_fulfillment)
VALUES
    ('5820441319', 'replacement', 1, TRUE, FALSE, FALSE, TRUE, FALSE,
     '2026-08-27', 'Thursday end of day', 'home_address_on_order'),
    ('5820441319', 'refund', 2, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL);

-- Progressive section reads on the disputed order.
--
-- The desk's order service serves the carrier-evidence panel only once the scan
-- has been asked for a second time; the first look returns the delivery scan
-- and nothing more. That is why the enacted agent put the caller on hold and
-- went back for the detail, and it is modelled here as a read count rather than
-- as a scripted sequence: the count is a row, and a reset rebuilds it with the
-- rest of the database.
INSERT INTO section_read_cursor (order_reference, section, reads_served)
VALUES ('5820447319', 'carrier_scans', 0);

INSERT INTO section_view (order_reference, section, view_index, payload, note)
VALUES
    ('5820447319', 'carrier_scans', 0, NULL,
     'First look: the evidence panel is not populated yet, so the section is omitted entirely rather than returned empty.');

-- The disclosed payload is not written by hand. It is projected here from the
-- carrier_scans row above, so the evidence a repeat read discloses cannot drift
-- away from the scan it claims to describe. Nulls are kept rather than stripped
-- because the registry types unit_number, locker, and photo as string-or-null:
-- an explicit null is the answer "the carrier recorded none", which is not the
-- same as the field being unavailable.
INSERT INTO section_view (order_reference, section, view_index, payload, note)
SELECT s.order_reference, 'carrier_scans', 1,
       jsonb_build_object(
           'scan_location', s.evidence_location,
           'unit_number', s.unit_number,
           'locker', s.locker,
           'photo', s.photo_reference,
           'possible_misscan', s.possible_misscan),
       'Second and later looks disclose the evidence panel: the geofence the scan landed in, whether a unit or locker was captured, and whether that is consistent with a mis-scan.'
  FROM carrier_scans s
 WHERE s.order_reference = '5820447319';

INSERT INTO scenario (key, value) VALUES
    ('next_support_case_id', '8cf648a4-ca60-4387-bc11-ec38f426123a'),
    ('next_support_case_number', 'WST481662'),
    ('target_case_id', '8cf648a4-ca60-4387-bc11-ec38f426123a'),
    ('target_case_number', 'WST481662'),
    ('next_notification_id', '521527dc-8856-4b33-8e08-69658b1ca80b')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;

COMMIT;
