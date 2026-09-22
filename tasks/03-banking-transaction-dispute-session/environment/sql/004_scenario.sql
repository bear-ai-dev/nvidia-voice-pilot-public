-- The entities this conversation touches.
--
-- Hand-written and loaded after the generated population so a regenerated seed
-- cannot move them. Every value the recorded tool results revealed is marked
-- REVEALED; everything else is plausible filler that the recording never
-- observed and that the results therefore do not depend on.

BEGIN;

-- REVEALED: the name 'Justin Porter' together with jporter92@email.com resolves
-- uniquely to 4e8c3a97-e6ad-4737-aede-c8acd4993277, and the profile requires the billing ZIP
-- and the birth month and day. The supplied ZIP 64114 and birthday
-- 'February 19' both match, so those are the profile's real values.
--
-- The recorded lookup reports no name back. This bank discloses the name it
-- landed on only when the caller resolved on an account identifier and needs it
-- read back to confirm the profile; a caller who supplied the name already has
-- nothing to confirm.
INSERT INTO customers (
    customer_id, account_id, full_name, family_name, verification_key, notice_slug,
    primary_email, notification_email, billing_zip, birth_month, birth_day,
    mobile_last4, caller_channel_match, required_verification_methods,
    login_identifier_kind
) VALUES (
    '4e8c3a97-e6ad-4737-aede-c8acd4993277', 'SF267510', 'Justin Porter', 'Porter',
    'justin-porter', 'justin',
    'jporter92@email.com', 'jporter92@email.com',
    '64114', 2, 19, '4106', NULL,
    ARRAY['billing_zip', 'birth_month_day'], 'email'
);

-- A second Justin Porter. The recorded lookup carries the address as well as the
-- name and resolves uniquely; this row is what makes the address load-bearing
-- rather than decorative.
INSERT INTO customers (
    customer_id, account_id, full_name, family_name, verification_key, notice_slug,
    primary_email, notification_email, billing_zip, birth_month, birth_day,
    mobile_last4, caller_channel_match, required_verification_methods,
    login_identifier_kind
) VALUES (
    '4e8c3a97-e6ad-4737-aede-c8acd4993277-ks', 'SF273844', 'Justin Porter', 'Porter',
    'SF273844', 'justinp',
    'justin.porter.ks@fastmail.example', 'justin.porter.ks@fastmail.example',
    '66206', 7, 8, '2270', NULL,
    ARRAY['billing_zip', 'card_last4'], 'username'
);

-- REVEALED, by omission: the recorded lookup reports no trusted channels. The
-- profile holds a handset that was never enrolled for confirmation challenges,
-- so the enrolment filter excludes it; without this row the empty list would be
-- an empty table rather than a filter result.
INSERT INTO trusted_channels (
    channel_id, customer_id, type, masked_destination, enrolled,
    confirmation_completes, confirmation_verified_at
) VALUES (
    'trusted-mobile-4106', '4e8c3a97-e6ad-4737-aede-c8acd4993277', 'sms', '***-***-4106', FALSE,
    FALSE, NULL
);

INSERT INTO trusted_channels (
    channel_id, customer_id, type, masked_destination, enrolled,
    confirmation_completes, confirmation_verified_at
) VALUES (
    'trusted-mobile-2270', '4e8c3a97-e6ad-4737-aede-c8acd4993277-ks', 'sms', '***-***-2270', TRUE,
    TRUE, '2026-02-25T16:24:00-05:00'
);

-- REVEALED, indirectly: the verification record is named
-- 'dbf560d6-9aa5-4471-a5b1-7a3af5c51fb8', so the open reason this profile is in
-- contact is a dispute, and that reason is a row rather than a string the
-- handler pastes together.
INSERT INTO service_cases (case_id, customer_id, case_kind, case_slug, status, opened_at)
VALUES ('case-justin-porter-dispute', '4e8c3a97-e6ad-4737-aede-c8acd4993277', 'dispute',
        'dispute', 'open', '2026-02-25T16:18:00-05:00');

-- REVEALED: the charge in question sits on the card ending 9102.
--
-- The limit and available credit are filler; nothing in this conversation reads
-- the card account.
INSERT INTO card_accounts (
    card_id, customer_id, card_last4, product_id, status, reported_lost,
    payment_status, credit_limit, available_credit
) VALUES (
    'card-justin-porter-9102', '4e8c3a97-e6ad-4737-aede-c8acd4993277', '9102',
    'everyday-cash-plus', 'active', FALSE, 'current', 7500.00, 5108.42
);

INSERT INTO card_accounts (
    card_id, customer_id, card_last4, product_id, status, reported_lost,
    payment_status, credit_limit, available_credit
) VALUES (
    'card-justin-porter-ks-3391', '4e8c3a97-e6ad-4737-aede-c8acd4993277-ks', '3391',
    'campus-start', 'active', FALSE, 'current', 1500.00, 942.10
);

-- REVEALED: the disputed charge is b2047621-d2c7-437c-bb56-c81a700e2099 for 243.18 USD in the
-- 'online marketplace' category, preceded by a 1.00 authorization, posted on
-- 2026-02-23, and carrying a descriptor that contains 'MRKTPLC*8472'.
--
-- The one-dollar pre-authorization is the detail that makes the call turn: it is
-- the signature of a card-on-file wallet rather than a fresh card entry, which
-- is why the household-use question is worth asking before a dispute is opened.
-- It is a column on the posted charge because that is what it describes, rather
-- than a separate ledger row the statement search would then also return.
--
-- resource_label and short_ref exist because a dispute session is named after
-- this activity and reads its label aloud: the session identifier
-- '0ae36057-bbbb-42e3-994f-55f064bc374c' and the label 'Review transaction ending 8472' are read
-- from these columns rather than assembled from the transaction id by string
-- surgery.
INSERT INTO transactions (
    transaction_id, card_id, kind, merchant_key, merchant, merchant_location,
    descriptor, category, amount, status, settlement_state, occurred_at,
    posted_date, preceded_by_authorization_amount, resource_label, short_ref
) VALUES (
    'b2047621-d2c7-437c-bb56-c81a700e2099', 'card-justin-porter-9102', 'posted', 'marketplace',
    'Meridian Marketplace', NULL, 'MRKTPLC*8472', 'online marketplace',
    243.18, 'posted', 'settled', '2026-02-22T21:14:00-05:00', '2026-02-23',
    1.00, 'transaction ending 8472', '8472'
);

-- Three decoys on the same card, one for each filter the recorded search uses.
-- A search that dropped any one of the amount, the descriptor, or the posting
-- date would return more than the single row the recording shows.
INSERT INTO transactions (
    transaction_id, card_id, kind, merchant_key, merchant, merchant_location,
    descriptor, category, amount, status, settlement_state, occurred_at,
    posted_date, preceded_by_authorization_amount, resource_label, short_ref
) VALUES
    -- Same order, same descriptor, same posting date, different amount: the
    -- marketplace shipped part of the basket separately.
    ('transaction-ending-8473', 'card-justin-porter-9102', 'posted', 'marketplace',
     'Meridian Marketplace', NULL, 'MRKTPLC*8472', 'online marketplace',
     18.99, 'posted', 'settled', '2026-02-22T21:15:00-05:00', '2026-02-23',
     1.00, 'transaction ending 8473', '8473'),
    -- Same amount, same posting date, a different merchant descriptor.
    ('transaction-ending-6620', 'card-justin-porter-9102', 'posted', 'hardware',
     'Fenwick Hardware', 'Kansas City, Missouri', 'FENWICK HDW',
     'home improvement', 243.18, 'posted', 'settled',
     '2026-02-22T13:02:00-05:00', '2026-02-23', NULL,
     'transaction ending 6620', '6620'),
    -- Same amount, same descriptor family, an earlier posting date.
    ('transaction-ending-7735', 'card-justin-porter-9102', 'posted', 'marketplace',
     'Meridian Marketplace', NULL, 'MRKTPLC*7735', 'online marketplace',
     243.18, 'posted', 'settled', '2026-02-18T10:41:00-05:00', '2026-02-19',
     1.00, 'transaction ending 7735', '7735');

INSERT INTO scenario (key, value) VALUES
    ('next_identity_verification_id', 'dbf560d6-9aa5-4471-a5b1-7a3af5c51fb8'),
    ('next_self_service_session_id', '0ae36057-bbbb-42e3-994f-55f064bc374c')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;

COMMIT;
