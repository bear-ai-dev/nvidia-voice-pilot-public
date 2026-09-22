"""Pharmacy tools over the conversation's JSON database.

Ported from the PostgreSQL tool server in PR #17. Each tool is (db, args) -> result
and may mutate db in place; see env/toolkit.py for the row layout and helpers.

Tools hold domain logic only. Every identifier, amount, store hour, payer
decision, and queue estimate that appears in a result is read from the database,
never computed from wall time or generated at random, so a result is reproducible
and an operator can explain any value by pointing at a row.

The original server ran on a database created with the C.UTF-8 locale, so text
compares and sorts by code point, which is how Python compares str. Where it
matched text case-insensitively it used SQL lower(), which maps one character to
one character; `_sql_lower` reproduces that.
"""
from __future__ import annotations

import re
from datetime import date

from toolkit import (NotFound, Refusal, allocate_id, as_int, as_list_always, compact,
                     first, insert, rows, scenario_value)

# Key columns per table: a row's key in db[table] is these columns joined by "|".
KEY_COLUMNS = {
    "claim_overrides": [
        "override_id"
    ],
    "claims": [
        "claim_seq"
    ],
    "fill_queue": [
        "prescription_id"
    ],
    "id_allocator": [
        "entity_type",
        "scope"
    ],
    "insurance_plans": [
        "plan_id"
    ],
    "medications": [
        "medication_id"
    ],
    "notification_destinations": [
        "destination_id"
    ],
    "patients": [
        "patient_id"
    ],
    "plan_override_rules": [
        "plan_id",
        "reason"
    ],
    "prescriptions": [
        "prescription_id"
    ],
    "scenario": [
        "key"
    ],
    "specialist_transfers": [
        "transfer_id"
    ],
    "store_inventory": [
        "store_id",
        "medication_id"
    ],
    "stores": [
        "store_id"
    ],
    "transfer_requests": [
        "transfer_id"
    ]
}

# Prescription lifecycle states that count as active for a patient-facing read.
ACTIVE_WORKFLOW_STATUSES = {
    "received", "claim_pending", "claim_rejected", "claim_paid",
    "awaiting_pharmacist_verification", "ready_for_pickup",
}

# Override decisions that let a claim be paid.
USABLE_OVERRIDE_DECISIONS = ("approved", "approved_one_time")


# ---------------------------------------------------------------------------
# SQL text semantics
# ---------------------------------------------------------------------------


def _sql_lower(text: str) -> str:
    """SQL lower(): a per-character mapping, never changing the length.

    Python's str.lower() applies full Unicode case mapping, under which one
    character (U+0130, capital I with dot above) lowers to two. The database
    lowers it to a plain "i".
    """
    return "".join(ch.lower() if len(ch.lower()) == 1 else "i" for ch in text)


def _ilike(value: str, pattern: str) -> bool:
    """`value ILIKE pattern` with the default backslash escape.

    `%` matches any run of characters, `_` any one character, and a backslash
    makes the next character literal. Case is folded with SQL lower() on both
    sides, as the database does for a multibyte encoding.
    """
    value, pattern = _sql_lower(value), _sql_lower(pattern)
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


def _same_date(stored: str, supplied: str) -> bool:
    """`date_column = %s`: both sides compared as dates, not as text."""
    return date.fromisoformat(stored) == date.fromisoformat(supplied)


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


def lookup_patient(db, args) -> dict:
    full_name = _sql_lower(args["full_name"].strip())
    matches = [
        row for row in rows(db, "patients")
        if _sql_lower(row["full_name"]) == full_name
        and _same_date(row["date_of_birth"], args["date_of_birth"])
    ]
    if not matches:
        return {"patient_id": "", "match": "not_found"}
    if len(matches) > 1:
        return {"patient_id": "", "match": "multiple_matches"}

    patient_id = matches[0]["patient_id"]
    # Rows come back in key order, which is destination_id order.
    destinations = rows(db, "notification_destinations", patient_id=patient_id, verified=True)
    return {
        "patient_id": patient_id,
        "match": "unique",
        "verified_notification_destinations": [
            {
                "destination_id": d["destination_id"],
                "channel": d["channel"],
                "masked_destination": d["masked_destination"],
                "verified": d["verified"],
            }
            for d in destinations
        ],
    }


def _select_prescription(db, patient_id: str, medication_name: str | None) -> dict:
    """Most recently received active prescription, optionally by medication.

    Matching is two-way on the medication display name, because a caller says
    "albuterol inhaler" for a record named "albuterol inhaler" but might equally
    say "albuterol" for "albuterol nebulizer solution".
    """
    needle = medication_name.strip().lower() if medication_name else None
    candidates = []
    for p in rows(db, "prescriptions", patient_id=patient_id):
        if p["workflow_status"] not in ACTIVE_WORKFLOW_STATUSES:
            continue
        medication = db["medications"].get(p["medication_id"])
        if medication is None:
            continue
        name = _sql_lower(medication["display_name"])
        if needle is not None and not (name in needle or needle in name):
            continue
        candidates.append({**p, "medication_name": medication["display_name"]})

    if not candidates:
        raise NotFound("no active prescription matches that patient and medication")
    # ORDER BY received_at DESC, prescription_id: two stable sorts, the
    # tiebreak first.
    candidates.sort(key=lambda p: p["prescription_id"])
    candidates.sort(key=lambda p: p["received_at"], reverse=True)
    return candidates[0]


def _latest_claim(db, prescription_id: str) -> dict | None:
    claims = rows(db, "claims", prescription_id=prescription_id)
    return max(claims, key=lambda c: c["claim_seq"]) if claims else None


def _queue_row(db, prescription_id: str) -> dict | None:
    return db["fill_queue"].get(prescription_id)


def get_prescription(db, args) -> dict:
    rx = _select_prescription(db, args["patient_id"], args.get("medication_name"))
    store = db["stores"][rx["fill_store_id"]]

    claim = _latest_claim(db, rx["prescription_id"])
    claim_view = {"status": "not_submitted"}
    if claim:
        claim_view = compact([("status", claim["status"]), ("reason", claim["reason"])])

    queue = _queue_row(db, rx["prescription_id"]) or {"status": "blocked_by_claim"}
    queue_view = compact([
        ("status", queue.get("status")),
        ("position", queue.get("position")),
        ("estimated_minutes", queue.get("estimated_minutes")),
    ])

    # Registry property order. Comparison is order-insensitive, but building in
    # declared order keeps a result readable next to its schema.
    return compact([
        ("prescription_id", rx["prescription_id"]),
        ("medication_id", rx["medication_id"]),
        ("medication_name", rx["medication_name"]),
        ("received_at", rx["received_at"]),
        ("prescription_valid", rx["prescription_valid"]),
        ("workflow_status", rx["workflow_status"]),
        ("customer_facing_status", rx["customer_facing_status"]),
        ("fill_store", {
            "store_id": store["store_id"],
            "display_name": store["display_name"],
            "counter_closes_at": store["counter_closes_at"],
            "front_store_closes_later": store["front_store_closes_later"],
        }),
        ("claim", claim_view),
        ("queue", queue_view),
        ("notification", {"ready_alert_destination_id": rx["ready_alert_destination_id"]}
            if rx["ready_alert_destination_id"] else None),
        ("payment_options", as_list_always(rx["payment_options"])),
    ])


def get_store_inventory(db, args) -> dict:
    store_id, medication_id = args["store_id"], args["medication_id"]
    if store_id not in db["stores"]:
        raise NotFound(f"unknown store {store_id!r}")
    if medication_id not in db["medications"]:
        raise NotFound(f"unknown medication {medication_id!r}")

    row = db["store_inventory"].get(f"{store_id}|{medication_id}")
    # A store that carries no inventory row for a medication does not stock it.
    return {
        "store_id": store_id,
        "medication_id": medication_id,
        "in_stock": bool(row and row["in_stock"]),
        "reserved": bool(row and row["reserved"]),
    }


def search_pharmacy_locations(db, args) -> dict:
    stores = rows(db, "stores")

    origin_id = args.get("origin_store_id")
    if origin_id:
        origin = db["stores"].get(origin_id)
        if origin is None:
            raise NotFound(f"unknown origin store {origin_id!r}")
        # A search anchored on a fill store covers that store's district. Without
        # this the whole chain would surface as "nearby".
        stores = [s for s in stores
                  if s["district"] == origin["district"] and s["store_id"] != origin_id]

    open_after = args.get("open_after_local_time")
    if open_after:
        # Both sides are HH:MM text, so a text comparison orders them by time.
        stores = [s for s in stores if s["counter_closes_at"] > open_after]

    query = args.get("query")
    if query:
        pattern = f"%{query}%"
        stores = [s for s in stores
                  if _ilike(s["display_name"], pattern) or _ilike(s["address"] or "", pattern)]

    stores.sort(key=lambda s: (s["proximity_rank"], s["store_id"]))
    return {
        "locations": [
            compact([
                ("store_id", s["store_id"]),
                ("display_name", s["display_name"]),
                ("address", s["address"]),
                ("counter_closes_at", s["counter_closes_at"]),
                ("front_store_closes_at", s["front_store_closes_at"]),
                ("timezone", s["timezone"]),
                ("services", s["services"]),
            ])
            for s in stores
        ]
    }


# ---------------------------------------------------------------------------
# mutations
# ---------------------------------------------------------------------------


def _prescription_patient(db, prescription_id: str) -> tuple[dict, dict] | None:
    """A prescription with its patient, or None for an unknown prescription."""
    rx = db["prescriptions"].get(prescription_id)
    if rx is None:
        return None
    return rx, db["patients"][rx["patient_id"]]


def request_claim_override(db, args) -> dict:
    prescription_id, reason = args["prescription_id"], args["reason"]
    found = _prescription_patient(db, prescription_id)
    if found is None:
        raise NotFound(f"unknown prescription {prescription_id!r}")
    plan_id = found[1]["insurance_plan_id"]

    # A patient without a plan matches no rule: plan_id = NULL is never true.
    rule = None if plan_id is None else first(db, "plan_override_rules",
                                              plan_id=plan_id, reason=reason)
    if rule is None:
        raise Refusal(f"payer plan {plan_id!r} has no policy for reason {reason!r}")

    requested_at = scenario_value(db, "scenario_time")
    # The override identifier is the plan's, per reason, so asking again upserts
    # the existing row: only the urgency and the request time are refreshed, and
    # the prescription, decision, and any consumption stay as first recorded.
    existing = db["claim_overrides"].get(rule["override_id"])
    if existing is not None:
        existing["urgency_context"] = args.get("urgency_context")
        existing["requested_at"] = requested_at
    else:
        insert(db, "claim_overrides", {
            "override_id": rule["override_id"],
            "prescription_id": prescription_id,
            "reason": reason,
            "status": rule["decision"],
            "urgency_context": args.get("urgency_context"),
            "requested_at": requested_at,
            "consumed_at": None,
        }, KEY_COLUMNS)
    return {"override_id": rule["override_id"], "status": rule["decision"]}


def _append_claim(db, row: dict) -> None:
    """Insert a claim under the next claim_seq.

    claim_seq is a BIGSERIAL that every seeded claim drew from, and no claim
    insert is ever rolled back, so the sequence's next value is one past the
    highest sequence number in the table.
    """
    seq = max((c["claim_seq"] for c in db["claims"].values()), default=0) + 1
    insert(db, "claims", {
        "claim_seq": seq, "prescription_id": None, "status": None, "reason": None,
        "copay": None, "currency": None, "override_id": None, "submitted_at": None,
        **row,
    }, KEY_COLUMNS)


def submit_prescription_claim(db, args) -> dict:
    prescription_id = args["prescription_id"]
    found = _prescription_patient(db, prescription_id)
    # The plan is an inner join, so a patient without one reads as unknown.
    plan_id = found and found[1]["insurance_plan_id"]
    plan = plan_id and db["insurance_plans"].get(plan_id)
    if not plan:
        raise NotFound(f"unknown prescription {prescription_id!r}")
    rx = found[0]

    submitted_at = scenario_value(db, "scenario_time")
    override = None
    override_id = args.get("override_id")
    if override_id:
        override = first(db, "claim_overrides",
                         override_id=override_id, prescription_id=prescription_id)

    # An approved_one_time override pays exactly one claim. The published
    # runnable-env branch records consumption and never checks it, so the same
    # one-time approval can pay repeatedly; the check is enforced here.
    approved = (
        override is not None
        and override["status"] in USABLE_OVERRIDE_DECISIONS
        and not (override["status"] == "approved_one_time" and override["consumed_at"])
    )

    queue = _queue_row(db, prescription_id)
    if not approved:
        prior = _latest_claim(db, prescription_id)
        reason = (prior or {}).get("reason") or "refill_too_soon"
        _append_claim(db, {"prescription_id": prescription_id, "status": "rejected",
                           "reason": reason, "submitted_at": submitted_at})
        rx["workflow_status"] = "claim_rejected"
        rx["customer_facing_status"] = "processing"
        rx["payment_options"] = list(plan["unpaid_payment_options"])
        if queue is not None:
            queue.update(status="blocked_by_claim", position=None, estimated_minutes=None,
                         pharmacist_verification_required=None)
        return {
            "claim_status": "rejected",
            "next_workflow_status": "claim_rejected",
            "queue": {"status": "blocked_by_claim"},
        }

    # Paid: the payer settles, the queue activates, and the fill moves on to
    # pharmacist verification. Position comes from the store's counter.
    store = db["stores"][rx["fill_store_id"]]
    position = store["queue_next_position"]
    store["queue_next_position"] = position + 1
    next_status = "awaiting_pharmacist_verification"

    _append_claim(db, {"prescription_id": prescription_id, "status": "paid",
                       "copay": plan["copay"], "currency": plan["currency"],
                       "override_id": override["override_id"], "submitted_at": submitted_at})
    if override["status"] == "approved_one_time":
        override["consumed_at"] = submitted_at
    rx["workflow_status"] = next_status
    rx["payment_options"] = list(plan["paid_payment_options"])
    # Upsert: an existing queue entry keeps its priority note.
    if queue is None:
        queue = insert(db, "fill_queue", {"prescription_id": prescription_id,
                                          "priority_note": "absent"}, KEY_COLUMNS)
    queue.update(status="active", position=position,
                 estimated_minutes=store["queue_estimated_minutes"],
                 pharmacist_verification_required=True)

    return {
        "claim_status": "paid",
        # Recorded as a JSON integer, so rendered as one rather than as 15.0.
        "copay": as_int(plan["copay"]),
        "currency": plan["currency"],
        "next_workflow_status": next_status,
        "queue": {
            "status": "active",
            "position": position,
            "estimated_minutes": store["queue_estimated_minutes"],
        },
        "payment_options": as_list_always(plan["paid_payment_options"]),
    }


def update_prescription(db, args) -> dict:
    prescription_id = args["prescription_id"]
    rx = db["prescriptions"].get(prescription_id)
    if rx is None:
        raise NotFound(f"unknown prescription {prescription_id!r}")

    updated: list[str] = []
    notification_changed = False

    if args.get("priority_reason") is not None:
        rx["priority_reason"] = args["priority_reason"]
        queue = _queue_row(db, prescription_id)
        if queue is not None:
            queue["priority_note"] = "present"
        updated.append("priority_reason")

    if args.get("notification_channel") is not None:
        rx["notification_channel"] = args["notification_channel"]
        rx["ready_alert"] = "enabled"
        updated.append("notification_channel")
        notification_changed = True

    if args.get("notification_destination_id") is not None:
        destination = first(db, "notification_destinations",
                            destination_id=args["notification_destination_id"],
                            patient_id=rx["patient_id"], verified=True)
        # An unverified destination, or one belonging to another patient, is not
        # a usable ready-alert target; the update is refused rather than partly
        # applied. The runtime rolls the earlier changes back with the refusal.
        if destination is None:
            raise Refusal(
                "notification destination is not a verified destination for this patient",
                {"status": "rejected", "updated_fields": []},
            )
        rx["ready_alert_destination_id"] = destination["destination_id"]
        rx["ready_alert"] = "enabled"
        updated.append("notification_destination")
        notification_changed = True

    if not updated:
        raise Refusal(
            "no updatable field was supplied",
            {"status": "rejected", "updated_fields": []},
        )

    result: dict = {"status": "updated", "updated_fields": updated}
    queue = _queue_row(db, prescription_id) or {}

    if notification_changed:
        # An inner join on ready_alert_destination_id: no destination, no block.
        destination_id = rx["ready_alert_destination_id"]
        current = destination_id and db["notification_destinations"].get(destination_id)
        if current:
            result["notification"] = {
                "ready_alert": rx["ready_alert"],
                "destination_id": current["destination_id"],
                "masked_destination": current["masked_destination"],
                "verified": current["verified"],
            }

    # The two update kinds report different slices of the queue, because they
    # answer different questions: a notification change reports only whether a
    # priority note is attached, while a priority change reports the operational
    # state that note now sits in. Neither reports queue position, which the
    # patient is not told over the phone.
    if "priority_reason" in updated:
        queue_view = compact([
            ("status", queue.get("status")),
            ("estimated_minutes", queue.get("estimated_minutes")),
            ("pharmacist_verification_required",
             queue.get("pharmacist_verification_required")),
        ])
        if notification_changed:
            queue_view["priority_note"] = queue.get("priority_note") or "absent"
        result["queue"] = queue_view
    elif notification_changed:
        result["queue"] = {"priority_note": queue.get("priority_note") or "absent"}

    return result


def request_prescription_transfer(db, args) -> dict:
    prescription_id = args["prescription_id"]
    destination_store_id = args["destination_store_id"]

    rx = db["prescriptions"].get(prescription_id)
    if rx is None:
        raise NotFound(f"unknown prescription {prescription_id!r}")
    if destination_store_id not in db["stores"]:
        raise NotFound(f"unknown destination store {destination_store_id!r}")

    # Policy requires explicit patient authorization. Without it no request row
    # is created, and the refusal is reported as a rejected transfer.
    if not args["patient_authorized"]:
        return {
            "status": "rejected",
            "source_store_id": rx["fill_store_id"],
            "destination_store_id": destination_store_id,
            "original_fill_active": True,
        }

    transfer_id = allocate_id(db, "transfer_request")
    insert(db, "transfer_requests", {
        "transfer_id": transfer_id,
        "prescription_id": prescription_id,
        "source_store_id": rx["fill_store_id"],
        "destination_store_id": destination_store_id,
        "status": "pending_pharmacist_review",
        "reason": args.get("reason"),
        "patient_authorized": True,
        "original_fill_active": True,
        "requested_at": scenario_value(db, "scenario_time"),
    }, KEY_COLUMNS)
    # A request is not a completed transfer: the original fill stays active until
    # a pharmacist accepts.
    return {
        "status": "pending_pharmacist_review",
        "source_store_id": rx["fill_store_id"],
        "destination_store_id": destination_store_id,
        "original_fill_active": True,
    }


def transfer_to_specialist(db, args) -> dict:
    transfer_id = allocate_id(db, "specialist_transfer")
    insert(db, "specialist_transfers", {
        "transfer_id": transfer_id,
        "reason": args["reason"],
        "summary": args["summary"],
        "status": "initiated",
        "created_at": scenario_value(db, "scenario_time"),
    }, KEY_COLUMNS)
    return {"status": "initiated", "transfer_id": transfer_id}


TOOLS = {
    "lookup_patient": lookup_patient,
    "get_prescription": get_prescription,
    "request_claim_override": request_claim_override,
    "submit_prescription_claim": submit_prescription_claim,
    "get_store_inventory": get_store_inventory,
    "search_pharmacy_locations": search_pharmacy_locations,
    "request_prescription_transfer": request_prescription_transfer,
    "update_prescription": update_prescription,
    "transfer_to_specialist": transfer_to_specialist,
}

# Tools that change the pharmacy's records. The grading layer holds reads free:
# an agent may look at anything as often as it likes.
WRITE_TOOLS = {
    "request_claim_override",
    "submit_prescription_claim",
    "request_prescription_transfer",
    "update_prescription",
    "transfer_to_specialist",
}

# No read tool writes anything here (the original server's only read-side write
# was its tool_call_log, which this database does not carry).
READ_SIDE_EFFECTS: dict[str, list[str] | str] = {}
