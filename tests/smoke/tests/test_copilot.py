"""Smoke test: Copilot tools (#901-905).

Exercises all five Copilot tools against a live mailbox. Copilot has no JSON
API -- `tools/copilot.py` drives its chat pane's DOM through Playwright -- so a
live run is the *only* way to find out whether the placeholder selectors in
`browser_session.py`'s Copilot section still match the real pane. Nothing here
can be covered by a unit test.

Two different results both count as a pass, because the module's contract
differs per backend:
  * modern Outlook ("bearer" auth mode): Copilot answers, so each tool must
    return `{"status": "ok", "text": ...}` (or a `timeout` carrying real
    `partial_text`, which the tools document as a non-failure).
  * classic canary-cookie OWA: Copilot does not exist there at all, and the
    documented behavior is to fail *clearly* with the bearer-mode message --
    so that exact message is a pass, recorded with a note.
Anything else -- a selector/DOM failure, a rate-limit banner, an empty
timeout -- is a real KO and fails the run.

Read-only: `draft_reply_with_copilot` and `coach_draft` only ask Copilot for
text (the tools never send or save anything), and the other three are pure
questions. No mailbox state is created or changed, so this is repeatable.

Note on cost: five chat-pane round trips are slow by nature (each one drives a
real browser), which is why TIMEOUT is well below the tools' own 90s default --
a smoke test wants a verdict, not the best possible answer.

Run standalone:
    python -m tests.smoke.tests.test_copilot
"""

import sys
from datetime import date, timedelta

from tests.smoke.mcp_client import call, run, session
from tests.smoke.results import is_error_payload, record

# Per-call ceiling. Five calls at the tools' 90s default would make the suite
# crawl, but 45s was too tight and that cost real coverage: on 2026-09-15
# `summarize_email_thread` did not finish inside it, and because a `timeout`
# carrying partial text counts as a pass below, the suite would have gone on
# reporting OK while never once checking that the tool produces a summary. The
# same item answered fully in ~70s, so the ceiling sits above that.
#
# Note what this does *not* guard against: a tool that never finishes still
# passes here as a timeout-with-partial-text, by the tools' documented contract.
# The ceiling is what decides whether that contract is exercised or leaned on.
TIMEOUT = 80
CALENDAR_WINDOW_DAYS = 14

# Substring of the classic-OWA message in tools/copilot.py's _ask(). Matching
# on it is what keeps this test a pass on tenants where Copilot cannot exist.
NOT_ON_CLASSIC_OWA = "requires the modern Outlook backend"

PING_PROMPT = "Reply with exactly the word PONG and nothing else."
DRAFT_INSTRUCTIONS = "Acknowledge the message and ask for any missing details."
DRAFT_TEXT = (
    "Thanks for the update. Could you send over the remaining details when "
    "you get a chance? I'd like to close this out this week."
)


def _classify(tool: str, args: dict, parsed) -> bool:
    """Record one Copilot call's result and report whether it's a pass."""
    err = is_error_payload(parsed)
    if err:
        if NOT_ON_CLASSIC_OWA in err:
            record(tool, args, "OK",
                   "not applicable: classic OWA backend, Copilot unreachable by design")
            return True

        # status "no_response" means the pane was reached and the prompt
        # submitted, but nothing in the pane ever changed. It carries a
        # content-free structural sketch of the pane, which is the whole point
        # of the run when it fails -- print it in full rather than let record()
        # truncate it to 300 chars, since it's what a real selector gets written
        # from. See browser_session._async_copilot_pane_structure.
        if isinstance(parsed, dict) and parsed.get("status") == "no_response":
            structure = parsed.get("pane_structure") or []
            print(f"\n--- {tool}: pane structure ({len(structure)} nodes) ---")
            for node in structure:
                print(f"  {node}")
            print("--- end pane structure ---\n")
            record(tool, args, "TOOL_ERROR", f"no_response: {err}")
            return False

        record(tool, args, "EXCEPTION" if "_exception" in str(parsed) else "TOOL_ERROR", err)
        return False

    if not isinstance(parsed, dict):
        record(tool, args, "TOOL_ERROR", f"non-object payload: {str(parsed)[:200]}")
        return False

    status = parsed.get("status")
    text = (parsed.get("text") or "").strip()
    partial = (parsed.get("partial_text") or "").strip()

    if status == "ok" and text:
        record(tool, args, "OK", f"{len(text)} chars: {text[:80]!r}")
        return True
    if status == "timeout" and partial:
        # Documented as a non-failure: Copilot was still generating. Real text
        # came back, so the pane was reached and driven correctly.
        record(tool, args, "OK", f"timeout after {TIMEOUT}s, partial: {partial[:80]!r}")
        return True

    record(tool, args, "TOOL_ERROR", f"unexpected payload: {str(parsed)[:200]}")
    return False


async def _first_email_id(s) -> str | None:
    """An Inbox conversation to ground the email-based tools against."""
    args = {"folder": "Inbox", "limit": 1, "ids_only": True}
    info = await call(s, "get_emails", **args)
    err = is_error_payload(info)
    ids = info.get("item_ids") if isinstance(info, dict) else None
    if err or not ids:
        record("get_emails (fixture)", args, "TOOL_ERROR",
               err or "no Inbox conversation to ground Copilot against")
        return None
    return ids[0].get("item_id")


async def _first_event_id(s) -> str | None:
    """An upcoming event to ground meeting_prep against."""
    today = date.today()
    args = {
        "start_date": today.isoformat(),
        "end_date": (today + timedelta(days=CALENDAR_WINDOW_DAYS)).isoformat(),
        "include_body": False,
    }
    events = await call(s, "get_calendar_events", **args)
    err = is_error_payload(events)
    if err or not isinstance(events, list):
        record("get_calendar_events (fixture)", args, "TOOL_ERROR", err or str(events)[:200])
        return None
    for event in events:
        # Synthesized recurrence occurrences carry an empty item_id on purpose
        # (see get_calendar_events' docstring) and can't be passed to a tool.
        if event.get("item_id"):
            return event["item_id"]
    record("get_calendar_events (fixture)", args, "TOOL_ERROR",
           f"no event with a real item_id in the next {CALENDAR_WINDOW_DAYS} days")
    return None


async def main() -> bool:
    async with session() as s:
        outcomes = []

        # 901 first, ungrounded: it needs no mailbox fixture, so a whole-module
        # verdict (e.g. "classic OWA, Copilot unreachable") is known before
        # spending calls on the grounding lookups below.
        ask_args = {"prompt": PING_PROMPT, "timeout": TIMEOUT}
        outcomes.append(_classify(
            "ask_copilot", ask_args, await call(s, "ask_copilot", **ask_args)))

        email_id = await _first_email_id(s)
        event_id = await _first_event_id(s)

        if email_id:
            args = {"item_id": email_id, "timeout": TIMEOUT}
            outcomes.append(_classify(
                "summarize_email_thread", args,
                await call(s, "summarize_email_thread", **args)))

            args = {"item_id": email_id, "instructions": DRAFT_INSTRUCTIONS,
                    "tone": "brief and professional", "timeout": TIMEOUT}
            outcomes.append(_classify(
                "draft_reply_with_copilot", args,
                await call(s, "draft_reply_with_copilot", **args)))

            args = {"item_id": email_id, "draft_text": DRAFT_TEXT, "timeout": TIMEOUT}
            outcomes.append(_classify(
                "coach_draft", args, await call(s, "coach_draft", **args)))

        if event_id:
            args = {"event_id": event_id, "timeout": TIMEOUT}
            outcomes.append(_classify(
                "meeting_prep", args, await call(s, "meeting_prep", **args)))

        # A missing fixture is itself a failed run: the fixture helpers already
        # recorded why, and the tools they feed were never exercised.
        return all(outcomes) and bool(email_id) and bool(event_id)


if __name__ == "__main__":
    ok = run(main())
    sys.exit(0 if ok else 1)
