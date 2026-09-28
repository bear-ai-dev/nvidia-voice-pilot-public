# Tool output revisions

The tool results in the annotated transcripts were written to fit what each
recorded agent said. An audit of all 85 recorded tool calls found that some of
those results made the agent's decisions for it: they named the airport to
recommend, answered the caller's exact question with a true or false, pointed
out the clue a call turns on, or told the agent what not to promise. An agent
trained on those results learns to read a verdict aloud, while Tau-voice
rewards an agent that reads data and reaches the conclusion itself.

This revision changes the tools so they return records and measurements, and
updates the recorded results to match. 35 of the 85 recorded results changed in
this pass, and a second pass (below) brought the total to 47. The spoken audio,
the transcripts of what was said, and the tool calls the agent made (names and
arguments) did not change.

## The rule applied

A tool returns what a real backend would hold or compute:

- Kept: prices and totals, balances, eligibility decided by business rules, a
  payer's decision, status codes, decline and rejection reasons, verification
  results, booking and case records.
- Removed: recommendations, verdicts about the caller's situation, answers keyed
  to the caller's question, pre-extracted clues, disclaimers for the agent to
  repeat, and relative, speech-ready times such as "18:00 tomorrow".
- Also removed: matching the caller's own words to a rule. A tool that took
  "folding walker" and returned the one rule that applies did the agent's
  classification; it now returns the published rules and the agent matches.

Where a policy forbade the agent from drawing a conclusion from returned data
(for example the airline rule against inferring that a fare is cheaper unless
the result says so), the policy now asks for conclusions that rest on returned
data instead.

## What changed, by conversation

### airline-family-reservation

| Call | Tool | Before | After |
|---|---|---|---|
| af-001 | `list_supported_airports` | Airport names, a recommended airport, and a stored sentence explaining why | Each airport with its distance and ground-access time |
| af-002 | `search_flights` | The cheapest nonstop each way, fares ranked "lower" and "higher" | Every nonstop with seats each way, each with its own fares |
| af-003 | `search_flights` | One pre-picked connection with its saving ($62) and "almost three hours" | Every qualifying connection with legs, layovers, durations and fares |
| af-003b | `check_mobility_device_requirements` | The rule for "folding walker", matched from the caller's words by the tool | The accessibility rules for all 12 device categories with the names each covers; the agent matches the walker to its category (one category, an oversized sports wheelchair, is a $35 paid bag) |
| af-005 | `get_customer_profile` | `duplicate_reservation: false` and a hint to ask for a certificate | The customer's reservations (none) and certificates on file |

### banking-account-email-card-application, banking-referral-missing-reward, banking-transaction-dispute-session, banking-declined-card-travel

| Call | Tool | Before | After |
|---|---|---|---|
| bc-007, bc-010, bc-011, bc-012, bc-014, br-005, br-009, bd-004, bd-007, bt-006 | `search_knowledge_base` | True or false fields answering the question asked, for example `automatic_free_checked_bag: false`, `phone_agent_can_override_underwriting: false`, `reapplication_guidance: "do not reapply..."` | A knowledge article: title and prose content stating the same facts |
| bc-006, bc-008 | `search_knowledge_base` | Product and offer rows, with disclaimer flags on the offers | The same rows; the disclaimers are terms text |
| br-004 | `get_referrals` | Invitation date as "August 2" | Invitation date and qualification deadline as dates |
| br-006 | `search_knowledge_base` | For one customer's referral: `deadline_status: "not_passed"`, date withheld | An article about the referral offer; the agent compares the deadline with the current time |
| bd-003 | `get_credit_card_transactions` | `preceded_by_authorization_amount: 1.0` on the charge | The authorization records linked to the charge, including the $1.00 one |
| bt-008 | `create_travel_notice` | `authorization_guaranteed: false` | Removed |

### pharmacy-travel-refill

| Call | Tool | Before | After |
|---|---|---|---|
| ph-002 | `get_prescription` | `front_store_closes_later: true` | `front_store_closes_at: "22:00"` |
| ph-004 | `submit_prescription_claim` | Payment option `pay_15_at_pickup` | `pay_at_pickup`; the $15 copay is its own field |

### retail-refund-bank-fee, retail-damaged-item-replacement, retail-missing-package

| Call | Tool | Before | After |
|---|---|---|---|
| rm-002 | `get_order` | `possible_misscan: true` | Raw scan fields: location, evidence location, unit, locker, photo, time |
| rm-001, rd-002, rd-004, rd-007, rm-004, rm-007, rr-001 | `get_order`, `create_replacement_order`, `open_delivery_trace` | "15:18 yesterday", "18:00 today", "18:00 tomorrow", "Thursday end of day", "9 days" ago | ISO timestamps and dates |
| rr-007, rm-005, rd-004 | `update_case`, `create_replacement_order` | `fee_reimbursement_approved: false`, `pickup_guaranteed: false`, a generated `review_instruction` sentence, `estimate_guaranteed: false` | Removed; the update returns the stored preference |

### telecom-data-usage-cleanup

| Call | Tool | Before | After |
|---|---|---|---|
| td-003 | `get_line_data_usage` | A last-24-hours request reported as midnight to 4 AM, the extent of the spike | The requested window with every hourly sample in it |
| td-004 | `get_customer_bills` | `cycle_resets_in_days: 9` | Removed; `cycle_end` is there |

## Does the recorded speech still follow?

Every revised call was checked against the agent lines that use its result.
All of them still follow from the new results, by reading or simple
arithmetic (the $62 saving is now the difference of two fares times two
travelers; "resets in nine days" is 5 September minus 27 August). Four are
looser than before:

- `retail-missing-package`: the agent says the next person will "check that location first". The case now stores the pickup preference, but nothing states it will be checked first.
- `banking-referral-missing-reward`: the referral record now carries the deadline (31 October), so the agent's later "the tracker will show the actual deadline" is still true but no longer the best answer.
- `airline-family-reservation`: "adds almost three hours each way" fits the outbound (170 minutes longer) better than the return (160 minutes).
- `retail-damaged-item-replacement`: "Thursday by end of day" rests on a delivery date; "end of day" is no longer stated.

## Second pass: consistent records and times

A review of every database change the calls make found places where the
backend was replaying the recording rather than behaving like a system: a
record that showed different fields on a first and second look, a status that
changed because someone looked at it, times frozen at one instant. After fixing
those, 16 more recorded results changed, 12 of them in calls this log does not
already list:

- Card and order lookups now return the full record every time. The first look
  in `banking-declined-card-travel` (bt-003, bt-004, bt-007),
  `retail-refund-bank-fee` (rr-001, rr-002, rr-003),
  `retail-damaged-item-replacement` (rd-001) and `retail-missing-package`
  (rm-001) now shows fields that used to appear only on a second look.
- Timestamps follow the call's own clock: the airline quote expiry (af-004), a
  banking verification time (br-003), and six telecom times (td-001b, td-003,
  td-004, td-004b, td-004c, td-005) that moved by one to three seconds; the
  telecom usage window is now measured back from the moment of the call.

What other people did during the call now shows in `db_after.json` as their own
changes rather than as side effects of a tool: the customer entering a texted
code or opening a link, a hotel retrying a charge once the card is unblocked,
an airport charge clearing, an email being delivered.

## Where the changes live

- `domains/<domain>/tool_registry.json` and `policy.md`
- `conversations/<id>/transcripts/annotated-transcript.json`: the revised results, and the policy and tool definitions embedded in each transcript (also synced in `duplicate-streaming-charge` and `flight-status-connection-risk`, whose own results did not change)
- `conversations/<id>/state/`: the JSON databases and the reconstructed state files
