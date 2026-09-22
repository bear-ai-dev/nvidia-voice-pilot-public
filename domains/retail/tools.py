"""Retail tools over the conversation's JSON database.

Ported from the Westline retail PostgreSQL tool server in PR #17. Each tool is
(db, args) -> result and may mutate db in place; see env/toolkit.py for the row
layout and helpers.

Handlers hold domain logic only. Every identifier, amount, deadline, carrier
detail and notification state that appears in a result is read from the
database, never computed from wall time or generated at random, so a result is
reproducible and an operator can explain any value by pointing at a row.

Three conventions are worth stating because they decide what a result looks
like:

Absent versus null. The registry types a handful of fields as string-or-null
("photo", "unit_number", "locker") and documents null as a positive answer: the
carrier recorded none. Those are emitted even when null. Every other field is
dropped when the backend does not know it, because the registry's rule is that
an absent field reads as unavailable.

Money. JSON distinguishes 40 from 40.0 and the recorded results use the integer
form for whole amounts and the decimal form otherwise, everywhere, without
exception. That is a single documented rule here rather than a per-field choice.
Stored amounts are NUMERIC(10, 2) columns, so a value written to one is rounded
to cents the way the database rounds it.

Human-relative time. Deadlines and scan times are stored as instants and
rendered against the scenario clock, so "18:00 tomorrow" is computed from
2026-08-26T18:00 and the frozen clock rather than stored as a sentence. Stored
instants are ISO 8601 strings in UTC, which is how the database returned them.

Ordering follows the original queries' ORDER BY clauses. The database used a
C.UTF-8 collation, so text sorts by code point exactly as Python compares str.

Three deliberate departures from the recorded backend, none of which a recorded
call exercises:

- update_case and send_case_notification answer an unknown case_id with 404
  "unknown case '<id>'". The original formatted that message from a
  case_number argument the registry never supplies, so it failed with a 500.
- Task 07's copy of the original server predated the case_id argument: its
  update_case and send_case_notification read case_number and failed on every
  registry-valid call. This port uses the working handlers on every
  conversation.
- Task 07's open_delivery_trace and open_refund_trace returned case_number
  without case_id, which the registry's result schema requires. This port
  returns both, as tasks 06 and 08 did.
"""
from __future__ import annotations

import datetime as dt
import re
from decimal import ROUND_HALF_UP, Decimal

from toolkit import (NotFound, Refusal, ToolError, allocate_id, as_float, as_int,
                     as_list_always, compact, first, insert, rows, scenario_id,
                     scenario_value)

# Key columns per table: a row's key in db[table] is these columns joined by "|".
KEY_COLUMNS = {
    "carrier_scans": [
        "scan_seq"
    ],
    "case_items": [
        "case_number",
        "item_reference"
    ],
    "case_notes": [
        "case_number",
        "note_no"
    ],
    "case_preferences": [
        "case_number"
    ],
    "case_type_policy": [
        "case_type"
    ],
    "cases": [
        "case_number"
    ],
    "customers": [
        "customer_id"
    ],
    "distribution_centers": [
        "dc_id"
    ],
    "eligible_resolutions": [
        "order_reference",
        "position"
    ],
    "id_allocator": [
        "entity_type",
        "scope"
    ],
    "note_topics": [
        "topic",
        "match_pattern"
    ],
    "notification_templates": [
        "template"
    ],
    "notifications": [
        "notification_id"
    ],
    "order_items": [
        "item_reference"
    ],
    "orders": [
        "order_reference"
    ],
    "payments": [
        "payment_seq"
    ],
    "pickup_site_suffixes": [
        "suffix"
    ],
    "product_variants": [
        "variant_reference"
    ],
    "products": [
        "product_reference"
    ],
    "refunds": [
        "refund_seq"
    ],
    "replacement_orders": [
        "replacement_order_reference"
    ],
    "returns": [
        "return_reference"
    ],
    "scenario": [
        "key"
    ],
    "section_read_cursor": [
        "order_reference",
        "section"
    ],
    "section_view": [
        "order_reference",
        "section",
        "view_index"
    ],
    "specialist_transfers": [
        "transfer_id"
    ]
}

# Case states that still represent work in progress. Anything else is history
# and must not be offered to a caller as something that can still be acted on.
OPEN_CASE_STATUSES = [
    "open", "awaiting_carrier_response", "pending_customer_or_external_response",
    "reviewing_merchant_and_tender_records", "awaiting_external_settlement",
    "eligibility_determined", "resolution_eligible_or_ineligible",
]


class DatabaseError(ToolError):
    """A write the database itself would have rejected, such as a duplicate key.

    The original server reported those as a 500 carrying Postgres's message, and
    the port does the same so a caller sees the same answer.
    """
    status = 500
    kind = "database_error"


def _add(db, table: str, row: dict) -> dict:
    """Insert a row, failing as the table's primary key would on a duplicate."""
    try:
        return insert(db, table, row, KEY_COLUMNS)
    except ValueError:
        columns = KEY_COLUMNS[table]
        raise DatabaseError(
            f'duplicate key value violates unique constraint "{table}_pkey"\n'
            f"DETAIL:  Key ({', '.join(columns)})="
            f"({', '.join(str(row[column]) for column in columns)}) already exists.")


# ---------------------------------------------------------------------------
# scenario clock and rendering
# ---------------------------------------------------------------------------


def _now(db) -> dt.datetime:
    return dt.datetime.fromisoformat(scenario_value(db, "scenario_time"))


def _instant(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value)


def _stored_instant(instant: dt.datetime) -> str:
    """A timestamptz as the database hands it back: the same instant, in UTC."""
    return instant.astimezone(dt.timezone.utc).isoformat()


def _stored_numeric(value) -> float:
    """A NUMERIC(10, 2) column after the write: rounded to cents, half away from zero."""
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _decimal(value) -> Decimal | None:
    """A stored NUMERIC read back as the exact decimal the database held."""
    return None if value is None else Decimal(str(value))


def _new_case_number(db) -> str:
    return (scenario_id(db, "next_support_case_id", "cases", "case_number")
            or allocate_id(db, "support_case"))


def _case_display_number(db, case_number: str) -> str:
    """The customer-facing number: the recorded one only for the recorded case."""
    if case_number == scenario_value(db, "next_support_case_id"):
        return scenario_value(db, "next_support_case_number") or case_number
    return case_number


def _money(value):
    """Render a monetary column the way this domain's results render money.

    Whole amounts are JSON integers and everything else is a JSON decimal. Every
    recorded retail amount follows that rule, so it is applied once here rather
    than chosen field by field.
    """
    if value is None:
        return None
    if value == int(value):
        return as_int(value)
    return as_float(value)


def _clock_display(instant: dt.datetime, now: dt.datetime) -> str:
    """Render an instant the way the desk speaks it: a time and a relative day."""
    delta = (instant.date() - now.date()).days
    hhmm = instant.strftime("%H:%M")
    if delta == 0:
        return f"{hhmm} today"
    if delta == 1:
        return f"{hhmm} tomorrow"
    if delta == -1:
        return f"{hhmm} yesterday"
    return f"{hhmm} on {instant.strftime('%B')} {instant.day}"


def _delivery_display(day: dt.date, now: dt.datetime) -> str:
    """Render a delivery estimate the way the desk speaks it.

    A date inside the coming week is named by its weekday, because that is what
    a caller can act on; anything further out falls back to a calendar date.
    """
    delta = (day - now.date()).days
    if 0 <= delta <= 6:
        return f"{day.strftime('%A')} end of day"
    return f"{day.strftime('%B')} {day.day} end of day"


def _mask_reference(db, order_reference: str) -> str:
    """Render an order reference the way this conversation's results render it.

    The desk discloses a redacted reference rather than the internal one. The
    format is a scenario setting because the sibling retail conversations
    redact the same value differently.
    """
    template = scenario_value(db, "order_reference_mask") or "{last4}"
    return template.format(last4=order_reference[-4:], reference=order_reference)


def _ilike(text: str, pattern: str) -> bool:
    """SQL `text ILIKE pattern`: % is any run, _ any one character, \\ escapes."""
    regex, characters = [], iter(pattern)
    for character in characters:
        if character == "\\":
            regex.append(re.escape(next(characters, "")))
        elif character == "%":
            regex.append(".*")
        elif character == "_":
            regex.append(".")
        else:
            regex.append(re.escape(character))
    return re.fullmatch("".join(regex), text, re.IGNORECASE | re.DOTALL) is not None


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------


def _common_suffix_length(left: str, right: str) -> int:
    length = 0
    while (length < len(left) and length < len(right)
           and left[-1 - length] == right[-1 - length]):
        length += 1
    return length


def _customer_by_email(db, email: str) -> dict | None:
    # The unique index is on lower(email), so at most one customer matches.
    wanted = email.strip().lower()
    return next((row for row in db["customers"].values()
                 if row["email"].lower() == wanted), None)


def _resolve_order(db, reference: str, customer_email: str | None = None) -> dict:
    """Resolve a full or partial order reference, optionally scoped to a customer.

    Policy allows a caller to give the last digits of an order number, so the
    desk matches on the longest trailing run of digits and refuses when two
    orders are equally good matches. An email scopes the search: an order
    reference that belongs to somebody else is not merely a worse match, it is
    not a candidate at all.

    Returns the order row merged with its customer's contact and region columns.
    """
    digits = "".join(character for character in reference if character.isdigit())
    if not digits:
        raise NotFound(f"{reference!r} contains no order digits to resolve")

    if customer_email is not None:
        customer = _customer_by_email(db, customer_email)
        if customer is None:
            raise NotFound(f"no customer with the email {customer_email!r}")
        candidates = rows(db, "orders", customer_id=customer["customer_id"])
    else:
        candidates = list(db["orders"].values())
    candidates = sorted(row["order_reference"] for row in candidates)

    if digits in candidates:
        resolved = digits
    else:
        minimum = int(scenario_value(db, "min_reference_suffix_digits") or 4)
        required = min(len(digits), minimum)
        scored = [(_common_suffix_length(candidate, digits), candidate)
                  for candidate in candidates]
        best = max((length for length, _ in scored), default=0)
        if best < required:
            raise NotFound(f"no order matches the reference {reference!r}")
        winners = [candidate for length, candidate in scored if length == best]
        if len(winners) > 1:
            raise Refusal(
                f"the reference {reference!r} matches more than one order equally well",
                {"candidate_count": len(winners),
                 "matched_trailing_digits": best},
            )
        resolved = winners[0]

    order = db["orders"][resolved]
    customer = db["customers"][order["customer_id"]]
    return dict(order, **{column: customer[column] for column in (
        "display_name", "masked_email", "masked_phone", "fulfillment_region",
        "address_label")})


def _resolve_variant(db, product_reference: str) -> dict:
    """Resolve a catalog variant from a variant, product, or order-item reference."""
    variant = db["product_variants"].get(product_reference)
    if variant:
        return variant

    item = db["order_items"].get(product_reference)
    if item and item["variant_reference"]:
        return _resolve_variant(db, item["variant_reference"])

    variants = rows(db, "product_variants", product_reference=product_reference)
    if not variants:
        raise NotFound(f"no product or variant matches {product_reference!r}")
    return min(variants, key=lambda row: row["variant_reference"])


# ---------------------------------------------------------------------------
# progressive section reads
# ---------------------------------------------------------------------------


def _serve_section(db, order_reference: str, section: str):
    """Serve a section from its read model, advancing the read count.

    Returns (True, payload) when the order has a read model for this section,
    where a payload of None means the service discloses nothing at this depth.
    Returns (False, None) when it has none, in which case the caller projects
    the section from the normalized tables instead.
    """
    views = sorted(rows(db, "section_view", order_reference=order_reference,
                        section=section),
                   key=lambda row: row["view_index"])
    if not views:
        return False, None

    cursor = db["section_read_cursor"].get(f"{order_reference}|{section}")
    if cursor is None:
        cursor = _add(db, "section_read_cursor", {
            "order_reference": order_reference, "section": section, "reads_served": 1})
    else:
        cursor["reads_served"] += 1
    # The deepest disclosure repeats once it has been reached; the service does
    # not fall back to a shallower one on a fourth look.
    index = min(cursor["reads_served"] - 1, len(views) - 1)
    return True, views[index]["payload"]


# ---------------------------------------------------------------------------
# projections
# ---------------------------------------------------------------------------


def _item_projection(include: list[str]) -> str:
    """Choose which columns of the item manifest this read discloses.

    The manifest is projected to the concern the request is about: money fields
    alongside a payments or refunds read, catalog identity alongside a
    resolutions read, and physical identity alongside a fulfillment read. This
    is why the same order returns a differently shaped item list to two
    different questions.
    """
    sections = set(include)
    if sections & {"payments", "refunds"}:
        return "money"
    if "eligible_resolutions" in sections:
        return "catalog"
    if sections & {"fulfillment", "carrier_scans"}:
        return "physical"
    return "minimal"


def _items(db, order_reference: str, include: list[str]) -> list[dict]:
    lines = sorted(rows(db, "order_items", order_reference=order_reference),
                   key=lambda row: row["line_no"])
    shape = _item_projection(include)
    projected = []
    for row in lines:
        if shape == "money":
            projected.append(compact([
                ("item_reference", row["item_reference"]),
                ("name", row["name"]),
                ("total_after_tax", _money(row["total_after_tax"])),
                ("currency", row["currency"] if row["total_after_tax"] is not None else None),
            ]))
        elif shape == "catalog":
            projected.append(compact([
                ("item_reference", row["item_reference"]),
                ("name", row["name"]),
                ("variant", row["variant_label"]),
            ]))
        elif shape == "physical":
            projected.append(compact([
                ("item_reference", row["item_reference"]),
                ("product_reference", row["product_reference"]),
                ("variant", row["variant_label"]),
                ("color", row["color"]),
            ]))
        else:
            projected.append(compact([
                ("item_reference", row["item_reference"]),
                ("name", row["name"]),
            ]))
    return projected


def _latest_scan(db, order_reference: str) -> dict | None:
    scans = rows(db, "carrier_scans", order_reference=order_reference)
    return max(scans, key=lambda row: (_instant(row["scanned_at"]), row["scan_seq"]),
               default=None)


def _public_case_number(db, case_id: str) -> str:
    if case_id == scenario_value(db, "target_case_id"):
        return scenario_value(db, "target_case_number") or case_id
    return case_id


def _open_cases(db, customer_id: str) -> list[dict]:
    """The customer's open cases, each with its type policy and pickup preference."""
    cases = [case for case in rows(db, "cases", customer_id=customer_id)
             if case["status"] in OPEN_CASE_STATUSES]
    cases.sort(key=lambda case: (_instant(case["opened_at"]), case["case_number"]))
    joined = []
    for case in cases:
        policy = db["case_type_policy"][case["case_type"]]
        preference = db["case_preferences"].get(case["case_number"])
        joined.append(dict(
            case,
            pickup_location=preference["pickup_location"] if preference else None,
            order_view_fields=policy["order_view_fields"],
            related_view_fields=policy["related_view_fields"],
        ))
    return joined


def _case_view(db, case: dict, reference: str) -> dict:
    """Project a support case for an order read.

    Which columns the panel shows is not decided here. `case_type_policy` holds
    one field list for a case on the order being read and another for a case
    carried over from a different order on the same account, and this assembles
    whichever the relationship calls for. Hard-coding either list would have put
    a recorded result's shape in Python, where the next conversation to read the
    same case would contradict it.
    """
    preferences = None
    if case["pickup_location"]:
        preferences = {"pickup": case["pickup_location"]}
    available = {
        "case_number": case["case_number"],
        "order_reference": _mask_reference(db, case["order_reference"]),
        "type": case["case_type"],
        "item": case["item_description"],
        "status": case["status"],
        "carrier_response": case["carrier_response"],
        "deadline": case["deadline_display"],
        "carrier_may_contact_customer": case["carrier_may_contact_customer"],
        "replacement_created": case["replacement_created"],
        "preferences": preferences,
    }
    fields = (case["order_view_fields"] if case["order_reference"] == reference
              else case["related_view_fields"])
    view = compact([(field, available[field]) for field in fields])
    if "case_number" in view:
        case_id = view.pop("case_number")
        view["case_id"] = case_id
        view["case_number"] = _public_case_number(db, case_id)
    return view


def _advance_notification(db, notification_id: str) -> dict:
    """Refresh a notification's delivery state and return the refreshed row.

    Delivery receipts arrive from the mail provider after the message is handed
    over. The scenario clock is frozen, so the refresh is driven by the read
    rather than by elapsed time: each look at the notification collects the next
    receipt the provider has for that message, and the last one repeats.
    """
    notification = db["notifications"][notification_id]
    progression = notification["status_progression"]
    notification["status_index"] = min(notification["status_index"] + 1,
                                       len(progression) - 1)
    notification["status"] = progression[notification["status_index"]]
    return notification


def _notification_view(db, notification: dict) -> dict:
    """Project a notification for an order read against its template's field list."""
    fields = db["notification_templates"][notification["template"]]["order_view_fields"]
    available = {
        "notification_id": notification["notification_id"],
        "type": notification["message_type"],
        "status": notification["status"],
        "subject_prefix": notification["subject_prefix"],
        "optional_photo_link": notification["optional_photo_link"],
        "photo_link_section": notification["photo_link_section"],
    }
    return compact([(field, available[field]) for field in fields])


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


def lookup_customer(db, args) -> dict:
    email = args.get("email")
    customer_id = args.get("customer_id")
    if not email and not customer_id:
        raise Refusal(
            "a verified email or customer identifier is required to look a customer up")

    if email:
        match = _customer_by_email(db, email)
        matches = [match] if match and (not customer_id
                                        or match["customer_id"] == customer_id) else []
    else:
        match = db["customers"].get(customer_id)
        matches = [match] if match else []
    if not matches:
        return {"customer_id": "", "match": "none"}

    customer = matches[0]
    # Recent orders exist to let a caller identify a second order they cannot
    # name, so the list is scoped to orders that still carry open support work.
    # Disclosing the rest of the account's history would answer a question
    # nobody asked.
    orders = []
    for order in rows(db, "orders", customer_id=customer["customer_id"]):
        for case in rows(db, "cases", order_reference=order["order_reference"]):
            if case["status"] in OPEN_CASE_STATUSES:
                orders.append((order, case))
    # placed_on is an ISO date, so "newest first" is a descending string sort,
    # done as two stable passes to keep the reference order ascending within it.
    # Two open cases on one order tie on both keys. The original left those in
    # Postgres's physical row order, which moves a case each time it is updated,
    # so no rule reproduces it; here they stay in case-number order.
    orders.sort(key=lambda pair: pair[0]["order_reference"])
    orders.sort(key=lambda pair: pair[0]["placed_on"], reverse=True)
    return {
        "customer_id": customer["customer_id"],
        "match": "unique",
        "display_name": customer["display_name"],
        "recent_orders": [
            compact([
                ("order_reference", _mask_reference(db, order["order_reference"])),
                ("item", order["representative_item"]),
                ("open_case_id", case["case_number"]),
                ("open_case_number", _public_case_number(db, case["case_number"])),
            ])
            for order, case in orders
        ],
    }


def get_order(db, args) -> dict:
    order = _resolve_order(db, args["order_reference"], args["customer_email"])
    reference = order["order_reference"]
    include = list(args.get("include") or [])

    result: list[tuple] = [("order_reference", _mask_reference(db, reference))]

    # The customer header travels with the item manifest. A section-only read is
    # a refresh of that section and returns no identity block.
    if "items" in include:
        # Which identity fields travel with the manifest is a desk setting, not
        # a property of this order: a desk that resolves cases by identifier
        # shows one, and a desk that only needs to confirm it has the right
        # person on the phone does not.
        available = {
            "customer_id": order["customer_id"],
            "display_name": order["display_name"],
            "verified_email": order["masked_email"],
        }
        fields = (scenario_value(db, "order_customer_fields")
                  or "customer_id,display_name,verified_email")
        result.append(("customer", compact([
            (field, available[field]) for field in fields.split(",")])))
        result.append(("items", _items(db, reference, include)))

    fulfillment: list[tuple] = []
    if "fulfillment" in include:
        scan = _latest_scan(db, reference)
        fulfillment.append(("status", order["fulfillment_status"]))
        if scan:
            fulfillment.append(("latest_scan", {
                "time": scan["scanned_at_display"],
                "location": scan["location"],
                # Null is the answer "the carrier took none", not an unknown.
                "photo": scan["photo_reference"],
            }))
    if "carrier_scans" in include:
        served, payload = _serve_section(db, reference, "carrier_scans")
        if served:
            if payload is not None:
                fulfillment.append(("carrier_evidence", payload))
        else:
            scan = _latest_scan(db, reference)
            if scan:
                fulfillment.append(("carrier_evidence", {
                    "scan_location": scan["evidence_location"] or scan["location"],
                    "unit_number": scan["unit_number"],
                    "locker": scan["locker"],
                    "photo": scan["photo_reference"],
                    "possible_misscan": scan["possible_misscan"],
                }))
    if fulfillment:
        result.append(("fulfillment", dict(fulfillment)))

    if "payments" in include:
        served, payload = _serve_section(db, reference, "payments")
        if served:
            if payload is not None:
                result.append(("payments", payload))
        else:
            payments = sorted(rows(db, "payments", order_reference=reference),
                              key=lambda row: row["payment_seq"])
            result.append(("payments", [
                compact([
                    ("type", row["tender_type"]),
                    ("amount", _money(row["amount"])),
                    ("currency", row["currency"]),
                    ("original_card_last4", row["original_card_last4"]),
                ])
                for row in payments
            ]))

    if "refunds" in include:
        served, payload = _serve_section(db, reference, "refunds")
        if served:
            if payload is not None:
                result.append(("refunds", payload))
        else:
            refunds = sorted(rows(db, "refunds", order_reference=reference),
                             key=lambda row: row["refund_seq"])
            result.append(("refunds", [
                compact([
                    ("tender_type", row["tender_type"]),
                    ("amount", _money(row["amount"])),
                    ("available_balance", _money(row["available_balance"])),
                    ("currency", row["currency"]),
                    ("status", row["status"]),
                    ("used", row["used"]),
                    ("delivery", row["delivery"]),
                    ("original_card_last4", row["original_card_last4"]),
                    ("initiation_source", row["initiation_source"]),
                ])
                for row in refunds
            ]))

    if "cases" in include:
        result.append(("cases", [_case_view(db, case, reference)
                                 for case in _open_cases(db, order["customer_id"])]))

    if "eligible_resolutions" in include:
        served, payload = _serve_section(db, reference, "eligible_resolutions")
        if served:
            if payload is not None:
                result.append(("eligible_resolutions", payload))
        else:
            resolutions = sorted(rows(db, "eligible_resolutions", order_reference=reference),
                                 key=lambda row: row["position"])
            result.append(("eligible_resolutions", [
                compact([
                    ("type", row["resolution_type"]),
                    ("preserves_original_price", row["preserves_original_price"]),
                    ("return_required", row["return_required"]),
                    ("photo_required", row["photo_required"]),
                    ("optional_photo_upload_available",
                     row["optional_photo_upload_available"]),
                    ("photo_upload_blocks_fulfillment",
                     row["photo_upload_blocks_fulfillment"]),
                    ("estimated_delivery", row["estimated_delivery_display"]),
                    ("default_fulfillment", row["default_fulfillment"]),
                ])
                for row in resolutions
            ]))

    if "notifications" in include:
        # A notification belongs to the order it was sent about, or to the
        # order of the case it was sent about.
        def about_this_order(notification):
            if notification["order_reference"] == reference:
                return True
            case = db["cases"].get(notification["case_number"])
            return case is not None and case["order_reference"] == reference

        notifications = sorted(
            (row for row in db["notifications"].values() if about_this_order(row)),
            key=lambda row: (_instant(row["created_at"]), row["notification_id"]))
        result.append(("notifications", [
            _notification_view(db, _advance_notification(db, row["notification_id"]))
            for row in notifications
        ]))

    return compact(result)


def get_product(db, args) -> dict:
    variant = _resolve_variant(db, args["product_reference"])
    result: list[tuple] = [("product_reference", variant["variant_reference"])]

    details = compact([
        ("name", variant["display_name"]),
        ("color", variant["color"]),
    ])
    if details:
        result.append(("variant", details))

    if args["include_inventory"]:
        inventory = compact([
            ("in_stock", variant["in_stock"]),
            ("same_variant_in_stock", variant["same_variant_in_stock"]),
        ])
        if inventory:
            result.append(("inventory", inventory))

    return dict(result)


# ---------------------------------------------------------------------------
# mutations
# ---------------------------------------------------------------------------


def _require_items(db, order_reference: str, item_references: list[str]) -> None:
    known = {row["item_reference"]
             for row in rows(db, "order_items", order_reference=order_reference)}
    missing = [reference for reference in item_references if reference not in known]
    if missing:
        raise Refusal(
            "those items are not on that order",
            {"order_reference": _mask_reference(db, order_reference),
             "unknown_item_references": missing},
        )


def _pickup_site(db, location: str) -> str:
    """Normalize a spoken pickup location to the site a reviewer instruction names.

    A customer asks for "the West 23rd Street pickup counter"; the instruction a
    reviewer reads is about checking West 23rd Street. The endings that get
    stripped are rows, so a new label form is a seed change rather than a code
    change. Longer endings are tried first, so " pickup counter" wins over
    " counter".
    """
    text = location.strip()
    suffixes = sorted((row["suffix"] for row in db["pickup_site_suffixes"].values()),
                      key=len, reverse=True)
    for suffix in suffixes:
        if text.lower().endswith(suffix.lower()):
            return text[: -len(suffix)].strip()
    return text


def _case_policy(db, case_type: str) -> dict:
    policy = db["case_type_policy"].get(case_type)
    if policy is None:
        raise Refusal(f"the desk has no policy for a {case_type}")
    return policy


def _case_row(**columns) -> dict:
    """A cases row with every column, defaulted the way the table defaults them."""
    row = dict.fromkeys([
        "case_number", "order_reference", "customer_id", "case_type", "status",
        "reason", "item_description", "carrier_response", "deadline_at",
        "deadline_display", "carrier_may_contact_customer", "replacement_created",
        "requested_resolution", "needed_by", "approval_required", "approval_channel",
        "next_action", "eligibility_triggers", "review_window_min_days",
        "review_window_max_days", "duplicate_refund_blocked",
        "return_evidence_attached", "return_reference", "payment_reference",
        "amount_under_review", "fee_reimbursement_approved", "pickup_guaranteed",
        "opened_at",
    ])
    row.update(replacement_created=False, fee_reimbursement_approved=False,
               pickup_guaranteed=False)
    row.update(columns)
    return row


def open_delivery_trace(db, args) -> dict:
    order = _resolve_order(db, args["order_reference"])
    _require_items(db, order["order_reference"], args["item_references"])
    policy = _case_policy(db, "delivery_trace")
    now = _now(db)

    deadline_day = now.date() + dt.timedelta(days=policy["deadline_offset_days"])
    hour, minute = policy["deadline_local_time"].split(":")
    deadline = dt.datetime(deadline_day.year, deadline_day.month, deadline_day.day,
                           int(hour), int(minute), tzinfo=now.tzinfo)
    deadline_display = _clock_display(deadline, now)

    case_number = _new_case_number(db)
    needed_by = args.get("needed_by")
    _add(db, "cases", _case_row(
        case_number=case_number,
        order_reference=order["order_reference"],
        customer_id=order["customer_id"],
        case_type="delivery_trace",
        status=policy["initial_status"],
        reason=args["reason"],
        item_description=order["representative_item"],
        carrier_response="none",
        deadline_at=_stored_instant(deadline),
        deadline_display=deadline_display,
        carrier_may_contact_customer=policy["carrier_may_contact_customer"],
        replacement_created=False,
        requested_resolution=args.get("requested_resolution"),
        # A DATE column: whatever ISO form the caller used reads back canonical.
        needed_by=dt.date.fromisoformat(needed_by).isoformat() if needed_by else None,
        approval_required=policy["approval_required"],
        approval_channel=policy["approval_channel"],
        next_action=policy["next_action"],
        eligibility_triggers=policy["eligibility_triggers"],
        pickup_guaranteed=policy["pickup_guaranteed"],
        opened_at=_stored_instant(now),
    ))
    for item_reference in args["item_references"]:
        _add(db, "case_items", {"case_number": case_number,
                                "item_reference": item_reference})

    # No confirmation has been sent yet: policy requires the customer to approve
    # a resolution through the trace notification, and the notification is a
    # separate authorized call.
    return compact([
        ("case_id", case_number),
        ("case_number", _case_display_number(db, case_number)),
        ("status", policy["initial_status"]),
        ("carrier_response_deadline", deadline_display),
        ("replacement_created", False),
        ("eligibility_triggers", as_list_always(policy["eligibility_triggers"])),
        ("next_action", policy["next_action"]),
        ("approval_required", policy["approval_required"]),
        ("approval_channel", policy["approval_channel"]),
        ("notification_status", "not_sent"),
    ])


def open_refund_trace(db, args) -> dict:
    order = _resolve_order(db, args["order_reference"])
    reference = order["order_reference"]

    digits = "".join(c for c in args["return_reference"] if c.isdigit())
    returns = sorted(rows(db, "returns", order_reference=reference),
                     key=lambda row: row["return_reference"])
    matched = [row for row in returns
               if _common_suffix_length(row["return_reference"], digits) >= len(digits)]
    if len(matched) != 1:
        raise Refusal(
            "that return reference does not identify exactly one return on the order",
            {"candidate_count": len(matched)},
        )
    accepted_return = matched[0]

    # An empty card token matches any refund on the order, as the original's
    # `original_card_last4 = %s OR %s = ''` did.
    card = "".join(c for c in args["payment_reference"] if c.isdigit())[-4:]
    refund = min(
        (row for row in rows(db, "refunds", order_reference=reference)
         if card == "" or row["original_card_last4"] == card),
        key=lambda row: row["refund_seq"], default=None)
    if refund is None:
        raise Refusal(
            "no refund on that order was issued against that payment reference")
    if abs(Decimal(str(args["amount"])) - _decimal(refund["amount"])) > Decimal("0.005"):
        raise Refusal(
            "the amount under review does not match the refund on that tender",
            {"refund_amount": _money(refund["amount"])},
        )

    policy = _case_policy(db, "refund_trace")
    now = _now(db)
    case_number = _new_case_number(db)
    # Evidence is attached when the return the customer named is a completed
    # return on the same order, which is the only thing Westline can attest to.
    evidence_attached = accepted_return["return_status"] == "complete"
    _add(db, "cases", _case_row(
        case_number=case_number,
        order_reference=reference,
        customer_id=order["customer_id"],
        case_type="refund_trace",
        status=policy["initial_status"],
        reason="missing_refund",
        item_description=order["representative_item"],
        replacement_created=False,
        review_window_min_days=policy["review_window_min_days"],
        review_window_max_days=policy["review_window_max_days"],
        duplicate_refund_blocked=policy["duplicate_refund_blocked"],
        return_evidence_attached=evidence_attached,
        return_reference=accepted_return["return_reference"],
        payment_reference=args["payment_reference"],
        amount_under_review=_stored_numeric(args["amount"]),
        opened_at=_stored_instant(now),
    ))
    return {
        "case_id": case_number,
        "case_number": _case_display_number(db, case_number),
        "status": policy["initial_status"],
        "review_window_business_days": [policy["review_window_min_days"],
                                        policy["review_window_max_days"]],
        "duplicate_refund_blocked": policy["duplicate_refund_blocked"],
        "return_evidence_attached": evidence_attached,
    }


def create_replacement_order(db, args) -> dict:
    original = _resolve_order(db, args["order_reference"])
    reference = original["order_reference"]
    _require_items(db, reference, args["item_references"])

    if not args["customer_authorized"]:
        raise Refusal(
            "policy requires explicit customer authorization before a replacement "
            "order is created")

    eligibility = first(db, "eligible_resolutions", order_reference=reference,
                        resolution_type="replacement")
    if eligibility is None:
        raise Refusal(
            "that order is not currently eligible for a replacement",
            {"order_reference": _mask_reference(db, reference)},
        )

    # Each order line being replaced, with its catalog stock and price and its
    # product's disposal rules; a missing variant or product reads as unknown.
    lines = []
    for item in sorted(rows(db, "order_items", order_reference=reference),
                       key=lambda row: row["line_no"]):
        if item["item_reference"] not in args["item_references"]:
            continue
        variant = db["product_variants"].get(item["variant_reference"]) or {}
        product = db["products"].get(item["product_reference"]) or {}
        lines.append(dict(
            item,
            in_stock=variant.get("in_stock"),
            same_variant_in_stock=variant.get("same_variant_in_stock"),
            current_price=variant.get("current_price"),
            disposal_disposition=product.get("disposal_disposition"),
            safety_instruction=product.get("safety_instruction"),
        ))
    unavailable = [line["item_reference"] for line in lines
                   if line["in_stock"] is False or line["same_variant_in_stock"] is False]
    if unavailable:
        raise Refusal(
            "the replacement stock for those items is not available",
            {"unavailable_item_references": unavailable},
        )

    now = _now(db)
    replacement_reference = allocate_id(db, "order", "replacement")
    _add(db, "orders", {
        "order_reference": replacement_reference,
        "customer_id": original["customer_id"],
        "placed_on": now.date().isoformat(),
        "fulfillment_status": "processing",
        "destination_label": args.get("fulfillment_location") or original["destination_label"],
        "replaces_order_reference": reference,
        "representative_item": original["representative_item"],
    })
    for line in lines:
        _add(db, "order_items", {
            "item_reference": f"{replacement_reference}-{line['item_reference']}",
            "order_reference": replacement_reference,
            "line_no": line["line_no"],
            "variant_reference": line["variant_reference"],
            "product_reference": line["product_reference"],
            "name": line["name"],
            "variant_label": line["variant_label"],
            "color": line["color"],
            "total_after_tax": line["total_after_tax"],
            "currency": line["currency"],
        })

    # The eligible original price is what the customer already paid. Where the
    # eligibility does not preserve it, the difference against today's catalog
    # price is what falls due, and never less than nothing.
    if eligibility["preserves_original_price"]:
        balance = Decimal("0.00")
    else:
        original_total = sum((_decimal(line["total_after_tax"]) or Decimal("0"))
                             for line in lines)
        current_total = sum((_decimal(line["current_price"])
                             or _decimal(line["total_after_tax"]) or Decimal("0"))
                            for line in lines)
        balance = max(Decimal("0.00"), current_total - original_total)

    centers = rows(db, "distribution_centers", region=original["fulfillment_region"])
    center = min(centers, key=lambda row: row["dc_id"], default=None)

    estimated_on = eligibility["estimated_delivery_on"]
    estimated_display = eligibility["estimated_delivery_display"]
    if estimated_on is not None and estimated_display is None:
        estimated_display = _delivery_display(dt.date.fromisoformat(estimated_on), now)

    disposition = None
    safety = None
    if not eligibility["return_required"]:
        disposition = next((line["disposal_disposition"] for line in lines
                            if line["disposal_disposition"]), None)
        safety = next((line["safety_instruction"] for line in lines
                       if line["safety_instruction"]), None)

    _add(db, "replacement_orders", {
        "replacement_order_reference": replacement_reference,
        "original_order_reference": reference,
        "reason": args["reason"],
        "status": "created",
        "balance_due": _stored_numeric(balance),
        "currency": "USD",
        "fulfillment_method": args["fulfillment_method"],
        "fulfillment_location": args.get("fulfillment_location") or original["address_label"],
        "estimated_delivery_on": estimated_on,
        "estimated_delivery_display": estimated_display,
        "estimate_guaranteed": False,
        "distribution_center": center["display_name"] if center else None,
        "distribution_center_status": "provisional_until_shipped",
        "tracking_notifications": True,
        "return_required": eligibility["return_required"],
        "disposition": disposition,
        "safety": safety,
        "created_at": _stored_instant(now),
    })

    template = db["notification_templates"]["replacement_confirmation"]
    _add(db, "notifications", {
        "notification_id": f"notification-{replacement_reference}",
        "case_number": None,
        "order_reference": replacement_reference,
        "channel": "email",
        "template": template["template"],
        "message_type": template["message_type"],
        "masked_destination": original["masked_email"],
        "status": template["initial_status"],
        "status_index": 0,
        "status_progression": template["delivery_progression"],
        "subject_prefix": template["subject_prefix"],
        # The optional photo link is offered because the eligibility offers it,
        # not because a replacement always carries one.
        "optional_photo_link": bool(eligibility["optional_photo_upload_available"]),
        "photo_link_section": template["photo_link_section"],
        "included_fields": template["included_fields"],
        "sent_at": _stored_instant(now),
        "sent_at_display": None,
        "created_at": _stored_instant(now),
    })

    # Any trace still open on the original order now has a replacement against
    # it, and a later read of that case must say so.
    for case in rows(db, "cases", order_reference=reference):
        if case["status"] in OPEN_CASE_STATUSES:
            case["replacement_created"] = True

    return compact([
        ("replacement_order_reference", _mask_reference(db, replacement_reference)),
        ("status", "created"),
        ("balance_due", _money(balance)),
        ("currency", "USD"),
        ("fulfillment", compact([
            ("method", args["fulfillment_method"]),
            ("location", args.get("fulfillment_location") or original["address_label"]),
            ("estimated_delivery", estimated_display),
            ("estimate_guaranteed", False),
            ("distribution_center", center["display_name"] if center else None),
            ("distribution_center_status", "provisional_until_shipped"),
            ("tracking_notifications", True),
        ])),
        ("return_disposition", compact([
            ("return_required", bool(eligibility["return_required"])),
            ("disposition", disposition),
            ("safety", safety),
        ])),
        ("notification", compact([
            ("status", template["initial_status"]),
            ("optional_photo_link",
             True if eligibility["optional_photo_upload_available"] else None),
            ("photo_link_section",
             template["photo_link_section"]
             if eligibility["optional_photo_upload_available"] else None),
        ])),
    ])


def update_case(db, args) -> dict:
    case = db["cases"].get(args["case_id"])
    if case is None:
        raise NotFound(f"unknown case {args['case_id']!r}")

    note = args.get("note")
    pickup = args.get("preferred_pickup_location")
    requested = args.get("requested_resolution")
    if note is None and pickup is None and requested is None:
        raise Refusal("no note, resolution, or preference was supplied")
    # What the case said before this update, which is what the result reports.
    before = dict(case)

    now = _now(db)
    fee_note = False
    if note is not None:
        # The note's topic is the first matching pattern, preferring one that
        # discloses the fee decision.
        topics = sorted(
            (row for row in db["note_topics"].values() if _ilike(note, row["match_pattern"])),
            key=lambda row: (not row["discloses_fee_decision"], row["topic"]))
        topic = topics[0] if topics else None
        fee_note = bool(topic and topic["discloses_fee_decision"])
        numbers = [row["note_no"] for row in rows(db, "case_notes",
                                                   case_number=case["case_number"])]
        _add(db, "case_notes", {
            "case_number": case["case_number"],
            "note_no": max(numbers, default=0) + 1,
            "note": note,
            "topic": topic["topic"] if topic else None,
            "visible_to_next_reviewer": True,
            "created_at": _stored_instant(now),
        })

    if requested is not None:
        case["requested_resolution"] = requested

    review_instruction = None
    if pickup is not None:
        policy = _case_policy(db, case["case_type"])
        template = policy["preference_instruction_template"]
        if not template:
            raise Refusal(
                f"a {case['case_type']} does not carry a pickup preference")
        site = _pickup_site(db, pickup)
        review_instruction = template.replace("{site}", site)
        preference = db["case_preferences"].get(case["case_number"])
        if preference is None:
            _add(db, "case_preferences", {
                "case_number": case["case_number"],
                "pickup_location": pickup,
                "pickup_site": site,
                "review_instruction": review_instruction,
                "visible_to_next_reviewer": True,
                "recorded_at": _stored_instant(now),
            })
        else:
            preference.update(pickup_location=pickup, pickup_site=site,
                              review_instruction=review_instruction,
                              recorded_at=_stored_instant(now))

    # A preference changes what the next reviewer will do; a note only tells
    # them something. The two outcomes are distinct in the registry and the
    # caller is told which one happened.
    status = "preference_added" if (pickup is not None or requested is not None) \
        else "note_added"
    return compact([
        ("status", status),
        ("visible_to_next_reviewer", True),
        ("review_instruction", review_instruction),
        ("pickup_guaranteed", before["pickup_guaranteed"] if pickup is not None else None),
        # Policy forbids approving a bank fee while the trace is open, so a note
        # that raises one is answered rather than silently filed.
        ("fee_reimbursement_approved",
         before["fee_reimbursement_approved"] if fee_note else None),
    ])


def send_case_notification(db, args) -> dict:
    case = db["cases"].get(args["case_id"])
    if case is None:
        raise NotFound(f"unknown case {args['case_id']!r}")
    customer = db["customers"][case["customer_id"]]

    template = db["notification_templates"].get(args["template"])
    if template is None:
        raise Refusal(f"unknown notification template {args['template']!r}")

    destination = (customer["masked_email"] if args["channel"] == "email"
                   else customer["masked_phone"])
    if not destination:
        raise Refusal(
            f"no verified {args['channel']} destination is on file for this case")

    now = _now(db)
    notification_id = scenario_id(
        db, "next_notification_id", "notifications", "notification_id",
        {"case_number": case["case_number"]}) or f"notification-{case['case_number']}"
    # A resend is the same message going out again, so it keeps its identifier
    # and restarts from the delivery state a fresh send has. Only the delivery
    # columns are rewritten; what the message is about and when it was first
    # created stay as they were.
    delivery = {
        "channel": args["channel"],
        "template": template["template"],
        "message_type": template["message_type"],
        "masked_destination": destination,
        "status": template["initial_status"],
        "status_index": 0,
        "status_progression": template["delivery_progression"],
        "included_fields": template["included_fields"],
        "sent_at": _stored_instant(now),
    }
    existing = db["notifications"].get(notification_id)
    if existing is None:
        _add(db, "notifications", dict(
            delivery,
            notification_id=notification_id,
            case_number=case["case_number"],
            order_reference=case["order_reference"],
            subject_prefix=template["subject_prefix"],
            optional_photo_link=template["optional_photo_link"],
            photo_link_section=template["photo_link_section"],
            sent_at_display=None,
            created_at=_stored_instant(now),
        ))
    else:
        existing.update(delivery)

    return {
        "notification_id": notification_id,
        "status": template["initial_status"],
        "masked_destination": destination,
        "case_id": case["case_number"],
        "case_number": _public_case_number(db, case["case_number"]),
        "included_fields": as_list_always(template["included_fields"]),
    }


def transfer_to_specialist(db, args) -> dict:
    transfer_id = allocate_id(db, "specialist_transfer")
    _add(db, "specialist_transfers", {
        "transfer_id": transfer_id,
        "reason": args["reason"],
        "summary": args["summary"],
        "status": "transferred",
        "created_at": _stored_instant(_now(db)),
    })
    return {"status": "transferred", "transfer_id": transfer_id}


TOOLS = {
    "lookup_customer": lookup_customer,
    "get_order": get_order,
    "get_product": get_product,
    "open_delivery_trace": open_delivery_trace,
    "open_refund_trace": open_refund_trace,
    "create_replacement_order": create_replacement_order,
    "update_case": update_case,
    "send_case_notification": send_case_notification,
    "transfer_to_specialist": transfer_to_specialist,
}

# Tools that change Westline's records. The test is whether the tool changes a
# business fact, not whether it writes a row. get_order writes on every call: it
# advances section_read_cursor and collects the next delivery receipt on any
# notification it discloses. Neither is a fact about the customer's order, so it
# stays a read.
WRITE_TOOLS = {
    "open_delivery_trace",
    "open_refund_trace",
    "create_replacement_order",
    "update_case",
    "send_case_notification",
    "transfer_to_specialist",
}

# What reads write as a side effect, excluded from a state hash so looking is
# free: get_order's section read counts, and the delivery receipt it collects on
# each notification it discloses. An inserted or otherwise changed notification
# is still a consequential write.
READ_SIDE_EFFECTS = {
    "notifications": ["status", "status_index"],
    "section_read_cursor": "*",
}
