"""Smoke test: find_free_time (availability.py).

Finds free slots in the mailbox's own calendar over the next 7 days.
Read-only, no mailbox state is created or changed, so it's naturally
repeatable.

Run standalone:
    python -m tests.smoke.tests.test_find_free_time
"""

import sys
from datetime import date, timedelta

from tests.smoke.mcp_client import call, run, session
from tests.smoke.results import is_error_payload, record

TODAY = date.today()
ARGS = {
    "start_date": TODAY.isoformat(),
    "end_date": (TODAY + timedelta(days=7)).isoformat(),
    "duration_minutes": 30,
    "start_hour": 9,
    "end_hour": 18,
}


async def main() -> bool:
    async with session() as s:
        info = await call(s, "find_free_time", **ARGS)

        err = is_error_payload(info)
        if err:
            record("find_free_time", ARGS, "EXCEPTION" if "_exception" in str(info) else "TOOL_ERROR", err)
            return False

        if not isinstance(info, dict) or "free_slots" not in info:
            record("find_free_time", ARGS, "EXCEPTION", f"unexpected shape: {info}")
            return False

        # The `timezone` block is recorded, not asserted, and the distinction is
        # the point. The frame these times are in is exactly what was wrong in
        # #601 (naive-UTC busy periods against a local working-day window), and
        # it is not checkable from here: only the mailbox knows its own offset,
        # and a suite that hardcoded one would fail on every other mailbox. What
        # a recorded note *does* buy is that a silent regression to `"source":
        # "utc"` — the pre-fix behaviour, which the fallback chain keeps
        # deliberately reachable — shows up in results.jsonl as a changed line
        # instead of as free slots that merely look plausible.
        tz = info.get("timezone") or {}
        tz_note = (
            f", tz {tz.get('utc_offset', '?')} via {tz.get('source', 'missing')}"
            + (f" [{tz['warning']}]" if tz.get("warning") else "")
        )
        record("find_free_time", ARGS, "OK",
               f"{len(info['free_slots'])} day(s) with free slots{tz_note}")
        return True


if __name__ == "__main__":
    ok = run(main())
    sys.exit(0 if ok else 1)
