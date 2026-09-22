-- Banking backend schema.
--
-- Shapes follow the result_schema definitions in
-- domains/banking/tool_registry.json; the lifecycle vocabularies come from
-- domains/banking/policy.md and are CHECK constraints rather than comments, so
-- an illegal state fails in the database and not only in the tool layer.
--
-- The whole registry is served from this one schema even though this
-- conversation only exercises part of it. A card read, a dispute session, or a
-- referral lookup an agent tries off the recorded path has to answer from the
-- same data the recorded calls read, so the card, referral, and dispute tables
-- are populated here too rather than stubbed.

BEGIN;

-- Scenario clock and other per-conversation constants. get_current_time and
-- every mutation timestamp read scenario_time from here rather than from wall
-- time, so a container started next year serves the same world.
CREATE TABLE scenario (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

-- Identifier allocation for entities whose identifiers are sequential rather
-- than derived from a business key. A real UPDATE ... RETURNING, so a second
-- allocation on the same scope returns the next value instead of repeating.
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

CREATE TABLE card_products (
    product_id                   TEXT PRIMARY KEY,
    product                      TEXT NOT NULL,
    family                       TEXT NOT NULL,
    category                     TEXT NOT NULL
        CHECK (category IN ('travel', 'cash_back', 'balance_transfer',
                            'student', 'secured', 'business')),
    annual_fee                   NUMERIC(10, 2) NOT NULL CHECK (annual_fee >= 0),
    annual_fee_currency          TEXT NOT NULL DEFAULT 'USD' CHECK (annual_fee_currency = 'USD'),
    foreign_transaction_fee      BOOLEAN NOT NULL,
    lounge_membership            BOOLEAN NOT NULL,
    airline_incidental_credit    BOOLEAN NOT NULL,
    automatic_free_checked_bag   BOOLEAN NOT NULL,
    airline_specific_rules_apply BOOLEAN NOT NULL,
    -- A withdrawn product still exists in the catalog and must not appear in a
    -- "current products" answer, which is what makes the filter load-bearing.
    active                       BOOLEAN NOT NULL,
    display_rank                 INTEGER NOT NULL
);

CREATE TABLE welcome_offers (
    offer_id      TEXT PRIMARY KEY,
    product_id    TEXT NOT NULL REFERENCES card_products(product_id),
    points        INTEGER NOT NULL CHECK (points >= 0),
    spend         NUMERIC(10, 2) NOT NULL CHECK (spend >= 0),
    spend_currency TEXT NOT NULL DEFAULT 'USD' CHECK (spend_currency = 'USD'),
    days          INTEGER NOT NULL CHECK (days >= 0),
    active        BOOLEAN NOT NULL,
    ends_on       DATE,
    display_rank  INTEGER NOT NULL
);

-- External knowledge. This is the one table that stores a payload instead of
-- decomposing it: search_knowledge_base's result schema is the typed union of
-- about twenty optional content shapes that never co-occur, and splitting a
-- union that never co-occurs into twenty tables would add schema surface and no
-- fidelity. See docs/SQL_ENVS.md.
--
-- query_pattern is a POSIX regular expression matched case-insensitively
-- against the caller's question. projection names an assembly the handler
-- performs against the normalized catalog, so the two records that are really
-- product listings are not frozen copies of the catalog.
CREATE TABLE kb_records (
    record_id          TEXT PRIMARY KEY,
    effective_at       DATE NOT NULL,
    query_pattern      TEXT NOT NULL,
    -- Higher wins when more than one record's pattern matches, so retrieval is
    -- deterministic rather than dependent on physical row order.
    priority           INTEGER NOT NULL DEFAULT 100,
    projection         TEXT
        CHECK (projection IN ('travel_card_matches', 'welcome_offers',
                              'product_airline_benefits')),
    subject_product_id TEXT REFERENCES card_products(product_id),
    payload            JSONB NOT NULL DEFAULT '{}'::jsonb,
    CHECK (projection IS DISTINCT FROM 'product_airline_benefits'
           OR subject_product_id IS NOT NULL)
);

-- Per-workflow surface of a secure self-service session. A NULL column means
-- the field is not part of that workflow's surface, which is why a
-- card_application session reports save_and_continue and a transaction_dispute
-- session reports claim_id instead: the difference is data, not a branch.
CREATE TABLE workflow_profiles (
    workflow                 TEXT PRIMARY KEY
        CHECK (workflow IN ('card_application', 'referral_status', 'transaction_dispute')),
    session_slug             TEXT NOT NULL,
    -- How the resource contributes to the session identifier.
    resource_suffix_source   TEXT NOT NULL
        CHECK (resource_suffix_source IN ('none', 'resource_id', 'resource_short_ref')),
    resume_supported         BOOLEAN NOT NULL,
    save_and_continue        BOOLEAN,
    credit_pull_authorized   BOOLEAN,
    visible_stages           TEXT[],
    claim_tracked            BOOLEAN NOT NULL DEFAULT FALSE,
    access_location          TEXT,
    display_label_template   TEXT,
    allowed_customer_actions TEXT[]
);

-- Delivery channels a session may be pushed to, and where each one's masked
-- destination comes from. secure_message lands in the in-account message centre
-- and therefore has no external destination to mask.
CREATE TABLE delivery_channels (
    channel            TEXT PRIMARY KEY
        CHECK (channel IN ('secure_message', 'email_notification')),
    destination_source TEXT NOT NULL
        CHECK (destination_source IN ('none', 'notification_email')),
    delivered_status   TEXT NOT NULL CHECK (delivered_status IN ('sent', 'delivered'))
);

CREATE TABLE notification_templates (
    template                     TEXT PRIMARY KEY,
    channel                      TEXT NOT NULL CHECK (channel IN ('email', 'sms')),
    status_on_send               TEXT NOT NULL
        CHECK (status_on_send IN ('requested', 'sent', 'delivered')),
    -- False for every approved template: policy forbids putting a working
    -- secure link in ordinary email or SMS. Stored so the tool reports the
    -- template's real property rather than a constant.
    contains_working_secure_link BOOLEAN NOT NULL
);

-- ---------------------------------------------------------------------------
-- people
-- ---------------------------------------------------------------------------

CREATE TABLE customers (
    customer_id                   TEXT PRIMARY KEY,
    account_id                    TEXT NOT NULL UNIQUE,
    full_name                     TEXT NOT NULL,
    family_name                   TEXT NOT NULL,
    -- The stem the bank uses when it names a record after this profile, e.g.
    -- 'SF204771' in verification-SF204771-email-change. Held as a column so
    -- identifiers that appear in tool results are read, never invented.
    verification_key              TEXT NOT NULL,
    -- Stem used when naming a travel notice after this customer.
    notice_slug                   TEXT NOT NULL,
    primary_email                 TEXT,
    notification_email            TEXT,
    billing_zip                   TEXT NOT NULL,
    birth_month                   INTEGER NOT NULL CHECK (birth_month BETWEEN 1 AND 12),
    birth_day                     INTEGER NOT NULL CHECK (birth_day BETWEEN 1 AND 31),
    mobile_last4                  TEXT CHECK (mobile_last4 ~ '^[0-9]{4}$'),
    -- NULL when the calling channel is not a verification factor for this
    -- profile, which is how caller_phone_match is absent from most lookups and
    -- present on the profile that requires it.
    caller_channel_match          BOOLEAN,
    required_verification_methods TEXT[] NOT NULL,
    -- 'email' profiles use the address as the login identifier, so an email
    -- change moves the login too. 'username' profiles do not.
    login_identifier_kind         TEXT NOT NULL
        CHECK (login_identifier_kind IN ('username', 'email'))
);

CREATE INDEX customers_name ON customers (lower(full_name));
CREATE INDEX customers_email ON customers (lower(notification_email));
CREATE INDEX customers_family ON customers (lower(family_name));

-- Channels already enrolled for confirmation challenges. Enrolment is not the
-- same as the number the call arrived on, which lives on the customer row.
CREATE TABLE trusted_channels (
    channel_id                TEXT PRIMARY KEY,
    customer_id               TEXT NOT NULL REFERENCES customers(customer_id),
    type                      TEXT NOT NULL CHECK (type IN ('sms', 'secure_message')),
    masked_destination        TEXT NOT NULL,
    enrolled                  BOOLEAN NOT NULL,
    -- Whether the customer completes a challenge sent here, and the time the
    -- completion is recorded. The completion happens through the approved
    -- secure path, outside every tool, so the backend's expectation of it is
    -- data: a channel whose customer never completes leaves a poll at 'sent'.
    confirmation_completes    BOOLEAN NOT NULL DEFAULT FALSE,
    confirmation_verified_at  TEXT
);

CREATE INDEX trusted_channels_customer ON trusted_channels (customer_id, type);

-- The open reason a profile is in contact, which is what a verification record
-- is scoped to. verify_customer_identity names its record after the customer's
-- open case, so re-verifying inside one case returns the same record instead of
-- minting a second one.
CREATE TABLE service_cases (
    case_id     TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
    case_kind   TEXT NOT NULL
        CHECK (case_kind IN ('email_change', 'referral', 'dispute', 'card',
                             'statement', 'payment')),
    case_slug   TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('open', 'closed')),
    opened_at   TEXT NOT NULL
);

CREATE UNIQUE INDEX service_cases_one_open
    ON service_cases (customer_id) WHERE status = 'open';

CREATE TABLE identity_verifications (
    verification_id  TEXT PRIMARY KEY,
    customer_id      TEXT NOT NULL REFERENCES customers(customer_id),
    status           TEXT NOT NULL
        CHECK (status IN ('verified', 'unverified', 'expired')),
    required_methods TEXT[] NOT NULL,
    matched_methods  TEXT[] NOT NULL,
    verified_at      TEXT,
    expires_at       TEXT,
    -- Only a verification the caller actually asserted a time for reports one
    -- back; see the handler. Held per record so the disclosure is inspectable.
    time_asserted    BOOLEAN NOT NULL DEFAULT FALSE,
    CHECK ((status = 'verified') = (verified_at IS NOT NULL))
);

CREATE TABLE channel_confirmations (
    confirmation_id    TEXT PRIMARY KEY,
    customer_id        TEXT NOT NULL REFERENCES customers(customer_id),
    channel_id         TEXT NOT NULL REFERENCES trusted_channels(channel_id),
    purpose            TEXT NOT NULL CHECK (purpose IN ('email_change')),
    masked_destination TEXT NOT NULL,
    status             TEXT NOT NULL
        CHECK (status IN ('requested', 'sent', 'delivered', 'verified', 'expired')),
    verification_id    TEXT REFERENCES identity_verifications(verification_id),
    sent_at            TEXT,
    verified_at        TEXT,
    expires_at         TEXT,
    CHECK ((status = 'verified') = (verified_at IS NOT NULL))
);

-- ---------------------------------------------------------------------------
-- card accounts
-- ---------------------------------------------------------------------------

CREATE TABLE card_accounts (
    card_id                   TEXT PRIMARY KEY,
    customer_id               TEXT NOT NULL REFERENCES customers(customer_id),
    card_last4                TEXT NOT NULL CHECK (card_last4 ~ '^[0-9]{4}$'),
    product_id                TEXT REFERENCES card_products(product_id),
    status                    TEXT NOT NULL
        CHECK (status IN ('active', 'temporarily_restricted')),
    reported_lost             BOOLEAN NOT NULL,
    payment_status            TEXT NOT NULL
        CHECK (payment_status IN ('current', 'past_due', 'in_collections')),
    credit_limit              NUMERIC(12, 2) NOT NULL CHECK (credit_limit >= 0),
    available_credit          NUMERIC(12, 2) NOT NULL CHECK (available_credit >= 0),
    available_credit_currency TEXT NOT NULL DEFAULT 'USD'
        CHECK (available_credit_currency = 'USD'),
    UNIQUE (customer_id, card_last4)
);

CREATE INDEX card_accounts_last4 ON card_accounts (card_last4);

-- Authorizations, declines, and posted charges in one ledger, because the tools
-- read them as three views of the same activity: get_card_account splits by
-- kind, get_credit_card_transactions reads the posted ones.
--
-- record_seq is the ledger order and is what an incremental section read
-- advances over, so "authorizations since the last account read" is answered by
-- a cursor over real rows rather than by remembering anything in the server.
CREATE TABLE transactions (
    transaction_id                   TEXT PRIMARY KEY,
    record_seq                       BIGSERIAL UNIQUE,
    card_id                          TEXT NOT NULL REFERENCES card_accounts(card_id),
    kind                             TEXT NOT NULL
        CHECK (kind IN ('authorization', 'decline', 'posted')),
    -- Local key the bank uses when naming a follow-on record after this
    -- activity, e.g. 'hotel' in hotel-authorization-840.
    merchant_key                     TEXT NOT NULL,
    merchant                         TEXT NOT NULL,
    merchant_location                TEXT,
    descriptor                       TEXT,
    category                         TEXT,
    amount                           NUMERIC(12, 2) NOT NULL CHECK (amount >= 0),
    currency                         TEXT NOT NULL DEFAULT 'USD' CHECK (currency = 'USD'),
    status                           TEXT NOT NULL
        CHECK (status IN ('approved', 'declined', 'posted')),
    reason                           TEXT
        CHECK (reason IN ('travel_review', 'prior_review_open', 'insufficient_credit',
                          'card_restricted', 'suspected_fraud', 'expired_card')),
    -- Whether an approved authorization is still an outstanding hold. A hold
    -- released by a resolved review has settled and is no longer reported as an
    -- outstanding authorization.
    settlement_state                 TEXT NOT NULL DEFAULT 'pending'
        CHECK (settlement_state IN ('pending', 'settled', 'reversed', 'not_applicable')),
    occurred_at                      TEXT,
    posted_date                      DATE,
    preceded_by_authorization_amount NUMERIC(12, 2)
        CHECK (preceded_by_authorization_amount >= 0),
    -- What a customer-facing session calls this activity, e.g.
    -- 'transaction ending 8472'. Stored because the label is read aloud.
    resource_label                   TEXT,
    -- Short reference the bank uses when naming a record after this activity,
    -- e.g. '8472' in session-dispute-8472.
    short_ref                        TEXT,
    -- Set when a declined attempt has been re-presented after a review was
    -- resolved, so the same attempt cannot be re-presented twice.
    represented_as                   TEXT,
    CHECK ((kind = 'decline') = (status = 'declined')),
    CHECK (reason IS NULL OR kind = 'decline')
);

CREATE INDEX transactions_card ON transactions (card_id, kind, record_seq);
CREATE INDEX transactions_amount ON transactions (card_id, amount);

CREATE TABLE card_restrictions (
    restriction_id TEXT PRIMARY KEY,
    card_id        TEXT NOT NULL REFERENCES card_accounts(card_id),
    kind           TEXT NOT NULL
        CHECK (kind IN ('travel_review', 'fraud_review', 'delinquency_hold',
                        'lost_card_block')),
    status         TEXT NOT NULL CHECK (status IN ('open', 'removed')),
    -- Whether confirming the linked activity is enough to lift it. A
    -- delinquency hold or a lost-card block is not, so a resolve attempt on one
    -- is refused instead of quietly succeeding.
    customer_resolvable BOOLEAN NOT NULL,
    opened_at      TEXT NOT NULL,
    resolved_at    TEXT,
    CHECK ((status = 'removed') = (resolved_at IS NOT NULL))
);

CREATE INDEX card_restrictions_card ON card_restrictions (card_id, status);

CREATE TABLE restriction_transactions (
    restriction_id TEXT NOT NULL REFERENCES card_restrictions(restriction_id),
    transaction_id TEXT NOT NULL REFERENCES transactions(transaction_id),
    link_rank      INTEGER NOT NULL,
    -- Written when the customer confirms this activity during a resolution.
    confirmed_at   TEXT,
    PRIMARY KEY (restriction_id, transaction_id)
);

CREATE TABLE travel_notices (
    notice_id               TEXT PRIMARY KEY,
    card_id                 TEXT NOT NULL REFERENCES card_accounts(card_id),
    destinations            TEXT[] NOT NULL,
    return_date             DATE,
    -- False on every notice: a notice is informational and guarantees nothing.
    -- Stored rather than asserted so the tool reports the record's property.
    authorization_guaranteed BOOLEAN NOT NULL,
    status                  TEXT NOT NULL CHECK (status IN ('created', 'expired', 'cancelled')),
    created_at              TEXT NOT NULL
);

CREATE INDEX travel_notices_card ON travel_notices (card_id, status);

-- How deep, and how far forward, each card section has been read. The retail
-- and card conversations read the same account section more than once and
-- legitimately get a different read model each time; docs/SQL_ENVS.md asks for
-- that to be state an operator can query rather than a list mutated inside the
-- server process.
CREATE TABLE card_section_policy (
    section    TEXT PRIMARY KEY
        CHECK (section IN ('status', 'available_credit', 'authorizations',
                           'declines', 'restrictions', 'travel_notices')),
    -- 'full' re-reads the whole section, at a disclosure depth chosen by the
    -- read cursor. 'incremental' reports only what the ledger recorded after
    -- the previous read of that section.
    disclosure TEXT NOT NULL CHECK (disclosure IN ('full', 'incremental'))
);

CREATE TABLE card_section_view (
    -- A card_id, or '*' for the depth every other card uses.
    scope      TEXT NOT NULL,
    section    TEXT NOT NULL REFERENCES card_section_policy(section),
    view_index INTEGER NOT NULL CHECK (view_index >= 0),
    -- Fields disclosed at this depth. A field whose column is NULL on the row
    -- is still omitted, so this declares what may be disclosed, not what is.
    fields     TEXT[] NOT NULL,
    PRIMARY KEY (scope, section, view_index)
);

CREATE TABLE card_section_read_cursor (
    card_id       TEXT NOT NULL REFERENCES card_accounts(card_id),
    section       TEXT NOT NULL REFERENCES card_section_policy(section),
    reads_served  INTEGER NOT NULL DEFAULT 0,
    last_seen_seq BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (card_id, section)
);

-- ---------------------------------------------------------------------------
-- referrals
-- ---------------------------------------------------------------------------

CREATE TABLE referrals (
    referral_id              TEXT PRIMARY KEY,
    referring_customer_id    TEXT NOT NULL REFERENCES customers(customer_id),
    -- The invitation date is read back to the customer as 'August 2'. The
    -- display string is emitted verbatim and the typed date exists so the data
    -- stays queryable; see docs/SQL_ENVS.md on human-relative strings.
    invited_at_display       TEXT,
    invited_on               DATE,
    invited_channel          TEXT CHECK (invited_channel IN ('email', 'sms')),
    invited_masked           TEXT,
    application_status       TEXT NOT NULL
        CHECK (application_status IN ('invited', 'applied', 'approved', 'declined',
                                      'expired')),
    qualification_status     TEXT NOT NULL
        CHECK (qualification_status IN ('purchase_pending', 'qualified', 'not_qualified')),
    offer                    TEXT NOT NULL,
    offer_version_record_id  TEXT,
    deadline_on              DATE,
    display_rank             INTEGER NOT NULL DEFAULT 100
);

CREATE INDEX referrals_customer ON referrals (referring_customer_id, display_rank);

-- ---------------------------------------------------------------------------
-- secure self-service
-- ---------------------------------------------------------------------------

CREATE TABLE self_service_sessions (
    session_id             TEXT PRIMARY KEY,
    customer_id            TEXT NOT NULL REFERENCES customers(customer_id),
    workflow               TEXT NOT NULL REFERENCES workflow_profiles(workflow),
    resource_id            TEXT NOT NULL,
    status                 TEXT NOT NULL
        CHECK (status IN ('requested', 'issued', 'open_not_submitted', 'saved',
                          'submitted', 'expired', 'closed')),
    submitted              BOOLEAN NOT NULL,
    resume_supported       BOOLEAN NOT NULL,
    save_and_continue      BOOLEAN,
    credit_pull_authorized BOOLEAN,
    claim_tracked          BOOLEAN NOT NULL,
    claim_id               TEXT,
    access_location        TEXT,
    display_label          TEXT,
    allowed_customer_actions TEXT[],
    visible_stages         TEXT[],
    -- Whether the customer opens the session after it is delivered. Opening
    -- happens in online banking, outside every tool, so the backend's
    -- expectation of it is data: a session nobody opens still reads 'issued'.
    customer_opens         BOOLEAN NOT NULL DEFAULT TRUE,
    issued_at              TEXT NOT NULL,
    opened_at              TEXT,
    expires_at             TEXT,
    CHECK (submitted = (status = 'submitted')),
    CHECK (claim_id IS NULL OR claim_tracked)
);

CREATE INDEX sessions_customer ON self_service_sessions (customer_id, workflow);

CREATE TABLE session_deliveries (
    session_id         TEXT NOT NULL REFERENCES self_service_sessions(session_id),
    channel            TEXT NOT NULL REFERENCES delivery_channels(channel),
    delivery_rank      INTEGER NOT NULL,
    status             TEXT NOT NULL CHECK (status IN ('sent', 'delivered', 'failed')),
    masked_destination TEXT,
    PRIMARY KEY (session_id, channel)
);

CREATE TABLE notifications (
    notification_id              TEXT PRIMARY KEY,
    customer_id                  TEXT NOT NULL REFERENCES customers(customer_id),
    related_resource_id          TEXT NOT NULL,
    channel                      TEXT NOT NULL CHECK (channel IN ('email', 'sms')),
    template                     TEXT NOT NULL REFERENCES notification_templates(template),
    status                       TEXT NOT NULL
        CHECK (status IN ('requested', 'sent', 'delivered', 'failed')),
    masked_destination           TEXT,
    contains_working_secure_link BOOLEAN NOT NULL,
    sent_at                      TEXT
);

CREATE INDEX notifications_customer ON notifications (customer_id);

CREATE TABLE specialist_transfers (
    transfer_id TEXT PRIMARY KEY,
    reason      TEXT NOT NULL,
    summary     TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('initiated', 'failed')),
    created_at  TEXT NOT NULL
);

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
