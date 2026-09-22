"""Banking tool handlers.

Each handler has the signature `handler(cur, args) -> dict` where `cur` is a dict
cursor inside a transaction and `args` is the already schema-validated arguments
object. The returned dict is the tool result, serialized as-is.

Handlers hold domain logic only. Every identifier, masked destination, amount,
status, product term, and timestamp that appears in a result is read from the
database or derived from a column by a stated rule, never generated at random or
taken from wall time, so a result is reproducible and an operator can explain any
value by pointing at a row.
"""
from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal

from db import NotFound, ToolRefusal, all_rows, allocate_id, one, scenario_id, scenario_value
from projection import as_float, as_list_always, compact

MONTH_NUMBERS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}

# Card sections in the order the registry declares them on the result.
SECTION_ORDER = ("status", "available_credit", "authorizations", "declines",
                 "restrictions", "travel_notices")


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def as_amount(value):
    """Render a money column the way this bank's records render it.

    The recorded card-account results carry whole-dollar amounts as JSON
    integers (840, 32, 912) and statement amounts with cents as JSON floats
    (243.18). Both forms are real, so the form follows the value: an amount with
    no cents is an integer, an amount with cents is a float.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def mask_email(email):
    """First character of the local part, then the domain.

    The recorded results render johnny.monroe.travel@outlook.com as
    'j***@outlook.com', so the masked form is derived from the stored address
    rather than stored a second time and allowed to drift from it.
    """
    if not email or "@" not in email:
        return None
    local, domain = email.split("@", 1)
    return f"{local[:1]}***@{domain}"


def destination_slug(destination: str) -> str:
    """Naming stem for a place, as the bank's record identifiers use it.

    'Portland, Maine' names a notice 'travel-notice-<customer>-portland': the
    city carries the name and the region only qualifies it.
    """
    head = destination.split(",")[0].strip().lower()
    return re.sub(r"[^a-z0-9]+", "-", head).strip("-") or "trip"


def _parse_iso(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def _is_expired(cur, expires_at) -> bool:
    """True when the scenario clock has passed a stored expiry."""
    deadline = _parse_iso(expires_at)
    now = _parse_iso(scenario_value(cur, "scenario_time"))
    if deadline is None or now is None:
        return False
    return now > deadline


def _plus_minutes(stamp, minutes: int):
    parsed = _parse_iso(stamp)
    if parsed is None:
        return None
    return (parsed + dt.timedelta(minutes=minutes)).isoformat()


def _unique_id(cur, table: str, column: str, candidate: str) -> str:
    """The candidate identifier, suffixed if the bank already issued it.

    Readable identifiers are derived from business keys, so a second record for
    the same key would collide. The suffix keeps them distinct rather than
    letting the second record overwrite the first.
    """
    taken = {r[column] for r in all_rows(
        cur,
        f"SELECT {column} FROM {table} WHERE {column} = %s OR {column} LIKE %s",
        (candidate, candidate + "-%"))}
    if candidate not in taken:
        return candidate
    suffix = 2
    while f"{candidate}-{suffix}" in taken:
        suffix += 1
    return f"{candidate}-{suffix}"


# ---------------------------------------------------------------------------
# customer resolution and verification
# ---------------------------------------------------------------------------


def lookup_customer(cur, args) -> dict:
    identifiers = ("account_id", "email", "full_name")
    corroborating = ("billing_zip", "card_last4", "account_match")
    if not any(args.get(key) for key in identifiers + corroborating):
        raise ToolRefusal("a lookup needs at least one identifier")

    clauses: list = []
    params: list = []
    resolved_by = None

    # Registry precedence: account_id, then email, then full_name. The lower
    # precedence identifiers are not also applied, so a caller who gives an
    # account id and misremembers the name still resolves.
    if args.get("account_id"):
        clauses.append("(c.account_id = %s OR c.customer_id = %s)")
        params.extend([args["account_id"], args["account_id"]])
        resolved_by = "account_id"
    elif args.get("email"):
        clauses.append("(lower(c.notification_email) = lower(%s) "
                       "OR lower(c.primary_email) = lower(%s))")
        params.extend([args["email"], args["email"]])
        resolved_by = "email"
    elif args.get("full_name"):
        clauses.append("lower(c.full_name) = lower(%s)")
        params.append(args["full_name"].strip())
        resolved_by = "full_name"

    if args.get("billing_zip"):
        clauses.append("c.billing_zip = %s")
        params.append(args["billing_zip"])
    if args.get("card_last4"):
        clauses.append("EXISTS (SELECT 1 FROM card_accounts a "
                       "WHERE a.customer_id = c.customer_id AND a.card_last4 = %s)")
        params.append(args["card_last4"])
    if args.get("account_match") == "caller_phone":
        clauses.append("c.caller_channel_match")

    matches = all_rows(
        cur,
        f"""
        SELECT c.customer_id, c.account_id, c.full_name, c.caller_channel_match,
               c.required_verification_methods
          FROM customers c
         WHERE {' AND '.join(clauses)}
         ORDER BY c.customer_id
        """,
        tuple(params),
    )
    if not matches:
        raise NotFound("no profile matches the supplied identifiers")
    if len(matches) > 1:
        # Policy forbids continuing on an ambiguous profile, so the ambiguity is
        # reported rather than resolved by taking the first row.
        raise ToolRefusal(
            "more than one profile matches; a narrower identifier is required",
            {"candidate_count": len(matches)},
        )

    customer = matches[0]
    channels = all_rows(
        cur,
        """
        SELECT channel_id, type, masked_destination
          FROM trusted_channels
         WHERE customer_id = %s AND enrolled
         ORDER BY channel_id
        """,
        (customer["customer_id"],),
    )
    return compact([
        ("customer_id", customer["customer_id"]),
        ("account_id", customer["account_id"] if resolved_by == "account_id" else None),
        ("match", "unique"),
        # A lookup that resolved on a stable account identifier reports the name
        # it landed on, which is what the agent reads back to confirm the
        # profile. A lookup that already carried the name has nothing to add.
        ("full_name", customer["full_name"] if resolved_by == "account_id" else None),
        ("caller_phone_match", customer["caller_channel_match"]),
        ("required_verification_methods",
         as_list_always(customer["required_verification_methods"])),
        ("trusted_channels", [
            {"channel_id": c["channel_id"], "type": c["type"],
             "masked_destination": c["masked_destination"]}
            for c in channels
        ] or None),
    ])


def get_current_time(cur, args) -> dict:
    status = scenario_value(cur, "time_status") or "available"
    if status != "available":
        return {"status": status}
    return {
        "status": "available",
        "timestamp": scenario_value(cur, "scenario_time"),
        "timezone": scenario_value(cur, "timezone"),
    }


def _customer(cur, customer_id: str) -> dict:
    row = one(cur, "SELECT * FROM customers WHERE customer_id = %s", (customer_id,))
    if row is None:
        raise NotFound(f"unknown customer {customer_id!r}")
    return row


def _matched_factors(cur, customer: dict, args) -> set:
    """Which permitted factors the supplied values actually match.

    Naming a factor does not match it: each supplied value is compared with the
    profile, and caller_phone is matched by the channel the call arrived on
    rather than by anything the caller says.
    """
    matched = set()
    if customer["caller_channel_match"]:
        matched.add("caller_phone")
    if args.get("billing_zip") and args["billing_zip"] == customer["billing_zip"]:
        matched.add("billing_zip")
    if args.get("mobile_last4") and args["mobile_last4"] == customer["mobile_last4"]:
        matched.add("mobile_last4")
    if args.get("card_last4") and one(
            cur,
            "SELECT 1 FROM card_accounts WHERE customer_id = %s AND card_last4 = %s",
            (customer["customer_id"], args["card_last4"])):
        matched.add("card_last4")
    supplied_birthday = args.get("birth_month_day")
    if supplied_birthday:
        month_name, _, day = supplied_birthday.partition(" ")
        if (MONTH_NUMBERS.get(month_name.lower()) == customer["birth_month"]
                and day.isdigit() and int(day) == customer["birth_day"]):
            matched.add("birth_month_day")
    return matched


def verify_customer_identity(cur, args) -> dict:
    customer = _customer(cur, args["customer_id"])
    required = list(customer["required_verification_methods"])
    matched = _matched_factors(cur, customer, args)
    # Emitted in the profile's required order, so the list reads as an answer to
    # the profile's requirement rather than as an echo of the arguments.
    matched_methods = [factor for factor in required if factor in matched]
    verified = all(factor in matched for factor in required)

    now = scenario_value(cur, "scenario_time")
    # A verification record is scoped to the reason the profile is in contact.
    # Re-verifying inside one case returns that case's record rather than
    # minting a second record for the same piece of work.
    case = one(
        cur,
        "SELECT case_slug FROM service_cases WHERE customer_id = %s AND status = 'open'",
        (customer["customer_id"],),
    )
    forced_id = scenario_id(cur, "next_identity_verification_id",
                            "identity_verifications", "verification_id",
                            {"customer_id": customer["customer_id"]})
    if forced_id:
        verification_id = forced_id
    elif case:
        verification_id = (f"verification-{customer['verification_key']}"
                           f"-{case['case_slug']}")
    else:
        verification_id = allocate_id(cur, "identity_verification")

    time_asserted = bool(args.get("verified_at"))
    cur.execute(
        """
        INSERT INTO identity_verifications
            (verification_id, customer_id, status, required_methods, matched_methods,
             verified_at, expires_at, time_asserted)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (verification_id) DO UPDATE
            SET status = EXCLUDED.status,
                matched_methods = EXCLUDED.matched_methods,
                verified_at = EXCLUDED.verified_at,
                expires_at = EXCLUDED.expires_at,
                time_asserted = EXCLUDED.time_asserted
        """,
        (verification_id, customer["customer_id"],
         "verified" if verified else "unverified", required, matched_methods,
         now if verified else None,
         _plus_minutes(now, 30) if verified else None, time_asserted),
    )

    return compact([
        ("verification_id", verification_id),
        ("status", "verified" if verified else "unverified"),
        ("matched_methods", as_list_always(matched_methods)),
        # The caller may assert the time it observed. The backend's own record
        # time is the authoritative one, so an asserted time is answered with
        # the time the record actually carries; a caller who asserted nothing is
        # given nothing to reconcile.
        ("verified_at", now if (time_asserted and verified) else None),
    ])


def _active_verification(cur, customer_id: str, verification_id: str) -> dict:
    row = one(
        cur,
        """
        SELECT verification_id, status, expires_at
          FROM identity_verifications
         WHERE verification_id = %s AND customer_id = %s
        """,
        (verification_id, customer_id),
    )
    if row is None:
        raise NotFound(f"unknown verification {verification_id!r} for this customer")
    if row["status"] != "verified":
        raise ToolRefusal("identity verification is not in a verified state",
                          {"verification_status": row["status"]})
    if _is_expired(cur, row["expires_at"]):
        raise ToolRefusal("identity verification has expired")
    return row


# ---------------------------------------------------------------------------
# trusted-channel confirmation and profile email
# ---------------------------------------------------------------------------


def start_trusted_channel_confirmation(cur, args) -> dict:
    customer = _customer(cur, args["customer_id"])
    _active_verification(cur, customer["customer_id"], args["verification_id"])

    channel = one(
        cur,
        """
        SELECT channel_id, masked_destination
          FROM trusted_channels
         WHERE customer_id = %s AND type = %s AND enrolled
         ORDER BY channel_id
         LIMIT 1
        """,
        (customer["customer_id"], args["channel"]),
    )
    if channel is None:
        raise ToolRefusal(f"no enrolled {args['channel']} channel on this profile",
                          {"channel": args["channel"]})

    now = scenario_value(cur, "scenario_time")
    purpose = args["purpose"]
    confirmation_id = scenario_id(
        cur, "next_channel_confirmation_id", "channel_confirmations", "confirmation_id",
        {"customer_id": customer["customer_id"], "purpose": purpose}) or (
        f"confirmation-{purpose.replace('_', '-')}-{customer['verification_key']}")
    # Starting the same purpose again re-sends the challenge on the same record
    # rather than opening a second one, and resets it to 'sent': a re-sent
    # challenge is not a completed one. It goes to the channel named this time,
    # so the record and the reply agree on where the code went.
    cur.execute(
        """
        INSERT INTO channel_confirmations
            (confirmation_id, customer_id, channel_id, purpose, masked_destination,
             status, verification_id, sent_at, verified_at, expires_at)
        VALUES (%s, %s, %s, %s, %s, 'sent', %s, %s, NULL, %s)
        ON CONFLICT (confirmation_id) DO UPDATE
            SET status = 'sent', verified_at = NULL, sent_at = EXCLUDED.sent_at,
                channel_id = EXCLUDED.channel_id,
                masked_destination = EXCLUDED.masked_destination,
                expires_at = EXCLUDED.expires_at,
                verification_id = EXCLUDED.verification_id
        """,
        (confirmation_id, customer["customer_id"], channel["channel_id"], purpose,
         channel["masked_destination"], args["verification_id"], now,
         _plus_minutes(now, 15)),
    )
    # The record carries an expiry and the mutation path enforces it, but this
    # bank does not disclose challenge expiry to the agent: only the delivery
    # state and the masked destination are reported.
    return {
        "confirmation_id": confirmation_id,
        "status": "sent",
        "masked_destination": channel["masked_destination"],
    }


def get_trusted_channel_confirmation(cur, args) -> dict:
    row = one(
        cur,
        """
        SELECT k.confirmation_id, k.status, k.verified_at, k.expires_at,
               t.confirmation_completes, t.confirmation_verified_at
          FROM channel_confirmations k
          JOIN trusted_channels t ON t.channel_id = k.channel_id
         WHERE k.confirmation_id = %s AND k.customer_id = %s
        """,
        (args["confirmation_id"], args["customer_id"]),
    )
    if row is None:
        raise NotFound(f"unknown confirmation {args['confirmation_id']!r}")

    status, verified_at = row["status"], row["verified_at"]
    if status in ("requested", "sent", "delivered"):
        if _is_expired(cur, row["expires_at"]):
            status, verified_at = "expired", None
        elif row["confirmation_completes"]:
            # The customer completes the challenge through the approved secure
            # path, which no tool can observe happening. The channel records
            # whether its owner completes it and when, so this read writes the
            # transition once and every later read reports the same record.
            status = "verified"
            verified_at = (row["confirmation_verified_at"]
                           or scenario_value(cur, "scenario_time"))
        cur.execute(
            "UPDATE channel_confirmations SET status = %s, verified_at = %s "
            "WHERE confirmation_id = %s",
            (status, verified_at, row["confirmation_id"]),
        )

    return compact([
        ("confirmation_id", row["confirmation_id"]),
        ("status", status),
        ("verified_at", verified_at),
    ])


def update_customer_email(cur, args) -> dict:
    customer = _customer(cur, args["customer_id"])
    _active_verification(cur, customer["customer_id"], args["verification_id"])

    confirmation = one(
        cur,
        """
        SELECT confirmation_id, purpose, status
          FROM channel_confirmations
         WHERE confirmation_id = %s AND customer_id = %s
        """,
        (args["confirmation_id"], customer["customer_id"]),
    )
    if confirmation is None:
        raise NotFound(f"unknown confirmation {args['confirmation_id']!r}")
    if confirmation["purpose"] != "email_change":
        raise ToolRefusal("confirmation was not issued for an email change",
                          {"purpose": confirmation["purpose"]})
    if confirmation["status"] != "verified":
        raise ToolRefusal("trusted-channel confirmation is not verified",
                          {"confirmation_status": confirmation["status"]})

    had_prior_address = bool(customer["primary_email"])
    cur.execute(
        "UPDATE customers SET primary_email = %s, notification_email = %s "
        "WHERE customer_id = %s",
        (args["new_email"], args["new_email"], customer["customer_id"]),
    )
    # A profile whose login identifier is the address moves the login with it; a
    # profile that logs in with a username does not.
    login_changed = customer["login_identifier_kind"] == "email"
    # The transition notice goes to the address being left as well as the one
    # being adopted, which is only possible when there was a prior address.
    notices = ["old_email", "new_email"] if had_prior_address else ["new_email"]
    return {
        "status": "updated",
        "primary_email": args["new_email"],
        "notification_email": args["new_email"],
        "login_identifier_changed": login_changed,
        "transition_security_notices": notices,
    }


# ---------------------------------------------------------------------------
# external knowledge
# ---------------------------------------------------------------------------


def _travel_card_matches(cur, _record) -> dict:
    rows = all_rows(
        cur,
        """
        SELECT product_id, product, annual_fee, annual_fee_currency,
               foreign_transaction_fee, lounge_membership
          FROM card_products
         WHERE category = 'travel' AND active
         ORDER BY display_rank, product_id
        """,
    )
    return {"matches": [
        compact([
            ("product_id", r["product_id"]),
            ("product", r["product"]),
            ("annual_fee", as_amount(r["annual_fee"])),
            ("annual_fee_currency", r["annual_fee_currency"]),
            ("foreign_transaction_fee", r["foreign_transaction_fee"]),
            ("lounge_membership", r["lounge_membership"]),
        ])
        for r in rows
    ]}


def _welcome_offers(cur, _record) -> dict:
    rows = all_rows(
        cur,
        """
        SELECT o.offer_id, o.product_id, p.product, o.points, o.spend,
               o.spend_currency, o.days
          FROM welcome_offers o
          JOIN card_products p ON p.product_id = o.product_id
         WHERE o.active AND p.active AND p.category = 'travel'
         ORDER BY o.display_rank, o.offer_id
        """,
    )
    return {"offers": [
        compact([
            ("product_id", r["product_id"]),
            ("product", r["product"]),
            ("points", r["points"]),
            ("spend", as_amount(r["spend"])),
            ("spend_currency", r["spend_currency"]),
            ("days", r["days"]),
        ])
        for r in rows
    ]}


def _product_airline_benefits(cur, record) -> dict:
    row = one(
        cur,
        """
        SELECT airline_incidental_credit, automatic_free_checked_bag,
               airline_specific_rules_apply
          FROM card_products
         WHERE product_id = %s
        """,
        (record["subject_product_id"],),
    )
    if row is None:
        raise NotFound(f"unknown product {record['subject_product_id']!r}")
    return {
        "airline_incidental_credit": row["airline_incidental_credit"],
        "automatic_free_checked_bag": row["automatic_free_checked_bag"],
        "airline_specific_rules_apply": row["airline_specific_rules_apply"],
    }


PROJECTIONS = {
    "travel_card_matches": _travel_card_matches,
    "welcome_offers": _welcome_offers,
    "product_airline_benefits": _product_airline_benefits,
}


def search_knowledge_base(cur, args) -> dict:
    # The best-matching record wins on priority, then on how specific its pattern
    # is, then on identifier, so retrieval never depends on physical row order,
    # and a query that matches nothing is reported rather than guessed at.
    record = one(
        cur,
        """
        SELECT record_id, effective_at, projection, subject_product_id, payload
          FROM kb_records
         WHERE %s ~* query_pattern
         ORDER BY priority DESC, length(query_pattern) DESC, record_id
         LIMIT 1
        """,
        (args["query"],),
    )
    if record is None:
        raise NotFound("no knowledge-base record answers that query")

    result: dict = {
        "record_id": record["record_id"],
        "effective_at": record["effective_at"].isoformat(),
    }
    # A record that is really a view of the product catalog assembles its content
    # from the catalog, so a product term the recording never asked about still
    # answers from the same rows.
    if record["projection"]:
        result.update(PROJECTIONS[record["projection"]](cur, record))
    result.update(record["payload"])
    return result


# ---------------------------------------------------------------------------
# card account
# ---------------------------------------------------------------------------


def _card(cur, customer_id: str, card_last4) -> dict:
    _customer(cur, customer_id)
    if card_last4:
        row = one(
            cur,
            "SELECT * FROM card_accounts WHERE customer_id = %s AND card_last4 = %s",
            (customer_id, card_last4),
        )
        if row is None:
            raise NotFound(f"no card ending {card_last4} on this profile")
        return row
    cards = all_rows(
        cur,
        "SELECT * FROM card_accounts WHERE customer_id = %s ORDER BY card_id",
        (customer_id,),
    )
    if not cards:
        raise NotFound("no card account on this profile")
    if len(cards) > 1:
        raise ToolRefusal("profile holds more than one card; card_last4 is required",
                          {"card_count": len(cards)})
    return cards[0]


TRANSACTION_FIELDS = {
    "transaction_id": lambda r: r["transaction_id"],
    "merchant": lambda r: r["merchant"],
    "merchant_location": lambda r: r["merchant_location"],
    "amount": lambda r: as_amount(r["amount"]),
    "currency": lambda r: r["currency"],
    "status": lambda r: r["status"],
    "reason": lambda r: r["reason"],
    "occurred_at": lambda r: r["occurred_at"],
}


def _section_depth(cur, card_id: str, section: str):
    """Disclosure depth this read serves, and where the last read stopped.

    A section that has been read before is served its next deeper read model,
    and once the deepest one has been served it repeats. The count is a row, so
    an operator can see how many times a section was read.
    """
    cursor = one(
        cur,
        """
        SELECT reads_served, last_seen_seq
          FROM card_section_read_cursor
         WHERE card_id = %s AND section = %s
        """,
        (card_id, section),
    ) or {"reads_served": 0, "last_seen_seq": 0}
    # A card with its own views uses them; every other card uses the '*' depth.
    deepest = one(
        cur,
        "SELECT max(view_index) AS deepest FROM card_section_view "
        "WHERE section = %s AND scope = %s",
        (section, card_id),
    )
    if (deepest or {}).get("deepest") is None:
        deepest = one(
            cur,
            "SELECT max(view_index) AS deepest FROM card_section_view "
            "WHERE section = %s AND scope = '*'",
            (section,),
        )
    return (min(cursor["reads_served"], (deepest or {}).get("deepest") or 0),
            cursor["last_seen_seq"])


def _section_fields(cur, card_id: str, section: str, view_index: int) -> list:
    row = one(
        cur,
        """
        SELECT fields FROM card_section_view
         WHERE section = %s AND view_index = %s AND scope IN (%s, '*')
         ORDER BY (scope = '*')
         LIMIT 1
        """,
        (section, view_index, card_id),
    )
    return list(row["fields"]) if row else []


def _advance_section(cur, card_id: str, section: str, new_seq: int) -> None:
    cur.execute(
        """
        INSERT INTO card_section_read_cursor
            (card_id, section, reads_served, last_seen_seq)
        VALUES (%s, %s, 1, %s)
        ON CONFLICT (card_id, section) DO UPDATE
            SET reads_served = card_section_read_cursor.reads_served + 1,
                last_seen_seq = GREATEST(card_section_read_cursor.last_seen_seq,
                                         EXCLUDED.last_seen_seq)
        """,
        (card_id, section, new_seq),
    )


def _ledger_section(cur, card: dict, section: str, where: str) -> list:
    """Rows of one transaction-backed section, at the depth this read serves."""
    view_index, last_seen_seq = _section_depth(cur, card["card_id"], section)
    fields = _section_fields(cur, card["card_id"], section, view_index)
    policy = one(cur, "SELECT disclosure FROM card_section_policy WHERE section = %s",
                 (section,))
    incremental = bool(policy and policy["disclosure"] == "incremental")

    rows = all_rows(
        cur,
        f"""
        SELECT * FROM transactions
         WHERE card_id = %s AND {where}
         ORDER BY record_seq
        """,
        (card["card_id"],),
    )
    highest = max([r["record_seq"] for r in rows] + [last_seen_seq])
    _advance_section(cur, card["card_id"], section, highest)

    if incremental:
        # An incremental section reports what the ledger recorded after the
        # previous read of it, which is how a second look at a card's
        # authorizations answers "what has happened since" rather than repeating.
        rows = [r for r in rows if r["record_seq"] > last_seen_seq]
    return [
        compact([(name, TRANSACTION_FIELDS[name](row)) for name in fields
                 if name in TRANSACTION_FIELDS])
        for row in rows
    ]


def get_card_account(cur, args) -> dict:
    card = _card(cur, args["customer_id"], args.get("card_last4"))
    requested = [s for s in SECTION_ORDER if s in (args.get("include") or [])]

    result: dict = {"customer_id": card["customer_id"],
                    "card_last4": card["card_last4"]}

    if "status" in requested:
        _advance_section(cur, card["card_id"], "status", 0)
        for name in _section_fields(cur, card["card_id"], "status", 0):
            if name in ("status", "reported_lost", "payment_status"):
                result[name] = card[name]

    if "available_credit" in requested:
        _advance_section(cur, card["card_id"], "available_credit", 0)
        result["available_credit"] = as_amount(card["available_credit"])
        result["available_credit_currency"] = card["available_credit_currency"]

    if "authorizations" in requested:
        result["authorizations"] = _ledger_section(
            cur, card, "authorizations",
            "kind = 'authorization' AND status = 'approved' "
            "AND settlement_state = 'pending'")

    if "declines" in requested:
        result["declines"] = _ledger_section(cur, card, "declines", "kind = 'decline'")

    if "restrictions" in requested:
        _advance_section(cur, card["card_id"], "restrictions", 0)
        result["restrictions"] = [
            compact([
                ("restriction_id", r["restriction_id"]),
                ("status", r["status"]),
                ("linked_transaction_ids", [
                    link["transaction_id"] for link in all_rows(
                        cur,
                        """
                        SELECT transaction_id FROM restriction_transactions
                         WHERE restriction_id = %s
                         ORDER BY link_rank, transaction_id
                        """,
                        (r["restriction_id"],),
                    )
                ] or None),
            ])
            for r in all_rows(
                cur,
                """
                SELECT restriction_id, status FROM card_restrictions
                 WHERE card_id = %s
                 ORDER BY opened_at, restriction_id
                """,
                (card["card_id"],),
            )
        ]

    if "travel_notices" in requested:
        _advance_section(cur, card["card_id"], "travel_notices", 0)
        result["travel_notices"] = [
            compact([
                ("notice_id", n["notice_id"]),
                ("destinations", as_list_always(n["destinations"])),
                ("return_date",
                 n["return_date"].isoformat() if n["return_date"] else None),
                ("authorization_guaranteed", n["authorization_guaranteed"]),
            ])
            for n in all_rows(
                cur,
                """
                SELECT notice_id, destinations, return_date, authorization_guaranteed
                  FROM travel_notices
                 WHERE card_id = %s AND status = 'created'
                 ORDER BY created_at, notice_id
                """,
                (card["card_id"],),
            )
        ]

    return result


def resolve_card_restriction(cur, args) -> dict:
    card = _card(cur, args["customer_id"], args.get("card_last4"))
    restriction = one(
        cur,
        """
        SELECT restriction_id, status, customer_resolvable
          FROM card_restrictions
         WHERE restriction_id = %s AND card_id = %s
        """,
        (args["restriction_id"], card["card_id"]),
    )
    if restriction is None:
        raise NotFound(f"no restriction {args['restriction_id']!r} on this card")
    if restriction["status"] != "open":
        raise ToolRefusal("restriction is not open",
                          {"restriction_status": restriction["status"]})
    if not restriction["customer_resolvable"]:
        # A delinquency hold or a lost-card block is not lifted by the customer
        # confirming activity, so the attempt is refused rather than succeeding
        # with a status the agent would read back as resolved.
        raise ToolRefusal("this restriction is not resolved by confirming activity")

    linked = all_rows(
        cur,
        """
        SELECT l.transaction_id, t.kind, t.merchant_key, t.amount,
               t.settlement_state, t.represented_as
          FROM restriction_transactions l
          JOIN transactions t ON t.transaction_id = l.transaction_id
         WHERE l.restriction_id = %s
         ORDER BY l.link_rank, l.transaction_id
        """,
        (restriction["restriction_id"],),
    )
    confirmed = set(args["confirmed_transaction_ids"])
    unconfirmed = [r["transaction_id"] for r in linked
                   if r["transaction_id"] not in confirmed]
    if unconfirmed:
        # Every activity the review holds has to be accounted for. Lifting the
        # review while some of it is unconfirmed would remove the control that
        # opened it.
        raise ToolRefusal("some activity linked to this restriction was not confirmed",
                          {"unconfirmed_count": len(unconfirmed)})

    now = scenario_value(cur, "scenario_time")
    cur.execute(
        "UPDATE card_restrictions SET status = 'removed', resolved_at = %s "
        "WHERE restriction_id = %s",
        (now, restriction["restriction_id"]),
    )
    cur.execute(
        "UPDATE restriction_transactions SET confirmed_at = %s WHERE restriction_id = %s",
        (now, restriction["restriction_id"]),
    )

    available = card["available_credit"]
    for row in linked:
        if row["kind"] == "authorization" and row["settlement_state"] == "pending":
            # A hold the review was sitting on settles once its activity is
            # confirmed, so it stops being an outstanding authorization.
            cur.execute(
                "UPDATE transactions SET settlement_state = 'settled' "
                "WHERE transaction_id = %s",
                (row["transaction_id"],),
            )
        elif row["kind"] == "decline" and row["represented_as"] is None:
            # A confirmed attempt the review declined is re-presented: the bank
            # pre-approves the amount so the merchant's next attempt carries an
            # approval instead of the same block. Everything that belongs to the
            # merchant's submission rather than to the pre-approval is left
            # unset: the time it is submitted and the location it is submitted
            # from are not known until the merchant submits, which happens
            # outside every tool here.
            #
            # A hold has to be covered in full by the line, so an attempt the
            # remaining credit cannot cover stays declined even though the
            # customer confirmed it.
            if available < row["amount"]:
                continue
            new_id = scenario_id(cur, "next_represented_transaction_id",
                                 "transactions", "transaction_id") or _unique_id(
                cur, "transactions", "transaction_id",
                f"{row['merchant_key']}-authorization-{as_amount(row['amount'])}")
            cur.execute(
                """
                INSERT INTO transactions
                    (transaction_id, card_id, kind, merchant_key, merchant,
                     amount, currency, status, settlement_state)
                SELECT %s, card_id, 'authorization', merchant_key, merchant,
                       amount, currency, 'approved', 'pending'
                  FROM transactions WHERE transaction_id = %s
                """,
                (new_id, row["transaction_id"]),
            )
            cur.execute(
                "UPDATE transactions SET represented_as = %s WHERE transaction_id = %s",
                (new_id, row["transaction_id"]),
            )
            available = available - row["amount"]
            cur.execute(
                "UPDATE card_accounts SET available_credit = %s WHERE card_id = %s",
                (available, card["card_id"]),
            )

    still_open = one(
        cur,
        "SELECT 1 FROM card_restrictions WHERE card_id = %s AND status = 'open'",
        (card["card_id"],),
    )
    card_status = "temporarily_restricted" if still_open else "active"
    cur.execute("UPDATE card_accounts SET status = %s WHERE card_id = %s",
                (card_status, card["card_id"]))
    # The record carries the resolution time; this tool reports the outcome and
    # the resulting card status, which is what the agent may state.
    return {"status": "removed", "card_status": card_status}


def create_travel_notice(cur, args) -> dict:
    card = _card(cur, args["customer_id"], args.get("card_last4"))
    customer = _customer(cur, args["customer_id"])
    notice_id = scenario_id(cur, "next_travel_notice_id",
                            "travel_notices", "notice_id") or _unique_id(
        cur, "travel_notices", "notice_id",
        f"travel-notice-{customer['notice_slug']}"
        f"-{destination_slug(args['destinations'][0])}")
    cur.execute(
        """
        INSERT INTO travel_notices
            (notice_id, card_id, destinations, return_date, authorization_guaranteed,
             status, created_at)
        VALUES (%s, %s, %s, %s, FALSE, 'created', %s)
        """,
        (notice_id, card["card_id"], args["destinations"], args.get("return_date"),
         scenario_value(cur, "scenario_time")),
    )
    stored = one(
        cur,
        "SELECT authorization_guaranteed FROM travel_notices WHERE notice_id = %s",
        (notice_id,),
    )
    return {
        "status": "created",
        "notice_id": notice_id,
        "authorization_guaranteed": stored["authorization_guaranteed"],
    }


# ---------------------------------------------------------------------------
# referrals and posted transactions
# ---------------------------------------------------------------------------


def get_referrals(cur, args) -> dict:
    _customer(cur, args["customer_id"])
    clauses = ["referring_customer_id = %s"]
    params: list = [args["customer_id"]]
    if args.get("referral_id"):
        clauses.append("referral_id = %s")
        params.append(args["referral_id"])
    rows = all_rows(
        cur,
        f"""
        SELECT referral_id, invited_at_display, invited_channel, invited_masked,
               application_status, qualification_status, offer
          FROM referrals
         WHERE {' AND '.join(clauses)}
         ORDER BY display_rank, referral_id
        """,
        tuple(params),
    )
    return {"referrals": [
        compact([
            ("referral_id", r["referral_id"]),
            # Emitted verbatim: the customer hears 'August 2', not an ISO date.
            ("invited_at", r["invited_at_display"]),
            ("invited_contact",
             {"channel": r["invited_channel"], "masked": r["invited_masked"]}
             if r["invited_channel"] and r["invited_masked"] else None),
            ("application_status", r["application_status"]),
            ("qualification_status", r["qualification_status"]),
            ("offer", r["offer"]),
        ])
        for r in rows
    ]}


def get_credit_card_transactions(cur, args) -> dict:
    _customer(cur, args["customer_id"])
    clauses = ["a.customer_id = %s", "t.kind = 'posted'"]
    params: list = [args["customer_id"]]
    if args.get("card_last4"):
        clauses.append("a.card_last4 = %s")
        params.append(args["card_last4"])
    if args.get("amount") is not None:
        clauses.append("t.amount = %s")
        params.append(args["amount"])
    if args.get("descriptor_contains"):
        clauses.append("coalesce(t.descriptor, '') ILIKE %s")
        params.append(f"%{args['descriptor_contains']}%")
    posted = args.get("posted_date")
    if posted:
        try:
            resolved = dt.date.fromisoformat(posted)
        except ValueError:
            # The registry allows a customer-relative date such as 'Monday'.
            # Without a calendar mapping that does not resolve to a posting date,
            # so it leaves the search unnarrowed instead of excluding everything.
            resolved = None
        if resolved is not None:
            clauses.append("t.posted_date = %s")
            params.append(resolved.isoformat())

    rows = all_rows(
        cur,
        f"""
        SELECT t.transaction_id, a.card_last4, t.amount, t.currency, t.category,
               t.preceded_by_authorization_amount
          FROM transactions t
          JOIN card_accounts a ON a.card_id = t.card_id
         WHERE {' AND '.join(clauses)}
         ORDER BY t.posted_date DESC, t.record_seq
        """,
        tuple(params),
    )
    # Statement amounts are decimal money and render as JSON floats, which is how
    # the recording carries both 243.18 and the 1.0 pre-authorization that
    # preceded it.
    return {"transactions": [
        compact([
            ("transaction_id", r["transaction_id"]),
            ("card_last4", r["card_last4"]),
            ("amount", as_float(r["amount"])),
            ("currency", r["currency"]),
            ("category", r["category"]),
            ("preceded_by_authorization_amount",
             as_float(r["preceded_by_authorization_amount"])),
        ])
        for r in rows
    ]}


# ---------------------------------------------------------------------------
# secure self-service and notifications
# ---------------------------------------------------------------------------


def _resource(cur, workflow: str, customer_id: str, resource_id: str) -> dict:
    """Check the resource exists, belongs to the customer, and is usable.

    A session is scoped to a real product, referral, or transaction; an
    identifier derived from a display name resolves to nothing here.
    """
    if workflow == "card_application":
        row = one(
            cur,
            "SELECT product AS label FROM card_products "
            "WHERE product_id = %s AND active",
            (resource_id,),
        )
        if row is None:
            raise NotFound(f"unknown or withdrawn card product {resource_id!r}")
        return {"label": row["label"], "short_ref": None}
    if workflow == "referral_status":
        row = one(
            cur,
            "SELECT referral_id FROM referrals "
            "WHERE referral_id = %s AND referring_customer_id = %s",
            (resource_id, customer_id),
        )
        if row is None:
            raise NotFound(f"no referral {resource_id!r} for this customer")
        return {"label": f"referral {resource_id}", "short_ref": None}
    row = one(
        cur,
        """
        SELECT t.resource_label, t.short_ref
          FROM transactions t
          JOIN card_accounts a ON a.card_id = t.card_id
         WHERE t.transaction_id = %s AND a.customer_id = %s
        """,
        (resource_id, customer_id),
    )
    if row is None:
        raise NotFound(f"no transaction {resource_id!r} on this profile")
    return {"label": row["resource_label"], "short_ref": row["short_ref"]}


def create_secure_self_service_session(cur, args) -> dict:
    customer = _customer(cur, args["customer_id"])
    profile = one(cur, "SELECT * FROM workflow_profiles WHERE workflow = %s",
                  (args["workflow"],))
    if profile is None:
        raise NotFound(f"unsupported workflow {args['workflow']!r}")

    resource = _resource(cur, args["workflow"], customer["customer_id"],
                         args["resource_id"])
    source = profile["resource_suffix_source"]
    if source == "resource_id":
        suffix = f"-{args['resource_id']}"
    elif source == "resource_short_ref":
        suffix = f"-{resource['short_ref'] or args['resource_id']}"
    else:
        suffix = ""
    session_id = scenario_id(cur, "next_self_service_session_id",
                             "self_service_sessions", "session_id") or _unique_id(
        cur, "self_service_sessions", "session_id",
        f"session-{profile['session_slug']}{suffix}")

    label = None
    if profile["display_label_template"]:
        label = profile["display_label_template"].replace(
            "{resource_label}", resource["label"] or args["resource_id"])

    now = scenario_value(cur, "scenario_time")
    cur.execute(
        """
        INSERT INTO self_service_sessions
            (session_id, customer_id, workflow, resource_id, status, submitted,
             resume_supported, save_and_continue, credit_pull_authorized,
             claim_tracked, claim_id, access_location, display_label,
             allowed_customer_actions, visible_stages, customer_opens, issued_at)
        VALUES (%s, %s, %s, %s, 'issued', FALSE, %s, %s, %s, %s, NULL, %s, %s, %s,
                %s, TRUE, %s)
        """,
        (session_id, customer["customer_id"], args["workflow"], args["resource_id"],
         profile["resume_supported"], profile["save_and_continue"],
         profile["credit_pull_authorized"], profile["claim_tracked"],
         profile["access_location"], label, profile["allowed_customer_actions"],
         profile["visible_stages"], now),
    )

    deliveries = []
    for rank, channel in enumerate(args["delivery_channels"]):
        spec = one(cur, "SELECT * FROM delivery_channels WHERE channel = %s", (channel,))
        if spec is None:
            raise NotFound(f"unsupported delivery channel {channel!r}")
        masked = None
        if spec["destination_source"] == "notification_email":
            masked = mask_email(customer["notification_email"])
            if masked is None:
                # Policy requires an authorized destination. A profile with no
                # notification address has none, so the delivery is refused
                # rather than reported as delivered to nowhere.
                raise ToolRefusal("profile has no notification email to deliver to")
        cur.execute(
            """
            INSERT INTO session_deliveries
                (session_id, channel, delivery_rank, status, masked_destination)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (session_id, channel, rank, spec["delivered_status"], masked),
        )
        deliveries.append(compact([
            ("channel", channel),
            ("status", spec["delivered_status"]),
            ("masked_destination", masked),
        ]))

    # A NULL column on the workflow profile means the field is not part of that
    # workflow's surface, which is why a card application reports
    # save_and_continue and a dispute reports claim_id.
    result = compact([
        ("session_id", session_id),
        ("status", "issued"),
        ("submitted", False),
        ("credit_pull_authorized", profile["credit_pull_authorized"]),
        ("save_and_continue", profile["save_and_continue"]),
        ("visible_stages", as_list_always(profile["visible_stages"])
            if profile["visible_stages"] is not None else None),
        ("access_location", profile["access_location"]),
        ("display_label", label),
        ("allowed_customer_actions", as_list_always(profile["allowed_customer_actions"])
            if profile["allowed_customer_actions"] is not None else None),
        ("deliveries", deliveries),
    ])
    # Null is the value here, not an absence: a dispute session reports that no
    # claim reference exists yet, which differs from not tracking one at all.
    if profile["claim_tracked"]:
        result["claim_id"] = None
    return result


def get_secure_self_service_session(cur, args) -> dict:
    session = one(
        cur,
        "SELECT * FROM self_service_sessions WHERE session_id = %s AND customer_id = %s",
        (args["session_id"], args["customer_id"]),
    )
    if session is None:
        raise NotFound(f"unknown session {args['session_id']!r} for this customer")

    status = session["status"]
    opened_at = session["opened_at"]
    if status == "issued":
        if _is_expired(cur, session["expires_at"]):
            status = "expired"
        elif session["customer_opens"]:
            # Opening happens in online banking, outside every tool. The session
            # records whether its owner opens it, so the first read after
            # delivery writes the transition it observes, and a session nobody
            # opened keeps reading 'issued'.
            status = "open_not_submitted"
            opened_at = scenario_value(cur, "scenario_time")
        cur.execute(
            "UPDATE self_service_sessions SET status = %s, opened_at = %s "
            "WHERE session_id = %s",
            (status, opened_at, session["session_id"]),
        )

    result = compact([
        ("session_id", session["session_id"]),
        ("status", status),
        ("submitted", session["submitted"]),
        ("resume_supported", session["resume_supported"]),
        ("save_and_continue", session["save_and_continue"]),
        ("credit_pull_authorized", session["credit_pull_authorized"]),
    ])
    if session["claim_tracked"]:
        result["claim_id"] = session["claim_id"]
    return result


def send_secure_notification(cur, args) -> dict:
    customer = _customer(cur, args["customer_id"])
    template = one(cur, "SELECT * FROM notification_templates WHERE template = %s",
                   (args["template"],))
    if template is None:
        raise NotFound(f"unapproved notification template {args['template']!r}")
    if template["channel"] != args["channel"]:
        raise ToolRefusal("template is not approved for that channel",
                          {"template_channel": template["channel"]})

    resource_id = args["related_resource_id"]
    known = one(
        cur,
        """
        SELECT 1 FROM self_service_sessions
         WHERE session_id = %s AND customer_id = %s
        UNION ALL
        SELECT 1 FROM referrals
         WHERE referral_id = %s AND referring_customer_id = %s
        """,
        (resource_id, customer["customer_id"], resource_id, customer["customer_id"]),
    )
    if known is None:
        raise NotFound(f"no secure resource {resource_id!r} for this customer")

    if args["channel"] == "email":
        masked = mask_email(customer["notification_email"])
    else:
        channel = one(
            cur,
            "SELECT masked_destination FROM trusted_channels "
            "WHERE customer_id = %s AND type = 'sms' AND enrolled ORDER BY channel_id "
            "LIMIT 1",
            (customer["customer_id"],),
        )
        masked = channel["masked_destination"] if channel else None
    if masked is None:
        raise ToolRefusal(f"profile has no {args['channel']} destination on file")

    # A notification is named after the secure resource it points at, so the
    # audit trail links the two without a second lookup.
    notification_id = scenario_id(cur, "next_notification_id",
                                  "notifications", "notification_id") or _unique_id(
        cur, "notifications", "notification_id",
        "notification-" + re.sub(r"^session-", "", resource_id))
    cur.execute(
        """
        INSERT INTO notifications
            (notification_id, customer_id, related_resource_id, channel, template,
             status, masked_destination, contains_working_secure_link, sent_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (notification_id, customer["customer_id"], resource_id, args["channel"],
         args["template"], template["status_on_send"], masked,
         template["contains_working_secure_link"],
         scenario_value(cur, "scenario_time")),
    )
    return {
        "notification_id": notification_id,
        "status": template["status_on_send"],
        "masked_destination": masked,
        "contains_working_secure_link": template["contains_working_secure_link"],
    }


def transfer_to_specialist(cur, args) -> dict:
    transfer_id = allocate_id(cur, "specialist_transfer")
    cur.execute(
        """
        INSERT INTO specialist_transfers (transfer_id, reason, summary, status, created_at)
        VALUES (%s, %s, %s, 'initiated', %s)
        """,
        (transfer_id, args["reason"], args["summary"],
         scenario_value(cur, "scenario_time")),
    )
    return {"status": "initiated", "transfer_id": transfer_id}


HANDLERS = {
    "lookup_customer": lookup_customer,
    "get_current_time": get_current_time,
    "verify_customer_identity": verify_customer_identity,
    "start_trusted_channel_confirmation": start_trusted_channel_confirmation,
    "get_trusted_channel_confirmation": get_trusted_channel_confirmation,
    "update_customer_email": update_customer_email,
    "search_knowledge_base": search_knowledge_base,
    "get_card_account": get_card_account,
    "resolve_card_restriction": resolve_card_restriction,
    "create_travel_notice": create_travel_notice,
    "get_referrals": get_referrals,
    "get_credit_card_transactions": get_credit_card_transactions,
    "create_secure_self_service_session": create_secure_self_service_session,
    "get_secure_self_service_session": get_secure_self_service_session,
    "send_secure_notification": send_secure_notification,
    "transfer_to_specialist": transfer_to_specialist,
}

# Tools that change the bank's records. Membership follows what a handler does to
# the database, not what its name suggests: get_trusted_channel_confirmation and
# get_secure_self_service_session both issue an UPDATE, but all either writes is
# the lifecycle marker for something the customer did outside every tool here -
# completing a challenge, opening a session - so they are reads that record an
# observation, and the two columns each one advances are dropped from the damage
# check's row hash instead, which leaves the tables themselves checked so that a
# session or a challenge nobody asked for is still caught. get_card_account is a
# blunter case one level down: it advances card_section_read_cursor and nothing
# else, and that table is excluded whole.
WRITE_TOOLS = {
    "verify_customer_identity",
    "start_trusted_channel_confirmation",
    "update_customer_email",
    "resolve_card_restriction",
    "create_travel_notice",
    "create_secure_self_service_session",
    "send_secure_notification",
    "transfer_to_specialist",
}
