"""Smoke test: get_calendar_events (calendar.py).

Lists events in a fixed date window. Read-only, no mailbox state is
created or changed, so it's naturally repeatable.

Run standalone:
    python -m tests.smoke.tests.test_get_calendar_events
"""

import sys
from datetime import date, timedelta

from tests.smoke.mcp_client import call, run, session
from tests.smoke.results import is_error_payload, record

TODAY = date.today()
ARGS = {
    "start_date": (TODAY - timedelta(days=7)).isoformat(),
    "end_date": (TODAY + timedelta(days=7)).isoformat(),
    "include_body": True,
    "expand_recurring": False,
}


async def main() -> bool:
    async with session() as s:
        info = await call(s, "get_calendar_events", **ARGS)

        err = is_error_payload(info)
        if err:
            record("get_calendar_events", ARGS, "EXCEPTION" if "_exception" in str(info) else "TOOL_ERROR", err)
            return False

        if not isinstance(info, list):
            record("get_calendar_events", ARGS, "EXCEPTION", f"unexpected shape: {info}")
            return False

        record("get_calendar_events", ARGS, "OK", f"{len(info)} event(s) returned")
        return True


if __name__ == "__main__":
    ok = run(main())
    sys.exit(0 if ok else 1)
