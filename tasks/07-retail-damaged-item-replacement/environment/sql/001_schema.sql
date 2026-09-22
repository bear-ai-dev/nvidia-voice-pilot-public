-- Westline retail backend schema.
--
-- Shapes follow domains/retail/tool_registry.json result schemas and the
-- lifecycles stated in domains/retail/policy.md. Lifecycle vocabularies are
-- CHECK constraints rather than comments so an illegal state fails in the
-- database and not only in the tool layer.
--
-- Two retail-specific problems drive the less obvious tables here.
--
-- The recorded conversation calls get_order on the same order three times and
-- receives successively deeper read models. That is modelled by
-- section_read_cursor and section_view rather than by a script: the read count
-- is a row an operator can query, and a NULL payload is how a first read
-- legitimately returns no carrier evidence at all.
--
-- Several recorded values are human-relative strings ("15:18 yesterday",
-- "18:00 tomorrow") that the agent must read back verbatim. Every one of them
-- is stored twice: a *_display column carrying the exact string the tool emits
-- and a typed column carrying the same instant resolved against the scenario
-- clock, so the data stays queryable and a future moving clock could regenerate
-- the display form.

BEGIN;

-- Scenario clock and other per-conversation constants. The tools read
-- scenario_time from here rather than from wall time, so a container started
-- next year serves the same world.
CREATE TABLE scenario (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

-- Identifier allocation. Every id that appears in a tool result is issued from
-- here, so results are deterministic and a second allocation on the same scope
-- returns the next value instead of repeating.
CREATE TABLE id_allocator (
    entity_type  TEXT NOT NULL,
    scope        TEXT NOT NULL DEFAULT '',
    next_value   BIGINT NOT NULL,
    template     TEXT NOT NULL,
    PRIMARY KEY (entity_type, scope)
);

CREATE TABLE customers (
    customer_id        TEXT PRIMARY KEY,
    display_name       TEXT NOT NULL,
    email              TEXT NOT NULL,
    -- Read aloud to the caller, so stored exactly as the recorded results
    -- render it rather than derived from an address the results never disclosed
    -- in full.
    masked_email       TEXT NOT NULL,
    masked_phone       TEXT,
    -- Drives distribution-centre assignment for replacements.
    fulfillment_region TEXT NOT NULL,
    address_label      TEXT NOT NULL DEFAULT 'home_address_on_order'
);

CREATE UNIQUE INDEX customers_email ON customers (lower(email));

CREATE TABLE distribution_centers (
    dc_id        TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    region       TEXT NOT NULL
);

CREATE INDEX distribution_centers_region ON distribution_centers (region, dc_id);

CREATE TABLE products (
    product_reference   TEXT PRIMARY KEY,
    display_name        TEXT,
    category            TEXT NOT NULL,
    -- Drives the safety line on a waived return. An electrical item that
    -- arrived wet must not be powered on, and that instruction is a property of
    -- the product, not a sentence the agent may compose.
    hazard_class        TEXT CHECK (hazard_class IN ('electrical', 'glass', 'none')),
    disposal_disposition TEXT,
    safety_instruction  TEXT
);

CREATE TABLE product_variants (
    variant_reference      TEXT PRIMARY KEY,
    product_reference      TEXT NOT NULL REFERENCES products(product_reference),
    display_name           TEXT,
    color                  TEXT,
    -- Availability is tracked at two grains and a catalog row may know only
    -- one of them. NULL means the catalog does not answer that question, and
    -- the registry's rule is that an absent field reads as unavailable.
    in_stock               BOOLEAN,
    same_variant_in_stock  BOOLEAN,
    current_price          NUMERIC(10, 2),
    currency               TEXT NOT NULL DEFAULT 'USD' CHECK (currency = 'USD')
);

CREATE TABLE orders (
    order_reference        TEXT PRIMARY KEY,
    customer_id            TEXT NOT NULL REFERENCES customers(customer_id),
    placed_on              DATE NOT NULL,
    fulfillment_status     TEXT NOT NULL
        CHECK (fulfillment_status IN ('placed', 'processing', 'fulfilled',
                                      'shipped', 'out_for_delivery', 'delivered')),
    destination_label      TEXT NOT NULL DEFAULT 'home_address_on_order',
    -- A replacement is a new order linked to the affected original; it never
    -- overwrites it, which is what policy.md requires.
    replaces_order_reference TEXT REFERENCES orders(order_reference),
    -- Representative item description used by lookup_customer. NULL where the
    -- recorded results never revealed a product name for the order.
    representative_item    TEXT
);

CREATE INDEX orders_customer ON orders (customer_id, placed_on DESC);

CREATE TABLE order_items (
    item_reference     TEXT PRIMARY KEY,
    order_reference    TEXT NOT NULL REFERENCES orders(order_reference),
    line_no            INTEGER NOT NULL,
    variant_reference  TEXT REFERENCES product_variants(variant_reference),
    product_reference  TEXT REFERENCES products(product_reference),
    name               TEXT,
    variant_label      TEXT,
    color              TEXT,
    total_after_tax    NUMERIC(10, 2),
    currency           TEXT NOT NULL DEFAULT 'USD' CHECK (currency = 'USD'),
    UNIQUE (order_reference, line_no)
);

CREATE INDEX order_items_order ON order_items (order_reference, line_no);

CREATE TABLE payments (
    payment_seq         BIGSERIAL PRIMARY KEY,
    order_reference     TEXT NOT NULL REFERENCES orders(order_reference),
    tender_type         TEXT NOT NULL
        CHECK (tender_type IN ('gift_card', 'debit', 'credit', 'store_credit')),
    amount              NUMERIC(10, 2) NOT NULL CHECK (amount >= 0),
    currency            TEXT NOT NULL DEFAULT 'USD' CHECK (currency = 'USD'),
    original_card_last4 TEXT CHECK (original_card_last4 ~ '^[0-9]{4}$'),
    -- A card token belongs to a card tender and nowhere else.
    CHECK (original_card_last4 IS NULL OR tender_type IN ('debit', 'credit'))
);

CREATE INDEX payments_order ON payments (order_reference, payment_seq);

CREATE TABLE returns (
    return_reference     TEXT PRIMARY KEY,
    order_reference      TEXT NOT NULL REFERENCES orders(order_reference),
    item_reference       TEXT REFERENCES order_items(item_reference),
    return_status        TEXT NOT NULL
        CHECK (return_status IN ('initiated', 'in_transit', 'received', 'complete',
                                 'rejected')),
    accepted_at          TEXT,
    -- Typed; the age in days a result reports is computed against the scenario
    -- clock rather than stored, so the two can never disagree.
    accepted_on          DATE,
    inventory_disposition TEXT
);

CREATE INDEX returns_order ON returns (order_reference);

CREATE TABLE refunds (
    refund_seq          BIGSERIAL PRIMARY KEY,
    order_reference     TEXT NOT NULL REFERENCES orders(order_reference),
    return_reference    TEXT REFERENCES returns(return_reference),
    tender_type         TEXT NOT NULL
        CHECK (tender_type IN ('gift_card', 'debit', 'credit', 'store_credit')),
    amount              NUMERIC(10, 2) NOT NULL CHECK (amount >= 0),
    currency            TEXT NOT NULL DEFAULT 'USD' CHECK (currency = 'USD'),
    status              TEXT NOT NULL
        CHECK (status IN ('issued_available', 'submitted_no_settlement_confirmation',
                          'active', 'settled', 'rejected')),
    -- A gift-card refund has two statuses that are not the same fact. `status`
    -- is where the refund stands with the payment processor; `ledger_status` is
    -- where the issued card stands on the gift-card ledger. A refund can be
    -- issued and available to the processor while the card it created is active
    -- and unspent, and the desk's balance panel reports the second.
    ledger_status       TEXT
        CHECK (ledger_status IN ('active', 'exhausted', 'expired')),
    available_balance   NUMERIC(10, 2),
    used                BOOLEAN,
    delivery            TEXT,
    original_card_last4 TEXT CHECK (original_card_last4 ~ '^[0-9]{4}$'),
    initiation_source   TEXT
);

CREATE INDEX refunds_order ON refunds (order_reference, refund_seq);

-- Carrier evidence. `location` is what the courier entered and what a customer
-- sees; `evidence_location` is where the scan actually landed. They differ on
-- this order, which is precisely why possible_misscan is true: the pair is the
-- evidence, not the agent's interpretation of it.
CREATE TABLE carrier_scans (
    scan_seq          BIGSERIAL PRIMARY KEY,
    order_reference   TEXT NOT NULL REFERENCES orders(order_reference),
    scanned_at        TIMESTAMPTZ NOT NULL,
    scanned_at_display TEXT NOT NULL,
    location          TEXT NOT NULL,
    evidence_location TEXT,
    unit_number       TEXT,
    locker            TEXT,
    photo_reference   TEXT,
    possible_misscan  BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX carrier_scans_order ON carrier_scans (order_reference, scanned_at DESC);

-- Per-case-type policy. Deadlines, review windows, eligibility triggers and the
-- next action a trace offers are the desk's rules, so a trace opened on an
-- order the recording never touched answers with the same rules rather than
-- with nothing.
CREATE TABLE case_type_policy (
    case_type                     TEXT PRIMARY KEY
        CHECK (case_type IN ('delivery_trace', 'refund_trace')),
    initial_status                TEXT NOT NULL,
    deadline_offset_days          INTEGER,
    deadline_local_time           TEXT
        CHECK (deadline_local_time ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'),
    review_window_min_days        INTEGER,
    review_window_max_days        INTEGER,
    duplicate_refund_blocked      BOOLEAN,
    approval_required             BOOLEAN,
    approval_channel              TEXT,
    next_action                   TEXT,
    eligibility_triggers          TEXT[],
    carrier_may_contact_customer  BOOLEAN,
    pickup_guaranteed             BOOLEAN NOT NULL DEFAULT FALSE,
    -- Rendered with the normalized pickup site substituted for {site}.
    preference_instruction_template TEXT,
    -- What an order read discloses about a case of this type. The desk's case
    -- panel shows a case on the order in front of you in operational detail and
    -- a case on another order of the same account as context, so the two field
    -- sets differ. They are rows rather than a hard-coded projection because
    -- which columns a panel shows is a desk configuration, and because the two
    -- retail conversations that read a delivery trace disclose different parts
    -- of it.
    order_view_fields             TEXT[] NOT NULL,
    related_view_fields           TEXT[] NOT NULL
);

CREATE TABLE cases (
    case_number                      TEXT PRIMARY KEY,
    order_reference              TEXT NOT NULL REFERENCES orders(order_reference),
    customer_id                  TEXT NOT NULL REFERENCES customers(customer_id),
    case_type                    TEXT NOT NULL REFERENCES case_type_policy(case_type),
    status                       TEXT NOT NULL
        CHECK (status IN ('open', 'awaiting_carrier_response',
                          'pending_customer_or_external_response',
                          'reviewing_merchant_and_tender_records',
                          'awaiting_external_settlement',
                          'eligibility_determined',
                          'resolution_eligible_or_ineligible',
                          'resolved', 'closed')),
    reason                       TEXT
        CHECK (reason IN ('delivered_not_received', 'stalled_tracking',
                          'wrong_location', 'missing_refund')),
    item_description             TEXT,
    carrier_response             TEXT,
    deadline_at                  TIMESTAMPTZ,
    deadline_display             TEXT,
    carrier_may_contact_customer BOOLEAN,
    replacement_created          BOOLEAN NOT NULL DEFAULT FALSE,
    requested_resolution         TEXT
        CHECK (requested_resolution IN ('replacement', 'refund', 'locate_only',
                                        'undecided')),
    needed_by                    DATE,
    approval_required            BOOLEAN,
    approval_channel             TEXT,
    next_action                  TEXT,
    eligibility_triggers         TEXT[],
    review_window_min_days       INTEGER,
    review_window_max_days       INTEGER,
    duplicate_refund_blocked     BOOLEAN,
    return_evidence_attached     BOOLEAN,
    return_reference             TEXT REFERENCES returns(return_reference),
    payment_reference            TEXT,
    amount_under_review          NUMERIC(10, 2),
    -- Policy forbids approving a bank fee while a trace is open, so this is
    -- false and stays false until a specialist changes it.
    fee_reimbursement_approved   BOOLEAN NOT NULL DEFAULT FALSE,
    pickup_guaranteed            BOOLEAN NOT NULL DEFAULT FALSE,
    opened_at                    TIMESTAMPTZ NOT NULL,
    -- A deadline must carry both representations or neither.
    CHECK ((deadline_at IS NULL) = (deadline_display IS NULL))
);

CREATE INDEX cases_order ON cases (order_reference, case_number);
CREATE INDEX cases_customer ON cases (customer_id, status);

CREATE TABLE case_items (
    case_number        TEXT NOT NULL REFERENCES cases(case_number),
    item_reference TEXT NOT NULL REFERENCES order_items(item_reference),
    PRIMARY KEY (case_number, item_reference)
);

-- Notes are numbered within their case rather than globally, so a note keeps
-- the same address whatever else the population happens to hold.
CREATE TABLE case_notes (
    case_number                 TEXT NOT NULL REFERENCES cases(case_number),
    note_no                 INTEGER NOT NULL,
    note                    TEXT NOT NULL,
    topic                   TEXT,
    visible_to_next_reviewer BOOLEAN NOT NULL DEFAULT TRUE,
    created_at              TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (case_number, note_no)
);

-- Note topics the desk recognises. A note that matches a topic marked
-- discloses_fee_decision makes the update result restate the case's standing
-- fee-reimbursement decision, because policy forbids an agent leaving a fee
-- claim on a case without saying what its status is.
CREATE TABLE note_topics (
    topic                  TEXT NOT NULL,
    match_pattern          TEXT NOT NULL,
    discloses_fee_decision BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (topic, match_pattern)
);

-- Customers name a pickup location the way they say it ("the West 23rd Street
-- pickup counter"); a reviewer instruction names the site. These are the label
-- endings the desk strips to get from one to the other, longest first.
CREATE TABLE pickup_site_suffixes (
    suffix TEXT PRIMARY KEY
);

CREATE TABLE case_preferences (
    case_number            TEXT PRIMARY KEY REFERENCES cases(case_number),
    -- What the customer said, kept verbatim.
    pickup_location    TEXT,
    -- The same location normalized to its site name, which is what a reviewer
    -- instruction is phrased around.
    pickup_site        TEXT,
    review_instruction TEXT,
    visible_to_next_reviewer BOOLEAN NOT NULL DEFAULT TRUE,
    recorded_at        TIMESTAMPTZ NOT NULL
);

CREATE TABLE eligible_resolutions (
    order_reference                TEXT NOT NULL REFERENCES orders(order_reference),
    resolution_type                TEXT NOT NULL
        CHECK (resolution_type IN ('replacement', 'refund', 'locate_only')),
    position                       INTEGER NOT NULL,
    preserves_original_price       BOOLEAN,
    return_required                BOOLEAN,
    photo_required                 BOOLEAN,
    optional_photo_upload_available BOOLEAN,
    photo_upload_blocks_fulfillment BOOLEAN,
    estimated_delivery_on          DATE,
    estimated_delivery_display     TEXT,
    default_fulfillment            TEXT,
    PRIMARY KEY (order_reference, resolution_type),
    -- The snapshot addresses rows by (order_reference, position), so it must be
    -- unique or two rows would share one digest entry in the damage check.
    UNIQUE (order_reference, position),
    CHECK ((estimated_delivery_on IS NULL) = (estimated_delivery_display IS NULL))
);

CREATE TABLE replacement_orders (
    replacement_order_reference TEXT PRIMARY KEY REFERENCES orders(order_reference),
    original_order_reference    TEXT NOT NULL REFERENCES orders(order_reference),
    reason                      TEXT NOT NULL
        CHECK (reason IN ('damaged', 'defective', 'missing', 'wrong_item')),
    status                      TEXT NOT NULL CHECK (status IN ('created', 'failed')),
    balance_due                 NUMERIC(10, 2) NOT NULL CHECK (balance_due >= 0),
    currency                    TEXT NOT NULL DEFAULT 'USD' CHECK (currency = 'USD'),
    fulfillment_method          TEXT NOT NULL
        CHECK (fulfillment_method IN ('ship_to_address', 'store_pickup')),
    fulfillment_location        TEXT,
    estimated_delivery_on       DATE,
    estimated_delivery_display  TEXT,
    estimate_guaranteed         BOOLEAN NOT NULL DEFAULT FALSE,
    distribution_center         TEXT,
    distribution_center_status  TEXT,
    tracking_notifications      BOOLEAN NOT NULL DEFAULT TRUE,
    return_required             BOOLEAN NOT NULL,
    disposition                 TEXT,
    safety                      TEXT,
    created_at                  TIMESTAMPTZ NOT NULL
);

CREATE TABLE notification_templates (
    template            TEXT PRIMARY KEY,
    message_type        TEXT NOT NULL
        CHECK (message_type IN ('replacement_confirmation', 'delivery_trace_confirmation',
                                'refund_trace_confirmation', 'case_reference')),
    subject_prefix      TEXT,
    included_fields     TEXT[] NOT NULL,
    -- Delivery state at the moment the message is created, followed by the
    -- receipt sequence the mail provider reports afterwards.
    initial_status      TEXT NOT NULL
        CHECK (initial_status IN ('queued', 'sent', 'delivered', 'failed')),
    delivery_progression TEXT[] NOT NULL,
    optional_photo_link BOOLEAN,
    photo_link_section  TEXT,
    -- What an order read discloses about a message sent from this template. A
    -- trace confirmation is quoted back by its identifier; a replacement
    -- confirmation is quoted back by its subject line and the photo-upload
    -- affordance the customer is being pointed at.
    order_view_fields   TEXT[] NOT NULL
);

-- Notification delivery advances on refresh rather than on wall time, because
-- the scenario clock is frozen: status_progression holds the receipt sequence
-- the mail provider reports for this message and status_index the last receipt
-- observed. Reading the order's notifications section polls for the next one,
-- which is why a message created as queued reads as sent a minute later.
CREATE TABLE notifications (
    notification_id     TEXT PRIMARY KEY,
    case_number             TEXT REFERENCES cases(case_number),
    order_reference     TEXT NOT NULL REFERENCES orders(order_reference),
    channel             TEXT NOT NULL CHECK (channel IN ('email', 'sms')),
    template            TEXT NOT NULL REFERENCES notification_templates(template),
    message_type        TEXT NOT NULL,
    masked_destination  TEXT NOT NULL,
    status              TEXT NOT NULL
        CHECK (status IN ('queued', 'sent', 'delivered', 'failed')),
    status_index        INTEGER NOT NULL DEFAULT 0,
    status_progression  TEXT[] NOT NULL,
    subject_prefix      TEXT,
    optional_photo_link BOOLEAN,
    photo_link_section  TEXT,
    included_fields     TEXT[] NOT NULL,
    sent_at             TIMESTAMPTZ,
    -- Null where the recorded results never disclosed a send timestamp; the
    -- typed column above still records one for auditing.
    sent_at_display     TEXT,
    created_at          TIMESTAMPTZ NOT NULL
);

CREATE INDEX notifications_order ON notifications (order_reference, notification_id);
CREATE INDEX notifications_case ON notifications (case_number);

CREATE TABLE specialist_transfers (
    transfer_id TEXT PRIMARY KEY,
    reason      TEXT NOT NULL,
    summary     TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('transferred', 'failed')),
    created_at  TIMESTAMPTZ NOT NULL
);

-- Progressive section reads.
--
-- The order service serves a deeper read model each time a section is asked for
-- again, which is why the enacted agent's second look at the carrier scan
-- carried evidence its first look did not. The read count is a row rather than
-- a list inside the server process, so an operator can query how many times a
-- section has been served and a reset rebuilds it with the database.
CREATE TABLE section_read_cursor (
    order_reference TEXT NOT NULL REFERENCES orders(order_reference),
    section         TEXT NOT NULL,
    reads_served    INTEGER NOT NULL DEFAULT 0 CHECK (reads_served >= 0),
    PRIMARY KEY (order_reference, section)
);

-- What to disclose at each depth. A NULL payload omits the section entirely,
-- which is how a first read legitimately returns no carrier evidence. Once the
-- deepest view has been served it repeats; the service does not cycle back to a
-- shallower disclosure.
--
-- Every payload is materialized in 004_scenario.sql by a query over the
-- normalized rows it projects. None is hand-written, so a payload cannot drift
-- away from the data it claims to summarize.
-- A materialized read model has to render money the way the tool layer does, or
-- the same amount would come back as 40 from one section and 40.0 from another.
-- Whole amounts are integers and the rest are decimals, which is the rule the
-- projection helpers apply to rows read directly.
CREATE FUNCTION money_json(amount NUMERIC) RETURNS JSONB AS $$
    SELECT CASE
        WHEN amount IS NULL THEN 'null'::jsonb
        WHEN amount = trunc(amount) THEN to_jsonb(amount::bigint)
        ELSE to_jsonb(amount::float8)
    END;
$$ LANGUAGE SQL IMMUTABLE;

CREATE TABLE section_view (
    order_reference TEXT NOT NULL REFERENCES orders(order_reference),
    section         TEXT NOT NULL,
    view_index      INTEGER NOT NULL CHECK (view_index >= 0),
    payload         JSONB,
    note            TEXT NOT NULL,
    PRIMARY KEY (order_reference, section, view_index)
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

-- Case notes and section read counts are keyed by a pair of columns; the
-- verifier addresses a row by one column, so it reads them through these views.
CREATE VIEW case_note_log AS
SELECT case_number || '#' || note_no AS note_key,
       case_number, note_no, note, topic, visible_to_next_reviewer
  FROM case_notes;

CREATE VIEW section_read_log AS
SELECT order_reference || '#' || section AS cursor_key,
       order_reference, section, reads_served
  FROM section_read_cursor;

COMMIT;
