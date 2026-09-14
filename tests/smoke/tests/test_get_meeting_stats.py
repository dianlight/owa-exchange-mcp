"""Smoke test: get_meeting_stats (analytics.py).

Gets meeting-count statistics for the mailbox's own address over the
past 7 days. Read-only, no mailbox state is created or changed, so
it's naturally repeatable.

Needs the mailbox's own address in $EXCHANGE_SMOKE_SELF_EMAIL (see
tests/smoke/config.py for why it isn't written down here).

Run standalone:
    EXCHANGE_SMOKE_SELF_EMAIL=you@example.com \
        python -m tests.smoke.tests.test_get_meeting_stats
"""

import sys
from datetime import date, timedelta

from tests.smoke.config import require_self_email
from tests.smoke.mcp_client import call, run, session
from tests.smoke.results import is_error_payload, record

TODAY = date.today()


async def main() -> bool:
    # args is built here rather than at module level: the address comes from the
    # environment, and an import-time failure couldn't be recorded.
    self_email = require_self_email("get_meeting_stats")
    if not self_email:
        return False

    args = {
        "people": self_email,
        "start_date": (TODAY - timedelta(days=7)).isoformat(),
        "end_date": TODAY.isoformat(),
    }

    async with session() as s:
        info = await call(s, "get_meeting_stats", **args)

        err = is_error_payload(info)
        if err:
            record("get_meeting_stats", args, "EXCEPTION" if "_exception" in str(info) else "TOOL_ERROR", err)
            return False

        if not isinstance(info, dict) or "stats" not in info:
            record("get_meeting_stats", args, "EXCEPTION", f"unexpected shape: {info}")
            return False

        note = f"{len(info['stats'])} person/people in stats"
        warnings = info.get("warnings")
        if warnings:
            note += f"; warnings: {warnings}"
        record("get_meeting_stats", args, "OK", note)
        return True


if __name__ == "__main__":
    ok = run(main())
    sys.exit(0 if ok else 1)
