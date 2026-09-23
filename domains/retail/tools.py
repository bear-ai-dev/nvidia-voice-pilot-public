"""Retail tools over the conversation's JSON database.

Ported from the Westline retail PostgreSQL tool server in PR #17. Each tool is
(db, args) -> result and may mutate db in place; see env/toolkit.py for the row
layout and helpers.

Handlers hold domain logic only. Every amount, deadline, carrier detail and
notification state that appears in a result is read from the database, so an
operator can explain any value by pointing at a row. A read returns the same
projection of the same rows every time it is made and writes nothing. What
changes between two reads is the records themselves: a write the agent made, or
something that happened outside the call in the meantime (the mail system
sending a queued email, the mail provider reporting it delivered), which the
conversation's events.json schedules at its own time.

New records take their identifiers from the environment's seeded generator
(toolkit.new_id), and their readable business numbers (case numbers, order
numbers, transfer ids) from the database's own sequences in id_allocator. Every
write is stamped with the call clock (toolkit.now), which moves through the
call, so two writes minutes apart carry different times.

Five conventions are worth stating because they decide what a result looks
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

Time. Results carry timestamps and dates, never phrases relative to the call:
a deadline is 2026-08-26T18:00:00-04:00, not "18:00 tomorrow", and working out
"tomorrow" from the current time is the agent's job. Stored instants are ISO
8601 strings in UTC, which is how the database returned them; a result renders
them in the scenario's timezone, which is how the desk displays them. Delivery
estimates are calendar dates.

Records, not conclusions. A result reports what the records hold. It does not
add a judgement the agent should draw from them (whether a scan looks like a
mis-scan), a disclaimer the agent should voice (that an estimate or a pickup is
not guaranteed), or a sentence written for the agent to repeat.

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
from zoneinfo import ZoneInfo

from toolkit import (NotFound, Refusal, ToolError, allocate_id, as_float, as_int,
                     as_list_always, compact, first, insert, new_id, rows,
                     scenario_value)
from toolkit import now as call_clock

# Key columns per table: a row's key in db[table] is these columns joined by "|".
KEY_COLUMNS = {
    "carrier_scans": [
        "scan_seq"
    ],
    "case_items": [
        "case_id",
        "item_reference"
    ],
    "case_notes": [
        "case_id",
        "note_no"
    ],
    "case_preferences": [
        "case_id"
    ],
    "case_type_policy": [
        "case_type"
    ],
    "cases": [
        "case_id"
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
# call clock and rendering
# ---------------------------------------------------------------------------


def _now(db) -> dt.datetime:
    """The current moment in the call, in the scenario's UTC offset.

    The runtime moves this clock through the call, so a write made seven minutes
    after another is stamped seven minutes later.
    """
    return call_clock(db)


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


def _new_case(db) -> tuple[str, str]:
    """Issue a new case's internal id and its customer-facing number together.

    The id is an opaque UUID from the environment's generator; the number comes
    from the desk's support_case sequence, the one every other case on file was
    numbered from. Both are stored on the case row.
    """
    return new_id(db, "support_case"), allocate_id(db, "support_case")


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


def _local_instant(db, value: str | dt.datetime | None) -> str | None:
    """Render an instant as ISO 8601 in the scenario's timezone.

    The scenario names its timezone; one that does not falls back to the
    scenario clock's own UTC offset.
    """
    if value is None:
        return None
    instant = _instant(value) if isinstance(value, str) else value
    zone = scenario_value(db, "timezone")
    tz = ZoneInfo(zone) if zone else _now(db).tzinfo
    return instant.astimezone(tz).isoformat()


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


def _carrier_evidence(db, scan: dict) -> dict:
    """The raw fields of a carrier scan, for the evidence panel.

    What the scan does or does not show about where the package went is for
    the reader to work out. The unit, locker and photo are emitted even when
    null, because null is the carrier's answer that it recorded none.
    """
    evidence = compact([
        ("scanned_at", _local_instant(db, scan["scanned_at"])),
        ("location", scan["location"]),
        ("evidence_location", scan["evidence_location"]),
    ])
    evidence.update(unit_number=scan["unit_number"], locker=scan["locker"],
                    photo=scan["photo_reference"])
    return evidence


def _open_cases(db, customer_id: str) -> list[dict]:
    """The customer's open cases, each with its type policy and pickup preference."""
    cases = [case for case in rows(db, "cases", customer_id=customer_id)
             if case["status"] in OPEN_CASE_STATUSES]
    cases.sort(key=lambda case: (_instant(case["opened_at"]), case["case_number"]))
    joined = []
    for case in cases:
        policy = db["case_type_policy"][case["case_type"]]
        preference = db["case_preferences"].get(case["case_id"])
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
        "order_reference": _mask_reference(db, case["order_reference"]),
        "type": case["case_type"],
        "item": case["item_description"],
        "status": case["status"],
        "carrier_response": case["carrier_response"],
        "deadline": _local_instant(db, case["deadline_at"]),
        "carrier_may_contact_customer": case["carrier_may_contact_customer"],
        "replacement_created": case["replacement_created"],
        "preferences": preferences,
    }
    fields = (case["order_view_fields"] if case["order_reference"] == reference
              else case["related_view_fields"])
    # The panel's "case_number" field is the case's identity, shown as both the
    # internal id later case tools take and the number the customer was given.
    view = compact([(field, available[field]) for field in fields
                    if field != "case_number"])
    if "case_number" in fields:
        view["case_id"] = case["case_id"]
        view["case_number"] = case["case_number"]
    return view


def _notification_view(db, notification: dict) -> dict:
    """Project a notification for an order read against its template's field list.

    The row holds what is particular to this message: its template, whether it
    carries the optional photo link, and its delivery status as the mail system
    last reported it. The message type, subject and layout belong to the
    template and are read from it, so a read shows the message as it was built.
    """
    template = db["notification_templates"][notification["template"]]
    has_photo_link = notification["optional_photo_link"]
    available = {
        "notification_id": notification["notification_id"],
        "type": template["message_type"],
        "status": notification["status"],
        "subject_prefix": template["subject_prefix"],
        "optional_photo_link": has_photo_link,
        "photo_link_section": template["photo_link_section"] if has_photo_link else None,
    }
    return compact([(field, available[field]) for field in template["order_view_fields"]])


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
    # so no rule reproduces it; here they stay in case-id order.
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
                ("open_case_id", case["case_id"]),
                ("open_case_number", case["case_number"]),
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
                "scanned_at": _local_instant(db, scan["scanned_at"]),
                "location": scan["location"],
                # Null is the answer "the carrier took none", not an unknown.
                "photo": scan["photo_reference"],
            }))
    if "carrier_scans" in include:
        scan = _latest_scan(db, reference)
        if scan:
            fulfillment.append(("carrier_evidence", _carrier_evidence(db, scan)))
    if fulfillment:
        result.append(("fulfillment", dict(fulfillment)))

    if "payments" in include:
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
        # The section is the money's paper trail: each return accepted against
        # the order, then each per-tender refund issued for it. A refund carries
        # two states. Its status is where the refund stands with Westline and
        # the processor; a gift-card or store-credit refund also has a ledger
        # entry of its own, whose status says whether the issued balance works.
        returns = sorted(rows(db, "returns", order_reference=reference),
                         key=lambda row: row["return_reference"])
        refunds = sorted(rows(db, "refunds", order_reference=reference),
                         key=lambda row: row["refund_seq"])
        result.append(("refunds", [
            compact([
                ("return_reference", _mask_reference(db, row["return_reference"])),
                ("return_status", row["return_status"]),
                ("accepted_at", row["accepted_at"]),
                ("accepted_on", row["accepted_on"]),
                ("inventory_disposition", row["inventory_disposition"]),
            ])
            for row in returns
        ] + [
            compact([
                ("tender_type", row["tender_type"]),
                ("amount", _money(row["amount"])),
                ("available_balance", _money(row["available_balance"])),
                ("currency", row["currency"]),
                ("status", row["status"]),
                ("ledger_status", row["ledger_status"]),
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
                ("estimated_delivery", row["estimated_delivery_on"]),
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
            case = db["cases"].get(notification["case_id"])
            return case is not None and case["order_reference"] == reference

        # The status is whatever the mail system last reported; reading it does
        # not move it.
        notifications = sorted(
            (row for row in db["notifications"].values() if about_this_order(row)),
            key=lambda row: (_instant(row["created_at"]), row["notification_id"]))
        result.append(("notifications", [_notification_view(db, row)
                                          for row in notifications]))

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


def _case_policy(db, case_type: str) -> dict:
    policy = db["case_type_policy"].get(case_type)
    if policy is None:
        raise Refusal(f"the desk has no policy for a {case_type}")
    return policy


def _case_row(**columns) -> dict:
    """A cases row with every column, defaulted the way the table defaults them."""
    row = dict.fromkeys([
        "case_id", "case_number", "order_reference", "customer_id", "case_type", "status",
        "reason", "item_description", "carrier_response", "deadline_at",
        "carrier_may_contact_customer", "replacement_created",
        "requested_resolution", "needed_by", "approval_required", "approval_channel",
        "next_action", "eligibility_triggers", "review_window_min_days",
        "review_window_max_days", "duplicate_refund_blocked",
        "return_evidence_attached", "return_reference", "payment_reference",
        "amount_under_review", "opened_at",
    ])
    row.update(replacement_created=False)
    row.update(columns)
    return row


def _notification_row(**columns) -> dict:
    """A notifications row with every column, defaulted the way the table defaults them.

    delivered_at stays null until the mail provider reports delivery.
    """
    row = dict.fromkeys([
        "notification_id", "case_id", "order_reference", "customer_id", "channel",
        "template", "status", "optional_photo_link", "created_at", "sent_at",
        "delivered_at",
    ])
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

    case_id, case_number = _new_case(db)
    needed_by = args.get("needed_by")
    _add(db, "cases", _case_row(
        case_id=case_id,
        case_number=case_number,
        order_reference=order["order_reference"],
        customer_id=order["customer_id"],
        case_type="delivery_trace",
        status=policy["initial_status"],
        reason=args["reason"],
        item_description=order["representative_item"],
        carrier_response="none",
        deadline_at=_stored_instant(deadline),
        carrier_may_contact_customer=policy["carrier_may_contact_customer"],
        requested_resolution=args.get("requested_resolution"),
        # A DATE column: whatever ISO form the caller used reads back canonical.
        needed_by=dt.date.fromisoformat(needed_by).isoformat() if needed_by else None,
        approval_required=policy["approval_required"],
        approval_channel=policy["approval_channel"],
        next_action=policy["next_action"],
        eligibility_triggers=policy["eligibility_triggers"],
        opened_at=_stored_instant(now),
    ))
    for item_reference in args["item_references"]:
        _add(db, "case_items", {"case_id": case_id,
                                "item_reference": item_reference})

    # No confirmation has been sent yet: policy requires the customer to approve
    # a resolution through the trace notification, and the notification is a
    # separate authorized call.
    return compact([
        ("case_id", case_id),
        ("case_number", case_number),
        ("status", policy["initial_status"]),
        ("carrier_response_deadline", _local_instant(db, deadline)),
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
    case_id, case_number = _new_case(db)
    # Evidence is attached when the return the customer named is a completed
    # return on the same order, which is the only thing Westline can attest to.
    evidence_attached = accepted_return["return_status"] == "complete"
    _add(db, "cases", _case_row(
        case_id=case_id,
        case_number=case_number,
        order_reference=reference,
        customer_id=order["customer_id"],
        case_type="refund_trace",
        status=policy["initial_status"],
        reason="missing_refund",
        item_description=order["representative_item"],
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
        "case_id": case_id,
        "case_number": case_number,
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
    location = args.get("fulfillment_location")
    # A replacement is an ordinary new order, numbered from the same order
    # sequence as any other; what makes it a replacement is the link back to the
    # original, kept in replaces_order_reference and replacement_orders.
    replacement_reference = allocate_id(db, "order")
    _add(db, "orders", {
        "order_reference": replacement_reference,
        "customer_id": original["customer_id"],
        "placed_on": now.date().isoformat(),
        "fulfillment_status": "processing",
        "destination_label": location or original["destination_label"],
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
    center_name = center["display_name"] if center else None
    fulfillment_location = location or original["address_label"]

    estimated_on = eligibility["estimated_delivery_on"]

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
        "fulfillment_location": fulfillment_location,
        "estimated_delivery_on": estimated_on,
        "distribution_center": center_name,
        "distribution_center_status": "provisional_until_shipped",
        "tracking_notifications": True,
        "return_required": eligibility["return_required"],
        "disposition": disposition,
        "safety": safety,
        "created_at": _stored_instant(now),
    })

    # The confirmation joins the outbound mail queue. It has not been sent yet,
    # so it has no sent_at; the mail system sends it later and the provider
    # reports delivery after that, each at its own time.
    template = db["notification_templates"]["replacement_confirmation"]
    status = template["initial_status"]
    _add(db, "notifications", _notification_row(
        notification_id=new_id(db, "notification"),
        order_reference=replacement_reference,
        channel="email",
        template=template["template"],
        customer_id=original["customer_id"],
        status=status,
        # The optional photo link is offered because the eligibility offers it,
        # not because a replacement always carries one.
        optional_photo_link=bool(eligibility["optional_photo_upload_available"]),
        sent_at=_stored_instant(now) if status != "queued" else None,
        created_at=_stored_instant(now),
    ))

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
            ("location", fulfillment_location),
            ("estimated_delivery", estimated_on),
            ("distribution_center", center_name),
            ("distribution_center_status", "provisional_until_shipped"),
            ("tracking_notifications", True),
        ])),
        ("return_disposition", compact([
            ("return_required", bool(eligibility["return_required"])),
            ("disposition", disposition),
            ("safety", safety),
        ])),
        ("notification", compact([
            ("status", status),
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

    now = _now(db)
    if note is not None:
        # The note's topic is the first matching topic, by name.
        topics = sorted(
            (row for row in db["note_topics"].values() if _ilike(note, row["match_pattern"])),
            key=lambda row: row["topic"])
        topic = topics[0] if topics else None
        numbers = [row["note_no"] for row in rows(db, "case_notes",
                                                   case_id=case["case_id"])]
        _add(db, "case_notes", {
            "case_id": case["case_id"],
            "note_no": max(numbers, default=0) + 1,
            "note": note,
            "topic": topic["topic"] if topic else None,
            "visible_to_next_reviewer": True,
            "created_at": _stored_instant(now),
        })

    if requested is not None:
        case["requested_resolution"] = requested

    if pickup is not None:
        if not _case_policy(db, case["case_type"])["accepts_pickup_preference"]:
            raise Refusal(
                f"a {case['case_type']} does not carry a pickup preference")
        preference = db["case_preferences"].get(case["case_id"])
        if preference is None:
            _add(db, "case_preferences", {
                "case_id": case["case_id"],
                "pickup_location": pickup,
                "visible_to_next_reviewer": True,
                "recorded_at": _stored_instant(now),
            })
        else:
            preference.update(pickup_location=pickup, recorded_at=_stored_instant(now))

    # A preference changes what the next reviewer will do; a note only tells
    # them something. The two outcomes are distinct in the registry and the
    # caller is told which one happened, along with the preference as stored.
    status = "preference_added" if (pickup is not None or requested is not None) \
        else "note_added"
    return compact([
        ("status", status),
        ("visible_to_next_reviewer", True),
        ("requested_resolution", case["requested_resolution"] if requested is not None else None),
        ("preferences", {"pickup": pickup} if pickup is not None else None),
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

    # Each send is a new message with its own id, including a resend of a
    # summary that went out before. The row records whose verified contact it
    # went to, so the actual address is one join away; the result shows it
    # masked, as the desk displays it.
    now = _now(db)
    status = template["initial_status"]
    notification_id = new_id(db, "notification")
    _add(db, "notifications", _notification_row(
        notification_id=notification_id,
        case_id=case["case_id"],
        order_reference=case["order_reference"],
        channel=args["channel"],
        template=template["template"],
        customer_id=customer["customer_id"],
        status=status,
        optional_photo_link=template["optional_photo_link"],
        sent_at=_stored_instant(now) if status != "queued" else None,
        created_at=_stored_instant(now),
    ))

    return {
        "notification_id": notification_id,
        "status": status,
        "masked_destination": destination,
        "case_id": case["case_id"],
        "case_number": case["case_number"],
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

# Tools that change Westline's records. Every other tool is a pure read.
WRITE_TOOLS = {
    "open_delivery_trace",
    "open_refund_trace",
    "create_replacement_order",
    "update_case",
    "send_case_notification",
    "transfer_to_specialist",
}

# Reads write nothing.
READ_SIDE_EFFECTS = {}

# Columns that only record when something happened. The DB score leaves them
# out, so a run is judged on what the agent did, not on the second it did it.
# orders.placed_on is the calendar day a replacement order was placed.
CLOCK_COLUMNS = {
    "cases": ["opened_at"],
    "case_notes": ["created_at"],
    "case_preferences": ["recorded_at"],
    "notifications": ["created_at", "sent_at", "delivered_at"],
    "orders": ["placed_on"],
    "replacement_orders": ["created_at"],
    "specialist_transfers": ["created_at"],
}
