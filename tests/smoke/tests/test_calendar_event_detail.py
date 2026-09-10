"""Smoke test: get_calendar_event (#211) + categories on get_calendar_events (#201).

Covers two related gaps closed together:
  * get_calendar_events dropped each item's Categories even though the OWA
    response already carried them -- assert the field now round-trips.
  * there was no per-event detail tool at all; get_calendar_event(item_id)
    now exposes the previously-internal _get_event_details helper.

Creates one disposable appointment, tags it with a category that already
exists in the mailbox (read via list_categories, so nothing extra needs
cleaning up), asserts both tools report that category, then cancels the
appointment.

Run standalone:
    python -m tests.smoke.tests.test_calendar_event_detail
"""

import sys
import time
from datetime import date, timedelta

from tests.smoke.mcp_client import call, run, session
from tests.smoke.results import is_error_payload, record

TAG = f"[cal-detail-smoke-{int(time.time())}]"
SUBJECT = f"{TAG} Exchange MCP get_calendar_event smoke test"
MEETING_DATE = (date.today() + timedelta(days=1)).isoformat()
LOCATION = "Smoke Test Room"


async def main() -> bool:
    ok = True
    async with session() as s:
        # 0. Pick a category that already exists, so this test doesn't have to
        #    create/delete one just to have something to assert on.
        # list_categories returns a bare JSON array of {"name", "color"} objects,
        # not a {"categories": [...]} wrapper -- handle both so this test can't
        # silently skip its own key assertion again.
        cats = await call(s, "list_categories")
        rows = cats if isinstance(cats, list) else (cats or {}).get("categories", [])
        names = [c.get("name") for c in rows
                 if isinstance(c, dict) and c.get("name")]
        category = names[0] if names else None
        if category is None:
            record("list_categories", {}, "TOOL_ERROR",
                   f"no existing categories to tag with; category assertions SKIPPED "
                   f"-- this test then proves nothing about #201 ({str(cats)[:150]})")
        else:
            record("list_categories", {}, "OK",
                   f"{len(names)} category/ies available; tagging with {category!r}")

        # 1. Create a disposable appointment inside the query window.
        create_args = {
            "subject": SUBJECT,
            "date": MEETING_DATE,
            "start_time": "15:00",
            "duration_minutes": 30,
            "location": LOCATION,
        }
        create_info = await call(s, "create_meeting", **create_args)
        err = is_error_payload(create_info)
        if err or not isinstance(create_info, dict) or "item_id" not in create_info:
            record("create_meeting", create_args,
                   "EXCEPTION" if "_exception" in str(create_info) else "TOOL_ERROR",
                   err or f"unexpected shape: {create_info}")
            return False
        item_id = create_info["item_id"]

        try:
            # 2. Tag it, so there is a known non-empty categories value.
            if category:
                tag_args = {"item_ids": [item_id], "categories": [category]}
                tag_info = await call(s, "assign_event_categories", **tag_args)
                err = is_error_payload(tag_info)
                if err:
                    record("assign_event_categories", tag_args,
                           "EXCEPTION" if "_exception" in str(tag_info) else "TOOL_ERROR", err)
                    category = None  # don't assert on a tag that failed to apply

            # 3. get_calendar_events must now include a categories field (#201).
            list_args = {"start_date": MEETING_DATE, "end_date": MEETING_DATE,
                         "include_body": False}
            events = await call(s, "get_calendar_events", **list_args)
            err = is_error_payload(events)
            if err or not isinstance(events, list):
                record("get_calendar_events", list_args,
                       "EXCEPTION" if "_exception" in str(events) else "TOOL_ERROR",
                       err or f"unexpected shape: {events}")
                ok = False
            else:
                match = next((e for e in events if e.get("item_id") == item_id), None)
                if match is None:
                    record("get_calendar_events", list_args, "TOOL_ERROR",
                           f"created appointment (item_id={item_id}) not among {len(events)} event(s)")
                    ok = False
                elif "categories" not in match:
                    record("get_calendar_events", list_args, "TOOL_ERROR",
                           "event dict has no 'categories' key -- the #201 mapping fix is not in effect")
                    ok = False
                elif category and category not in match["categories"]:
                    record("get_calendar_events", list_args, "TOOL_ERROR",
                           f"categories={match['categories']!r} does not contain the assigned {category!r}")
                    ok = False
                else:
                    record("get_calendar_events", list_args, "OK",
                           f"categories present: {match['categories']!r}")

            # 4. get_calendar_event detail tool (#211).
            detail_args = {"item_id": item_id}
            detail = await call(s, "get_calendar_event", **detail_args)
            err = is_error_payload(detail)
            if err or not isinstance(detail, dict):
                record("get_calendar_event", detail_args,
                       "EXCEPTION" if "_exception" in str(detail) else "TOOL_ERROR",
                       err or f"unexpected shape: {detail}")
                return False

            missing = [k for k in ("subject", "start", "end", "categories", "organizer",
                                   "item_id", "calendar_item_type", "change_key")
                       if k not in detail]
            if missing:
                record("get_calendar_event", detail_args, "TOOL_ERROR",
                       f"missing expected keys: {missing}")
                ok = False
            elif TAG not in (detail.get("subject") or ""):
                record("get_calendar_event", detail_args, "TOOL_ERROR",
                       f"subject mismatch: {detail.get('subject')!r}")
                ok = False
            elif category and category not in (detail.get("categories") or []):
                record("get_calendar_event", detail_args, "TOOL_ERROR",
                       f"categories={detail.get('categories')!r} missing assigned {category!r}")
                ok = False
            else:
                record("get_calendar_event", detail_args, "OK",
                       f"subject/start/end/categories all present; "
                       f"start={detail.get('start')!r} categories={detail.get('categories')!r} "
                       f"change_key={'set' if detail.get('change_key') else 'EMPTY'}")

            # 5. A bogus item_id must fail cleanly, not raise.
            bad = await call(s, "get_calendar_event", item_id="not-a-real-item-id")
            if isinstance(bad, dict) and (bad.get("error") or bad.get("_transport_error")):
                record("get_calendar_event", {"item_id": "not-a-real-item-id"}, "OK",
                       "invalid item_id rejected cleanly")
            else:
                record("get_calendar_event", {"item_id": "not-a-real-item-id"}, "TOOL_ERROR",
                       f"expected an error payload for a bogus id, got: {str(bad)[:200]}")
                ok = False

            return ok
        finally:
            cancel_info = await call(s, "cancel_meeting", item_id=item_id,
                                     message="Auto-cancelled by smoke test.")
            err = is_error_payload(cancel_info)
            if err:
                record("cancel_meeting", {"item_id": item_id},
                       "EXCEPTION" if "_exception" in str(cancel_info) else "TOOL_ERROR", err)


if __name__ == "__main__":
    sys.exit(0 if run(main()) else 1)
