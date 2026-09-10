"""Smoke test / diagnostic: expand_recurrences on get_calendar_events (#201).

READ-ONLY: mutates nothing. It can't create its own fixture -- create_meeting
has no recurrence parameters, so a recurring series can only be made in OWA's
own UI -- so this test works against whatever real series already exist in the
mailbox.

Its single most valuable output is the raw `Recurrence` payload dumped in step
2. `_expand_recurrence_occurrences` was written entirely from the EWS spec,
never from an observed response, and this tenant is already known to deviate
from standard EWS in several places (CalendarView neither filters by date nor
expands occurrences; GetUserAvailability is a permanent NotImplementedException;
category writes need a bespoke UpdateCalendarEvent action). So "standard EWS
says X" is a weak prior here -- see PROJECT_STATUS.md section 4. Capturing one
real payload converts that assumption into a fact.

Run standalone:
    python -m tests.smoke.tests.test_recurrence_expansion
"""

import json
import sys
import time
from collections import Counter
from datetime import date, timedelta

from tests.smoke.mcp_client import call, run, session
from tests.smoke.results import is_error_payload, record

# Deliberately narrow. Note the cost model: with expand_recurrences=True the
# implementation reconsiders EVERY RecurringMaster in the calendar folder (not
# just the window), because CalendarView does no server-side date filtering and
# a master's own Start/End describe only its first occurrence. Each one costs a
# GetItem, i.e. one browser-tab round-trip, so this is O(masters in mailbox),
# not O(events in window). Widening the window does not reduce that cost, but
# it does add client-side work -- keep it small.
START = date.today().isoformat()
END = (date.today() + timedelta(days=14)).isoformat()

# Master discovery needs a much wider window than the expansion window: a
# RecurringMaster's own Start/End describe its FIRST occurrence, so an ongoing
# weekly series that began two years ago is absent from any current-window
# listing. (Verified 2026-09-10: the narrow window above reports 0 masters
# while the mailbox actually holds 90.) This is the same asymmetry the
# expansion path itself has to work around.
DISCOVERY_START = (date.today() - timedelta(days=1095)).isoformat()
DISCOVERY_END = (date.today() + timedelta(days=365)).isoformat()


async def main() -> bool:
    ok = True
    async with session() as s:
        # 1. Find an existing recurring series (plain listing, no expansion).
        disco_args = {"start_date": DISCOVERY_START, "end_date": DISCOVERY_END,
                      "include_body": False}
        events = await call(s, "get_calendar_events", **disco_args)
        err = is_error_payload(events)
        if err or not isinstance(events, list):
            record("get_calendar_events", disco_args,
                   "EXCEPTION" if "_exception" in str(events) else "TOOL_ERROR",
                   err or f"unexpected shape: {str(events)[:150]}")
            return False

        masters = [e for e in events
                   if e.get("calendar_item_type") == "RecurringMaster" and e.get("item_id")]
        record("get_calendar_events", disco_args, "OK",
               f"{len(events)} event(s) in discovery window, {len(masters)} RecurringMaster")

        if not masters:
            record("expand_recurrences", disco_args, "TOOL_ERROR",
                   "no RecurringMaster found at all -- create a recurring series in OWA; "
                   "expansion cannot be verified without one")
            return False

        # 2. THE diagnostic: dump one real Recurrence payload.
        master = masters[0]
        detail = await call(s, "get_calendar_event", item_id=master["item_id"])
        err = is_error_payload(detail)
        if err or not isinstance(detail, dict):
            record("get_calendar_event", {"item_id": master["item_id"]},
                   "EXCEPTION" if "_exception" in str(detail) else "TOOL_ERROR",
                   err or f"unexpected shape: {str(detail)[:150]}")
            return False

        recurrence = detail.get("recurrence")
        print()
        print("=" * 72)
        print(f"RAW Recurrence payload for series: {detail.get('subject')!r}")
        print("=" * 72)
        print(json.dumps(recurrence, indent=2, ensure_ascii=False))
        print("=" * 72)
        print()

        if not recurrence:
            record("get_calendar_event", {"field": "recurrence"}, "TOOL_ERROR",
                   "a RecurringMaster returned an EMPTY recurrence -- this OWA backend does "
                   "not expose the EWS Recurrence property on GetItem/AllProperties, so "
                   "client-side expansion cannot work as designed (mark #201's "
                   "expand_recurrences path Dev / KNOWN_BUGGY_TOOLS)")
            return False

        # Verified 2026-09-10 across all 90 series in this mailbox: the variant
        # is carried in `__type` under fixed RecurrencePattern/RecurrenceRange
        # wrapper keys, NOT as the key itself the way plain-EWS JSON does it.
        known_patterns = {"DailyRecurrence", "WeeklyRecurrence",
                          "AbsoluteMonthlyRecurrence", "RelativeMonthlyRecurrence",
                          "AbsoluteYearlyRecurrence", "RelativeYearlyRecurrence"}
        known_ranges = {"NoEndRecurrence", "EndDateRecurrence", "NumberedRecurrence"}

        def variant(block):
            return str((block or {}).get("__type", "")).split(":")[0]

        pat = variant(recurrence.get("RecurrencePattern"))
        rng = variant(recurrence.get("RecurrenceRange"))
        # Accept the plain-EWS shape too -- _recurrence_block() handles both.
        if not pat:
            pat = next((k for k in known_patterns if k in recurrence), "")
        if not rng:
            rng = next((k for k in known_ranges if k in recurrence), "")

        if pat in known_patterns and rng in known_ranges:
            record("get_calendar_event", {"field": "recurrence"}, "OK",
                   f"recurrence schema recognized: pattern={pat}, range={rng}, "
                   f"top-level keys={sorted(recurrence)}")
        else:
            record("get_calendar_event", {"field": "recurrence"}, "TOOL_ERROR",
                   f"unrecognized recurrence schema -- keys={sorted(recurrence)}, "
                   f"pattern={pat!r}, range={rng!r}. Fix _recurrence_block/"
                   f"_expand_recurrence_occurrences, or mark the path Dev.")
            ok = False

        # 3. Run the expansion and check the invariants that must hold either way.
        exp_args = {"start_date": START, "end_date": END,
                    "include_body": False, "expand_recurrences": True}
        t0 = time.monotonic()
        expanded = await call(s, "get_calendar_events", **exp_args)
        elapsed = time.monotonic() - t0
        err = is_error_payload(expanded)
        if err or not isinstance(expanded, list):
            record("get_calendar_events", exp_args,
                   "EXCEPTION" if "_exception" in str(expanded) else "TOOL_ERROR",
                   err or f"unexpected shape: {str(expanded)[:150]}")
            return False

        synth = [e for e in expanded if e.get("is_synthesized_occurrence")]
        real = [e for e in expanded if not e.get("is_synthesized_occurrence")]

        # Invariant A: synthesized rows must carry no item_id (guards against a
        # caller mutating a whole series through an occurrence's id).
        leaked = [e for e in synth if e.get("item_id")]
        if leaked:
            record("expand_recurrences", exp_args, "TOOL_ERROR",
                   f"{len(leaked)} synthesized occurrence(s) carry a non-empty item_id -- "
                   f"a caller could mutate the entire series through one")
            ok = False

        # Invariant B: every synthesized start must land inside the window.
        out_of_range = [e for e in synth
                        if not (START <= (e.get("start") or "")[:10] <= END)]
        if out_of_range:
            record("expand_recurrences", exp_args, "TOOL_ERROR",
                   f"{len(out_of_range)} synthesized occurrence(s) fall outside "
                   f"[{START}, {END}], e.g. {out_of_range[0].get('start')!r}")
            ok = False

        # Invariant C: no real row may be emitted twice. The expansion path adds
        # out-of-window masters that yield occurrences on top of the normal
        # in-window rows, so double-appending one is the specific regression
        # risk here. (Checked as "no duplicate item_id" rather than "master[0]
        # appears once", because the series discovered above may be one that
        # ended years ago and legitimately contributes nothing to this window.)
        counts = Counter(e["item_id"] for e in real if e.get("item_id"))
        dupes = {i: n for i, n in counts.items() if n > 1}
        if dupes:
            record("expand_recurrences", exp_args, "TOOL_ERROR",
                   f"{len(dupes)} item_id(s) emitted more than once among non-synthesized "
                   f"rows, e.g. {list(dupes.items())[:2]}")
            ok = False

        # Every synthesized occurrence must trace back to a real master row.
        orphan_subjects = {e.get("subject") for e in synth} - {e.get("subject") for e in real}
        if orphan_subjects:
            record("expand_recurrences", exp_args, "TOOL_ERROR",
                   f"{len(orphan_subjects)} synthesized subject(s) have no master row: "
                   f"{sorted(orphan_subjects)[:3]}")
            ok = False

        if not synth:
            record("expand_recurrences", exp_args, "TOOL_ERROR",
                   f"expansion produced ZERO synthesized occurrences from {len(masters)} "
                   f"master(s) -- the parser silently degraded (its designed behaviour for "
                   f"an unrecognized payload), so expansion is not working on this tenant")
            ok = False
        elif ok:
            sample = [(e.get("subject", "")[:28], e.get("start")) for e in synth[:5]]
            record("expand_recurrences", exp_args, "OK",
                   f"{len(synth)} synthesized occurrence(s) + {len(real)} real row(s) "
                   f"in {elapsed:.1f}s; all in-window, all item_id-less, master intact; "
                   f"sample={sample}")

        record("expand_recurrences (cost)", exp_args, "OK",
               f"{elapsed:.1f}s wall clock for a {len(masters)}-master window -- expansion "
               f"does one GetItem per RecurringMaster in the whole folder")
        return ok


if __name__ == "__main__":
    sys.exit(0 if run(main()) else 1)
