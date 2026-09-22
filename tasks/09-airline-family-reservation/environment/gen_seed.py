#!/usr/bin/env python3
"""Author-time generator for this task's catalog and population SQL.

Writes two files next to itself:

  sql/002_reference.sql   airports, destination areas, schedules, fare options,
                          availability, connecting itineraries, baggage and
                          accessibility tariffs, insurance bands, the scenario
                          clock, and the identifier allocators
  sql/003_population.sql  customers, cards, certificates, prior verifications,
                          cached searches and quotes, the record-locator pool,
                          and roughly three hundred existing reservations with
                          their travelers, tenders, and certificate drawdowns

The rows the recorded conversation reads are declared explicitly near the top of
each section rather than drawn from the random population, so regenerating with a
different RNG seed cannot move them. Everything else exists to make the lookups
non-trivial: customers who share the caller's surname, an almost identical email
address, certificates that are expired or nearly spent, fare families that are
sold out on the requested date, and a stranger holding a reservation on exactly
the itinerary being booked.

Every monetary figure the recording reports is asserted here against the sum of
the components that produce it, so a mistyped fare fails at generation time
rather than at replay time. The assertions at the bottom of `build_reference`
are the arithmetic the README describes.

This script never enters the container image; see environment/.dockerignore.

Usage:  python3 gen_seed.py
"""
from __future__ import annotations

import datetime as dt
import os
import random
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
SQL = os.path.join(HERE, "sql")

RNG_SEED = 20260826
SCENARIO_TIME = "2026-08-26T12:30:00-07:00"
SCENARIO_DATE = dt.date(2026, 8, 26)
CURRENCY = "USD"

# Dates the seeded schedule is bookable on. The conversation's outbound and
# return dates are in the list; the rest give an off-path search somewhere to go.
AVAILABILITY_DATES = ["2026-10-05", "2026-10-12", "2026-10-14",
                      "2026-10-16", "2026-10-19"]
OUTBOUND_DATE = "2026-10-14"
RETURN_DATE = "2026-10-19"

FARE_CLASSES = ["basic_economy", "standard_economy"]

# Size of the existing reservation estate, and the record-locator pool sequence
# left free for 004_scenario.sql to seed with the code the recording issues.
POPULATION_RESERVATIONS = 380
SCENARIO_POOL_SEQ = POPULATION_RESERVATIONS + 1

# Verification attempts already on file. The unresolved ones are numbered from
# the same series the identity_verification allocator issues from, so the
# allocator's counter has to start past this many of them.
POPULATION_VERIFICATIONS = 50


def money(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def q(value) -> str:
    """Render a Python value as a SQL literal."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, Decimal):
        return f"'{value}'"
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


def hhmm(minutes: int) -> str:
    minutes %= 1440
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def to_minutes(clock: str) -> int:
    hours, mins = clock.split(":")
    return int(hours) * 60 + int(mins)


# ---------------------------------------------------------------------------
# airports
# ---------------------------------------------------------------------------

# (code, name, city, region, timezone, utc offset in minutes for October 2026)
#
# The first five are read back by the recording: Phoenix and the three
# Washington-area airports by name, and Chicago as the connection point the
# one-stop comparison routes through. Offsets are the ones in effect for the
# seeded schedule season, and block times below are derived from them.
SCENARIO_AIRPORTS = [
    ("PHX", "Phoenix Sky Harbor", "Phoenix", "AZ", "America/Phoenix", -420),
    ("DCA", "Washington Reagan National", "Washington", "DC", "America/New_York", -240),
    ("IAD", "Washington Dulles International", "Dulles", "VA", "America/New_York", -240),
    ("BWI", "Baltimore/Washington International", "Baltimore", "MD", "America/New_York", -240),
    ("ORD", "Chicago O'Hare International", "Chicago", "IL", "America/Chicago", -300),
]

CATALOG_AIRPORTS = [
    ("ATL", "Atlanta Hartsfield-Jackson", "Atlanta", "GA", "America/New_York", -240),
    ("AUS", "Austin-Bergstrom", "Austin", "TX", "America/Chicago", -300),
    ("BNA", "Nashville International", "Nashville", "TN", "America/Chicago", -300),
    ("BOS", "Boston Logan", "Boston", "MA", "America/New_York", -240),
    ("CLT", "Charlotte Douglas", "Charlotte", "NC", "America/New_York", -240),
    ("CVG", "Cincinnati/Northern Kentucky", "Covington", "KY", "America/New_York", -240),
    ("DEN", "Denver International", "Denver", "CO", "America/Denver", -360),
    ("DFW", "Dallas/Fort Worth", "Dallas", "TX", "America/Chicago", -300),
    ("DTW", "Detroit Metropolitan", "Detroit", "MI", "America/New_York", -240),
    ("EWR", "Newark Liberty", "Newark", "NJ", "America/New_York", -240),
    ("FLL", "Fort Lauderdale-Hollywood", "Fort Lauderdale", "FL", "America/New_York", -240),
    ("IAH", "Houston George Bush", "Houston", "TX", "America/Chicago", -300),
    ("JFK", "New York Kennedy", "New York", "NY", "America/New_York", -240),
    ("LAS", "Las Vegas Harry Reid", "Las Vegas", "NV", "America/Los_Angeles", -420),
    ("LAX", "Los Angeles International", "Los Angeles", "CA", "America/Los_Angeles", -420),
    ("LGA", "New York LaGuardia", "New York", "NY", "America/New_York", -240),
    ("MCO", "Orlando International", "Orlando", "FL", "America/New_York", -240),
    ("MDW", "Chicago Midway", "Chicago", "IL", "America/Chicago", -300),
    ("MIA", "Miami International", "Miami", "FL", "America/New_York", -240),
    ("MSP", "Minneapolis-Saint Paul", "Minneapolis", "MN", "America/Chicago", -300),
    ("MSY", "New Orleans Louis Armstrong", "New Orleans", "LA", "America/Chicago", -300),
    ("OAK", "Oakland International", "Oakland", "CA", "America/Los_Angeles", -420),
    ("PDX", "Portland International", "Portland", "OR", "America/Los_Angeles", -420),
    ("PHL", "Philadelphia International", "Philadelphia", "PA", "America/New_York", -240),
    ("PIT", "Pittsburgh International", "Pittsburgh", "PA", "America/New_York", -240),
    ("RDU", "Raleigh-Durham", "Raleigh", "NC", "America/New_York", -240),
    ("SAN", "San Diego International", "San Diego", "CA", "America/Los_Angeles", -420),
    ("SAT", "San Antonio International", "San Antonio", "TX", "America/Chicago", -300),
    ("SEA", "Seattle-Tacoma", "Seattle", "WA", "America/Los_Angeles", -420),
    ("SFO", "San Francisco International", "San Francisco", "CA", "America/Los_Angeles", -420),
    ("SJC", "San Jose Mineta", "San Jose", "CA", "America/Los_Angeles", -420),
    ("SLC", "Salt Lake City International", "Salt Lake City", "UT", "America/Denver", -360),
    ("SNA", "Orange County John Wayne", "Santa Ana", "CA", "America/Los_Angeles", -420),
    ("STL", "St. Louis Lambert", "St. Louis", "MO", "America/Chicago", -300),
    ("TPA", "Tampa International", "Tampa", "FL", "America/New_York", -240),
]

AIRPORTS = SCENARIO_AIRPORTS + CATALOG_AIRPORTS
OFFSET = {code: offset for (code, _n, _c, _r, _tz, offset) in AIRPORTS}

# ---------------------------------------------------------------------------
# destination areas
# ---------------------------------------------------------------------------

# (area_id, display_name, short_name, kind, search terms, recommendation basis,
#  retrieved_at, [(airport, distance_miles, ground_minutes, rank), ...])
#
# The National Mall row is the one the recording reads: its three airports, their
# order, the recommended code, the basis sentence, and the retrieval timestamp
# are all reproduced from it. It is a landmark rather than a metro area, and the
# handler resolves landmarks ahead of the metro that contains them, which is why
# "Washington, DC National Mall" does not fall through to the metro row below it.
DESTINATION_AREAS = [
    ("national-mall", "National Mall, Washington, DC", "National Mall", "landmark",
     ["national mall", "the mall washington", "smithsonian", "capitol mall"],
     "closest airport returned for the National Mall destination area and most "
     "direct city access among the supported options",
     "2026-08-26T12:31:00-07:00",
     [("DCA", 3.2, 18, 1), ("IAD", 26.4, 52, 2), ("BWI", 32.1, 64, 3)]),
    ("washington-dc-metro", "Washington, DC metropolitan area", "Washington, DC", "metro",
     ["washington, dc", "washington dc", "district of columbia", "washington"],
     "shortest scheduled ground access to the District among the supported "
     "Washington-area options",
     "2026-08-26T11:58:00-07:00",
     [("DCA", 4.8, 22, 1), ("IAD", 27.0, 55, 2), ("BWI", 33.6, 68, 3)]),
    ("baltimore-inner-harbor", "Inner Harbor, Baltimore", "Inner Harbor", "landmark",
     ["inner harbor", "baltimore harbor", "camden yards"],
     "closest airport to the Inner Harbor and the only supported option with "
     "direct rail access to it",
     "2026-08-26T11:41:00-07:00",
     [("BWI", 9.4, 24, 1), ("DCA", 39.8, 78, 2), ("IAD", 51.2, 96, 3)]),
    ("manhattan-midtown", "Midtown Manhattan, New York", "Midtown Manhattan", "district",
     ["midtown manhattan", "times square", "manhattan", "new york city", "new york"],
     "shortest scheduled ground access to Midtown among the supported New York "
     "area options",
     "2026-08-26T10:12:00-07:00",
     [("LGA", 8.6, 34, 1), ("JFK", 16.4, 58, 2), ("EWR", 17.2, 62, 3)]),
    ("chicago-loop", "The Loop, Chicago", "the Loop", "district",
     ["the loop", "downtown chicago", "magnificent mile", "chicago"],
     "closest airport to the Loop and the shorter of the two supported rail "
     "connections into it",
     "2026-08-26T09:47:00-07:00",
     [("MDW", 10.1, 32, 1), ("ORD", 17.9, 48, 2)]),
    ("south-beach", "South Beach, Miami", "South Beach", "landmark",
     ["south beach", "miami beach", "ocean drive", "miami"],
     "closest airport to South Beach among the supported south Florida options",
     "2026-08-26T09:02:00-07:00",
     [("MIA", 11.7, 30, 1), ("FLL", 24.3, 46, 2)]),
    ("walt-disney-world", "Walt Disney World, Florida", "Walt Disney World", "landmark",
     ["walt disney world", "disney world", "magic kingdom", "orlando"],
     "closest airport to the resort area with scheduled shuttle service among "
     "the supported central Florida options",
     "2026-08-26T08:35:00-07:00",
     [("MCO", 21.6, 38, 1), ("TPA", 78.4, 104, 2)]),
    ("napa-valley", "Napa Valley, California", "Napa Valley", "district",
     ["napa valley", "napa", "sonoma", "wine country"],
     "shortest drive to the valley floor among the supported Bay Area options",
     "2026-08-26T08:11:00-07:00",
     [("OAK", 41.5, 62, 1), ("SFO", 52.8, 84, 2), ("SJC", 71.3, 108, 3)]),
    ("hollywood", "Hollywood, Los Angeles", "Hollywood", "district",
     ["hollywood", "west hollywood", "santa monica", "los angeles"],
     "closest airport to Hollywood among the supported southern California "
     "options",
     "2026-08-26T07:54:00-07:00",
     [("LAX", 14.2, 40, 1), ("SNA", 44.7, 72, 2), ("SAN", 121.4, 168, 3)]),
    ("las-vegas-strip", "The Strip, Las Vegas", "the Strip", "landmark",
     ["the strip", "las vegas strip", "las vegas", "vegas"],
     "the only supported airport serving the Strip, adjacent to its southern end",
     "2026-08-26T07:38:00-07:00",
     [("LAS", 2.4, 12, 1)]),
    ("grand-canyon-south-rim", "Grand Canyon South Rim, Arizona", "the South Rim",
     "landmark",
     ["grand canyon", "south rim", "canyon south rim"],
     "shortest scheduled ground connection to the South Rim among the supported "
     "options",
     "2026-08-26T07:15:00-07:00",
     [("PHX", 223.0, 268, 1), ("LAS", 278.0, 306, 2)]),
    ("french-quarter", "French Quarter, New Orleans", "the French Quarter", "district",
     ["french quarter", "bourbon street", "new orleans"],
     "the only supported airport serving the French Quarter, with direct highway "
     "access",
     "2026-08-26T06:52:00-07:00",
     [("MSY", 14.8, 28, 1)]),
    ("pike-place-market", "Pike Place Market, Seattle", "Pike Place Market", "landmark",
     ["pike place", "space needle", "downtown seattle", "seattle"],
     "the only supported airport serving downtown Seattle, with direct light "
     "rail to the market",
     "2026-08-26T06:30:00-07:00",
     [("SEA", 15.3, 38, 1)]),
    ("boston-north-end", "North End, Boston", "the North End", "district",
     ["north end", "fenway", "back bay", "boston"],
     "the only supported airport serving central Boston, and the shortest "
     "harbour tunnel transfer of the returned options",
     "2026-08-26T06:07:00-07:00",
     [("BOS", 3.6, 16, 1)]),
    ("silicon-valley", "Silicon Valley, California", "Silicon Valley", "district",
     ["silicon valley", "palo alto", "mountain view", "san jose"],
     "shortest scheduled ground access to the valley among the supported Bay "
     "Area options",
     "2026-08-26T05:44:00-07:00",
     [("SJC", 9.8, 22, 1), ("SFO", 31.2, 48, 2), ("OAK", 34.6, 56, 3)]),
]

# ---------------------------------------------------------------------------
# schedules and fares
# ---------------------------------------------------------------------------

# (flight_id, flight_number, origin, destination, departure_time, duration,
#  stops, basic base fare, standard base fare, per-direction tax)
#
# The first two rows are the itinerary the caller books. Their prices are the
# ones the recording reports, decomposed: a fare family's price per traveler is
# the sum over both directions of base fare plus tax, so
#
#   basic_economy     (219.00 + 37.20) * 2 = 512.40
#   standard_economy  (241.90 + 37.20) * 2 = 558.20
#
# and nothing stores 512.40 or 558.20 anywhere. Arrival times are computed from
# the departure time, the block time, and the two airports' offsets, and the
# assertions in build_reference check them against what the recording read out.
SCENARIO_FLIGHTS = [
    ("2133fbc8-ed10-42aa-baa2-12e3d15a6a05", "BM 418", "PHX", "DCA", "09:10", 260, 0, "219.00", "241.90", "37.20"),
    ("3ac31d55-0dbc-4f79-892e-743257ec9f13", "BM 419", "DCA", "PHX", "15:40", 290, 0, "219.00", "241.90", "37.20"),
    # Same route and same day, sold out in both fare families on 14 October, so a
    # nonstop search on that date has exactly one sellable outbound. On any other
    # seeded date this flight is the one that comes back.
    ("BM-PHX-DCA-1725", "BM 422", "PHX", "DCA", "17:25", 260, 0, "229.00", "251.90", "37.20"),
    ("BM-DCA-PHX-0730", "BM 423", "DCA", "PHX", "07:30", 290, 0, "229.00", "251.90", "37.20"),
    # The connection the price comparison routes through. Segment fares sum to
    # 31.00 per traveler below the nonstop in both fare families, which is the
    # 62.00 total saving for two travelers that the recording reports.
    ("BM-PHX-ORD-0600", "BM 610", "PHX", "ORD", "06:00", 165, 0, "105.00", "118.40", "18.60"),
    ("BM-ORD-DCA-1310", "BM 611", "ORD", "DCA", "13:10", 120, 0, "98.00", "108.00", "18.60"),
    ("BM-DCA-ORD-1215", "BM 612", "DCA", "ORD", "12:15", 135, 0, "100.00", "112.40", "18.60"),
    ("BM-ORD-PHX-1445", "BM 613", "ORD", "PHX", "14:45", 240, 0, "104.00", "114.00", "18.60"),
    # Other Washington-area service out of Phoenix, so a search on the airports
    # the caller rejected is answerable.
    ("BM-PHX-IAD-0745", "BM 430", "PHX", "IAD", "07:45", 265, 0, "204.00", "228.50", "36.40"),
    ("BM-IAD-PHX-1620", "BM 431", "IAD", "PHX", "16:20", 295, 0, "204.00", "228.50", "36.40"),
    ("BM-PHX-BWI-0655", "BM 444", "PHX", "BWI", "06:55", 270, 0, "196.00", "217.75", "35.90"),
    ("BM-BWI-PHX-1710", "BM 445", "BWI", "PHX", "17:10", 300, 0, "196.00", "217.75", "35.90"),
]

# Layovers in the connecting itinerary, in minutes, after each first segment.
# Derived from the segment times above: ORD arrival 10:45 to 13:10 departure is
# 145 minutes outbound, 13:30 to 14:45 is 75 minutes on the way home.
SCENARIO_CONNECTION = {
    "itinerary_id": "87ba9919-0d38-418a-868c-42185e5a9ab9",
    "origin": "PHX",
    "destination": "DCA",
    "via": "ORD",
    "display": "almost three hours",
    "outbound": [("BM-PHX-ORD-0600", 145), ("BM-ORD-DCA-1310", 0)],
    "return": [("BM-DCA-ORD-1215", 75), ("BM-ORD-PHX-1445", 0)],
}

BAGGAGE_FEES = [("basic_economy", "40.00"), ("standard_economy", "35.00")]

# Trip-cost bands on the per-traveler fare and taxes, exclusive lower bound and
# inclusive upper bound. The recorded itinerary prices at 558.20 per traveler,
# which lands in the third standard-tier band at 47.30 a head, so two travelers
# come to the 94.60 the recording reports.
INSURANCE_BANDS = [
    ("band-1", "0.00", "300.00", "21.40", "33.20", "19.00"),
    ("band-2", "300.00", "500.00", "33.80", "48.60", "30.00"),
    ("band-3", "500.00", "750.00", "47.30", "66.40", "42.00"),
    ("band-4", "750.00", "1200.00", "62.90", "88.10", "55.00"),
    ("band-5", "1200.00", "9999.00", "84.50", "118.00", "74.00"),
]

INSURANCE_TIERS = [
    ("standard", "BlueMesa Trip Protection", "insurance-plan-document-current", True),
    ("plus", "BlueMesa Trip Protection Plus", "insurance-plan-document-plus-current", True),
    ("legacy", "BlueMesa Trip Cover 2025", "insurance-plan-document-2025-archive", False),
]

# (device_type, effective_at, aliases, counts_as_paid_bag, fee, serial required,
#  labeling guidance, airport notification required, quote default)
#
# The folding-walker row effective 1 July 2026 is the one the recording reads,
# including its labeling sentence. The 2025 version above it is superseded and
# exists so that version selection is a real query rather than a single row that
# cannot be wrong. Devices that carry a serial-number requirement or that exceed
# the free allowance are here so an unrecorded device type gets the rule that
# applies to it instead of the walker's rule.
MOBILITY_RULES = [
    ("folding walker", "2025-06-01", ["walker", "folding walker"], False, "0.00", False,
     "label with customer contact information", False, False),
    ("folding walker", "2026-07-01", ["walker", "folding walker", "collapsible walker"],
     False, "0.00", False,
     "label with customer contact information and identify it as a mobility "
     "device at the airport", True, False),
    ("manual wheelchair", "2026-07-01", ["wheelchair", "manual chair", "push chair"],
     False, "0.00", False,
     "label with customer contact information and identify it as a mobility "
     "device at the airport", True, False),
    ("powered wheelchair", "2026-07-01",
     ["electric wheelchair", "power chair", "powered chair"], False, "0.00", True,
     "label with customer contact information, record the battery serial number, "
     "and identify it as a mobility device at the airport", True, False),
    ("mobility scooter", "2026-07-01", ["scooter", "travel scooter"], False, "0.00", True,
     "label with customer contact information, record the battery serial number, "
     "and identify it as a mobility device at the airport", True, False),
    ("rollator", "2026-07-01", ["rolling walker", "four wheel walker"], False, "0.00",
     False,
     "label with customer contact information and identify it as a mobility "
     "device at the airport", True, False),
    ("walking cane", "2026-04-01", ["cane", "walking stick"], False, "0.00", False,
     "keep the cane with the customer in the cabin", False, False),
    ("crutches", "2026-04-01", ["crutch", "forearm crutches"], False, "0.00", False,
     "keep crutches with the customer in the cabin", False, False),
    ("knee scooter", "2026-07-01", ["knee walker"], False, "0.00", False,
     "label with customer contact information and identify it as a mobility "
     "device at the airport", True, False),
    ("transport chair", "2026-07-01", ["companion chair"], False, "0.00", False,
     "label with customer contact information and identify it as a mobility "
     "device at the airport", True, False),
    ("portable oxygen concentrator", "2026-07-01", ["oxygen concentrator", "poc"],
     False, "0.00", True,
     "label with customer contact information, record the device serial number, "
     "and notify the airport medical desk", True, False),
    ("oversized sports wheelchair", "2026-07-01", ["sports wheelchair", "racing chair"],
     True, "35.00", True,
     "label with customer contact information, record the frame serial number, "
     "and present the chair at the oversized counter", True, False),
    # What a quote uses before the caller has named a device, and what an
    # unrecognized device type falls back to.
    ("unspecified mobility device", "2026-07-01", [], False, "0.00", False,
     "label with customer contact information and identify it as a mobility "
     "device at the airport", True, True),
]


def insurance_plans() -> list[tuple]:
    rows = []
    for (band, low, high, standard, plus, legacy) in INSURANCE_BANDS:
        prices = {"standard": standard, "plus": plus, "legacy": legacy}
        for (tier, name, document, active) in INSURANCE_TIERS:
            rows.append((f"insurance-{tier}-{band}", f"{name} ({band})", tier,
                         money(low), money(high), money(prices[tier]), CURRENCY,
                         document, active))
    return rows


class Pricer:
    """The pricing rules, held in one place so the generator and the tool server
    agree by construction rather than by coincidence.

    The tool server implements the same arithmetic against the same rows; this
    copy exists so the generator can assert that the seeded components really do
    add up to the figures the recording reports.
    """

    def __init__(self, fares: dict, bag_fees: dict, plans: list[tuple],
                 device_default_fee: Decimal):
        self.fares = fares
        self.bag_fees = bag_fees
        self.plans = plans
        self.device_default_fee = device_default_fee

    def per_traveler(self, outbound: str, inbound: str, fare_class: str) -> Decimal:
        total = Decimal("0.00")
        for flight_id in (outbound, inbound):
            base, tax = self.fares[(flight_id, fare_class)]
            total += base + tax
        return total

    def plan_for(self, per_traveler: Decimal) -> tuple:
        for row in self.plans:
            (_plan_id, _name, tier, low, high, _price, _cur, _doc, active) = row
            if active and tier == "standard" and low < per_traveler <= high:
                return row
        raise ValueError(f"no active standard insurance band covers {per_traveler}")

    def quote(self, outbound: str, inbound: str, fare_class: str, travelers: int,
              bags: int, devices: int, insurance: bool) -> dict:
        per = self.per_traveler(outbound, inbound, fare_class)
        fare_taxes_bags = per * travelers + self.bag_fees[fare_class] * bags
        device_charge = self.device_default_fee * devices
        result = {
            "per_traveler": per,
            "fare_taxes_and_checked_bags": money(fare_taxes_bags),
            "mobility_device_charge": money(device_charge),
            "trip_insurance": None,
            "plan_id": None,
            "total_with_insurance": None,
        }
        if insurance:
            plan = self.plan_for(per)
            premium = money(plan[5] * travelers)
            result["trip_insurance"] = premium
            result["plan_id"] = plan[0]
            result["total_with_insurance"] = money(
                result["fare_taxes_and_checked_bags"] + result["mobility_device_charge"]
                + premium)
        return result

    def charged_total(self, outbound: str, inbound: str, fare_class: str,
                      travelers: int, bags: int, devices: int,
                      insurance: bool) -> tuple[Decimal, str | None, Decimal | None]:
        quote = self.quote(outbound, inbound, fare_class, travelers, bags, devices,
                           insurance)
        if insurance:
            return quote["total_with_insurance"], quote["plan_id"], quote["trip_insurance"]
        return (money(quote["fare_taxes_and_checked_bags"]
                      + quote["mobility_device_charge"]), None, None)


def build_flight_rows(rng: random.Random) -> tuple[list[tuple], dict]:
    """Schedules and fare options, scenario rows first then a wide catalog."""
    flights: list[tuple] = []
    fares: dict = {}

    def add(flight_id, number, origin, destination, departure, duration, stops,
            basic, standard, tax):
        departure_minutes = to_minutes(departure)
        arrival_minutes = (departure_minutes - OFFSET[origin] + duration
                           + OFFSET[destination])
        flights.append((flight_id, "BM", number, origin, destination, departure,
                        hhmm(arrival_minutes), duration, stops,
                        arrival_minutes >= 1440))
        fares[(flight_id, "basic_economy")] = (money(basic), money(tax))
        fares[(flight_id, "standard_economy")] = (money(standard), money(tax))

    for row in SCENARIO_FLIGHTS:
        add(*row)

    # A catalog wide enough that a route lookup has to discriminate: hub-heavy,
    # both directions on every route, and block times derived from the same
    # offset arithmetic as the scenario rows.
    hubs = ["ORD", "DFW", "ATL", "DEN", "CLT", "IAH", "MSP", "PHL"]
    spokes = [code for (code, *_rest) in AIRPORTS]
    routes: list[tuple[str, str]] = []
    for hub in hubs:
        for spoke in spokes:
            if spoke != hub:
                routes.append((hub, spoke))
    rng.shuffle(routes)

    used = {(f[3], f[4], f[5]) for f in flights}
    number = 700
    for (origin, destination) in routes:
        if len(flights) >= 200:
            break
        offset_gap = abs(OFFSET[origin] - OFFSET[destination]) // 60
        duration = rng.randint(70, 155) + 45 * offset_gap
        departure = hhmm(rng.choice([5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16,
                                     17, 18, 19, 20]) * 60
                         + rng.choice([0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55]))
        if (origin, destination, departure) in used:
            continue
        used.add((origin, destination, departure))
        basic = money(rng.randint(78, 268)) + money(rng.choice(["0.00", "0.50", "0.75"]))
        standard = basic + money(rng.randint(14, 62)) + money(rng.choice(["0.00", "0.40", "0.90"]))
        tax = money(rng.randint(24, 44)) + money(rng.choice(["0.00", "0.20", "0.60", "0.80"]))
        number += 1
        add(f"BM-{origin}-{destination}-{departure.replace(':', '')}",
            f"BM {number}", origin, destination, departure, duration,
            0 if rng.random() > 0.08 else 1, basic, standard, tax)

    return flights, fares


def build_availability(rng: random.Random, flights: list[tuple]) -> list[tuple]:
    rows: list[tuple] = []
    # The booked itinerary and the connection segments are comfortably open on
    # the dates the caller asked for; the second Phoenix-Washington pair is sold
    # out on those dates so the recorded search has one sellable option.
    fixed = {
        ("2133fbc8-ed10-42aa-baa2-12e3d15a6a05", OUTBOUND_DATE): (11, 14),
        ("3ac31d55-0dbc-4f79-892e-743257ec9f13", RETURN_DATE): (9, 12),
        ("BM-PHX-DCA-1725", OUTBOUND_DATE): (0, 0),
        ("BM-DCA-PHX-0730", RETURN_DATE): (0, 0),
        ("BM-PHX-ORD-0600", OUTBOUND_DATE): (16, 21),
        ("BM-ORD-DCA-1310", OUTBOUND_DATE): (13, 18),
        ("BM-DCA-ORD-1215", RETURN_DATE): (12, 17),
        ("BM-ORD-PHX-1445", RETURN_DATE): (10, 15),
    }
    for flight in flights:
        flight_id = flight[0]
        for date in AVAILABILITY_DATES:
            seats = fixed.get((flight_id, date))
            if seats is None:
                sold_out = rng.random() < 0.12
                seats = (0, 0) if sold_out else (rng.randint(2, 34), rng.randint(2, 34))
            rows.append((flight_id, date, "basic_economy", seats[0]))
            rows.append((flight_id, date, "standard_economy", seats[1]))
    return rows


def build_connections(rng: random.Random, flights: list[tuple],
                      fares: dict) -> tuple[list[tuple], list[tuple]]:
    """The scenario connection plus a few built out of the generated schedule."""
    by_id = {f[0]: f for f in flights}

    def utc_arrival(flight_id: str) -> int:
        f = by_id[flight_id]
        return to_minutes(f[5]) - OFFSET[f[3]] + f[7]

    def utc_departure(flight_id: str) -> int:
        f = by_id[flight_id]
        return to_minutes(f[5]) - OFFSET[f[3]]

    itineraries: list[tuple] = []
    segments: list[tuple] = []

    connection = SCENARIO_CONNECTION
    itineraries.append((connection["itinerary_id"], connection["origin"],
                        connection["destination"], OUTBOUND_DATE, RETURN_DATE,
                        connection["via"], connection["display"], True))
    for direction in ("outbound", "return"):
        for index, (flight_id, layover) in enumerate(connection[direction], start=1):
            segments.append((connection["itinerary_id"], direction, index, flight_id,
                             layover))

    # Connections over generated flights: a hub where the inbound arrival and the
    # onward departure are 45 to 300 minutes apart in real time, in both
    # directions, on a route that also has a nonstop to compare against.
    nonstop = {}
    for f in flights:
        if f[8] == 0:
            nonstop.setdefault((f[3], f[4]), []).append(f[0])
    departures: dict[str, list[str]] = {}
    for f in flights:
        departures.setdefault(f[3], []).append(f[0])

    def leg(origin: str, destination: str, via: str) -> tuple | None:
        for first in departures.get(origin, []):
            if by_id[first][4] != via:
                continue
            for second in departures.get(via, []):
                if by_id[second][4] != destination:
                    continue
                layover = (utc_departure(second) - utc_arrival(first)) % 1440
                if 45 <= layover <= 300:
                    return (first, second, layover)
        return None

    made = 0
    for (origin, destination) in sorted(nonstop):
        if made >= 10:
            break
        if (destination, origin) not in nonstop:
            continue
        for via in ("ORD", "DFW", "ATL", "DEN", "CLT", "IAH", "MSP", "PHL"):
            if via in (origin, destination):
                continue
            out = leg(origin, destination, via)
            back = leg(destination, origin, via)
            if not out or not back:
                continue
            itinerary_id = f"itinerary-{origin.lower()}-{destination.lower()}-via-{via.lower()}"
            departure_date = rng.choice(AVAILABILITY_DATES[:3])
            return_date = AVAILABILITY_DATES[-1]
            direct_out = nonstop[(origin, destination)][0]
            direct_back = nonstop[(destination, origin)][0]
            direct = sum(by_id[f][7] for f in (direct_out, direct_back))
            elapsed_out = by_id[out[0]][7] + out[2] + by_id[out[1]][7]
            elapsed_back = by_id[back[0]][7] + back[2] + by_id[back[1]][7]
            added = max(elapsed_out - by_id[direct_out][7],
                        elapsed_back - by_id[direct_back][7])
            if added <= 0 or direct <= 0:
                continue
            itineraries.append((itinerary_id, origin, destination, departure_date,
                                return_date, via, describe_duration(added),
                                rng.random() > 0.2))
            segments.append((itinerary_id, "outbound", 1, out[0], out[2]))
            segments.append((itinerary_id, "outbound", 2, out[1], 0))
            segments.append((itinerary_id, "return", 1, back[0], back[2]))
            segments.append((itinerary_id, "return", 2, back[1], 0))
            made += 1
            break

    return itineraries, segments


def describe_duration(minutes: int) -> str:
    """Spoken form of an added travel time, as the recording reads it aloud."""
    hours, rest = divmod(minutes, 60)
    if rest == 0:
        return f"about {hours} hours" if hours != 1 else "about an hour"
    if rest >= 45:
        return f"almost {hours + 1} hours"
    if rest <= 15:
        return f"just over {hours} hours"
    return f"about {hours} and a half hours"


def build_reference() -> tuple[str, dict]:
    rng = random.Random(RNG_SEED)
    out = [
        "-- Airports, destination areas, schedules, fares, tariffs, insurance\n"
        "-- bands, the scenario clock, and the identifier allocators.\n"
        "-- Generated by environment/gen_seed.py; do not edit by hand.\n\n"
        "BEGIN;\n\n"
    ]

    out.append(insert("scenario", ["key", "value"], [
        ("scenario_time", SCENARIO_TIME),
        ("conversation_id", "airline-family-reservation"),
        ("domain", "airline"),
        ("timezone", "America/Phoenix"),
        ("currency", CURRENCY),
        # How long a fresh quote holds, and the sentinels the registry documents
        # in place of a timestamp.
        ("quote_validity_hours", "24"),
        ("verification_expiry", "end_of_call"),
        ("search_expiry", "quote_required_before_booking"),
    ]))

    # Searches, quotes, and specialist transfers are numbered sequentially.
    # Record locators are not, so they come out of confirmation_code_pool.
    out.append(insert("id_allocator",
                      ["entity_type", "scope", "next_value", "template"], [
                          ("flight_search", "", 31, "search-{n:05d}"),
                          ("fare_quote", "", 51, "quote-{n:05d}"),
                          ("specialist_transfer", "", 1, "specialist-transfer-{n:04d}"),
                          # A verification record is named after the customer it
                          # clears, so this allocator serves attempts that
                          # cleared nobody, which still have to leave an
                          # addressable record. The estate's own failed attempts
                          # occupy the low end of the series, which is why the
                          # counter starts past them.
                          ("identity_verification", "", POPULATION_VERIFICATIONS + 1,
                           "verification-unresolved-{n:04d}"),
                      ]))

    out.append(insert("airports",
                      ["code", "name", "city", "state_or_region", "timezone",
                       "utc_offset_minutes", "served"],
                      [(code, name, city, region, tz, offset, True)
                       for (code, name, city, region, tz, offset) in AIRPORTS]))

    area_rows, link_rows = [], []
    for (area_id, display, short, kind, terms, basis, retrieved, links) in DESTINATION_AREAS:
        area_rows.append((area_id, display, short, kind, terms, basis, retrieved))
        for (code, miles, minutes, rank) in links:
            link_rows.append((area_id, code, money(miles), minutes, rank))
    out.append(insert("destination_areas",
                      ["area_id", "display_name", "short_name", "area_kind",
                       "search_terms", "recommendation_basis", "retrieved_at"],
                      area_rows))
    out.append(insert("airport_area_links",
                      ["area_id", "airport_code", "distance_miles",
                       "ground_access_minutes", "proximity_rank"], link_rows))

    flights, fares = build_flight_rows(rng)
    out.append(insert("flights",
                      ["flight_id", "carrier", "flight_number", "origin_code",
                       "destination_code", "departure_time", "arrival_time",
                       "duration_minutes", "stops", "arrives_next_day"], flights))

    fare_rows = []
    for flight in flights:
        for fare_class in FARE_CLASSES:
            base, tax = fares[(flight[0], fare_class)]
            fare_rows.append((flight[0], fare_class, base, tax,
                              fare_class == "standard_economy"))
    out.append(insert("fare_options",
                      ["flight_id", "fare_class", "base_fare", "tax_amount",
                       "advance_seat_selection_allowed"], fare_rows))

    availability = build_availability(rng, flights)
    out.append(insert("flight_availability",
                      ["flight_id", "departure_date", "fare_class", "seats_remaining"],
                      availability))

    itineraries, segments = build_connections(rng, flights, fares)
    out.append(insert("connecting_itineraries",
                      ["itinerary_id", "origin_code", "destination_code",
                       "departure_date", "return_date", "via_airport_code",
                       "additional_duration_display", "offered"], itineraries))
    out.append(insert("connecting_itinerary_segments",
                      ["itinerary_id", "direction", "segment_index", "flight_id",
                       "layover_after_minutes"], segments))

    out.append(insert("baggage_fees", ["fare_class", "per_bag_amount", "currency"],
                      [(fare_class, money(amount), CURRENCY)
                       for (fare_class, amount) in BAGGAGE_FEES]))

    out.append(insert("mobility_device_rules",
                      ["device_type", "effective_at", "aliases", "counts_as_paid_bag",
                       "fee", "currency", "serial_number_required", "labeling_guidance",
                       "airport_notification_required", "is_quote_default"],
                      [(device, effective, aliases, bag, money(fee), CURRENCY,
                        serial, labeling, notify, default)
                       for (device, effective, aliases, bag, fee, serial, labeling,
                            notify, default) in MOBILITY_RULES]))

    plans = insurance_plans()
    out.append(insert("insurance_plans",
                      ["plan_id", "display_name", "tier", "min_trip_cost",
                       "max_trip_cost", "price_per_traveler", "currency",
                       "document_reference", "active"], plans))

    out.append("COMMIT;\n")

    bag_fees = {fare_class: money(amount) for (fare_class, amount) in BAGGAGE_FEES}
    # The tariff line a quote uses before a device type has been stated.
    device_default = next(money(rule[4]) for rule in MOBILITY_RULES if rule[8])
    pricer = Pricer(fares, bag_fees, plans, device_default)

    # The recording's arithmetic, checked against the components above.
    by_id = {f[0]: f for f in flights}
    assert by_id["2133fbc8-ed10-42aa-baa2-12e3d15a6a05"][6] == "16:30", by_id["2133fbc8-ed10-42aa-baa2-12e3d15a6a05"][6]
    assert by_id["3ac31d55-0dbc-4f79-892e-743257ec9f13"][6] == "17:30", by_id["3ac31d55-0dbc-4f79-892e-743257ec9f13"][6]
    assert pricer.per_traveler("2133fbc8-ed10-42aa-baa2-12e3d15a6a05", "3ac31d55-0dbc-4f79-892e-743257ec9f13",
                               "basic_economy") == money("512.40")
    assert pricer.per_traveler("2133fbc8-ed10-42aa-baa2-12e3d15a6a05", "3ac31d55-0dbc-4f79-892e-743257ec9f13",
                               "standard_economy") == money("558.20")

    connection_price = {}
    for fare_class in FARE_CLASSES:
        total = Decimal("0.00")
        for direction in ("outbound", "return"):
            for (flight_id, _layover) in SCENARIO_CONNECTION[direction]:
                base, tax = fares[(flight_id, fare_class)]
                total += base + tax
        connection_price[fare_class] = money(total)
    assert connection_price["standard_economy"] == money("527.20"), connection_price
    assert connection_price["basic_economy"] == money("481.40"), connection_price
    for fare_class in FARE_CLASSES:
        direct = pricer.per_traveler("2133fbc8-ed10-42aa-baa2-12e3d15a6a05", "3ac31d55-0dbc-4f79-892e-743257ec9f13", fare_class)
        assert (direct - connection_price[fare_class]) * 2 == money("62.00"), fare_class

    outbound_elapsed = 165 + 145 + 120
    return_elapsed = 135 + 75 + 240
    assert max(outbound_elapsed - 260, return_elapsed - 290) == 170

    quote = pricer.quote("2133fbc8-ed10-42aa-baa2-12e3d15a6a05", "3ac31d55-0dbc-4f79-892e-743257ec9f13", "standard_economy",
                         2, 2, 1, True)
    assert quote["fare_taxes_and_checked_bags"] == money("1186.40"), quote
    assert quote["mobility_device_charge"] == money("0.00"), quote
    assert quote["trip_insurance"] == money("94.60"), quote
    assert quote["total_with_insurance"] == money("1281.00"), quote
    assert quote["plan_id"] == "insurance-standard-band-3", quote

    print("reference: airports={a} areas={r} flights={f} fare_options={o} "
          "availability={v} connections={c} segments={s} insurance_plans={p} "
          "mobility_rules={m}".format(
              a=len(AIRPORTS), r=len(area_rows), f=len(flights), o=len(fare_rows),
              v=len(availability), c=len(itineraries), s=len(segments),
              p=len(plans), m=len(MOBILITY_RULES)))

    return "".join(out), {"flights": flights, "fares": fares, "pricer": pricer,
                          "availability": availability, "plans": plans}


# ---------------------------------------------------------------------------
# population
# ---------------------------------------------------------------------------

FIRST_NAMES = [
    "Linda", "Marcus", "Priya", "Andre", "Kelsey", "Rosa", "Tomas", "Nadia",
    "Grant", "Imani", "Victor", "Leah", "Owen", "Farah", "Desmond", "Yuki",
    "Camila", "Errol", "Bianca", "Hugo", "Sana", "Elise", "Rahul", "Noor",
    "Trevor", "Anya", "Jonah", "Celia", "Malik", "Renata", "Kwame", "Ingrid",
    "Silas", "Lorna", "Petra", "Devon", "Aiko", "Bram", "Colette", "Evan",
]

MIDDLE_NAMES = [
    "Marie", "Lee", "James", "Anne", "Rae", "John", "Elise", "Dean", "Faye",
    "Paul", "Nicole", "Grace", "Reid", "Blair", "Quinn",
]

# Carver recurs deliberately: a lookup on the caller's surname alone must not
# resolve to one row, and one of the Carvers holds a reservation on exactly the
# itinerary she is about to book.
LAST_NAMES = [
    "Carver", "Carver", "Carvalho", "Whitfield", "Okonkwo", "Delgado", "Novak",
    "Bergstrom", "Haddad", "Lindqvist", "Moreau", "Ferraro", "Nakamura",
    "Oyelaran", "Vasquez", "Kaur", "Brennan", "Sorensen", "Achebe", "Marchetti",
    "Kovacs", "Ilyin", "Santoro", "Abbasi", "Fontaine", "Reyes", "Thorne",
    "Mbeki", "Duarte", "Castellanos",
]

CARD_BRANDS = ["Visa", "Mastercard", "Amex", "Discover"]

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

# Customers the environment needs by name rather than by luck. Each one exists to
# make a specific lookup capable of failing.
DECLARED_CUSTOMERS = [
    # Same full name as the caller, one character away on the email address, and
    # a different date of birth. Resolving on the name or on a mistyped address
    # lands here.
    ("customer-linda-m-carver", "linda-m-carver", "Linda Marie Carver", "1971-11-02",
     "linda.carver@outlook.com", "4471", False),
    # Holds a confirmed reservation on the itinerary the caller is booking, so a
    # duplicate check has something to find.
    ("customer-marcus-carver", "marcus-carver", "Marcus Lee Carver", "1968-02-19",
     "marcus.carver4@outlook.com", "2210", False),
    # Profile carries a hold: name, date of birth, and email all match and the
    # verification still comes back needs_more_factors.
    ("customer-noor-abbasi", "noor-abbasi", "Noor Quinn Abbasi", "1983-09-30",
     "noor.abbasi@fastmail.com", "8890", True),
    # Owns an expired certificate and nothing else of interest.
    ("customer-devon-thorne", "devon-thorne", "Devon Rae Thorne", "1990-05-12",
     "devon.thorne@proton.me", "5512", False),
    # Owns a certificate that cannot cover a two-traveler itinerary on its own.
    ("customer-imani-okonkwo", "imani-okonkwo", "Imani Grace Okonkwo", "1977-12-03",
     "imani.okonkwo@gmail.com", "3067", False),
]


def build_population(reference: dict) -> str:
    rng = random.Random(RNG_SEED + 1)
    pricer: Pricer = reference["pricer"]
    flights = reference["flights"]
    by_id = {f[0]: f for f in flights}

    out = [
        "-- Customers, cards, certificates, prior verifications, cached searches\n"
        "-- and quotes, the record-locator pool, and the existing reservation\n"
        "-- estate. Generated by environment/gen_seed.py; do not edit by hand.\n"
        "-- The caller's own profile, certificate, quote, and searches are in\n"
        "-- 004_scenario.sql.\n\n"
        "BEGIN;\n\n"
    ]

    # -- customers -----------------------------------------------------------
    customers: list[tuple] = []
    used_emails = {"linda.carver9@outlook.com"}
    used_slugs = {"linda-carver"}
    for row in DECLARED_CUSTOMERS:
        customers.append(row + ("2024-03-14T09:00:00-07:00",))
        used_emails.add(row[4])
        used_slugs.add(row[1])

    for i in range(100):
        first = rng.choice(FIRST_NAMES)
        middle = rng.choice(MIDDLE_NAMES)
        last = rng.choice(LAST_NAMES)
        slug = f"{first.lower()}-{last.lower()}-{i:03d}"
        if slug in used_slugs:
            continue
        used_slugs.add(slug)
        email = f"{first.lower()}.{last.lower()}{i}@{rng.choice(['outlook.com', 'gmail.com', 'fastmail.com', 'proton.me', 'yahoo.com'])}"
        if email in used_emails:
            continue
        used_emails.add(email)
        dob = dt.date(rng.randint(1944, 2012), rng.randint(1, 12), rng.randint(1, 28))
        customers.append((
            f"customer-{slug}", slug, f"{first} {middle} {last}", dob.isoformat(),
            email, f"{rng.randint(1000, 9999)}", rng.random() < 0.04,
            f"202{rng.randint(0, 5)}-0{rng.randint(1, 9)}-1{rng.randint(0, 9)}T"
            f"{rng.randint(10, 19)}:00:00-07:00",
        ))
    out.append(insert("customers",
                      ["customer_id", "slug", "full_name", "date_of_birth", "email",
                       "phone_last4", "elevated_verification", "created_at"],
                      customers))

    customer_ids = [c[0] for c in customers]

    # -- cards ---------------------------------------------------------------
    cards: list[tuple] = []
    cards_by_customer: dict[str, list[tuple]] = {}
    for customer in customers:
        for k in range(rng.choice([1, 1, 2, 2, 3])):
            brand = rng.choice(CARD_BRANDS)
            last4 = f"{rng.randint(1000, 9999)}"
            token = f"{brand.lower()}-on-file-{last4}"
            if any(row[0] == token for row in cards):
                continue
            active = rng.random() > 0.1
            cards.append((token, customer[0], brand, last4, active,
                          "2025-06-02T11:20:00-07:00"))
            if active:
                cards_by_customer.setdefault(customer[0], []).append((token, brand, last4))
    out.append(insert("payment_methods",
                      ["token", "customer_id", "brand", "last4", "active", "added_at"],
                      cards))

    # -- certificates --------------------------------------------------------
    # Declared first: an expired one and one too small to cover the itinerary,
    # so a certificate lookup can return something other than a clean approval.
    certificates: list[tuple] = [
        ["certificate-CT-330014", "CT-330014", "customer-devon-thorne", "CT-***014",
         "expired", money("150.00"), money("150.00"), CURRENCY, "2026-03-31"],
        ["certificate-CT-771205", "CT-771205", "customer-imani-okonkwo", "CT-***205",
         "valid", money("120.00"), money("75.00"), CURRENCY, "2027-02-28"],
        # Same first six characters as the caller's certificate and owned by
        # somebody else, so a misheard code cannot quietly validate.
        ["certificate-CT-449103", "CT-449103", "customer-linda-m-carver", "CT-***103",
         "valid", money("250.00"), money("250.00"), CURRENCY, "2026-11-30"],
    ]
    used_codes = {row[1] for row in certificates} | {"CT-449108"}
    for i in range(60):
        owner = rng.choice(customer_ids)
        code = f"CT-{rng.randint(100000, 999999)}"
        if code in used_codes:
            continue
        used_codes.add(code)
        original = money(rng.choice([50, 75, 100, 125, 150, 200, 250, 300, 400]))
        state = rng.choices(["valid", "valid", "valid", "expired", "redeemed", "void"],
                            k=1)[0]
        balance = original if state == "valid" else (
            money("0.00") if state in ("redeemed", "void") else original)
        expires = None if rng.random() < 0.1 else (
            f"202{rng.choice([6, 6, 7])}-{rng.randint(1, 12):02d}-28")
        certificates.append([f"certificate-{code}", code, owner, f"CT-***{code[-3:]}",
                             state, original, balance, CURRENCY, expires])

    # -- prior verifications -------------------------------------------------
    # A cleared attempt is filed under the customer it cleared; an attempt that
    # cleared nobody gets an unresolved number, because a failed guess must not
    # be able to overwrite -- and so revoke -- a verification that succeeded.
    verifications: list[tuple] = []
    unresolved = 0
    for customer in rng.sample(customers, POPULATION_VERIFICATIONS):
        status = rng.choices(["verified", "verified", "verified", "failed"], k=1)[0]
        if status == "verified":
            verification_id = f"verification-{customer[1]}-booking"
        else:
            unresolved += 1
            verification_id = f"verification-unresolved-{unresolved:04d}"
        verifications.append((
            verification_id,
            customer[0] if status == "verified" else None,
            "booking", status,
            ["full_name", "date_of_birth", "email"] if status == "verified"
            else ["email"],
            "end_of_call",
            "2026-08-1{0}T{1}:0{2}:00-07:00".format(rng.randint(0, 9),
                                                    rng.randint(10, 19),
                                                    rng.randint(0, 9)),
        ))
    out.append(insert("identity_verifications",
                      ["verification_id", "customer_id", "purpose", "status",
                       "matched_factors", "expires_at", "created_at"], verifications))

    # -- cached searches -----------------------------------------------------
    searches: list[tuple] = []
    seen_search: set[tuple] = {
        ("PHX", "DCA", OUTBOUND_DATE, RETURN_DATE, "nonstop"),
        ("PHX", "DCA", OUTBOUND_DATE, RETURN_DATE, "one_stop"),
    }
    routes = sorted({(f[3], f[4]) for f in flights})
    index = 0
    while len(searches) < 30 and index < 400:
        index += 1
        origin, destination = rng.choice(routes)
        departure = rng.choice(AVAILABILITY_DATES[:-1])
        back = AVAILABILITY_DATES[-1]
        profile = rng.choice(["nonstop", "one_stop"])
        key = (origin, destination, departure, back, profile)
        if key in seen_search:
            continue
        seen_search.add(key)
        searches.append((f"search-{len(searches) + 1:05d}", origin, destination,
                         departure, back, profile,
                         f"2026-08-2{rng.randint(0, 5)}T{rng.randint(8, 19)}:"
                         f"{rng.randint(10, 59)}:{rng.randint(10, 59)}-07:00",
                         "quote_required_before_booking"))
    out.append(insert("flight_searches",
                      ["search_id", "origin_code", "destination_code", "departure_date",
                       "return_date", "stop_profile", "availability_checked_at",
                       "expires_at"], searches))

    # -- cached quotes -------------------------------------------------------
    # Priced from the same components as everything else, so a stored quote total
    # is never a number that the fare rows cannot reproduce.
    round_trips = []
    for (origin, destination) in routes:
        outbound_options = [f[0] for f in flights if f[3] == origin and f[4] == destination]
        return_options = [f[0] for f in flights if f[3] == destination and f[4] == origin]
        if outbound_options and return_options:
            round_trips.append((outbound_options[0], return_options[0]))

    quotes: list[tuple] = []
    seen_quote: set[tuple] = {
        ("2133fbc8-ed10-42aa-baa2-12e3d15a6a05", "3ac31d55-0dbc-4f79-892e-743257ec9f13", "standard_economy", 2, 2, 1, True),
    }
    attempts = 0
    while len(quotes) < 50 and attempts < 600:
        attempts += 1
        outbound, inbound = rng.choice(round_trips)
        fare_class = rng.choice(FARE_CLASSES)
        travelers = rng.choice([1, 1, 2, 2, 3, 4])
        bags = rng.choice([0, 0, 1, 2, 2, 3])
        devices = rng.choice([0, 0, 0, 1])
        insurance = rng.random() > 0.4
        key = (outbound, inbound, fare_class, travelers, bags, devices, insurance)
        if key in seen_quote:
            continue
        seen_quote.add(key)
        priced = pricer.quote(outbound, inbound, fare_class, travelers, bags, devices,
                              insurance)
        quotes.append((
            f"quote-{len(quotes) + 1:05d}", outbound, inbound,
            rng.choice(AVAILABILITY_DATES[:-1]), AVAILABILITY_DATES[-1], fare_class,
            travelers, bags, devices, insurance, priced["plan_id"],
            priced["fare_taxes_and_checked_bags"], priced["mobility_device_charge"],
            priced["trip_insurance"],
            priced["total_with_insurance"] if insurance else None,
            CURRENCY,
            f"2026-08-2{rng.randint(1, 6)}T{rng.randint(8, 19)}:{rng.randint(10, 59)}:00-07:00",
            f"2026-08-2{rng.randint(0, 5)}T{rng.randint(8, 19)}:{rng.randint(10, 59)}:00-07:00",
        ))
    out.append(insert("fare_quotes",
                      ["quote_id", "outbound_flight_id", "return_flight_id",
                       "departure_date", "return_date", "fare_class", "traveler_count",
                       "checked_bag_count", "mobility_device_count", "include_insurance",
                       "insurance_plan_id", "fare_taxes_and_checked_bags",
                       "mobility_device_charge", "trip_insurance",
                       "total_with_insurance", "currency", "expires_at",
                       "last_priced_at"], quotes))

    # -- record locators -----------------------------------------------------
    # The leading sequences are spent by the existing estate, the next one is
    # left for 004_scenario.sql to seed with the code the recording issues, and
    # the rest are unissued spares, so a second booking in a run gets a
    # different code rather than colliding with the first.
    codes: list[str] = []
    seen_codes = {"B9RT6M"}
    while len(codes) < POPULATION_RESERVATIONS:
        code = "".join(rng.choice(CODE_ALPHABET) for _ in range(6))
        if code in seen_codes:
            continue
        seen_codes.add(code)
        codes.append(code)
    spares: list[str] = []
    while len(spares) < 49:
        code = "".join(rng.choice(CODE_ALPHABET) for _ in range(6))
        if code in seen_codes:
            continue
        seen_codes.add(code)
        spares.append(code)

    pool_rows = [(i + 1, code, "2026-0{0}-1{1}T{2}:00:00-07:00".format(
        rng.randint(1, 8), rng.randint(0, 9), rng.randint(10, 19)))
        for i, code in enumerate(codes)]
    pool_rows += [(SCENARIO_POOL_SEQ + 1 + i, code, None)
                  for i, code in enumerate(spares)]
    out.append(insert("confirmation_code_pool", ["pool_seq", "code", "issued_at"],
                      pool_rows))

    # -- reservations --------------------------------------------------------
    certificate_state = {row[0]: row for row in certificates}
    certificates_by_customer: dict[str, list[str]] = {}
    for row in certificates:
        if row[4] == "valid" and row[6] > 0:
            certificates_by_customer.setdefault(row[2], []).append(row[0])

    availability_dates = AVAILABILITY_DATES
    # Mostly flown-and-paid, with a tail of reservations still short of capture
    # and a few that never left the shopping stage, so the lifecycle column is
    # exercised rather than constant.
    status_mix = [
        ("confirmed", "ticketed", "captured"),
        ("confirmed", "ticketed", "captured"),
        ("confirmed", "ticketed", "captured"),
        ("confirmed", "ticketed", "captured"),
        ("confirmed", "pending", "authorized"),
        ("pending_payment", "pending", "authorized"),
        ("quoted", "pending", None),
        ("draft", "pending", None),
    ]

    reservations, travelers_rows, device_rows = [], [], []
    allocation_rows, redemption_rows = [], []
    # The declared duplicate: Marcus Carver already holds the caller's itinerary.
    forced = [("customer-marcus-carver", "2133fbc8-ed10-42aa-baa2-12e3d15a6a05", "3ac31d55-0dbc-4f79-892e-743257ec9f13",
               OUTBOUND_DATE, RETURN_DATE, "standard_economy")]

    for i in range(POPULATION_RESERVATIONS):
        code = codes[i]
        reservation_id = f"reservation-{code}"
        if i < len(forced):
            customer_id, outbound, inbound, departure, back, fare_class = forced[i]
            status, ticketing, payment = "confirmed", "ticketed", "captured"
        else:
            customer_id = rng.choice(customer_ids)
            outbound, inbound = rng.choice(round_trips)
            departure = rng.choice(availability_dates[:-1])
            back = availability_dates[-1]
            fare_class = rng.choice(FARE_CLASSES)
            status, ticketing, payment = rng.choice(status_mix)

        traveler_count = rng.choice([1, 1, 2, 2, 2, 3])
        bags = rng.choice([0, 1, 2, 2, 3])
        insurance = rng.random() > 0.55
        total, plan_id, premium = pricer.charged_total(outbound, inbound, fare_class,
                                                       traveler_count, bags, 0, insurance)
        customer = next(c for c in customers if c[0] == customer_id)
        reservations.append((
            reservation_id, code, customer_id, None, outbound, inbound, departure, back,
            customer[4], fare_class, bags, status, ticketing, payment,
            fare_class == "standard_economy", insurance, plan_id, premium, total,
            CURRENCY,
            f"2026-0{rng.randint(1, 8)}-1{rng.randint(0, 9)}T{rng.randint(9, 19)}:"
            f"{rng.randint(10, 59)}:00-07:00",
        ))

        for index in range(1, traveler_count + 1):
            if index == 1:
                full_name = customer[2]
                dob = customer[3]
            else:
                full_name = (f"{rng.choice(FIRST_NAMES)} {rng.choice(MIDDLE_NAMES)} "
                             f"{customer[2].split()[-1]}")
                dob = dt.date(rng.randint(1948, 2016), rng.randint(1, 12),
                              rng.randint(1, 28)).isoformat()
            parts = full_name.lower().split()
            travelers_rows.append((
                reservation_id, f"traveler-{parts[0]}-{parts[-1]}-{i:04d}-{index}",
                full_name, dob, index))

        if rng.random() < 0.12:
            device_rows.append((f"{reservation_id}-device-1", reservation_id, 1,
                                rng.choice(["folding walker", "manual wheelchair",
                                            "walking cane", "rollator"]),
                                money("0.00"), False, False, "2026-07-01"))

        # Tenders. A reservation with no payment position yet has no allocation.
        if payment is None:
            continue
        remaining = total
        allocation_index = 0
        owned = certificates_by_customer.get(customer_id, [])
        if owned and rng.random() < 0.45:
            certificate_id = owned[0]
            row = certificate_state[certificate_id]
            applied = min(row[6], money(total * Decimal("0.30")))
            if applied > 0:
                allocation_index += 1
                allocation_rows.append((
                    f"{reservation_id}-tender-{allocation_index}", reservation_id,
                    allocation_index, f"travel_certificate_{row[1]}",
                    "travel_certificate", applied, CURRENCY))
                redemption_rows.append((
                    f"{reservation_id}-redemption-1", certificate_id, reservation_id,
                    applied, CURRENCY, "2026-07-19T14:05:00-07:00"))
                row[6] = money(row[6] - applied)
                if row[6] == 0:
                    row[4] = "redeemed"
                    certificates_by_customer[customer_id] = owned[1:]
                remaining = money(remaining - applied)
        held = cards_by_customer.get(customer_id) or [("visa-on-file-0000", "Visa", "0000")]
        # A customer holding two active cards sometimes splits the remainder
        # across both, so a tender list is not always one certificate and one
        # card. The shares are complements, so they still sum to what was charged.
        if len(held) > 2 and rng.random() < 0.3:
            first = money(remaining * Decimal("0.4"))
            second = money(remaining * Decimal("0.35"))
            shares = [first, second, money(remaining - first - second)]
        elif len(held) > 1 and rng.random() < 0.55:
            first = money(remaining * Decimal("0.5"))
            shares = [first, money(remaining - first)]
        else:
            shares = [remaining]
        for card, share in zip(held, shares):
            allocation_index += 1
            allocation_rows.append((
                f"{reservation_id}-tender-{allocation_index}", reservation_id,
                allocation_index, f"{card[1].lower()}_ending_{card[2]}", "card",
                share, CURRENCY))

    # Certificates are written after the estate has drawn them down, so a
    # balance in the seed is the balance the ledger below it explains.
    out.append(insert("travel_certificates",
                      ["certificate_id", "code", "customer_id", "masked_code", "status",
                       "original_amount", "available_balance", "currency", "expires_at"],
                      [tuple(row) for row in certificates]))
    out.append(insert("reservations",
                      ["reservation_id", "confirmation_code", "customer_id", "quote_id",
                       "outbound_flight_id", "return_flight_id", "departure_date",
                       "return_date", "contact_email", "fare_class", "checked_bag_count",
                       "status", "ticketing_status", "payment_status",
                       "seat_selection_available", "insurance_included",
                       "insurance_plan_id", "insurance_price", "charged_total",
                       "currency", "created_at"], reservations))
    out.append(insert("travelers",
                      ["reservation_id", "traveler_id", "full_name", "date_of_birth",
                       "traveler_index"], travelers_rows))
    out.append(insert("reservation_mobility_devices",
                      ["device_entry_id", "reservation_id", "device_index",
                       "device_type", "fee", "counts_as_paid_bag",
                       "serial_number_required", "rule_effective_at"], device_rows))
    out.append(insert("payment_allocations",
                      ["allocation_id", "reservation_id", "allocation_index", "tender",
                       "tender_kind", "amount", "currency"], allocation_rows))
    out.append(insert("certificate_redemptions",
                      ["redemption_id", "certificate_id", "reservation_id", "amount",
                       "currency", "redeemed_at"], redemption_rows))

    out.append("COMMIT;\n")

    print("population: customers={c} cards={p} certificates={t} verifications={v} "
          "searches={s} quotes={q} pool={l} reservations={r} travelers={tr} "
          "devices={d} allocations={a} redemptions={rd}".format(
              c=len(customers), p=len(cards), t=len(certificates),
              v=len(verifications), s=len(searches), q=len(quotes), l=len(pool_rows),
              r=len(reservations), tr=len(travelers_rows), d=len(device_rows),
              a=len(allocation_rows), rd=len(redemption_rows)))
    return "".join(out)


def main() -> None:
    os.makedirs(SQL, exist_ok=True)
    reference, context = build_reference()
    population = build_population(context)
    with open(os.path.join(SQL, "002_reference.sql"), "w") as fh:
        fh.write(reference)
    with open(os.path.join(SQL, "003_population.sql"), "w") as fh:
        fh.write(population)
    print(f"wrote 002_reference.sql ({len(reference)} bytes) and "
          f"003_population.sql ({len(population)} bytes)")


if __name__ == "__main__":
    main()
