-- ClearWave Mobile carrier-support backend schema.
--
-- Shapes follow domains/telecom/tool_registry.json result schemas and the
-- lifecycle vocabularies stated in domains/telecom/policy.md. Those
-- vocabularies are CHECK constraints rather than comments so an illegal status
-- fails in the database and not only in the tool layer.
--
-- The organising decision here is that no high-speed data figure is stored.
-- A plan carries an allowance, usage_samples carry consumption, and
-- addon_transactions carry purchased increments; every "used" and "remaining"
-- number a tool reports is an aggregate over those rows. The recorded call's
-- 11.8 GB, 2.2 GB, and 7.2 GB are therefore consequences of the seed rather
-- than columns, and an add-on purchased off the recorded path moves them.

BEGIN;

-- ---------------------------------------------------------------------------
-- infrastructure
-- ---------------------------------------------------------------------------

-- Scenario clock and other per-conversation constants. The tools read
-- scenario_time from here rather than from wall time, so a container started
-- next year serves the same world.
CREATE TABLE scenario (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

-- Identifier allocation. Every id a mutation puts into a tool result is issued
-- from here, so results are deterministic and a second mutation on the same
-- scope cannot reissue the first one's identifier.
CREATE TABLE id_allocator (
    entity_type  TEXT NOT NULL,
    scope        TEXT NOT NULL DEFAULT '',
    next_value   INTEGER NOT NULL,
    template     TEXT NOT NULL,
    PRIMARY KEY (entity_type, scope)
);

-- Every recorded result that carries a timestamp carries a different one: the
-- verification lands 68 seconds into the call, the usage read 105, the second
-- bill read 262. A backend would stamp these from its own clock, which this
-- environment deliberately does not have, so the elapsed offsets are recorded
-- as data: one row per tool per invocation ordinal.
--
-- This is the same shape as the section_read_cursor pattern in docs/SQL_ENVS.md
-- and for the same reason: the read count is a row an operator can query rather
-- than a counter living inside the server process.
CREATE TABLE tool_clock (
    tool_name       TEXT NOT NULL,
    call_index      INTEGER NOT NULL CHECK (call_index >= 1),
    offset_seconds  INTEGER NOT NULL CHECK (offset_seconds >= 0),
    PRIMARY KEY (tool_name, call_index)
);

-- How many times each tool has been called, and how far the clock advances for
-- a call the recording never made. Past the recorded offsets the clock keeps
-- moving forward in default_step_seconds increments, so an off-path second
-- add-on gets a later effective_at than the first rather than the same one.
CREATE TABLE tool_clock_cursor (
    tool_name             TEXT PRIMARY KEY,
    calls_served          INTEGER NOT NULL DEFAULT 0 CHECK (calls_served >= 0),
    default_step_seconds  INTEGER NOT NULL DEFAULT 30 CHECK (default_step_seconds > 0)
);

-- Which access scope a tool needs from an identity verification. Held as data
-- so the gate is inspectable and so a verification tier that authorizes usage
-- but not billing is a seeding decision rather than a code branch. A tool with
-- no row here is ungated: lookup and verification run before any scope exists.
CREATE TABLE tool_access_requirements (
    tool_name       TEXT PRIMARY KEY,
    required_scope  TEXT NOT NULL
        CHECK (required_scope IN ('lines', 'devices', 'plans', 'usage', 'billing'))
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

-- ---------------------------------------------------------------------------
-- time rendering
-- ---------------------------------------------------------------------------

-- Render an instant the way the recorded results render it: local wall time in
-- a named zone with an explicit numeric offset. The offset is derived from the
-- zone rather than pasted in, so a cycle that crossed a DST boundary would
-- still print correctly.
CREATE FUNCTION iso8601(ts TIMESTAMPTZ, tz TEXT) RETURNS TEXT
LANGUAGE sql STABLE AS $$
    SELECT to_char(ts AT TIME ZONE tz, 'YYYY-MM-DD"T"HH24:MI:SS')
           || CASE WHEN off_seconds < 0 THEN '-' ELSE '+' END
           || lpad((abs(off_seconds) / 3600)::text, 2, '0')
           || ':'
           || lpad(((abs(off_seconds) % 3600) / 60)::text, 2, '0')
      FROM (
        SELECT (EXTRACT(EPOCH FROM (ts AT TIME ZONE tz))
                - EXTRACT(EPOCH FROM (ts AT TIME ZONE 'UTC')))::bigint AS off_seconds
      ) AS o;
$$;

CREATE FUNCTION scenario_timezone() RETURNS TEXT
LANGUAGE sql STABLE AS $$
    SELECT value FROM scenario WHERE key = 'timezone';
$$;

-- The conversation's clock. Handlers use this in place of now(), which is what
-- makes two runs of the image agree.
CREATE FUNCTION scenario_now() RETURNS TIMESTAMPTZ
LANGUAGE sql STABLE AS $$
    SELECT (SELECT value FROM scenario WHERE key = 'scenario_time')::timestamptz;
$$;

CREATE FUNCTION scenario_iso(ts TIMESTAMPTZ) RETURNS TEXT
LANGUAGE sql STABLE AS $$
    SELECT iso8601(ts, scenario_timezone());
$$;

-- ---------------------------------------------------------------------------
-- catalogs
-- ---------------------------------------------------------------------------

-- What a measurement source can tell you. Carrier metering reports aggregate
-- line usage and nothing per-application; the policy turns on that distinction,
-- so it is a property of the source rather than a literal in a handler.
CREATE TABLE measurement_sources (
    source_id                 TEXT PRIMARY KEY,
    app_attribution_available BOOLEAN NOT NULL,
    description               TEXT NOT NULL
);

CREATE TABLE plans (
    plan_id                          TEXT PRIMARY KEY,
    name                             TEXT NOT NULL,
    -- The included high-speed allowance. Every remaining-data figure any tool
    -- reports is this minus metered consumption plus purchased add-ons.
    high_speed_allowance_gigabytes   NUMERIC(10, 2) NOT NULL
        CHECK (high_speed_allowance_gigabytes >= 0),
    after_high_speed_allowance       TEXT NOT NULL
        CHECK (after_high_speed_allowance IN ('speed_reduced', 'overage_billed')),
    monthly_price                    NUMERIC(10, 2) NOT NULL,
    currency                         TEXT NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    addons_allowed                   BOOLEAN NOT NULL DEFAULT TRUE
);

-- The identity factors a given intake channel requires and the account scope a
-- successful verification on that channel grants. The recorded call arrives on
-- the support channel, which is why its verification grants lines, usage, and
-- billing and not devices or plans.
CREATE TABLE verification_policies (
    channel           TEXT PRIMARY KEY,
    required_factors  TEXT[] NOT NULL,
    granted_scope     TEXT[] NOT NULL
);

CREATE TABLE addon_offers (
    offer_id              TEXT PRIMARY KEY,
    plan_id               TEXT NOT NULL REFERENCES plans(plan_id),
    data_gigabytes        NUMERIC(10, 2) NOT NULL CHECK (data_gigabytes > 0),
    price                 NUMERIC(10, 2) NOT NULL CHECK (price >= 0),
    currency              TEXT NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    billing_timing        TEXT NOT NULL
        CHECK (billing_timing IN ('next_bill', 'immediate')),
    effective_timing      TEXT NOT NULL
        CHECK (effective_timing IN ('immediate', 'next_cycle')),
    expires_at            TIMESTAMPTZ NOT NULL,
    -- Eligibility preconditions. Checked against the line at read time, so the
    -- same catalog row is eligible for one line and not for another.
    requires_line_status  TEXT NOT NULL DEFAULT 'active'
        CHECK (requires_line_status IN ('active', 'suspended', 'disconnected', 'pending')),
    requires_autopay      BOOLEAN NOT NULL DEFAULT FALSE,
    withdrawn             BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX addon_offers_plan ON addon_offers (plan_id, expires_at);

-- ---------------------------------------------------------------------------
-- account
-- ---------------------------------------------------------------------------

CREATE TABLE customers (
    customer_id     TEXT PRIMARY KEY,
    -- Stem the carrier's per-customer record identifiers are built from. The
    -- verification record id is derived from this and the intake channel rather
    -- than drawn from a counter, because a verification is one record per
    -- caller per channel and re-verifying refreshes it.
    slug            TEXT NOT NULL UNIQUE,
    full_name       TEXT NOT NULL,
    date_of_birth   DATE NOT NULL,
    account_status  TEXT NOT NULL
        CHECK (account_status IN ('active', 'suspended', 'closed', 'pending_activation')),
    autopay_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    -- A hold makes verification inconclusive even when every factor matches,
    -- which is the case the policy's "offer a retry or transfer" branch exists
    -- for. NULL is the ordinary case.
    identity_hold   TEXT
        CHECK (identity_hold IN ('security_review', 'fraud_review', 'port_protection'))
);

CREATE INDEX customers_name_dob ON customers (lower(full_name), date_of_birth);

CREATE TABLE billing_cycles (
    billing_cycle_id  TEXT PRIMARY KEY,
    customer_id       TEXT NOT NULL REFERENCES customers(customer_id),
    cycle_start       TIMESTAMPTZ NOT NULL,
    cycle_end         TIMESTAMPTZ NOT NULL,
    -- Exactly one cycle per customer is the open one. Kept as a column rather
    -- than derived from scenario_now() so a cycle boundary is a fact about the
    -- account and not a side effect of the clock.
    is_current        BOOLEAN NOT NULL DEFAULT FALSE,
    CHECK (cycle_end > cycle_start)
);

CREATE UNIQUE INDEX billing_cycles_one_current
    ON billing_cycles (customer_id) WHERE is_current;

CREATE TABLE lines (
    line_id               TEXT PRIMARY KEY,
    customer_id           TEXT NOT NULL REFERENCES customers(customer_id),
    mobile_number         TEXT NOT NULL
        CHECK (mobile_number ~ '^[0-9]{3}-[0-9]{3}-[0-9]{4}$'),
    -- The masked form a verified read discloses. Generated from the stored
    -- number so the two cannot drift apart, and so the mask is visibly a
    -- projection of account data rather than a separately authored string.
    masked_mobile_number  TEXT GENERATED ALWAYS AS
        ('***-***-' || substr(mobile_number, 9, 4)) STORED,
    status                TEXT NOT NULL
        CHECK (status IN ('active', 'suspended', 'disconnected', 'pending')),
    plan_id               TEXT NOT NULL REFERENCES plans(plan_id),
    billing_cycle_id      TEXT NOT NULL REFERENCES billing_cycles(billing_cycle_id),
    is_primary            BOOLEAN NOT NULL DEFAULT FALSE,
    autopay_enabled       BOOLEAN NOT NULL DEFAULT FALSE,
    -- Metering the carrier applies to this line. Reported when a usage window
    -- holds no samples at all and there is nothing to read a source from.
    metering_source       TEXT NOT NULL REFERENCES measurement_sources(source_id),
    activated_on          DATE,
    ported_out_at         TIMESTAMPTZ,
    CHECK (ported_out_at IS NULL OR status = 'disconnected')
);

CREATE INDEX lines_customer ON lines (customer_id, line_id);
CREATE INDEX lines_number ON lines (mobile_number);

CREATE TABLE devices (
    device_id            TEXT PRIMARY KEY,
    line_id              TEXT NOT NULL REFERENCES lines(line_id),
    manufacturer         TEXT NOT NULL,
    model                TEXT NOT NULL,
    -- Carrier-observed provisioning state. Deliberately not a place to record
    -- anything the customer says about the handset; see
    -- customer_reported_device_state.
    provisioning_status  TEXT NOT NULL
        CHECK (provisioning_status IN ('active', 'pending', 'deprovisioned', 'blocked')),
    imei_suffix          TEXT CHECK (imei_suffix ~ '^[0-9]{4}$'),
    activated_at         TIMESTAMPTZ
);

CREATE INDEX devices_line ON devices (line_id);

CREATE TABLE identity_verifications (
    verification_id     TEXT PRIMARY KEY,
    customer_id         TEXT NOT NULL REFERENCES customers(customer_id),
    channel             TEXT NOT NULL REFERENCES verification_policies(channel),
    status              TEXT NOT NULL
        CHECK (status IN ('verified', 'failed', 'inconclusive')),
    matched_factors     TEXT[] NOT NULL,
    -- Empty for anything but a verified record: a failed verification grants
    -- nothing, and the gate reads this column rather than the status.
    access_scope        TEXT[] NOT NULL,
    verified_at         TIMESTAMPTZ NOT NULL,
    -- The exact string the tool emitted, written by the handler through
    -- scenario_iso(). Kept alongside the typed column so the emitted form is a
    -- row an operator can read back, per docs/SQL_ENVS.md.
    verified_at_display TEXT NOT NULL,
    UNIQUE (customer_id, channel)
);

CREATE TABLE bills (
    bill_id           TEXT PRIMARY KEY,
    billing_cycle_id  TEXT NOT NULL UNIQUE REFERENCES billing_cycles(billing_cycle_id),
    customer_id       TEXT NOT NULL REFERENCES customers(customer_id),
    -- A cycle exists whether or not it has been billed, and the open bill of
    -- the current cycle is the one a next-bill charge lands on.
    status            TEXT NOT NULL
        CHECK (status IN ('open', 'issued', 'paid', 'overdue', 'void')),
    currency          TEXT NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    issued_at         TIMESTAMPTZ,
    due_at            TIMESTAMPTZ,
    CHECK ((status = 'open') = (issued_at IS NULL))
);

CREATE INDEX bills_customer ON bills (customer_id, status);

-- Individual charges. An overage figure is the sum of the overage lines on a
-- bill, so a plan that reduces speed instead of billing overage reports zero
-- because it has no such lines, not because a column says zero.
CREATE TABLE bill_charges (
    charge_id       TEXT PRIMARY KEY,
    bill_id         TEXT NOT NULL REFERENCES bills(bill_id),
    kind            TEXT NOT NULL
        CHECK (kind IN ('recurring_plan', 'addon', 'overage', 'equipment',
                        'tax', 'credit', 'adjustment')),
    description     TEXT NOT NULL,
    amount          NUMERIC(10, 2) NOT NULL,
    currency        TEXT NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    billing_timing  TEXT NOT NULL DEFAULT 'next_bill'
        CHECK (billing_timing IN ('next_bill', 'immediate'))
);

CREATE INDEX bill_charges_bill ON bill_charges (bill_id, kind);

-- Metered consumption, one row per measured interval. A usage figure is a sum
-- over these and a reported window is their extent, which is why the recorded
-- read of the last 24 hours reports midnight to four in the morning: that is
-- where the samples are, not what was asked for.
CREATE TABLE usage_samples (
    sample_id           TEXT PRIMARY KEY,
    line_id             TEXT NOT NULL REFERENCES lines(line_id),
    billing_cycle_id    TEXT NOT NULL REFERENCES billing_cycles(billing_cycle_id),
    window_start        TIMESTAMPTZ NOT NULL,
    window_end          TIMESTAMPTZ NOT NULL,
    gigabytes           NUMERIC(10, 2) NOT NULL CHECK (gigabytes >= 0),
    measurement_source  TEXT NOT NULL REFERENCES measurement_sources(source_id),
    CHECK (window_end > window_start)
);

CREATE INDEX usage_samples_line_window
    ON usage_samples (line_id, window_start, window_end);
CREATE INDEX usage_samples_line_cycle ON usage_samples (line_id, billing_cycle_id);

-- Purchased high-speed increments. Separate rows rather than an increment to a
-- plan or line column, so two purchases in one cycle both count and either can
-- be reversed without recomputing a stored balance.
CREATE TABLE addon_transactions (
    transaction_id           TEXT PRIMARY KEY,
    line_id                  TEXT NOT NULL REFERENCES lines(line_id),
    offer_id                 TEXT NOT NULL REFERENCES addon_offers(offer_id),
    billing_cycle_id         TEXT NOT NULL REFERENCES billing_cycles(billing_cycle_id),
    bill_id                  TEXT NOT NULL REFERENCES bills(bill_id),
    status                   TEXT NOT NULL
        CHECK (status IN ('pending', 'active', 'failed', 'reversed')),
    data_gigabytes           NUMERIC(10, 2) NOT NULL CHECK (data_gigabytes > 0),
    charged_price            NUMERIC(10, 2) NOT NULL CHECK (charged_price >= 0),
    currency                 TEXT NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    effective_at             TIMESTAMPTZ NOT NULL,
    effective_at_display     TEXT NOT NULL,
    authorized_by_customer   BOOLEAN NOT NULL
);

CREATE INDEX addon_transactions_line_cycle
    ON addon_transactions (line_id, billing_cycle_id, status);

-- What the customer said about the handset, kept apart from everything the
-- network observed. The policy forbids presenting one as the other, and no tool
-- in this registry writes here: the registry has no device-telemetry or
-- customer-report operation, so an app-usage figure a caller reads off their
-- screen during a call must not end up in the carrier's records at all. The
-- seeded rows are reports taken on earlier contacts.
CREATE TABLE customer_reported_device_state (
    report_id           TEXT PRIMARY KEY,
    line_id             TEXT NOT NULL REFERENCES lines(line_id),
    reported_at         TIMESTAMPTZ NOT NULL,
    -- How the report was referred to on the call it came from. Non-ISO by
    -- design; the typed column above is what queries use.
    reported_at_display TEXT NOT NULL,
    report_kind         TEXT NOT NULL
        CHECK (report_kind IN ('app_usage_screen', 'app_setting', 'device_setting',
                               'speed_test')),
    channel             TEXT NOT NULL REFERENCES verification_policies(channel),
    app_name            TEXT,
    reported_gigabytes  NUMERIC(10, 2) CHECK (reported_gigabytes >= 0),
    setting_name        TEXT,
    setting_value       TEXT
);

CREATE INDEX customer_reported_line ON customer_reported_device_state (line_id);

CREATE TABLE specialist_transfers (
    transfer_id  TEXT PRIMARY KEY,
    reason       TEXT NOT NULL,
    summary      TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('accepted', 'failed')),
    created_at   TIMESTAMPTZ NOT NULL,
    created_at_display TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- derived
-- ---------------------------------------------------------------------------

-- The high-speed position of every line in its current cycle, as an aggregate.
-- This is the view the usage and add-on tools read, and it is the reason
-- add_data_addon changes what get_line_data_usage reports: the add-on inserts a
-- row that this sum picks up. Nothing writes remaining_gigabytes.
CREATE VIEW line_high_speed_balance AS
SELECT l.line_id,
       l.billing_cycle_id,
       p.high_speed_allowance_gigabytes AS allowance_gigabytes,
       COALESCE(u.consumed, 0)::numeric(12, 2) AS consumed_gigabytes,
       COALESCE(a.added, 0)::numeric(12, 2) AS added_gigabytes,
       GREATEST(p.high_speed_allowance_gigabytes
                + COALESCE(a.added, 0)
                - COALESCE(u.consumed, 0), 0)::numeric(12, 2) AS remaining_gigabytes
  FROM lines l
  JOIN plans p ON p.plan_id = l.plan_id
  LEFT JOIN (
      SELECT line_id, billing_cycle_id, SUM(gigabytes) AS consumed
        FROM usage_samples
       GROUP BY line_id, billing_cycle_id
  ) u ON u.line_id = l.line_id AND u.billing_cycle_id = l.billing_cycle_id
  LEFT JOIN (
      SELECT line_id, billing_cycle_id, SUM(data_gigabytes) AS added
        FROM addon_transactions
       WHERE status = 'active'
       GROUP BY line_id, billing_cycle_id
  ) a ON a.line_id = l.line_id AND a.billing_cycle_id = l.billing_cycle_id;

COMMIT;
