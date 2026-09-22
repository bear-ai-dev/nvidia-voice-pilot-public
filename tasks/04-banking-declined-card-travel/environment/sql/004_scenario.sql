-- The entities this conversation touches.
--
-- Hand-written and loaded after the generated population so a regenerated seed
-- cannot move them. Every value the recorded tool results revealed is marked
-- REVEALED; everything else is plausible filler that the recording never
-- observed and that the results therefore do not depend on.

BEGIN;

-- REVEALED: the name 'Colin Reeves' with billing ZIP 20005 and card last four
-- 6148 resolves uniquely; the profile counts the calling channel as a factor and
-- the call did arrive on it (caller_phone_match true); and all three of
-- caller_phone, billing_zip, and card_last4 are required and matched.
--
-- caller_channel_match is TRUE because the caller confirmed he was ringing from
-- the number on the account. It is a column rather than an argument: the channel
-- the call arrived on is not something the caller can assert into a tool, and a
-- profile that counts it and whose call did not arrive on it cannot be verified
-- on that factor at all.
INSERT INTO customers (
    customer_id, account_id, full_name, family_name, verification_key, notice_slug,
    primary_email, notification_email, billing_zip, birth_month, birth_day,
    mobile_last4, caller_channel_match, required_verification_methods,
    login_identifier_kind
) VALUES (
    'c68e3baf-9314-4fd5-8b8f-85e747f0f9d9', 'SF255310', 'Colin Reeves', 'Reeves',
    'colin-reeves', 'colin',
    'colin.reeves@mailhaven.example', 'colin.reeves@mailhaven.example',
    '20005', 6, 24, '7749', TRUE,
    ARRAY['caller_phone', 'billing_zip', 'card_last4'], 'username'
);

-- A second Colin Reeves at the same billing ZIP. The recorded lookup supplies the
-- card's last four as well, which is the only one of the three identifiers that
-- separates these two rows; without this customer the card_last4 argument would
-- be decorative. This profile holds no card ending 6148.
INSERT INTO customers (
    customer_id, account_id, full_name, family_name, verification_key, notice_slug,
    primary_email, notification_email, billing_zip, birth_month, birth_day,
    mobile_last4, caller_channel_match, required_verification_methods,
    login_identifier_kind
) VALUES (
    'c68e3baf-9314-4fd5-8b8f-85e747f0f9d9-dc', 'SF291662', 'Colin Reeves', 'Reeves',
    'SF291662', 'colinr',
    'c.reeves.dc@fastmail.example', 'c.reeves.dc@fastmail.example',
    '20005', 1, 30, '5583', FALSE,
    ARRAY['caller_phone', 'billing_zip', 'card_last4'], 'username'
);

-- REVEALED, by omission: the recorded lookup reports no trusted channels. The
-- profile holds a handset that was never enrolled for confirmation challenges,
-- so the enrolment filter excludes it.
INSERT INTO trusted_channels (
    channel_id, customer_id, type, masked_destination, enrolled,
    confirmation_completes, confirmation_verified_at
) VALUES
    ('trusted-mobile-7749', 'c68e3baf-9314-4fd5-8b8f-85e747f0f9d9', 'sms', '***-***-7749', FALSE,
     FALSE, NULL),
    ('trusted-mobile-5583', 'c68e3baf-9314-4fd5-8b8f-85e747f0f9d9-dc', 'sms', '***-***-5583', TRUE,
     FALSE, NULL);

-- REVEALED, indirectly: the verification record is named
-- '308f6850-19d3-4667-9f16-97bc37d46902', so the open reason this profile is in contact
-- is a card question, and that reason is a row.
INSERT INTO service_cases (case_id, customer_id, case_kind, case_slug, status, opened_at)
VALUES ('case-colin-reeves-card', 'c68e3baf-9314-4fd5-8b8f-85e747f0f9d9', 'card', 'card', 'open',
        '2026-08-28T14:28:00-04:00');

-- REVEALED: the card ends 6148, is temporarily_restricted, is not reported lost,
-- is current on payments, and has 912 USD of available credit.
--
-- The available credit is the number the whole second half of the call turns on:
-- 840 fits inside it and a larger incidental hold would not, and when the review
-- is lifted only one of the two 840 attempts can be re-presented because the
-- second no longer fits. The credit limit and the product are filler.
INSERT INTO card_accounts (
    card_id, customer_id, card_last4, product_id, status, reported_lost,
    payment_status, credit_limit, available_credit
) VALUES (
    'card-colin-6148', 'c68e3baf-9314-4fd5-8b8f-85e747f0f9d9', '6148', 'summit-journey',
    'temporarily_restricted', FALSE, 'current', 5000.00, 912.00
);

INSERT INTO card_accounts (
    card_id, customer_id, card_last4, product_id, status, reported_lost,
    payment_status, credit_limit, available_credit
) VALUES (
    'card-colin-reeves-dc-2914', 'c68e3baf-9314-4fd5-8b8f-85e747f0f9d9-dc', '2914',
    'everyday-cash', 'temporarily_restricted', TRUE, 'current', 3000.00, 2455.00
);

-- REVEALED: two declined attempts at Harbor View Hotel in Portland, Maine, both
-- for 840 USD, the first declined for a travel review and the second because the
-- first review was still open; and one approved 32 USD authorization at Logan
-- Airport at 08:05 with no location recorded against it.
--
-- The two attempts are inserted in the order the recorded reads return them, so
-- the ledger's own record_seq is the order, rather than the reads depending on a
-- sort over amounts or timestamps that happen to agree with it.
--
-- merchant_key is the stem the bank uses when it names a follow-on record after
-- this activity: it is what makes the re-presented authorization
-- 'f96da295-2259-4311-b31d-661ab6053092' rather than a generated string.
--
-- The airport parking charge is the third pending item the caller recognises near
-- the end of the call. It is posted and settled, so it is correctly absent from
-- the recorded authorizations read; it exists because the caller reads it off his
-- own app, which is not a tool result.
INSERT INTO transactions (
    transaction_id, card_id, kind, merchant_key, merchant, merchant_location,
    descriptor, category, amount, status, reason, settlement_state, occurred_at,
    posted_date, resource_label, short_ref
) VALUES
    ('5af2030f-3fad-402f-a1de-23501596194a', 'card-colin-6148', 'decline', 'hotel',
     'Harbor View Hotel', 'Portland, Maine', 'HARBOR VIEW HTL', 'lodging',
     840.00, 'declined', 'travel_review', 'not_applicable',
     '2026-08-28T13:52:00-04:00', NULL, 'attempt at Harbor View Hotel', '0001'),
    ('3065d723-2455-4a43-9d59-91c8dbb11da0', 'card-colin-6148', 'decline', 'hotel',
     'Harbor View Hotel', 'Portland, Maine', 'HARBOR VIEW HTL', 'lodging',
     840.00, 'declined', 'prior_review_open', 'not_applicable',
     '2026-08-28T14:06:00-04:00', NULL, 'attempt at Harbor View Hotel', '0002'),
    ('131fc14b-fc2e-4ec9-ad07-248d01f7d54b', 'card-colin-6148', 'authorization', 'logan',
     'Logan Airport', NULL, 'LOGAN AIRPORT F&B', 'travel',
     32.00, 'approved', NULL, 'pending',
     '2026-08-28T08:05:00-04:00', NULL, 'transaction ending 0003', '0003'),
    ('airside-parking-46', 'card-colin-6148', 'posted', 'parking',
     'Airside Parking', 'Boston, Massachusetts', 'AIRSIDE PKG', 'parking',
     46.00, 'posted', NULL, 'settled',
     '2026-08-28T06:40:00-04:00', '2026-08-28', 'transaction ending 0004', '0004');

-- REVEALED: one open restriction, 'a4e9ecb1-a664-45d0-b3f4-cfe80b54ea8f', linked to the two
-- hotel attempts and the Logan authorization in that order.
--
-- customer_resolvable is TRUE because this is a travel review, which is lifted by
-- the customer confirming the activity that opened it. A delinquency hold or a
-- lost-card block is not, and the tool refuses those rather than reporting a
-- removal the agent would read back.
INSERT INTO card_restrictions (
    restriction_id, card_id, kind, status, customer_resolvable, opened_at, resolved_at
) VALUES (
    'a4e9ecb1-a664-45d0-b3f4-cfe80b54ea8f', 'card-colin-6148', 'travel_review', 'open', TRUE,
    '2026-08-28T13:52:00-04:00', NULL
);

INSERT INTO restriction_transactions (restriction_id, transaction_id, link_rank)
VALUES
    ('a4e9ecb1-a664-45d0-b3f4-cfe80b54ea8f', '5af2030f-3fad-402f-a1de-23501596194a', 0),
    ('a4e9ecb1-a664-45d0-b3f4-cfe80b54ea8f', '3065d723-2455-4a43-9d59-91c8dbb11da0', 1),
    ('a4e9ecb1-a664-45d0-b3f4-cfe80b54ea8f', '131fc14b-fc2e-4ec9-ad07-248d01f7d54b', 2);

-- The other Colin Reeves reported his card lost, which is a block that confirming
-- activity does not lift. It is here so an attempt to resolve a restriction of
-- the wrong kind has a real row to be refused against.
INSERT INTO card_restrictions (
    restriction_id, card_id, kind, status, customer_resolvable, opened_at, resolved_at
) VALUES (
    'restriction-colin-reeves-dc-lost-card', 'card-colin-reeves-dc-2914',
    'lost_card_block', 'open', FALSE, '2026-08-22T09:15:00-04:00', NULL
);

INSERT INTO scenario (key, value) VALUES
    ('next_identity_verification_id', '308f6850-19d3-4667-9f16-97bc37d46902'),
    ('next_represented_transaction_id', 'f96da295-2259-4311-b31d-661ab6053092'),
    ('next_travel_notice_id', '31d46fb6-e721-4a9c-b54d-6c631dbe9686')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;

COMMIT;
