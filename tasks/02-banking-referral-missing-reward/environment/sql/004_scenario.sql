-- The entities this conversation touches.
--
-- Hand-written and loaded after the generated population so a regenerated seed
-- cannot move them. Every value the recorded tool results revealed is marked
-- REVEALED; everything else is plausible filler that the recording never
-- observed and that the results therefore do not depend on.

BEGIN;

-- REVEALED: the address daniel.brooks17@gmail.com resolves uniquely to
-- 34d61a18-ede8-4533-b130-7e5937bc1e9d, and the profile requires the billing ZIP and the
-- birth month and day. The supplied ZIP 27609 and birthday 'October 12' both
-- match, so those are the profile's real values.
--
-- The recorded lookup reports no name. This bank discloses the name it landed on
-- only when the caller resolved on an account identifier and needs the name read
-- back to confirm the profile; a caller who resolved on their own address has
-- nothing to confirm. The name is here because later work needs it, not because
-- the recording revealed it.
--
-- login_identifier_kind is 'email' as filler: this conversation never changes
-- the address, so nothing observes it.
INSERT INTO customers (
    customer_id, account_id, full_name, family_name, verification_key, notice_slug,
    primary_email, notification_email, billing_zip, birth_month, birth_day,
    mobile_last4, caller_channel_match, required_verification_methods,
    login_identifier_kind
) VALUES (
    '34d61a18-ede8-4533-b130-7e5937bc1e9d', 'SF241903', 'Daniel Brooks', 'Brooks',
    'daniel-brooks', 'daniel',
    'daniel.brooks17@gmail.com', 'daniel.brooks17@gmail.com',
    '27609', 10, 12, '3318', NULL,
    ARRAY['billing_zip', 'birth_month_day'], 'email'
);

-- A different Daniel Brooks at an address one token away from the target's. The
-- recorded lookup carries the full address and still resolves uniquely; this row
-- is what makes that a real filter rather than a formality.
INSERT INTO customers (
    customer_id, account_id, full_name, family_name, verification_key, notice_slug,
    primary_email, notification_email, billing_zip, birth_month, birth_day,
    mobile_last4, caller_channel_match, required_verification_methods,
    login_identifier_kind
) VALUES (
    '34d61a18-ede8-4533-b130-7e5937bc1e9d-nc', 'SF288140', 'Daniel Brooks', 'Brooks',
    'SF288140', 'danielb',
    'daniel.brooks@gmail.com', 'daniel.brooks@gmail.com',
    '28202', 3, 29, '9052', NULL,
    ARRAY['billing_zip', 'card_last4'], 'username'
);

-- REVEALED, by omission: the recorded lookup reports no trusted channels at all.
-- The profile does hold a handset, but it was never enrolled for confirmation
-- challenges, so the enrolment filter excludes it. Without this row the empty
-- list would be an empty table rather than a filter result.
INSERT INTO trusted_channels (
    channel_id, customer_id, type, masked_destination, enrolled,
    confirmation_completes, confirmation_verified_at
) VALUES (
    'trusted-mobile-3318', '34d61a18-ede8-4533-b130-7e5937bc1e9d', 'sms', '***-***-3318', FALSE,
    FALSE, NULL
);

INSERT INTO trusted_channels (
    channel_id, customer_id, type, masked_destination, enrolled,
    confirmation_completes, confirmation_verified_at
) VALUES (
    'trusted-mobile-9052', '34d61a18-ede8-4533-b130-7e5937bc1e9d-nc', 'sms', '***-***-9052', TRUE,
    TRUE, '2026-08-28T09:12:00-04:00'
);

-- REVEALED, indirectly: the verification record is named
-- '05f09e63-bd3c-4a75-813f-5429ac6d685c', so the open reason this profile is in
-- contact is a referral question, and that reason is a row rather than a string
-- the handler pastes together.
INSERT INTO service_cases (case_id, customer_id, case_kind, case_slug, status, opened_at)
VALUES ('case-daniel-brooks-referral', '34d61a18-ede8-4533-b130-7e5937bc1e9d', 'referral',
        'referral', 'open', '2026-08-28T09:08:03-04:00');

-- REVEALED: referral ce7d58b0-b5fc-42cb-a6e0-7a57929e1205 was invited on 'August 2' by email to a destination
-- masked 'a-brooks…', the application is approved, qualification is still
-- purchase_pending, and the offer is a $100 statement credit.
--
-- invited_at_display is emitted verbatim because that is the string the customer
-- hears; invited_on carries the same fact as a date so the record stays
-- queryable. See docs/SQL_ENVS.md on human-relative strings.
--
-- deadline_on is filler. The recorded knowledge answer says the deadline has not
-- passed and that the exact date lives in the tracker rather than being read
-- aloud, so no tool result depends on this value; it exists so the record is not
-- missing a fact the bank would hold.
INSERT INTO referrals (
    referral_id, referring_customer_id, invited_at_display, invited_on,
    invited_channel, invited_masked, application_status, qualification_status,
    offer, offer_version_record_id, deadline_on, display_rank
) VALUES (
    'ce7d58b0-b5fc-42cb-a6e0-7a57929e1205', '34d61a18-ede8-4533-b130-7e5937bc1e9d', 'August 2', '2026-08-02',
    'email', 'a-brooks…', 'approved', 'purchase_pending',
    '$100 statement credit', 'dab2da6a-4343-4ea3-9b0d-df98a0c96360', '2026-10-31', 100
);

-- A referral belonging to the other Daniel Brooks. The recorded read returns one
-- row, so a read that leaked across profiles would be visible.
INSERT INTO referrals (
    referral_id, referring_customer_id, invited_at_display, invited_on,
    invited_channel, invited_masked, application_status, qualification_status,
    offer, offer_version_record_id, deadline_on, display_rank
) VALUES (
    'RF8244', '34d61a18-ede8-4533-b130-7e5937bc1e9d-nc', 'August 5', '2026-08-05',
    'sms', '***-***-6610', 'declined', 'not_qualified',
    '$150 statement credit', NULL, '2026-11-03', 100
);

-- Filler. The profile holds one card, so an off-path card or statement read
-- answers from real rows.
INSERT INTO card_accounts (
    card_id, customer_id, card_last4, product_id, status, reported_lost,
    payment_status, credit_limit, available_credit
) VALUES (
    'card-daniel-brooks-5107', '34d61a18-ede8-4533-b130-7e5937bc1e9d', '5107', 'everyday-cash',
    'active', FALSE, 'current', 11000.00, 9633.40
);

INSERT INTO card_accounts (
    card_id, customer_id, card_last4, product_id, status, reported_lost,
    payment_status, credit_limit, available_credit
) VALUES (
    'card-daniel-brooks-nc-8890', '34d61a18-ede8-4533-b130-7e5937bc1e9d-nc', '8890',
    'horizon-balance', 'active', FALSE, 'current', 4000.00, 1220.75
);

INSERT INTO transactions (
    transaction_id, card_id, kind, merchant_key, merchant, merchant_location,
    descriptor, category, amount, status, settlement_state, occurred_at,
    posted_date, resource_label, short_ref
) VALUES
    ('transaction-brooks-4402', 'card-daniel-brooks-5107', 'posted', 'hardware',
     'Fenwick Hardware', 'Raleigh, North Carolina', 'FENWICK HDW',
     'home improvement', 212.64, 'posted', 'settled',
     '2026-08-21T15:41:00-04:00', '2026-08-22', 'transaction ending 4402', '4402'),
    ('transaction-brooks-4517', 'card-daniel-brooks-5107', 'posted', 'restaurant',
     'Bellweather Kitchen', 'Raleigh, North Carolina', 'BELLWEATHER KIT',
     'restaurant', 63.80, 'posted', 'settled',
     '2026-08-26T19:22:00-04:00', '2026-08-27', 'transaction ending 4517', '4517');

INSERT INTO scenario (key, value) VALUES
    ('next_identity_verification_id', '05f09e63-bd3c-4a75-813f-5429ac6d685c'),
    ('next_self_service_session_id', 'b549b0e6-fced-4596-8794-db42a62476a6'),
    ('next_notification_id', '4f70ccca-a2a3-4b99-ae66-cfb9d0e975c8'),
    ('target_referral_id', 'ce7d58b0-b5fc-42cb-a6e0-7a57929e1205'),
    ('target_referral_reference', 'RF8241')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;

COMMIT;
