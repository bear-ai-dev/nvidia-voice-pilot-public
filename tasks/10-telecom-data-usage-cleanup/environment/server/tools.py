"""ClearWave Mobile tool handlers.

Each handler has the signature `handler(cur, args) -> dict` where `cur` is a dict
cursor inside a transaction and `args` is the already schema-validated arguments
object. The returned dict is the tool result, serialized as-is.

Handlers hold domain logic only. Every identifier, price, allowance, timestamp,
and eligibility decision that appears in a result is read or computed from the
database, never from wall time and never generated at random.

The load-bearing case is high-speed data. No handler stores or reads a
"remaining" figure: the plan carries the allowance, `usage_samples` carry
consumption, `addon_transactions` carry purchased increments, and the
`line_high_speed_balance` view sums them. That is why buying an add-on changes
what a later usage read reports, and why buying a second one changes it again.
"""
from __future__ import annotations

from db import NotFound, ToolRefusal, all_rows, allocate_id, one, scalar, scenario_id, scenario_value
from projection import as_float, as_int, as_list_always, compact


# ---------------------------------------------------------------------------
# scenario clock and access gate
# ---------------------------------------------------------------------------


def _intake_channel(cur) -> str:
    """The channel this call arrived on, which selects the verification policy."""
    return scalar(cur, "SELECT value FROM scenario WHERE key = 'intake_channel'")


def _tool_time(cur, tool_name: str) -> tuple[object, str]:
    """Advance the tool's call counter and return the instant it reports.

    Recorded results carry a different timestamp per call, and a backend would
    take those from its own clock. This environment has no clock, so the elapsed
    offsets observed on the call live in `tool_clock`, keyed by tool and
    invocation ordinal. Past the recorded offsets the cursor's step keeps the
    clock moving forward, so an unrecorded second call is stamped later than the
    first rather than identically.

    Returns the typed instant and the string the tool emits for it.
    """
    cursor = one(
        cur,
        """
        UPDATE tool_clock_cursor
           SET calls_served = calls_served + 1
         WHERE tool_name = %s
        RETURNING calls_served AS call_index, default_step_seconds
        """,
        (tool_name,),
    )
    if cursor is None:
        raise KeyError(f"no clock cursor for tool {tool_name!r}")

    index = cursor["call_index"]
    recorded = one(
        cur,
        "SELECT offset_seconds FROM tool_clock WHERE tool_name = %s AND call_index = %s",
        (tool_name, index),
    )
    if recorded is not None:
        offset = recorded["offset_seconds"]
    else:
        last = one(
            cur,
            """
            SELECT max(call_index) AS last_index, max(offset_seconds) AS last_offset
              FROM tool_clock
             WHERE tool_name = %s
            """,
            (tool_name,),
        )
        last_index = last["last_index"] or 0
        last_offset = last["last_offset"] or 0
        offset = last_offset + cursor["default_step_seconds"] * (index - last_index)

    stamped = one(
        cur,
        """
        SELECT scenario_now() + make_interval(secs => %s) AS at,
               scenario_iso(scenario_now() + make_interval(secs => %s)) AS display
        """,
        (offset, offset),
    )
    return stamped["at"], stamped["display"]


def _required_scope(cur, tool_name: str) -> str | None:
    return scalar(
        cur,
        "SELECT required_scope FROM tool_access_requirements WHERE tool_name = %s",
        (tool_name,),
    )


def _check_verification(cur, tool_name: str, verification_id: str, customer_id: str):
    """Refuse a protected read unless a verification authorizes it.

    The policy is explicit that a resolved record is not an authorization, so the
    gate reads the verification's own `access_scope` rather than its status: a
    record that failed grants nothing because its scope is empty, and a channel
    whose tier does not cover billing cannot reach a bill even after a successful
    verification.
    """
    scope = _required_scope(cur, tool_name)
    if scope is None:
        return None

    record = one(
        cur,
        """
        SELECT verification_id, customer_id, status, access_scope
          FROM identity_verifications
         WHERE verification_id = %s
        """,
        (verification_id,),
    )
    if record is None:
        raise ToolRefusal(f"no identity verification named {verification_id!r}")
    if record["customer_id"] != customer_id:
        raise ToolRefusal(
            f"verification {verification_id!r} was issued for another customer")
    if record["status"] != "verified":
        raise ToolRefusal(
            f"verification {verification_id!r} is {record['status']}, not verified")
    if scope not in as_list_always(record["access_scope"]):
        raise ToolRefusal(
            f"verification {verification_id!r} does not authorize {scope!r} access")
    return record


def _check_customer_verified(cur, tool_name: str, customer_id: str) -> None:
    """Gate a mutation that takes no verification argument.

    `add_data_addon` carries only a line, an offer, and an authorization flag, so
    there is no verification id to cite. The requirement still holds, so it is
    checked against state: the account must already hold a verified record whose
    scope covers the mutation.
    """
    scope = _required_scope(cur, tool_name)
    if scope is None:
        return
    holder = one(
        cur,
        """
        SELECT verification_id
          FROM identity_verifications
         WHERE customer_id = %s AND status = 'verified' AND %s = ANY(access_scope)
         ORDER BY verified_at DESC
         LIMIT 1
        """,
        (customer_id, scope),
    )
    if holder is None:
        raise ToolRefusal(
            f"account {customer_id!r} holds no verified identity record "
            f"authorizing {scope!r} access")


# ---------------------------------------------------------------------------
# shared lookups
# ---------------------------------------------------------------------------


def _line(cur, line_id: str) -> dict:
    row = one(
        cur,
        """
        SELECT l.line_id, l.customer_id, l.status, l.plan_id, l.billing_cycle_id,
               l.autopay_enabled, l.metering_source,
               p.addons_allowed, p.after_high_speed_allowance
          FROM lines l
          JOIN plans p ON p.plan_id = l.plan_id
         WHERE l.line_id = %s
        """,
        (line_id,),
    )
    if row is None:
        raise NotFound(f"unknown line {line_id!r}")
    return row


def _balance(cur, line_id: str) -> dict:
    """The line's high-speed position in its current cycle, as an aggregate."""
    return one(
        cur,
        """
        SELECT allowance_gigabytes, consumed_gigabytes, added_gigabytes,
               remaining_gigabytes
          FROM line_high_speed_balance
         WHERE line_id = %s
        """,
        (line_id,),
    )


def _overdue_bills(cur, customer_id: str) -> int:
    return scalar(
        cur,
        "SELECT count(*) FROM bills WHERE customer_id = %s AND status = 'overdue'",
        (customer_id,),
    )


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


def lookup_customer(cur, args) -> dict:
    channel = _intake_channel(cur)
    factors = scalar(
        cur,
        "SELECT required_factors FROM verification_policies WHERE channel = %s",
        (channel,),
    )
    if factors is None:
        raise ToolRefusal(f"no verification policy for intake channel {channel!r}")

    # The date of birth arrives the way a caller says it, so it is compared
    # against the stored date rendered in that form rather than parsed.
    matches = all_rows(
        cur,
        """
        SELECT DISTINCT c.customer_id
          FROM customers c
          JOIN lines l ON l.customer_id = c.customer_id
         WHERE l.mobile_number = %s
           AND lower(c.full_name) = lower(%s)
           AND to_char(c.date_of_birth, 'FMMonth FMDD, YYYY') = %s
         ORDER BY c.customer_id
        """,
        (args["mobile_number"], args["full_name"].strip(), args["date_of_birth"]),
    )

    # A duplicate account record makes even the full factor set ambiguous, and
    # the registry's contract is to say so rather than to pick one.
    if not matches:
        return {"match": "none"}
    if len(matches) > 1:
        return {"match": "multiple"}

    return {
        "customer_id": matches[0]["customer_id"],
        "match": "unique",
        "required_verification_factors": as_list_always(factors),
    }


def verify_customer_identity(cur, args) -> dict:
    customer = one(
        cur,
        """
        SELECT customer_id, slug, full_name, account_status, identity_hold,
               to_char(date_of_birth, 'FMMonth FMDD, YYYY') AS date_of_birth_spoken
          FROM customers
         WHERE customer_id = %s
        """,
        (args["customer_id"],),
    )
    if customer is None:
        raise NotFound(f"unknown customer {args['customer_id']!r}")

    channel = _intake_channel(cur)
    policy = one(
        cur,
        """
        SELECT required_factors, granted_scope
          FROM verification_policies
         WHERE channel = %s
        """,
        (channel,),
    )
    if policy is None:
        raise ToolRefusal(f"no verification policy for intake channel {channel!r}")

    on_account = one(
        cur,
        "SELECT 1 AS present FROM lines WHERE customer_id = %s AND mobile_number = %s",
        (customer["customer_id"], args["mobile_number"]),
    )
    outcomes = {
        "mobile_number": on_account is not None,
        "full_name": customer["full_name"].strip().lower()
        == args["full_name"].strip().lower(),
        "date_of_birth": customer["date_of_birth_spoken"] == args["date_of_birth"],
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
    # One verification record per caller per channel: re-verifying the same
    # caller on the same channel refreshes it rather than accumulating identical
    # records, so the identifier is derived from the account stem and the channel
    # instead of being drawn from a counter.
    verification_id = (scenario_id(cur, "next_identity_verification_id",
                                   "identity_verifications", "verification_id",
                                   {"customer_id": customer["customer_id"], "channel": channel})
                       or f"verification-{customer['slug']}-{channel}")
    verified_at, verified_at_display = _tool_time(cur, "verify_customer_identity")

    cur.execute(
        """
        INSERT INTO identity_verifications
            (verification_id, customer_id, channel, status, matched_factors,
             access_scope, verified_at, verified_at_display)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (verification_id) DO UPDATE
            SET status = EXCLUDED.status,
                matched_factors = EXCLUDED.matched_factors,
                access_scope = EXCLUDED.access_scope,
                verified_at = EXCLUDED.verified_at,
                verified_at_display = EXCLUDED.verified_at_display
        """,
        (verification_id, customer["customer_id"], channel, status, matched, scope,
         verified_at, verified_at_display),
    )

    return {
        "verification_id": verification_id,
        "status": status,
        "matched_factors": matched,
        "access_scope": scope,
        "verified_at": verified_at_display,
    }


def get_customer_account(cur, args) -> dict:
    customer_id = args["customer_id"]
    if not one(cur, "SELECT 1 FROM customers WHERE customer_id = %s", (customer_id,)):
        raise NotFound(f"unknown customer {customer_id!r}")
    _check_verification(cur, "get_customer_account", args["verification_id"],
                        customer_id)

    sections = set(args["include"])
    lines = devices = plans = None

    if "lines" in sections:
        lines = [
            {
                "line_id": row["line_id"],
                "masked_mobile_number": row["masked_mobile_number"],
                "status": row["status"],
                "billing_cycle_id": row["billing_cycle_id"],
            }
            for row in all_rows(
                cur,
                """
                SELECT line_id, masked_mobile_number, status, billing_cycle_id
                  FROM lines
                 WHERE customer_id = %s
                 ORDER BY line_id
                """,
                (customer_id,),
            )
        ]

    if "devices" in sections:
        devices = [
            {
                "device_id": row["device_id"],
                "model": row["model"],
                "line_id": row["line_id"],
                "provisioning_status": row["provisioning_status"],
            }
            for row in all_rows(
                cur,
                """
                SELECT d.device_id, d.model, d.line_id, d.provisioning_status
                  FROM devices d
                  JOIN lines l ON l.line_id = d.line_id
                 WHERE l.customer_id = %s
                 ORDER BY d.line_id, d.device_id
                """,
                (customer_id,),
            )
        ]

    if "plans" in sections:
        # One entry per line, not per distinct plan: the registry's plan section
        # says which plan each line is on.
        plans = [
            {"plan_id": row["plan_id"], "name": row["name"], "line_id": row["line_id"]}
            for row in all_rows(
                cur,
                """
                SELECT p.plan_id, p.name, l.line_id
                  FROM lines l
                  JOIN plans p ON p.plan_id = l.plan_id
                 WHERE l.customer_id = %s
                 ORDER BY l.line_id
                """,
                (customer_id,),
            )
        ]

    return compact([
        ("customer_id", customer_id),
        ("lines", lines),
        ("devices", devices),
        ("plans", plans),
    ])


def get_line_data_usage(cur, args) -> dict:
    line = _line(cur, args["line_id"])
    _check_verification(cur, "get_line_data_usage", args["verification_id"],
                        line["customer_id"])

    window = args["window"]
    if window == "custom":
        # The registry makes both bounds required for a custom window; without
        # them there is nothing to measure, so the read is refused rather than
        # silently widened to a default.
        if not args.get("window_start") or not args.get("window_end"):
            raise ToolRefusal(
                "a custom window requires window_start and window_end")
        bounds = one(
            cur,
            "SELECT %s::timestamptz AS lo, %s::timestamptz AS hi",
            (args["window_start"], args["window_end"]),
        )
    elif window == "last_24_hours":
        bounds = one(
            cur,
            """
            SELECT scenario_now() - interval '24 hours' AS lo, scenario_now() AS hi
            """,
        )
    else:
        bounds = one(
            cur,
            """
            SELECT cycle_start AS lo, cycle_end AS hi
              FROM billing_cycles
             WHERE billing_cycle_id = %s
            """,
            (line["billing_cycle_id"],),
        )
    if bounds["hi"] <= bounds["lo"]:
        raise ToolRefusal("window_end must be later than window_start")

    # The reported window is the extent of the metered records inside the
    # requested interval, not the interval itself. That is why a request for the
    # last twenty-four hours comes back as midnight to four in the morning: the
    # samples are where the traffic was. With nothing metered the requested
    # bounds are reported and the total is zero.
    measured = one(
        cur,
        """
        SELECT scenario_iso(COALESCE(min(window_start), %s)) AS window_start,
               scenario_iso(COALESCE(max(window_end), %s)) AS window_end,
               COALESCE(sum(gigabytes), 0)::numeric(12, 2) AS used_gigabytes,
               count(*) AS samples,
               min(measurement_source) AS source,
               count(DISTINCT billing_cycle_id) AS cycle_count,
               min(billing_cycle_id) AS billing_cycle_id
          FROM usage_samples
         WHERE line_id = %s
           AND window_end > %s
           AND window_start < %s
        """,
        (bounds["lo"], bounds["hi"], line["line_id"], bounds["lo"], bounds["hi"]),
    )

    source = measured["source"] or line["metering_source"]
    attribution = scalar(
        cur,
        "SELECT app_attribution_available FROM measurement_sources WHERE source_id = %s",
        (source,),
    )

    # A window that straddles two cycles is not attributable to either, so the
    # line's current cycle is reported instead of an arbitrary one of them.
    cycle_id = (measured["billing_cycle_id"] if measured["cycle_count"] == 1
                else line["billing_cycle_id"])

    balance = _balance(cur, line["line_id"])
    as_of = _tool_time(cur, "get_line_data_usage")[1]

    return {
        "line_id": line["line_id"],
        "billing_cycle_id": cycle_id,
        "measurement_source": source,
        "window_start": measured["window_start"],
        "window_end": measured["window_end"],
        "used_gigabytes": as_float(measured["used_gigabytes"]),
        # Always the current cycle's balance, per the registry: the window says
        # what was consumed, the balance says what is left to consume.
        "remaining_high_speed_gigabytes": as_float(balance["remaining_gigabytes"]),
        "app_attribution_available": attribution,
        "as_of": as_of,
    }


def get_customer_bills(cur, args) -> dict:
    customer_id = args["customer_id"]
    if not one(cur, "SELECT 1 FROM customers WHERE customer_id = %s", (customer_id,)):
        raise NotFound(f"unknown customer {customer_id!r}")
    _check_verification(cur, "get_customer_bills", args["verification_id"],
                        customer_id)

    # Days to reset counted by calendar date in the account's zone, which is how
    # the answer is spoken: the cycle ends on 5 September and the call is on 27
    # August, so it resets in nine days. A past cycle has already reset, and the
    # registry's floor of zero says so.
    select_bill = """
        SELECT b.bill_id, b.billing_cycle_id, b.currency,
               scenario_iso(c.cycle_start) AS cycle_start,
               scenario_iso(c.cycle_end) AS cycle_end,
               GREATEST((c.cycle_end AT TIME ZONE scenario_timezone())::date
                        - (scenario_now() AT TIME ZONE scenario_timezone())::date,
                        0) AS cycle_resets_in_days
          FROM bills b
          JOIN billing_cycles c ON c.billing_cycle_id = b.billing_cycle_id
         WHERE b.customer_id = %s
    """
    if args["status"] == "current":
        bill = one(cur, select_bill + " AND c.is_current", (customer_id,))
    else:
        bill = one(
            cur,
            select_bill + " AND NOT c.is_current ORDER BY c.cycle_end DESC LIMIT 1",
            (customer_id,),
        )
    if bill is None:
        raise NotFound(f"customer {customer_id!r} has no {args['status']} bill")

    sections = set(args["include"])
    money_requested = bool(sections & {"charges", "overages"})

    overage = currency = None
    if money_requested:
        # Zero here is an empty sum, not a stored zero: a plan that reduces speed
        # past its allowance never writes an overage line, so there is nothing to
        # add up.
        overage = scalar(
            cur,
            """
            SELECT COALESCE(sum(amount), 0)::numeric(12, 2)
              FROM bill_charges
             WHERE bill_id = %s AND kind = 'overage'
            """,
            (bill["bill_id"],),
        )
        currency = bill["currency"]

    behaviour = None
    if "plan_behavior" in sections:
        # Post-allowance behaviour belongs to the plan, and the bill is the
        # account's, so it is read from the account's primary line.
        behaviour = scalar(
            cur,
            """
            SELECT p.after_high_speed_allowance
              FROM lines l
              JOIN plans p ON p.plan_id = l.plan_id
             WHERE l.customer_id = %s
             ORDER BY l.is_primary DESC, l.line_id
             LIMIT 1
            """,
            (customer_id,),
        )

    cycle_requested = "cycle" in sections
    as_of = _tool_time(cur, "get_customer_bills")[1]

    return compact([
        ("bill_id", bill["bill_id"]),
        ("billing_cycle_id", bill["billing_cycle_id"]),
        ("cycle_start", bill["cycle_start"] if cycle_requested else None),
        ("cycle_end", bill["cycle_end"] if cycle_requested else None),
        ("cycle_resets_in_days",
         as_int(bill["cycle_resets_in_days"]) if cycle_requested else None),
        ("overage_charge", as_float(overage) if money_requested else None),
        ("currency", currency),
        ("after_high_speed_allowance", behaviour),
        ("as_of", as_of),
    ])


def get_data_addon_offers(cur, args) -> dict:
    line = _line(cur, args["line_id"])
    _check_verification(cur, "get_data_addon_offers", args["verification_id"],
                        line["customer_id"])

    overdue = _overdue_bills(cur, line["customer_id"])
    # Current means unexpired against the scenario clock and not withdrawn from
    # the catalog. Eligibility is reported per offer rather than filtered on, so
    # an offer this line cannot buy comes back saying so instead of vanishing.
    offers = all_rows(
        cur,
        """
        SELECT offer_id, data_gigabytes, price, currency, billing_timing,
               effective_timing, scenario_iso(expires_at) AS expires_at,
               requires_line_status, requires_autopay
          FROM addon_offers
         WHERE plan_id = %s
           AND NOT withdrawn
           AND expires_at > scenario_now()
         ORDER BY data_gigabytes, offer_id
        """,
        (line["plan_id"],),
    )
    as_of = _tool_time(cur, "get_data_addon_offers")[1]

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
                "expires_at": offer["expires_at"],
            }
            for offer in offers
        ],
        "as_of": as_of,
    }


# ---------------------------------------------------------------------------
# mutations
# ---------------------------------------------------------------------------


def _allocate_transaction_id(cur, line_id: str, gigabytes: float) -> str:
    """Issue the add-on transaction identifier for a purchase on this line.

    The allocator holds the account stem; the size of the add-on completes it,
    which is where `dfdab773-2580-4908-91cf-99a2b4826547` comes from. The issued
    ordinal is appended only from the second purchase onward, so the first
    purchase reads as a name and a repeat cannot collide with it. Written here
    rather than through db.allocate_id because that helper substitutes the
    ordinal unconditionally.
    """
    issued = one(
        cur,
        """
        UPDATE id_allocator
           SET next_value = next_value + 1
         WHERE entity_type = 'addon_transaction' AND scope = %s
        RETURNING next_value - 1 AS ordinal, template
        """,
        (line_id,),
    )
    if issued is None:
        raise ToolRefusal(f"line {line_id!r} has no add-on transaction allocator")
    stem = f"{issued['template']}-{gigabytes:g}gb"
    return stem if issued["ordinal"] == 1 else f"{stem}-{issued['ordinal']}"


def add_data_addon(cur, args) -> dict:
    line = _line(cur, args["line_id"])
    _check_customer_verified(cur, "add_data_addon", line["customer_id"])

    offer = one(
        cur,
        """
        SELECT offer_id, plan_id, data_gigabytes, price, currency, billing_timing,
               effective_timing, requires_line_status, requires_autopay, withdrawn,
               expires_at > scenario_now() AS current
          FROM addon_offers
         WHERE offer_id = %s
        """,
        (args["offer_id"],),
    )
    if offer is None:
        raise NotFound(f"unknown offer {args['offer_id']!r}")

    # Policy order: the product has to be real and buyable for this line before
    # the authorization means anything, and the authorization has to be explicit
    # before anything is charged.
    if offer["plan_id"] != line["plan_id"]:
        raise ToolRefusal(
            f"offer {offer['offer_id']!r} is not offered on plan {line['plan_id']!r}")
    if offer["withdrawn"] or not offer["current"]:
        raise ToolRefusal(f"offer {offer['offer_id']!r} is no longer current")

    overdue = _overdue_bills(cur, line["customer_id"])
    if _eligibility(line, offer, overdue) != "eligible":
        raise ToolRefusal(
            f"line {line['line_id']!r} is not eligible for offer {offer['offer_id']!r}")
    if not args["customer_authorized"]:
        raise ToolRefusal(
            "the customer has not authorized the data amount, price, currency, "
            "and billing timing")

    bill = one(
        cur,
        """
        SELECT bill_id, currency
          FROM bills b
          JOIN billing_cycles c ON c.billing_cycle_id = b.billing_cycle_id
         WHERE b.customer_id = %s AND c.is_current AND b.status = 'open'
        """,
        (line["customer_id"],),
    )
    # A next-bill charge needs a bill that has not been issued yet. Without one
    # there is nowhere to put the charge, and inventing one would misreport where
    # the customer will see it.
    if bill is None:
        raise ToolRefusal(
            f"account {line['customer_id']!r} has no open bill to charge")

    gigabytes = as_float(offer["data_gigabytes"])
    transaction_id = (scenario_id(cur, "next_addon_transaction_id",
                                  "addon_transactions", "transaction_id")
                      or _allocate_transaction_id(cur, line["line_id"], gigabytes))
    effective_at, effective_at_display = _tool_time(cur, "add_data_addon")

    # An add-on that only takes effect next cycle is not usable data yet, so it
    # is recorded as pending and the balance view, which counts active rows only,
    # does not pick it up.
    status = "active" if offer["effective_timing"] == "immediate" else "pending"

    cur.execute(
        """
        INSERT INTO addon_transactions
            (transaction_id, line_id, offer_id, billing_cycle_id, bill_id, status,
             data_gigabytes, charged_price, currency, effective_at,
             effective_at_display, authorized_by_customer)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
        """,
        (transaction_id, line["line_id"], offer["offer_id"],
         line["billing_cycle_id"], bill["bill_id"], status,
         offer["data_gigabytes"], offer["price"], offer["currency"], effective_at,
         effective_at_display),
    )
    cur.execute(
        """
        INSERT INTO bill_charges
            (charge_id, bill_id, kind, description, amount, currency, billing_timing)
        VALUES (%s, %s, 'addon', %s, %s, %s, %s)
        """,
        (f"charge-{transaction_id}", bill["bill_id"],
         f"Data add-on {gigabytes:g} GB", offer["price"], offer["currency"],
         offer["billing_timing"]),
    )

    # Read back through the same aggregate the usage tool uses, after the insert,
    # so the balance reported here and the balance a later usage read reports
    # cannot disagree.
    balance = _balance(cur, line["line_id"])

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


def transfer_to_specialist(cur, args) -> dict:
    transfer_id = allocate_id(cur, "specialist_transfer")
    created_at, created_at_display = _tool_time(cur, "transfer_to_specialist")
    cur.execute(
        """
        INSERT INTO specialist_transfers
            (transfer_id, reason, summary, status, created_at, created_at_display)
        VALUES (%s, %s, %s, 'accepted', %s, %s)
        """,
        (transfer_id, args["reason"], args["summary"], created_at,
         created_at_display),
    )
    return {"status": "accepted", "transfer_id": transfer_id}


HANDLERS = {
    "lookup_customer": lookup_customer,
    "verify_customer_identity": verify_customer_identity,
    "get_customer_account": get_customer_account,
    "get_line_data_usage": get_line_data_usage,
    "get_customer_bills": get_customer_bills,
    "get_data_addon_offers": get_data_addon_offers,
    "add_data_addon": add_data_addon,
    "transfer_to_specialist": transfer_to_specialist,
}

# Tools that change the carrier's records. The grading layer holds reads free —
# an agent may look at anything as often as it likes — so the distinction has to
# be stated somewhere, and the handlers are where it is known. What counts is
# whether the tool changes the world the caller cares about, not whether it
# happens to touch a table: get_line_data_usage, get_customer_bills and
# get_data_addon_offers each advance tool_clock_cursor so that a second call is
# stamped later than the first, and that is bookkeeping, not a change to his
# account. verify_customer_identity is here because the record it files is what
# authorizes account access, and its scope is what every protected read is
# checked against.
WRITE_TOOLS = {
    "verify_customer_identity",
    "add_data_addon",
    "transfer_to_specialist",
}
