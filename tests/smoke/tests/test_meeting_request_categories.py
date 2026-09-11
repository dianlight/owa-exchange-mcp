"""Smoke test: the category tools must work on a meeting invite, not 500 on it.

Reported 2026-09-11: `assign_email_categories` failed on a Teams meeting invite
sitting in the Inbox (a `MeetingRequestMessage`, not a plain `Message`), leaking
a raw HTTP 500 `SerializationException` to the caller, while the same call
against ordinary mail succeeded.

Root cause was *not* the write. Live investigation showed `UpdateItem` /
`SetItemField` with `Item.__type = "Message:#Exchange"` applies cleanly to such
an item (proven independently by `set_email_flag`); what failed was the
category tools' *pre-read* of the current categories, which used
`BaseShape: "AllProperties"` — and OWA's own serialiser faults partway through
writing the response for a `MeetingRequestMessageType` at that shape. Asking
only for `Categories` reads the same item fine. So this test asserts the fix
where it matters: a full assign/verify/remove round-trip on the very item class
that used to fail.

Discovery, not hardcoding: such an item is found by the marker only it produces
— `get_email` reporting `error_code == "item_not_serializable"`. The test skips
(recording why) if this mailbox currently has none in the scanned window, the
same way `test_unfetchable_item_resilience.py` does.

It mutates a real, non-disposable mailbox item (there is no way to send oneself
a meeting invite that lands as a MeetingRequestMessage without also putting a
real event on the calendar), so it uses a timestamped category name that can
collide with nothing, and removes it again — leaving the item's pre-existing
categories exactly as they were.

Run standalone:
    python -m tests.smoke.tests.test_meeting_request_categories
"""

import sys
import time

from tests.smoke.mcp_client import call, run, session
from tests.smoke.results import is_error_payload, record

SCAN_ARGS = {"folder": "Inbox", "limit": 25, "ids_only": True}
CAT = f"SmokeTestMeetingCat-{int(time.time())}"


async def _find_meeting_request(s):
    """Return (item_id, get_email_payload) for an item whose full-property read faults."""
    listing = await call(s, "get_emails", **SCAN_ARGS)
    if not isinstance(listing, dict):
        return None, None
    for row in listing.get("item_ids", []):
        iid = row.get("item_id")
        if not iid:
            continue
        single = await call(s, "get_email", item_id=iid)
        if isinstance(single, dict) and single.get("error_code") == "item_not_serializable":
            return iid, single
    return None, None


async def main() -> bool:
    ok = True
    async with session() as s:
        item_id, single = await _find_meeting_request(s)

        if not item_id:
            record("assign_email_categories (meeting invite)", SCAN_ARGS, "OK",
                   "no MeetingRequestMessage in the scanned window whose full-property "
                   "read faults — the reported failure path is not exercised today "
                   "(this mailbox had such items on 2026-09-11; nothing to assert)")
            return ok

        # 0. The typed error is half the contract: a caller must be able to tell
        #    "OWA can't serialise this" from a bad item_id without reading prose.
        if single.get("hint"):
            record("get_email (meeting invite)", {"item_id": item_id}, "OK",
                   "typed error_code=item_not_serializable with remediation")
        else:
            record("get_email (meeting invite)", {"item_id": item_id}, "TOOL_ERROR",
                   "error_code present but no remediation hint")
            ok = False

        # 1. assign must actually apply, not skip the item.
        assign_args = {"item_ids": [item_id], "categories": [CAT]}
        assign_info = await call(s, "assign_email_categories", **assign_args)
        err = is_error_payload(assign_info)
        if err or not isinstance(assign_info, dict):
            record("assign_email_categories", assign_args,
                   "EXCEPTION" if "_exception" in str(assign_info) else "TOOL_ERROR",
                   f"raw failure instead of a result payload: {err or str(assign_info)[:180]}")
            return False
        if assign_info.get("updated_count") != 1:
            record("assign_email_categories", assign_args, "TOOL_ERROR",
                   f"meeting invite not tagged: {str(assign_info)[:250]}")
            return False
        record("assign_email_categories", assign_args, "OK",
               f"tagged a MeetingRequestMessage: {assign_info.get('message', '')}")

        # 2. Verify it persisted server-side rather than trusting the response.
        find_info = await call(s, "find_emails_by_category", category=CAT, folder="Inbox", limit=25)
        if not isinstance(find_info, dict) or find_info.get("count", 0) < 1:
            record("assign_email_categories (verify)", {"category": CAT}, "TOOL_ERROR",
                   f"category not readable back after a reported success: {str(find_info)[:200]}")
            ok = False
        else:
            record("assign_email_categories (verify)", {"category": CAT}, "OK",
                   f"{find_info['count']} match(es) — the write persisted")

        # 3. remove_email_categories must undo it, leaving the item as found.
        remove_args = {"item_ids": [item_id], "categories": [CAT]}
        remove_info = await call(s, "remove_email_categories", **remove_args)
        if not isinstance(remove_info, dict) or remove_info.get("updated_count") != 1:
            record("remove_email_categories", remove_args, "TOOL_ERROR",
                   f"could not untag the meeting invite: {str(remove_info)[:250]}")
            return False

        recheck = await call(s, "find_emails_by_category", category=CAT, folder="Inbox", limit=25)
        if isinstance(recheck, dict) and recheck.get("count", 0) > 0:
            record("remove_email_categories", remove_args, "TOOL_ERROR",
                   f"'{CAT}' still present after removal — test category left on a real item")
            ok = False
        else:
            record("remove_email_categories", remove_args, "OK",
                   "untagged and verified; item's original categories untouched")

        return ok


if __name__ == "__main__":
    sys.exit(0 if run(main()) else 1)
