#!/usr/bin/env python3
"""Author-time generator for this task's catalog and population SQL.

Writes two files next to itself:

  sql/002_reference.sql   medications, stores, inventory, plans, override rules,
                          the scenario clock, and the identifier allocators
  sql/003_population.sql  patients, notification destinations, prescriptions,
                          claims, and fill-queue rows for a realistic estate

The rows the recorded conversation touches are declared explicitly at the top of
each section rather than drawn from the random population, so regenerating with a
different RNG seed cannot move them. Everything else exists to make lookups
non-trivial: patients who share a surname with the target, stores that fail the
opening-hours filter, plans whose payer denies the override reason the recorded
call happened to use, and medications that are out of stock where the target's is
not.

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
SCENARIO_TIME = "2026-08-27T18:12:00-05:00"

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


# ---------------------------------------------------------------------------
# catalogs
# ---------------------------------------------------------------------------

# Scenario-critical medications first, then a catalog wide enough that a
# medication_name search has to discriminate.
SCENARIO_MEDICATIONS = [
    ("9dcd4906-4db3-4290-bccd-afde111823cb", "albuterol inhaler", "metered-dose inhaler", "90 mcg/actuation", False),
]

CATALOG_MEDICATIONS = [
    ("levothyroxine-50mcg", "levothyroxine", "tablet", "50 mcg", False),
    ("lisinopril-10mg", "lisinopril", "tablet", "10 mg", False),
    ("atorvastatin-20mg", "atorvastatin", "tablet", "20 mg", False),
    ("metformin-500mg", "metformin", "tablet", "500 mg", False),
    ("amlodipine-5mg", "amlodipine", "tablet", "5 mg", False),
    ("omeprazole-20mg", "omeprazole", "capsule", "20 mg", False),
    ("sertraline-50mg", "sertraline", "tablet", "50 mg", False),
    ("montelukast-10mg", "montelukast", "tablet", "10 mg", False),
    ("fluticasone-nasal", "fluticasone nasal spray", "nasal spray", "50 mcg/actuation", False),
    ("budesonide-formoterol", "budesonide-formoterol inhaler", "metered-dose inhaler", "160/4.5 mcg", False),
    ("ipratropium-inhaler", "ipratropium inhaler", "metered-dose inhaler", "17 mcg/actuation", False),
    ("albuterol-nebulizer", "albuterol nebulizer solution", "nebulizer solution", "2.5 mg/3 mL", False),
    ("prednisone-10mg", "prednisone", "tablet", "10 mg", False),
    ("azithromycin-250mg", "azithromycin", "tablet", "250 mg", False),
    ("amoxicillin-500mg", "amoxicillin", "capsule", "500 mg", False),
    ("gabapentin-300mg", "gabapentin", "capsule", "300 mg", False),
    ("hydrochlorothiazide-25mg", "hydrochlorothiazide", "tablet", "25 mg", False),
    ("warfarin-5mg", "warfarin", "tablet", "5 mg", False),
    ("insulin-glargine", "insulin glargine", "injection pen", "100 units/mL", False),
    ("semaglutide-injection", "semaglutide", "injection pen", "0.25 mg/dose", False),
    ("escitalopram-10mg", "escitalopram", "tablet", "10 mg", False),
    ("bupropion-150mg", "bupropion", "tablet", "150 mg", False),
    ("tamsulosin-0.4mg", "tamsulosin", "capsule", "0.4 mg", False),
    ("rosuvastatin-10mg", "rosuvastatin", "tablet", "10 mg", False),
    ("pantoprazole-40mg", "pantoprazole", "tablet", "40 mg", False),
    ("cetirizine-10mg", "cetirizine", "tablet", "10 mg", False),
    ("meloxicam-15mg", "meloxicam", "tablet", "15 mg", False),
    ("duloxetine-30mg", "duloxetine", "capsule", "30 mg", False),
    ("apixaban-5mg", "apixaban", "tablet", "5 mg", False),
    ("furosemide-40mg", "furosemide", "tablet", "40 mg", False),
    ("oxycodone-5mg", "oxycodone", "tablet", "5 mg", True),
    ("alprazolam-0.5mg", "alprazolam", "tablet", "0.5 mg", True),
    ("methylphenidate-10mg", "methylphenidate", "tablet", "10 mg", True),
    ("clonazepam-1mg", "clonazepam", "tablet", "1 mg", True),
    ("tiotropium-inhaler", "tiotropium inhaler", "dry powder inhaler", "18 mcg/capsule", False),
    ("mometasone-nasal", "mometasone nasal spray", "nasal spray", "50 mcg/actuation", False),
    ("doxycycline-100mg", "doxycycline", "capsule", "100 mg", False),
    ("ondansetron-4mg", "ondansetron", "tablet", "4 mg", False),
    ("losartan-50mg", "losartan", "tablet", "50 mg", False),
]

MEDICATIONS = SCENARIO_MEDICATIONS + CATALOG_MEDICATIONS

# (store_id, display_name, address, counter_closes_at, front_later,
#  front_closes_at, timezone, services, next_position, est_minutes,
#  proximity_rank, district)
#
# The three north-loop stores are the ones a nearby search from the fill store
# can reach. Park Avenue is the only one of them whose counter is still open
# after 19:00, which is what the recorded search relies on; Maple Grove closes
# at 18:00 and is the distractor that must be filtered out.
SCENARIO_STORES = [
    ("8e22d41d-843c-4e73-95c0-3c9877366ba9", "Oak Street Pharmacy", "1420 Oak Street", "19:00", True,
     "22:00", "America/Chicago", ["immunization", "consultation"], 1, 30, 0, "north-loop"),
    ("155059ca-b6aa-4372-b0af-86c2d681c393", "Park Avenue", None, "21:00", False,
     None, "America/Chicago", None, 1, 25, 10, "north-loop"),
    ("maple-grove", "Maple Grove", "88 Maple Grove Road", "18:00", False,
     None, "America/Chicago", ["immunization"], 1, 25, 20, "north-loop"),
]

_OTHER_STORE_SPECS = [
    ("cedar-hill", "Cedar Hill", "301 Cedar Hill Avenue", "20:00", True, "23:00", "river-bend"),
    ("lakeside-commons", "Lakeside Commons", "12 Commons Way", "19:30", False, None, "river-bend"),
    ("brookfield-plaza", "Brookfield Plaza", "755 Brookfield Plaza", "18:30", True, "21:30", "river-bend"),
    ("harborview", "Harborview", "9 Harborview Terrace", "22:00", False, None, "harbor"),
    ("dockside-market", "Dockside Market", "410 Dockside Road", "17:00", True, "20:00", "harbor"),
    ("saltmarsh-corner", "Saltmarsh Corner", "66 Saltmarsh Lane", "21:00", False, None, "harbor"),
    ("willow-crossing", "Willow Crossing", "2200 Willow Crossing", "20:30", False, None, "westgate"),
    ("stonegate-square", "Stonegate Square", "17 Stonegate Square", "19:00", True, "22:30", "westgate"),
    ("fairmount-annex", "Fairmount Annex", "540 Fairmount Street", "16:30", False, None, "westgate"),
    ("orchard-terrace", "Orchard Terrace", "121 Orchard Terrace", "21:30", True, "23:30", "eastvale"),
    ("juniper-row", "Juniper Row", "83 Juniper Row", "18:00", False, None, "eastvale"),
    ("bellmont-gate", "Bellmont Gate", "1901 Bellmont Gate", "20:00", False, None, "eastvale"),
    ("crestline-north", "Crestline North", "44 Crestline Boulevard", "19:45", True, "22:00", "crestline"),
    ("crestline-south", "Crestline South", "902 Crestline Boulevard", "17:30", False, None, "crestline"),
    ("mill-quarter", "Mill Quarter", "5 Mill Quarter", "22:30", False, None, "crestline"),
    ("granite-park", "Granite Park", "718 Granite Park Drive", "20:00", True, "21:00", "granite"),
    ("ashford-green", "Ashford Green", "260 Ashford Green", "18:15", False, None, "granite"),
    ("ridgeway-center", "Ridgeway Center", "1330 Ridgeway Center", "21:00", True, "23:00", "granite"),
    ("sunset-arcade", "Sunset Arcade", "77 Sunset Arcade", "19:15", False, None, "sunset"),
    ("palmetto-court", "Palmetto Court", "915 Palmetto Court", "16:45", False, None, "sunset"),
    ("beacon-yard", "Beacon Yard", "3 Beacon Yard", "23:00", True, "23:59", "sunset"),
    ("thornbury-lane", "Thornbury Lane", "480 Thornbury Lane", "18:45", False, None, "thornbury"),
]

PLANS = [
    # plan_id, display_name, copay, paid options, unpaid options
    ("plan-midwest-choice-ppo", "Midwest Choice PPO", "15.00",
     ["app_prepay_if_offered_when_ready", "pay_15_at_pickup"], ["pay_at_pickup"]),
    ("plan-heartland-basic-hmo", "Heartland Basic HMO", "25.00",
     ["pay_25_at_pickup"], ["pay_at_pickup"]),
    ("plan-northstar-select", "Northstar Select", "10.00",
     ["app_prepay_if_offered_when_ready", "pay_10_at_pickup"], ["pay_at_pickup"]),
    ("plan-lakeshore-value", "Lakeshore Value", "40.00",
     ["pay_40_at_pickup"], ["pay_at_pickup"]),
    ("plan-uninsured-cash", "Cash Price", "62.00",
     ["pay_62_at_pickup"], ["pay_at_pickup"]),
]

# Payer policy per plan and reason. The target plan approves a lost-medication
# override once; every other reason on that plan lands somewhere the recorded
# call would not have survived, which is the point of keeping them.
OVERRIDE_RULES = {
    "plan-midwest-choice-ppo": {
        "lost_medication": ("9e90544a-3497-4aee-a1f0-4437d4f4684c", "approved_one_time"),
        "vacation_supply": ("override-vacation-supply", "pending_patient_participation"),
        "dose_change": ("override-dose-change", "pending_patient_participation"),
        "other": ("override-other", "denied"),
    },
    "plan-heartland-basic-hmo": {
        "lost_medication": ("override-hb-lost", "pending_patient_participation"),
        "vacation_supply": ("override-hb-vacation", "denied"),
        "dose_change": ("override-hb-dose", "approved"),
        "other": ("override-hb-other", "denied"),
    },
    "plan-northstar-select": {
        "lost_medication": ("override-ns-lost", "approved"),
        "vacation_supply": ("override-ns-vacation", "approved_one_time"),
        "dose_change": ("override-ns-dose", "approved"),
        "other": ("override-ns-other", "pending_patient_participation"),
    },
    "plan-lakeshore-value": {
        "lost_medication": ("override-lv-lost", "approved_one_time"),
        "vacation_supply": ("override-lv-vacation", "pending_patient_participation"),
        "dose_change": ("override-lv-dose", "denied"),
        "other": ("override-lv-other", "denied"),
    },
    "plan-uninsured-cash": {
        "lost_medication": ("override-uc-lost", "denied"),
        "vacation_supply": ("override-uc-vacation", "denied"),
        "dose_change": ("override-uc-dose", "denied"),
        "other": ("override-uc-other", "denied"),
    },
}


def build_reference() -> str:
    rng = random.Random(RNG_SEED)
    out = [
        "-- Catalogs, scenario clock, and identifier allocators.\n"
        "-- Generated by environment/gen_seed.py; do not edit by hand.\n\n"
        "BEGIN;\n\n"
    ]

    out.append(insert("scenario", ["key", "value"], [
        ("scenario_time", SCENARIO_TIME),
        ("conversation_id", "pharmacy-travel-refill"),
        ("domain", "pharmacy"),
        ("timezone", "America/Chicago"),
    ]))

    # Allocators. Override ids come from per-plan policy rows rather than from a
    # counter, so only the two genuinely sequential entities appear here.
    out.append(insert("id_allocator",
                      ["entity_type", "scope", "next_value", "template"], [
                          ("transfer_request", "", 1, "transfer-request-{n:04d}"),
                          ("specialist_transfer", "", 1, "specialist-transfer-{n:04d}"),
                      ]))

    out.append(insert("medications",
                      ["medication_id", "display_name", "form", "strength", "controlled"],
                      MEDICATIONS))

    store_rows = []
    for (sid, name, addr, close, later, front, tz, svcs, pos, est, rank, district) in SCENARIO_STORES:
        store_rows.append((sid, name, addr, close, later, front, tz, svcs, pos, est, rank, district))
    for i, (sid, name, addr, close, later, front, district) in enumerate(_OTHER_STORE_SPECS):
        svcs = rng.choice([None, ["immunization"], ["immunization", "consultation"],
                           ["consultation"], ["immunization", "travel_clinic"]])
        store_rows.append((sid, name, addr, close, later, front, "America/Chicago",
                           svcs, 1, rng.choice([20, 25, 30, 35, 40]), 100 + i, district))
    out.append(insert("stores",
                      ["store_id", "display_name", "address", "counter_closes_at",
                       "front_store_closes_later", "front_store_closes_at", "timezone",
                       "services", "queue_next_position", "queue_estimated_minutes",
                       "proximity_rank", "district"], store_rows))

    out.append(insert("insurance_plans",
                      ["plan_id", "display_name", "copay", "currency",
                       "paid_payment_options", "unpaid_payment_options"],
                      [(p, n, c, "USD", paid, unpaid) for (p, n, c, paid, unpaid) in PLANS]))

    rule_rows = []
    for plan, reasons in OVERRIDE_RULES.items():
        for reason, (oid, decision) in reasons.items():
            rule_rows.append((plan, reason, oid, decision))
    out.append(insert("plan_override_rules",
                      ["plan_id", "reason", "override_id", "decision"], rule_rows))

    # Inventory. The three scenario rows are fixed; the rest is random but
    # weighted so most things are in stock and some are not.
    inv_rows = [
        ("8e22d41d-843c-4e73-95c0-3c9877366ba9", "9dcd4906-4db3-4290-bccd-afde111823cb", True, False, 6),
        ("155059ca-b6aa-4372-b0af-86c2d681c393", "9dcd4906-4db3-4290-bccd-afde111823cb", True, False, 4),
        ("maple-grove", "9dcd4906-4db3-4290-bccd-afde111823cb", False, False, 0),
    ]
    seen = {(r[0], r[1]) for r in inv_rows}
    all_store_ids = [r[0] for r in store_rows]
    for sid in all_store_ids:
        for (mid, *_rest) in MEDICATIONS:
            if (sid, mid) in seen:
                continue
            in_stock = rng.random() > 0.18
            inv_rows.append((sid, mid, in_stock, False,
                             rng.randint(2, 40) if in_stock else 0))
    out.append(insert("store_inventory",
                      ["store_id", "medication_id", "in_stock", "reserved", "on_hand_units"],
                      inv_rows))

    out.append("COMMIT;\n")
    return "".join(out)


# ---------------------------------------------------------------------------
# population
# ---------------------------------------------------------------------------

FIRST_NAMES = [
    "Miles", "Dana", "Priya", "Andre", "Kelsey", "Rosa", "Tomas", "Nadia", "Grant",
    "Imani", "Victor", "Leah", "Owen", "Farah", "Desmond", "Yuki", "Camila", "Errol",
    "Bianca", "Hugo", "Sana", "Marcus", "Elise", "Rahul", "Noor", "Trevor", "Anya",
    "Jonah", "Celia", "Malik", "Renata", "Kwame", "Ingrid", "Silas", "Lorna", "Petra",
    "Devon", "Aiko", "Bram", "Colette",
]

# Carter recurs deliberately: a lookup on the target's surname alone must not
# resolve to one row.
LAST_NAMES = [
    "Carter", "Carter", "Carteret", "Whitfield", "Okonkwo", "Delgado", "Novak",
    "Bergstrom", "Haddad", "Lindqvist", "Moreau", "Ferraro", "Nakamura", "Oyelaran",
    "Vasquez", "Kaur", "Brennan", "Sorensen", "Achebe", "Marchetti", "Kovacs",
    "Ilyin", "Santoro", "Abbasi", "Fontaine", "Reyes", "Thorne", "Mbeki", "Duarte",
    "Castellanos",
]

WORKFLOW_MIX = [
    ("ready_for_pickup", "ready", "paid"),
    ("awaiting_pharmacist_verification", "processing", "paid"),
    ("claim_paid", "processing", "paid"),
    ("claim_rejected", "processing", "rejected"),
    ("claim_pending", "processing", "pending"),
    ("received", "processing", "not_submitted"),
    ("picked_up", "picked_up", "paid"),
]

REJECT_REASONS = ["refill_too_soon", "not_covered",
                  "prior_authorization_required", "plan_inactive"]


def build_population() -> str:
    rng = random.Random(RNG_SEED + 1)
    out = [
        "-- Patient population, prescriptions, claims, and queue rows.\n"
        "-- Generated by environment/gen_seed.py; do not edit by hand.\n"
        "-- The scenario's own patient and prescription are in 004_scenario.sql.\n\n"
        "BEGIN;\n\n"
    ]

    store_ids = [s[0] for s in SCENARIO_STORES] + [s[0] for s in _OTHER_STORE_SPECS]
    plan_ids = [p[0] for p in PLANS]
    med_ids = [m[0] for m in MEDICATIONS]

    patients, destinations = [], []
    used_names: set[tuple[str, str]] = set()
    for i in range(104):
        first = rng.choice(FIRST_NAMES)
        last = rng.choice(LAST_NAMES)
        # 1988-06-14 belongs to the scenario patient; never reissue that pair.
        dob = dt.date(rng.randint(1942, 2008), rng.randint(1, 12), rng.randint(1, 28))
        name = f"{first} {last}"
        if (name.lower(), dob.isoformat()) in used_names or (
                name == "Miles Carter" and dob == dt.date(1988, 6, 14)):
            continue
        used_names.add((name.lower(), dob.isoformat()))
        pid = f"patient-{first.lower()}-{last.lower()}-{i:03d}"
        patients.append((pid, name, dob.isoformat(), rng.choice(store_ids),
                         rng.choice(plan_ids), []))
        for k in range(rng.choice([1, 1, 1, 2, 2, 3])):
            channel = rng.choice(["sms", "sms", "phone", "email"])
            masked = {"sms": "***-***-on-file", "phone": "***-***-on-file",
                      "email": "***@on-file"}[channel]
            destinations.append((f"{pid}-dest-{k}", pid, channel, masked,
                                 rng.random() > 0.12))

    out.append(insert("patients",
                      ["patient_id", "full_name", "date_of_birth",
                       "preferred_store_id", "insurance_plan_id", "allergies"],
                      patients))
    out.append(insert("notification_destinations",
                      ["destination_id", "patient_id", "channel",
                       "masked_destination", "verified"], destinations))

    dests_by_patient: dict[str, list[str]] = {}
    for did, pid, _c, _m, verified in destinations:
        if verified:
            dests_by_patient.setdefault(pid, []).append(did)

    plan_copay = {p[0]: p[2] for p in PLANS}
    plan_paid = {p[0]: p[3] for p in PLANS}
    plan_unpaid = {p[0]: p[4] for p in PLANS}

    rxs, claims, queues = [], [], []
    # Queue positions are handed out per store from a counter, not drawn at
    # random, so that every store's occupied positions are 1..k with no
    # duplicates and its queue_next_position lands on k+1. A store whose counter
    # claims to be about to issue position 1 while three fills already sit at
    # position 1 is the kind of inconsistency an operator would notice.
    next_position: dict[str, int] = {sid: 1 for sid in store_ids}
    # The scenario store's queue is empty: it is 18:12 and the counter closes at
    # 19:00, so the recorded fill is the next one it will take. Population fills
    # that would occupy its queue are placed elsewhere.
    SCENARIO_STORE = "8e22d41d-843c-4e73-95c0-3c9877366ba9"
    ACTIVE_WORKFLOWS = ("claim_paid", "awaiting_pharmacist_verification",
                        "ready_for_pickup")

    n = 0
    for (pid, _name, _dob, pref_store, plan, _allergies) in patients:
        for _ in range(rng.choice([1, 2, 2, 3, 3, 4, 5])):
            n += 1
            rx_id = f"prescription-{n:04d}"
            wf, cfs, claim_status = rng.choice(WORKFLOW_MIX)
            store = pref_store if rng.random() > 0.15 else rng.choice(store_ids)
            if wf in ACTIVE_WORKFLOWS and store == SCENARIO_STORE:
                store = rng.choice([s for s in store_ids if s != SCENARIO_STORE])
            received = dt.datetime(2026, 8, rng.randint(10, 27),
                                   rng.randint(8, 18), rng.choice([0, 12, 27, 42, 55]))
            dest_pool = dests_by_patient.get(pid, [])
            ready_dest = rng.choice(dest_pool) if dest_pool and rng.random() > 0.2 else None
            paid = claim_status == "paid"
            rxs.append((
                rx_id, pid, rng.choice(med_ids),
                f"Dr. {rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}",
                rng.choice(["30 tablets", "60 tablets", "90 tablets",
                            "1 inhaler (8.5 g)", "1 pen", "1 bottle (150 mL)"]),
                rng.choice([30, 30, 60, 90]), rng.randint(0, 5),
                (received - dt.timedelta(days=rng.randint(14, 40))).date().isoformat(),
                received.strftime("%Y-%m-%dT%H:%M:00-05:00"),
                rng.random() > 0.04, store, wf, cfs, None,
                plan_paid[plan] if paid else plan_unpaid[plan],
                ready_dest,
                "sms" if ready_dest else None,
                "enabled" if ready_dest else None,
            ))
            if claim_status == "not_submitted":
                claims.append((rx_id, "not_submitted", None, None, None, None, None))
            elif claim_status == "rejected":
                claims.append((rx_id, "rejected", rng.choice(REJECT_REASONS),
                               None, None, None,
                               received.strftime("%Y-%m-%dT%H:%M:00-05:00")))
            elif claim_status == "pending":
                claims.append((rx_id, "pending", None, None, None, None,
                               received.strftime("%Y-%m-%dT%H:%M:00-05:00")))
            else:
                claims.append((rx_id, "paid", None, plan_copay[plan], "USD", None,
                               received.strftime("%Y-%m-%dT%H:%M:00-05:00")))

            if wf in ("received", "claim_pending", "claim_rejected"):
                queues.append((rx_id, "blocked_by_claim", None, None, None, "absent"))
            elif wf == "picked_up":
                queues.append((rx_id, "completed", None, None, False, "absent"))
            else:
                position = next_position[store]
                next_position[store] = position + 1
                queues.append((rx_id, "active", position,
                               rng.choice([15, 20, 25, 30, 45]),
                               wf == "awaiting_pharmacist_verification", "absent"))

    out.append(insert("prescriptions",
                      ["prescription_id", "patient_id", "medication_id", "prescriber",
                       "quantity", "days_supply", "refills_remaining", "last_fill_date",
                       "received_at", "prescription_valid", "fill_store_id",
                       "workflow_status", "customer_facing_status", "priority_reason",
                       "payment_options", "ready_alert_destination_id",
                       "notification_channel", "ready_alert"], rxs))
    out.append(insert("claims",
                      ["prescription_id", "status", "reason", "copay", "currency",
                       "override_id", "submitted_at"], claims))
    out.append(insert("fill_queue",
                      ["prescription_id", "status", "position", "estimated_minutes",
                       "pharmacist_verification_required", "priority_note"], queues))

    # Each counter's next position follows from the fills already queued there.
    out.append("-- Align each store's counter with the queue it actually holds.\n")
    for sid in store_ids:
        if next_position[sid] > 1:
            out.append(f"UPDATE stores SET queue_next_position = {next_position[sid]} "
                       f"WHERE store_id = {q(sid)};\n")
    out.append("\n")

    out.append("COMMIT;\n")

    counts = {"patients": len(patients), "destinations": len(destinations),
              "prescriptions": len(rxs), "claims": len(claims), "queues": len(queues),
              "scenario_store_queue_depth": next_position[SCENARIO_STORE] - 1}
    print("population:", ", ".join(f"{k}={v}" for k, v in counts.items()))
    return "".join(out)


def main() -> None:
    os.makedirs(SQL, exist_ok=True)
    ref = build_reference()
    pop = build_population()
    with open(os.path.join(SQL, "002_reference.sql"), "w") as fh:
        fh.write(ref)
    with open(os.path.join(SQL, "003_population.sql"), "w") as fh:
        fh.write(pop)
    print(f"wrote 002_reference.sql ({len(ref)} bytes) and "
          f"003_population.sql ({len(pop)} bytes)")


if __name__ == "__main__":
    main()
