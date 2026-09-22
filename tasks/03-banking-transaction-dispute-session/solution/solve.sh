#!/bin/bash
# Oracle. Issues the tool calls a correct handling of this call makes, in order,
# and says the things a correct handling has to say.
#
# This is the control on the verifier: it must score 1.0. It is not a model of
# how an agent should reason, only of what a correct reasoning process ends up
# doing to the bank's records and telling the caller.
#
# The utterances are paraphrases of the recording, not the recorded wording. That
# is deliberate: if the oracle only passed by reciting the transcript verbatim,
# the communication check would be testing recall rather than whether the caller
# was actually told what he needed to know.
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
say "Harveston Bank, this is the service desk. Before we do anything about the charge, let us work out what it actually is. Can I take your name and the address on your profile?"

# The register holds another Justin Porter, so the address is what resolves the
# profile; the name alone is refused.
call lookup_customer \
    '{"full_name": "Justin Porter", "email": "jporter92@email.com"}'

# The two factors this profile requires, checked against the profile.
call verify_customer_identity \
    '{"customer_id": "4e8c3a97-e6ad-4737-aede-c8acd4993277", "billing_zip": "64114",
      "birth_month_day": "February 19"}'

say "Your ZIP and your birth month and day both match, so you are verified."

# Find the charge on everything the caller could read off his statement. Each of
# the three narrowings matters: the card carries a second charge with the same
# descriptor, one with the same amount on the same day, and an earlier one with
# the same amount from the same merchant.
call get_credit_card_transactions \
    '{"customer_id": "4e8c3a97-e6ad-4737-aede-c8acd4993277", "amount": 243.18,
      "descriptor_contains": "MRKTPLC*8472", "posted_date": "2026-02-23"}'

# What the ledger actually shows, including the detail that points away from a
# stolen card.
say "I have it on the card ending 9102. It is categorised as an online marketplace and it was preceded by a one dollar authorisation, which is the pattern you get when a saved payment method is used at checkout rather than a card being copied."
say "Does anyone else in the house have that card saved in a wallet? And please do not ring any phone number printed in the charge description - if a number is there, that is a reason to be more careful, not less."

# The one-dollar pre-authorization on the charge points at a saved wallet rather
# than a stolen card, so the procedure is read before anything is filed.
call search_knowledge_base \
    '{"query": "unauthorized transaction procedure for unfamiliar marketplace descriptor and possible household use"}'

# The two hard limits, given before he decides rather than after.
say "Two things I should be straight about before you decide anything. I cannot erase a posted charge from this seat - the only route is a dispute, and it goes through the secure form rather than through me. And if you do file, provisional credit is not guaranteed; how long the investigation takes depends on the type of transaction."

# Open the form so it is ready while he checks with his daughter. Opening it is
# not filing it.
call create_secure_self_service_session \
    '{"customer_id": "4e8c3a97-e6ad-4737-aede-c8acd4993277", "workflow": "transaction_dispute",
      "resource_id": "b2047621-d2c7-437c-bb56-c81a700e2099",
      "delivery_channels": ["secure_message"]}'

say "I have put the form in online banking for you - look for Review transaction ending 8472. Have it open while you check with your daughter."

# He asks what happens after submit. The answer is read from the session: it is
# issued and open, and no claim is attached to it.
call get_secure_self_service_session \
    '{"customer_id": "4e8c3a97-e6ad-4737-aede-c8acd4993277", "session_id": "0ae36057-bbbb-42e3-994f-55f064bc374c"}'

say "I can see it is open on your side and there is no claim number on it, because nothing is filed until you press submit yourself. If you do submit, you get a claim reference and the written timing disclosures in secure messages."

# The charge turns out to be his daughter's, so nothing is submitted.
say "Then that settles it - if she used the card that was saved in the marketplace wallet, this should not be reported as fraud just because the merchant name looked unfamiliar. Close the page without submitting and sort the money out with her, or ask the marketplace whether its return policy covers it."

# He asks whether the unused session costs him anything later; that is published
# guidance.
call search_knowledge_base \
    '{"query": "Does an unused unsubmitted dispute session affect future dispute rights?"}'

say "Leaving it unsubmitted does not waive your rights. If something genuinely unauthorised turns up later, ring us straight away and today's unused form counts against you not at all."

echo "oracle sequence complete" >&2
