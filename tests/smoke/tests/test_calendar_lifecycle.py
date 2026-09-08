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

Run standalone:
    python -m tests.smoke.tests.test_calendar_lifecycle
"""

import sys
import time
from datetime import date, timedelta

from tests.smoke.mcp_client import call, run, session
from tests.smoke.results import is_error_payload, record

SELF_EMAIL = "lucio.tarantino@unipol.it"
TAG = f"[cal-smoke-{int(time.time())}]"
SUBJECT = f"{TAG} Exchange MCP smoke test meeting"
MEETING_DATE = (date.today() + timedelta(days=1)).isoformat()


async def main() -> bool:
    async with session() as s:
        # 1. create_meeting
        create_args = {
            "subject": SUBJECT,
            "date": MEETING_DATE,
            "start_time": "14:00",
            "duration_minutes": 30,
            "required_attendees": [SELF_EMAIL],
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
        record("create_meeting", create_args, "OK", f"item_id={organizer_item_id}")

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
