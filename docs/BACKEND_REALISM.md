# Backend realism

Every change a tool call makes to a conversation's JSON database was reviewed
against one question: would a production system write this? The review rated
91 row changes across 48 write calls; 14 were rated high, meaning the backend
was replaying the recording rather than behaving like a system. All 14 are
fixed. This page lists what changed and what is still open.

The audio, what was said, and the tool calls with their arguments did not
change.

## What was wrong, and what replaced it

### Reading a record changed it

- **Before:** looking up an SMS confirmation marked it verified, as if the look
  had entered the code. Looking up a secure session marked it opened. Looking up
  an order moved its email from queued to sent to delivered. Card and order
  reads kept a read counter, and each read returned a different stored copy of
  the recorded output, hiding fields the first time and showing them the second.
- **Now:** reads never write. `env/replay.py` fails any read tool that changes a
  row. Card and order reads return the full record every time. What other people
  did during the call (the customer entering the code, opening the link, the
  mail provider delivering an email) is a scheduled event at its own time.

### Writes invented events nobody performed

- **Before:** lifting the block on a card also created a new $840 hotel
  authorization, cut the available credit, and settled a $32 airport charge.
- **Now:** lifting the block only lifts the block. The hotel retrying the charge
  and the airport charge clearing are merchant and card-network events. The
  hotel's retry happens only once the card is active; if the agent never lifts
  the block, the retry never happens.

### "New" records were already waiting

- **Before:** pricing filled in a quote row that already existed with the
  recorded ID. Booking took the next code from a stored pool of 430 codes, the
  next one being the recorded `B9RT6M`. Case numbers, notification IDs and a
  pharmacy override ID were copied from stored "what the recording used" values.
- **Now:** a record exists only once the action that creates it happens. New IDs
  and codes come from a seeded ID generator that lives with the environment, not
  in the business tables (`state/ids.json`), so the recorded values still come
  out and every recorded call's arguments stay valid. Pricing is a write tool,
  because it creates a quote.

### Time did not move

- **Before:** every write in a call carried the same instant, and telecom stamped
  times from per-tool offsets copied from the recording.
- **Now:** the runtime keeps a clock: the call's start time plus each tool call's
  time in the audio. Recorded timestamps fit it to the second in airline and
  banking; telecom's shifted by one to three seconds.

## The outside events, per conversation

| Conversation | Time | Who | What happens |
|---|---|---|---|
| banking-account-email-card-application | 3:39 | customer | Enters the code texted to their phone |
| banking-account-email-card-application | 7:56 | customer | Opens the card application from the secure message |
| banking-transaction-dispute-session | 3:58 | customer | Opens the dispute session in online banking |
| banking-referral-missing-reward | 4:43 | customer | Opens the referral tracker |
| banking-declined-card-travel | 3:20 | card network | The $32 airport authorization clears |
| banking-declined-card-travel | 3:49 | merchant | The hotel retries the $840 authorization, once the card is active |
| retail-damaged-item-replacement | 6:06, 6:18 | mail system, mail provider | The replacement confirmation is sent, then delivered |
| retail-missing-package | 7:28 | mail provider | The trace confirmation is delivered |
| retail-refund-bank-fee | 7:50 | mail provider | The trace confirmation is delivered |

Airline, pharmacy and telecom have no outside events: nothing happens in those
calls that the agent's own tools do not do.

## Recorded outputs that changed

16 recorded tool results changed: 8 card and order reads now return the full
record on the first look (previously some fields appeared only on a second
look), and 8 timestamps moved to the clock (the airline quote expiry, a banking
verification time, and six telecom times that shifted by one to three seconds,
plus the telecom usage window, now measured back from the moment of the call). Every changed result was checked
against the agent lines that use it; all still follow. In two places the agent
now re-checks something it already has ("let me check the decline reason",
"let me check the payment history"), which is redundant but not wrong.

## Scoring

The DB check ignores columns that only record when something happened
(`CLOCK_COLUMNS` in each domain's `tools.py`): an agent is judged on what it did,
not the second it did it. Airline fare quotes are ignored entirely, because
quotes lapse and an agent may price as often as it likes. After the last tool
call the remaining scheduled events play out, for the reference run and the
agent's run alike, and then the two end states are compared.

## Still open

- **Airline flight searches** still keep the two recorded searches in the
  database, because pricing and booking take no travel dates and read them from
  the latest search. The clean fix is date arguments on pricing, which would
  change a recorded call.
- **Medium findings not yet addressed:** writes do not record which agent,
  channel or verification authorized them; some operations do not write the
  records they imply (no stock movement for a replacement, no ticket or payment
  capture records for a booking, no security notices for an email change);
  policy and template values are still copied onto some rows; ID formats are
  mixed between the recorded records and the background population.
