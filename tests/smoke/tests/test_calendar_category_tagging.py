"""Smoke test: assign_event_categories, remove_event_categories,
find_events_by_category (calendar.py).

Chains all three around one disposable, uniquely-tagged self-invited
meeting: create -> assign two categories -> find by one of them (verify
present) -> remove one category (verify gone, other kept) -> cleanup
(cancel the meeting).

Repeatable: the subject/category tags include a timestamp, so re-runs
never collide with a leftover meeting from a prior run.

Run standalone:
    python -m tests.smoke.tests.test_calendar_category_tagging
"""

import sys
import time
from datetime import date, timedelta

from tests.smoke.mcp_client import call, run, session
from tests.smoke.results import is_error_payload, record

SELF_EMAIL = "lucio.tarantino@unipol.it"
TAG = f"[cal-cat-smoke-{int(time.time())}]"
SUBJECT = f"{TAG} Exchange MCP category smoke test meeting"
MEETING_DATE = (date.today() + timedelta(days=1)).isoformat()
CAT_A = f"SmokeTestCatA-{int(time.time())}"
CAT_B = f"SmokeTestCatB-{int(time.time())}"


async def main() -> bool:
    async with session() as s:
        # --- setup: create a disposable tagged meeting ---
        create_args = {
            "subject": SUBJECT,
            "date": MEETING_DATE,
            "start_time": "16:00",
            "duration_minutes": 30,
            "required_attendees": [SELF_EMAIL],
            "description": "Automated smoke-test meeting for category tagging. Safe to ignore/cancel.",
        }
        create_info = await call(s, "create_meeting", **create_args)
        err = is_error_payload(create_info)
        if err or not isinstance(create_info, dict) or "item_id" not in create_info:
            record("create_meeting (setup)", create_args, "EXCEPTION" if "_exception" in str(create_info) else "TOOL_ERROR",
                   err or f"unexpected shape: {create_info}")
            return False
        item_id = create_info["item_id"]

        # 1. assign_event_categories
        assign_args = {"item_ids": [item_id], "categories": [CAT_A, CAT_B]}
        assign_info = await call(s, "assign_event_categories", **assign_args)
        err = is_error_payload(assign_info)
        if err or not isinstance(assign_info, dict) or not assign_info.get("success"):
            record("assign_event_categories", assign_args, "EXCEPTION" if "_exception" in str(assign_info) else "TOOL_ERROR",
                   err or f"unexpected shape: {assign_info}")
            return False
        record("assign_event_categories", assign_args, "OK", assign_info.get("message", ""))

        # 2. find_events_by_category -- should find the tagged meeting under CAT_A
        find_args = {"category": CAT_A, "start_date": MEETING_DATE, "end_date": MEETING_DATE, "limit": 10}
        find_info = await call(s, "find_events_by_category", **find_args)
        err = is_error_payload(find_info)
        if err or not isinstance(find_info, dict) or "events" not in find_info:
            record("find_events_by_category", find_args, "EXCEPTION" if "_exception" in str(find_info) else "TOOL_ERROR",
                   err or f"unexpected shape: {find_info}")
            return False
        if find_info["count"] < 1:
            record("find_events_by_category", find_args, "TOOL_ERROR",
                   f"expected at least 1 match for '{CAT_A}', got {find_info['count']}")
            return False
        record("find_events_by_category", find_args, "OK", f"{find_info['count']} match(es) found")

        # 3. remove_event_categories -- remove CAT_A, keep CAT_B
        remove_args = {"item_ids": [item_id], "categories": [CAT_A]}
        remove_info = await call(s, "remove_event_categories", **remove_args)
        err = is_error_payload(remove_info)
        if err or not isinstance(remove_info, dict) or not remove_info.get("success"):
            record("remove_event_categories", remove_args, "EXCEPTION" if "_exception" in str(remove_info) else "TOOL_ERROR",
                   err or f"unexpected shape: {remove_info}")
            return False

        # Verify: CAT_A gone, CAT_B still there
        recheck_info = await call(s, "find_events_by_category", category=CAT_A, start_date=MEETING_DATE, end_date=MEETING_DATE, limit=10)
        if is_error_payload(recheck_info):
            record("remove_event_categories (verify)", {"category": CAT_A}, "EXCEPTION" if "_exception" in str(recheck_info) else "TOOL_ERROR",
                   is_error_payload(recheck_info))
            return False
        if recheck_info.get("count", 0) > 0:
            record("remove_event_categories", remove_args, "TOOL_ERROR",
                   f"'{CAT_A}' still found after removal ({recheck_info['count']} match(es))")
            return False

        keep_info = await call(s, "find_events_by_category", category=CAT_B, start_date=MEETING_DATE, end_date=MEETING_DATE, limit=10)
        if is_error_payload(keep_info) or keep_info.get("count", 0) < 1:
            record("remove_event_categories", remove_args, "TOOL_ERROR",
                   f"'{CAT_B}' unexpectedly missing after removing only '{CAT_A}'")
            return False
        record("remove_event_categories", remove_args, "OK", f"'{CAT_A}' removed, '{CAT_B}' kept, both verified")

        # --- cleanup: cancel the disposable test meeting ---
        cancel_args = {"item_id": item_id, "message": "Auto-cancelled by smoke test."}
        cancel_info = await call(s, "cancel_meeting", **cancel_args)
        if is_error_payload(cancel_info):
            record("cancel_meeting (cleanup)", cancel_args, "TOOL_ERROR", is_error_payload(cancel_info))
        else:
            record("cancel_meeting (cleanup)", cancel_args, "OK", cancel_info.get("message", ""))

        return True


if __name__ == "__main__":
    ok = run(main())
    sys.exit(0 if ok else 1)
