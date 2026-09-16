"""Smoke test: create_meeting, get_event_links, download_event_attachments,
update_meeting, cancel_meeting (calendar.py).

Chains five of the six remaining calendar tools around one disposable,
uniquely-tagged meeting created on the mailbox's own calendar with
itself as the sole required attendee.

respond_to_meeting is NOT exercised by this test: confirmed by direct
observation (2026-09-08) that self-inviting produces no meeting-request
email at all -- Exchange doesn't ask an organizer to accept their own
invite, since the item already lives directly on their calendar. This
tool can only be verified manually, against a real incoming invite from
a different mailbox/account (see PROJECT_STATUS.md for that result).

cancel_meeting only supports DeleteType=MoveToDeletedItems (the tool
has no HardDelete option), so the cancelled occurrence is expected to
remain in Deleted Items -- that's a property of the tool, not a test
bug.

Repeatable: the subject tag includes a timestamp, so re-runs never
collide with a leftover meeting from a prior run.

Needs the mailbox's own address in $EXCHANGE_SMOKE_SELF_EMAIL (see
tests/smoke/config.py for why it isn't written down here) -- it is the sole
required attendee, which is what makes the meeting self-invited.

Run standalone:
    EXCHANGE_SMOKE_SELF_EMAIL=you@example.com \
        python -m tests.smoke.tests.test_calendar_lifecycle
"""

import sys
import time
from datetime import date, timedelta

from tests.smoke.config import require_self_email
from tests.smoke.mcp_client import call, run, session
from tests.smoke.results import is_error_payload, record

TAG = f"[cal-smoke-{int(time.time())}]"
SUBJECT = f"{TAG} Exchange MCP smoke test meeting"
MEETING_DATE = (date.today() + timedelta(days=1)).isoformat()


async def main() -> bool:
    self_email = require_self_email("create_meeting")
    if not self_email:
        return False

    async with session() as s:
        # 1. create_meeting
        create_args = {
            "subject": SUBJECT,
            "date": MEETING_DATE,
            "start_time": "14:00",
            "duration_minutes": 30,
            "required_attendees": [self_email],
            "location": "Test Room",
            "description": "Automated smoke-test meeting from the exchange-mcp test suite. Safe to ignore/cancel.",
        }
        create_info = await call(s, "create_meeting", **create_args)
        err = is_error_payload(create_info)
        if err or not isinstance(create_info, dict) or "item_id" not in create_info:
            record("create_meeting", create_args, "EXCEPTION" if "_exception" in str(create_info) else "TOOL_ERROR",
                   err or f"unexpected shape: {create_info}")
            return False
        organizer_item_id = create_info["item_id"]

        # Read the meeting back and compare the *time*, which this suite never
        # did. That omission is why `create_meeting` sat at OK while it was
        # storing meetings 1-2 hours early: the write sends Start/End as an
        # unqualified wall clock, so whatever TimeZoneContext goes with it decides
        # the instant, and that context was a hardcoded UTC+3 (issue #8, fixed
        # 2026-09-16). Asking for 14:00 in a W. Europe mailbox produced 13:00 in
        # summer and 12:00 in winter -- a meeting that plainly existed, at a
        # plausible-looking hour, so every check short of comparing the number
        # passed.
        #
        # Compared in UTC rather than against "14:00": get_calendar_events returns
        # Z-suffixed instants, and re-deriving the expected local wall clock here
        # would mean this test carrying its own copy of the timezone logic it is
        # supposed to be checking. The mailbox's offset comes from the tool that
        # reports it.
        tz_probe = await call(s, "find_free_time", start_date=MEETING_DATE)
        offset = (tz_probe or {}).get("timezone", {}).get("utc_offset", "")
        events = await call(s, "get_calendar_events", start_date=MEETING_DATE,
                            end_date=MEETING_DATE, include_body=False)
        actual_start = ""
        if isinstance(events, list):
            for ev in events:
                if TAG in ev.get("subject", ""):
                    actual_start = ev.get("start", "")
                    break

        note = f"item_id={organizer_item_id}"
        if not actual_start:
            note += "; WARNING could not read the meeting back to check its time"
        elif offset and len(offset) == 6:
            sign, hh, mm = offset[0], int(offset[1:3]), int(offset[4:6])
            delta = (hh * 60 + mm) * (1 if sign == "+" else -1)
            asked = 14 * 60
            got_utc_hhmm = actual_start[11:16]
            got_local = (int(got_utc_hhmm[:2]) * 60 + int(got_utc_hhmm[3:5]) + delta) % (24 * 60)
            verdict = "matches" if got_local == asked else "MISMATCH"
            note += (f"; asked 14:00, stored {got_utc_hhmm}Z, mailbox offset {offset} "
                     f"-> {got_local // 60:02d}:{got_local % 60:02d} local ({verdict})")
            if got_local != asked:
                record("create_meeting", create_args, "TOOL_ERROR", note)
                return False
        else:
            note += f"; stored {actual_start} (no mailbox offset available to check it against)"
        record("create_meeting", create_args, "OK", note)

        # 2. get_event_links
        links_args = {"item_id": organizer_item_id}
        links_info = await call(s, "get_event_links", **links_args)
        err = is_error_payload(links_info)
        if err or not isinstance(links_info, dict) or "links" not in links_info:
            record("get_event_links", links_args, "EXCEPTION" if "_exception" in str(links_info) else "TOOL_ERROR",
                   err or f"unexpected shape: {links_info}")
        else:
            record("get_event_links", links_args, "OK", f"{links_info['count']} link(s) found")

        # 3. download_event_attachments (expect none -- that's a valid pass)
        dl_args = {"item_id": organizer_item_id, "target_folder": "tests/smoke/.state/attachments"}
        dl_info = await call(s, "download_event_attachments", **dl_args)
        err = is_error_payload(dl_info)
        if err:
            record("download_event_attachments", dl_args, "EXCEPTION" if "_exception" in str(dl_info) else "TOOL_ERROR", err)
        else:
            record("download_event_attachments", dl_args, "OK",
                   dl_info.get("message", f"{dl_info.get('count', 0)} file(s)"))

        # 4. update_meeting (cancel + recreate internally -- item_id changes)
        update_args = {"item_id": organizer_item_id, "start_time": "15:00", "location": "Updated Test Room"}
        update_info = await call(s, "update_meeting", **update_args)
        err = is_error_payload(update_info)
        if err or not isinstance(update_info, dict) or "item_id" not in update_info:
            record("update_meeting", update_args, "EXCEPTION" if "_exception" in str(update_info) else "TOOL_ERROR",
                   err or f"unexpected shape: {update_info}")
            # Can't reliably locate the meeting to cancel afterwards; stop here.
            return False
        new_item_id = update_info["item_id"]
        record("update_meeting", update_args, "OK", f"new item_id={new_item_id}")

        # 5. cancel_meeting (soft-delete -- no HardDelete option on this tool;
        # the cancelled occurrence is expected to remain in Deleted Items)
        cancel_args = {"item_id": new_item_id, "message": "Auto-cancelled by smoke test."}
        cancel_info = await call(s, "cancel_meeting", **cancel_args)
        err = is_error_payload(cancel_info)
        if err:
            record("cancel_meeting", cancel_args, "EXCEPTION" if "_exception" in str(cancel_info) else "TOOL_ERROR", err)
            return False
        record("cancel_meeting", cancel_args, "OK", cancel_info.get("message", ""))

        return True


if __name__ == "__main__":
    ok = run(main())
    sys.exit(0 if ok else 1)
