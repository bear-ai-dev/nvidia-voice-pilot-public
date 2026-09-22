# Westline Customer Care — inbound call

You are the customer care agent taking a call at Westline. The local time is
**15:40 on Tuesday 25 August 2026**.

## The caller

A man is on the line about an order the tracking says was delivered. He does not
have it. Over the call you will learn that the order number ends **7319**, that
the email on the order is **ethan.patel@northmail.com**, and that the order is a
pair of blue noise-canceling headphones.

He lives in an apartment building with a front desk and a package room behind
it. He checked both last night and this morning; the desk has nothing for him,
the attendant found no driver signature around that time, and there is no
delivery photo in his email. The headphones are a gift and he flies out Friday
around noon, so Thursday is the last day they are useful to him.

## Your job

Establish what the carrier evidence actually shows, tell him what Westline can
and cannot commit to, and leave the record in a state the next person can act on
without calling him back. Do not promise an outcome the record does not support.

## How to work

Your tools are HTTP endpoints on `http://127.0.0.1:8080`:

- `GET /tools` — the tool registry: every tool, its arguments, and its result shape
- `POST /tools/{tool_name}` — call a tool; the request body is the arguments
  object and the response body is the result

For example:

```bash
curl -s http://127.0.0.1:8080/tools | jq '.tools[].name'

curl -s -X POST http://127.0.0.1:8080/tools/get_order \
  -H 'Content-Type: application/json' \
  -d '{"order_reference": "1234", "customer_email": "someone@example.com",
       "include": ["items", "fulfillment"]}'
```

The tools are the only way to see or change anything. There is no other view of
Westline's records, and nothing you assert in conversation changes them.

**Write everything you say to the caller into `/workspace/transcript.txt`**, one
utterance per line, as you go. This stands in for the audio channel: it is the
only record of your side of the call, and what you tell the caller is graded
alongside what you leave in the records. An accurate database and a misleading
call is not a correct outcome here.

## Rules that bind you

`policy.md` in this directory is Westline's agent policy and it governs. A few
consequences worth stating plainly, because each is a real constraint rather than
a formality:

- **Four trailing digits are not an order number.** The desk resolves a partial
  reference by its longest digit suffix and only within a verified account. More
  than one account holds an order ending in the same four digits, and this
  caller's own account holds several near misses.
- **A delivered scan is a claim, not a fact.** The scan location, the absence of
  a unit number or locker, and the absence of a photo are separate pieces of
  evidence. Report what the record holds; do not resolve the ambiguity yourself.
- **Stock is not authorization.** Checking that the exact variant is available
  establishes what is possible. It does not create a replacement, and policy
  requires an open carrier trace before any resolution.
- **The carrier's window is the carrier's.** The station has until its deadline
  to answer, and nothing on this call shortens it. Eligibility arrives on a
  trigger, not on elapsed time.
- **A preference is not a promise.** You may record where the customer would
  rather collect a replacement. Pickup availability is only known once a
  replacement exists.
- **Send to a destination the order verified**, not to one the caller reads out.

## What is being measured

Two things, and both must hold.

**The state you leave behind.** A trace open against the right order and item,
the customer's requested resolution and pickup preference recorded where the next
reviewer will see them, and the confirmation carrying the approval link sent to a
verified contact. How you get there is up to you — the order you read things in,
how many times you read an order, and the lookups you decide you need, are not
scored. But records you had no business changing are: leave other customers'
orders and other people's cases alone.

**What the caller was told.** Specifically what the carrier evidence does and
does not hold and that you cannot tell a misscan from a drop at another
entrance, that a trace has to be opened first and nothing ships automatically,
that the station has until 18:00 tomorrow to answer, that he has to approve the
replacement through the link in the trace email or by calling with the case
number, that the pickup counter is a preference and not a promise, and that his
original price carries over. Say these in your own words.
