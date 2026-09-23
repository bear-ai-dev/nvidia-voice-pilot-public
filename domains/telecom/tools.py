"""Telecom (ClearWave Mobile) tools over the conversation's JSON database.

Ported from the PostgreSQL tool server in PR #17. Each tool is (db, args) -> result
and may mutate db in place; see env/toolkit.py for the row layout and helpers.

Handlers hold domain logic only. Every identifier, price, allowance, timestamp,
and eligibility decision that appears in a result is read or computed from the
database, never from wall time and never generated at random.

The load-bearing case is high-speed data. No handler stores or reads a
"remaining" figure: the plan carries the allowance, `usage_samples` carry
consumption, `addon_transactions` carry purchased increments, and `_balance`
sums them the way the original line_high_speed_balance view did. That is why
buying an add-on changes what a later usage read reports, and why buying a
second one changes it again.

Timestamps are stored as UTC ISO strings, as the database returned them, and
rendered in the scenario's zone with an explicit offset, as the original
scenario_iso() SQL function did. NUMERIC arithmetic is done in Decimal.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from toolkit import (NotFound, Refusal, allocate_id, as_float, as_list_always, compact,
                     first, insert, rows, scenario_id, scenario_value)

KEY_COLUMNS = {
    "addon_offers": [
        "offer_id"
    ],
    "addon_transactions": [
        "transaction_id"
    ],
    "bill_charges": [
        "charge_id"
    ],
    "billing_cycles": [
        "billing_cycle_id"
    ],
    "bills": [
        "bill_id"
    ],
    "customer_reported_device_state": [
        "report_id"
    ],
    "customers": [
        "customer_id"
    ],
    "devices": [
        "device_id"
    ],
    "id_allocator": [
        "entity_type",
        "scope"
    ],
    "identity_verifications": [
        "verification_id"
    ],
    "lines": [
        "line_id"
    ],
    "measurement_sources": [
        "source_id"
    ],
    "plans": [
        "plan_id"
    ],
    "scenario": [
        "key"
    ],
    "specialist_transfers": [
        "transfer_id"
    ],
    "tool_access_requirements": [
        "tool_name"
    ],
    "tool_clock": [
        "tool_name",
        "call_index"
    ],
    "tool_clock_cursor": [
        "tool_name"
    ],
    "usage_samples": [
        "sample_id"
    ],
    "verification_policies": [
        "channel"
    ]
}

# Tables and columns that read tools write as a side effect. Every timed read
# advances its tool's call counter in tool_clock_cursor so that a second read is
# stamped later than the first; that is bookkeeping about the call, not a change
# to anyone's account, so a state comparison leaves it out.
READ_SIDE_EFFECTS: dict[str, list[str] | str] = {
    "tool_clock_cursor": "*",
}

UTC = timezone.utc

# to_char's FMMonth, which does not depend on the process locale.
MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December",
)

CENT = Decimal("0.01")

FRACTIONAL_SECONDS = re.compile(r"\.(\d+)")


# ---------------------------------------------------------------------------
# SQL value semantics
# ---------------------------------------------------------------------------


def _instant(value: str) -> datetime:
    """A timestamptz from its RFC 3339 text, as `::timestamptz` reads it.

    The database takes a lower-case "t" or "z" and any number of fractional
    digits, rounding them to the microsecond; fromisoformat takes neither and
    truncates, so both are normalized first.
    """
    text = value.upper()
    micros = 0
    fraction = FRACTIONAL_SECONDS.search(text)
    if fraction:
        text = text[:fraction.start()] + text[fraction.end():]
        micros = int((Decimal("0." + fraction.group(1)) * 10**6)
                     .to_integral_value(ROUND_HALF_EVEN))
    return datetime.fromisoformat(text) + timedelta(microseconds=micros)


def _stored(instant: datetime) -> str:
    """A timestamptz as the database hands it back: UTC with an explicit offset."""
    return instant.astimezone(UTC).isoformat()


def _decimal(value) -> Decimal:
    """A NUMERIC column as the exact decimal the database holds."""
    return Decimal(str(value))


def _numeric_12_2(value: Decimal) -> Decimal:
    """`::numeric(12, 2)`, which rounds half away from zero."""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _sql_lower(text: str) -> str:
    """SQL lower(): a per-character mapping, never changing the length.

    Python's str.lower() applies full Unicode case mapping, under which one
    character (U+0130, capital I with dot above) lowers to two. The database
    lowers it to a plain "i".
    """
    return "".join(ch.lower() if len(ch.lower()) == 1 else "i" for ch in text)


def _spoken_date(iso_date: str) -> str:
    """`to_char(date, 'FMMonth FMDD, YYYY')`: the date the way a caller says it."""
    day = date.fromisoformat(iso_date)
    return f"{MONTH_NAMES[day.month - 1]} {day.day}, {day.year:04d}"


# ---------------------------------------------------------------------------
# scenario clock and access gate
# ---------------------------------------------------------------------------


def _scenario_now(db) -> datetime:
    """scenario_now(): the conversation's clock, in place of now()."""
    return _instant(scenario_value(db, "scenario_time"))


def _scenario_zone(db) -> ZoneInfo:
    return ZoneInfo(scenario_value(db, "timezone"))


def _scenario_iso(db, instant: datetime) -> str:
    """scenario_iso(): local wall time in the scenario's zone with an explicit
    numeric offset, to the whole second.

    The offset is derived from the zone rather than pasted in, so an instant on
    the other side of a DST boundary still prints correctly.
    """
    local = instant.astimezone(_scenario_zone(db))
    offset = int(local.utcoffset().total_seconds())
    sign = "-" if offset < 0 else "+"
    hours, minutes = abs(offset) // 3600, abs(offset) % 3600 // 60
    return f"{local.strftime('%Y-%m-%dT%H:%M:%S')}{sign}{hours:02d}:{minutes:02d}"


def _intake_channel(db) -> str:
    """The channel this call arrived on, which selects the verification policy."""
    return scenario_value(db, "intake_channel")


def _tool_time(db, tool_name: str) -> tuple[datetime, str]:
    """Advance the tool's call counter and return the instant it reports.

    Recorded results carry a different timestamp per call, and a backend would
    take those from its own clock. This environment has no clock, so the elapsed
    offsets observed on the call live in `tool_clock`, keyed by tool and
    invocation ordinal. Past the recorded offsets the cursor's step keeps the
    clock moving forward, so an unrecorded second call is stamped later than the
    first rather than identically.

    Returns the typed instant and the string the tool emits for it.
    """
    cursor = db["tool_clock_cursor"].get(tool_name)
    if cursor is None:
        raise KeyError(f"no clock cursor for tool {tool_name!r}")
    cursor["calls_served"] += 1

    index = cursor["calls_served"]
    recorded = db["tool_clock"].get(f"{tool_name}|{index}")
    if recorded is not None:
        offset = recorded["offset_seconds"]
    else:
        clock = rows(db, "tool_clock", tool_name=tool_name)
        last_index = max((row["call_index"] for row in clock), default=0)
        last_offset = max((row["offset_seconds"] for row in clock), default=0)
        offset = last_offset + cursor["default_step_seconds"] * (index - last_index)

    stamped = _scenario_now(db) + timedelta(seconds=offset)
    return stamped, _scenario_iso(db, stamped)


def _required_scope(db, tool_name: str) -> str | None:
    requirement = db["tool_access_requirements"].get(tool_name)
    return None if requirement is None else requirement["required_scope"]


def _check_verification(db, tool_name: str, verification_id: str, customer_id: str):
    """Refuse a protected read unless a verification authorizes it.

    The policy is explicit that a resolved record is not an authorization, so the
    gate reads the verification's own `access_scope` rather than its status: a
    record that failed grants nothing because its scope is empty, and a channel
    whose tier does not cover billing cannot reach a bill even after a successful
    verification.
    """
    scope = _required_scope(db, tool_name)
    if scope is None:
        return None

    record = db["identity_verifications"].get(verification_id)
    if record is None:
        raise Refusal(f"no identity verification named {verification_id!r}")
    if record["customer_id"] != customer_id:
        raise Refusal(
            f"verification {verification_id!r} was issued for another customer")
    if record["status"] != "verified":
        raise Refusal(
            f"verification {verification_id!r} is {record['status']}, not verified")
    if scope not in as_list_always(record["access_scope"]):
        raise Refusal(
            f"verification {verification_id!r} does not authorize {scope!r} access")
    return record


def _check_customer_verified(db, tool_name: str, customer_id: str) -> None:
    """Gate a mutation that takes no verification argument.

    `add_data_addon` carries only a line, an offer, and an authorization flag, so
    there is no verification id to cite. The requirement still holds, so it is
    checked against state: the account must already hold a verified record whose
    scope covers the mutation.
    """
    scope = _required_scope(db, tool_name)
    if scope is None:
        return
    verified = rows(db, "identity_verifications", customer_id=customer_id, status="verified")
    if not any(scope in as_list_always(record["access_scope"]) for record in verified):
        raise Refusal(
            f"account {customer_id!r} holds no verified identity record "
            f"authorizing {scope!r} access")


# ---------------------------------------------------------------------------
# shared lookups
# ---------------------------------------------------------------------------


def _line(db, line_id: str) -> dict:
    """The line joined to the plan terms that decide what it may buy."""
    line = db["lines"].get(line_id)
    plan = None if line is None else db["plans"].get(line["plan_id"])
    if line is None or plan is None:
        raise NotFound(f"unknown line {line_id!r}")
    return dict(line,
                addons_allowed=plan["addons_allowed"],
                after_high_speed_allowance=plan["after_high_speed_allowance"])


def _balance(db, line: dict) -> dict:
    """The line's high-speed position in its current cycle, as an aggregate.

    This is the line_high_speed_balance view: the plan's allowance, plus active
    add-ons, less metered consumption, all scoped to the line's current cycle and
    floored at zero. Nothing stores the remaining figure.
    """
    cycle = line["billing_cycle_id"]
    allowance = _decimal(db["plans"][line["plan_id"]]["high_speed_allowance_gigabytes"])
    consumed = sum(
        (_decimal(sample["gigabytes"])
         for sample in rows(db, "usage_samples", line_id=line["line_id"],
                            billing_cycle_id=cycle)),
        Decimal(0))
    added = sum(
        (_decimal(addon["data_gigabytes"])
         for addon in rows(db, "addon_transactions", line_id=line["line_id"],
                           billing_cycle_id=cycle, status="active")),
        Decimal(0))
    return {
        "allowance_gigabytes": allowance,
        "consumed_gigabytes": _numeric_12_2(consumed),
        "added_gigabytes": _numeric_12_2(added),
        "remaining_gigabytes": _numeric_12_2(max(allowance + added - consumed, Decimal(0))),
    }


def _overdue_bills(db, customer_id: str) -> int:
    return len(rows(db, "bills", customer_id=customer_id, status="overdue"))


def _eligibility(line: dict, offer: dict, overdue: int) -> str:
    """Whether this line may buy this offer, from the line and the offer's terms.

    Computed per read rather than stored on the offer, because the same catalog
    row is eligible for one line and not for another: a suspended line, a plan
    that carries no add-ons, an autopay-only price, or an unpaid balance each
    make the offer unusable while leaving it a current offer.
    """
    if not line["addons_allowed"]:
        return "ineligible"
    if line["status"] != offer["requires_line_status"]:
        return "ineligible"
    if offer["requires_autopay"] and not line["autopay_enabled"]:
        return "ineligible"
    if overdue:
        return "ineligible"
    return "eligible"


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


def lookup_customer(db, args) -> dict:
    channel = _intake_channel(db)
    policy = db["verification_policies"].get(channel)
    if policy is None:
        raise Refusal(f"no verification policy for intake channel {channel!r}")

    # The date of birth arrives the way a caller says it, so it is compared
    # against the stored date rendered in that form rather than parsed.
    full_name = _sql_lower(args["full_name"].strip())
    matched_ids = set()
    for line in rows(db, "lines", mobile_number=args["mobile_number"]):
        customer = db["customers"][line["customer_id"]]
        if (_sql_lower(customer["full_name"]) == full_name
                and _spoken_date(customer["date_of_birth"]) == args["date_of_birth"]):
            matched_ids.add(line["customer_id"])
    matches = sorted(matched_ids)

    # A duplicate account record makes even the full factor set ambiguous, and
    # the registry's contract is to say so rather than to pick one.
    if not matches:
        return {"match": "none"}
    if len(matches) > 1:
        return {"match": "multiple"}

    return {
        "customer_id": matches[0],
        "match": "unique",
        "required_verification_factors": as_list_always(policy["required_factors"]),
    }


def _verification_id(db, customer: dict, channel: str) -> str:
    """The identifier of this caller's verification record on this channel.

    One verification record per caller per channel: re-verifying the same caller
    on the same channel refreshes it rather than accumulating identical records,
    so a caller who already holds one keeps its identifier. A new record takes
    the scenario's seeded identifier while that is free, and otherwise one
    derived from the account stem and the channel.

    The existing record is looked up first on purpose. The PostgreSQL handler
    went straight to the seeded identifier, so the first verification of a
    caller who already held a record on the channel (such as the register's
    other Benjamin Reed) tried to file a second one and failed with a 500 on the
    one-record-per-channel constraint.
    """
    existing = first(db, "identity_verifications",
                     customer_id=customer["customer_id"], channel=channel)
    if existing is not None:
        return existing["verification_id"]
    return (scenario_id(db, "next_identity_verification_id",
                        "identity_verifications", "verification_id",
                        {"customer_id": customer["customer_id"], "channel": channel})
            or f"verification-{customer['slug']}-{channel}")


def verify_customer_identity(db, args) -> dict:
    customer = db["customers"].get(args["customer_id"])
    if customer is None:
        raise NotFound(f"unknown customer {args['customer_id']!r}")

    channel = _intake_channel(db)
    policy = db["verification_policies"].get(channel)
    if policy is None:
        raise Refusal(f"no verification policy for intake channel {channel!r}")

    on_account = first(db, "lines", customer_id=customer["customer_id"],
                       mobile_number=args["mobile_number"])
    outcomes = {
        "mobile_number": on_account is not None,
        "full_name": customer["full_name"].strip().lower()
        == args["full_name"].strip().lower(),
        "date_of_birth": _spoken_date(customer["date_of_birth"]) == args["date_of_birth"],
    }

    required = as_list_always(policy["required_factors"])
    matched = [factor for factor in required if outcomes.get(factor)]

    # A hold is not a mismatch: the factors were right and the carrier still will
    # not open the account, which is the case the policy's retry-or-transfer
    # branch exists for. A closed or suspended account is a hard failure.
    if len(matched) < len(required):
        status = "failed"
    elif customer["identity_hold"]:
        status = "inconclusive"
    elif customer["account_status"] != "active":
        status = "failed"
    else:
        status = "verified"

    scope = as_list_always(policy["granted_scope"]) if status == "verified" else []
    verification_id = _verification_id(db, customer, channel)
    verified_at, verified_at_display = _tool_time(db, "verify_customer_identity")

    # An upsert on the identifier: a refresh keeps the record's customer and
    # channel and replaces the outcome.
    outcome = {
        "status": status,
        "matched_factors": matched,
        "access_scope": scope,
        "verified_at": _stored(verified_at),
        "verified_at_display": verified_at_display,
    }
    record = db["identity_verifications"].get(verification_id)
    if record is not None:
        record.update(outcome)
    else:
        insert(db, "identity_verifications",
               dict(verification_id=verification_id,
                    customer_id=customer["customer_id"], channel=channel, **outcome),
               KEY_COLUMNS)

    return {
        "verification_id": verification_id,
        "status": status,
        "matched_factors": matched,
        "access_scope": scope,
        "verified_at": verified_at_display,
    }


def get_customer_account(db, args) -> dict:
    customer_id = args["customer_id"]
    if db["customers"].get(customer_id) is None:
        raise NotFound(f"unknown customer {customer_id!r}")
    _check_verification(db, "get_customer_account", args["verification_id"],
                        customer_id)

    sections = set(args["include"])
    accounts_lines = sorted(rows(db, "lines", customer_id=customer_id),
                            key=lambda line: line["line_id"])
    lines = devices = plans = None

    if "lines" in sections:
        lines = [
            {
                "line_id": line["line_id"],
                "masked_mobile_number": line["masked_mobile_number"],
                "status": line["status"],
                "billing_cycle_id": line["billing_cycle_id"],
            }
            for line in accounts_lines
        ]

    if "devices" in sections:
        devices = [
            {
                "device_id": device["device_id"],
                "model": device["model"],
                "line_id": device["line_id"],
                "provisioning_status": device["provisioning_status"],
            }
            for device in sorted(
                (device for line in accounts_lines
                 for device in rows(db, "devices", line_id=line["line_id"])),
                key=lambda device: (device["line_id"], device["device_id"]))
        ]

    if "plans" in sections:
        # One entry per line, not per distinct plan: the registry's plan section
        # says which plan each line is on.
        plans = [
            {"plan_id": line["plan_id"], "name": db["plans"][line["plan_id"]]["name"],
             "line_id": line["line_id"]}
            for line in accounts_lines
        ]

    return compact([
        ("customer_id", customer_id),
        ("lines", lines),
        ("devices", devices),
        ("plans", plans),
    ])


def get_line_data_usage(db, args) -> dict:
    line = _line(db, args["line_id"])
    _check_verification(db, "get_line_data_usage", args["verification_id"],
                        line["customer_id"])

    window = args["window"]
    if window == "custom":
        # The registry makes both bounds required for a custom window; without
        # them there is nothing to measure, so the read is refused rather than
        # silently widened to a default.
        if not args.get("window_start") or not args.get("window_end"):
            raise Refusal("a custom window requires window_start and window_end")
        lo, hi = _instant(args["window_start"]), _instant(args["window_end"])
    elif window == "last_24_hours":
        hi = _scenario_now(db)
        lo = hi - timedelta(hours=24)
    else:
        cycle = db["billing_cycles"][line["billing_cycle_id"]]
        lo, hi = _instant(cycle["cycle_start"]), _instant(cycle["cycle_end"])
    if hi <= lo:
        raise Refusal("window_end must be later than window_start")

    # The requested window is reported as asked, with every metered sample that
    # overlaps it. Where in the window the traffic fell is for the reader of the
    # samples to see, not something the meter summarises.
    samples = sorted(
        (sample for sample in rows(db, "usage_samples", line_id=line["line_id"])
         if _instant(sample["window_end"]) > lo and _instant(sample["window_start"]) < hi),
        key=lambda sample: _instant(sample["window_start"]),
    )
    used = _numeric_12_2(sum((_decimal(s["gigabytes"]) for s in samples), Decimal(0)))
    cycles = sorted({s["billing_cycle_id"] for s in samples})

    source = (min((s["measurement_source"] for s in samples), default=None)
              or line["metering_source"])
    meter = db["measurement_sources"].get(source)
    attribution = None if meter is None else meter["app_attribution_available"]

    # A window that straddles two cycles is not attributable to either, so the
    # line's current cycle is reported instead of an arbitrary one of them.
    cycle_id = cycles[0] if len(cycles) == 1 else line["billing_cycle_id"]

    balance = _balance(db, line)
    as_of = _tool_time(db, "get_line_data_usage")[1]

    return {
        "line_id": line["line_id"],
        "billing_cycle_id": cycle_id,
        "measurement_source": source,
        "window_start": _scenario_iso(db, lo),
        "window_end": _scenario_iso(db, hi),
        "samples": [
            {"start": _scenario_iso(db, _instant(s["window_start"])),
             "end": _scenario_iso(db, _instant(s["window_end"])),
             "gigabytes": as_float(s["gigabytes"])}
            for s in samples
        ],
        "used_gigabytes": as_float(used),
        # Always the current cycle's balance, per the registry: the window says
        # what was consumed, the balance says what is left to consume.
        "remaining_high_speed_gigabytes": as_float(balance["remaining_gigabytes"]),
        "app_attribution_available": attribution,
        "as_of": as_of,
    }


def get_customer_bills(db, args) -> dict:
    customer_id = args["customer_id"]
    if db["customers"].get(customer_id) is None:
        raise NotFound(f"unknown customer {customer_id!r}")
    _check_verification(db, "get_customer_bills", args["verification_id"],
                        customer_id)

    billed = [
        (bill, db["billing_cycles"][bill["billing_cycle_id"]])
        for bill in rows(db, "bills", customer_id=customer_id)
    ]
    if args["status"] == "current":
        chosen = [(bill, cycle) for bill, cycle in billed if cycle["is_current"]]
    else:
        chosen = sorted(((bill, cycle) for bill, cycle in billed if not cycle["is_current"]),
                        key=lambda pair: _instant(pair[1]["cycle_end"]), reverse=True)
    if not chosen:
        raise NotFound(f"customer {customer_id!r} has no {args['status']} bill")
    bill, cycle = chosen[0]

    sections = set(args["include"])
    money_requested = bool(sections & {"charges", "overages"})

    overage = currency = None
    if money_requested:
        # Zero here is an empty sum, not a stored zero: a plan that reduces speed
        # past its allowance never writes an overage line, so there is nothing to
        # add up.
        overage = _numeric_12_2(sum(
            (_decimal(charge["amount"])
             for charge in rows(db, "bill_charges", bill_id=bill["bill_id"], kind="overage")),
            Decimal(0)))
        currency = bill["currency"]

    behaviour = None
    if "plan_behavior" in sections:
        # Post-allowance behaviour belongs to the plan, and the bill is the
        # account's, so it is read from the account's primary line.
        accounts_lines = sorted(rows(db, "lines", customer_id=customer_id),
                                key=lambda line: (not line["is_primary"], line["line_id"]))
        if accounts_lines:
            behaviour = db["plans"][accounts_lines[0]["plan_id"]]["after_high_speed_allowance"]

    # The cycle's bounds are returned as dates; how long until it resets is the
    # reader's arithmetic against the call's own clock.
    cycle_requested = "cycle" in sections
    as_of = _tool_time(db, "get_customer_bills")[1]

    return compact([
        ("bill_id", bill["bill_id"]),
        ("billing_cycle_id", bill["billing_cycle_id"]),
        ("cycle_start", _scenario_iso(db, _instant(cycle["cycle_start"]))
         if cycle_requested else None),
        ("cycle_end", _scenario_iso(db, _instant(cycle["cycle_end"]))
         if cycle_requested else None),
        ("overage_charge", as_float(overage) if money_requested else None),
        ("currency", currency),
        ("after_high_speed_allowance", behaviour),
        ("as_of", as_of),
    ])


def get_data_addon_offers(db, args) -> dict:
    line = _line(db, args["line_id"])
    _check_verification(db, "get_data_addon_offers", args["verification_id"],
                        line["customer_id"])

    overdue = _overdue_bills(db, line["customer_id"])
    # Current means unexpired against the scenario clock and not withdrawn from
    # the catalog. Eligibility is reported per offer rather than filtered on, so
    # an offer this line cannot buy comes back saying so instead of vanishing.
    now = _scenario_now(db)
    offers = sorted(
        (offer for offer in rows(db, "addon_offers", plan_id=line["plan_id"], withdrawn=False)
         if _instant(offer["expires_at"]) > now),
        key=lambda offer: (_decimal(offer["data_gigabytes"]), offer["offer_id"]))
    as_of = _tool_time(db, "get_data_addon_offers")[1]

    return {
        "line_id": line["line_id"],
        "offers": [
            {
                "offer_id": offer["offer_id"],
                "eligibility_status": _eligibility(line, offer, overdue),
                "data_gigabytes": as_float(offer["data_gigabytes"]),
                "price": as_float(offer["price"]),
                "currency": offer["currency"],
                "billing_timing": offer["billing_timing"],
                "effective_timing": offer["effective_timing"],
                "expires_at": _scenario_iso(db, _instant(offer["expires_at"])),
            }
            for offer in offers
        ],
        "as_of": as_of,
    }


# ---------------------------------------------------------------------------
# mutations
# ---------------------------------------------------------------------------


def _allocate_transaction_id(db, line_id: str, gigabytes: float) -> str:
    """Issue the add-on transaction identifier for a purchase on this line.

    The allocator holds the account stem; the size of the add-on completes it,
    which is where `dfdab773-2580-4908-91cf-99a2b4826547` comes from. The issued
    ordinal is appended only from the second purchase onward, so the first
    purchase reads as a name and a repeat cannot collide with it. Written here
    rather than through toolkit.allocate_id because that helper substitutes the
    ordinal unconditionally.

    The stem belongs to the account, not the line, so two lines on one account
    can arrive at the same identifier for the same size of add-on. An issued
    identifier that is already taken is skipped and the next ordinal drawn. The
    PostgreSQL handler did not skip, and the second purchase failed with a 500 on
    the transaction's primary key.
    """
    issued = db["id_allocator"].get(f"addon_transaction|{line_id}")
    if issued is None:
        raise Refusal(f"line {line_id!r} has no add-on transaction allocator")
    stem = f"{issued['template']}-{gigabytes:g}gb"
    while True:
        ordinal = issued["next_value"]
        issued["next_value"] = ordinal + 1
        transaction_id = stem if ordinal == 1 else f"{stem}-{ordinal}"
        if db["addon_transactions"].get(transaction_id) is None:
            return transaction_id


def add_data_addon(db, args) -> dict:
    line = _line(db, args["line_id"])
    _check_customer_verified(db, "add_data_addon", line["customer_id"])

    offer = db["addon_offers"].get(args["offer_id"])
    if offer is None:
        raise NotFound(f"unknown offer {args['offer_id']!r}")

    # Policy order: the product has to be real and buyable for this line before
    # the authorization means anything, and the authorization has to be explicit
    # before anything is charged.
    if offer["plan_id"] != line["plan_id"]:
        raise Refusal(
            f"offer {offer['offer_id']!r} is not offered on plan {line['plan_id']!r}")
    if offer["withdrawn"] or not _instant(offer["expires_at"]) > _scenario_now(db):
        raise Refusal(f"offer {offer['offer_id']!r} is no longer current")

    overdue = _overdue_bills(db, line["customer_id"])
    if _eligibility(line, offer, overdue) != "eligible":
        raise Refusal(
            f"line {line['line_id']!r} is not eligible for offer {offer['offer_id']!r}")
    if not args["customer_authorized"]:
        raise Refusal(
            "the customer has not authorized the data amount, price, currency, "
            "and billing timing")

    # A next-bill charge needs a bill that has not been issued yet. Without one
    # there is nowhere to put the charge, and inventing one would misreport where
    # the customer will see it.
    bill = next(
        (bill for bill in rows(db, "bills", customer_id=line["customer_id"], status="open")
         if db["billing_cycles"][bill["billing_cycle_id"]]["is_current"]),
        None)
    if bill is None:
        raise Refusal(
            f"account {line['customer_id']!r} has no open bill to charge")

    gigabytes = as_float(offer["data_gigabytes"])
    transaction_id = (scenario_id(db, "next_addon_transaction_id",
                                  "addon_transactions", "transaction_id")
                      or _allocate_transaction_id(db, line["line_id"], gigabytes))
    effective_at, effective_at_display = _tool_time(db, "add_data_addon")

    # An add-on that only takes effect next cycle is not usable data yet, so it
    # is recorded as pending and the balance, which counts active rows only, does
    # not pick it up.
    status = "active" if offer["effective_timing"] == "immediate" else "pending"

    insert(db, "addon_transactions", {
        "transaction_id": transaction_id,
        "line_id": line["line_id"],
        "offer_id": offer["offer_id"],
        "billing_cycle_id": line["billing_cycle_id"],
        "bill_id": bill["bill_id"],
        "status": status,
        "data_gigabytes": offer["data_gigabytes"],
        "charged_price": offer["price"],
        "currency": offer["currency"],
        "effective_at": _stored(effective_at),
        "effective_at_display": effective_at_display,
        "authorized_by_customer": True,
    }, KEY_COLUMNS)
    insert(db, "bill_charges", {
        "charge_id": f"charge-{transaction_id}",
        "bill_id": bill["bill_id"],
        "kind": "addon",
        "description": f"Data add-on {gigabytes:g} GB",
        "amount": offer["price"],
        "currency": offer["currency"],
        "billing_timing": offer["billing_timing"],
    }, KEY_COLUMNS)

    # Read back through the same aggregate the usage tool uses, after the insert,
    # so the balance reported here and the balance a later usage read reports
    # cannot disagree.
    balance = _balance(db, line)

    return {
        "transaction_id": transaction_id,
        "status": status,
        "offer_id": offer["offer_id"],
        "effective_at": effective_at_display,
        "bill_reference": bill["bill_id"],
        "charged_price": as_float(offer["price"]),
        "currency": offer["currency"],
        "added_high_speed_gigabytes": gigabytes,
        "remaining_high_speed_gigabytes": as_float(balance["remaining_gigabytes"]),
    }


def transfer_to_specialist(db, args) -> dict:
    transfer_id = allocate_id(db, "specialist_transfer")
    created_at, created_at_display = _tool_time(db, "transfer_to_specialist")
    insert(db, "specialist_transfers", {
        "transfer_id": transfer_id,
        "reason": args["reason"],
        "summary": args["summary"],
        "status": "accepted",
        "created_at": _stored(created_at),
        "created_at_display": created_at_display,
    }, KEY_COLUMNS)
    return {"status": "accepted", "transfer_id": transfer_id}


TOOLS = {
    "lookup_customer": lookup_customer,
    "verify_customer_identity": verify_customer_identity,
    "get_customer_account": get_customer_account,
    "get_line_data_usage": get_line_data_usage,
    "get_customer_bills": get_customer_bills,
    "get_data_addon_offers": get_data_addon_offers,
    "add_data_addon": add_data_addon,
    "transfer_to_specialist": transfer_to_specialist,
}

# Tools that change the carrier's records. Reads are free - an agent may look at
# anything as often as it likes - so the distinction has to be stated somewhere,
# and the handlers are where it is known. What counts is whether the tool changes
# the world the caller cares about, not whether it happens to touch a table:
# get_line_data_usage, get_customer_bills and get_data_addon_offers each advance
# tool_clock_cursor so that a second call is stamped later than the first, and
# that is bookkeeping (READ_SIDE_EFFECTS), not a change to the customer's account.
# verify_customer_identity is here because the record it files is what
# authorizes account access, and its scope is what every protected read is
# checked against.
WRITE_TOOLS = {
    "verify_customer_identity",
    "add_data_addon",
    "transfer_to_specialist",
}
