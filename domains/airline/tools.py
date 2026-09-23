"""Airline tools over the conversation's JSON database.

Ported from the PostgreSQL tool server in PR #17. Each tool is (db, args) -> result
and may mutate db in place; see env/toolkit.py for the row layout and helpers.

Handlers hold domain logic only. Every identifier, schedule, price, premium,
certificate balance, and record locator that appears in a result is read or
computed from the database, never generated at random or from wall time, so a
result is reproducible and an operator can explain any figure by pointing at the
rows it was summed from.

Money is the load-bearing part of this domain. No total is stored: a fare family
price is the sum over both directions of base fare plus tax, paid bags are the
bag tariff times the bag count, insurance is a per-traveler premium from the band
the fare lands in, and a mobility device charge is the accessibility tariff times
the device count. The same arithmetic runs again at booking, so a reservation
cannot be charged a total the fare rows can no longer reproduce.

The database stores NUMERIC columns as JSON numbers. Arithmetic runs on Decimal
built from each number's shortest decimal form, as the PostgreSQL backend's
NUMERIC arithmetic did, and amounts are written back as numbers.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from toolkit import (NotFound, Refusal, ToolError, allocate_id, as_float, as_int,
                     compact, first, insert, rows, scenario_id, scenario_value)

# Key columns per table: a row's key in db[table] is these columns joined by "|".
KEY_COLUMNS = {
    "airport_area_links": [
        "area_id",
        "airport_code"
    ],
    "airports": [
        "code"
    ],
    "baggage_fees": [
        "fare_class"
    ],
    "certificate_redemptions": [
        "redemption_id"
    ],
    "confirmation_code_pool": [
        "pool_seq"
    ],
    "connecting_itineraries": [
        "itinerary_id"
    ],
    "connecting_itinerary_segments": [
        "itinerary_id",
        "direction",
        "segment_index"
    ],
    "customers": [
        "customer_id"
    ],
    "destination_areas": [
        "area_id"
    ],
    "fare_options": [
        "flight_id",
        "fare_class"
    ],
    "fare_quotes": [
        "quote_id"
    ],
    "flight_availability": [
        "flight_id",
        "departure_date",
        "fare_class"
    ],
    "flight_searches": [
        "search_id"
    ],
    "flights": [
        "flight_id"
    ],
    "id_allocator": [
        "entity_type",
        "scope"
    ],
    "identity_verifications": [
        "verification_id"
    ],
    "insurance_plans": [
        "plan_id"
    ],
    "mobility_device_rules": [
        "device_type",
        "effective_at"
    ],
    "payment_allocations": [
        "allocation_id"
    ],
    "payment_methods": [
        "token"
    ],
    "reservation_mobility_devices": [
        "device_entry_id"
    ],
    "reservations": [
        "reservation_id"
    ],
    "scenario": [
        "key"
    ],
    "specialist_transfers": [
        "transfer_id"
    ],
    "travel_certificates": [
        "certificate_id"
    ],
    "travelers": [
        "reservation_id",
        "traveler_index"
    ]
}

# Reservation states that occupy a seat and count against a duplicate check.
ACTIVE_RESERVATION_STATUSES = ["pending_payment", "confirmed", "ticketed"]

# Identity factors this verification tier collects, in the order the registry
# declares them.
IDENTITY_FACTORS = ["full_name", "date_of_birth", "email"]

ZERO = Decimal("0.00")


class DatabaseError(ToolError):
    """A constraint the PostgreSQL schema enforces was violated.

    The original backend let the database refuse the statement and reported the
    driver's message as a database_error, so the port does the same with the
    same text.
    """
    status = 500
    kind = "database_error"


def _num(value) -> Decimal:
    """A NUMERIC column as Decimal, from the number's shortest decimal form."""
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _cents(value) -> Decimal:
    return _num(value).quantize(Decimal("0.01"))


def _date(value: str) -> str:
    """A date argument as a DATE column holds it, so it compares equal to one."""
    return date.fromisoformat(value).isoformat()


def _order(candidates, *keys):
    """Sort like ORDER BY: `keys` are (key function, descending) pairs, most
    significant first. Python's sort is stable, so sorting on the least
    significant key first and the most significant last gives the same order."""
    ordered = list(candidates)
    for key, descending in reversed(keys):
        ordered.sort(key=key, reverse=descending)
    return ordered


def _slug(full_name: str) -> str:
    """First and last name, lowercased and hyphenated.

    Traveler identifiers in the recorded result are the caller's names in this
    form. Deriving them keeps a booking reproducible without a random suffix, and
    the traveler table is keyed per reservation so two reservations naming the
    same person do not collide.
    """
    cleaned = "".join(character if character.isalnum() or character.isspace() else " "
                      for character in full_name.lower())
    parts = cleaned.split()
    if not parts:
        return "traveler"
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]}-{parts[-1]}"


# ---------------------------------------------------------------------------
# shared lookups
# ---------------------------------------------------------------------------


def _airport(db, code: str) -> dict:
    row = first(db, "airports", code=code, served=True)
    if row is None:
        raise NotFound(f"unknown or unserved airport {code!r}")
    return {"code": row["code"], "name": row["name"]}


def _flight(db, flight_id: str) -> dict:
    row = first(db, "flights", flight_id=flight_id)
    if row is None:
        raise NotFound(f"unknown flight {flight_id!r}")
    return row


def _device_rule(db, device_type: str) -> dict:
    """Accessibility tariff in force for a device type at the scenario clock.

    Matching is exact on the canonical name, then on the rule's aliases, then on
    a two-way substring so "walker" reaches the folding-walker rule and "folding
    walker with seat" does too. A device type no rule names falls back to the
    unspecified-device line rather than being refused, because the airline has a
    tariff position on any device a caller might bring.
    """
    scenario_date = (scenario_value(db, "scenario_time") or "")[:10]
    needle = device_type.strip().lower()

    # An exact name beats an alias, which beats a substring, so a caller who says
    # "folding walker with a seat" gets the folding-walker line rather than
    # whichever rule happens to sort first. Every version in effect is a
    # candidate, so an alias an older version carried still reaches the rule.
    def rank(rule) -> int | None:
        name = rule["device_type"].lower()
        if name == needle:
            return 0
        if needle in rule["aliases"]:
            return 1
        if name in needle:
            return 2
        if any(alias in needle for alias in rule["aliases"]):
            return 3
        return None

    named = _order(
        (rule for rule in db["mobility_device_rules"].values()
         if rule["effective_at"] <= scenario_date and rank(rule) is not None),
        (rank, False),
        (lambda rule: len(rule["device_type"]), True),
        (lambda rule: rule["device_type"], False),
    )
    if not named:
        return _quote_device_rule(db)
    # The newest version of the named rule in effect at the scenario clock.
    versions = [rule for rule in rows(db, "mobility_device_rules",
                                      device_type=named[0]["device_type"])
                if rule["effective_at"] <= scenario_date]
    return max(versions, key=lambda rule: rule["effective_at"])


def _quote_device_rule(db) -> dict:
    """The tariff line a quote uses when no device type has been stated yet."""
    scenario_date = (scenario_value(db, "scenario_time") or "")[:10]
    defaults = _order(
        (rule for rule in rows(db, "mobility_device_rules", is_quote_default=True)
         if rule["effective_at"] <= scenario_date),
        (lambda rule: rule["effective_at"], True),
        (lambda rule: rule["device_type"], False),
    )
    if not defaults:
        raise NotFound("no default mobility-device tariff is in effect")
    return defaults[0]


def _leg_price(db, flight_id: str, fare_class: str) -> Decimal:
    row = first(db, "fare_options", flight_id=flight_id, fare_class=fare_class)
    if row is None:
        raise NotFound(f"fare class {fare_class!r} is not offered on {flight_id!r}")
    return _cents(_num(row["base_fare"]) + _num(row["tax_amount"]))


def _bag_fee(db, fare_class: str) -> Decimal:
    row = first(db, "baggage_fees", fare_class=fare_class)
    if row is None or row["per_bag_amount"] is None:
        raise NotFound(f"no baggage tariff for fare class {fare_class!r}")
    return _cents(row["per_bag_amount"])


def _insurance_plan(db, per_traveler: Decimal) -> dict:
    """The current plan for a fare, chosen by the trip-cost band it falls in.

    Bands partition the range and only the active standard tier is offered at the
    reservation desk, so exactly one plan applies to any fare. The premium is per
    traveler, which is why two travelers on a 558.20 fare come to 94.60 rather
    than to a number stored against the itinerary.
    """
    bands = _order(
        (plan for plan in rows(db, "insurance_plans", active=True, tier="standard")
         if _num(plan["min_trip_cost"]) < per_traveler <= _num(plan["max_trip_cost"])),
        (lambda plan: plan["plan_id"], False),
    )
    if not bands:
        raise NotFound(f"no active insurance band covers a fare of {per_traveler}")
    return bands[0]


def _price_itinerary(db, outbound_id: str, return_id: str, fare_class: str,
                     traveler_count: int, checked_bag_count: int,
                     device_fees: list[Decimal], include_insurance: bool) -> dict:
    """Compute a quote from the fare, tariff, and premium rows.

    `device_fees` is one amount per recorded device. A pricing call has only a
    device count and uses the unspecified-device line for each; a booking knows
    the device types and uses each device's own line, so a device that carries a
    fee is charged for at booking even though the quote could not know it would.
    """
    per_traveler = _cents(_leg_price(db, outbound_id, fare_class)
                          + _leg_price(db, return_id, fare_class))
    fare_taxes_and_bags = _cents(per_traveler * traveler_count
                                 + _bag_fee(db, fare_class) * checked_bag_count)
    device_charge = _cents(sum(device_fees, ZERO))
    priced = {
        "per_traveler": per_traveler,
        "fare_taxes_and_checked_bags": fare_taxes_and_bags,
        "mobility_device_charge": device_charge,
        "trip_insurance": None,
        "plan": None,
        "total": _cents(fare_taxes_and_bags + device_charge),
    }
    if include_insurance:
        plan = _insurance_plan(db, per_traveler)
        premium = _cents(_num(plan["price_per_traveler"]) * traveler_count)
        priced["trip_insurance"] = premium
        priced["plan"] = plan
        priced["total"] = _cents(fare_taxes_and_bags + device_charge + premium)
    return priced


def _current_quote(db) -> dict | None:
    """The most recently priced quote: the desk's current pricing context.

    A certificate validation has to know what amount it is being applied to and
    carries no itinerary in its arguments, so it reads the quote that pricing
    last touched. `last_priced_at` is a text column, so "most recent" is the
    greatest string, with the greatest quote_id breaking a tie.
    """
    priced = [quote for quote in db["fare_quotes"].values()
              if quote["last_priced_at"] is not None]
    if not priced:
        return None
    return max(priced, key=lambda quote: (quote["last_priced_at"], quote["quote_id"]))


def _quote_payable(quote: dict) -> Decimal:
    if quote.get("total_with_insurance") is not None:
        return _cents(quote["total_with_insurance"])
    fare = _num(quote.get("fare_taxes_and_checked_bags") or ZERO)
    devices = _num(quote.get("mobility_device_charge") or ZERO)
    return _cents(fare + devices)


def _cleared_verification(db, verification_id: str, customer_id: str) -> dict:
    """A verification record that authorizes account access for this customer.

    The policy makes a self-stated name, date of birth, or email insufficient on
    its own, so account reads and the booking take a verification identifier and
    it is checked here rather than trusted.
    """
    row = first(db, "identity_verifications", verification_id=verification_id)
    if row is None:
        raise Refusal(f"unknown verification record {verification_id!r}")
    if row["status"] != "verified" or row["customer_id"] != customer_id:
        raise Refusal(
            "verification record does not clear account access for this customer",
            {"verification_status": row["status"]})
    return row


def _customer_by_email(db, email: str) -> dict | None:
    # Case-insensitive, and the lowest customer_id wins if two profiles share an
    # address.
    needle = email.strip().lower()
    matches = [customer for customer in db["customers"].values()
               if customer["email"].lower() == needle]
    return min(matches, key=lambda customer: customer["customer_id"]) if matches else None


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


def list_supported_airports(db, args) -> dict:
    query = args["destination_area"].strip().lower()
    # A landmark resolves ahead of the district or metro that contains it,
    # because a caller who names the National Mall is asking about the Mall and
    # not about Washington generally. Within a kind, the longest matching phrase
    # wins, so a two-word city name is not beaten by a one-word substring of it.
    matched = []
    for area in db["destination_areas"].values():
        lengths = [len(term) for term in area["search_terms"] if term in query]
        if lengths:
            matched.append((area, max(lengths)))
    if not matched:
        raise NotFound(
            f"no supported airports are catalogued for destination area {args['destination_area']!r}")
    kind_rank = {"landmark": 0, "district": 1}
    area, _ = _order(
        matched,
        (lambda pair: kind_rank.get(pair[0]["area_kind"], 2), False),
        (lambda pair: pair[1], True),
        (lambda pair: pair[0]["area_id"], False),
    )[0]

    linked = []
    for link in rows(db, "airport_area_links", area_id=area["area_id"]):
        airport = first(db, "airports", code=link["airport_code"], served=True)
        if airport is not None:
            linked.append({
                "code": airport["code"],
                "name": airport["name"],
                "distance_miles": as_float(link["distance_miles"]),
                "ground_access_minutes": as_int(link["ground_access_minutes"]),
            })
    if not linked:
        raise NotFound(f"destination area {area['area_id']!r} has no served airports")

    # Nearest first is a listing order, not a recommendation: the caller gets the
    # distance and ground travel time for every airport and weighs them.
    return {
        "destination_area": area["display_name"],
        "airports": _order(linked, (lambda row: row["distance_miles"], False),
                           (lambda row: row["code"], False)),
    }


def _sellable_fares(db, flight_id: str, departure_date: str, traveler_count: int) -> dict:
    """Fare families with room for the party on this flight and date."""
    sellable = {}
    for option in rows(db, "fare_options", flight_id=flight_id):
        seats = first(db, "flight_availability", flight_id=flight_id,
                      departure_date=departure_date, fare_class=option["fare_class"])
        if seats is not None and seats["seats_remaining"] >= traveler_count:
            sellable[option["fare_class"]] = {
                "fare_class": option["fare_class"],
                "leg_price": _num(option["base_fare"]) + _num(option["tax_amount"]),
                "advance_seat_selection_allowed": option["advance_seat_selection_allowed"],
                "seats_remaining": seats["seats_remaining"],
            }
    return sellable


def _fare_option_views(db, priced: list[tuple]) -> list[dict]:
    """Fare families as (price per traveler, fare class, advance seat selection)
    tuples, rendered cheapest first with the scenario's currency."""
    currency = scenario_value(db, "currency")
    return [
        {
            "fare_class": fare_class,
            "price_per_traveler": as_float(price),
            "currency": currency,
            "advance_seat_selection_allowed": seats,
        }
        for price, fare_class, seats in sorted(priced)
    ]


def _direct_flights(db, origin: str, destination: str, departure_date: str,
                    travel_day: str, traveler_count: int, max_stops: int) -> list[dict]:
    """Every flight on a route and date within the stop limit with room for the
    party, ordered by departure time, each with the fare families it can sell.

    Prices are one way, per traveler, for that flight. No flight is picked over
    another; the caller has the times and prices to choose.
    """
    found = []
    flights = _order(rows(db, "flights", origin_code=origin, destination_code=destination),
                     (lambda row: row["departure_time"], False),
                     (lambda row: row["flight_id"], False))
    for flight in flights:
        if flight["stops"] > max_stops:
            continue
        sellable = _sellable_fares(db, flight["flight_id"], travel_day, traveler_count)
        if not sellable:
            continue
        priced = [(_cents(fare["leg_price"]), fare["fare_class"],
                   fare["advance_seat_selection_allowed"])
                  for fare in sellable.values()]
        found.append({
            **_flight_view(db, flight, departure_date),
            "fare_options": _fare_option_views(db, priced),
        })
    return found


def _connection_legs(db, itinerary_id: str) -> dict:
    legs: dict = {"outbound": [], "return": []}
    segments = _order(rows(db, "connecting_itinerary_segments", itinerary_id=itinerary_id),
                      (lambda row: row["direction"], False),
                      (lambda row: row["segment_index"], False))
    for segment in segments:
        flight = first(db, "flights", flight_id=segment["flight_id"])
        if flight is None:
            continue
        legs[segment["direction"]].append({"segment": segment, "flight": flight})
    return legs


def _connections(db, origin: str, destination: str, departure_date: str,
                 return_date: str, traveler_count: int, max_stops: int,
                 max_layover_minutes) -> list[dict]:
    """Every offered connecting itinerary on the route and dates with room for the
    party, within the stop and layover limits.

    Each is returned with its legs, its elapsed time per direction, and the price
    per traveler of every fare family sellable on all of its segments. Nothing is
    compared against the nonstop option or against another connection; the
    caller has the figures to do that.
    """
    candidates = _order(
        rows(db, "connecting_itineraries", origin_code=origin, destination_code=destination,
             departure_date=departure_date, return_date=return_date, offered=True),
        (lambda row: row["itinerary_id"], False),
    )
    dates = {"outbound": departure_date, "return": return_date}

    found = []
    for candidate in candidates:
        legs = _connection_legs(db, candidate["itinerary_id"])
        if not legs["outbound"] or not legs["return"]:
            continue
        if max(len(legs["outbound"]), len(legs["return"])) - 1 > max_stops:
            continue
        layovers = [leg["segment"]["layover_after_minutes"] for direction in legs.values()
                    for leg in direction[:-1]]
        if max_layover_minutes is not None and layovers and max(layovers) > max_layover_minutes:
            continue

        # A fare family is only sellable on the connection when every segment has
        # room for the party in it.
        sellable: set[str] | None = None
        for direction, travel_date in dates.items():
            for leg in legs[direction]:
                classes = set(_sellable_fares(db, leg["flight"]["flight_id"], travel_date,
                                              traveler_count))
                sellable = classes if sellable is None else sellable & classes
        if not sellable:
            continue

        flight_ids = [leg["flight"]["flight_id"] for direction in legs.values()
                      for leg in direction]
        priced = []
        for fare_class in sellable:
            price = _cents(sum((_leg_price(db, flight_id, fare_class)
                                for flight_id in flight_ids), ZERO))
            seats = all(first(db, "fare_options", flight_id=flight_id,
                              fare_class=fare_class)["advance_seat_selection_allowed"]
                        for flight_id in flight_ids)
            priced.append((price, fare_class, seats))

        itinerary: dict = {"itinerary_id": candidate["itinerary_id"]}
        for direction, travel_date in dates.items():
            itinerary[direction] = {
                # Elapsed time from the first departure to the last arrival:
                # block time plus the connection time on the ground.
                "total_duration_minutes": as_int(sum(
                    leg["flight"]["duration_minutes"] + leg["segment"]["layover_after_minutes"]
                    for leg in legs[direction])),
                "legs": [
                    {
                        "segment_index": as_int(leg["segment"]["segment_index"]),
                        **_flight_view(db, leg["flight"], travel_date),
                        "layover_after_minutes": as_int(leg["segment"]["layover_after_minutes"]),
                    }
                    for leg in legs[direction]
                ],
            }
        itinerary["fare_options"] = _fare_option_views(db, priced)
        found.append(itinerary)
    return found


def search_flights(db, args) -> dict:
    origin = _airport(db, args["origin_airport"])
    destination = _airport(db, args["destination_airport"])
    # The dates as stated are echoed back; the dates as a DATE column holds them
    # are what rows are matched and filed on.
    departure_date = args["departure_date"]
    return_date = args["return_date"]
    departure_day, return_day = _date(departure_date), _date(return_date)
    traveler_count = args["traveler_count"]
    max_stops = args["max_stops"]
    max_layover = args.get("max_layover_minutes")

    # A search that allows a stop is asking about connections: every qualifying
    # connecting itinerary is listed. With none, the search falls back to the
    # direct option set, which is also what a nonstop-only search returns.
    connections = []
    if max_stops >= 1:
        connections = _connections(db, origin["code"], destination["code"],
                                   departure_day, return_day, traveler_count,
                                   max_stops, max_layover)
    profile = "one_stop" if connections else "nonstop"

    search = first(db, "flight_searches", origin_code=origin["code"],
                   destination_code=destination["code"], departure_date=departure_day,
                   return_date=return_day, stop_profile=profile)
    if search is None:
        # A route and date pair nobody has checked before is recorded now, with
        # an allocated identifier, so the search a caller just ran is as real as
        # the ones already in the cache.
        search = insert(db, "flight_searches", {
            "search_id": allocate_id(db, "flight_search"),
            "origin_code": origin["code"],
            "destination_code": destination["code"],
            "departure_date": departure_day,
            "return_date": return_day,
            "stop_profile": profile,
            "availability_checked_at": scenario_value(db, "scenario_time"),
            "expires_at": scenario_value(db, "search_expiry"),
        }, KEY_COLUMNS)

    result: dict = {"search_id": search["search_id"]}

    if connections:
        result["connections"] = connections
    else:
        result["outbound"] = _direct_flights(db, origin["code"], destination["code"],
                                             departure_date, departure_day,
                                             traveler_count, max_stops)
        result["return"] = _direct_flights(db, destination["code"], origin["code"],
                                           return_date, return_day,
                                           traveler_count, max_stops)

    result["availability_checked_at"] = search["availability_checked_at"]
    result["expires_at"] = search["expires_at"]
    return result


def _flight_view(db, flight: dict, departure_date: str) -> dict:
    return {
        "flight_id": flight["flight_id"],
        "origin": _airport(db, flight["origin_code"]),
        "destination": _airport(db, flight["destination_code"]),
        "departure_date": departure_date,
        "departure_time": flight["departure_time"],
        "arrival_date": (date.fromisoformat(departure_date) + timedelta(days=1)).isoformat()
        if flight["arrives_next_day"] else departure_date,
        "arrival_time": flight["arrival_time"],
        "duration_minutes": as_int(flight["duration_minutes"]),
        "stops": as_int(flight["stops"]),
    }


def _resolve_itinerary_dates(db, outbound: dict, inbound: dict) -> tuple:
    """Dates for a quote raised without them.

    A pricing call names flights, not dates, so the dates come from the most
    recent availability check on the same route, and failing that from the next
    seeded departure. A quote that already exists carries its own dates.
    """
    searches = rows(db, "flight_searches", origin_code=outbound["origin_code"],
                    destination_code=outbound["destination_code"])
    if searches:
        search = max(searches, key=lambda row: (row["availability_checked_at"],
                                                row["search_id"]))
        return search["departure_date"], search["return_date"]

    scenario_date = (scenario_value(db, "scenario_time") or "")[:10]
    departures = [row["departure_date"]
                  for row in rows(db, "flight_availability", flight_id=outbound["flight_id"])
                  if row["departure_date"] >= scenario_date]
    if not departures:
        raise NotFound(f"flight {outbound['flight_id']!r} has no seeded departures")
    departure = min(departures)
    backs = [row["departure_date"]
             for row in rows(db, "flight_availability", flight_id=inbound["flight_id"])
             if row["departure_date"] >= departure]
    return departure, min(backs) if backs else departure


def calculate_itinerary_price(db, args) -> dict:
    outbound = _flight(db, args["outbound_flight_id"])
    inbound = _flight(db, args["return_flight_id"])
    fare_class = args["fare_class"]
    traveler_count = args["traveler_count"]
    bags = args["checked_bag_count"]
    devices = args["mobility_device_count"]
    include_insurance = args["include_insurance_quote"]

    # A pricing call has a device count and no device types, so each device is
    # priced on the unspecified-device line of the accessibility tariff.
    device_fee = _cents(_quote_device_rule(db)["fee"])
    priced = _price_itinerary(db, outbound["flight_id"], inbound["flight_id"],
                              fare_class, traveler_count, bags,
                              [device_fee] * devices, include_insurance)

    quote = first(db, "fare_quotes", outbound_flight_id=outbound["flight_id"],
                  return_flight_id=inbound["flight_id"], fare_class=fare_class,
                  traveler_count=traveler_count, checked_bag_count=bags,
                  mobility_device_count=devices, include_insurance=include_insurance)
    priced_at = scenario_value(db, "scenario_time")
    if quote is None:
        quote_id = allocate_id(db, "fare_quote")
        departure_date, return_date = _resolve_itinerary_dates(db, outbound, inbound)
        # A fresh quote holds for the validity window the airline publishes,
        # measured from the scenario clock rather than from wall time.
        hours = int(scenario_value(db, "quote_validity_hours") or 24)
        quote = insert(db, "fare_quotes", {
            "quote_id": quote_id,
            "outbound_flight_id": outbound["flight_id"],
            "return_flight_id": inbound["flight_id"],
            "departure_date": departure_date,
            "return_date": return_date,
            "fare_class": fare_class,
            "traveler_count": traveler_count,
            "checked_bag_count": bags,
            "mobility_device_count": devices,
            "include_insurance": include_insurance,
            "insurance_plan_id": None,
            "fare_taxes_and_checked_bags": None,
            "mobility_device_charge": None,
            "trip_insurance": None,
            "total_with_insurance": None,
            "currency": scenario_value(db, "currency"),
            "expires_at": _shift_hours(priced_at, hours),
            "last_priced_at": None,
        }, KEY_COLUMNS)

    # The computed amounts are written back so a later booking can be held to the
    # figure the customer authorized, and so the desk's current pricing context
    # is a row rather than a memory.
    quote.update({
        "fare_taxes_and_checked_bags": as_float(priced["fare_taxes_and_checked_bags"]),
        "mobility_device_charge": as_float(priced["mobility_device_charge"]),
        "trip_insurance": as_float(priced["trip_insurance"]),
        "total_with_insurance": as_float(priced["total"]) if include_insurance else None,
        "insurance_plan_id": priced["plan"]["plan_id"] if priced["plan"] else None,
        "last_priced_at": priced_at,
    })

    return compact([
        ("quote_id", quote["quote_id"]),
        ("fare_taxes_and_checked_bags", as_float(priced["fare_taxes_and_checked_bags"])),
        ("mobility_device_charge", as_float(priced["mobility_device_charge"])),
        ("trip_insurance", as_float(priced["trip_insurance"])),
        ("insurance_plan_document",
         priced["plan"]["document_reference"] if priced["plan"] else None),
        ("total_with_insurance",
         as_float(priced["total"]) if include_insurance else None),
        ("currency", scenario_value(db, "currency")),
        ("expires_at", quote["expires_at"]),
    ])


def _shift_hours(timestamp: str, hours: int) -> str:
    """Advance an ISO timestamp that carries an offset, keeping the offset."""
    return (datetime.fromisoformat(timestamp) + timedelta(hours=hours)).isoformat()


def check_mobility_device_requirements(db, args) -> dict:
    rule = _device_rule(db, args["device_type"])
    return {
        # The canonical name of the rule that applies, so a caller who says
        # "walker" is told which tariff line was read.
        "device_type": rule["device_type"],
        "counts_as_paid_bag": rule["counts_as_paid_bag"],
        "fee": as_float(rule["fee"]),
        "currency": rule["currency"],
        "serial_number_required": rule["serial_number_required"],
        "labeling_guidance": rule["labeling_guidance"],
        "airport_notification_required": rule["airport_notification_required"],
        "effective_at": rule["effective_at"],
    }


def get_customer_profile(db, args) -> dict:
    customer = _customer_by_email(db, args["email"])
    if customer is None:
        raise NotFound(f"no customer profile for email {args['email']!r}")
    _cleared_verification(db, args["verification_id"], customer["customer_id"])

    sections = set(args["include"])

    result: dict = {
        "customer_id": customer["customer_id"],
        "verification_id": args["verification_id"],
    }

    if "reservations" in sections:
        # Every reservation on the account, whatever its status. Whether one of
        # them duplicates the itinerary being booked is for the caller to judge
        # from the route and dates.
        held = _order(rows(db, "reservations", customer_id=customer["customer_id"]),
                      (lambda row: row["departure_date"], False),
                      (lambda row: row["reservation_id"], False))
        result["reservations"] = []
        for row in held:
            outbound = _flight(db, row["outbound_flight_id"])
            result["reservations"].append({
                "reservation_id": row["reservation_id"],
                "confirmation_code": row["confirmation_code"],
                "origin_code": outbound["origin_code"],
                "destination_code": outbound["destination_code"],
                "departure_date": row["departure_date"],
                "return_date": row["return_date"],
                "status": row["status"],
            })

    if "payment_methods" in sections:
        cards = _order(rows(db, "payment_methods", customer_id=customer["customer_id"],
                            active=True),
                       (lambda row: row["added_at"], False),
                       (lambda row: row["token"], False))
        result["payment_methods"] = [
            {"token": row["token"], "brand": row["brand"], "last4": row["last4"]}
            for row in cards
        ]

    if "travel_certificates" in sections:
        # Every certificate on the account, with its status and balance as held.
        # Codes are never disclosed by a profile read; only the masked form is.
        certificates = _order(rows(db, "travel_certificates",
                                   customer_id=customer["customer_id"]),
                              (lambda row: row["expires_at"] or "", False),
                              (lambda row: row["certificate_id"], False))
        result["travel_certificates"] = [
            compact([
                ("certificate_id", row["certificate_id"]),
                ("masked_code", row["masked_code"]),
                ("status", row["status"]),
                ("available_balance", as_float(row["available_balance"])),
                ("currency", row["currency"]),
                ("expires_at", row["expires_at"]),
            ])
            for row in certificates
        ]

    return result


def verify_customer_identity(db, args) -> dict:
    customer = _customer_by_email(db, args["email"])
    matched: list[str] = []
    if customer is not None:
        if customer["full_name"].strip().lower() == args["full_name"].strip().lower():
            matched.append("full_name")
        if customer["date_of_birth"] == args["date_of_birth"]:
            matched.append("date_of_birth")
        matched.append("email")

    if customer is None:
        status = "failed"
    elif len(matched) < len(IDENTITY_FACTORS):
        # A supplied factor contradicts the profile. The policy sends this to a
        # specialist rather than to another guess.
        status = "failed"
    elif customer["elevated_verification"]:
        # Every supplied factor matched and the profile still needs another one.
        status = "needs_more_factors"
    else:
        status = "verified"

    if status == "verified":
        # A cleared verification is filed under the customer it cleared, so
        # verifying the same person twice in one call resolves to the one record
        # rather than to a second one.
        verification_id = (scenario_id(db, "next_identity_verification_id",
                                       "identity_verifications", "verification_id",
                                       {"customer_id": customer["customer_id"]})
                           or f"verification-{customer['slug']}-booking")
    else:
        # An attempt that cleared nobody is its own record. Filing it under the
        # profile it was tried against would let a wrong date of birth overwrite
        # a verification that already cleared, revoking account access the
        # caller legitimately holds for the rest of the call.
        verification_id = allocate_id(db, "identity_verification")

    created_at = scenario_value(db, "scenario_time")
    expires_at = scenario_value(db, "verification_expiry")
    factors = [factor for factor in IDENTITY_FACTORS if factor in matched]
    filed = {
        "customer_id": customer["customer_id"] if status == "verified" else None,
        "status": status,
        "matched_factors": factors,
        "created_at": created_at,
    }
    # An upsert: a record already filed under this identifier is rewritten in
    # place, keeping its purpose and expiry, as ON CONFLICT DO UPDATE did.
    existing = first(db, "identity_verifications", verification_id=verification_id)
    if existing is None:
        insert(db, "identity_verifications", {
            "verification_id": verification_id, "purpose": "booking",
            "expires_at": expires_at, **filed,
        }, KEY_COLUMNS)
    else:
        existing.update(filed)

    return {
        "verification_id": verification_id,
        "status": status,
        "customer_id": customer["customer_id"] if status == "verified" else None,
        "matched_factors": factors,
        "expires_at": expires_at,
    }


def validate_travel_certificate(db, args) -> dict:
    customer_id = args["customer_id"]
    _cleared_verification(db, args["verification_id"], customer_id)

    code = args["certificate_code"].strip().upper()
    certificate = next((row for row in rows(db, "travel_certificates", customer_id=customer_id)
                        if row["code"].upper() == code), None)
    # A code that does not exist and a code belonging to somebody else are the
    # same answer here, so validation cannot be used to discover whose a
    # certificate is.
    if certificate is None:
        raise NotFound("no travel certificate with that code is held on this account")

    scenario_date = (scenario_value(db, "scenario_time") or "")[:10]
    expired = (certificate["status"] == "expired"
               or (certificate["expires_at"] is not None
                   and certificate["expires_at"] < scenario_date))
    balance = _cents(certificate["available_balance"])
    if expired:
        status = "expired"
    elif certificate["status"] != "valid" or balance <= 0:
        # Spent or voided certificates have no value to apply; the registry has
        # no separate state for them.
        status = "invalid"
    else:
        status = "valid"

    applicable = ZERO
    if status == "valid":
        quote = _current_quote(db)
        applicable = min(balance, _quote_payable(quote)) if quote else balance

    return compact([
        ("certificate_id", certificate["certificate_id"]),
        ("masked_code", certificate["masked_code"]),
        ("status", status),
        ("available_balance", as_float(balance)),
        ("applicable_amount", as_float(applicable)),
        ("currency", certificate["currency"]),
        ("expires_at", certificate["expires_at"]),
    ])


# ---------------------------------------------------------------------------
# mutations
# ---------------------------------------------------------------------------


def _allocate_confirmation_code(db) -> str:
    """Take the next unissued record locator out of the pool.

    Airlines issue locators from a pre-generated pool rather than from a counter.
    Marking the row issued as it is taken means a second booking in a run cannot
    be handed the first booking's code.
    """
    unissued = rows(db, "confirmation_code_pool", issued_at=None)
    if not unissued:
        raise Refusal("no unissued record locator is available")
    row = min(unissued, key=lambda entry: entry["pool_seq"])
    row["issued_at"] = scenario_value(db, "scenario_time")
    return row["code"]


def _next_traveler_seq(db) -> int:
    """The value the travelers.traveler_seq BIGSERIAL would hand out next.

    The seed inserts travelers without naming traveler_seq, so the sequence
    stands at the largest value issued and the next row takes the one after it.
    """
    return max((row["traveler_seq"] for row in db["travelers"].values()), default=0) + 1


def book_reservation(db, args) -> dict:
    customer_id = args["customer_id"]
    customer = first(db, "customers", customer_id=customer_id)
    if customer is None:
        raise NotFound(f"unknown customer {customer_id!r}")
    _cleared_verification(db, args["verification_id"], customer_id)

    # The policy makes authorization of the exact submitted state a precondition,
    # not a formality, so an unauthorized submission creates nothing.
    if not args["customer_authorized"]:
        raise Refusal("customer has not authorized this itinerary and total")

    outbound = _flight(db, args["outbound_flight_id"])
    inbound = _flight(db, args["return_flight_id"])
    fare_class = args["fare_class"]
    travelers = args["travelers"]
    traveler_count = len(travelers)
    bags = args["checked_bag_count"]
    devices = args["mobility_devices"]
    include_insurance = args["include_trip_insurance"]

    quote = None
    if args["quote_id"] is not None:
        quote = first(db, "fare_quotes", quote_id=args["quote_id"])
        if quote is None:
            raise NotFound(f"unknown quote {args['quote_id']!r}")
        mismatch = {
            "outbound_flight_id": (quote["outbound_flight_id"], outbound["flight_id"]),
            "return_flight_id": (quote["return_flight_id"], inbound["flight_id"]),
            "fare_class": (quote["fare_class"], fare_class),
            "traveler_count": (quote["traveler_count"], traveler_count),
            "checked_bag_count": (quote["checked_bag_count"], bags),
            "mobility_device_count": (quote["mobility_device_count"], len(devices)),
            "include_insurance": (quote["include_insurance"], include_insurance),
        }
        differing = sorted(field for field, (quoted, asked) in mismatch.items()
                           if quoted != asked)
        if differing:
            raise Refusal(
                "booking does not match the quote it cites",
                {"quote_id": quote["quote_id"], "differing_fields": differing})
        scenario_time = scenario_value(db, "scenario_time")
        if quote["expires_at"] < scenario_time:
            raise Refusal("quote has expired; reprice before booking",
                          {"quote_id": quote["quote_id"],
                           "expires_at": quote["expires_at"]})
        departure_date, return_date = quote["departure_date"], quote["return_date"]
    else:
        departure_date, return_date = _resolve_itinerary_dates(db, outbound, inbound)

    # A booking knows the device types, so each one is priced on its own tariff
    # line rather than on the unspecified-device line the quote had to use.
    device_rules = [_device_rule(db, device) for device in devices]
    priced = _price_itinerary(db, outbound["flight_id"], inbound["flight_id"],
                              fare_class, traveler_count, bags,
                              [_cents(rule["fee"]) for rule in device_rules],
                              include_insurance)
    charged_total = priced["total"]

    # The customer authorized a number. Charging a different one is a policy
    # violation even when the difference is in the customer's favour.
    if _cents(Decimal(str(args["confirmed_total"]))) != charged_total:
        raise Refusal(
            "confirmed total does not match the current price of this itinerary",
            {"confirmed_total": float(args["confirmed_total"]),
             "current_total": float(charged_total)})

    duplicate = next((
        reservation for reservation in rows(
            db, "reservations", customer_id=customer_id,
            outbound_flight_id=outbound["flight_id"], return_flight_id=inbound["flight_id"],
            departure_date=departure_date, return_date=return_date)
        if reservation["status"] in ACTIVE_RESERVATION_STATUSES), None)
    if duplicate is not None:
        raise Refusal("customer already holds an active reservation on this itinerary",
                      {"reservation_id": duplicate["reservation_id"]})

    # Seats come out of inventory. A fare family without room for the party is
    # refused rather than oversold.
    for flight_id, travel_date in ((outbound["flight_id"], departure_date),
                                   (inbound["flight_id"], return_date)):
        seats = first(db, "flight_availability", flight_id=flight_id,
                      departure_date=travel_date, fare_class=fare_class)
        if seats is None or seats["seats_remaining"] < traveler_count:
            raise Refusal(
                f"{fare_class} has no seats for {traveler_count} on {flight_id}",
                {"flight_id": flight_id, "departure_date": str(travel_date)})
        seats["seats_remaining"] -= traveler_count

    certificate = None
    certificate_applied = ZERO
    if args["certificate_id"] is not None:
        certificate = first(db, "travel_certificates",
                            certificate_id=args["certificate_id"], customer_id=customer_id)
        if certificate is None:
            raise NotFound(
                f"certificate {args['certificate_id']!r} is not held on this account")
        scenario_date = (scenario_value(db, "scenario_time") or "")[:10]
        if certificate["status"] != "valid" or (
                certificate["expires_at"] is not None
                and certificate["expires_at"] < scenario_date):
            raise Refusal("certificate is not valid for use",
                          {"certificate_id": certificate["certificate_id"],
                           "status": certificate["status"]})
        certificate_applied = min(_cents(certificate["available_balance"]),
                                  charged_total)
        if certificate_applied <= 0:
            raise Refusal("certificate has no balance left to apply",
                          {"certificate_id": certificate["certificate_id"]})

    card = first(db, "payment_methods", token=args["payment_method_token"],
                 customer_id=customer_id, active=True)
    if card is None:
        raise Refusal(
            "payment method is not an active tokenized method on this account",
            {"payment_method_token": args["payment_method_token"]})

    remainder = _cents(charged_total - certificate_applied)
    code = _allocate_confirmation_code(db)
    reservation_id = (scenario_id(db, "next_reservation_id", "reservations", "reservation_id")
                      or f"reservation-{code}")
    created_at = scenario_value(db, "scenario_time")
    currency = scenario_value(db, "currency")
    seat_selection_available = bool(first(
        db, "fare_options", flight_id=outbound["flight_id"],
        fare_class=fare_class)["advance_seat_selection_allowed"])

    insert(db, "reservations", {
        "reservation_id": reservation_id,
        "confirmation_code": code,
        "customer_id": customer_id,
        "quote_id": args["quote_id"],
        "outbound_flight_id": outbound["flight_id"],
        "return_flight_id": inbound["flight_id"],
        "departure_date": departure_date,
        "return_date": return_date,
        "contact_email": args["contact_email"],
        "fare_class": fare_class,
        "checked_bag_count": bags,
        "status": "confirmed",
        "ticketing_status": "ticketed",
        "payment_status": "captured",
        "seat_selection_available": seat_selection_available,
        "insurance_included": include_insurance,
        "insurance_plan_id": priced["plan"]["plan_id"] if priced["plan"] else None,
        "insurance_price": as_float(priced["trip_insurance"]),
        "charged_total": as_float(charged_total),
        "currency": currency,
        "created_at": created_at,
    }, KEY_COLUMNS)

    traveler_views = []
    for index, traveler in enumerate(travelers, start=1):
        traveler_id = (scenario_value(db, f"next_traveler_{index}_id")
                       or f"traveler-{_slug(traveler['full_name'])}")
        # UNIQUE (reservation_id, traveler_id): two travelers whose names slug
        # alike in one reservation are refused by the database, and the original
        # backend reported that as a database error.
        if any(view["traveler_id"] == traveler_id for view in traveler_views):
            raise DatabaseError(
                'duplicate key value violates unique constraint '
                '"travelers_reservation_id_traveler_id_key"\n'
                f'DETAIL:  Key (reservation_id, traveler_id)=({reservation_id}, '
                f'{traveler_id}) already exists.')
        insert(db, "travelers", {
            "traveler_seq": _next_traveler_seq(db),
            "reservation_id": reservation_id,
            "traveler_id": traveler_id,
            "full_name": traveler["full_name"],
            "date_of_birth": _date(traveler["date_of_birth"]),
            "traveler_index": index,
        }, KEY_COLUMNS)
        traveler_views.append({
            "traveler_id": traveler_id,
            "full_name": traveler["full_name"],
            "date_of_birth": traveler["date_of_birth"],
        })

    device_views = []
    for index, rule in enumerate(device_rules, start=1):
        insert(db, "reservation_mobility_devices", {
            "device_entry_id": f"{reservation_id}-device-{index}",
            "reservation_id": reservation_id,
            "device_index": index,
            "device_type": rule["device_type"],
            "fee": as_float(_cents(rule["fee"])),
            "counts_as_paid_bag": rule["counts_as_paid_bag"],
            "serial_number_required": rule["serial_number_required"],
            "rule_effective_at": rule["effective_at"],
        }, KEY_COLUMNS)
        device_views.append({
            "device_type": rule["device_type"],
            "fee": as_float(rule["fee"]),
            "serial_number_required": rule["serial_number_required"],
        })

    allocations: list[tuple[str, str, Decimal]] = []
    if certificate is not None:
        allocations.append((f"travel_certificate_{certificate['code']}",
                            "travel_certificate", certificate_applied))
        # The balance is drawn down, and a certificate drawn to nothing is spent.
        left = _num(certificate["available_balance"]) - certificate_applied
        certificate["available_balance"] = as_float(_cents(left))
        if left <= 0:
            certificate["status"] = "redeemed"
        insert(db, "certificate_redemptions", {
            "redemption_id": f"{reservation_id}-redemption-1",
            "certificate_id": certificate["certificate_id"],
            "reservation_id": reservation_id,
            "amount": as_float(certificate_applied),
            "currency": currency,
            "redeemed_at": created_at,
        }, KEY_COLUMNS)
    allocations.append((f"{card['brand'].lower()}_ending_{card['last4']}", "card",
                        remainder))

    for index, (tender, kind, amount) in enumerate(allocations, start=1):
        insert(db, "payment_allocations", {
            "allocation_id": f"{reservation_id}-tender-{index}",
            "reservation_id": reservation_id,
            "allocation_index": index,
            "tender": tender,
            "tender_kind": kind,
            "amount": as_float(amount),
            "currency": currency,
        }, KEY_COLUMNS)

    # The split has to add up to what was charged. Checked here rather than
    # trusted, so a tender arithmetic error rolls the booking back instead of
    # leaving a reservation whose money does not reconcile.
    allocated = sum((_num(row["amount"])
                     for row in rows(db, "payment_allocations", reservation_id=reservation_id)),
                    ZERO)
    if _cents(allocated) != charged_total:
        raise Refusal("tender allocation does not sum to the charged total",
                      {"allocated": float(allocated),
                       "charged_total": float(charged_total)})

    trip_insurance: dict = {"included": include_insurance}
    if include_insurance:
        trip_insurance["price"] = as_float(priced["trip_insurance"])
        trip_insurance["covered_traveler_ids"] = [view["traveler_id"]
                                                  for view in traveler_views]

    return {
        "reservation_id": reservation_id,
        "confirmation_code": code,
        # Confirmed and ticketed are separate facts, as are authorized and
        # captured. The policy forbids collapsing them, and the columns keep them
        # apart.
        "status": "confirmed",
        "ticketing_status": "ticketed",
        "itinerary": {
            "origin": _airport(db, outbound["origin_code"]),
            "destination": _airport(db, outbound["destination_code"]),
            "departure_date": departure_date,
            "return_date": return_date,
        },
        "travelers": traveler_views,
        "fare_class": fare_class,
        # A confirmed reservation does not confirm seats. The list stays empty
        # until a seat action fills it, and there is no seat tool.
        "seat_selection": {
            "available": seat_selection_available,
            "confirmed_seats": [],
        },
        "checked_bag_count": as_int(bags),
        "mobility_devices": device_views,
        "trip_insurance": trip_insurance,
        "payment_allocation": [
            {"tender": tender, "amount": as_float(amount)}
            for (tender, _kind, amount) in allocations
        ],
        "payment_status": "captured",
        "currency": currency,
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
    "list_supported_airports": list_supported_airports,
    "search_flights": search_flights,
    "calculate_itinerary_price": calculate_itinerary_price,
    "check_mobility_device_requirements": check_mobility_device_requirements,
    "get_customer_profile": get_customer_profile,
    "verify_customer_identity": verify_customer_identity,
    "validate_travel_certificate": validate_travel_certificate,
    "book_reservation": book_reservation,
    "transfer_to_specialist": transfer_to_specialist,
}

# Tools that change the airline's records. The grading layer holds reads free —
# an agent may look at anything as often as it likes — so the distinction has to
# be stated somewhere, and the handlers are where it is known. What counts is
# whether the tool changes the world the caller cares about, not whether it
# happens to touch a table: search_flights files the search it just ran and
# calculate_itinerary_price writes the figures it just computed, and both are
# reads a caller may ask for again. verify_customer_identity is here because the
# record it files is what authorizes account access for the rest of the call.
WRITE_TOOLS = {
    "verify_customer_identity",
    "book_reservation",
    "transfer_to_specialist",
}

# What the two filing reads above write: the search cache and the quote cache.
# A state hash leaves these tables out so that reading twice is not damage.
READ_SIDE_EFFECTS: dict[str, list[str] | str] = {
    "flight_searches": "*",
    "fare_quotes": "*",
}
