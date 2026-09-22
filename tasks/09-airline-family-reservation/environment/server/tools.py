"""Airline tool handlers.

Each handler has the signature `handler(cur, args) -> dict` where `cur` is a dict
cursor inside a transaction and `args` is the already schema-validated arguments
object. The returned dict is the tool result, serialized as-is.

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
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from db import NotFound, ToolRefusal, all_rows, allocate_id, one, scalar, scenario_id, scenario_value
from projection import as_float, as_int, compact

# Reservation states that occupy a seat and count against a duplicate check. A
# list, not a tuple: psycopg2 adapts a list to a Postgres array, which is what
# `= ANY(...)` needs, and adapts a tuple to a row constructor, which it does not.
ACTIVE_RESERVATION_STATUSES = ["pending_payment", "confirmed", "ticketed"]

# Identity factors this verification tier collects, in the order the registry
# declares them.
IDENTITY_FACTORS = ["full_name", "date_of_birth", "email"]

ZERO = Decimal("0.00")


def _cents(value: Decimal) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"))


def _slug(full_name: str) -> str:
    """First and last name, lowercased and hyphenated.

    Traveler identifiers in the recorded result are the caller's names in this
    form. Deriving them keeps a booking reproducible without a random suffix, and
    the traveler table is keyed per reservation so two reservations naming the
    same person do not collide.
    """
    parts = [part for part in "".join(
        character if character.isalnum() or character.isspace() else " "
        for character in full_name.lower()).split() if part]
    if not parts:
        return "traveler"
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]}-{parts[-1]}"


# ---------------------------------------------------------------------------
# shared lookups
# ---------------------------------------------------------------------------


def _airport(cur, code: str) -> dict:
    row = one(cur, "SELECT code, name FROM airports WHERE code = %s AND served",
              (code,))
    if row is None:
        raise NotFound(f"unknown or unserved airport {code!r}")
    return {"code": row["code"], "name": row["name"]}


def _flight(cur, flight_id: str) -> dict:
    row = one(
        cur,
        """
        SELECT flight_id, origin_code, destination_code, departure_time,
               arrival_time, duration_minutes, stops, arrives_next_day
          FROM flights
         WHERE flight_id = %s
        """,
        (flight_id,),
    )
    if row is None:
        raise NotFound(f"unknown flight {flight_id!r}")
    return row


def _device_rule(cur, device_type: str) -> dict:
    """Accessibility tariff in force for a device type at the scenario clock.

    Matching is exact on the canonical name, then on the rule's aliases, then on
    a two-way substring so "walker" reaches the folding-walker rule and "folding
    walker with seat" does too. A device type no rule names falls back to the
    unspecified-device line rather than being refused, because the airline has a
    tariff position on any device a caller might bring.
    """
    scenario_date = (scenario_value(cur, "scenario_time") or "")[:10]
    params = {"needle": device_type.strip().lower(), "as_of": scenario_date}
    # An exact name beats an alias, which beats a substring, so a caller who says
    # "folding walker with a seat" gets the folding-walker line rather than
    # whichever rule happens to sort first.
    named = one(
        cur,
        """
        SELECT device_type
          FROM mobility_device_rules
         WHERE effective_at <= %(as_of)s
           AND (lower(device_type) = %(needle)s
                OR %(needle)s = ANY (aliases)
                OR position(lower(device_type) in %(needle)s) > 0
                OR EXISTS (SELECT 1 FROM unnest(aliases) alias
                            WHERE position(alias in %(needle)s) > 0))
         ORDER BY CASE WHEN lower(device_type) = %(needle)s THEN 0
                       WHEN %(needle)s = ANY (aliases) THEN 1
                       WHEN position(lower(device_type) in %(needle)s) > 0 THEN 2
                       ELSE 3 END,
                  length(device_type) DESC, device_type
         LIMIT 1
        """,
        params,
    )
    if named is None:
        return _quote_device_rule(cur)
    return one(
        cur,
        """
        SELECT device_type, effective_at, counts_as_paid_bag, fee, currency,
               serial_number_required, labeling_guidance,
               airport_notification_required
          FROM mobility_device_rules
         WHERE device_type = %s AND effective_at <= %s
         ORDER BY effective_at DESC
         LIMIT 1
        """,
        (named["device_type"], scenario_date),
    )


def _quote_device_rule(cur) -> dict:
    """The tariff line a quote uses when no device type has been stated yet."""
    scenario_date = (scenario_value(cur, "scenario_time") or "")[:10]
    row = one(
        cur,
        """
        SELECT device_type, effective_at, counts_as_paid_bag, fee, currency,
               serial_number_required, labeling_guidance,
               airport_notification_required
          FROM mobility_device_rules
         WHERE is_quote_default AND effective_at <= %s
         ORDER BY effective_at DESC, device_type
         LIMIT 1
        """,
        (scenario_date,),
    )
    if row is None:
        raise NotFound("no default mobility-device tariff is in effect")
    return row


def _leg_price(cur, flight_id: str, fare_class: str) -> Decimal:
    row = one(
        cur,
        """
        SELECT base_fare, tax_amount
          FROM fare_options
         WHERE flight_id = %s AND fare_class = %s
        """,
        (flight_id, fare_class),
    )
    if row is None:
        raise NotFound(f"fare class {fare_class!r} is not offered on {flight_id!r}")
    return _cents(row["base_fare"] + row["tax_amount"])


def _bag_fee(cur, fare_class: str) -> Decimal:
    amount = scalar(cur, "SELECT per_bag_amount FROM baggage_fees WHERE fare_class = %s",
                    (fare_class,))
    if amount is None:
        raise NotFound(f"no baggage tariff for fare class {fare_class!r}")
    return _cents(amount)


def _insurance_plan(cur, per_traveler: Decimal) -> dict:
    """The current plan for a fare, chosen by the trip-cost band it falls in.

    Bands partition the range and only the active standard tier is offered at the
    reservation desk, so exactly one plan applies to any fare. The premium is per
    traveler, which is why two travelers on a 558.20 fare come to 94.60 rather
    than to a number stored against the itinerary.
    """
    row = one(
        cur,
        """
        SELECT plan_id, price_per_traveler, document_reference, currency
          FROM insurance_plans
         WHERE active AND tier = 'standard'
           AND min_trip_cost < %s AND max_trip_cost >= %s
         ORDER BY plan_id
         LIMIT 1
        """,
        (per_traveler, per_traveler),
    )
    if row is None:
        raise NotFound(f"no active insurance band covers a fare of {per_traveler}")
    return row


def _price_itinerary(cur, outbound_id: str, return_id: str, fare_class: str,
                     traveler_count: int, checked_bag_count: int,
                     device_fees: list[Decimal], include_insurance: bool) -> dict:
    """Compute a quote from the fare, tariff, and premium rows.

    `device_fees` is one amount per recorded device. A pricing call has only a
    device count and uses the unspecified-device line for each; a booking knows
    the device types and uses each device's own line, so a device that carries a
    fee is charged for at booking even though the quote could not know it would.
    """
    per_traveler = _cents(_leg_price(cur, outbound_id, fare_class)
                          + _leg_price(cur, return_id, fare_class))
    fare_taxes_and_bags = _cents(per_traveler * traveler_count
                                 + _bag_fee(cur, fare_class) * checked_bag_count)
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
        plan = _insurance_plan(cur, per_traveler)
        premium = _cents(plan["price_per_traveler"] * traveler_count)
        priced["trip_insurance"] = premium
        priced["plan"] = plan
        priced["total"] = _cents(fare_taxes_and_bags + device_charge + premium)
    return priced


def _current_quote(cur) -> dict | None:
    """The most recently priced quote: the desk's current pricing context.

    A profile read has to know which itinerary to run a duplicate check against,
    and a certificate validation has to know what amount it is being applied to.
    Neither call carries an itinerary in its arguments, so both read the quote
    that pricing last touched. `last_priced_at` is a column, so which quote that
    is can be queried rather than inferred.
    """
    return one(
        cur,
        """
        SELECT quote_id, outbound_flight_id, return_flight_id, departure_date,
               return_date, fare_class, traveler_count, include_insurance,
               fare_taxes_and_checked_bags, mobility_device_charge,
               trip_insurance, total_with_insurance
          FROM fare_quotes
         WHERE last_priced_at IS NOT NULL
         ORDER BY last_priced_at DESC, quote_id DESC
         LIMIT 1
        """,
    )


def _quote_payable(quote: dict) -> Decimal:
    if quote.get("total_with_insurance") is not None:
        return _cents(quote["total_with_insurance"])
    fare = quote.get("fare_taxes_and_checked_bags") or ZERO
    devices = quote.get("mobility_device_charge") or ZERO
    return _cents(fare + devices)


def _cleared_verification(cur, verification_id: str, customer_id: str) -> dict:
    """A verification record that authorizes account access for this customer.

    The policy makes a self-stated name, date of birth, or email insufficient on
    its own, so account reads and the booking take a verification identifier and
    it is checked here rather than trusted.
    """
    row = one(
        cur,
        """
        SELECT verification_id, customer_id, status
          FROM identity_verifications
         WHERE verification_id = %s
        """,
        (verification_id,),
    )
    if row is None:
        raise ToolRefusal(f"unknown verification record {verification_id!r}")
    if row["status"] != "verified" or row["customer_id"] != customer_id:
        raise ToolRefusal(
            "verification record does not clear account access for this customer",
            {"verification_status": row["status"]})
    return row


def _customer_by_email(cur, email: str) -> dict | None:
    return one(
        cur,
        """
        SELECT customer_id, slug, full_name, date_of_birth, email,
               elevated_verification
          FROM customers
         WHERE lower(email) = lower(%s)
         ORDER BY customer_id
         LIMIT 1
        """,
        (email.strip(),),
    )


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


def list_supported_airports(cur, args) -> dict:
    query = args["destination_area"].strip().lower()
    # A landmark resolves ahead of the district or metro that contains it,
    # because a caller who names the National Mall is asking about the Mall and
    # not about Washington generally. Within a kind, the longest matching phrase
    # wins, so a two-word city name is not beaten by a one-word substring of it.
    area = one(
        cur,
        """
        SELECT area_id, short_name, recommendation_basis, retrieved_at,
               (SELECT max(length(term)) FROM unnest(search_terms) term
                 WHERE position(term in %s) > 0) AS match_length
          FROM destination_areas
         WHERE EXISTS (SELECT 1 FROM unnest(search_terms) term
                        WHERE position(term in %s) > 0)
         ORDER BY CASE area_kind WHEN 'landmark' THEN 0 WHEN 'district' THEN 1
                                 ELSE 2 END,
                  match_length DESC, area_id
         LIMIT 1
        """,
        (query, query),
    )
    if area is None:
        raise NotFound(
            f"no supported airports are catalogued for destination area {args['destination_area']!r}")

    airports = all_rows(
        cur,
        """
        SELECT a.code, a.name
          FROM airport_area_links l
          JOIN airports a ON a.code = l.airport_code AND a.served
         WHERE l.area_id = %s
         ORDER BY l.proximity_rank, a.code
        """,
        (area["area_id"],),
    )
    if not airports:
        raise NotFound(f"destination area {area['area_id']!r} has no served airports")

    return {
        "airports": [{"code": row["code"], "name": row["name"]} for row in airports],
        "recommended_airport_code": airports[0]["code"],
        "recommendation_basis": area["recommendation_basis"],
        "retrieved_at": area["retrieved_at"],
    }


def _sellable_fares(cur, flight_id: str, date, traveler_count: int) -> dict:
    """Fare families with room for the party on this flight and date."""
    rows = all_rows(
        cur,
        """
        SELECT fo.fare_class, fo.base_fare + fo.tax_amount AS leg_price,
               fo.advance_seat_selection_allowed, fa.seats_remaining
          FROM fare_options fo
          JOIN flight_availability fa
            ON fa.flight_id = fo.flight_id AND fa.fare_class = fo.fare_class
         WHERE fo.flight_id = %s AND fa.departure_date = %s
           AND fa.seats_remaining >= %s
         ORDER BY fo.fare_class
        """,
        (flight_id, date, traveler_count),
    )
    return {row["fare_class"]: row for row in rows}


def _best_flight(cur, origin: str, destination: str, date, traveler_count: int,
                 max_stops: int) -> dict | None:
    """Cheapest sellable flight on a route and date, earliest departure on a tie."""
    return one(
        cur,
        """
        SELECT f.flight_id, f.duration_minutes,
               min(fo.base_fare + fo.tax_amount) AS lowest_leg_price
          FROM flights f
          JOIN fare_options fo ON fo.flight_id = f.flight_id
          JOIN flight_availability fa
            ON fa.flight_id = f.flight_id AND fa.fare_class = fo.fare_class
         WHERE f.origin_code = %s AND f.destination_code = %s
           AND f.stops <= %s AND fa.departure_date = %s
           AND fa.seats_remaining >= %s
         GROUP BY f.flight_id, f.duration_minutes, f.departure_time
         ORDER BY lowest_leg_price, f.departure_time, f.flight_id
         LIMIT 1
        """,
        (origin, destination, max_stops, date, traveler_count),
    )


def _connection_legs(cur, itinerary_id: str) -> dict:
    rows = all_rows(
        cur,
        """
        SELECT s.direction, s.segment_index, s.flight_id, s.layover_after_minutes,
               f.duration_minutes, f.origin_code, f.destination_code
          FROM connecting_itinerary_segments s
          JOIN flights f ON f.flight_id = s.flight_id
         WHERE s.itinerary_id = %s
         ORDER BY s.direction, s.segment_index
        """,
        (itinerary_id,),
    )
    legs: dict = {"outbound": [], "return": []}
    for row in rows:
        legs[row["direction"]].append(row)
    return legs


def _best_connection(cur, origin: str, destination: str, departure_date,
                     return_date, traveler_count: int, max_stops: int,
                     max_layover_minutes, direct_outbound: dict | None,
                     direct_return: dict | None) -> dict | None:
    """Cheapest offered connection within the stop and layover limits.

    Savings compare the cheapest fare family sellable across every segment of the
    connection against the cheapest family on the direct pair, for the whole
    party, which is what the registry means by savings versus the comparable
    direct itinerary. Without a direct pair there is nothing to compare against
    and no comparison is returned.
    """
    if direct_outbound is None or direct_return is None:
        return None
    direct_classes = _shared_fare_classes(cur, direct_outbound["flight_id"],
                                          departure_date, direct_return["flight_id"],
                                          return_date, traveler_count)
    if not direct_classes:
        return None
    direct_cheapest = min(
        _cents(_leg_price(cur, direct_outbound["flight_id"], fare_class)
               + _leg_price(cur, direct_return["flight_id"], fare_class))
        for fare_class in direct_classes
    )

    candidates = all_rows(
        cur,
        """
        SELECT itinerary_id, additional_duration_display
          FROM connecting_itineraries
         WHERE origin_code = %s AND destination_code = %s
           AND departure_date = %s AND return_date = %s AND offered
         ORDER BY itinerary_id
        """,
        (origin, destination, departure_date, return_date),
    )

    best = None
    for candidate in candidates:
        legs = _connection_legs(cur, candidate["itinerary_id"])
        if not legs["outbound"] or not legs["return"]:
            continue
        if max(len(legs["outbound"]), len(legs["return"])) - 1 > max_stops:
            continue
        layovers = [row["layover_after_minutes"] for direction in legs.values()
                    for row in direction[:-1]]
        if max_layover_minutes is not None and layovers and max(layovers) > max_layover_minutes:
            continue

        # A fare family is only sellable on the connection when every segment has
        # room for the party in it.
        sellable: set[str] | None = None
        for direction, date in (("outbound", departure_date), ("return", return_date)):
            for row in legs[direction]:
                classes = set(_sellable_fares(cur, row["flight_id"], date,
                                              traveler_count))
                sellable = classes if sellable is None else sellable & classes
        if not sellable:
            continue

        price = min(
            _cents(sum((_leg_price(cur, row["flight_id"], fare_class)
                        for direction in legs.values() for row in direction), ZERO))
            for fare_class in sorted(sellable)
        )
        savings = _cents((direct_cheapest - price) * traveler_count)
        if savings <= 0:
            continue

        added = max(
            sum(row["duration_minutes"] + row["layover_after_minutes"]
                for row in legs["outbound"]) - direct_outbound["duration_minutes"],
            sum(row["duration_minutes"] + row["layover_after_minutes"]
                for row in legs["return"]) - direct_return["duration_minutes"],
        )
        if added <= 0:
            continue
        if best is None or price < best["price"]:
            best = {
                "itinerary_id": candidate["itinerary_id"],
                "price": price,
                "savings": savings,
                "display": candidate["additional_duration_display"],
                "added_minutes": added,
            }
    return best


def _shared_fare_classes(cur, outbound_id: str, departure_date, return_id: str,
                         return_date, traveler_count: int) -> list[str]:
    outbound = _sellable_fares(cur, outbound_id, departure_date, traveler_count)
    inbound = _sellable_fares(cur, return_id, return_date, traveler_count)
    return sorted(set(outbound) & set(inbound))


def search_flights(cur, args) -> dict:
    origin = _airport(cur, args["origin_airport"])
    destination = _airport(cur, args["destination_airport"])
    departure_date = args["departure_date"]
    return_date = args["return_date"]
    traveler_count = args["traveler_count"]
    max_stops = args["max_stops"]
    max_layover = args.get("max_layover_minutes")

    outbound = _best_flight(cur, origin["code"], destination["code"], departure_date,
                            traveler_count, max_stops)
    inbound = _best_flight(cur, destination["code"], origin["code"], return_date,
                           traveler_count, max_stops)

    # A search that allows a stop is a comparison request: the registry has the
    # connecting comparison and the direct option set as alternatives, and the
    # connecting form is what a caller asking about stops is being shown. With no
    # qualifying connection the search falls back to the direct option set, which
    # is also what a nonstop-only search returns.
    connection = None
    if max_stops >= 1:
        connection = _best_connection(cur, origin["code"], destination["code"],
                                      departure_date, return_date, traveler_count,
                                      max_stops, max_layover, outbound, inbound)
    profile = "one_stop" if connection else "nonstop"

    search = one(
        cur,
        """
        SELECT search_id, availability_checked_at, expires_at
          FROM flight_searches
         WHERE origin_code = %s AND destination_code = %s AND departure_date = %s
           AND return_date = %s AND stop_profile = %s
        """,
        (origin["code"], destination["code"], departure_date, return_date, profile),
    )
    if search is None:
        # A route and date pair nobody has checked before is recorded now, with
        # an allocated identifier, so the search a caller just ran is as real as
        # the ones already in the cache.
        search_id = allocate_id(cur, "flight_search")
        checked_at = scenario_value(cur, "scenario_time")
        expires_at = scenario_value(cur, "search_expiry")
        cur.execute(
            """
            INSERT INTO flight_searches
                (search_id, origin_code, destination_code, departure_date,
                 return_date, stop_profile, availability_checked_at, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (search_id, origin["code"], destination["code"], departure_date,
             return_date, profile, checked_at, expires_at),
        )
        search = {"search_id": search_id, "availability_checked_at": checked_at,
                  "expires_at": expires_at}

    result: dict = {"search_id": search["search_id"]}

    if connection is not None:
        result["best_connection"] = {
            "itinerary_id": connection["itinerary_id"],
            "total_savings": as_float(connection["savings"]),
            "currency": scenario_value(cur, "currency"),
            "additional_duration_each_way": connection["display"],
            "additional_duration_minutes_each_way": as_int(connection["added_minutes"]),
        }
    elif outbound is not None and inbound is not None:
        outbound_row = _flight(cur, outbound["flight_id"])
        inbound_row = _flight(cur, inbound["flight_id"])
        result["outbound"] = _flight_view(cur, outbound_row, departure_date)
        result["return"] = _flight_view(cur, inbound_row, return_date)

        shared = _shared_fare_classes(cur, outbound["flight_id"], departure_date,
                                      inbound["flight_id"], return_date,
                                      traveler_count)
        priced = []
        for fare_class in shared:
            option = one(
                cur,
                """
                SELECT advance_seat_selection_allowed
                  FROM fare_options
                 WHERE flight_id = %s AND fare_class = %s
                """,
                (outbound["flight_id"], fare_class),
            )
            priced.append((
                _cents(_leg_price(cur, outbound["flight_id"], fare_class)
                       + _leg_price(cur, inbound["flight_id"], fare_class)),
                fare_class, option["advance_seat_selection_allowed"]))
        priced.sort()
        currency = scenario_value(cur, "currency")
        if priced:
            result["fare_options"] = [
                {
                    "fare_class": fare_class,
                    "price_per_traveler": as_float(price),
                    "currency": currency,
                    # Rank within the returned set, so the cheapest family is the
                    # lower one and the rest are higher. Nothing stores it.
                    "relative_price_rank": "lower" if index == 0 else "higher",
                    "advance_seat_selection_allowed": seats,
                }
                for index, (price, fare_class, seats) in enumerate(priced)
            ]

    result["availability_checked_at"] = search["availability_checked_at"]
    result["expires_at"] = search["expires_at"]
    return result


def _flight_view(cur, flight: dict, date) -> dict:
    return {
        "flight_id": flight["flight_id"],
        "origin": _airport(cur, flight["origin_code"]),
        "destination": _airport(cur, flight["destination_code"]),
        "departure_date": date if isinstance(date, str) else date.isoformat(),
        "departure_time": flight["departure_time"],
        "arrival_time": flight["arrival_time"],
        "duration_minutes": as_int(flight["duration_minutes"]),
        "stops": as_int(flight["stops"]),
    }


def _resolve_itinerary_dates(cur, outbound: dict, inbound: dict) -> tuple:
    """Dates for a quote raised without them.

    A pricing call names flights, not dates, so the dates come from the most
    recent availability check on the same route, and failing that from the next
    seeded departure. A quote that already exists carries its own dates.
    """
    search = one(
        cur,
        """
        SELECT departure_date, return_date
          FROM flight_searches
         WHERE origin_code = %s AND destination_code = %s
         ORDER BY availability_checked_at DESC, search_id DESC
         LIMIT 1
        """,
        (outbound["origin_code"], outbound["destination_code"]),
    )
    if search is not None:
        return search["departure_date"], search["return_date"]

    scenario_date = (scenario_value(cur, "scenario_time") or "")[:10]
    departure = scalar(
        cur,
        """
        SELECT min(departure_date) FROM flight_availability
         WHERE flight_id = %s AND departure_date >= %s
        """,
        (outbound["flight_id"], scenario_date),
    )
    if departure is None:
        raise NotFound(f"flight {outbound['flight_id']!r} has no seeded departures")
    back = scalar(
        cur,
        """
        SELECT min(departure_date) FROM flight_availability
         WHERE flight_id = %s AND departure_date >= %s
        """,
        (inbound["flight_id"], departure),
    )
    return departure, back or departure


def calculate_itinerary_price(cur, args) -> dict:
    outbound = _flight(cur, args["outbound_flight_id"])
    inbound = _flight(cur, args["return_flight_id"])
    fare_class = args["fare_class"]
    traveler_count = args["traveler_count"]
    bags = args["checked_bag_count"]
    devices = args["mobility_device_count"]
    include_insurance = args["include_insurance_quote"]

    # A pricing call has a device count and no device types, so each device is
    # priced on the unspecified-device line of the accessibility tariff.
    device_fee = _cents(_quote_device_rule(cur)["fee"])
    priced = _price_itinerary(cur, outbound["flight_id"], inbound["flight_id"],
                              fare_class, traveler_count, bags,
                              [device_fee] * devices, include_insurance)

    quote = one(
        cur,
        """
        SELECT quote_id, expires_at
          FROM fare_quotes
         WHERE outbound_flight_id = %s AND return_flight_id = %s AND fare_class = %s
           AND traveler_count = %s AND checked_bag_count = %s
           AND mobility_device_count = %s AND include_insurance = %s
        """,
        (outbound["flight_id"], inbound["flight_id"], fare_class, traveler_count,
         bags, devices, include_insurance),
    )
    priced_at = scenario_value(cur, "scenario_time")
    if quote is None:
        quote_id = allocate_id(cur, "fare_quote")
        departure_date, return_date = _resolve_itinerary_dates(cur, outbound, inbound)
        # A fresh quote holds for the validity window the airline publishes,
        # measured from the scenario clock rather than from wall time.
        hours = int(scenario_value(cur, "quote_validity_hours") or 24)
        expires_at = _shift_hours(priced_at, hours)
        cur.execute(
            """
            INSERT INTO fare_quotes
                (quote_id, outbound_flight_id, return_flight_id, departure_date,
                 return_date, fare_class, traveler_count, checked_bag_count,
                 mobility_device_count, include_insurance, currency, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (quote_id, outbound["flight_id"], inbound["flight_id"], departure_date,
             return_date, fare_class, traveler_count, bags, devices,
             include_insurance, scenario_value(cur, "currency"), expires_at),
        )
        quote = {"quote_id": quote_id, "expires_at": expires_at}

    # The computed amounts are written back so a later booking can be held to the
    # figure the customer authorized, and so the desk's current pricing context
    # is a row rather than a memory.
    cur.execute(
        """
        UPDATE fare_quotes
           SET fare_taxes_and_checked_bags = %s,
               mobility_device_charge = %s,
               trip_insurance = %s,
               total_with_insurance = %s,
               insurance_plan_id = %s,
               last_priced_at = %s
         WHERE quote_id = %s
        """,
        (priced["fare_taxes_and_checked_bags"], priced["mobility_device_charge"],
         priced["trip_insurance"],
         priced["total"] if include_insurance else None,
         priced["plan"]["plan_id"] if priced["plan"] else None,
         priced_at, quote["quote_id"]),
    )

    return compact([
        ("quote_id", quote["quote_id"]),
        ("fare_taxes_and_checked_bags", as_float(priced["fare_taxes_and_checked_bags"])),
        ("mobility_device_charge", as_float(priced["mobility_device_charge"])),
        ("trip_insurance", as_float(priced["trip_insurance"])),
        ("insurance_plan_document",
         priced["plan"]["document_reference"] if priced["plan"] else None),
        ("total_with_insurance",
         as_float(priced["total"]) if include_insurance else None),
        ("currency", scenario_value(cur, "currency")),
        ("expires_at", quote["expires_at"]),
    ])


def _shift_hours(timestamp: str, hours: int) -> str:
    """Advance an ISO timestamp that carries an offset, keeping the offset."""
    return (datetime.fromisoformat(timestamp) + timedelta(hours=hours)).isoformat()


def check_mobility_device_requirements(cur, args) -> dict:
    rule = _device_rule(cur, args["device_type"])
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
        "effective_at": rule["effective_at"].isoformat(),
    }


def get_customer_profile(cur, args) -> dict:
    customer = _customer_by_email(cur, args["email"])
    if customer is None:
        raise NotFound(f"no customer profile for email {args['email']!r}")
    _cleared_verification(cur, args["verification_id"], customer["customer_id"])

    sections = set(args["include"])

    # Duplicate check against the itinerary currently being priced at the desk.
    # With nothing priced there is no itinerary to duplicate.
    duplicate = False
    quote = _current_quote(cur)
    if quote is not None:
        duplicate = bool(one(
            cur,
            """
            SELECT 1 FROM reservations
             WHERE customer_id = %s AND outbound_flight_id = %s
               AND return_flight_id = %s AND departure_date = %s
               AND return_date = %s AND status = ANY(%s)
            """,
            (customer["customer_id"], quote["outbound_flight_id"],
             quote["return_flight_id"], quote["departure_date"],
             quote["return_date"], ACTIVE_RESERVATION_STATUSES),
        ))

    result: dict = {
        "customer_id": customer["customer_id"],
        "verification_id": args["verification_id"],
        "duplicate_reservation": duplicate,
    }

    if "payment_methods" in sections:
        cards = all_rows(
            cur,
            """
            SELECT token, brand, last4
              FROM payment_methods
             WHERE customer_id = %s AND active
             ORDER BY added_at, token
            """,
            (customer["customer_id"],),
        )
        result["payment_methods"] = [
            {"token": row["token"], "brand": row["brand"], "last4": row["last4"]}
            for row in cards
        ]

    if "travel_certificates" in sections:
        # Certificate codes are never disclosed by a profile read, so a customer
        # holding usable certificate value has to supply the code. A customer
        # with nothing usable on file has nothing to supply.
        usable = scalar(
            cur,
            """
            SELECT count(*) FROM travel_certificates
             WHERE customer_id = %s AND status = 'valid' AND available_balance > 0
            """,
            (customer["customer_id"],),
        )
        result["travel_certificate_input_required"] = bool(usable)

    return result


def verify_customer_identity(cur, args) -> dict:
    customer = _customer_by_email(cur, args["email"])
    matched: list[str] = []
    if customer is not None:
        if customer["full_name"].strip().lower() == args["full_name"].strip().lower():
            matched.append("full_name")
        if customer["date_of_birth"].isoformat() == args["date_of_birth"]:
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
        verification_id = (scenario_id(cur, "next_identity_verification_id",
                                       "identity_verifications", "verification_id",
                                       {"customer_id": customer["customer_id"]})
                           or f"verification-{customer['slug']}-booking")
    else:
        # An attempt that cleared nobody is its own record. Filing it under the
        # profile it was tried against would let a wrong date of birth overwrite
        # a verification that already cleared, revoking account access the
        # caller legitimately holds for the rest of the call.
        verification_id = allocate_id(cur, "identity_verification")

    created_at = scenario_value(cur, "scenario_time")
    expires_at = scenario_value(cur, "verification_expiry")
    cur.execute(
        """
        INSERT INTO identity_verifications
            (verification_id, customer_id, purpose, status, matched_factors,
             expires_at, created_at)
        VALUES (%s, %s, 'booking', %s, %s, %s, %s)
        ON CONFLICT (verification_id) DO UPDATE
            SET customer_id = EXCLUDED.customer_id,
                status = EXCLUDED.status,
                matched_factors = EXCLUDED.matched_factors,
                created_at = EXCLUDED.created_at
        """,
        (verification_id,
         customer["customer_id"] if status == "verified" else None,
         status, [factor for factor in IDENTITY_FACTORS if factor in matched],
         expires_at, created_at),
    )

    return {
        "verification_id": verification_id,
        "status": status,
        "customer_id": customer["customer_id"] if status == "verified" else None,
        "matched_factors": [factor for factor in IDENTITY_FACTORS if factor in matched],
        "expires_at": expires_at,
    }


def validate_travel_certificate(cur, args) -> dict:
    customer_id = args["customer_id"]
    _cleared_verification(cur, args["verification_id"], customer_id)

    certificate = one(
        cur,
        """
        SELECT certificate_id, code, masked_code, status, available_balance,
               currency, expires_at
          FROM travel_certificates
         WHERE upper(code) = upper(%s) AND customer_id = %s
        """,
        (args["certificate_code"].strip(), customer_id),
    )
    # A code that does not exist and a code belonging to somebody else are the
    # same answer here, so validation cannot be used to discover whose a
    # certificate is.
    if certificate is None:
        raise NotFound("no travel certificate with that code is held on this account")

    scenario_date = (scenario_value(cur, "scenario_time") or "")[:10]
    expired = (certificate["status"] == "expired"
               or (certificate["expires_at"] is not None
                   and certificate["expires_at"].isoformat() < scenario_date))
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
        quote = _current_quote(cur)
        applicable = min(balance, _quote_payable(quote)) if quote else balance

    return compact([
        ("certificate_id", certificate["certificate_id"]),
        ("masked_code", certificate["masked_code"]),
        ("status", status),
        ("available_balance", as_float(balance)),
        ("applicable_amount", as_float(applicable)),
        ("currency", certificate["currency"]),
        ("expires_at", certificate["expires_at"].isoformat()
         if certificate["expires_at"] else None),
    ])


# ---------------------------------------------------------------------------
# mutations
# ---------------------------------------------------------------------------


def _allocate_confirmation_code(cur) -> str:
    """Take the next unissued record locator out of the pool.

    Airlines issue locators from a pre-generated pool rather than from a counter.
    Marking the row issued in the same statement means a second booking in a run
    cannot be handed the first booking's code.
    """
    row = one(
        cur,
        """
        UPDATE confirmation_code_pool
           SET issued_at = %s
         WHERE pool_seq = (SELECT min(pool_seq) FROM confirmation_code_pool
                            WHERE issued_at IS NULL)
        RETURNING code
        """,
        (scenario_value(cur, "scenario_time"),),
    )
    if row is None:
        raise ToolRefusal("no unissued record locator is available")
    return row["code"]


def book_reservation(cur, args) -> dict:
    customer_id = args["customer_id"]
    customer = one(
        cur,
        "SELECT customer_id, email FROM customers WHERE customer_id = %s",
        (customer_id,),
    )
    if customer is None:
        raise NotFound(f"unknown customer {customer_id!r}")
    _cleared_verification(cur, args["verification_id"], customer_id)

    # The policy makes authorization of the exact submitted state a precondition,
    # not a formality, so an unauthorized submission creates nothing.
    if not args["customer_authorized"]:
        raise ToolRefusal("customer has not authorized this itinerary and total")

    outbound = _flight(cur, args["outbound_flight_id"])
    inbound = _flight(cur, args["return_flight_id"])
    fare_class = args["fare_class"]
    travelers = args["travelers"]
    traveler_count = len(travelers)
    bags = args["checked_bag_count"]
    devices = args["mobility_devices"]
    include_insurance = args["include_trip_insurance"]

    quote = None
    if args["quote_id"] is not None:
        quote = one(
            cur,
            """
            SELECT quote_id, outbound_flight_id, return_flight_id, departure_date,
                   return_date, fare_class, traveler_count, checked_bag_count,
                   mobility_device_count, include_insurance, expires_at
              FROM fare_quotes
             WHERE quote_id = %s
            """,
            (args["quote_id"],),
        )
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
            raise ToolRefusal(
                "booking does not match the quote it cites",
                {"quote_id": quote["quote_id"], "differing_fields": differing})
        scenario_time = scenario_value(cur, "scenario_time")
        if quote["expires_at"] < scenario_time:
            raise ToolRefusal("quote has expired; reprice before booking",
                              {"quote_id": quote["quote_id"],
                               "expires_at": quote["expires_at"]})
        departure_date, return_date = quote["departure_date"], quote["return_date"]
    else:
        departure_date, return_date = _resolve_itinerary_dates(cur, outbound, inbound)

    # A booking knows the device types, so each one is priced on its own tariff
    # line rather than on the unspecified-device line the quote had to use.
    device_rules = [_device_rule(cur, device) for device in devices]
    priced = _price_itinerary(cur, outbound["flight_id"], inbound["flight_id"],
                              fare_class, traveler_count, bags,
                              [_cents(rule["fee"]) for rule in device_rules],
                              include_insurance)
    charged_total = priced["total"]

    # The customer authorized a number. Charging a different one is a policy
    # violation even when the difference is in the customer's favour.
    if _cents(Decimal(str(args["confirmed_total"]))) != charged_total:
        raise ToolRefusal(
            "confirmed total does not match the current price of this itinerary",
            {"confirmed_total": float(args["confirmed_total"]),
             "current_total": float(charged_total)})

    duplicate = one(
        cur,
        """
        SELECT reservation_id FROM reservations
         WHERE customer_id = %s AND outbound_flight_id = %s AND return_flight_id = %s
           AND departure_date = %s AND return_date = %s AND status = ANY(%s)
        """,
        (customer_id, outbound["flight_id"], inbound["flight_id"], departure_date,
         return_date, ACTIVE_RESERVATION_STATUSES),
    )
    if duplicate is not None:
        raise ToolRefusal("customer already holds an active reservation on this itinerary",
                          {"reservation_id": duplicate["reservation_id"]})

    # Seats come out of inventory. A fare family without room for the party is
    # refused rather than oversold.
    for flight_id, date in ((outbound["flight_id"], departure_date),
                            (inbound["flight_id"], return_date)):
        seats = one(
            cur,
            """
            UPDATE flight_availability
               SET seats_remaining = seats_remaining - %s
             WHERE flight_id = %s AND departure_date = %s AND fare_class = %s
               AND seats_remaining >= %s
            RETURNING seats_remaining
            """,
            (traveler_count, flight_id, date, fare_class, traveler_count),
        )
        if seats is None:
            raise ToolRefusal(
                f"{fare_class} has no seats for {traveler_count} on {flight_id}",
                {"flight_id": flight_id, "departure_date": str(date)})

    certificate = None
    certificate_applied = ZERO
    if args["certificate_id"] is not None:
        certificate = one(
            cur,
            """
            SELECT certificate_id, code, status, available_balance, expires_at
              FROM travel_certificates
             WHERE certificate_id = %s AND customer_id = %s
               FOR UPDATE
            """,
            (args["certificate_id"], customer_id),
        )
        if certificate is None:
            raise NotFound(
                f"certificate {args['certificate_id']!r} is not held on this account")
        scenario_date = (scenario_value(cur, "scenario_time") or "")[:10]
        if certificate["status"] != "valid" or (
                certificate["expires_at"] is not None
                and certificate["expires_at"].isoformat() < scenario_date):
            raise ToolRefusal("certificate is not valid for use",
                              {"certificate_id": certificate["certificate_id"],
                               "status": certificate["status"]})
        certificate_applied = min(_cents(certificate["available_balance"]),
                                  charged_total)
        if certificate_applied <= 0:
            raise ToolRefusal("certificate has no balance left to apply",
                              {"certificate_id": certificate["certificate_id"]})

    card = one(
        cur,
        """
        SELECT token, brand, last4 FROM payment_methods
         WHERE token = %s AND customer_id = %s AND active
        """,
        (args["payment_method_token"], customer_id),
    )
    if card is None:
        raise ToolRefusal(
            "payment method is not an active tokenized method on this account",
            {"payment_method_token": args["payment_method_token"]})

    remainder = _cents(charged_total - certificate_applied)
    code = _allocate_confirmation_code(cur)
    reservation_id = (scenario_id(cur, "next_reservation_id", "reservations", "reservation_id")
                      or f"reservation-{code}")
    created_at = scenario_value(cur, "scenario_time")
    currency = scenario_value(cur, "currency")
    seat_selection_available = bool(scalar(
        cur,
        """
        SELECT advance_seat_selection_allowed FROM fare_options
         WHERE flight_id = %s AND fare_class = %s
        """,
        (outbound["flight_id"], fare_class),
    ))

    cur.execute(
        """
        INSERT INTO reservations
            (reservation_id, confirmation_code, customer_id, quote_id,
             outbound_flight_id, return_flight_id, departure_date, return_date,
             contact_email, fare_class, checked_bag_count, status, ticketing_status,
             payment_status, seat_selection_available, insurance_included,
             insurance_plan_id, insurance_price, charged_total, currency, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                'confirmed', 'ticketed', 'captured', %s, %s, %s, %s, %s, %s, %s)
        """,
        (reservation_id, code, customer_id, args["quote_id"], outbound["flight_id"],
         inbound["flight_id"], departure_date, return_date, args["contact_email"],
         fare_class, bags, seat_selection_available, include_insurance,
         priced["plan"]["plan_id"] if priced["plan"] else None,
         priced["trip_insurance"], charged_total, currency, created_at),
    )

    traveler_views = []
    for index, traveler in enumerate(travelers, start=1):
        traveler_id = (scenario_value(cur, f"next_traveler_{index}_id")
                       or f"traveler-{_slug(traveler['full_name'])}")
        cur.execute(
            """
            INSERT INTO travelers
                (reservation_id, traveler_id, full_name, date_of_birth,
                 traveler_index)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (reservation_id, traveler_id, traveler["full_name"],
             traveler["date_of_birth"], index),
        )
        traveler_views.append({
            "traveler_id": traveler_id,
            "full_name": traveler["full_name"],
            "date_of_birth": traveler["date_of_birth"],
        })

    device_views = []
    for index, (device, rule) in enumerate(zip(devices, device_rules), start=1):
        cur.execute(
            """
            INSERT INTO reservation_mobility_devices
                (device_entry_id, reservation_id, device_index, device_type, fee,
                 counts_as_paid_bag, serial_number_required, rule_effective_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (f"{reservation_id}-device-{index}", reservation_id, index,
             rule["device_type"], _cents(rule["fee"]), rule["counts_as_paid_bag"],
             rule["serial_number_required"], rule["effective_at"]),
        )
        device_views.append({
            "device_type": rule["device_type"],
            "fee": as_float(rule["fee"]),
            "serial_number_required": rule["serial_number_required"],
        })

    allocations: list[tuple[str, str, Decimal]] = []
    if certificate is not None and certificate_applied > 0:
        allocations.append((f"travel_certificate_{certificate['code']}",
                            "travel_certificate", certificate_applied))
        cur.execute(
            """
            UPDATE travel_certificates
               SET available_balance = available_balance - %s,
                   status = CASE WHEN available_balance - %s <= 0 THEN 'redeemed'
                                 ELSE status END
             WHERE certificate_id = %s
            """,
            (certificate_applied, certificate_applied, certificate["certificate_id"]),
        )
        cur.execute(
            """
            INSERT INTO certificate_redemptions
                (redemption_id, certificate_id, reservation_id, amount, currency,
                 redeemed_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (f"{reservation_id}-redemption-1", certificate["certificate_id"],
             reservation_id, certificate_applied, currency, created_at),
        )
    allocations.append((f"{card['brand'].lower()}_ending_{card['last4']}", "card",
                        remainder))

    for index, (tender, kind, amount) in enumerate(allocations, start=1):
        cur.execute(
            """
            INSERT INTO payment_allocations
                (allocation_id, reservation_id, allocation_index, tender,
                 tender_kind, amount, currency)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (f"{reservation_id}-tender-{index}", reservation_id, index, tender, kind,
             amount, currency),
        )

    # The split has to add up to what was charged. Checked here rather than
    # trusted, so a tender arithmetic error rolls the booking back instead of
    # leaving a reservation whose money does not reconcile.
    allocated = scalar(
        cur,
        "SELECT coalesce(sum(amount), 0) FROM payment_allocations WHERE reservation_id = %s",
        (reservation_id,),
    )
    if _cents(allocated) != charged_total:
        raise ToolRefusal("tender allocation does not sum to the charged total",
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
            "origin": _airport(cur, outbound["origin_code"]),
            "destination": _airport(cur, outbound["destination_code"]),
            "departure_date": departure_date if isinstance(departure_date, str)
            else departure_date.isoformat(),
            "return_date": return_date if isinstance(return_date, str)
            else return_date.isoformat(),
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
