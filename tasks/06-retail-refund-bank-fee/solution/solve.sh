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
say "Westline customer care. Can I take the order number, the return receipt, and the email on the order?"

# Open the order. The email is not a formality: this account holds three other
# orders ending in digits close to 5624, and the register holds a second Torrez.
call get_order \
    '{"order_reference": "ending-5624", "customer_email": "teddy.torrez@harbormail.com",
      "include": ["items", "payments", "refunds"]}'

# What the first read shows: the item, the split tender, and the store's side of
# the return already finished.
say "I have the black standing desk converter at \$186.42 after tax, settled on a gift card and a debit card. The Eastwood store accepted the return nine days ago and the unit is back in inventory, so the return itself is closed out."

# The customer says his bank shows nothing. Go back for the payment detail
# rather than repeating the summary: what the trace needs is the token the
# refund was raised against.
call get_order \
    '{"order_reference": "ending-5624", "customer_email": "teddy.torrez@harbormail.com",
      "include": ["payments", "refunds"]}'

# The heart of the call. The two halves of the tender are in different states,
# and the card half is described as the record has it rather than as the caller
# hopes. "Sent" is the word the record does not support.
say "The two halves went different ways. The forty dollars went back to the gift card and completed. The \$146.42 on the debit card was raised by the store register and there is no settlement confirmation from the processor, so I won't tell you it's on the way when our own record cannot say the bank took it."
say "That is also why your bank has nothing pending. Your card ending 2047 was replaced, and a refund raised against a retired token can sit unmatched, but I can only tell you what Westline's side shows - I cannot see inside your account."

# He asks whether the gift card actually works. Read the ledger rather than
# inferring it from the refund having been issued.
call get_order \
    '{"order_reference": "ending-5624", "customer_email": "teddy.torrez@harbormail.com",
      "include": ["payments", "refunds"]}'

# Answer from the ledger, which is a different fact from the refund having been
# issued.
say "I checked the gift-card ledger directly: the card is active with \$40 available, it has not been used, and the number in that email is live."

# Open the trace against the original card token and attach the store return as
# evidence. Reissuing to the new card is what policy forbids while this is open.
call open_refund_trace \
    '{"order_reference": "ending-5624", "return_reference": "ending-9182",
      "payment_reference": "card-ending-2047", "amount": 146.42}'

# He has asked for the money to go to the replacement card. The answer is no,
# and why, rather than a vague deferral.
say "I have opened a payment trace against the original card reference with the store return attached as evidence, and the case number is WST735204."
say "I cannot put it on the new card while the original request is still open. If I raised a second refund now you could end up with both, and the payments team has to close or cancel the first one before anything is reissued."
say "The review window is three to five business days from today. Plenty come back sooner, but I am not going to name you a date before the processor answers."

# Record what the customer reported about his cards. Westline cannot see his
# bank, so the note says who reported it rather than asserting it.
call update_case \
    '{"case_id": "b0fee5fa-0534-4c47-a42a-b3a252106888",
      "note": "Customer reports card ending 2047 was replaced by card ending 8163 and believes both map to the same checking account; external bank-account status is not verified by Westline."}'

# Send the confirmation to the verified contact on the order.
call send_case_notification \
    '{"case_id": "b0fee5fa-0534-4c47-a42a-b3a252106888", "channel": "email",
      "destination_source": "order_contact", "template": "refund_trace_confirmation"}'

say "The confirmation is on its way to the address on the order. It carries the amount under review, the last four of the original card, the response window, and the case number."
say "The store has finished its part of this. From here it sits with our payments team and the card processor, so calling the branch back would not move it along any faster."

# Document the overdraft fee. Documenting it is the whole action: approving a
# bank fee while the trace is open is not available, and the result says so.
call update_case \
    '{"case_id": "b0fee5fa-0534-4c47-a42a-b3a252106888",
      "note": "Customer reports an overdraft fee while expecting the refund; supporting statement may be reviewed after trace outcome."}'

# The fee is the caller's bank's charge, and the desk's standing decision on it
# is not the desk's to change today.
say "On the overdraft charge: your bank levied that, not Westline, and I cannot approve a bank fee while this trace is open. I have put the claim on the case with your statement noted, and customer relations can look at it once the refund outcome is known. I am not promising you a reimbursement."

echo "oracle sequence complete" >&2
