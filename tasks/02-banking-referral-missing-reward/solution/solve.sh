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
say "Let us pull the referral up and find out exactly where it has stalled. Can I take your full name and the address on your profile?"

# The caller gives a name and an address. The register holds another Daniel
# Brooks, so the address is what resolves the profile.
call lookup_customer '{"email": "daniel.brooks17@gmail.com"}'

# The agent states the verification time on the call, so it comes from the
# backend clock first.
call get_current_time '{}'

# The two factors this profile requires. The time the agent quoted aloud is
# asserted with the check, and the record answers with the time it carries.
call verify_customer_identity \
    '{"customer_id": "34d61a18-ede8-4533-b130-7e5937bc1e9d", "billing_zip": "27609",
      "birth_month_day": "October 12", "verified_at": "2026-08-28T09:09:00-04:00"}'

say "I am logging the verification at 9:09 this morning. Two questions off your profile, and please do not read me a full card number: your billing ZIP, and the month and day of your birth."
say "Both of those match, so you are verified."

# Read the referral the caller created. This is the whole of what he is entitled
# to see: his sister's account and purchases are not on it.
call get_referrals '{"customer_id": "34d61a18-ede8-4533-b130-7e5937bc1e9d"}'

say "Here is what your side of the referral shows: the invitation went out on August 2 to an address beginning a-brooks, the application is recorded as approved, and the qualifying purchase stage is still pending."

# Why "approved" and "pending" can both be true at once is in the published
# terms, not in the referral row.
call search_knowledge_base \
    '{"query": "referral qualifying purchase rules and reward posting time"}'

# The part he has misunderstood, said plainly rather than softened.
say "This is the bit I think has been misleading you: approval alone is not enough to earn the bonus. Under the published terms the person you referred has to make a qualifying purchase within 90 days of the invitation, and the credit can then take up to two billing cycles to appear after that purchase posts."
say "Cash advances, balance transfers, fees and anything returned are excluded, but an ordinary card purchase in a shop counts."

# The offer this specific invitation was created under, rather than whatever the
# current banner says.
call search_knowledge_base '{"query": "offer amount and deadline for referral RF8241"}'

say "I have checked the version of the offer your invitation was created under, and its deadline has not passed."

# The privacy boundary, and the reason he cannot simply be told the answer.
say "What I genuinely cannot do is look at your sister's side of this. I cannot see or disclose her purchases, so I am not able to confirm from here whether the grocery run you mentioned has posted to her account."

# Give him a way to watch the stages himself instead of calling weekly. The
# tracker exposes stages, not his sister's activity.
call create_secure_self_service_session \
    '{"customer_id": "34d61a18-ede8-4533-b130-7e5937bc1e9d", "workflow": "referral_status",
      "resource_id": "ce7d58b0-b5fc-42cb-a6e0-7a57929e1205", "delivery_channels": ["secure_message"]}'

say "So that you are not ringing us every week, I have put a referral status tracker in your secure message centre. It shows the invite, approval, qualification and reward posting stages, and the exact deadline attached to the invitation. It does not show anything your sister bought."

# He asks for the link by email. The working tool stays inside online banking, so
# what goes out is a notification that a secure message is waiting.
call send_secure_notification \
    '{"customer_id": "34d61a18-ede8-4533-b130-7e5937bc1e9d",
      "related_resource_id": "b549b0e6-fced-4596-8794-db42a62476a6",
      "channel": "email", "template": "secure_message_waiting"}'

say "I have also emailed you a note saying a secure message is waiting. The working tool itself stays inside online banking, so the email is a heads-up rather than the tracker."

# The sister applied from a different address than the one invited. Whether that
# breaks the match, and whether she should reapply, is published guidance.
call search_knowledge_base \
    '{"query": "Referral matching when invitation and application emails differ; duplicate application guidance"}'

# The one actively harmful thing he might go and do, headed off.
say "On her applying from a different address: that can slow the automatic matching, but your record already reads approved, so the application did attach to your referral. Please do not reapply and do not ask her to - duplicate applications create a separate problem and would not speed this up."
say "If it is still pending two billing cycles after her purchase posts, call us back and quote referral RF8241."

echo "oracle sequence complete" >&2
