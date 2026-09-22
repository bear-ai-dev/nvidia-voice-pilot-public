"""Westline retail tool handlers.

Each handler has the signature `handler(cur, args) -> dict` where `cur` is a dict
cursor inside a transaction and `args` is the already schema-validated arguments
object. The returned dict is the tool result, serialized as-is.

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

Human-relative time. Deadlines and scan times are stored as instants and
rendered against the scenario clock, so "18:00 tomorrow" is computed from
2026-08-26T18:00 and the frozen clock rather than stored as a sentence.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from db import NotFound, ToolRefusal, all_rows, allocate_id, one, scenario_value
from projection import as_float, as_int, as_list_always, compact

# Case states that still represent work in progress. Anything else is history
# and must not be offered to a caller as something that can still be acted on.
OPEN_CASE_STATUSES = [
    "open", "awaiting_carrier_response", "pending_customer_or_external_response",
    "reviewing_merchant_and_tender_records", "awaiting_external_settlement",
    "eligibility_determined", "resolution_eligible_or_ineligible",
]


# ---------------------------------------------------------------------------
# scenario clock and rendering
# ---------------------------------------------------------------------------


def _now(cur) -> dt.datetime:
    return dt.datetime.fromisoformat(scenario_value(cur, "scenario_time"))


def _money(value):
    """Render a monetary column the way this domain's results render money.

    Whole amounts are JSON integers and everything else is a JSON decimal. Every
    recorded retail amount follows that rule, so it is applied once here rather
    than chosen field by field.
    """
    if value is None:
        return None
    if isinstance(value, Decimal) and value == value.to_integral_value():
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


def _mask_reference(cur, order_reference: str) -> str:
    """Render an order reference the way this conversation's results render it.

    The desk discloses a redacted reference rather than the internal one. The
    format is a scenario setting because the sibling retail conversations
    redact the same value differently.
    """
    template = scenario_value(cur, "order_reference_mask") or "{last4}"
    return template.format(last4=order_reference[-4:], reference=order_reference)


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------


def _common_suffix_length(left: str, right: str) -> int:
    length = 0
    while (length < len(left) and length < len(right)
           and left[-1 - length] == right[-1 - length]):
        length += 1
    return length


def _resolve_order(cur, reference: str, customer_email: str | None = None) -> dict:
    """Resolve a full or partial order reference, optionally scoped to a customer.

    Policy allows a caller to give the last digits of an order number, so the
    desk matches on the longest trailing run of digits and refuses when two
    orders are equally good matches. An email scopes the search: an order
    reference that belongs to somebody else is not merely a worse match, it is
    not a candidate at all.
    """
    digits = "".join(character for character in reference if character.isdigit())
    if not digits:
        raise NotFound(f"{reference!r} contains no order digits to resolve")

    params: list = []
    scope = ""
    if customer_email is not None:
        customer = one(
            cur,
            "SELECT customer_id FROM customers WHERE lower(email) = lower(%s)",
            (customer_email.strip(),),
        )
        if customer is None:
            raise NotFound(f"no customer with the email {customer_email!r}")
        scope = "WHERE customer_id = %s"
        params.append(customer["customer_id"])

    candidates = all_rows(
        cur,
        f"SELECT order_reference FROM orders {scope} ORDER BY order_reference",
        tuple(params),
    )
    exact = [row for row in candidates if row["order_reference"] == digits]
    if not exact:
        minimum = int(scenario_value(cur, "min_reference_suffix_digits") or 4)
        required = min(len(digits), minimum)
        scored = [(_common_suffix_length(row["order_reference"], digits),
                   row["order_reference"]) for row in candidates]
        best = max((length for length, _ in scored), default=0)
        if best < required:
            raise NotFound(f"no order matches the reference {reference!r}")
        winners = [ref for length, ref in scored if length == best]
        if len(winners) > 1:
            raise ToolRefusal(
                f"the reference {reference!r} matches more than one order equally well",
                {"candidate_count": len(winners),
                 "matched_trailing_digits": best},
            )
        exact = [{"order_reference": winners[0]}]

    order = one(
        cur,
        """
        SELECT o.order_reference, o.customer_id, o.placed_on, o.fulfillment_status,
               o.destination_label, o.replaces_order_reference, o.representative_item,
               c.display_name, c.masked_email, c.masked_phone, c.fulfillment_region,
               c.address_label
          FROM orders o
          JOIN customers c ON c.customer_id = o.customer_id
         WHERE o.order_reference = %s
        """,
        (exact[0]["order_reference"],),
    )
    if order is None:
        raise NotFound(f"no order matches the reference {reference!r}")
    return order


def _resolve_variant(cur, product_reference: str) -> dict:
    """Resolve a catalog variant from a variant, product, or order-item reference."""
    variant = one(
        cur,
        """
        SELECT variant_reference, product_reference, display_name, color,
               in_stock, same_variant_in_stock, current_price
          FROM product_variants
         WHERE variant_reference = %s
        """,
        (product_reference,),
    )
    if variant:
        return variant

    item = one(
        cur,
        "SELECT variant_reference FROM order_items WHERE item_reference = %s",
        (product_reference,),
    )
    if item and item["variant_reference"]:
        return _resolve_variant(cur, item["variant_reference"])

    variant = one(
        cur,
        """
        SELECT variant_reference, product_reference, display_name, color,
               in_stock, same_variant_in_stock, current_price
          FROM product_variants
         WHERE product_reference = %s
         ORDER BY variant_reference
         LIMIT 1
        """,
        (product_reference,),
    )
    if variant is None:
        raise NotFound(f"no product or variant matches {product_reference!r}")
    return variant


# ---------------------------------------------------------------------------
# progressive section reads
# ---------------------------------------------------------------------------


def _serve_section(cur, order_reference: str, section: str):
    """Serve a section from its read model, advancing the read count.

    Returns (True, payload) when the order has a read model for this section,
    where a payload of None means the service discloses nothing at this depth.
    Returns (False, None) when it has none, in which case the caller projects
    the section from the normalized tables instead.
    """
    views = all_rows(
        cur,
        """
        SELECT view_index, payload
          FROM section_view
         WHERE order_reference = %s AND section = %s
         ORDER BY view_index
        """,
        (order_reference, section),
    )
    if not views:
        return False, None

    served = one(
        cur,
        """
        INSERT INTO section_read_cursor (order_reference, section, reads_served)
        VALUES (%s, %s, 1)
        ON CONFLICT (order_reference, section) DO UPDATE
            SET reads_served = section_read_cursor.reads_served + 1
        RETURNING reads_served
        """,
        (order_reference, section),
    )
    # The deepest disclosure repeats once it has been reached; the service does
    # not fall back to a shallower one on a fourth look.
    index = min(served["reads_served"] - 1, len(views) - 1)
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


def _items(cur, order_reference: str, include: list[str]) -> list[dict]:
    rows = all_rows(
        cur,
        """
        SELECT item_reference, product_reference, name, variant_label, color,
               total_after_tax, currency
          FROM order_items
         WHERE order_reference = %s
         ORDER BY line_no
        """,
        (order_reference,),
    )
    shape = _item_projection(include)
    projected = []
    for row in rows:
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


def _latest_scan(cur, order_reference: str) -> dict | None:
    return one(
        cur,
        """
        SELECT scanned_at, scanned_at_display, location, evidence_location,
               unit_number, locker, photo_reference, possible_misscan
          FROM carrier_scans
         WHERE order_reference = %s
         ORDER BY scanned_at DESC, scan_seq DESC
         LIMIT 1
        """,
        (order_reference,),
    )


def _public_case_number(cur, case_id: str) -> str:
    if case_id == scenario_value(cur, "target_case_id"):
        return scenario_value(cur, "target_case_number") or case_id
    return case_id


def _open_cases(cur, customer_id: str) -> list[dict]:
    return all_rows(
        cur,
        """
        SELECT k.case_number, k.order_reference, k.case_type, k.status,
               k.item_description, k.carrier_response, k.deadline_display,
               k.carrier_may_contact_customer, k.replacement_created,
               p.pickup_location,
               t.order_view_fields, t.related_view_fields
          FROM cases k
          JOIN case_type_policy t ON t.case_type = k.case_type
          LEFT JOIN case_preferences p ON p.case_number = k.case_number
         WHERE k.customer_id = %s AND k.status = ANY(%s)
         ORDER BY k.opened_at, k.case_number
        """,
        (customer_id, OPEN_CASE_STATUSES),
    )


def _case_view(cur, case: dict, reference: str) -> dict:
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
        "order_reference": _mask_reference(cur, case["order_reference"]),
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
        view["case_number"] = _public_case_number(cur, case_id)
    return view


def _advance_notification(cur, notification_id: str) -> dict:
    """Refresh a notification's delivery state and return the refreshed row.

    Delivery receipts arrive from the mail provider after the message is handed
    over. The scenario clock is frozen, so the refresh is driven by the read
    rather than by elapsed time: each look at the notification collects the next
    receipt the provider has for that message, and the last one repeats.
    """
    return one(
        cur,
        """
        UPDATE notifications
           SET status_index = least(status_index + 1,
                                    array_length(status_progression, 1) - 1),
               status = status_progression[least(status_index + 2,
                                                 array_length(status_progression, 1))]
         WHERE notification_id = %s
        RETURNING notification_id, status, message_type, subject_prefix,
                  optional_photo_link, photo_link_section, template
        """,
        (notification_id,),
    )


def _notification_view(cur, notification: dict) -> dict:
    """Project a notification for an order read against its template's field list."""
    fields = one(
        cur,
        "SELECT order_view_fields FROM notification_templates WHERE template = %s",
        (notification["template"],),
    )["order_view_fields"]
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


def lookup_customer(cur, args) -> dict:
    email = args.get("email")
    customer_id = args.get("customer_id")
    if not email and not customer_id:
        raise ToolRefusal(
            "a verified email or customer identifier is required to look a customer up")

    clauses, params = [], []
    if email:
        clauses.append("lower(email) = lower(%s)")
        params.append(email.strip())
    if customer_id:
        clauses.append("customer_id = %s")
        params.append(customer_id)

    matches = all_rows(
        cur,
        f"""
        SELECT customer_id, display_name
          FROM customers
         WHERE {' AND '.join(clauses)}
         ORDER BY customer_id
        """,
        tuple(params),
    )
    if not matches:
        return {"customer_id": "", "match": "none"}
    if len(matches) > 1:
        return {"customer_id": "", "match": "multiple"}

    customer = matches[0]
    # Recent orders exist to let a caller identify a second order they cannot
    # name, so the list is scoped to orders that still carry open support work.
    # Disclosing the rest of the account's history would answer a question
    # nobody asked.
    orders = all_rows(
        cur,
        """
        SELECT o.order_reference, o.representative_item, k.case_number
          FROM orders o
          JOIN cases k ON k.order_reference = o.order_reference
                      AND k.status = ANY(%s)
         WHERE o.customer_id = %s
         ORDER BY o.placed_on DESC, o.order_reference
        """,
        (OPEN_CASE_STATUSES, customer["customer_id"]),
    )
    return {
        "customer_id": customer["customer_id"],
        "match": "unique",
        "display_name": customer["display_name"],
        "recent_orders": [
            compact([
                ("order_reference", _mask_reference(cur, row["order_reference"])),
                ("item", row["representative_item"]),
                ("open_case_id", row["case_number"]),
                ("open_case_number", _public_case_number(cur, row["case_number"])),
            ])
            for row in orders
        ],
    }


def get_order(cur, args) -> dict:
    order = _resolve_order(cur, args["order_reference"], args["customer_email"])
    reference = order["order_reference"]
    include = list(args.get("include") or [])

    result: list[tuple] = [("order_reference", _mask_reference(cur, reference))]

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
        fields = (scenario_value(cur, "order_customer_fields")
                  or "customer_id,display_name,verified_email")
        result.append(("customer", compact([
            (field, available[field]) for field in fields.split(",")])))
        result.append(("items", _items(cur, reference, include)))

    fulfillment: list[tuple] = []
    if "fulfillment" in include:
        scan = _latest_scan(cur, reference)
        fulfillment.append(("status", order["fulfillment_status"]))
        if scan:
            fulfillment.append(("latest_scan", {
                "time": scan["scanned_at_display"],
                "location": scan["location"],
                # Null is the answer "the carrier took none", not an unknown.
                "photo": scan["photo_reference"],
            }))
    if "carrier_scans" in include:
        served, payload = _serve_section(cur, reference, "carrier_scans")
        if served:
            if payload is not None:
                fulfillment.append(("carrier_evidence", payload))
        else:
            scan = _latest_scan(cur, reference)
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
        served, payload = _serve_section(cur, reference, "payments")
        if served:
            if payload is not None:
                result.append(("payments", payload))
        else:
            rows = all_rows(
                cur,
                """
                SELECT tender_type, amount, currency, original_card_last4
                  FROM payments
                 WHERE order_reference = %s
                 ORDER BY payment_seq
                """,
                (reference,),
            )
            result.append(("payments", [
                compact([
                    ("type", row["tender_type"]),
                    ("amount", _money(row["amount"])),
                    ("currency", row["currency"]),
                    ("original_card_last4", row["original_card_last4"]),
                ])
                for row in rows
            ]))

    if "refunds" in include:
        served, payload = _serve_section(cur, reference, "refunds")
        if served:
            if payload is not None:
                result.append(("refunds", payload))
        else:
            rows = all_rows(
                cur,
                """
                SELECT tender_type, amount, currency, status, available_balance,
                       used, delivery, original_card_last4, initiation_source
                  FROM refunds
                 WHERE order_reference = %s
                 ORDER BY refund_seq
                """,
                (reference,),
            )
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
                for row in rows
            ]))

    if "cases" in include:
        result.append(("cases", [_case_view(cur, case, reference)
                                 for case in _open_cases(cur, order["customer_id"])]))

    if "eligible_resolutions" in include:
        served, payload = _serve_section(cur, reference, "eligible_resolutions")
        if served:
            if payload is not None:
                result.append(("eligible_resolutions", payload))
        else:
            rows = all_rows(
                cur,
                """
                SELECT resolution_type, preserves_original_price, return_required,
                       photo_required, optional_photo_upload_available,
                       photo_upload_blocks_fulfillment, estimated_delivery_display,
                       default_fulfillment
                  FROM eligible_resolutions
                 WHERE order_reference = %s
                 ORDER BY position
                """,
                (reference,),
            )
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
                for row in rows
            ]))

    if "notifications" in include:
        rows = all_rows(
            cur,
            """
            SELECT n.notification_id
              FROM notifications n
              LEFT JOIN cases k ON k.case_number = n.case_number
             WHERE n.order_reference = %s OR k.order_reference = %s
             ORDER BY n.created_at, n.notification_id
            """,
            (reference, reference),
        )
        result.append(("notifications", [
            _notification_view(cur, _advance_notification(cur, row["notification_id"]))
            for row in rows
        ]))

    return compact(result)


def get_product(cur, args) -> dict:
    variant = _resolve_variant(cur, args["product_reference"])
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


def _require_items(cur, order_reference: str, item_references: list[str]) -> None:
    known = {row["item_reference"] for row in all_rows(
        cur,
        "SELECT item_reference FROM order_items WHERE order_reference = %s",
        (order_reference,),
    )}
    missing = [reference for reference in item_references if reference not in known]
    if missing:
        raise ToolRefusal(
            "those items are not on that order",
            {"order_reference": _mask_reference(cur, order_reference),
             "unknown_item_references": missing},
        )


def _pickup_site(cur, location: str) -> str:
    """Normalize a spoken pickup location to the site a reviewer instruction names.

    A customer asks for "the West 23rd Street pickup counter"; the instruction a
    reviewer reads is about checking West 23rd Street. The endings that get
    stripped are rows, so a new label form is a seed change rather than a code
    change.
    """
    text = location.strip()
    for row in all_rows(
            cur,
            "SELECT suffix FROM pickup_site_suffixes ORDER BY length(suffix) DESC",
    ):
        suffix = row["suffix"]
        if text.lower().endswith(suffix.lower()):
            return text[: -len(suffix)].strip()
    return text


def _case_policy(cur, case_type: str) -> dict:
    policy = one(
        cur, "SELECT * FROM case_type_policy WHERE case_type = %s", (case_type,))
    if policy is None:
        raise ToolRefusal(f"the desk has no policy for a {case_type}")
    return policy


def open_delivery_trace(cur, args) -> dict:
    order = _resolve_order(cur, args["order_reference"])
    _require_items(cur, order["order_reference"], args["item_references"])
    policy = _case_policy(cur, "delivery_trace")
    now = _now(cur)

    deadline_day = now.date() + dt.timedelta(days=policy["deadline_offset_days"])
    hour, minute = policy["deadline_local_time"].split(":")
    deadline = dt.datetime(deadline_day.year, deadline_day.month, deadline_day.day,
                           int(hour), int(minute), tzinfo=now.tzinfo)
    deadline_display = _clock_display(deadline, now)

    case_number = allocate_id(cur, "support_case")
    cur.execute(
        """
        INSERT INTO cases
            (case_number, order_reference, customer_id, case_type, status, reason,
             item_description, carrier_response, deadline_at, deadline_display,
             carrier_may_contact_customer, replacement_created,
             requested_resolution, needed_by, approval_required, approval_channel,
             next_action, eligibility_triggers, pickup_guaranteed, opened_at)
        VALUES (%s, %s, %s, 'delivery_trace', %s, %s, %s, 'none', %s, %s, %s,
                FALSE, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (case_number, order["order_reference"], order["customer_id"],
         policy["initial_status"], args["reason"], order["representative_item"],
         deadline, deadline_display, policy["carrier_may_contact_customer"],
         args.get("requested_resolution"), args.get("needed_by"),
         policy["approval_required"], policy["approval_channel"],
         policy["next_action"], policy["eligibility_triggers"],
         policy["pickup_guaranteed"], now),
    )
    for item_reference in args["item_references"]:
        cur.execute(
            "INSERT INTO case_items (case_number, item_reference) VALUES (%s, %s)",
            (case_number, item_reference),
        )

    # No confirmation has been sent yet: policy requires the customer to approve
    # a resolution through the trace notification, and the notification is a
    # separate authorized call.
    return compact([
        ("case_number", case_number),
        ("status", policy["initial_status"]),
        ("carrier_response_deadline", deadline_display),
        ("replacement_created", False),
        ("eligibility_triggers", as_list_always(policy["eligibility_triggers"])),
        ("next_action", policy["next_action"]),
        ("approval_required", policy["approval_required"]),
        ("approval_channel", policy["approval_channel"]),
        ("notification_status", "not_sent"),
    ])


def open_refund_trace(cur, args) -> dict:
    order = _resolve_order(cur, args["order_reference"])
    reference = order["order_reference"]

    digits = "".join(c for c in args["return_reference"] if c.isdigit())
    returns = all_rows(
        cur,
        """
        SELECT return_reference, return_status
          FROM returns
         WHERE order_reference = %s
         ORDER BY return_reference
        """,
        (reference,),
    )
    matched = [row for row in returns
               if _common_suffix_length(row["return_reference"], digits) >= len(digits)]
    if len(matched) != 1:
        raise ToolRefusal(
            "that return reference does not identify exactly one return on the order",
            {"candidate_count": len(matched)},
        )
    accepted_return = matched[0]

    card = "".join(c for c in args["payment_reference"] if c.isdigit())[-4:]
    refund = one(
        cur,
        """
        SELECT refund_seq, tender_type, amount, status, original_card_last4
          FROM refunds
         WHERE order_reference = %s
           AND (original_card_last4 = %s OR %s = '')
         ORDER BY refund_seq
         LIMIT 1
        """,
        (reference, card, card),
    )
    if refund is None:
        raise ToolRefusal(
            "no refund on that order was issued against that payment reference")
    if abs(Decimal(str(args["amount"])) - refund["amount"]) > Decimal("0.005"):
        raise ToolRefusal(
            "the amount under review does not match the refund on that tender",
            {"refund_amount": _money(refund["amount"])},
        )

    policy = _case_policy(cur, "refund_trace")
    now = _now(cur)
    case_number = allocate_id(cur, "support_case")
    # Evidence is attached when the return the customer named is a completed
    # return on the same order, which is the only thing Westline can attest to.
    evidence_attached = accepted_return["return_status"] == "complete"
    cur.execute(
        """
        INSERT INTO cases
            (case_number, order_reference, customer_id, case_type, status, reason,
             item_description, replacement_created, review_window_min_days,
             review_window_max_days, duplicate_refund_blocked,
             return_evidence_attached, return_reference, payment_reference,
             amount_under_review, opened_at)
        VALUES (%s, %s, %s, 'refund_trace', %s, 'missing_refund', %s, FALSE,
                %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (case_number, reference, order["customer_id"], policy["initial_status"],
         order["representative_item"], policy["review_window_min_days"],
         policy["review_window_max_days"], policy["duplicate_refund_blocked"],
         evidence_attached, accepted_return["return_reference"],
         args["payment_reference"], args["amount"], now),
    )
    return {
        "case_number": case_number,
        "status": policy["initial_status"],
        "review_window_business_days": [policy["review_window_min_days"],
                                        policy["review_window_max_days"]],
        "duplicate_refund_blocked": policy["duplicate_refund_blocked"],
        "return_evidence_attached": evidence_attached,
    }


def create_replacement_order(cur, args) -> dict:
    original = _resolve_order(cur, args["order_reference"])
    reference = original["order_reference"]
    _require_items(cur, reference, args["item_references"])

    if not args["customer_authorized"]:
        raise ToolRefusal(
            "policy requires explicit customer authorization before a replacement "
            "order is created")

    eligibility = one(
        cur,
        """
        SELECT preserves_original_price, return_required, photo_required,
               optional_photo_upload_available, photo_upload_blocks_fulfillment,
               estimated_delivery_on, estimated_delivery_display
          FROM eligible_resolutions
         WHERE order_reference = %s AND resolution_type = 'replacement'
        """,
        (reference,),
    )
    if eligibility is None:
        raise ToolRefusal(
            "that order is not currently eligible for a replacement",
            {"order_reference": _mask_reference(cur, reference)},
        )

    lines = all_rows(
        cur,
        """
        SELECT i.item_reference, i.line_no, i.variant_reference, i.product_reference,
               i.name, i.variant_label, i.color, i.total_after_tax, i.currency,
               v.in_stock, v.same_variant_in_stock, v.current_price,
               p.disposal_disposition, p.safety_instruction
          FROM order_items i
          LEFT JOIN product_variants v ON v.variant_reference = i.variant_reference
          LEFT JOIN products p ON p.product_reference = i.product_reference
         WHERE i.order_reference = %s AND i.item_reference = ANY(%s)
         ORDER BY i.line_no
        """,
        (reference, list(args["item_references"])),
    )
    unavailable = [line["item_reference"] for line in lines
                   if line["in_stock"] is False or line["same_variant_in_stock"] is False]
    if unavailable:
        raise ToolRefusal(
            "the replacement stock for those items is not available",
            {"unavailable_item_references": unavailable},
        )

    now = _now(cur)
    replacement_reference = allocate_id(cur, "order", "replacement")
    cur.execute(
        """
        INSERT INTO orders
            (order_reference, customer_id, placed_on, fulfillment_status,
             destination_label, replaces_order_reference, representative_item)
        VALUES (%s, %s, %s, 'processing', %s, %s, %s)
        """,
        (replacement_reference, original["customer_id"], now.date(),
         args.get("fulfillment_location") or original["destination_label"],
         reference, original["representative_item"]),
    )
    for line in lines:
        cur.execute(
            """
            INSERT INTO order_items
                (item_reference, order_reference, line_no, variant_reference,
                 product_reference, name, variant_label, color, total_after_tax,
                 currency)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (f"{replacement_reference}-{line['item_reference']}",
             replacement_reference, line["line_no"], line["variant_reference"],
             line["product_reference"], line["name"], line["variant_label"],
             line["color"], line["total_after_tax"], line["currency"]),
        )

    # The eligible original price is what the customer already paid. Where the
    # eligibility does not preserve it, the difference against today's catalog
    # price is what falls due, and never less than nothing.
    if eligibility["preserves_original_price"]:
        balance = Decimal("0.00")
    else:
        original_total = sum((line["total_after_tax"] or Decimal("0")) for line in lines)
        current_total = sum((line["current_price"] or line["total_after_tax"]
                             or Decimal("0")) for line in lines)
        balance = max(Decimal("0.00"), current_total - original_total)

    center = one(
        cur,
        """
        SELECT display_name
          FROM distribution_centers
         WHERE region = %s
         ORDER BY dc_id
         LIMIT 1
        """,
        (original["fulfillment_region"],),
    )

    estimated_on = eligibility["estimated_delivery_on"]
    estimated_display = eligibility["estimated_delivery_display"]
    if estimated_on is not None and estimated_display is None:
        estimated_display = _delivery_display(estimated_on, now)

    disposition = None
    safety = None
    if not eligibility["return_required"]:
        disposition = next((line["disposal_disposition"] for line in lines
                            if line["disposal_disposition"]), None)
        safety = next((line["safety_instruction"] for line in lines
                       if line["safety_instruction"]), None)

    cur.execute(
        """
        INSERT INTO replacement_orders
            (replacement_order_reference, original_order_reference, reason, status,
             balance_due, currency, fulfillment_method, fulfillment_location,
             estimated_delivery_on, estimated_delivery_display, estimate_guaranteed,
             distribution_center, distribution_center_status,
             tracking_notifications, return_required, disposition, safety,
             created_at)
        VALUES (%s, %s, %s, 'created', %s, 'USD', %s, %s, %s, %s, FALSE, %s,
                'provisional_until_shipped', TRUE, %s, %s, %s, %s)
        """,
        (replacement_reference, reference, args["reason"], balance,
         args["fulfillment_method"],
         args.get("fulfillment_location") or original["address_label"],
         estimated_on, estimated_display,
         center["display_name"] if center else None,
         eligibility["return_required"], disposition, safety, now),
    )

    template = one(
        cur,
        "SELECT * FROM notification_templates WHERE template = 'replacement_confirmation'",
        (),
    )
    notification_id = f"notification-{replacement_reference}"
    cur.execute(
        """
        INSERT INTO notifications
            (notification_id, case_number, order_reference, channel, template,
             message_type, masked_destination, status, status_index,
             status_progression, subject_prefix, optional_photo_link,
             photo_link_section, included_fields, sent_at, created_at)
        VALUES (%s, NULL, %s, 'email', %s, %s, %s, %s, 0, %s, %s, %s, %s, %s,
                %s, %s)
        """,
        (notification_id, replacement_reference, template["template"],
         template["message_type"], original["masked_email"],
         template["initial_status"], template["delivery_progression"],
         template["subject_prefix"],
         # The optional photo link is offered because the eligibility offers it,
         # not because a replacement always carries one.
         bool(eligibility["optional_photo_upload_available"]),
         template["photo_link_section"], template["included_fields"], now, now),
    )

    # Any trace still open on the original order now has a replacement against
    # it, and a later read of that case must say so.
    cur.execute(
        """
        UPDATE cases
           SET replacement_created = TRUE
         WHERE order_reference = %s AND status = ANY(%s)
        """,
        (reference, OPEN_CASE_STATUSES),
    )

    return compact([
        ("replacement_order_reference", _mask_reference(cur, replacement_reference)),
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


def update_case(cur, args) -> dict:
    case = one(cur, "SELECT * FROM cases WHERE case_number = %s", (args["case_number"],))
    if case is None:
        raise NotFound(f"unknown case {args['case_number']!r}")

    note = args.get("note")
    pickup = args.get("preferred_pickup_location")
    requested = args.get("requested_resolution")
    if note is None and pickup is None and requested is None:
        raise ToolRefusal("no note, resolution, or preference was supplied")

    now = _now(cur)
    fee_note = False
    if note is not None:
        topic = one(
            cur,
            """
            SELECT topic, discloses_fee_decision
              FROM note_topics
             WHERE %s ILIKE match_pattern
             ORDER BY discloses_fee_decision DESC, topic
             LIMIT 1
            """,
            (note,),
        )
        fee_note = bool(topic and topic["discloses_fee_decision"])
        cur.execute(
            """
            INSERT INTO case_notes
                (case_number, note_no, note, topic, visible_to_next_reviewer, created_at)
            SELECT %s, coalesce(max(note_no), 0) + 1, %s, %s, TRUE, %s
              FROM case_notes WHERE case_number = %s
            """,
            (case["case_number"], note, topic["topic"] if topic else None, now,
             case["case_number"]),
        )

    if requested is not None:
        cur.execute(
            "UPDATE cases SET requested_resolution = %s WHERE case_number = %s",
            (requested, case["case_number"]),
        )

    review_instruction = None
    if pickup is not None:
        policy = _case_policy(cur, case["case_type"])
        template = policy["preference_instruction_template"]
        if not template:
            raise ToolRefusal(
                f"a {case['case_type']} does not carry a pickup preference")
        site = _pickup_site(cur, pickup)
        review_instruction = template.replace("{site}", site)
        cur.execute(
            """
            INSERT INTO case_preferences
                (case_number, pickup_location, pickup_site, review_instruction,
                 visible_to_next_reviewer, recorded_at)
            VALUES (%s, %s, %s, %s, TRUE, %s)
            ON CONFLICT (case_number) DO UPDATE
                SET pickup_location = EXCLUDED.pickup_location,
                    pickup_site = EXCLUDED.pickup_site,
                    review_instruction = EXCLUDED.review_instruction,
                    recorded_at = EXCLUDED.recorded_at
            """,
            (case["case_number"], pickup, site, review_instruction, now),
        )

    # A preference changes what the next reviewer will do; a note only tells
    # them something. The two outcomes are distinct in the registry and the
    # caller is told which one happened.
    status = "preference_added" if (pickup is not None or requested is not None) \
        else "note_added"
    return compact([
        ("status", status),
        ("visible_to_next_reviewer", True),
        ("review_instruction", review_instruction),
        ("pickup_guaranteed", case["pickup_guaranteed"] if pickup is not None else None),
        # Policy forbids approving a bank fee while the trace is open, so a note
        # that raises one is answered rather than silently filed.
        ("fee_reimbursement_approved",
         case["fee_reimbursement_approved"] if fee_note else None),
    ])


def send_case_notification(cur, args) -> dict:
    case = one(
        cur,
        """
        SELECT k.case_number, k.order_reference, c.masked_email, c.masked_phone
          FROM cases k
          JOIN customers c ON c.customer_id = k.customer_id
         WHERE k.case_number = %s
        """,
        (args["case_number"],),
    )
    if case is None:
        raise NotFound(f"unknown case {args['case_number']!r}")

    template = one(
        cur,
        "SELECT * FROM notification_templates WHERE template = %s",
        (args["template"],),
    )
    if template is None:
        raise ToolRefusal(f"unknown notification template {args['template']!r}")

    destination = case["masked_email"] if args["channel"] == "email" else case["masked_phone"]
    if not destination:
        raise ToolRefusal(
            f"no verified {args['channel']} destination is on file for this case")

    now = _now(cur)
    notification_id = f"notification-{case['case_number']}"
    # A resend is the same message going out again, so it keeps its identifier
    # and restarts from the delivery state a fresh send has.
    cur.execute(
        """
        INSERT INTO notifications
            (notification_id, case_number, order_reference, channel, template,
             message_type, masked_destination, status, status_index,
             status_progression, subject_prefix, optional_photo_link,
             photo_link_section, included_fields, sent_at, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (notification_id) DO UPDATE
            SET channel = EXCLUDED.channel,
                template = EXCLUDED.template,
                message_type = EXCLUDED.message_type,
                masked_destination = EXCLUDED.masked_destination,
                status = EXCLUDED.status,
                status_index = 0,
                status_progression = EXCLUDED.status_progression,
                included_fields = EXCLUDED.included_fields,
                sent_at = EXCLUDED.sent_at
        """,
        (notification_id, case["case_number"], case["order_reference"], args["channel"],
         template["template"], template["message_type"], destination,
         template["initial_status"], template["delivery_progression"],
         template["subject_prefix"], template["optional_photo_link"],
         template["photo_link_section"], template["included_fields"], now, now),
    )

    return {
        "notification_id": notification_id,
        "status": template["initial_status"],
        "masked_destination": destination,
        "case_number": case["case_number"],
        "included_fields": as_list_always(template["included_fields"]),
    }


def transfer_to_specialist(cur, args) -> dict:
    transfer_id = allocate_id(cur, "specialist_transfer")
    cur.execute(
        """
        INSERT INTO specialist_transfers (transfer_id, reason, summary, status,
                                          created_at)
        VALUES (%s, %s, %s, 'transferred', %s)
        """,
        (transfer_id, args["reason"], args["summary"], _now(cur)),
    )
    return {"status": "transferred", "transfer_id": transfer_id}


HANDLERS = {
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

# Tools that change Westline's records. The grading layer holds reads free — an
# agent may look at anything as often as it likes — so the distinction has to be
# stated somewhere, and the handlers are where it is known.
#
# The test is whether the tool changes a business fact, not whether it writes a
# row. get_order writes on every call: it advances section_read_cursor and
# collects the next delivery receipt on any notification it discloses. Neither
# is a fact about the customer's order, so it stays a read.
WRITE_TOOLS = {
    "open_delivery_trace",
    "open_refund_trace",
    "create_replacement_order",
    "update_case",
    "send_case_notification",
    "transfer_to_specialist",
}
