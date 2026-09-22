#!/bin/bash
# Oracle. Issues the tool calls a correct handling of this call makes, in order,
# and says the things a correct handling has to say.
#
# This is the control on the verifier: it must score 1.0. It is not a model of
# how an agent should reason, only of what a correct reasoning process ends up
# doing to the airline's records and telling the caller.
#
# The utterances are paraphrases of the recording, not the recorded wording. That
# is deliberate: if the oracle only passed by reciting the transcript verbatim,
# the communication check would be testing recall rather than whether the caller
# was actually told what she needed to know.
set -euo pipefail

BASE=${TOOL_SERVER_URL:-http://127.0.0.1:8080}
TRANSCRIPT=${AGENT_TRANSCRIPT:-/workspace/transcript.txt}

call() {
    local tool=$1 args=$2
    echo "--> ${tool} ${args}" >&2
    curl -sf -X POST "${BASE}/tools/${tool}" \
        -H 'Content-Type: application/json' \
        -d "${args}"
    echo
}

say() {
    echo "$1" >> "$TRANSCRIPT"
    echo "    say: $1" >&2
}

: > "$TRANSCRIPT"
say "Thanks for calling BlueMesa Airlines. I will settle the airport first and I will not book anything until we have been through the whole price together."

# The caller names where she is staying rather than an airport. The policy
# forbids calling one airport closest without a comparison basis, so the basis
# comes from the catalog with the list.
call list_supported_airports \
    '{"destination_area": "Washington, DC National Mall"}'

# Three airports, and the reason for preferring one of them is the catalog's,
# not the desk's.
say "Three airports serve that area: Reagan National, Dulles and Baltimore. For a stay by the National Mall the catalogue puts Reagan National closest, with the most direct access into the city, so that is the one I will search."

# Her route, her dates, her party, and the nonstop constraint she stated.
call search_flights \
    '{"origin_airport": "PHX", "destination_airport": "DCA",
      "departure_date": "2026-10-14", "return_date": "2026-10-19",
      "traveler_count": 2, "max_stops": 0}'

# The fare-family difference that decides her choice: she and the boy have to
# sit together, and only one of the two families lets seats be chosen in advance.
say "There is a 09:10 departure out of Phoenix on the 14th and a 15:40 back from Reagan on the 19th, both nonstop. Basic economy is the cheaper family but it does not allow seats to be chosen in advance. Standard economy does, which is what matters if the two of you have to sit together."

# She agrees to a one-stop price comparison but not to a long layover, so the
# layover limit is part of the search rather than something to filter by ear.
call search_flights \
    '{"origin_airport": "PHX", "destination_airport": "DCA",
      "departure_date": "2026-10-14", "return_date": "2026-10-19",
      "traveler_count": 2, "max_stops": 1, "max_layover_minutes": 300}'

# The comparison she asked for, quantified both ways so she can weigh it.
say "I have compared the connecting option. It comes to sixty-two dollars less for the two of you in total, and it adds almost three hours in each direction, so the saving is small for the time it costs."

# The walker's treatment comes from the accessibility tariff before anything is
# said about baggage. The policy forbids asserting that a mobility device needs
# no special handling.
call check_mobility_device_requirements \
    '{"device_type": "folding walker"}'

# What the accessibility rule actually says, including the part that is a
# requirement on her rather than a concession to her.
say "I have read the accessibility rule for a folding walker. It is not counted as one of your paid bags and there is no charge for it, but label it with your contact details and tell the staff at the airport that it is a mobility device."

# Price the itinerary she chose, with the two bags, the walker, and the optional
# insurance she asked about, before any figure is read to her.
call calculate_itinerary_price \
    '{"outbound_flight_id": "2133fbc8-ed10-42aa-baa2-12e3d15a6a05", "return_flight_id": "3ac31d55-0dbc-4f79-892e-743257ec9f13",
      "traveler_count": 2, "fare_class": "standard_economy", "checked_bag_count": 2,
      "mobility_device_count": 1, "include_insurance_quote": true}'

# Every figure read out is a figure the backend returned, and the optional
# product comes with the caveat the policy requires rather than a summary of the
# cover, which is not the desk's to give.
say "Fare, taxes and the two checked bags come to \$1,186.40 for the pair of you. Trip insurance would add \$94.60 on top of that. That coverage has exclusions and I am not able to summarise them for you, so read the plan document the quote points at before you decide."

# Identity first: the stored card, the reservations, and the certificate are all
# account data, and a self-stated name and email are not authentication.
call verify_customer_identity \
    '{"full_name": "Linda Marie Carver", "date_of_birth": "1954-03-08",
      "email": "linda.carver9@outlook.com"}'

# Read the profile under that verification: the duplicate check and the masked
# card come from the backend, not from what she said she had on file.
call get_customer_profile \
    '{"email": "linda.carver9@outlook.com",
      "verification_id": "661f8b5c-f3d8-4612-8645-00f9a5ecded8",
      "include": ["reservations", "payment_methods", "travel_certificates"]}'

say "Your identity checks out and I have the account open. There is no duplicate reservation on these dates, and the active card on file is the Visa ending in 1182."

# Validate the certificate before treating its value as available. Her estimate
# of the balance is not the balance.
call validate_travel_certificate \
    '{"customer_id": "7fe38f6f-b7c5-4f50-a56a-e2d68d6b11d0",
      "verification_id": "661f8b5c-f3d8-4612-8645-00f9a5ecded8",
      "certificate_code": "CT-449108"}'

# The split, read out in full and authorized as a whole, because approving the
# itinerary is not approving the tender.
say "Certificate CT-449108 is valid with two hundred dollars on it. The trip comes to \$1,281.00, the certificate covers \$200 of that and it is used up in doing so, and the remaining \$1,081.00 goes to the Visa ending in 1182. May I book exactly that?"

# Book against the resolved identifiers, with the total she authorized. The
# certificate is drawn down and the remainder goes to the tokenized card.
call book_reservation \
    '{"customer_id": "7fe38f6f-b7c5-4f50-a56a-e2d68d6b11d0",
      "verification_id": "661f8b5c-f3d8-4612-8645-00f9a5ecded8",
      "quote_id": "b28a0bbf-614c-4616-862c-fbeef88f6495",
      "certificate_id": "18e422e6-a2c3-4cb8-93fa-daf7a28c328b",
      "outbound_flight_id": "2133fbc8-ed10-42aa-baa2-12e3d15a6a05", "return_flight_id": "3ac31d55-0dbc-4f79-892e-743257ec9f13",
      "travelers": [{"full_name": "Linda Marie Carver", "date_of_birth": "1954-03-08"},
                    {"full_name": "Evan James Carver", "date_of_birth": "2014-07-21"}],
      "contact_email": "linda.carver9@outlook.com", "fare_class": "standard_economy",
      "checked_bag_count": 2, "mobility_devices": ["folding walker"],
      "include_trip_insurance": true, "payment_method_token": "c1200f87-9c48-41f4-a6f9-04d89f28b2b2",
      "confirmed_total": 1281.0, "customer_authorized": true}'

# Confirmed, ticketed and captured are three separate facts and are read back as
# three, because the policy forbids collapsing them into one claim.
say "That is booked. The reservation is confirmed, the tickets are issued and the payment is captured. Your confirmation code is B9RT6M: Bravo, nine, Romeo, Tango, six, Mike."

# The limit that matters most to her, since sitting together is why she paid for
# the more expensive family in the first place.
say "One thing to be clear about: a confirmed reservation does not confirm seats. You still need to pick your seats yourself on the reservation screen, and check the aircraft map for each direction separately, because the two aircraft are not laid out the same way."

# Her question about the boy's paperwork. The booking rule is the airline's to
# state; the screening and custody rules are not, and saying so is the answer.
say "On your grandson: our booking process does not ask for a consent letter for a domestic trip like this one. What identification and custody paperwork he needs is not a rule I am allowed to paraphrase for you, so please check the current official government guidance closer to the date and carry whatever his mother thinks is appropriate."

echo "oracle sequence complete" >&2
