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
say "Pioneer Bank Card Services. Declined at a hotel desk - let us sort that out. Are you calling from the number on the account? And can I have your billing ZIP and the last four of the card?"

# Name plus billing ZIP plus the card's last four. The register holds a second
# Colin Reeves at the same ZIP, so the card is what separates them.
call lookup_customer \
    '{"full_name": "Colin Reeves", "billing_zip": "20005", "card_last4": "6148"}'

# All three factors this profile requires. The calling channel is one of them and
# is matched by the channel the call arrived on, not by anything the caller says.
call verify_customer_identity \
    '{"customer_id": "c68e3baf-9314-4fd5-8b8f-85e747f0f9d9", "billing_zip": "20005",
      "card_last4": "6148"}'

say "That all matches and the line you are on is the one on the account, so you are verified."

# Read the attempts before reading them back. This first look is what the agent
# needs to ask "are those both yours": the merchant, the amount, and that both
# were declined.
call get_card_account \
    '{"customer_id": "c68e3baf-9314-4fd5-8b8f-85e747f0f9d9", "card_last4": "6148",
      "include": ["declines"]}'

say "I can see two attempts from Harbor View Hotel, both for 840 dollars, both declined. Are those both you?"

# Now work the review. The second look at the declines adds where each attempt
# came from and why it failed, alongside the card's status, its available credit,
# what is still outstanding, and the restriction itself.
call get_card_account \
    '{"customer_id": "c68e3baf-9314-4fd5-8b8f-85e747f0f9d9", "card_last4": "6148",
      "include": ["status", "available_credit", "authorizations", "declines",
                  "restrictions"]}'

say "Here is what actually happened. The first attempt tripped a travel review, and the second one failed only because that review was still open - so it was not the card and it was not your balance. Nothing is reported lost and the account is current."
say "There is also a 32 dollar authorisation from Logan Airport this morning sitting under the same review. Was that yours?"

# Every piece of activity the review is holding has been confirmed by the caller,
# so the review can be lifted. Confirming all of it is what lifts it; confirming
# some of it does not.
call resolve_card_restriction \
    '{"customer_id": "c68e3baf-9314-4fd5-8b8f-85e747f0f9d9", "card_last4": "6148",
      "restriction_id": "a4e9ecb1-a664-45d0-b3f4-cfe80b54ea8f",
      "confirmed_transaction_ids": ["5af2030f-3fad-402f-a1de-23501596194a", "3065d723-2455-4a43-9d59-91c8dbb11da0",
                                    "131fc14b-fc2e-4ec9-ad07-248d01f7d54b"]}'

say "You have confirmed all three, so I have lifted the review and the card is active again."

# Before the desk tries again, read what the bank publishes about hotel holds, so
# the warning about a larger hold is the bank's position and not the agent's.
call search_knowledge_base \
    '{"query": "Hotel incidental authorization holds, available-credit constraints, authorization guarantees, and pending duration"}'

# The arithmetic he needs before the desk swipes again, and the rule behind it.
say "One thing to get straight before they try again. Your available credit is 912 dollars. Hotels very often authorise more than the room total to cover incidentals, and the whole hold has to be covered by the line, so the 840 fits but a larger hold may not. Have them run it once, not repeatedly, and ask them to tell you the figure first if they change it."

# Confirm the hold actually landed rather than taking the caller's word for the
# receipt.
call get_card_account \
    '{"customer_id": "c68e3baf-9314-4fd5-8b8f-85e747f0f9d9", "card_last4": "6148",
      "include": ["authorizations"]}'

say "I can see the 840 approved on our side now, so you are checked in as far as the card goes. Be aware the hotel hold can stay pending for a few days after checkout, depending on when they finalise it, so that 840 will sit against your line for a while yet."

# Record the trip. A notice is a note on the account and guarantees nothing, which
# is what the tool reports back.
call create_travel_notice \
    '{"customer_id": "c68e3baf-9314-4fd5-8b8f-85e747f0f9d9", "card_last4": "6148",
      "destinations": ["Portland, Maine"], "return_date": "2026-08-30"}'

say "I have put a travel note on the account for Portland through Sunday. To be clear about what that buys you: it is a note for our reviewers and it does not guarantee every purchase will be approved. If something is declined while you are away, ring us."

echo "oracle sequence complete" >&2
