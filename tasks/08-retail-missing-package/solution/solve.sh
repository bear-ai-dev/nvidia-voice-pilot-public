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
say "Westline customer care. Can I take the order number and the email address on the order?"

# The caller gives four digits and the email on the order. Both are needed: the
# register holds another Patel, and three of this caller's own orders end in
# digits close enough to matter.
call get_order \
    '{"order_reference": "7319", "customer_email": "ethan.patel@northmail.com",
      "include": ["items", "fulfillment", "carrier_scans"]}'

say "I have the blue noise-canceling headphones. The carrier scanned them delivered yesterday at 15:18 and recorded the front entrance. Is this an apartment building, and has the desk checked its log and the package room?"

# The delivery scan says the front entrance and the customer says the front desk
# has nothing. Go back to the carrier evidence rather than deciding between them.
call get_order \
    '{"order_reference": "7319", "customer_email": "ethan.patel@northmail.com",
      "include": ["carrier_scans"]}'

# What the evidence actually holds, including what it does not hold. The desk
# cannot choose between the two explanations, and saying so is the honest answer.
say "I have gone back into the scan detail. It puts the driver near the building, but there is no unit number, no locker and no delivery photo against it, and it is flagged as a possible misscan. That leaves two readings - a bad scan, or a drop at another entrance - and I cannot tell which from here."

# Check the exact blue variant before saying anything about a replacement. This
# is a read: it establishes what is possible, not what is authorized.
call get_product \
    '{"product_reference": "blue-noise-canceling-headphones", "include_inventory": true}'

say "The same blue model is in stock, so a replacement is possible. It is not something I can just send today, though: I have to open a carrier trace first, and nothing goes out automatically off the back of it."

# The trace is what policy requires before any resolution. The customer's timing
# requirement is recorded on the case; it does not shorten the carrier's window.
call open_delivery_trace \
    '{"order_reference": "7319",
      "item_references": ["blue-noise-canceling-headphones"],
      "reason": "delivered_not_received", "requested_resolution": "undecided",
      "needed_by": "2026-08-27"}'

# The carrier's window and what happens on either side of it. He is flying out
# Friday, so the deadline is the fact he is planning around.
say "The trace is open as case WST481662, and the station has until 18:00 tomorrow to come back on it. If they mark it missing before that, we can move earlier; if the deadline passes with no answer, come back to us and we will review the replacement then. I have put your Thursday requirement on the case, but it does not shorten the carrier's window."
say "When it does become eligible, you will need to approve it. There is an approval link in the trace email I am about to send, and if that email does not reach you, call us with that case number instead."

# Record the pickup preference and the variant the customer wants preserved, on
# this case and no other. Recording a preference is not promising it.
call update_case \
    '{"case_id": "8cf648a4-ca60-4387-bc11-ec38f426123a", "requested_resolution": "replacement",
      "preferred_pickup_location": "West 23rd Street pickup counter",
      "note": "Preserve exact blue variant and original price if replacement becomes eligible."}'

say "I have logged that you want the replacement rather than a refund, in the same blue variant, and that your original price carries over to it, so the change on the black one does not touch you."
say "I have also added the West 23rd Street counter as a preference, and I want to be straight with you that it is a preference and not a promise. Whether pickup is available for this item is only known once the replacement actually exists; the reviewer will check that location first."

# Send the trace confirmation to the verified contact on the order, because the
# approval link the customer will need lives in it.
call send_case_notification \
    '{"case_id": "8cf648a4-ca60-4387-bc11-ec38f426123a", "channel": "email",
      "destination_source": "order_contact", "template": "delivery_trace_confirmation"}'

# Confirm what the customer is looking at: the open trace and the message that
# actually reached them, rather than the send status from a minute ago.
call get_order \
    '{"order_reference": "7319", "customer_email": "ethan.patel@northmail.com",
      "include": ["cases", "notifications"]}'

say "That has reached the address on the order, and it is the same case you are looking at. If the carrier locates the parcel they may contact you directly. If it simply turns up before anything ships, keep it and the trace closes; if it turns up after a replacement has shipped, call us before opening the new one and we will tell you which goes back - you will not be charged again without notice."

echo "oracle sequence complete" >&2
