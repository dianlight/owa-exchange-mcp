"""Smoke test: get_meeting_stats (analytics.py).

Gets meeting-count statistics for the mailbox's own address over the
past 7 days. Read-only, no mailbox state is created or changed, so
it's naturally repeatable.

Run standalone:
    python -m tests.smoke.tests.test_get_meeting_stats
"""

import sys
from datetime import date, timedelta

from tests.smoke.mcp_client import call, run, session
from tests.smoke.results import is_error_payload, record

TODAY = date.today()
SELF_EMAIL = "lucio.tarantino@unipol.it"
ARGS = {
    "people": SELF_EMAIL,
    "start_date": (TODAY - timedelta(days=7)).isoformat(),
    "end_date": TODAY.isoformat(),
}


async def main() -> bool:
    async with session() as s:
        info = await call(s, "get_meeting_stats", **ARGS)

        err = is_error_payload(info)
        if err:
            record("get_meeting_stats", ARGS, "EXCEPTION" if "_exception" in str(info) else "TOOL_ERROR", err)
            return False

        if not isinstance(info, dict) or "stats" not in info:
            record("get_meeting_stats", ARGS, "EXCEPTION", f"unexpected shape: {info}")
            return False

        note = f"{len(info['stats'])} person/people in stats"
        warnings = info.get("warnings")
        if warnings:
            note += f"; warnings: {warnings}"
        record("get_meeting_stats", ARGS, "OK", note)
        return True


if __name__ == "__main__":
    ok = run(main())
    sys.exit(0 if ok else 1)
