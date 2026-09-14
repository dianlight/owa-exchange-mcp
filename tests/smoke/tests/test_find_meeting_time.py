"""Smoke test: find_meeting_time (availability.py).

Finds common free slots for a single-attendee list (the mailbox's own
address) over the next 7 days. Using only the self address keeps this
read-only and avoids querying a real colleague's availability without
their mailbox being part of this test run. Naturally repeatable.

Needs the mailbox's own address in $EXCHANGE_SMOKE_SELF_EMAIL (see
tests/smoke/config.py for why it isn't written down here).

Run standalone:
    EXCHANGE_SMOKE_SELF_EMAIL=you@example.com \
        python -m tests.smoke.tests.test_find_meeting_time
"""

import sys
from datetime import date, timedelta

from tests.smoke.config import require_self_email
from tests.smoke.mcp_client import call, run, session
from tests.smoke.results import is_error_payload, record

TODAY = date.today()


async def main() -> bool:
    # args is built here rather than at module level: the attendee address comes
    # from the environment, and an import-time failure couldn't be recorded.
    self_email = require_self_email("find_meeting_time")
    if not self_email:
        return False

    args = {
        "emails": self_email,
        "start_date": TODAY.isoformat(),
        "end_date": (TODAY + timedelta(days=7)).isoformat(),
        "duration_minutes": 30,
        "start_hour": 9,
        "end_hour": 18,
    }

    async with session() as s:
        info = await call(s, "find_meeting_time", **args)

        err = is_error_payload(info)
        if err:
            record("find_meeting_time", args, "EXCEPTION" if "_exception" in str(info) else "TOOL_ERROR", err)
            return False

        if not isinstance(info, dict) or "free_slots" not in info or "attendees" not in info:
            record("find_meeting_time", args, "EXCEPTION", f"unexpected shape: {info}")
            return False

        record("find_meeting_time", args, "OK",
               f"{len(info['attendees'])} attendee(s), {len(info['free_slots'])} day(s) with free slots")
        return True


if __name__ == "__main__":
    ok = run(main())
    sys.exit(0 if ok else 1)
