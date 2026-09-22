#!/usr/bin/env python3
"""Grade a run and write reward.json.

This scores what an agent left behind. It does not replay the recorded call
sequence and does not require the agent to have taken the recorded path: an agent
that verifies identity in a different order, skips a lookup it did not need, or
reads a record twice has done nothing wrong. Only outcomes are graded.

    reward = db_reward * communicate_reward

db_reward is 1.0 when both of these hold:

  Required facts   every field in expected_final_state.json is present with that
                   value. A subset assertion, so the database knowing more than
                   the recording did is fine.

  No damage        no row outside the gold path's work area differs from the
                   initial state. A row is damage when the agent inserted,
                   deleted or modified it and the gold path left it untouched.
                   Rows the gold path also touched are the agent's work area:
                   the agent may leave one as it started or end it as the gold
                   path did, so reaching the right outcome by a different route
                   is not penalised, but writing a third value into one is
                   damage too. That catches a wrong value in a column the
                   required facts do not assert.

communicate_reward is 1.0 when every entry in communicate_info.json is satisfied
by the agent's utterances. End-state checking cannot see the part of these
conversations that happens in speech, and for several tasks that part is where
the call is handled well or badly.

Write-call coverage against the gold path is reported but does not gate: it
measures similarity to one reference trajectory, not correctness.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
from decimal import Decimal, InvalidOperation

import statecheck

DEFAULT_VERIFIER_DIR = "/var/lib/task-data/verifier"
DEFAULT_BASE = os.environ.get("TOOL_SERVER_URL", statecheck.DEFAULT_BASE)
DEFAULT_TRANSCRIPT = os.environ.get("AGENT_TRANSCRIPT", "/workspace/transcript.txt")


# Transcripts from LLMs and speech recognisers often use typographic
# punctuation, while the required forms are written in ASCII: without this,
# "can’t promise approval" would not match "can't promise approval".
TYPOGRAPHIC = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u02bc": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"',
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-",
    "\u2212": "-",
})


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.translate(TYPOGRAPHIC)).strip().lower()


def read_transcript(path: str) -> tuple[str, str]:
    """Return (normalized text, description of what was found).

    Accepts either plain text or a harness-supplied message list, so a pipeline
    driving a real voice agent can hand over its own conversation log unmodified.
    Only assistant-role content counts: what the caller said is not the agent's
    to be credited with.
    """
    if not os.path.exists(path):
        return "", f"no transcript at {path}"
    with open(path, encoding="utf-8", errors="replace") as fh:
        raw = fh.read()
    if not raw.strip():
        return "", f"transcript at {path} is empty"

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return normalize(raw), f"{len(raw)} characters of plain text"

    messages = None
    if isinstance(payload, list):
        messages = payload
    elif isinstance(payload, dict):
        for key in ("messages", "transcript", "turns"):
            if isinstance(payload.get(key), list):
                messages = payload[key]
                break
    if messages is None:
        return normalize(raw), f"{len(raw)} characters of JSON, read as text"

    spoken = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role", "")).lower()
        if role and role not in ("assistant", "agent"):
            continue
        content = message.get("content") or message.get("text") or ""
        if isinstance(content, list):
            content = " ".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        if content:
            spoken.append(str(content))
    plural = "" if len(spoken) == 1 else "s"
    return normalize(" ".join(spoken)), f"{len(spoken)} assistant message{plural}"


# A form is "bare" when it is a single number or a single word. Bare numbers are
# compared by numeric value rather than as substrings: the required 95 may match
# 95.00, but must not match 950, 95,000, 95k, 1.95, or 95.09. Nor may it match a
# number that is part of a clock time, a date or an ordinal - the 15 in 10:15,
# 8/15, "august 15" or "the 15th" is not a $15 copay. Bare words use word
# boundaries so 'two' is not found inside 'network', and a number word is not
# found inside a compound such as 'forty-five'. Multi-word forms remain
# substrings because several are deliberate stems - '90-day' is there to match
# both "90-day window" and "90-days".
BARE_NUMBER = re.compile(
    r"^(?P<currency>[$€£]?)(?P<number>\d[\d,]*(?:\.\d+)?)$"
)
BARE_WORD = re.compile(r"^[a-z]+$")
NUMBER_TOKEN = re.compile(
    r"(?<![\w.,+\-])(?<!\d:)(?<!\d/)"
    r"(?P<currency>[$€£])?"
    r"(?P<number>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:e[+\-]?\d+)?)"
    r"(?!\d|,\d|\.\d|:\d|/\d)",
    re.IGNORECASE,
)
MAGNITUDE_SUFFIX = re.compile(
    r"\s*(?:[kmb]|hundred|thousand|million|billion)\b", re.IGNORECASE
)
ORDINAL_SUFFIX = re.compile(r"(?:st|nd|rd|th)\b")
MONTH_BEFORE = re.compile(
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.? $"
)
NUMBER_WORD_COMPOUND = re.compile(
    r"-(?:one|two|three|four|five|six|seven|eight|nine"
    r"|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth)\b"
)


def form_matcher(form: str):
    """Return a predicate deciding whether `form` occurs in a transcript.

    Numeric forms compare complete numeric tokens by Decimal value. This keeps
    equivalent renderings flexible while preventing a required amount from being
    found inside a different amount. Compact magnitude suffixes are rejected, so
    95k cannot satisfy 95; ordinary unit suffixes remain valid, so 11.8GB can
    satisfy 11.8. Currency-prefixed forms still require that currency symbol.
    """
    numeric = BARE_NUMBER.fullmatch(form)
    if numeric:
        required_currency = numeric.group("currency") or None
        try:
            required_value = Decimal(numeric.group("number").replace(",", ""))
        except InvalidOperation:
            return lambda _text: False

        def matches_number(text: str) -> bool:
            for candidate in NUMBER_TOKEN.finditer(text):
                # If the regex restarted after a currency sign (for example the
                # 95 in -$95), preserve the sign fence that applied before it.
                start = candidate.start()
                if (candidate.group("currency") is None and start >= 2
                        and text[start - 1] in "$€£"
                        and text[start - 2] in "+-"):
                    continue
                if (required_currency is not None
                        and candidate.group("currency") != required_currency):
                    continue
                try:
                    value = Decimal(candidate.group("number").replace(",", ""))
                except InvalidOperation:
                    continue
                if value != required_value:
                    continue
                if MAGNITUDE_SUFFIX.match(text, candidate.end()):
                    continue
                if ORDINAL_SUFFIX.match(text, candidate.end()):
                    continue
                if MONTH_BEFORE.search(text, max(0, start - 12), start):
                    continue
                return True
            return False

        return matches_number

    if BARE_WORD.fullmatch(form):
        pattern = re.compile(r"(?<!-)\b" + re.escape(form) + r"\b")

        def matches_word(text: str) -> bool:
            for candidate in pattern.finditer(text):
                if not NUMBER_WORD_COMPOUND.match(text, candidate.end()):
                    return True
            return False

        return matches_word
    return lambda text: form in text


def check_communication(transcript: str, required: list[dict]) -> list[dict]:
    results = []
    for entry in required:
        forms = [normalize(f) for f in entry.get("any_of", []) if f.strip()]
        matched = next(
            (f for f in forms if f and form_matcher(f)(transcript)), None
        )
        results.append({
            "id": entry.get("id", "?"),
            "met": matched is not None,
            "matched_form": matched,
            "why": entry.get("why", ""),
            "any_of": entry.get("any_of", []),
        })
    return results


def find_damage(now: dict, digest: dict, policy: dict) -> list[dict]:
    """Rows the agent changed that the gold path left untouched, and rows in the
    gold path's work area that the agent left in neither the initial nor the gold
    state."""
    initial = digest["initial"]
    gold = digest["gold_final"]
    ignore = set(policy.get("ignore_tables", []))
    append_ok = set(policy.get("append_tolerated", []))

    damage = []
    for table in sorted(set(initial) | set(now)):
        if table in ignore:
            continue
        before = initial.get(table, {})
        after = now.get(table, {})
        gold_after = gold.get(table, {})

        # Everything the gold path touched is the agent's work area. Leaving a
        # row there as it started is the required facts' business, not damage,
        # and ending it as the gold path did is the point. Anything else means
        # the agent wrote a value nobody asked for: a session delivered to the
        # address the caller cannot reach, or a seat count decremented twice.
        gold_touched = {
            key for key in set(before) | set(gold_after)
            if before.get(key) != gold_after.get(key)
        }

        for key in sorted(set(before) | set(after)):
            if key in gold_touched:
                if after.get(key) in (before.get(key), gold_after.get(key)):
                    continue
                kind = "diverged from the gold path"
            elif before.get(key) == after.get(key):
                continue
            elif key not in before:
                if table in append_ok:
                    continue
                kind = "inserted"
            elif key not in after:
                kind = "deleted"
            else:
                kind = "modified"
            damage.append({"table": table, "row": key, "kind": kind})
    return damage


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verifier-dir", default=DEFAULT_VERIFIER_DIR)
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--token-file", default=statecheck.DEFAULT_TOKEN_FILE)
    parser.add_argument("--transcript", default=DEFAULT_TRANSCRIPT)
    parser.add_argument("--out", required=True)
    parser.add_argument("--report", default=None)
    args = parser.parse_args()

    def load(name):
        with open(os.path.join(args.verifier_dir, name)) as fh:
            return json.load(fh)

    lines: list[str] = []
    failures: list[str] = []

    # --- the database half -------------------------------------------------
    state_errors: list[str] = []
    damage: list[dict] = []
    checked = matched = 0
    try:
        token = statecheck.read_token(args.token_file)
        schema = statecheck.fetch_schema(args.base, token)
        key_columns = schema["key_columns"]
        state = statecheck.fetch_state(args.base, token)

        state_errors, checked, matched = statecheck.assert_required_facts(
            state, load("expected_final_state.json"), key_columns
        )
        digest = load("state_digest.json")
        policy = load("grading.json").get("damage", {})

        # The exclusions recorded in the digest, not the ones in grading.json: the
        # hashes being compared against were built with these, so reading the live
        # policy here would compare hashes computed two ways. Which makes an edited
        # policy and a stale digest silently produce wrong damage results, so say so
        # rather than scoring on it. This is an environment defect, not a bad run.
        recorded = digest.get("read_volatile_columns", {})
        declared = policy.get("read_volatile_columns", {})
        if recorded != declared:
            raise SystemExit(
                "state_digest.json was built with read_volatile_columns "
                f"{recorded!r} but grading.json now declares {declared!r}. The "
                "digest is stale and the damage check would be meaningless. "
                "Regenerate it with tests/make_digest.py."
            )

        damage = find_damage(
            statecheck.digest_state(state, key_columns, recorded), digest, policy
        )
    except (OSError, urllib.error.URLError, json.JSONDecodeError, KeyError) as exc:
        state_errors = [f"could not read final state: {type(exc).__name__}: {exc}"]

    lines.append(f"required facts:  {matched}/{checked}")
    for error in state_errors:
        lines.append(f"  FACT {error}")
    lines.append(f"collateral damage: {len(damage)} row(s) changed beyond what the gold path did")
    for row in damage[:20]:
        lines.append(f"  DAMAGE {row['table']}[{row['row']}] {row['kind']}")
    if len(damage) > 20:
        lines.append(f"  ... and {len(damage) - 20} more")

    db_ok = not state_errors and checked > 0 and not damage
    db_reward = 1.0 if db_ok else 0.0
    if state_errors:
        failures.append("required facts not met")
    if damage:
        failures.append(f"{len(damage)} damaged row(s)")
    if checked == 0:
        failures.append("no facts were checked")

    # --- the speech half ---------------------------------------------------
    transcript, source = read_transcript(args.transcript)
    lines.append(f"transcript: {source}")
    try:
        required = load("communicate_info.json").get("required", [])
        comm_error = None
    except (OSError, json.JSONDecodeError) as exc:
        required, comm_error = [], f"{type(exc).__name__}: {exc}"

    comm_results = check_communication(transcript, required)
    comm_met = sum(1 for r in comm_results if r["met"])
    lines.append(f"communicated:    {comm_met}/{len(comm_results)}")
    for result in comm_results:
        if not result["met"]:
            forms = " | ".join(result["any_of"][:3])
            lines.append(f"  UNSAID {result['id']}: {result['why']}")
            lines.append(f"         expected any of: {forms}")

    if comm_error is not None:
        lines.append(f"  COMM could not read communicate_info.json: {comm_error}")
        failures.append("communication requirements unreadable")
        comm_reward = 0.0
    elif not required:
        lines.append("  COMM communicate_info.json lists no requirements")
        failures.append("no communication requirements declared")
        comm_reward = 0.0
    else:
        comm_reward = 1.0 if comm_met == len(comm_results) else 0.0
        if comm_reward == 0.0:
            failures.append(f"{len(comm_results) - comm_met} thing(s) left unsaid")

    # --- diagnostics, which do not gate ------------------------------------
    gold_writes: list[str] = []
    writes_made = 0
    calls_made = 0
    try:
        token = statecheck.read_token(args.token_file)
        write_tools = set(statecheck.fetch_schema(args.base, token)["write_tools"])
        calls = statecheck.fetch_calls(args.base, token)
        calls_made = len(calls)
        gold_calls = load("gold_calls.json")["calls"]
        gold_writes = sorted({c["name"] for c in gold_calls if c["name"] in write_tools})
        attempted = {c["tool_name"] for c in calls}
        writes_made = sum(1 for name in gold_writes if name in attempted)
    except (OSError, urllib.error.URLError, json.JSONDecodeError, KeyError):
        pass

    lines.append(
        f"[diagnostic] tool calls made: {calls_made}; "
        f"gold write tools used: {writes_made}/{len(gold_writes)} "
        f"(similarity to one reference path, not gating)"
    )

    reward = db_reward * comm_reward
    lines.append("")
    lines.append(f"reward: {reward}  (db {db_reward} x communicate {comm_reward})")
    if failures:
        lines.append("failed because: " + "; ".join(failures))

    payload = {
        "reward": reward,
        "score": reward,
        "reward_breakdown": {"db": db_reward, "communicate": comm_reward},
        "state_fields_checked": checked,
        "state_fields_matched": max(matched, 0),
        "damage_rows": len(damage),
        "communicate_required": len(comm_results),
        "communicate_met": comm_met,
        "gold_write_calls_made": writes_made,
        "gold_write_calls_total": len(gold_writes),
        "tool_calls_made": calls_made,
    }
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")

    text = "\n".join(lines) + "\n"
    if args.report:
        with open(args.report, "w") as fh:
            fh.write(text)
    print(text, end="")
    sys.exit(0 if reward == 1.0 else 1)


if __name__ == "__main__":
    main()
