#!/bin/bash
# Oracle. Issues the tool calls a correct handling of this call makes, in order,
# and says the things a correct handling has to say.
#
# This is the control on the verifier: it must score 1.0. It is not a model of
# how an agent should reason, only of what a correct reasoning process ends up
# doing to the carrier's records and telling the caller.
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
say "Thanks for calling ClearWave Mobile. Let us work out whether that usage is real, which line it came from and what was running. Can I take the mobile number, your full name and your date of birth?"

# The number, name, and date of birth together. The register holds another
# Benjamin Reed and several other Reeds, so the full factor set is what resolves
# to one record.
call lookup_customer \
    '{"mobile_number": "404-555-0176", "full_name": "Benjamin Reed",
      "date_of_birth": "November 22, 1991"}'

# A unique match is a candidate record, not an authorization. Verification is the
# step that returns a scope, and every read below cites it.
call verify_customer_identity \
    '{"customer_id": "6dcb2039-012b-4723-a256-13bb7b6467c2", "mobile_number": "404-555-0176",
      "full_name": "Benjamin Reed", "date_of_birth": "November 22, 1991"}'

# Establish which line, handset, and plan the call is about before discussing
# usage, so the caller can confirm the Pixel 8 is the phone in his hand.
call get_customer_account \
    '{"customer_id": "6dcb2039-012b-4723-a256-13bb7b6467c2",
      "verification_id": "ae1d7bf3-792a-4a70-8673-16c535063f29",
      "include": ["lines", "devices", "plans"]}'

say "You are verified and I have the account open. There is one line on the Unlimited Start plan with a Pixel 8 on it. Is that the handset you are holding?"

# Carrier metering for the window the caller was asleep in. The reported bounds
# are where the traffic actually is, which is what makes "between midnight and
# four in the morning" a measurement rather than a guess.
call get_line_data_usage \
    '{"line_id": "ec8443dc-5fa9-4579-8dbe-5eafc61d53ac",
      "verification_id": "ae1d7bf3-792a-4a70-8673-16c535063f29",
      "window": "last_24_hours"}'

# The measurement, and what it is and is not evidence of. The metering is
# aggregate, so the cause has to come from his handset rather than from here.
say "The metering shows the line carried 11.8 gigabytes between midnight and four in the morning, so the traffic is real. That does not mean anybody got into your phone. What our side records is the total on the line, not which app spent it, so open the data-usage screen on the handset and tell me what is at the top."

# Used data is used, and the handset change he just made does not undo it.
say "Turning that setting off stops it happening again, but the data that has gone cannot be put back on the plan. That leaves you 2.2 gigabytes at full speed for the rest of the cycle."

# The reset date is asked for first and on its own, because that is the only
# thing said at this point in the call.
call get_customer_bills \
    '{"customer_id": "6dcb2039-012b-4723-a256-13bb7b6467c2",
      "verification_id": "ae1d7bf3-792a-4a70-8673-16c535063f29",
      "status": "current", "include": ["cycle"]}'

say "Your cycle resets in nine days. Let me open the current bill itself and see whether anything has landed on it."

# The charge, overage, and post-allowance sections are a separate read, made
# after saying the bill is being opened, so the chronology of the call and the
# chronology of the reads agree.
call get_customer_bills \
    '{"customer_id": "6dcb2039-012b-4723-a256-13bb7b6467c2",
      "verification_id": "ae1d7bf3-792a-4a70-8673-16c535063f29",
      "status": "current", "include": ["charges", "overages", "plan_behavior"]}'

# The thing he was bracing for, answered: he is not being billed for it, the
# plan slows him down instead.
say "Good news on the money: there is no overage charge on that bill. Your plan does not bill you past the included high-speed amount, it slows down instead. I cannot tell you an exact speed because it varies with the network, but maps and mail should still work while video and big downloads will drag."

# No price is quoted before an offer read returns one that is current and
# eligible for this line.
call get_data_addon_offers \
    '{"line_id": "ec8443dc-5fa9-4579-8dbe-5eafc61d53ac",
      "verification_id": "ae1d7bf3-792a-4a70-8673-16c535063f29"}'

# The terms as the offer returned them, and the authorization asked for
# explicitly rather than assumed from his interest.
say "There is one add-on your line can take: five gigabytes of high-speed data for \$40. The data is usable straight away and the \$40 is a single charge on your next bill rather than a new monthly cost. I will not put it through unless you tell me to."

# Submitted by offer id after the caller authorized the amount, price, currency,
# and billing timing that read returned.
call add_data_addon \
    '{"line_id": "ec8443dc-5fa9-4579-8dbe-5eafc61d53ac", "offer_id": "21298486-3eca-4c2e-8d07-2b13c0a33fcc",
      "customer_authorized": true}'

say "That is added. Your line now shows 7.2 gigabytes of high-speed data for the rest of the cycle. Keep an eye on the usage screen for a day, and if the number climbs while the phone is idle, call us back and we will look again."

echo "oracle sequence complete" >&2
