"""Pharmacy tool handlers.

Each handler has the signature `handler(cur, args) -> dict` where `cur` is a dict
cursor inside a transaction and `args` is the already schema-validated arguments
object. The returned dict is the tool result, serialized as-is.

Handlers hold domain logic only. Every identifier, amount, store hour, payer
decision, and queue estimate that appears in a result is read from the database,
never computed from wall time or generated at random, so a result is reproducible
and an operator can explain any value by pointing at a row.
"""
from __future__ import annotations

from db import NotFound, ToolRefusal, all_rows, allocate_id, one, scenario_value
from projection import as_int, as_list_always, compact

# Prescription lifecycle states that count as active for a patient-facing read.
# A list, not a tuple: psycopg2 adapts a list to a Postgres array, which is what
# `= ANY(...)` needs, and adapts a tuple to a row constructor, which it does not.
ACTIVE_WORKFLOW_STATUSES = [
    "received", "claim_pending", "claim_rejected", "claim_paid",
    "awaiting_pharmacist_verification", "ready_for_pickup",
]

# Override decisions that let a claim be paid.
USABLE_OVERRIDE_DECISIONS = ("approved", "approved_one_time")


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


def lookup_patient(cur, args) -> dict:
    matches = all_rows(
        cur,
        """
        SELECT patient_id
          FROM patients
         WHERE lower(full_name) = lower(%s)
           AND date_of_birth = %s
         ORDER BY patient_id
        """,
        (args["full_name"].strip(), args["date_of_birth"]),
    )
    if not matches:
        return {"patient_id": "", "match": "not_found"}
    if len(matches) > 1:
        return {"patient_id": "", "match": "multiple_matches"}

    patient_id = matches[0]["patient_id"]
    destinations = all_rows(
        cur,
        """
        SELECT destination_id, channel, masked_destination, verified
          FROM notification_destinations
         WHERE patient_id = %s AND verified
         ORDER BY destination_id
        """,
        (patient_id,),
    )
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


def _select_prescription(cur, patient_id: str, medication_name: str | None) -> dict:
    """Most recently received active prescription, optionally by medication.

    Matching is two-way on the medication display name, because a caller says
    "albuterol inhaler" for a record named "albuterol inhaler" but might equally
    say "albuterol" for "albuterol nebulizer solution".
    """
    params: list = [patient_id, ACTIVE_WORKFLOW_STATUSES]
    filter_sql = ""
    if medication_name:
        needle = medication_name.strip().lower()
        filter_sql = """
           AND (position(lower(m.display_name) in %s) > 0
                OR position(%s in lower(m.display_name)) > 0)
        """
        params.extend([needle, needle])

    row = one(
        cur,
        f"""
        SELECT p.prescription_id, p.medication_id, m.display_name AS medication_name,
               p.received_at, p.prescription_valid, p.workflow_status,
               p.customer_facing_status, p.payment_options,
               p.ready_alert_destination_id, p.fill_store_id
          FROM prescriptions p
          JOIN medications m ON m.medication_id = p.medication_id
         WHERE p.patient_id = %s
           AND p.workflow_status = ANY(%s)
           {filter_sql}
         ORDER BY p.received_at DESC, p.prescription_id
         LIMIT 1
        """,
        tuple(params),
    )
    if row is None:
        raise NotFound("no active prescription matches that patient and medication")
    return row


def _latest_claim(cur, prescription_id: str) -> dict | None:
    return one(
        cur,
        """
        SELECT status, reason, copay, currency, override_id
          FROM claims
         WHERE prescription_id = %s
         ORDER BY claim_seq DESC
         LIMIT 1
        """,
        (prescription_id,),
    )


def _queue_row(cur, prescription_id: str) -> dict | None:
    return one(
        cur,
        """
        SELECT status, position, estimated_minutes,
               pharmacist_verification_required, priority_note
          FROM fill_queue
         WHERE prescription_id = %s
        """,
        (prescription_id,),
    )


def get_prescription(cur, args) -> dict:
    rx = _select_prescription(cur, args["patient_id"], args.get("medication_name"))

    store = one(
        cur,
        """
        SELECT store_id, display_name, counter_closes_at, front_store_closes_later
          FROM stores
         WHERE store_id = %s
        """,
        (rx["fill_store_id"],),
    )

    claim = _latest_claim(cur, rx["prescription_id"])
    claim_view = {"status": "not_submitted"}
    if claim:
        claim_view = compact([("status", claim["status"]), ("reason", claim["reason"])])

    queue = _queue_row(cur, rx["prescription_id"]) or {"status": "blocked_by_claim"}
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


def get_store_inventory(cur, args) -> dict:
    store_id, medication_id = args["store_id"], args["medication_id"]
    if not one(cur, "SELECT 1 FROM stores WHERE store_id = %s", (store_id,)):
        raise NotFound(f"unknown store {store_id!r}")
    if not one(cur, "SELECT 1 FROM medications WHERE medication_id = %s", (medication_id,)):
        raise NotFound(f"unknown medication {medication_id!r}")

    row = one(
        cur,
        """
        SELECT in_stock, reserved
          FROM store_inventory
         WHERE store_id = %s AND medication_id = %s
        """,
        (store_id, medication_id),
    )
    # A store that carries no inventory row for a medication does not stock it.
    return {
        "store_id": store_id,
        "medication_id": medication_id,
        "in_stock": bool(row and row["in_stock"]),
        "reserved": bool(row and row["reserved"]),
    }


def search_pharmacy_locations(cur, args) -> dict:
    clauses, params = [], []

    origin_id = args.get("origin_store_id")
    if origin_id:
        origin = one(cur, "SELECT district FROM stores WHERE store_id = %s", (origin_id,))
        if origin is None:
            raise NotFound(f"unknown origin store {origin_id!r}")
        # A search anchored on a fill store covers that store's district. Without
        # this the whole chain would surface as "nearby".
        clauses.append("district = %s")
        params.append(origin["district"])
        clauses.append("store_id <> %s")
        params.append(origin_id)

    open_after = args.get("open_after_local_time")
    if open_after:
        clauses.append("counter_closes_at > %s")
        params.append(open_after)

    query = args.get("query")
    if query:
        clauses.append("(display_name ILIKE %s OR coalesce(address, '') ILIKE %s)")
        params.extend([f"%{query}%", f"%{query}%"])

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = all_rows(
        cur,
        f"""
        SELECT store_id, display_name, address, counter_closes_at,
               front_store_closes_at, timezone, services
          FROM stores
        {where}
         ORDER BY proximity_rank, store_id
        """,
        tuple(params),
    )

    return {
        "locations": [
            compact([
                ("store_id", r["store_id"]),
                ("display_name", r["display_name"]),
                ("address", r["address"]),
                ("counter_closes_at", r["counter_closes_at"]),
                ("front_store_closes_at", r["front_store_closes_at"]),
                ("timezone", r["timezone"]),
                ("services", r["services"]),
            ])
            for r in rows
        ]
    }


# ---------------------------------------------------------------------------
# mutations
# ---------------------------------------------------------------------------


def request_claim_override(cur, args) -> dict:
    prescription_id, reason = args["prescription_id"], args["reason"]
    rx = one(
        cur,
        """
        SELECT p.prescription_id, pt.insurance_plan_id
          FROM prescriptions p
          JOIN patients pt ON pt.patient_id = p.patient_id
         WHERE p.prescription_id = %s
        """,
        (prescription_id,),
    )
    if rx is None:
        raise NotFound(f"unknown prescription {prescription_id!r}")

    rule = one(
        cur,
        """
        SELECT override_id, decision
          FROM plan_override_rules
         WHERE plan_id = %s AND reason = %s
        """,
        (rx["insurance_plan_id"], reason),
    )
    if rule is None:
        raise ToolRefusal(
            f"payer plan {rx['insurance_plan_id']!r} has no policy for reason {reason!r}")

    requested_at = scenario_value(cur, "scenario_time")
    cur.execute(
        """
        INSERT INTO claim_overrides
            (override_id, prescription_id, reason, status, urgency_context, requested_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (override_id) DO UPDATE
            SET urgency_context = EXCLUDED.urgency_context,
                requested_at = EXCLUDED.requested_at
        """,
        (rule["override_id"], prescription_id, reason, rule["decision"],
         args.get("urgency_context"), requested_at),
    )
    return {"override_id": rule["override_id"], "status": rule["decision"]}


def submit_prescription_claim(cur, args) -> dict:
    prescription_id = args["prescription_id"]
    rx = one(
        cur,
        """
        SELECT p.prescription_id, p.fill_store_id, pl.plan_id, pl.copay, pl.currency,
               pl.paid_payment_options, pl.unpaid_payment_options
          FROM prescriptions p
          JOIN patients pt ON pt.patient_id = p.patient_id
          JOIN insurance_plans pl ON pl.plan_id = pt.insurance_plan_id
         WHERE p.prescription_id = %s
        """,
        (prescription_id,),
    )
    if rx is None:
        raise NotFound(f"unknown prescription {prescription_id!r}")

    submitted_at = scenario_value(cur, "scenario_time")
    override = None
    override_id = args.get("override_id")
    if override_id:
        override = one(
            cur,
            """
            SELECT override_id, status, consumed_at
              FROM claim_overrides
             WHERE override_id = %s AND prescription_id = %s
            """,
            (override_id, prescription_id),
        )

    # An approved_one_time override pays exactly one claim. The published
    # runnable-env branch records consumption and never checks it, so the same
    # one-time approval can pay repeatedly; the check is enforced here.
    approved = (
        override is not None
        and override["status"] in USABLE_OVERRIDE_DECISIONS
        and not (override["status"] == "approved_one_time" and override["consumed_at"])
    )

    if not approved:
        prior = _latest_claim(cur, prescription_id)
        reason = (prior or {}).get("reason") or "refill_too_soon"
        cur.execute(
            """
            INSERT INTO claims (prescription_id, status, reason, submitted_at)
            VALUES (%s, 'rejected', %s, %s)
            """,
            (prescription_id, reason, submitted_at),
        )
        cur.execute(
            """
            UPDATE prescriptions
               SET workflow_status = 'claim_rejected',
                   customer_facing_status = 'processing',
                   payment_options = %s
             WHERE prescription_id = %s
            """,
            (rx["unpaid_payment_options"], prescription_id),
        )
        cur.execute(
            """
            UPDATE fill_queue
               SET status = 'blocked_by_claim', position = NULL,
                   estimated_minutes = NULL, pharmacist_verification_required = NULL
             WHERE prescription_id = %s
            """,
            (prescription_id,),
        )
        return {
            "claim_status": "rejected",
            "next_workflow_status": "claim_rejected",
            "queue": {"status": "blocked_by_claim"},
        }

    # Paid: the payer settles, the queue activates, and the fill moves on to
    # pharmacist verification. Position comes from the store's counter.
    store = one(
        cur,
        """
        UPDATE stores
           SET queue_next_position = queue_next_position + 1
         WHERE store_id = %s
        RETURNING queue_next_position - 1 AS position, queue_estimated_minutes
        """,
        (rx["fill_store_id"],),
    )
    next_status = "awaiting_pharmacist_verification"

    cur.execute(
        """
        INSERT INTO claims
            (prescription_id, status, copay, currency, override_id, submitted_at)
        VALUES (%s, 'paid', %s, %s, %s, %s)
        """,
        (prescription_id, rx["copay"], rx["currency"], override["override_id"],
         submitted_at),
    )
    if override["status"] == "approved_one_time":
        cur.execute(
            "UPDATE claim_overrides SET consumed_at = %s WHERE override_id = %s",
            (submitted_at, override["override_id"]),
        )
    cur.execute(
        """
        UPDATE prescriptions
           SET workflow_status = %s, payment_options = %s
         WHERE prescription_id = %s
        """,
        (next_status, rx["paid_payment_options"], prescription_id),
    )
    cur.execute(
        """
        INSERT INTO fill_queue
            (prescription_id, status, position, estimated_minutes,
             pharmacist_verification_required, priority_note)
        VALUES (%s, 'active', %s, %s, TRUE, 'absent')
        ON CONFLICT (prescription_id) DO UPDATE
            SET status = 'active', position = EXCLUDED.position,
                estimated_minutes = EXCLUDED.estimated_minutes,
                pharmacist_verification_required = TRUE
        """,
        (prescription_id, store["position"], store["queue_estimated_minutes"]),
    )

    return {
        "claim_status": "paid",
        # Recorded as a JSON integer, so rendered as one rather than as 15.0.
        "copay": as_int(rx["copay"]),
        "currency": rx["currency"],
        "next_workflow_status": next_status,
        "queue": {
            "status": "active",
            "position": store["position"],
            "estimated_minutes": store["queue_estimated_minutes"],
        },
        "payment_options": as_list_always(rx["paid_payment_options"]),
    }


def update_prescription(cur, args) -> dict:
    prescription_id = args["prescription_id"]
    rx = one(
        cur,
        "SELECT prescription_id, patient_id FROM prescriptions WHERE prescription_id = %s",
        (prescription_id,),
    )
    if rx is None:
        raise NotFound(f"unknown prescription {prescription_id!r}")

    updated: list[str] = []
    notification_changed = False

    if args.get("priority_reason") is not None:
        cur.execute(
            "UPDATE prescriptions SET priority_reason = %s WHERE prescription_id = %s",
            (args["priority_reason"], prescription_id),
        )
        cur.execute(
            "UPDATE fill_queue SET priority_note = 'present' WHERE prescription_id = %s",
            (prescription_id,),
        )
        updated.append("priority_reason")

    if args.get("notification_channel") is not None:
        cur.execute(
            """
            UPDATE prescriptions
               SET notification_channel = %s, ready_alert = 'enabled'
             WHERE prescription_id = %s
            """,
            (args["notification_channel"], prescription_id),
        )
        updated.append("notification_channel")
        notification_changed = True

    if args.get("notification_destination_id") is not None:
        destination = one(
            cur,
            """
            SELECT destination_id, masked_destination, verified
              FROM notification_destinations
             WHERE destination_id = %s AND patient_id = %s AND verified
            """,
            (args["notification_destination_id"], rx["patient_id"]),
        )
        # An unverified destination, or one belonging to another patient, is not
        # a usable ready-alert target; the update is refused rather than partly
        # applied. The transaction rolls back with the refusal.
        if destination is None:
            raise ToolRefusal(
                "notification destination is not a verified destination for this patient",
                {"status": "rejected", "updated_fields": []},
            )
        cur.execute(
            """
            UPDATE prescriptions
               SET ready_alert_destination_id = %s, ready_alert = 'enabled'
             WHERE prescription_id = %s
            """,
            (destination["destination_id"], prescription_id),
        )
        updated.append("notification_destination")
        notification_changed = True

    if not updated:
        raise ToolRefusal(
            "no updatable field was supplied",
            {"status": "rejected", "updated_fields": []},
        )

    result: dict = {"status": "updated", "updated_fields": updated}
    queue = _queue_row(cur, prescription_id) or {}

    if notification_changed:
        current = one(
            cur,
            """
            SELECT p.ready_alert, d.destination_id, d.masked_destination, d.verified
              FROM prescriptions p
              JOIN notification_destinations d
                ON d.destination_id = p.ready_alert_destination_id
             WHERE p.prescription_id = %s
            """,
            (prescription_id,),
        )
        if current:
            result["notification"] = {
                "ready_alert": current["ready_alert"],
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


def request_prescription_transfer(cur, args) -> dict:
    prescription_id = args["prescription_id"]
    destination_store_id = args["destination_store_id"]

    rx = one(
        cur,
        "SELECT prescription_id, fill_store_id FROM prescriptions WHERE prescription_id = %s",
        (prescription_id,),
    )
    if rx is None:
        raise NotFound(f"unknown prescription {prescription_id!r}")
    if not one(cur, "SELECT 1 FROM stores WHERE store_id = %s", (destination_store_id,)):
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

    transfer_id = allocate_id(cur, "transfer_request")
    cur.execute(
        """
        INSERT INTO transfer_requests
            (transfer_id, prescription_id, source_store_id, destination_store_id,
             status, reason, patient_authorized, original_fill_active, requested_at)
        VALUES (%s, %s, %s, %s, 'pending_pharmacist_review', %s, TRUE, TRUE, %s)
        """,
        (transfer_id, prescription_id, rx["fill_store_id"], destination_store_id,
         args.get("reason"), scenario_value(cur, "scenario_time")),
    )
    # A request is not a completed transfer: the original fill stays active until
    # a pharmacist accepts.
    return {
        "status": "pending_pharmacist_review",
        "source_store_id": rx["fill_store_id"],
        "destination_store_id": destination_store_id,
        "original_fill_active": True,
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

# Tools that change the pharmacy's records. The grading layer holds reads free —
# an agent may look at anything as often as it likes — so the distinction has to
# be stated somewhere, and the handlers are where it is known.
WRITE_TOOLS = {
    "request_claim_override",
    "submit_prescription_claim",
    "request_prescription_transfer",
    "update_prescription",
    "transfer_to_specialist",
}
