#!/bin/bash
# Oracle. Issues the tool calls a correct handling of this call makes, in order,
# and says the things a correct handling has to say.
#
# This is the control on the verifier: it must score 1.0. It is not a model of
# how an agent should reason, only of what a correct reasoning process ends up
# doing to the pharmacy's records and telling the caller.
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
say "Oak Street Pharmacy, this is the support desk. Can I take your name and date of birth?"

# The caller gives a name and date of birth; both are needed, because the
# register holds another Miles Carter.
call lookup_patient \
    '{"full_name": "Miles Carter", "date_of_birth": "1988-06-14"}'

# Read the record rather than trusting the app's "processing" label. This is
# where the refill-too-soon rejection is visible.
call get_prescription \
    '{"patient_id": "0e28b5d4-a93b-437e-9ba0-cbe4e7d9dbbb", "medication_name": "albuterol inhaler"}'

# Explain the actual blocker. The app said "processing"; the record says the plan
# rejected the claim as a refill too soon, and the caller is entitled to that.
say "I have the prescription here. It arrived this morning but stopped at the insurance step: the plan rejected the claim because it reads the refill as too soon after your last inhaler."

# The inhaler was lost, which is an eligible override reason on this plan. The
# payer decides; the urgency the caller gave is passed along, not acted on.
call request_claim_override \
    '{"prescription_id": "9670dbb4-6227-48ed-99fc-7ce746085502", "reason": "lost_medication",
      "urgency_context": "Patient leaves town on the morning of 2026-08-28 and needs the inhaler on the evening of 2026-08-27"}'

# With the override approved, rerun the claim. This spends the one-time approval.
call submit_prescription_claim \
    '{"prescription_id": "9670dbb4-6227-48ed-99fc-7ce746085502", "override_id": "9e90544a-3497-4aee-a1f0-4437d4f4684c"}'

# The payer's decision, stated as the payer's: approved, and not repeatable.
say "The plan approved a one-time lost-medication override, so I reran the claim and it paid. Your co-pay is \$15."

# Flag the travel context for the pharmacist. This does not skip verification and
# is not represented to the caller as if it did.
call update_prescription \
    '{"prescription_id": "9670dbb4-6227-48ed-99fc-7ce746085502",
      "priority_reason": "traveling on 2026-08-28; waiting for pickup on the evening of 2026-08-27"}'

# What the note does and does not buy him. He is deciding whether to drive over,
# so the limit is the part that matters.
say "I have flagged that you are travelling and waiting on it tonight. I cannot skip the pharmacist's verification, though. You are next in the queue and it is about 30 minutes."

# Before naming a backup location, check which nearby counters are open past the
# 19:00 close, rather than recalling one.
call search_pharmacy_locations \
    '{"origin_store_id": "8e22d41d-843c-4e73-95c0-3c9877366ba9", "open_after_local_time": "19:00"}'

# And confirm the backup actually has the medication before offering it.
call get_store_inventory \
    '{"store_id": "155059ca-b6aa-4372-b0af-86c2d681c393", "medication_id": "9dcd4906-4db3-4290-bccd-afde111823cb"}'

# Point the ready alert at the caller's verified mobile destination.
call update_prescription \
    '{"prescription_id": "9670dbb4-6227-48ed-99fc-7ce746085502", "notification_channel": "sms",
      "notification_destination_id": "94c55afb-c756-4c14-9f4a-0c0cf2e3d69e"}'

# Hours and stock for the backup come from the two calls above, not from memory.
say "Park Avenue closes at 21:00 and shows the same inhaler in stock, so that is the backup, but I would not transfer it yet because ours should be ready first."
say "Your mobile number is set for the ready alert. Wait for that text before you drive over."

echo "oracle sequence complete" >&2
