#!/usr/bin/env python3
"""Author-time generator for this task's catalog and population SQL.

Writes two files next to itself:

  sql/002_reference.sql   measurement sources, plans, verification policies, the
                          add-on catalog, the scenario clock, the per-tool call
                          clock, and the tool access gate
  sql/003_population.sql  customers, cycles, bills and their charges, lines,
                          devices, usage samples, historical add-ons and
                          verifications, and the per-line id allocators

The rows the recorded conversation touches are not here: they are declared by
hand in sql/004_scenario.sql, so regenerating with a different RNG seed cannot
move them. Rows that exist only to be distractors are declared explicitly in
GUARANTEED_* below rather than drawn at random, so a probe against them stays
stable across regenerations.

Everything else exists to make the lookups do work: customers who share the
target's surname, a second Benjamin Reed with a different date of birth, a
duplicate account pair that makes a mobile-number lookup ambiguous, holds that
make verification inconclusive, suspended and ported-out lines, expired and
autopay-gated offers, overdue bills, and usage samples in cycles other than the
current one.

This script never enters the container image; see environment/.dockerignore.

Usage:  python3 gen_seed.py
"""
from __future__ import annotations

import datetime as dt
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))
SQL = os.path.join(HERE, "sql")

RNG_SEED = 20260827
SCENARIO_TIME = "2026-08-27T19:30:00-05:00"
SCENARIO_TZ = "America/Chicago"
# Every date in this seed falls inside 2026 US central daylight time, so the
# literal offset below is the one iso8601() derives from the zone. Anything
# outside that range would need the zone, not this constant.
OFFSET = "-05:00"

SCENARIO_NOW = dt.datetime(2026, 8, 27, 19, 30)

# Held by the scenario's line and by the declared distractor lines below. The
# random population must never reissue one, so a lookup or a probe against any
# of them keeps resolving to the record it was written for.
RESERVED_NUMBERS = {"0176", "0431", "0612", "0777", "0888"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def q(value) -> str:
    """Render a Python value as a SQL literal."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        if not value:
            return "'{}'"
        inner = ",".join('"' + str(v).replace('"', '\\"') + '"' for v in value)
        return "'{" + inner + "}'"
    return "'" + str(value).replace("'", "''") + "'"


def insert(table: str, columns: list[str], rows: list[tuple]) -> str:
    if not rows:
        return ""
    head = f"INSERT INTO {table} ({', '.join(columns)}) VALUES\n"
    body = ",\n".join("    (" + ", ".join(q(v) for v in row) + ")" for row in rows)
    return head + body + ";\n\n"


def stamp(moment: dt.datetime) -> str:
    """Local wall time as the offset-bearing literal the schema stores."""
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + OFFSET


def add_months(day: dt.date, months: int) -> dt.date:
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    return dt.date(year, month, day.day)


def money(value: float) -> str:
    return f"{value:.2f}"


# ---------------------------------------------------------------------------
# catalogs
# ---------------------------------------------------------------------------

MEASUREMENT_SOURCES = [
    ("carrier_metering", False,
     "Network-side aggregate metering. Reports bytes on the line and cannot "
     "attribute them to an application."),
    ("device_agent", True,
     "Handset-side reporting agent. Attributes usage per application, and is "
     "not provisioned on consumer lines."),
]

# plan_id, name, allowance GB, post-allowance behaviour, monthly price,
# add-ons allowed
#
# Unlimited Start is the scenario's plan. The recording revealed its identifier
# and display name; the 15 GB allowance is inferred, and the inference is
# checked by the call itself: the caller quotes an alert saying he had used 85%,
# and 12.8 GB metered against a 15 GB allowance is 85.3%.
PLANS = [
    ("70102739-54a7-4e7b-b251-5040b1fc2f21", "Unlimited Start", 15.00, "speed_reduced", 65.00, True),
    ("unlimited-plus", "Unlimited Plus", 50.00, "speed_reduced", 85.00, True),
    ("unlimited-ultimate", "Unlimited Ultimate", 100.00, "speed_reduced", 105.00, True),
    ("flex-5", "Flex 5", 5.00, "overage_billed", 35.00, True),
    ("flex-15", "Flex 15", 15.00, "overage_billed", 55.00, True),
    ("prepaid-basic", "Prepaid Basic", 2.00, "speed_reduced", 25.00, False),
    ("business-share-100", "Business Share 100", 100.00, "overage_billed", 180.00, True),
]

# The support channel is the one this call arrives on. Its granted scope is the
# recorded one: lines, usage, billing. Devices and plans are readable under it
# because they are attributes of a line, which is why the account read is gated
# on "lines" rather than on each requested section; the retail channel exists to
# show that the other scope values are reachable and mean something.
VERIFICATION_POLICIES = [
    ("support", ["mobile_number", "full_name", "date_of_birth"],
     ["lines", "usage", "billing"]),
    ("retail_store", ["mobile_number", "full_name", "date_of_birth"],
     ["lines", "devices", "plans"]),
    ("ivr", ["mobile_number"], ["billing"]),
]

# offer_id, plan_id, GB, price, billing timing, effective timing, expires_at,
# requires_line_status, requires_autopay, withdrawn
#
# Unlimited Start deliberately has exactly one unexpired offer, and it lives in
# 004_scenario.sql because every field of it was disclosed on the call. The two
# Unlimited Start rows here have already expired, so an offer read for the
# scenario's line has to filter them out to reproduce the recorded single-offer
# result.
ADDON_OFFERS = [
    ("offer-2gb-20-expired", "70102739-54a7-4e7b-b251-5040b1fc2f21", 2.00, 20.00, "next_bill",
     "immediate", "2026-08-14T23:59:00", "active", False, False),
    ("offer-10gb-60-expired", "70102739-54a7-4e7b-b251-5040b1fc2f21", 10.00, 60.00, "next_bill",
     "immediate", "2026-08-20T23:59:00", "active", False, False),
    ("offer-5gb-30-plus", "unlimited-plus", 5.00, 30.00, "next_bill",
     "immediate", "2026-09-04T23:59:00", "active", False, False),
    ("offer-15gb-70-plus", "unlimited-plus", 15.00, 70.00, "next_bill",
     "immediate", "2026-09-10T23:59:00", "active", False, False),
    ("offer-5gb-25-plus-autopay", "unlimited-plus", 5.00, 25.00, "next_bill",
     "immediate", "2026-09-12T23:59:00", "active", True, False),
    ("offer-30gb-90-ultimate", "unlimited-ultimate", 30.00, 90.00, "next_bill",
     "immediate", "2026-09-15T23:59:00", "active", False, False),
    ("offer-10gb-45-ultimate", "unlimited-ultimate", 10.00, 45.00, "immediate",
     "immediate", "2026-09-08T23:59:00", "active", False, False),
    ("offer-3gb-15-flex5", "flex-5", 3.00, 15.00, "immediate", "immediate",
     "2026-09-02T23:59:00", "active", False, False),
    ("offer-5gb-24-flex5-next-cycle", "flex-5", 5.00, 24.00, "next_bill",
     "next_cycle", "2026-09-18T23:59:00", "active", False, False),
    ("offer-8gb-38-flex15", "flex-15", 8.00, 38.00, "next_bill", "immediate",
     "2026-09-06T23:59:00", "active", False, False),
    ("offer-2gb-12-flex15-withdrawn", "flex-15", 2.00, 12.00, "next_bill",
     "immediate", "2026-09-20T23:59:00", "active", False, True),
    ("offer-1gb-10-prepaid", "prepaid-basic", 1.00, 10.00, "immediate",
     "immediate", "2026-09-09T23:59:00", "active", False, False),
    ("offer-50gb-140-business", "business-share-100", 50.00, 140.00, "next_bill",
     "immediate", "2026-09-25T23:59:00", "active", False, False),
    ("offer-20gb-80-business-suspended", "business-share-100", 20.00, 80.00,
     "next_bill", "immediate", "2026-09-25T23:59:00", "suspended", False, False),
]

# Elapsed seconds from the start of the call to each recorded timestamp, per
# tool, in the order that tool was invoked. Read from the recording:
# verification at 19:31:08, usage at 19:31:45, the two bill reads at 19:34:10
# and 19:34:22, the offer read at 19:34:46, the add-on at 19:37:35.
TOOL_CLOCK = [
    ("verify_customer_identity", 1, 68),
    ("get_line_data_usage", 1, 105),
    ("get_customer_bills", 1, 250),
    ("get_customer_bills", 2, 262),
    ("get_data_addon_offers", 1, 286),
    ("add_data_addon", 1, 455),
]

TOOL_NAMES = [
    "lookup_customer", "verify_customer_identity", "get_customer_account",
    "get_line_data_usage", "get_customer_bills", "get_data_addon_offers",
    "add_data_addon", "transfer_to_specialist",
]

# Which scope each protected tool needs. lookup_customer and
# verify_customer_identity run before any scope exists, and
# transfer_to_specialist must stay available precisely when verification failed.
TOOL_ACCESS = [
    ("get_customer_account", "lines"),
    ("get_line_data_usage", "usage"),
    ("get_customer_bills", "billing"),
    ("get_data_addon_offers", "billing"),
    ("add_data_addon", "billing"),
]


def build_reference() -> str:
    out = [
        "-- Catalogs, scenario clock, per-tool call clock, and the access gate.\n"
        "-- Generated by environment/gen_seed.py; do not edit by hand.\n\n"
        "BEGIN;\n\n"
    ]

    out.append(insert("scenario", ["key", "value"], [
        ("scenario_time", SCENARIO_TIME),
        ("conversation_id", "telecom-data-usage-cleanup"),
        ("domain", "telecom"),
        ("timezone", SCENARIO_TZ),
        # The channel the call arrived on. Selects the verification policy, and
        # so both the factors the lookup asks for and the scope it grants.
        ("intake_channel", "support"),
    ]))

    out.append(insert("measurement_sources",
                      ["source_id", "app_attribution_available", "description"],
                      MEASUREMENT_SOURCES))

    out.append(insert("plans",
                      ["plan_id", "name", "high_speed_allowance_gigabytes",
                       "after_high_speed_allowance", "monthly_price", "currency",
                       "addons_allowed"],
                      [(pid, name, money(gb), behaviour, money(price), "USD", addons)
                       for (pid, name, gb, behaviour, price, addons) in PLANS]))

    out.append(insert("verification_policies",
                      ["channel", "required_factors", "granted_scope"],
                      VERIFICATION_POLICIES))

    out.append(insert("addon_offers",
                      ["offer_id", "plan_id", "data_gigabytes", "price", "currency",
                       "billing_timing", "effective_timing", "expires_at",
                       "requires_line_status", "requires_autopay", "withdrawn"],
                      [(oid, plan, money(gb), money(price), "USD", billing,
                        effective, expires + OFFSET, line_status, autopay, withdrawn)
                       for (oid, plan, gb, price, billing, effective, expires,
                            line_status, autopay, withdrawn) in ADDON_OFFERS]))

    out.append(insert("tool_clock", ["tool_name", "call_index", "offset_seconds"],
                      TOOL_CLOCK))
    out.append(insert("tool_clock_cursor",
                      ["tool_name", "calls_served", "default_step_seconds"],
                      [(name, 0, 30) for name in TOOL_NAMES]))
    out.append(insert("tool_access_requirements", ["tool_name", "required_scope"],
                      TOOL_ACCESS))

    out.append(insert("id_allocator",
                      ["entity_type", "scope", "next_value", "template"],
                      [("specialist_transfer", "", 1, "specialist-transfer-{n:04d}")]))

    out.append("COMMIT;\n")
    return "".join(out)


# ---------------------------------------------------------------------------
# population
# ---------------------------------------------------------------------------

FIRST_NAMES = [
    "Marcus", "Dana", "Priya", "Andre", "Kelsey", "Rosa", "Tomas", "Nadia",
    "Grant", "Imani", "Victor", "Leah", "Owen", "Farah", "Desmond", "Yuki",
    "Camila", "Errol", "Bianca", "Hugo", "Sana", "Elise", "Rahul", "Noor",
    "Trevor", "Anya", "Jonah", "Celia", "Malik", "Renata", "Kwame", "Ingrid",
    "Silas", "Lorna", "Petra", "Devon", "Aiko", "Bram", "Colette", "Oscar",
]

# Reed recurs deliberately: a lookup that leans on the surname alone must not
# resolve to one row.
LAST_NAMES = [
    "Reed", "Reed", "Reeder", "Whitfield", "Okonkwo", "Delgado", "Novak",
    "Bergstrom", "Haddad", "Lindqvist", "Moreau", "Ferraro", "Nakamura",
    "Oyelaran", "Vasquez", "Kaur", "Brennan", "Sorensen", "Achebe", "Marchetti",
    "Kovacs", "Ilyin", "Santoro", "Abbasi", "Fontaine", "Reyes", "Thorne",
    "Mbeki", "Duarte", "Castellanos", "Okafor",
]

DEVICE_MODELS = [
    ("Google", "Pixel 8"), ("Google", "Pixel 7a"), ("Apple", "iPhone 15"),
    ("Apple", "iPhone 14"), ("Apple", "iPhone 13 mini"), ("Samsung", "Galaxy S24"),
    ("Samsung", "Galaxy S23 FE"), ("Samsung", "Galaxy A54"), ("Motorola", "Edge 50"),
    ("OnePlus", "12R"), ("Nothing", "Phone 2a"), ("Apple", "iPhone SE"),
]

APPS = ["StreamBox", "CloudPhotos", "WorkMail", "MapRoute", "PodCatcher",
        "SocialFeed", "GameHub", "NewsWire"]

# Deliberate distractors, declared rather than drawn, so a probe against any of
# them keeps returning the same thing.
#
# customer_id, slug, full name, dob, account status, autopay, identity hold
GUARANTEED_CUSTOMERS = [
    # Shares the target's surname, and holds a line whose last four digits are a
    # near miss for the target's.
    ("customer-marcus-reed", "marcus-reed", "Marcus Reed", "1984-02-09",
     "active", True, None),
    # Same full name as the target, different date of birth: the factor that
    # actually discriminates.
    ("6dcb2039-012b-4723-a256-13bb7b6467c2-1978", "benjamin-reed-1978", "Benjamin Reed",
     "1978-05-30", "active", False, None),
    ("customer-dana-reed", "dana-reed", "Dana Reed", "1996-09-01",
     "active", False, None),
    # A duplicate account pair created during a store migration: same name, same
    # date of birth, same mobile number on both records, so a lookup on the full
    # factor set legitimately reports "multiple".
    ("customer-marisol-okafor-a", "marisol-okafor-a", "Marisol Okafor",
     "1990-04-17", "active", False, None),
    ("customer-marisol-okafor-b", "marisol-okafor-b", "Marisol Okafor",
     "1990-04-17", "active", False, None),
    # Holds make verification inconclusive even when every factor matches.
    ("customer-oscar-lindqvist", "oscar-lindqvist", "Oscar Lindqvist",
     "1971-12-03", "active", False, "security_review"),
    ("customer-nadia-brennan", "nadia-brennan", "Nadia Brennan",
     "1988-07-25", "active", True, "fraud_review"),
    # A suspended account: verification fails on account state rather than on
    # the factors.
    ("customer-tomas-delgado", "tomas-delgado", "Tomas Delgado",
     "1965-03-11", "suspended", False, None),
    ("customer-imani-achebe", "imani-achebe", "Imani Achebe", "1999-11-08",
     "active", False, None),
]

# line_id, customer_id, number suffix, status, plan, primary, autopay,
# ported_out
GUARANTEED_LINES = [
    ("line-4045550431-a", "customer-marisol-okafor-a", "0431", "active",
     "unlimited-plus", True, False, None),
    ("line-4045550431-b", "customer-marisol-okafor-b", "0431", "active",
     "flex-15", True, False, None),
    # Suspended on a plan that does have a live offer, so an offer read returns
    # the offer marked ineligible rather than returning nothing.
    ("line-4045550612", "customer-marcus-reed", "0612", "suspended",
     "unlimited-plus", True, False, None),
    # Ported out to another carrier.
    ("line-4045550777", "customer-imani-achebe", "0777", "disconnected",
     "flex-5", True, False, "2026-07-19T11:04:00"),
    # Prepaid: the plan forbids add-ons at all.
    ("line-4045550888", "customer-imani-achebe", "0888", "active",
     "prepaid-basic", False, False, None),
]


def build_population() -> str:
    rng = random.Random(RNG_SEED + 1)
    out = [
        "-- Customers, cycles, bills, lines, devices, usage, and history.\n"
        "-- Generated by environment/gen_seed.py; do not edit by hand.\n"
        "-- The scenario's own customer, line, cycle, bill, and samples are in\n"
        "-- 004_scenario.sql.\n\n"
        "BEGIN;\n\n"
    ]

    plan_ids = [p[0] for p in PLANS]
    offers_by_plan: dict[str, list[tuple]] = {}
    for (oid, plan, gb, price, billing, effective, _exp, _ls, _ap, withdrawn) in ADDON_OFFERS:
        if not withdrawn:
            offers_by_plan.setdefault(plan, []).append((oid, gb, price, billing, effective))

    # ---- customers ------------------------------------------------------

    customers: list[tuple] = []
    used_identities: set[tuple[str, str]] = {("benjamin reed", "1991-11-22")}
    used_slugs: set[str] = set()

    for (cid, slug, name, dob, status, autopay, hold) in GUARANTEED_CUSTOMERS:
        customers.append((cid, slug, name, dob, status, autopay, hold))
        used_identities.add((name.lower(), dob))
        used_slugs.add(slug)

    for i in range(96):
        first = rng.choice(FIRST_NAMES)
        last = rng.choice(LAST_NAMES)
        name = f"{first} {last}"
        dob = dt.date(rng.randint(1942, 2006), rng.randint(1, 12),
                      rng.randint(1, 28)).isoformat()
        slug = f"{first.lower()}-{last.lower()}-{i:03d}"
        if (name.lower(), dob) in used_identities or slug in used_slugs:
            continue
        used_identities.add((name.lower(), dob))
        used_slugs.add(slug)
        customers.append((
            f"customer-{slug}", slug, name, dob,
            "active" if rng.random() > 0.06 else rng.choice(["suspended", "closed"]),
            rng.random() > 0.55,
            "port_protection" if rng.random() > 0.96 else None,
        ))

    out.append(insert("customers",
                      ["customer_id", "slug", "full_name", "date_of_birth",
                       "account_status", "autopay_enabled", "identity_hold"],
                      customers))

    # ---- cycles and bills -----------------------------------------------

    # Five consecutive monthly cycles per customer, the last of which contains
    # the scenario clock. Cycles and bills are separate tables because a cycle
    # exists whether or not it has been billed: the current cycle's usage is
    # attributable long before its bill leaves the open state.
    cycles: list[tuple] = []
    bills: list[tuple] = []
    charges: list[tuple] = []
    current_cycle: dict[str, str] = {}
    current_bill: dict[str, str] = {}
    cycle_bounds: dict[str, tuple[dt.datetime, dt.datetime]] = {}
    cycles_by_customer: dict[str, list[str]] = {}

    plan_price = {p[0]: p[4] for p in PLANS}

    for (cid, slug, _name, _dob, status, _autopay, _hold) in customers:
        bill_day = rng.randint(1, 28)
        anchor = (dt.date(2026, 8, bill_day) if bill_day <= 27
                  else dt.date(2026, 7, bill_day))
        for back in range(4, -1, -1):
            start = add_months(anchor, -back)
            end = add_months(anchor, -back + 1)
            cycle_id = f"cycle-{slug}-{start.isoformat()}"
            is_current = back == 0
            cycles.append((cycle_id, cid,
                           stamp(dt.datetime.combine(start, dt.time())),
                           stamp(dt.datetime.combine(end, dt.time())),
                           is_current))
            cycle_bounds[cycle_id] = (dt.datetime.combine(start, dt.time()),
                                      dt.datetime.combine(end, dt.time()))
            cycles_by_customer.setdefault(cid, []).append(cycle_id)
            if is_current:
                current_cycle[cid] = cycle_id

            bill_id = f"bill-{slug}-{start.isoformat()}"
            if is_current:
                bill_status, issued, due = "open", None, stamp(
                    dt.datetime.combine(add_months(end, 0), dt.time()) +
                    dt.timedelta(days=15))
                current_bill[cid] = bill_id
            else:
                bill_status = rng.choice(["paid", "paid", "paid", "paid",
                                          "issued", "overdue"])
                issued = stamp(dt.datetime.combine(end, dt.time()))
                due = stamp(dt.datetime.combine(end, dt.time()) + dt.timedelta(days=15))
            bills.append((bill_id, cycle_id, cid, bill_status, "USD", issued, due))

            base = plan_price[rng.choice(plan_ids)]
            charges.append((f"charge-{bill_id}-plan", bill_id, "recurring_plan",
                            "Monthly plan charge", money(base), "USD", "next_bill"))
            charges.append((f"charge-{bill_id}-tax", bill_id, "tax",
                            "Federal and state surcharges",
                            money(round(base * 0.08, 2)), "USD", "next_bill"))
            # Overage lines exist only where the plan bills overage, so an
            # overage read on a speed-reduced plan reports zero from an empty
            # sum rather than from a stored zero.
            if not is_current and rng.random() > 0.82:
                charges.append((f"charge-{bill_id}-overage", bill_id, "overage",
                                "Data overage", money(rng.choice([5.0, 10.0, 15.0, 20.0])),
                                "USD", "next_bill"))

    out.append(insert("billing_cycles",
                      ["billing_cycle_id", "customer_id", "cycle_start",
                       "cycle_end", "is_current"], cycles))
    out.append(insert("bills",
                      ["bill_id", "billing_cycle_id", "customer_id", "status",
                       "currency", "issued_at", "due_at"], bills))
    out.append(insert("bill_charges",
                      ["charge_id", "bill_id", "kind", "description", "amount",
                       "currency", "billing_timing"], charges))

    # ---- lines and devices ----------------------------------------------

    suffix_pool = [f"{n:04d}" for n in range(100, 9999)
                   if f"{n:04d}" not in RESERVED_NUMBERS]
    rng.shuffle(suffix_pool)
    suffixes = iter(suffix_pool)

    lines: list[tuple] = []
    devices: list[tuple] = []
    line_plan: dict[str, str] = {}
    line_customer: dict[str, str] = {}
    lines_by_customer: dict[str, list[str]] = {}

    def add_line(line_id, cid, suffix, status, plan, primary, autopay, ported):
        lines.append((line_id, cid, f"404-555-{suffix}", status, plan,
                      current_cycle[cid], primary, autopay, "carrier_metering",
                      (SCENARIO_NOW.date() - dt.timedelta(days=rng.randint(90, 1500))
                       ).isoformat(),
                      (ported + OFFSET) if ported else None))
        line_plan[line_id] = plan
        line_customer[line_id] = cid
        lines_by_customer.setdefault(cid, []).append(line_id)

    for spec in GUARANTEED_LINES:
        add_line(*spec)

    for (cid, _slug, _name, _dob, _status, autopay, _hold) in customers:
        already = len(lines_by_customer.get(cid, []))
        for k in range(rng.choice([1, 2, 2, 3, 3, 4, 4])):
            suffix = next(suffixes)
            add_line(f"line-404555{suffix}", cid, suffix,
                     rng.choice(["active"] * 8 + ["suspended", "pending"]),
                     rng.choice(plan_ids), already == 0 and k == 0,
                     autopay and rng.random() > 0.3, None)

    for index, line in enumerate(lines):
        line_id = line[0]
        if rng.random() > 0.12:  # some lines are bring-your-own with no record
            manufacturer, model = rng.choice(DEVICE_MODELS)
            devices.append((
                f"device-{line_id.replace('line-', '')}-{index:04d}", line_id,
                manufacturer, model,
                rng.choice(["active"] * 9 + ["pending", "deprovisioned"]),
                f"{rng.randint(1000, 9999)}",
                stamp(SCENARIO_NOW - dt.timedelta(days=rng.randint(30, 900))),
            ))

    out.append(insert("lines",
                      ["line_id", "customer_id", "mobile_number", "status",
                       "plan_id", "billing_cycle_id", "is_primary",
                       "autopay_enabled", "metering_source", "activated_on",
                       "ported_out_at"], lines))
    out.append(insert("devices",
                      ["device_id", "line_id", "manufacturer", "model",
                       "provisioning_status", "imei_suffix", "activated_at"],
                      devices))

    # ---- usage samples --------------------------------------------------

    # One metered interval per line in the current cycle, and for some lines a
    # second in an earlier cycle. The earlier ones are there so a remaining
    # balance that summed every sample for a line, rather than every sample in
    # the line's current cycle, would come out wrong.
    samples: list[tuple] = []
    for line in lines:
        line_id, cid = line[0], line[1]
        for index, cycle_id in enumerate(reversed(cycles_by_customer[cid])):
            if index > 0 and rng.random() > 0.45:
                continue
            if index > 2:
                break
            start_day, _end_day = cycle_bounds[cycle_id]
            begin = start_day + dt.timedelta(days=rng.randint(1, 20), hours=10)
            samples.append((
                f"sample-{line_id}-{cycle_id[-10:]}-{index}", line_id, cycle_id,
                stamp(begin), stamp(begin + dt.timedelta(hours=rng.randint(2, 9))),
                money(rng.choice([0.4, 0.9, 1.6, 2.3, 3.8, 5.5, 7.2, 9.1])),
                "carrier_metering",
            ))

    out.append(insert("usage_samples",
                      ["sample_id", "line_id", "billing_cycle_id", "window_start",
                       "window_end", "gigabytes", "measurement_source"], samples))

    # ---- history --------------------------------------------------------

    transactions: list[tuple] = []
    for line in lines:
        line_id, cid = line[0], line[1]
        plan = line_plan[line_id]
        pool = offers_by_plan.get(plan)
        if not pool or rng.random() > 0.16:
            continue
        offer_id, gb, price, _billing, _effective = rng.choice(pool)
        cycle_id = current_cycle[cid]
        moment = SCENARIO_NOW - dt.timedelta(days=rng.randint(1, 12),
                                             hours=rng.randint(0, 20))
        status = rng.choice(["active"] * 6 + ["reversed", "failed"])
        transactions.append((
            f"addon-transaction-{line_id.replace('line-', '')}-prior", line_id,
            offer_id, cycle_id, current_bill[cid], status, money(gb), money(price),
            "USD", stamp(moment), stamp(moment), True,
        ))

    out.append(insert("addon_transactions",
                      ["transaction_id", "line_id", "offer_id", "billing_cycle_id",
                       "bill_id", "status", "data_gigabytes", "charged_price",
                       "currency", "effective_at", "effective_at_display",
                       "authorized_by_customer"], transactions))

    # Verifications from earlier contacts, including ones that did not succeed.
    # A failed record grants nothing, which is why access_scope is empty rather
    # than the channel's scope.
    verifications: list[tuple] = []
    for (cid, slug, _name, _dob, _status, _autopay, hold) in customers:
        if rng.random() > 0.45:
            continue
        channel = rng.choice(["support", "support", "ivr", "retail_store"])
        granted = dict((c, s) for (c, _r, s) in VERIFICATION_POLICIES)[channel]
        factors = dict((c, r) for (c, r, _s) in VERIFICATION_POLICIES)[channel]
        if hold:
            status, matched, scope = "inconclusive", factors, []
        elif rng.random() > 0.8:
            status, matched, scope = "failed", factors[:1], []
        else:
            status, matched, scope = "verified", factors, granted
        moment = SCENARIO_NOW - dt.timedelta(days=rng.randint(6, 120),
                                             hours=rng.randint(1, 20))
        verifications.append((f"verification-{slug}-{channel}", cid, channel,
                              status, matched, scope, stamp(moment), stamp(moment)))

    out.append(insert("identity_verifications",
                      ["verification_id", "customer_id", "channel", "status",
                       "matched_factors", "access_scope", "verified_at",
                       "verified_at_display"], verifications))

    # What callers said about their handsets on earlier contacts. No tool writes
    # here, so nothing a caller reads off a screen during this call can end up
    # in the carrier's records looking like telemetry.
    reports: list[tuple] = []
    for line in lines:
        line_id = line[0]
        if rng.random() > 0.1:
            continue
        moment = SCENARIO_NOW - dt.timedelta(days=rng.randint(20, 200))
        kind = rng.choice(["app_usage_screen", "app_setting", "device_setting",
                           "speed_test"])
        reports.append((
            f"report-{line_id.replace('line-', '')}", line_id, stamp(moment),
            f"the {moment.strftime('%B')} {moment.day} call", kind, "support",
            rng.choice(APPS) if kind in ("app_usage_screen", "app_setting") else None,
            money(rng.choice([0.2, 0.6, 1.1, 2.4])) if kind == "app_usage_screen" else None,
            {"app_setting": "download_over_cellular",
             "device_setting": "data_saver",
             "speed_test": "downlink_mbps"}.get(kind),
            {"app_setting": rng.choice(["on", "off"]),
             "device_setting": rng.choice(["on", "off"]),
             "speed_test": str(rng.randint(8, 120))}.get(kind),
        ))

    out.append(insert("customer_reported_device_state",
                      ["report_id", "line_id", "reported_at", "reported_at_display",
                       "report_kind", "channel", "app_name", "reported_gigabytes",
                       "setting_name", "setting_value"], reports))

    # ---- allocators -----------------------------------------------------

    # One add-on transaction allocator per line, so a purchase anywhere in the
    # estate issues an identifier from the database rather than failing for want
    # of a counter. The template is the customer stem; the handler appends the
    # offer's size, and the issued ordinal only when the line has bought before.
    slug_by_customer = {c[0]: c[1] for c in customers}
    allocators = [("addon_transaction", line[0], 1,
                   f"addon-transaction-{slug_by_customer[line[1]]}")
                  for line in lines]
    out.append(insert("id_allocator",
                      ["entity_type", "scope", "next_value", "template"],
                      allocators))

    out.append("COMMIT;\n")

    counts = {
        "customers": len(customers), "billing_cycles": len(cycles),
        "bills": len(bills), "bill_charges": len(charges), "lines": len(lines),
        "devices": len(devices), "usage_samples": len(samples),
        "addon_transactions": len(transactions),
        "identity_verifications": len(verifications),
        "customer_reported_device_state": len(reports),
        "id_allocator": len(allocators),
    }
    print("population:", ", ".join(f"{k}={v}" for k, v in counts.items()))
    return "".join(out)


def main() -> None:
    os.makedirs(SQL, exist_ok=True)
    reference = build_reference()
    population = build_population()
    with open(os.path.join(SQL, "002_reference.sql"), "w") as fh:
        fh.write(reference)
    with open(os.path.join(SQL, "003_population.sql"), "w") as fh:
        fh.write(population)
    print(f"wrote 002_reference.sql ({len(reference)} bytes) and "
          f"003_population.sql ({len(population)} bytes)")


if __name__ == "__main__":
    main()
