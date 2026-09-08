"""Smoke test: find_meeting_time (availability.py).

Finds common free slots for a single-attendee list (the mailbox's own
address) over the next 7 days. Using only the self address keeps this
read-only and avoids querying a real colleague's availability without
their mailbox being part of this test run. Naturally repeatable.

Run standalone:
    python -m tests.smoke.tests.test_find_meeting_time
"""

import sys
from datetime import date, timedelta

from tests.smoke.mcp_client import call, run, session
from tests.smoke.results import is_error_payload, record

TODAY = date.today()
SELF_EMAIL = "lucio.tarantino@unipol.it"
ARGS = {
    "emails": SELF_EMAIL,
    "start_date": TODAY.isoformat(),
    "end_date": (TODAY + timedelta(days=7)).isoformat(),
    "duration_minutes": 30,
    "start_hour": 9,
    "end_hour": 18,
}


async def main() -> bool:
    async with session() as s:
        info = await call(s, "find_meeting_time", **ARGS)

        err = is_error_payload(info)
        if err:
            record("find_meeting_time", ARGS, "EXCEPTION" if "_exception" in str(info) else "TOOL_ERROR", err)
            return False

        if not isinstance(info, dict) or "free_slots" not in info or "attendees" not in info:
            record("find_meeting_time", ARGS, "EXCEPTION", f"unexpected shape: {info}")
            return False

        record("find_meeting_time", ARGS, "OK",
               f"{len(info['attendees'])} attendee(s), {len(info['free_slots'])} day(s) with free slots")
        return True


if __name__ == "__main__":
    ok = run(main())
    sys.exit(0 if ok else 1)
