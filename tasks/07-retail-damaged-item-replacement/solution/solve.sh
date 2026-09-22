#!/bin/bash
# Oracle. Issues the tool calls a correct handling of this call makes, in order,
# and says the things a correct handling has to say.
#
# This is the control on the verifier: it must score 1.0. It is not a model of
# how an agent should reason, only of what a correct reasoning process ends up
# doing to Westline's records and telling the caller.
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
say "Westline customer care. I can pick up the coffee-maker order separately from yesterday's headphones - can I take the order number and the email on it?"

# Open the coffee-maker order. The email is not a formality: this account holds
# three other orders whose references end in digits close to 4086, and the
# register holds a second Patel.
call get_order \
    '{"order_reference": "ending-4086", "customer_email": "ethan.patel@northmail.com",
      "include": ["items", "eligible_resolutions", "cases"]}'

say "I have the 12-cup coffee maker in matte black. Before I do anything with it: what was wrong with it when the box came open, and have you had it switched on at all?"

# Ask the returns service what this damage claim actually unlocks, after
# establishing what was wrong with the item. Read the options rather than
# offering the one the customer asked for.
call get_order \
    '{"order_reference": "ending-4086", "customer_email": "ethan.patel@northmail.com",
      "include": ["eligible_resolutions"]}'

# The condition he attached to choosing a replacement, answered from the
# eligibility rather than from goodwill.
say "Good - leave it off. On your question about the price: a replacement on this claim holds the price you already paid, so the increase since you ordered does not touch you and there is no balance due."
say "Photographs are not needed to process it either. I can put an optional photo link in the confirmation if you want the condition on record, but nothing about it holds up the shipment."

# Confirm the exact variant is available before committing to a replacement.
call get_product \
    '{"product_reference": "coffee-maker-matte-black-12-cup", "include_inventory": true}'

say "The same matte black 12-cup is in stock, and the returns service is waiving the return - no label, no drop-off, Westline does not need the damaged one back."

# Create the replacement, to the home address. The pickup preference belongs to
# yesterday's headphones case and must not follow this order.
call create_replacement_order \
    '{"order_reference": "ending-4086",
      "item_references": ["coffee-maker-matte-black-12-cup"],
      "reason": "damaged", "fulfillment_method": "ship_to_address",
      "fulfillment_location": "home_address_on_order", "customer_authorized": true}'

# Where it is going, and the limit on the date. He asked about both.
say "That has gone through as a new order ending 8821, shipping to your home address. The pickup counter you asked for stays only on yesterday's headphones case - the two are not linked and I have not touched it."
say "Thursday end of day is the estimate, and it is not guaranteed. It is provisionally assigned to the Edison distribution centre, and that can still change before it ships. I cannot see which warehouse packed the first one."

# What to do with the damaged unit. Since it stays with him, the hazard is the
# part that matters.
say "As for the damaged one, you can discard or recycle it - the electronics bin is fine. Because there is water sitting in the base, do not plug it in for any reason, not even to see whether it works."

# The customer cannot see the confirmation yet. Read the message's current
# delivery state rather than restating the state it had when it was created.
call get_order \
    '{"order_reference": "ending-8821", "customer_email": "ethan.patel@northmail.com",
      "include": ["notifications"]}'

say "I have just refreshed the email and it has gone out. The subject line starts with Your Westline replacement, and the optional photo link is further down it."

# The customer asks about the other order without naming it. Resolve it from the
# account's open support work rather than guessing which order he means.
call lookup_customer '{"email": "ethan.patel@northmail.com"}'

# Report yesterday's trace as it stands. Reading it is the whole action: the
# deadline has not passed, so nothing about it should be changed on this call.
call get_order \
    '{"order_reference": "ending-7319", "customer_email": "ethan.patel@northmail.com",
      "include": ["cases"]}'

say "On the headphones: that trace is still open, there is no carrier response yet, and the deadline is 18:00 today. I am leaving it as it is until that window closes rather than changing anything early."

echo "oracle sequence complete" >&2
