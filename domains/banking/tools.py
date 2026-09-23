"""Banking tools over the conversation's JSON database.

Ported from the PostgreSQL tool server in PR #17. Each tool is (db, args) -> result
and may mutate db in place; see env/toolkit.py for the row layout and helpers.

Tools hold domain logic only. Every masked destination, amount, status, and
product term that appears in a result is read from the database or derived from a
column by a stated rule. A new record's identifier comes from the environment's
seeded generator (toolkit.new_id) and its timestamps from the call clock
(toolkit.now), the way a real service reads its id generator and its clock, so a
replay is still reproducible.

Reads never write. What other people do while the call is in progress (the
customer entering a code or opening a link, a merchant retrying a charge, an
authorization clearing) is not a side effect of any tool: each conversation lists
those as scheduled events in state/events.json, applied by the runtime at their
own time.

The original server ran on a database created with the C.UTF-8 locale, so text
compares and sorts by code point, which is how Python compares str. Where it
matched text case-insensitively it used SQL lower(), ILIKE, and the ~* regular
expression operator; `_sql_lower`, `_ilike`, and `_kb_pattern` reproduce those.
Money columns are NUMERIC in the original and floats in this database, so
arithmetic and comparisons on them go through Decimal.
"""
from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal

from toolkit import (NotFound, Refusal, ToolError, as_float, as_list_always, compact,
                     first, insert, new_id, now, now_iso, rows, scenario_value)

# Key columns per table: a row's key in db[table] is these columns joined by "|".
KEY_COLUMNS = {
    "card_accounts": ["card_id"],
    "card_products": ["product_id"],
    "card_restrictions": ["restriction_id"],
    "channel_confirmations": ["confirmation_id"],
    "customers": ["customer_id"],
    "delivery_channels": ["channel"],
    "identity_verifications": ["verification_id"],
    "kb_records": ["record_id"],
    "notification_templates": ["template"],
    "notifications": ["notification_id"],
    "referrals": ["referral_id"],
    "restriction_transactions": ["restriction_id", "transaction_id"],
    "scenario": ["key"],
    "self_service_sessions": ["session_id"],
    "service_cases": ["case_id"],
    "session_deliveries": ["session_id", "channel"],
    "specialist_transfers": ["transfer_id"],
    "transactions": ["transaction_id"],
    "travel_notices": ["notice_id"],
    "trusted_channels": ["channel_id"],
    "welcome_offers": ["offer_id"],
    "workflow_profiles": ["workflow"],
}

MONTH_NUMBERS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}

# Card sections in the order the registry declares them on the result.
SECTION_ORDER = ("status", "available_credit", "authorizations", "declines",
                 "restrictions", "travel_notices")

# The fields each ledger-backed section returns: every field the registry declares
# for its items.
LEDGER_SECTION_FIELDS = {
    "authorizations": ("transaction_id", "merchant", "merchant_location", "amount",
                       "currency", "status", "occurred_at"),
    "declines": ("transaction_id", "merchant", "merchant_location", "amount",
                 "currency", "status", "reason"),
}


class DatabaseError(ToolError):
    """A constraint the original database enforced itself, reported as it did."""
    status = 500
    kind = "database_error"


# ---------------------------------------------------------------------------
# SQL semantics
# ---------------------------------------------------------------------------


def _sql_lower(text: str) -> str:
    """SQL lower(): a per-character mapping, never changing the length.

    Python's str.lower() applies full Unicode case mapping, under which one
    character (U+0130, capital I with dot above) lowers to two and a final
    sigma lowers differently from any other. The database lowers each character
    on its own, and U+0130 to a plain "i".
    """
    return "".join(ch.lower() if len(ch.lower()) == 1 else "i" for ch in text)


def _like(value: str, pattern: str) -> bool:
    """`value LIKE pattern` with the default backslash escape.

    `%` matches any run of characters, `_` any one character, and a backslash
    makes the next character literal.
    """
    regex, i = [], 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\":
            if i + 1 == len(pattern):
                # Unreachable from the tools here, whose patterns always end in
                # "%", but kept so the matcher never silently accepts it.
                raise ValueError("LIKE pattern must not end with escape character")
            regex.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        regex.append(".*" if ch == "%" else "." if ch == "_" else re.escape(ch))
        i += 1
    return re.fullmatch("".join(regex), value, re.DOTALL) is not None


def _ilike(value: str, pattern: str) -> bool:
    """`value ILIKE pattern`: LIKE after SQL lower() on both sides, as the
    database does for a multibyte encoding."""
    return _like(_sql_lower(value), _sql_lower(pattern))


def _case_variants(ch: str) -> str:
    """The character with its one-character lower and upper forms."""
    variants = {ch} | {v for v in (ch.lower(), ch.upper()) if len(v) == 1}
    return "".join(sorted(variants))


def _kb_pattern(pattern: str) -> re.Pattern:
    """Compile a kb_records.query_pattern as the database's `~*` reads it.

    The patterns are POSIX advanced regular expressions. The subset the records
    use (literals, groups, alternation, `.`, `?`, `*`, and the `\\y` word
    boundary) means the same thing to Python once `\\y` is spelled `\\b`, with
    two differences in how the operator applies them. `.` also matches a
    newline. And case-insensitivity matches a pattern character against itself
    and its lower- and upper-case forms only, which is narrower than Python's
    IGNORECASE (under which 's' also matches the long s 'ſ'), so each letter is
    expanded to that set instead.
    """
    out, i, bracket = [], 0, False
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\" and i + 1 < len(pattern):
            escaped = pattern[i + 1]
            out.append({"y": r"\b", "m": r"\b(?=\w)", "M": r"\b(?<=\w)"}.get(
                escaped, "\\" + escaped))
            i += 2
            continue
        if bracket:
            # A bracket expression folds case as a whole.
            out.append(ch)
            bracket = ch != "]"
            if not bracket:
                out.append(")")
        elif ch == "[":
            out.append("(?i:[")
            bracket = True
        else:
            variants = _case_variants(ch)
            out.append(f"[{re.escape(variants)}]" if len(variants) > 1 else ch)
        i += 1
    return re.compile("".join(out), re.DOTALL)


def _decimal(value) -> Decimal | None:
    """A NUMERIC column or argument as the exact decimal the database held."""
    return None if value is None else Decimal(str(value))


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
    amount = _decimal(value)
    if amount is None:
        return None
    return int(amount) if amount == amount.to_integral_value() else float(amount)


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


def _parse_iso(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def _is_expired(db, expires_at) -> bool:
    """True when the call clock has passed a stored expiry."""
    deadline = _parse_iso(expires_at)
    if deadline is None:
        return False
    return now(db) > deadline


def _plus_minutes(stamp, minutes: int):
    parsed = _parse_iso(stamp)
    if parsed is None:
        return None
    return (parsed + dt.timedelta(minutes=minutes)).isoformat()


# ---------------------------------------------------------------------------
# customer resolution and verification
# ---------------------------------------------------------------------------


def lookup_customer(db, args) -> dict:
    identifiers = ("account_id", "email", "full_name")
    corroborating = ("billing_zip", "card_last4", "account_match")
    if not any(args.get(key) for key in identifiers + corroborating):
        raise Refusal("a lookup needs at least one identifier")

    # Registry precedence: account_id, then email, then full_name. The lower
    # precedence identifiers are not also applied, so a caller who gives an
    # account id and misremembers the name still resolves.
    resolved_by = None
    if args.get("account_id"):
        resolved_by = "account_id"
        candidates = (rows(db, "customers", account_id=args["account_id"])
                      + rows(db, "customers", customer_id=args["account_id"]))
    elif args.get("email"):
        resolved_by = "email"
        email = _sql_lower(args["email"])
        candidates = [c for c in db["customers"].values()
                      if any(address is not None and _sql_lower(address) == email
                             for address in (c["notification_email"], c["primary_email"]))]
    elif args.get("full_name"):
        resolved_by = "full_name"
        name = _sql_lower(args["full_name"].strip())
        candidates = [c for c in db["customers"].values()
                      if _sql_lower(c["full_name"]) == name]
    else:
        candidates = list(db["customers"].values())

    matches = []
    for customer in {c["customer_id"]: c for c in candidates}.values():
        if args.get("billing_zip") and customer["billing_zip"] != args["billing_zip"]:
            continue
        if args.get("card_last4") and first(db, "card_accounts",
                                            customer_id=customer["customer_id"],
                                            card_last4=args["card_last4"]) is None:
            continue
        if args.get("account_match") == "caller_phone" and not customer["caller_channel_match"]:
            continue
        matches.append(customer)
    matches.sort(key=lambda c: c["customer_id"])
    if not matches:
        raise NotFound("no profile matches the supplied identifiers")
    if len(matches) > 1:
        # Policy forbids continuing on an ambiguous profile, so the ambiguity is
        # reported rather than resolved by taking the first row.
        raise Refusal(
            "more than one profile matches; a narrower identifier is required",
            {"candidate_count": len(matches)},
        )

    customer = matches[0]
    channels = _enrolled_channels(db, customer["customer_id"])
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


def get_current_time(db, args) -> dict:
    status = scenario_value(db, "time_status") or "available"
    if status != "available":
        return {"status": status}
    return {
        "status": "available",
        "timestamp": now_iso(db),
        "timezone": scenario_value(db, "timezone"),
    }


def _customer(db, customer_id: str) -> dict:
    row = db["customers"].get(customer_id)
    if row is None:
        raise NotFound(f"unknown customer {customer_id!r}")
    return row


def _enrolled_channels(db, customer_id: str, **where) -> list:
    """The customer's enrolled trusted channels, in channel_id order."""
    return sorted(rows(db, "trusted_channels", customer_id=customer_id, **where,
                       enrolled=True),
                  key=lambda c: c["channel_id"])


def _matched_factors(db, customer: dict, args) -> set:
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
    if args.get("card_last4") and first(db, "card_accounts",
                                        customer_id=customer["customer_id"],
                                        card_last4=args["card_last4"]):
        matched.add("card_last4")
    supplied_birthday = args.get("birth_month_day")
    if supplied_birthday:
        month_name, _, day = supplied_birthday.partition(" ")
        if (MONTH_NUMBERS.get(month_name.lower()) == customer["birth_month"]
                and day.isdigit() and int(day) == customer["birth_day"]):
            matched.add("birth_month_day")
    return matched


def verify_customer_identity(db, args) -> dict:
    customer = _customer(db, args["customer_id"])
    required = list(customer["required_verification_methods"])
    matched = _matched_factors(db, customer, args)
    # Emitted in the profile's required order, so the list reads as an answer to
    # the profile's requirement rather than as an echo of the arguments.
    matched_methods = [factor for factor in required if factor in matched]
    verified = all(factor in matched for factor in required)

    stamp = now_iso(db)
    # A verification record is scoped to the reason the profile is in contact.
    # Re-verifying inside one open case updates that case's record rather than
    # minting a second record for the same piece of work; a profile with no open
    # case gets a new record per attempt.
    case = first(db, "service_cases", customer_id=customer["customer_id"], status="open")
    existing = case and first(db, "identity_verifications",
                              customer_id=customer["customer_id"], case_id=case["case_id"])
    outcome = {
        "status": "verified" if verified else "unverified",
        "matched_methods": matched_methods,
        "verified_at": stamp if verified else None,
        "expires_at": _plus_minutes(stamp, 30) if verified else None,
    }
    if existing:
        verification_id = existing["verification_id"]
        existing.update(outcome)
    else:
        verification_id = new_id(db, "identity_verification")
        insert(db, "identity_verifications", {
            "verification_id": verification_id,
            "customer_id": customer["customer_id"],
            "case_id": case["case_id"] if case else None,
            "required_methods": required,
            **outcome,
        }, KEY_COLUMNS)

    return compact([
        ("verification_id", verification_id),
        ("status", outcome["status"]),
        ("matched_methods", as_list_always(matched_methods)),
        # The caller may assert the time it observed. The backend's own record
        # time is the authoritative one, so an asserted time is answered with
        # the time the record actually carries; a caller who asserted nothing is
        # given nothing to reconcile.
        ("verified_at", outcome["verified_at"] if args.get("verified_at") else None),
    ])


def _active_verification(db, customer_id: str, verification_id: str) -> dict:
    row = first(db, "identity_verifications", verification_id=verification_id,
                customer_id=customer_id)
    if row is None:
        raise NotFound(f"unknown verification {verification_id!r} for this customer")
    if row["status"] != "verified":
        raise Refusal("identity verification is not in a verified state",
                      {"verification_status": row["status"]})
    if _is_expired(db, row["expires_at"]):
        raise Refusal("identity verification has expired")
    return row


# ---------------------------------------------------------------------------
# trusted-channel confirmation and profile email
# ---------------------------------------------------------------------------


def start_trusted_channel_confirmation(db, args) -> dict:
    customer = _customer(db, args["customer_id"])
    _active_verification(db, customer["customer_id"], args["verification_id"])

    channels = _enrolled_channels(db, customer["customer_id"], type=args["channel"])
    if not channels:
        raise Refusal(f"no enrolled {args['channel']} channel on this profile",
                      {"channel": args["channel"]})
    channel = channels[0]

    stamp = now_iso(db)
    purpose = args["purpose"]
    # Starting the same purpose again while a challenge is still outstanding
    # re-sends that challenge rather than opening a second one. It goes to the
    # channel named this time, so the record and the reply agree on where the
    # code went. A completed or expired challenge is not reopened.
    pending = [c for c in rows(db, "channel_confirmations",
                               customer_id=customer["customer_id"], purpose=purpose,
                               status="sent")
               if not _is_expired(db, c["expires_at"])]
    challenge = {
        "status": "sent",
        "verified_at": None,
        "sent_at": stamp,
        "channel_id": channel["channel_id"],
        "masked_destination": channel["masked_destination"],
        "expires_at": _plus_minutes(stamp, 15),
        "verification_id": args["verification_id"],
    }
    if pending:
        confirmation_id = pending[0]["confirmation_id"]
        pending[0].update(challenge)
    else:
        confirmation_id = new_id(db, "channel_confirmation")
        insert(db, "channel_confirmations", {
            "confirmation_id": confirmation_id,
            "customer_id": customer["customer_id"],
            "purpose": purpose,
            **challenge,
        }, KEY_COLUMNS)
    # The record carries an expiry and the mutation path enforces it, but this
    # bank does not disclose challenge expiry to the agent: only the delivery
    # state and the masked destination are reported.
    return {
        "confirmation_id": confirmation_id,
        "status": "sent",
        "masked_destination": channel["masked_destination"],
    }


def get_trusted_channel_confirmation(db, args) -> dict:
    row = first(db, "channel_confirmations", confirmation_id=args["confirmation_id"],
                customer_id=args["customer_id"])
    channel = row and db["trusted_channels"].get(row["channel_id"])
    if channel is None:
        raise NotFound(f"unknown confirmation {args['confirmation_id']!r}")

    # The challenge moves to 'verified' only when the customer answers it
    # through the secure path, which the OTP service records on the row. This
    # read reports the row; an outstanding challenge past its expiry reads as
    # expired without the read changing anything.
    status, verified_at = row["status"], row["verified_at"]
    if status in ("requested", "sent", "delivered") and _is_expired(db, row["expires_at"]):
        status, verified_at = "expired", None

    return compact([
        ("confirmation_id", row["confirmation_id"]),
        ("status", status),
        ("verified_at", verified_at),
    ])


def update_customer_email(db, args) -> dict:
    customer = _customer(db, args["customer_id"])
    _active_verification(db, customer["customer_id"], args["verification_id"])

    confirmation = first(db, "channel_confirmations", confirmation_id=args["confirmation_id"],
                         customer_id=customer["customer_id"])
    if confirmation is None:
        raise NotFound(f"unknown confirmation {args['confirmation_id']!r}")
    if confirmation["purpose"] != "email_change":
        raise Refusal("confirmation was not issued for an email change",
                      {"purpose": confirmation["purpose"]})
    if confirmation["status"] != "verified":
        raise Refusal("trusted-channel confirmation is not verified",
                      {"confirmation_status": confirmation["status"]})

    had_prior_address = bool(customer["primary_email"])
    customer["primary_email"] = args["new_email"]
    customer["notification_email"] = args["new_email"]
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


def _travel_card_matches(db, _record) -> dict:
    products = sorted(rows(db, "card_products", category="travel", active=True),
                      key=lambda p: (p["display_rank"], p["product_id"]))
    return {"matches": [
        compact([
            ("product_id", p["product_id"]),
            ("product", p["product"]),
            ("annual_fee", as_amount(p["annual_fee"])),
            ("annual_fee_currency", p["annual_fee_currency"]),
            ("foreign_transaction_fee", p["foreign_transaction_fee"]),
            ("lounge_membership", p["lounge_membership"]),
        ])
        for p in products
    ]}


def _welcome_offers(db, _record) -> dict:
    offers = []
    for offer in rows(db, "welcome_offers", active=True):
        product = db["card_products"].get(offer["product_id"])
        if product and product["active"] and product["category"] == "travel":
            offers.append((offer, product))
    offers.sort(key=lambda pair: (pair[0]["display_rank"], pair[0]["offer_id"]))
    return {"offers": [
        compact([
            ("product_id", offer["product_id"]),
            ("product", product["product"]),
            ("points", offer["points"]),
            ("spend", as_amount(offer["spend"])),
            ("spend_currency", offer["spend_currency"]),
            ("days", offer["days"]),
        ])
        for offer, product in offers
    ]}


PROJECTIONS = {
    "travel_card_matches": _travel_card_matches,
    "welcome_offers": _welcome_offers,
}


def search_knowledge_base(db, args) -> dict:
    # The best-matching record wins on priority, then on how specific its pattern
    # is, then on identifier, so retrieval never depends on physical row order,
    # and a query that matches nothing is reported rather than guessed at.
    matching = [r for r in db["kb_records"].values()
                if _kb_pattern(r["query_pattern"]).search(args["query"])]
    if not matching:
        raise NotFound("no knowledge-base record answers that query")
    record = min(matching, key=lambda r: (-r["priority"], -len(r["query_pattern"]),
                                          r["record_id"]))

    # A record is a knowledge article: a title and prose content, written as the
    # bank's own documentation rather than as an answer to the query that found
    # it. A record that is a view of the product catalog assembles its rows from
    # the catalog, so a product term the recording never asked about still answers
    # from the same rows; its content, when it has any, is the terms text that
    # goes with those rows.
    result: dict = {
        "record_id": record["record_id"],
        "title": record["title"],
        "effective_at": record["effective_at"],
    }
    if record["projection"]:
        result.update(PROJECTIONS[record["projection"]](db, record))
    if record["content"] is not None:
        result["content"] = record["content"]
    return result


# ---------------------------------------------------------------------------
# card account
# ---------------------------------------------------------------------------


def _card(db, customer_id: str, card_last4) -> dict:
    _customer(db, customer_id)
    if card_last4:
        row = first(db, "card_accounts", customer_id=customer_id, card_last4=card_last4)
        if row is None:
            raise NotFound(f"no card ending {card_last4} on this profile")
        return row
    cards = sorted(rows(db, "card_accounts", customer_id=customer_id),
                   key=lambda c: c["card_id"])
    if not cards:
        raise NotFound("no card account on this profile")
    if len(cards) > 1:
        raise Refusal("profile holds more than one card; card_last4 is required",
                      {"card_count": len(cards)})
    return cards[0]


def _ledger_section(db, card: dict, section: str, **where) -> list:
    """Rows of one transaction-backed section, in ledger order."""
    ledger = sorted(rows(db, "transactions", card_id=card["card_id"], **where),
                    key=lambda t: t["record_seq"])
    return [
        compact([(name, as_amount(t[name]) if name == "amount" else t[name])
                 for name in LEDGER_SECTION_FIELDS[section]])
        for t in ledger
    ]


def _linked_transaction_ids(db, restriction_id: str) -> list:
    links = sorted(rows(db, "restriction_transactions", restriction_id=restriction_id),
                   key=lambda link: (link["link_rank"], link["transaction_id"]))
    return [link["transaction_id"] for link in links]


def get_card_account(db, args) -> dict:
    card = _card(db, args["customer_id"], args.get("card_last4"))
    requested = [s for s in SECTION_ORDER if s in (args.get("include") or [])]

    result: dict = {"customer_id": card["customer_id"],
                    "card_last4": card["card_last4"]}

    if "status" in requested:
        for name in ("status", "reported_lost", "payment_status"):
            result[name] = card[name]

    if "available_credit" in requested:
        result["available_credit"] = as_amount(card["available_credit"])
        result["available_credit_currency"] = card["available_credit_currency"]

    if "authorizations" in requested:
        result["authorizations"] = _ledger_section(
            db, card, "authorizations",
            kind="authorization", status="approved", settlement_state="pending")

    if "declines" in requested:
        result["declines"] = _ledger_section(db, card, "declines", kind="decline")

    if "restrictions" in requested:
        restrictions = sorted(rows(db, "card_restrictions", card_id=card["card_id"]),
                              key=lambda r: (r["opened_at"], r["restriction_id"]))
        result["restrictions"] = [
            compact([
                ("restriction_id", r["restriction_id"]),
                ("status", r["status"]),
                ("linked_transaction_ids",
                 _linked_transaction_ids(db, r["restriction_id"]) or None),
            ])
            for r in restrictions
        ]

    if "travel_notices" in requested:
        notices = sorted(rows(db, "travel_notices", card_id=card["card_id"], status="created"),
                         key=lambda n: (n["created_at"], n["notice_id"]))
        result["travel_notices"] = [
            compact([
                ("notice_id", n["notice_id"]),
                ("destinations", as_list_always(n["destinations"])),
                ("return_date", n["return_date"] or None),
            ])
            for n in notices
        ]

    return result


def resolve_card_restriction(db, args) -> dict:
    card = _card(db, args["customer_id"], args.get("card_last4"))
    restriction = first(db, "card_restrictions", restriction_id=args["restriction_id"],
                        card_id=card["card_id"])
    if restriction is None:
        raise NotFound(f"no restriction {args['restriction_id']!r} on this card")
    if restriction["status"] != "open":
        raise Refusal("restriction is not open",
                      {"restriction_status": restriction["status"]})
    if not restriction["customer_resolvable"]:
        # A delinquency hold or a lost-card block is not lifted by the customer
        # confirming activity, so the attempt is refused rather than succeeding
        # with a status the agent would read back as resolved.
        raise Refusal("this restriction is not resolved by confirming activity")

    confirmed = set(args["confirmed_transaction_ids"])
    unconfirmed = [transaction_id
                   for transaction_id in _linked_transaction_ids(db, restriction["restriction_id"])
                   if transaction_id not in confirmed]
    if unconfirmed:
        # Every activity the review holds has to be accounted for. Lifting the
        # review while some of it is unconfirmed would remove the control that
        # opened it.
        raise Refusal("some activity linked to this restriction was not confirmed",
                      {"unconfirmed_count": len(unconfirmed)})

    # Lifting the review lifts the block and nothing else. The ledger is left as
    # it is: a declined attempt stays declined, and a pending authorization stays
    # pending until it clears. A merchant that tries again after this submits a
    # new authorization through the card network, which arrives as its own ledger
    # row with its own time and holds credit only then.
    stamp = now_iso(db)
    restriction["status"], restriction["resolved_at"] = "removed", stamp
    for link in rows(db, "restriction_transactions",
                     restriction_id=restriction["restriction_id"]):
        link["confirmed_at"] = stamp

    still_open = first(db, "card_restrictions", card_id=card["card_id"], status="open")
    card_status = "temporarily_restricted" if still_open else "active"
    card["status"] = card_status
    # The record carries the resolution time; this tool reports the outcome and
    # the resulting card status, which is what the agent may state.
    return {"status": "removed", "card_status": card_status}


def create_travel_notice(db, args) -> dict:
    card = _card(db, args["customer_id"], args.get("card_last4"))
    notice_id = new_id(db, "travel_notice")
    # return_date is a DATE column, so the record holds the calendar date the
    # argument names in its canonical YYYY-MM-DD form.
    return_date = args.get("return_date")
    insert(db, "travel_notices", {
        "notice_id": notice_id,
        "card_id": card["card_id"],
        "destinations": list(args["destinations"]),
        "return_date": dt.date.fromisoformat(return_date).isoformat() if return_date else None,
        "status": "created",
        "created_at": now_iso(db),
    }, KEY_COLUMNS)
    # The record is what was created. What a notice does and does not do for
    # later authorizations is policy and knowledge-base content, not a field of it.
    return {"status": "created", "notice_id": notice_id}


# ---------------------------------------------------------------------------
# referrals and posted transactions
# ---------------------------------------------------------------------------


def get_referrals(db, args) -> dict:
    _customer(db, args["customer_id"])
    where = {"referring_customer_id": args["customer_id"]}
    if args.get("referral_id"):
        where["referral_id"] = args["referral_id"]
    referrals = sorted(rows(db, "referrals", **where),
                       key=lambda r: (r["display_rank"], r["referral_id"]))
    return {"referrals": [
        compact([
            ("referral_id", r["referral_id"]),
            ("reference_code", r["reference_code"]),
            ("invited_at", r["invited_on"]),
            ("invited_contact",
             {"channel": r["invited_channel"], "masked": r["invited_masked"]}
             if r["invited_channel"] and r["invited_masked"] else None),
            ("application_status", r["application_status"]),
            ("qualification_status", r["qualification_status"]),
            ("offer", r["offer"]),
            # The date itself, not whether it has passed: the agent compares it
            # with get_current_time.
            ("qualification_deadline", r["deadline_on"]),
        ])
        for r in referrals
    ]}


def get_credit_card_transactions(db, args) -> dict:
    _customer(db, args["customer_id"])
    card_where = {"customer_id": args["customer_id"]}
    if args.get("card_last4"):
        card_where["card_last4"] = args["card_last4"]
    posted = args.get("posted_date")
    resolved = None
    if posted:
        try:
            resolved = dt.date.fromisoformat(posted).isoformat()
        except ValueError:
            # The registry allows a customer-relative date such as 'Monday'.
            # Without a calendar mapping that does not resolve to a posting date,
            # so it leaves the search unnarrowed instead of excluding everything.
            resolved = None
    amount = _decimal(args.get("amount"))

    found = []
    for card in rows(db, "card_accounts", **card_where):
        for t in rows(db, "transactions", card_id=card["card_id"], kind="posted"):
            if amount is not None and _decimal(t["amount"]) != amount:
                continue
            if (args.get("descriptor_contains")
                    and not _ilike(t["descriptor"] or "", f"%{args['descriptor_contains']}%")):
                continue
            if resolved is not None and t["posted_date"] != resolved:
                continue
            found.append((t, card))
    # ORDER BY posted_date DESC, record_seq: newest posting first, where a
    # descending sort puts a missing date ahead of every date, then ledger order.
    found.sort(key=lambda pair: pair[0]["record_seq"])
    found.sort(key=lambda pair: (pair[0]["posted_date"] is None, pair[0]["posted_date"] or ""),
               reverse=True)
    # Statement amounts are decimal money and render as JSON floats, which is how
    # the recording carries 243.18 and the 1.0 authorization linked to it.
    return {"transactions": [
        compact([
            ("transaction_id", t["transaction_id"]),
            ("card_last4", card["card_last4"]),
            ("amount", as_float(t["amount"])),
            ("currency", t["currency"]),
            ("category", t["category"]),
            ("authorizations", _linked_authorizations(db, t["transaction_id"]) or None),
        ])
        for t, card in found
    ]}


def _linked_authorizations(db, transaction_id: str) -> list:
    """Authorization records the ledger links to a posted transaction.

    A merchant may authorize a small amount before it submits the charge, for
    example to check a saved card. That authorization is its own ledger row
    pointing at the posting through linked_transaction_id, and is reported as a
    row rather than summarised, so the agent reads it as it reads any other.
    """
    linked = sorted(rows(db, "transactions", kind="authorization",
                         linked_transaction_id=transaction_id),
                    key=lambda a: (a["occurred_at"] or "", a["record_seq"]))
    return [
        compact([
            ("transaction_id", a["transaction_id"]),
            ("amount", as_float(a["amount"])),
            ("currency", a["currency"]),
            ("status", a["status"]),
            ("occurred_at", a["occurred_at"]),
        ])
        for a in linked
    ]


# ---------------------------------------------------------------------------
# secure self-service and notifications
# ---------------------------------------------------------------------------


def _resource_label(db, workflow: str, customer_id: str, resource_id: str) -> str | None:
    """Check the resource exists, belongs to the customer, and is usable, and
    return the label a session shows for it.

    A session is scoped to a real product, referral, or transaction; an
    identifier derived from a display name resolves to nothing here.
    """
    if workflow == "card_application":
        product = first(db, "card_products", product_id=resource_id, active=True)
        if product is None:
            raise NotFound(f"unknown or withdrawn card product {resource_id!r}")
        return product["product"]
    if workflow == "referral_status":
        if first(db, "referrals", referral_id=resource_id,
                 referring_customer_id=customer_id) is None:
            raise NotFound(f"no referral {resource_id!r} for this customer")
        return f"referral {resource_id}"
    transaction = db["transactions"].get(resource_id)
    card = transaction and db["card_accounts"].get(transaction["card_id"])
    if card is None or card["customer_id"] != customer_id:
        raise NotFound(f"no transaction {resource_id!r} on this profile")
    return transaction["resource_label"]


def create_secure_self_service_session(db, args) -> dict:
    customer = _customer(db, args["customer_id"])
    profile = db["workflow_profiles"].get(args["workflow"])
    if profile is None:
        raise NotFound(f"unsupported workflow {args['workflow']!r}")

    resource_label = _resource_label(db, args["workflow"], customer["customer_id"],
                                     args["resource_id"])
    session_id = new_id(db, "self_service_session")

    label = None
    if profile["display_label_template"]:
        label = profile["display_label_template"].replace(
            "{resource_label}", resource_label or args["resource_id"])

    insert(db, "self_service_sessions", {
        "session_id": session_id,
        "customer_id": customer["customer_id"],
        "workflow": args["workflow"],
        "resource_id": args["resource_id"],
        "status": "issued",
        "submitted": False,
        "resume_supported": profile["resume_supported"],
        "save_and_continue": profile["save_and_continue"],
        "credit_pull_authorized": profile["credit_pull_authorized"],
        "claim_tracked": profile["claim_tracked"],
        "claim_id": None,
        "access_location": profile["access_location"],
        "display_label": label,
        "allowed_customer_actions": profile["allowed_customer_actions"],
        "visible_stages": profile["visible_stages"],
        "issued_at": now_iso(db),
        "opened_at": None,
        "expires_at": None,
    }, KEY_COLUMNS)

    deliveries = []
    for rank, channel in enumerate(args["delivery_channels"]):
        spec = db["delivery_channels"].get(channel)
        if spec is None:
            raise NotFound(f"unsupported delivery channel {channel!r}")
        masked = None
        if spec["destination_source"] == "notification_email":
            masked = mask_email(customer["notification_email"])
            if masked is None:
                # Policy requires an authorized destination. A profile with no
                # notification address has none, so the delivery is refused
                # rather than reported as delivered to nowhere.
                raise Refusal("profile has no notification email to deliver to")
        if f"{session_id}|{channel}" in db["session_deliveries"]:
            # A channel named twice is a second delivery row under the same
            # primary key, which the original database rejected as such.
            raise DatabaseError(
                'duplicate key value violates unique constraint "session_deliveries_pkey"\n'
                f"DETAIL:  Key (session_id, channel)=({session_id}, {channel}) already exists.")
        insert(db, "session_deliveries", {
            "session_id": session_id,
            "channel": channel,
            "delivery_rank": rank,
            "status": spec["delivered_status"],
            "masked_destination": masked,
        }, KEY_COLUMNS)
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


def get_secure_self_service_session(db, args) -> dict:
    session = first(db, "self_service_sessions", session_id=args["session_id"],
                    customer_id=args["customer_id"])
    if session is None:
        raise NotFound(f"unknown session {args['session_id']!r} for this customer")

    # Online banking moves the session to 'open_not_submitted' when the customer
    # opens it, and on to saved, submitted or closed as they act in it. This read
    # reports that record; an unopened session past its expiry reads as expired
    # without the read changing anything.
    status = session["status"]
    if status == "issued" and _is_expired(db, session["expires_at"]):
        status = "expired"

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


def send_secure_notification(db, args) -> dict:
    customer = _customer(db, args["customer_id"])
    template = db["notification_templates"].get(args["template"])
    if template is None:
        raise NotFound(f"unapproved notification template {args['template']!r}")
    if template["channel"] != args["channel"]:
        raise Refusal("template is not approved for that channel",
                      {"template_channel": template["channel"]})

    resource_id = args["related_resource_id"]
    known = (first(db, "self_service_sessions", session_id=resource_id,
                   customer_id=customer["customer_id"])
             or first(db, "referrals", referral_id=resource_id,
                      referring_customer_id=customer["customer_id"]))
    if known is None:
        raise NotFound(f"no secure resource {resource_id!r} for this customer")

    if args["channel"] == "email":
        masked = mask_email(customer["notification_email"])
    else:
        channels = _enrolled_channels(db, customer["customer_id"], type="sms")
        masked = channels[0]["masked_destination"] if channels else None
    if masked is None:
        raise Refusal(f"profile has no {args['channel']} destination on file")

    notification_id = new_id(db, "notification")
    insert(db, "notifications", {
        "notification_id": notification_id,
        "customer_id": customer["customer_id"],
        "related_resource_id": resource_id,
        "channel": args["channel"],
        "template": args["template"],
        "status": template["status_on_send"],
        "masked_destination": masked,
        "contains_working_secure_link": template["contains_working_secure_link"],
        "sent_at": now_iso(db),
    }, KEY_COLUMNS)
    return {
        "notification_id": notification_id,
        "status": template["status_on_send"],
        "masked_destination": masked,
        "contains_working_secure_link": template["contains_working_secure_link"],
    }


def transfer_to_specialist(db, args) -> dict:
    transfer_id = new_id(db, "specialist_transfer")
    insert(db, "specialist_transfers", {
        "transfer_id": transfer_id,
        "reason": args["reason"],
        "summary": args["summary"],
        "status": "initiated",
        "created_at": now_iso(db),
    }, KEY_COLUMNS)
    return {"status": "initiated", "transfer_id": transfer_id}


TOOLS = {
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

# Tools that change the bank's records. Membership follows what a tool does to
# the database, not what its name suggests; every other tool only reads.
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

# Reads write nothing.
READ_SIDE_EFFECTS: dict[str, list[str] | str] = {}

# Columns that only record when something happened. The DB score leaves them out,
# so an agent is judged on what it did rather than the second it did it, and a
# scheduled event that waits on the agent (a hotel retry after the block is
# lifted) is judged on having happened rather than on when.
CLOCK_COLUMNS: dict[str, list[str] | str] = {
    "identity_verifications": ["verified_at", "expires_at"],
    "channel_confirmations": ["sent_at", "verified_at", "expires_at"],
    "self_service_sessions": ["issued_at", "opened_at", "expires_at"],
    "card_restrictions": ["resolved_at"],
    "restriction_transactions": ["confirmed_at"],
    "transactions": ["occurred_at"],
    "travel_notices": ["created_at"],
    "notifications": ["sent_at"],
    "specialist_transfers": ["created_at"],
}
