"""Smoke test: get_calendar_events (calendar.py).

Creates a disposable, uniquely-tagged appointment inside the query window
and asserts it comes back out of get_calendar_events by item_id. A plain
"did it return a list" check previously let a real regression slip through
undetected -- get_calendar_events returned an empty list for every date
range (see PROJECT_STATUS.md note), and the old assertion ("is it a list")
was satisfied by an empty list just as much as a correct one.

Run standalone:
    python -m tests.smoke.tests.test_get_calendar_events
"""

import sys
import time
from datetime import date, timedelta

from tests.smoke.mcp_client import call, run, session
from tests.smoke.results import is_error_payload, record

TAG = f"[cal-events-smoke-{int(time.time())}]"
SUBJECT = f"{TAG} Exchange MCP get_calendar_events smoke test"
MEETING_DATE = (date.today() + timedelta(days=1)).isoformat()
# Deliberately a single day, not a wide window: this mailbox has hundreds of
# real events per week, and include_body=True does one GetItem round-trip
# per event, so a wide window here would make the smoke suite slow for no
# extra coverage -- the point of this test is just that a known event in a
# small range comes back, not mailbox-wide performance.
ARGS = {
    "start_date": MEETING_DATE,
    "end_date": MEETING_DATE,
    "include_body": True,
}


async def main() -> bool:
    async with session() as s:
        # 1. Create a known appointment inside the query window.
        create_args = {
            "subject": SUBJECT,
            "date": MEETING_DATE,
            "start_time": "16:00",
            "duration_minutes": 30,
        }
        create_info = await call(s, "create_meeting", **create_args)
        err = is_error_payload(create_info)
        if err or not isinstance(create_info, dict) or "item_id" not in create_info:
            record("create_meeting", create_args, "EXCEPTION" if "_exception" in str(create_info) else "TOOL_ERROR",
                   err or f"unexpected shape: {create_info}")
            return False
        item_id = create_info["item_id"]

        try:
            # 2. get_calendar_events over a window containing the appointment.
            info = await call(s, "get_calendar_events", **ARGS)

            err = is_error_payload(info)
            if err:
                record("get_calendar_events", ARGS, "EXCEPTION" if "_exception" in str(info) else "TOOL_ERROR", err)
                return False

            if not isinstance(info, list):
                record("get_calendar_events", ARGS, "EXCEPTION", f"unexpected shape: {info}")
                return False

            match = next((ev for ev in info if ev.get("item_id") == item_id), None)
            if match is None:
                record("get_calendar_events", ARGS, "TOOL_ERROR",
                       f"created appointment (item_id={item_id}) not found among {len(info)} event(s) returned")
                return False

            record("get_calendar_events", ARGS, "OK",
                   f"{len(info)} event(s) returned, including the known test appointment")
            return True
        finally:
            # 3. Clean up the test appointment regardless of outcome above.
            cancel_info = await call(s, "cancel_meeting", item_id=item_id, message="Auto-cancelled by smoke test.")
            err = is_error_payload(cancel_info)
            if err:
                record("cancel_meeting", {"item_id": item_id}, "EXCEPTION" if "_exception" in str(cancel_info) else "TOOL_ERROR", err)


if __name__ == "__main__":
    ok = run(main())
    sys.exit(0 if ok else 1)
