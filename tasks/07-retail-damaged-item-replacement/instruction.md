# Westline Customer Care — inbound call

You are the customer care agent taking a call at Westline. The local time is
**11:20 on Wednesday 26 August 2026**.

## The caller

Ethan Patel is on the line. He spoke to this desk yesterday about a pair of
headphones that were scanned delivered and never arrived; that case is still
open and he will ask about it before the call ends. He is calling today about a
different order.

A coffee maker arrived damaged. Over the call you will learn that the order
number ends **4086**, that the email on the order is
**ethan.patel@northmail.com**, that the water tank is cracked at the bottom and
the base was damp inside the plastic, and that the outside of the box was not
beaten up. He has not tried to power it on. He would rather have a replacement
than a refund, provided he does not pay the current price, which has gone up
since he ordered.

## Your job

Establish what this damage claim actually entitles him to, act on it, and keep
the two matters apart. He is explicitly worried the two orders will get mixed
up. Do not let today's replacement inherit anything from yesterday's case, and
do not change yesterday's case while its carrier window is still running.

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
  -d '{"order_reference": "ending-1234", "customer_email": "someone@example.com",
       "include": ["items", "eligible_resolutions"]}'
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
  reference by its longest digit suffix and only within a verified account. This
  caller's own account holds three orders ending in digits close to `4086`.
- **Read the resolutions before offering one.** What a damage claim unlocks —
  whether the original price carries over, whether a return is required, whether
  a photo is needed to process it — is on the record. Do not infer it from what
  the customer asked for.
- **Stock is not the same question as the price basis.** Check both.
- **A preference recorded on one case does not travel.** Yesterday's pickup
  request belongs to yesterday's case. A replacement created today goes where
  this order's eligibility says it goes unless the customer asks otherwise.
- **An estimate is not a guarantee**, and a distribution centre assigned before
  shipment can still change. Say so.
- **Do not touch an open case whose window has not closed.** Reading it is the
  action; changing it is not.

## What is being measured

Two things, and both must hold.

**The state you leave behind.** A real replacement order against the damaged item
at the price already paid, going to this order's own destination, its
confirmation raised to the verified contact, and yesterday's delivery trace still
open and unaltered with its preference still attached to it and to nothing else.
How you get there is up to you — the order you read things in, how many times you
read an order, and the lookups you decide you need, are not scored. But records
you had no business changing are: yesterday's case is one of them, and so is
every other customer's order.

**What the caller was told.** Specifically that a replacement holds the price he
already paid, that no return is required and there is no label or drop-off, that
the unit must not be plugged in because there is water in its base, that Thursday
is an estimate rather than a guaranteed window, that the replacement is going to
his home address and the pickup counter stays on yesterday's case, and where
yesterday's trace actually stands. Say these in your own words.
