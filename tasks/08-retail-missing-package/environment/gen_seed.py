#!/usr/bin/env python3
"""Author-time generator for this task's catalog and population SQL.

Writes two files next to itself:

  sql/002_reference.sql   the scenario clock, identifier allocators, desk policy
                          for each case type, notification templates,
                          distribution centres, and the product catalog
  sql/003_population.sql  customers, orders, items, tenders, returns, refunds,
                          carrier scans, and unrelated open cases

The rows the recorded conversation touches are not generated here. They are
declared explicitly in sql/004_scenario.sql so that regenerating with a
different RNG seed cannot move them, and so a reviewer can read the recorded
world in one file.

Everything this script emits exists to make a lookup non-trivial. The catalog
carries variants that are out of stock and variants whose availability the
catalog simply does not know; the population carries customers who share the
caller's surname, orders whose references collide with the scenario order on
three trailing digits, cases that are already closed, and one pair of orders on
different accounts that share all four trailing digits, so an order reference
resolved without a customer scope has something to be ambiguous about.

This script never enters the container image; see environment/.dockerignore.

Usage:  python3 gen_seed.py
"""
from __future__ import annotations

import datetime as dt
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))
SQL = os.path.join(HERE, "sql")

RNG_SEED = 20260825
SCENARIO_TIME = "2026-08-25T15:40:00-04:00"
SCENARIO_DATE = dt.date(2026, 8, 25)

# The scenario order, the near-miss references declared alongside it, the
# references the replacement allocator will issue, and the deliberately
# ambiguous pair below. Nothing generated at random may share four trailing
# digits with any of them, or a recorded call would resolve to the wrong order.
RESERVED_SUFFIXES = {"7319", "8821", "8822", "8823", "5501",
                     "1319", "9319", "7019", "7329"}


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
# reference data
# ---------------------------------------------------------------------------

DISTRIBUTION_CENTERS = [
    ("edison", "Edison", "nj-metro"),
    ("secaucus", "Secaucus", "nj-metro"),
    ("mount-vernon", "Mount Vernon", "ny-metro"),
    ("bristol", "Bristol", "new-england"),
    ("aurora", "Aurora", "midwest"),
    ("mesquite", "Mesquite", "southwest"),
    ("tualatin", "Tualatin", "northwest"),
    ("lithia-springs", "Lithia Springs", "southeast"),
]

# (product_reference, display_name, category, hazard_class, disposal, safety)
#
# The scenario product carries no display name: the recorded results for this
# conversation disclose the headphones only by reference and colour, and a name
# the recording never revealed would be invented rather than observed.
SCENARIO_PRODUCTS = [
    ("blue-noise-canceling-headphones", None, "electronics", "electrical",
     "discard_or_recycle", "do_not_power_on"),
]

CATALOG_PRODUCTS = [
    ("12-cup-coffee-maker", "12-cup coffee maker", "small_appliance", "electrical",
     "discard_or_recycle", "do_not_power_on"),
    ("standing-desk-converter", "standing desk converter", "furniture", "none",
     "discard_or_recycle", None),
    ("over-ear-studio-headphones", "over-ear studio headphones", "electronics",
     "electrical", "discard_or_recycle", "do_not_power_on"),
    ("wireless-earbuds", "wireless earbuds", "electronics", "electrical",
     "discard_or_recycle", "do_not_power_on"),
    ("electric-kettle", "electric kettle", "small_appliance", "electrical",
     "discard_or_recycle", "do_not_power_on"),
    ("espresso-machine", "espresso machine", "small_appliance", "electrical",
     "discard_or_recycle", "do_not_power_on"),
    ("blender", "countertop blender", "small_appliance", "electrical",
     "discard_or_recycle", "do_not_power_on"),
    ("air-fryer", "air fryer", "small_appliance", "electrical",
     "discard_or_recycle", "do_not_power_on"),
    ("stand-mixer", "stand mixer", "small_appliance", "electrical",
     "discard_or_recycle", "do_not_power_on"),
    ("toaster-oven", "toaster oven", "small_appliance", "electrical",
     "discard_or_recycle", "do_not_power_on"),
    ("desk-lamp", "desk lamp", "lighting", "electrical",
     "discard_or_recycle", "do_not_power_on"),
    ("floor-lamp", "floor lamp", "lighting", "electrical",
     "discard_or_recycle", "do_not_power_on"),
    ("monitor-arm", "monitor arm", "office", "none", "discard_or_recycle", None),
    ("mesh-task-chair", "mesh task chair", "furniture", "none",
     "discard_or_recycle", None),
    ("laptop-stand", "laptop stand", "office", "none", "discard_or_recycle", None),
    ("mechanical-keyboard", "mechanical keyboard", "electronics", "electrical",
     "discard_or_recycle", "do_not_power_on"),
    ("wireless-mouse", "wireless mouse", "electronics", "electrical",
     "discard_or_recycle", "do_not_power_on"),
    ("usb-c-hub", "USB-C hub", "electronics", "electrical",
     "discard_or_recycle", "do_not_power_on"),
    ("portable-ssd", "portable SSD", "electronics", "electrical",
     "discard_or_recycle", "do_not_power_on"),
    ("bluetooth-speaker", "bluetooth speaker", "electronics", "electrical",
     "discard_or_recycle", "do_not_power_on"),
    ("smart-thermostat", "smart thermostat", "smart_home", "electrical",
     "discard_or_recycle", "do_not_power_on"),
    ("video-doorbell", "video doorbell", "smart_home", "electrical",
     "discard_or_recycle", "do_not_power_on"),
    ("robot-vacuum", "robot vacuum", "small_appliance", "electrical",
     "discard_or_recycle", "do_not_power_on"),
    ("cordless-stick-vacuum", "cordless stick vacuum", "small_appliance",
     "electrical", "discard_or_recycle", "do_not_power_on"),
    ("air-purifier", "air purifier", "small_appliance", "electrical",
     "discard_or_recycle", "do_not_power_on"),
    ("humidifier", "humidifier", "small_appliance", "electrical",
     "discard_or_recycle", "do_not_power_on"),
    ("space-heater", "space heater", "small_appliance", "electrical",
     "discard_or_recycle", "do_not_power_on"),
    ("tower-fan", "tower fan", "small_appliance", "electrical",
     "discard_or_recycle", "do_not_power_on"),
    ("weighted-blanket", "weighted blanket", "bedding", "none",
     "discard_or_recycle", None),
    ("duvet-cover-set", "duvet cover set", "bedding", "none",
     "discard_or_recycle", None),
    ("memory-foam-pillow", "memory foam pillow", "bedding", "none",
     "discard_or_recycle", None),
    ("bath-towel-set", "bath towel set", "bath", "none", "discard_or_recycle", None),
    ("ceramic-dinnerware-set", "ceramic dinnerware set", "kitchen", "glass",
     "discard_or_recycle", "handle_with_care"),
    ("drinking-glass-set", "drinking glass set", "kitchen", "glass",
     "discard_or_recycle", "handle_with_care"),
    ("cast-iron-skillet", "cast iron skillet", "kitchen", "none",
     "discard_or_recycle", None),
    ("nonstick-cookware-set", "nonstick cookware set", "kitchen", "none",
     "discard_or_recycle", None),
    ("chef-knife", "chef knife", "kitchen", "none", "discard_or_recycle", None),
    ("cutting-board", "cutting board", "kitchen", "none", "discard_or_recycle", None),
    ("food-storage-set", "food storage set", "kitchen", "none",
     "discard_or_recycle", None),
    ("insulated-water-bottle", "insulated water bottle", "outdoor", "none",
     "discard_or_recycle", None),
    ("camping-tent", "camping tent", "outdoor", "none", "discard_or_recycle", None),
    ("sleeping-bag", "sleeping bag", "outdoor", "none", "discard_or_recycle", None),
    ("hiking-backpack", "hiking backpack", "outdoor", "none",
     "discard_or_recycle", None),
    ("trail-running-shoes", "trail running shoes", "apparel", "none",
     "discard_or_recycle", None),
    ("rain-jacket", "rain jacket", "apparel", "none", "discard_or_recycle", None),
    ("merino-base-layer", "merino base layer", "apparel", "none",
     "discard_or_recycle", None),
    ("wool-socks", "wool socks", "apparel", "none", "discard_or_recycle", None),
    ("yoga-mat", "yoga mat", "fitness", "none", "discard_or_recycle", None),
    ("adjustable-dumbbells", "adjustable dumbbells", "fitness", "none",
     "discard_or_recycle", None),
    ("resistance-band-set", "resistance band set", "fitness", "none",
     "discard_or_recycle", None),
    ("foam-roller", "foam roller", "fitness", "none", "discard_or_recycle", None),
    ("fitness-tracker", "fitness tracker", "electronics", "electrical",
     "discard_or_recycle", "do_not_power_on"),
    ("smart-scale", "smart scale", "electronics", "electrical",
     "discard_or_recycle", "do_not_power_on"),
    ("electric-toothbrush", "electric toothbrush", "personal_care", "electrical",
     "discard_or_recycle", "do_not_power_on"),
    ("hair-dryer", "hair dryer", "personal_care", "electrical",
     "discard_or_recycle", "do_not_power_on"),
    ("beard-trimmer", "beard trimmer", "personal_care", "electrical",
     "discard_or_recycle", "do_not_power_on"),
    ("bookshelf", "bookshelf", "furniture", "none", "discard_or_recycle", None),
    ("coffee-table", "coffee table", "furniture", "none", "discard_or_recycle", None),
    ("nightstand", "nightstand", "furniture", "none", "discard_or_recycle", None),
    ("area-rug", "area rug", "home_decor", "none", "discard_or_recycle", None),
    ("wall-mirror", "wall mirror", "home_decor", "glass",
     "discard_or_recycle", "handle_with_care"),
    ("picture-frame-set", "picture frame set", "home_decor", "glass",
     "discard_or_recycle", "handle_with_care"),
    ("scented-candle-set", "scented candle set", "home_decor", "none",
     "discard_or_recycle", None),
    ("planter-set", "planter set", "home_decor", "none", "discard_or_recycle", None),
    ("storage-bins", "storage bins", "storage", "none", "discard_or_recycle", None),
    ("garment-rack", "garment rack", "storage", "none", "discard_or_recycle", None),
    ("shoe-cabinet", "shoe cabinet", "storage", "none", "discard_or_recycle", None),
    ("board-game", "board game", "toys", "none", "discard_or_recycle", None),
    ("building-block-set", "building block set", "toys", "none",
     "discard_or_recycle", None),
    ("plush-toy", "plush toy", "toys", "none", "discard_or_recycle", None),
    ("puzzle-1000-piece", "1000-piece puzzle", "toys", "none",
     "discard_or_recycle", None),
    ("dog-bed", "dog bed", "pet", "none", "discard_or_recycle", None),
    ("cat-tree", "cat tree", "pet", "none", "discard_or_recycle", None),
    ("pet-water-fountain", "pet water fountain", "pet", "electrical",
     "discard_or_recycle", "do_not_power_on"),
    ("travel-duffel", "travel duffel", "luggage", "none", "discard_or_recycle", None),
    ("carry-on-suitcase", "carry-on suitcase", "luggage", "none",
     "discard_or_recycle", None),
    ("packing-cube-set", "packing cube set", "luggage", "none",
     "discard_or_recycle", None),
    ("desk-organizer", "desk organizer", "office", "none",
     "discard_or_recycle", None),
    ("notebook-set", "notebook set", "office", "none", "discard_or_recycle", None),
    ("label-maker", "label maker", "office", "electrical",
     "discard_or_recycle", "do_not_power_on"),
]

PRODUCTS = SCENARIO_PRODUCTS + CATALOG_PRODUCTS

COLORS = ["black", "matte black", "white", "graphite", "navy", "blue", "sand",
          "olive", "burgundy", "silver"]

# (template, message_type, subject_prefix, included_fields, initial_status,
#  delivery_progression, optional_photo_link, photo_link_section)
#
# A case notification is handed to the mail provider synchronously and is
# already sent when the tool returns; a replacement confirmation is built by the
# order pipeline and is still queued at creation time. Both then progress
# through the same receipts, which is why a later read reports a later state.
NOTIFICATION_TEMPLATES = [
    ("delivery_trace_confirmation", "delivery_trace_confirmation",
     "Your Westline delivery trace",
     ["case_number", "status", "carrier_response_deadline", "approval_link"],
     "sent", ["sent", "delivered"], None, None,
     ["notification_id", "type", "status"]),
    ("refund_trace_confirmation", "refund_trace_confirmation",
     "Your Westline refund trace",
     ["amount", "masked_original_payment_reference", "review_window", "case_number"],
     "sent", ["sent", "delivered"], None, None,
     ["notification_id", "type", "status"]),
    ("case_reference", "case_reference", "Your Westline case",
     ["case_number", "status"], "sent", ["sent", "delivered"], None, None,
     ["notification_id", "type", "status"]),
    ("replacement_confirmation", "replacement_confirmation",
     "Your Westline replacement",
     ["replacement_order_reference", "balance_due", "estimated_delivery",
      "return_disposition"],
     "queued", ["queued", "sent", "delivered"], True, "later_in_email",
     ["type", "status", "subject_prefix", "optional_photo_link",
      "photo_link_section"]),
]

CASE_TYPE_POLICY = [
    # Delivery traces give the carrier station until 18:00 the following day and
    # establish eligibility only on a trigger, never on elapsed time alone.
    ("delivery_trace", "open", 1, "18:00", None, None, None, True,
     "trace_notification",
     "review requested resolution and fulfillment after an eligibility trigger",
     ["carrier_confirms_missing", "carrier_response_deadline_expires"],
     True, False,
     "Check {site} pickup availability first after replacement eligibility.",
     # This conversation reads its own trace to confirm what the customer is
     # looking at: whether it is still open, when the carrier must answer,
     # whether the carrier may approach him directly, and whether anything has
     # shipped against it yet.
     ["case_number", "status", "deadline", "carrier_may_contact_customer",
      "replacement_created"],
     ["case_number", "order_reference", "item", "status", "preferences"]),
    # Refund traces run to a business-day review window and block a second
    # refund on the same tender for as long as they are open.
    ("refund_trace", "open", None, None, 3, 5, True, None, None,
     "await the payment team's settlement review", None, None, False, None,
     ["case_number", "type", "status", "deadline"],
     ["case_number", "order_reference", "item", "status"]),
]

# Notes whose subject is a bank fee. Policy forbids approving one while a trace
# is open, so a note that raises one makes the update result restate the case's
# standing decision instead of leaving the claim unanswered.
# Label endings the desk strips to get from what a customer says to the site a
# reviewer instruction is phrased around.
PICKUP_SITE_SUFFIXES = [
    " pickup counter", " pickup point", " pickup location", " service desk",
    " counter", " store", " branch",
]

NOTE_TOPICS = [
    ("bank_fee", "%overdraft fee%", True),
    ("bank_fee", "%bank fee%", True),
    ("bank_fee", "%interest charge%", True),
    ("bank_fee", "%foreign-transaction fee%", True),
    ("card_replacement", "%card%replaced%", False),
]


def build_reference() -> str:
    rng = random.Random(RNG_SEED)
    out = [
        "-- Scenario clock, identifier allocators, desk policy, notification\n"
        "-- templates, distribution centres, and the product catalog.\n"
        "-- Generated by environment/gen_seed.py; do not edit by hand.\n\n"
        "BEGIN;\n\n"
    ]

    out.append(insert("scenario", ["key", "value"], [
        ("scenario_time", SCENARIO_TIME),
        ("conversation_id", "retail-missing-package"),
        ("domain", "retail"),
        ("timezone", "America/New_York"),
        # This conversation's recorded results render a redacted order
        # reference as the bare last four digits. The sibling retail
        # conversations render the same redaction as "ending-NNNN"; the format
        # is a setting rather than a literal in the handler so the two can
        # differ without the code differing.
        ("order_reference_mask", "{last4}"),
        # A partial order reference must pin down at least this many trailing
        # digits before the desk will resolve it.
        ("min_reference_suffix_digits", "4"),
        # The identity block an order read carries alongside the item manifest.
        ("order_customer_fields", "customer_id,display_name,verified_email"),
    ]))

    # Allocators. The support-case allocator is seeded so that the first trace
    # opened in this conversation issues the recorded case id and a second one
    # issues WST481663 rather than repeating it.
    out.append(insert("id_allocator",
                      ["entity_type", "scope", "next_value", "template"], [
                          ("support_case", "", 481662, "WST{n}"),
                          ("order", "replacement", 5820458821, "{n}"),
                          ("specialist_transfer", "", 1, "transfer-{n:04d}"),
                      ]))

    out.append(insert("case_type_policy",
                      ["case_type", "initial_status", "deadline_offset_days",
                       "deadline_local_time", "review_window_min_days",
                       "review_window_max_days", "duplicate_refund_blocked",
                       "approval_required", "approval_channel", "next_action",
                       "eligibility_triggers", "carrier_may_contact_customer",
                       "pickup_guaranteed", "preference_instruction_template",
                       "order_view_fields", "related_view_fields"],
                      CASE_TYPE_POLICY))

    out.append(insert("note_topics",
                      ["topic", "match_pattern", "discloses_fee_decision"],
                      NOTE_TOPICS))

    out.append(insert("pickup_site_suffixes", ["suffix"],
                      [(s,) for s in PICKUP_SITE_SUFFIXES]))

    out.append(insert("notification_templates",
                      ["template", "message_type", "subject_prefix",
                       "included_fields", "initial_status", "delivery_progression",
                       "optional_photo_link", "photo_link_section",
                       "order_view_fields"],
                      NOTIFICATION_TEMPLATES))

    out.append(insert("distribution_centers", ["dc_id", "display_name", "region"],
                      DISTRIBUTION_CENTERS))

    out.append(insert("products",
                      ["product_reference", "display_name", "category",
                       "hazard_class", "disposal_disposition", "safety_instruction"],
                      PRODUCTS))

    # Variants. The scenario variant is fixed: the catalog knows that the exact
    # blue model is available but does not answer the coarser product-level
    # question, and the recorded result reflects exactly that. The four that
    # follow it are the variants 004_scenario.sql attaches to the caller's other
    # orders, pinned here so a change of RNG seed cannot delete them.
    variants = [
        ("blue-noise-canceling-headphones", "blue-noise-canceling-headphones",
         None, "blue", None, True, "214.99"),
        ("desk-lamp-graphite", "desk-lamp", "desk lamp", "graphite",
         True, True, "48.30"),
        ("wool-socks-olive", "wool-socks", "wool socks", "olive",
         True, True, "24.75"),
        ("cutting-board-sand", "cutting-board", "cutting board", "sand",
         True, True, "39.10"),
        ("yoga-mat-navy", "yoga-mat", "yoga mat", "navy", True, True, "61.00"),
    ]
    taken = {v[0] for v in variants}
    for (pref, name, _cat, _hz, _disp, _safe) in CATALOG_PRODUCTS:
        for color in rng.sample(COLORS, rng.choice([1, 1, 2])):
            slug = color.replace(" ", "-")
            reference = f"{pref}-{slug}"
            if reference in taken:
                continue
            taken.add(reference)
            in_stock = rng.random() > 0.22
            variants.append((
                reference, pref, name, color, in_stock,
                in_stock and rng.random() > 0.25,
                f"{rng.randrange(1200, 34000) / 100:.2f}",
            ))
    out.append(insert("product_variants",
                      ["variant_reference", "product_reference", "display_name",
                       "color", "in_stock", "same_variant_in_stock",
                       "current_price"], variants))

    out.append("COMMIT;\n")
    print(f"reference: products={len(PRODUCTS)}, variants={len(variants)}")
    return "".join(out), variants


# ---------------------------------------------------------------------------
# population
# ---------------------------------------------------------------------------

FIRST_NAMES = [
    "Ethan", "Marisol", "Devon", "Priya", "Callum", "Nadia", "Omar", "Junie",
    "Beatriz", "Kwame", "Sirisha", "Tobias", "Lena", "Rafael", "Ingrid", "Hugo",
    "Amara", "Felix", "Rosalind", "Nikhil", "Cora", "Emeka", "Sana", "Teodoro",
    "Wren", "Anders", "Yusuf", "Delphine", "Mateo", "Harriet", "Zoya", "Lucas",
    "Adaeze", "Bram", "Ines", "Sung-min", "Vera", "Odalys", "Kiran", "Noor",
]

# Patel recurs deliberately: an agent who tries to resolve the caller by
# surname rather than by the verified email on the order finds several people.
LAST_NAMES = [
    "Patel", "Patel", "Patell", "Torrez", "Okafor", "Lindqvist", "Brennan",
    "Nakashima", "Ferraro", "Duarte", "Abbasi", "Whitfield", "Mbeki", "Kovacs",
    "Santoro", "Reyes", "Thorne", "Novak", "Haddad", "Marchetti", "Delgado",
    "Bergstrom", "Achebe", "Sorensen", "Fontaine", "Castellanos", "Ilyin",
    "Oyelaran", "Vasquez", "Kaur",
]

EMAIL_DOMAINS = ["northmail.com", "harbormail.com", "brightpost.net",
                 "quillmail.com", "riverpost.org"]

REGIONS = [d[2] for d in DISTRIBUTION_CENTERS]

FULFILLMENT_MIX = [
    ("delivered", 0.55), ("out_for_delivery", 0.08), ("shipped", 0.14),
    ("fulfilled", 0.08), ("processing", 0.10), ("placed", 0.05),
]

SCAN_LOCATIONS = ["front entrance", "front desk", "mailroom", "package room",
                  "lobby", "side entrance", "left with attendant",
                  "parcel locker", "reception"]

CLOSED_CASE_STATUSES = ["closed", "resolved", "closed", "resolved", "closed"]
OPEN_CASE_STATUSES = ["open", "awaiting_carrier_response",
                      "pending_customer_or_external_response",
                      "reviewing_merchant_and_tender_records"]


def weighted(rng: random.Random, mix: list[tuple[str, float]]) -> str:
    roll = rng.random()
    cumulative = 0.0
    for value, weight in mix:
        cumulative += weight
        if roll <= cumulative:
            return value
    return mix[-1][0]


def build_population(variants: list[tuple]) -> str:
    rng = random.Random(RNG_SEED + 1)
    out = [
        "-- Customers, orders, tenders, returns, refunds, carrier scans, and\n"
        "-- unrelated support cases.\n"
        "-- Generated by environment/gen_seed.py; do not edit by hand.\n"
        "-- The caller, his order, and his product are in 004_scenario.sql.\n\n"
        "BEGIN;\n\n"
    ]

    variant_by_ref = {v[0]: v for v in variants}
    catalog_variants = [v for v in variants
                        if v[0] != "blue-noise-canceling-headphones"]

    customers = []
    used_emails: set[str] = set()
    for i in range(100):
        first = rng.choice(FIRST_NAMES)
        last = rng.choice(LAST_NAMES)
        local = f"{first.lower()}.{last.lower()}{i:02d}"
        email = f"{local}@{rng.choice(EMAIL_DOMAINS)}"
        if email in used_emails or email == "ethan.patel@northmail.com":
            continue
        used_emails.add(email)
        customers.append((
            f"customer-{first.lower()}-{last.lower()}-{i:03d}",
            f"{first} {last}", email,
            f"{email[0]}***@{email.split('@')[1]}",
            f"***-***-{rng.randrange(1000, 9999)}",
            rng.choice(REGIONS), "home_address_on_order",
        ))

    out.append(insert("customers",
                      ["customer_id", "display_name", "email", "masked_email",
                       "masked_phone", "fulfillment_region", "address_label"],
                      customers))

    # Order references. Four trailing digits are what a caller reads out, so the
    # generator refuses any that would collide with the scenario order or with a
    # reference the replacement allocator will issue.
    used_suffix_counts: dict[str, int] = {}
    references: list[str] = []

    def new_reference() -> str:
        while True:
            candidate = f"{rng.randrange(1000000000, 9999999999)}"
            suffix = candidate[-4:]
            if suffix in RESERVED_SUFFIXES or candidate in references:
                continue
            if used_suffix_counts.get(suffix, 0) >= 1:
                continue
            used_suffix_counts[suffix] = used_suffix_counts.get(suffix, 0) + 1
            references.append(candidate)
            return candidate

    orders, items, payments, returns_rows, refunds, scans = [], [], [], [], [], []
    line_counter = 0
    for (cid, _name, _email, _masked, _phone, _region, _addr) in customers:
        for _ in range(rng.choice([2, 3, 3, 3, 4, 4, 5])):
            reference = new_reference()
            status = weighted(rng, FULFILLMENT_MIX)
            placed = SCENARIO_DATE - dt.timedelta(days=rng.randint(2, 60))
            representative = None
            order_items_here = []
            for line in range(rng.choice([1, 2, 2, 3])):
                line_counter += 1
                variant = rng.choice(catalog_variants)
                price = float(variant[6])
                item_reference = f"item-{line_counter:05d}"
                order_items_here.append((item_reference, variant, price))
                if representative is None:
                    representative = variant[2]
                items.append((
                    item_reference, reference, line + 1, variant[0], variant[1],
                    variant[2], variant[3], variant[3], f"{price:.2f}", "USD",
                ))
            orders.append((reference, cid, placed.isoformat(), status,
                           "home_address_on_order", None, representative))

            total = sum(p for (_i, _v, p) in order_items_here)
            gift = None
            if rng.random() < 0.16:
                gift = min(round(rng.choice([10, 20, 25, 40, 50]), 2), total)
                payments.append((reference, "gift_card", f"{gift:.2f}", "USD", None))
                remainder = total - gift
            else:
                remainder = total
            tender = rng.choice(["debit", "credit", "credit", "debit"])
            card_last4 = f"{rng.randrange(1000, 9999)}"
            payments.append((reference, tender, f"{remainder:.2f}", "USD",
                             card_last4))

            # A shipment leaves a trail, not a single event: the handover, the
            # sorting scans, and the delivery attempt are separate rows, which
            # is what makes "the latest scan" a real query.
            if status in ("delivered", "out_for_delivery", "shipped"):
                handed_over = placed + dt.timedelta(days=1)
                stops = ["origin facility", "regional sort center", "local depot"]
                for offset, stop in enumerate(stops[:rng.choice([1, 1, 2])]):
                    moment = dt.datetime(handed_over.year, handed_over.month,
                                         handed_over.day, 6 + offset * 4,
                                         rng.choice([5, 17, 29, 44, 51]))
                    moment += dt.timedelta(days=offset)
                    scans.append((
                        reference, moment.strftime("%Y-%m-%dT%H:%M:00-04:00"),
                        moment.strftime("%H:%M on %B ") + str(moment.day),
                        stop, stop, None, None, None, False,
                    ))
                if status == "delivered":
                    delivered_on = placed + dt.timedelta(days=rng.randint(2, 6))
                    scanned = dt.datetime(delivered_on.year, delivered_on.month,
                                          delivered_on.day, rng.randint(9, 19),
                                          rng.choice([4, 12, 18, 26, 33, 41, 55]))
                    scans.append((
                        reference, scanned.strftime("%Y-%m-%dT%H:%M:00-04:00"),
                        scanned.strftime("%H:%M on %B ") + str(scanned.day),
                        rng.choice(SCAN_LOCATIONS), None, None, None, None, False,
                    ))

            # Refunds deliberately outnumber returns, because on a real ledger
            # they do and the difference is what a refund lookup has to sort
            # out. A completed return refunds to every tender the order was
            # settled on, so a part-gift-card order produces two rows; and money
            # goes back without any return at all when a price drops inside the
            # guarantee window or a late delivery voids the shipping charge. An
            # agent that assumes one refund per return, or that the only refund
            # on an order is the one for the returned item, picks the wrong row.
            def issue_refund(return_reference, amount, source, status_pool):
                refund_status = rng.choice(status_pool)
                card_share = amount if gift is None else round(amount * 0.7, 2)
                refunds.append((
                    reference, return_reference, tender, f"{card_share:.2f}",
                    "USD", refund_status, None, None, None, None, card_last4,
                    source,
                ))
                if gift is not None:
                    refunds.append((
                        reference, return_reference, "gift_card",
                        f"{amount - card_share:.2f}", "USD", "issued_available",
                        rng.choice(["active", "active", "exhausted"]),
                        f"{amount - card_share:.2f}", False,
                        "digital_gift_card_number_in_email", None, source,
                    ))

            if status == "delivered" and rng.random() < 0.65:
                for item_reference, variant, price in \
                        order_items_here[:rng.choice([1, 1, 2])]:
                    return_reference = new_reference()
                    accepted = SCENARIO_DATE - dt.timedelta(days=rng.randint(1, 21))
                    return_status = rng.choice(
                        ["complete", "complete", "complete", "complete",
                         "received", "in_transit"])
                    returns_rows.append((
                        return_reference, reference, item_reference, return_status,
                        rng.choice(["Eastwood store", "Rivergate store",
                                    "Clayborne store", "Northfield store"]),
                        accepted.isoformat(),
                        "restocked" if return_status == "complete" else None,
                    ))
                    if return_status == "complete":
                        issue_refund(return_reference, price, "store_register",
                                     ["issued_available", "settled", "settled",
                                      "submitted_no_settlement_confirmation"])

            if status in ("delivered", "shipped", "out_for_delivery") \
                    and rng.random() < 0.40:
                issue_refund(None, round(rng.randrange(200, 2600) / 100, 2),
                             "price_adjustment", ["settled", "settled", "active"])

            if rng.random() < 0.45:
                issue_refund(None, rng.choice([4.99, 6.95, 9.99, 12.5]),
                             "shipping_charge_reversal",
                             ["settled", "issued_available"])

    # One pair of orders on different accounts deliberately shares all four
    # trailing digits, so a reference resolved without a customer scope has a
    # genuine ambiguity to refuse rather than a theoretical one.
    ambiguous = [("4180265501", customers[3][0]), ("9047315501", customers[41][0])]
    for reference, cid in ambiguous:
        line_counter += 1
        variant = rng.choice(catalog_variants)
        orders.append((reference, cid, (SCENARIO_DATE - dt.timedelta(days=12)).isoformat(),
                       "delivered", "home_address_on_order", None, variant[2]))
        items.append((f"item-{line_counter:05d}", reference, 1, variant[0],
                      variant[1], variant[2], variant[3], variant[3],
                      f"{float(variant[6]):.2f}", "USD"))
        payments.append((reference, "credit", f"{float(variant[6]):.2f}", "USD",
                         f"{rng.randrange(1000, 9999)}"))

    out.append(insert("orders",
                      ["order_reference", "customer_id", "placed_on",
                       "fulfillment_status", "destination_label",
                       "replaces_order_reference", "representative_item"], orders))
    out.append(insert("order_items",
                      ["item_reference", "order_reference", "line_no",
                       "variant_reference", "product_reference", "name",
                       "variant_label", "color", "total_after_tax", "currency"],
                      items))
    out.append(insert("payments",
                      ["order_reference", "tender_type", "amount", "currency",
                       "original_card_last4"], payments))
    out.append(insert("returns",
                      ["return_reference", "order_reference", "item_reference",
                       "return_status", "accepted_at", "accepted_on",
                       "inventory_disposition"], returns_rows))
    out.append(insert("refunds",
                      ["order_reference", "return_reference", "tender_type",
                       "amount", "currency", "status", "ledger_status",
                       "available_balance", "used", "delivery",
                       "original_card_last4", "initiation_source"],
                      refunds))
    out.append(insert("carrier_scans",
                      ["order_reference", "scanned_at", "scanned_at_display",
                       "location", "evidence_location", "unit_number", "locker",
                       "photo_reference", "possible_misscan"], scans))

    # Unrelated cases. Roughly a third are already closed, so an agent that
    # assumes any case it finds is actionable is wrong about a third of the time.
    orders_by_customer: dict[str, list[str]] = {}
    for (reference, cid, _p, status, _d, _r, _item) in orders:
        if status == "delivered":
            orders_by_customer.setdefault(cid, []).append(reference)

    cases, case_items, case_notes = [], [], []
    candidates = [(cid, refs) for cid, refs in orders_by_customer.items() if refs]
    rng.shuffle(candidates)
    items_by_order: dict[str, list[str]] = {}
    for (item_reference, order_reference, *_rest) in items:
        items_by_order.setdefault(order_reference, []).append(item_reference)

    for index, (cid, refs) in enumerate(candidates[:60]):
        reference = rng.choice(refs)
        case_number = f"WST{rng.randrange(200000, 460000)}"
        closed = index % 3 == 0
        status = (rng.choice(CLOSED_CASE_STATUSES) if closed
                  else rng.choice(OPEN_CASE_STATUSES))
        opened = SCENARIO_DATE - dt.timedelta(days=rng.randint(1, 30))
        deadline_day = opened + dt.timedelta(days=1)
        cases.append((
            case_number, reference, cid, "delivery_trace", status,
            "delivered_not_received", None, "none",
            f"{deadline_day.isoformat()}T18:00:00-04:00",
            f"18:00 on {deadline_day.strftime('%B')} {deadline_day.day}",
            True, False, rng.choice(["undecided", "replacement", "locate_only"]),
            None, True, "trace_notification",
            "review requested resolution and fulfillment after an eligibility trigger",
            ["carrier_confirms_missing", "carrier_response_deadline_expires"],
            None, None, None, None, None, None, None, False, False,
            f"{opened.isoformat()}T10:00:00-04:00",
        ))
        for item_reference in items_by_order.get(reference, [])[:1]:
            case_items.append((case_number, item_reference))
        if rng.random() < 0.45:
            case_notes.append((
                case_number, 1,
                "Customer confirmed the building was checked before the trace was opened.",
                None, True, f"{opened.isoformat()}T10:05:00-04:00",
            ))

    out.append(insert("cases",
                      ["case_number", "order_reference", "customer_id", "case_type",
                       "status", "reason", "item_description", "carrier_response",
                       "deadline_at", "deadline_display",
                       "carrier_may_contact_customer", "replacement_created",
                       "requested_resolution", "needed_by", "approval_required",
                       "approval_channel", "next_action", "eligibility_triggers",
                       "review_window_min_days", "review_window_max_days",
                       "duplicate_refund_blocked", "return_evidence_attached",
                       "return_reference", "payment_reference",
                       "amount_under_review", "fee_reimbursement_approved",
                       "pickup_guaranteed", "opened_at"], cases))
    out.append(insert("case_items", ["case_number", "item_reference"], case_items))

    # Resolutions the desk has already unlocked on other people's traces.
    # Roughly a third of them unlock a refund and nothing else, so an agent that
    # reads "this case has eligible resolutions" as "a replacement can be
    # created" is wrong often enough to notice, and a replacement created off
    # this data is created from a real eligibility row rather than from the
    # caller's request.
    resolutions = []
    for index, (case_number, reference, *_rest) in enumerate(cases):
        if index % 5 not in (0, 3):
            continue
        eta = SCENARIO_DATE + dt.timedelta(days=rng.randint(2, 6))
        if index % 3 == 1:
            resolutions.append((reference, "refund", 1, None, True, False,
                                None, None, None, None, None))
            continue
        resolutions.append((
            reference, "replacement", 1, rng.choice([True, True, False]),
            False, False, True, False, eta.isoformat(),
            f"{eta.strftime('%B')} {eta.day}", "ship_to_address",
        ))
    out.append(insert("eligible_resolutions",
                      ["order_reference", "resolution_type", "position",
                       "preserves_original_price", "return_required",
                       "photo_required", "optional_photo_upload_available",
                       "photo_upload_blocks_fulfillment", "estimated_delivery_on",
                       "estimated_delivery_display", "default_fulfillment"],
                      resolutions))
    out.append(insert("case_notes",
                      ["case_number", "note_no", "note", "topic",
                       "visible_to_next_reviewer", "created_at"], case_notes))

    out.append("COMMIT;\n")

    counts = {
        "customers": len(customers), "orders": len(orders),
        "order_items": len(items), "payments": len(payments),
        "returns": len(returns_rows), "refunds": len(refunds),
        "carrier_scans": len(scans), "cases": len(cases),
        "case_notes": len(case_notes),
    }
    print("population:", ", ".join(f"{k}={v}" for k, v in counts.items()))
    return "".join(out)


def main() -> None:
    os.makedirs(SQL, exist_ok=True)
    reference, variants = build_reference()
    population = build_population(variants)
    with open(os.path.join(SQL, "002_reference.sql"), "w") as fh:
        fh.write(reference)
    with open(os.path.join(SQL, "003_population.sql"), "w") as fh:
        fh.write(population)
    print(f"wrote 002_reference.sql ({len(reference)} bytes) and "
          f"003_population.sql ({len(population)} bytes)")


if __name__ == "__main__":
    main()
