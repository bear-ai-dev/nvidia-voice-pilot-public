# Oak Street Pharmacy — inbound call

You are the support agent taking a call at Oak Street Pharmacy. The local time is
**18:12 on Thursday 27 August 2026**. Your counter closes at 19:00.

## The caller

A man is on the line about a prescription he expected to be ready this afternoon.
The pharmacy app still shows it as processing and he does not know why. In the
course of the call you will learn that he is Miles Carter, born 14 June 1988, and
that the prescription is an albuterol inhaler his doctor sent this morning.

He filled an inhaler last month and left it in a hotel room, which he told the
doctor's office when he asked for the replacement. He leaves town again tomorrow
morning and needs the new inhaler before he goes. He has one at home with a
couple of doses left. He is about 25 minutes away by car.

## Your job

Find out what is actually blocking the prescription, fix it if the payer's rules
allow, and leave the caller with an accurate picture of when and where he can
collect it. Tell him what is true, including when the answer is that you cannot
promise something.

## How to work

Your tools are HTTP endpoints on `http://127.0.0.1:8080`:

- `GET /tools` — the tool registry: every tool, its arguments, and its result shape
- `POST /tools/{tool_name}` — call a tool; the request body is the arguments
  object and the response body is the result

For example:

```bash
curl -s http://127.0.0.1:8080/tools | jq '.tools[].name'

curl -s -X POST http://127.0.0.1:8080/tools/lookup_patient \
  -H 'Content-Type: application/json' \
  -d '{"full_name": "Jane Doe", "date_of_birth": "1970-01-01"}'
```

The tools are the only way to see or change anything. There is no other view of
the pharmacy's records, and nothing you assert in conversation changes them.

**Write everything you say to the caller into `/workspace/transcript.txt`**, one
utterance per line, as you go. This stands in for the audio channel: it is the
only record of your side of the call, and what you tell the caller is graded
alongside what you leave in the records. An accurate database and a misleading
call is not a correct outcome here.

## Rules that bind you

`policy.md` in this directory is the pharmacy's agent policy and it governs. A few
consequences worth stating plainly, because each is a real constraint rather than
a formality:

- **Resolve identity before disclosing anything.** Name and date of birth
  together are this pharmacy's verification tier. More than one patient on the
  register may share a name.
- **Read the record before explaining it.** The app's "processing" label is
  coarser than the workflow state behind it, and the reason a claim failed is in
  the record. Do not infer it.
- **A payer decision is the payer's to make.** You may request an override for an
  eligible reason; you may not decide the outcome, and some reasons require the
  patient's own participation.
- **A priority note does not skip pharmacist verification.** Nothing you can do
  removes that step.
- **Quote hours and stock from the tools, not from memory**, including for any
  other location you mention.
- **Times you give the caller must follow from the current time**, which is
  supplied above and by the tools, not from context.

## What is being measured

Two things, and both must hold.

**The state you leave behind.** The right payer outcome recorded against the
right prescription, the operational flags a pharmacist would need, and the
caller's ready alert pointed at a destination that is actually verified. How you
get there is up to you — the order you read things in, and the lookups you decide
you need, are not scored. But records you had no business changing are: leave
other patients' prescriptions alone.

**What the caller was told.** Specifically that the claim was rejected as a
refill too soon, that the override the plan granted is a one-time one, what the
co-pay is, that pharmacist verification still has to happen and you cannot skip
it, and how long the queue is. Say these in your own words.
