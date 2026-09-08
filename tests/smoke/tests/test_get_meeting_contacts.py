"""Smoke test: get_meeting_contacts (analytics.py).

Builds the "who you meet with most" connection matrix from the
mailbox's own calendar over the past 30 days. Read-only, no mailbox
state is created or changed, so it's naturally repeatable.

Run standalone:
    python -m tests.smoke.tests.test_get_meeting_contacts
"""

import sys
from datetime import date, timedelta

from tests.smoke.mcp_client import call, run, session
from tests.smoke.results import is_error_payload, record

TODAY = date.today()
ARGS = {
    "start_date": (TODAY - timedelta(days=30)).isoformat(),
    "end_date": TODAY.isoformat(),
    "top_n": 10,
}


async def main() -> bool:
    async with session() as s:
        info = await call(s, "get_meeting_contacts", **ARGS)

        err = is_error_payload(info)
        if err:
            record("get_meeting_contacts", ARGS, "EXCEPTION" if "_exception" in str(info) else "TOOL_ERROR", err)
            return False

        if not isinstance(info, dict) or "contacts" not in info:
            record("get_meeting_contacts", ARGS, "EXCEPTION", f"unexpected shape: {info}")
            return False

        note = f"{info.get('unique_contacts', 0)} unique contact(s), {len(info['contacts'])} returned"
        warnings = info.get("warnings")
        if warnings:
            note += f"; warnings: {warnings}"
        record("get_meeting_contacts", ARGS, "OK", note)
        return True


if __name__ == "__main__":
    ok = run(main())
    sys.exit(0 if ok else 1)
