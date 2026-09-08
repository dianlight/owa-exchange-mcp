"""Smoke test: assign_email_categories, remove_email_categories,
find_emails_by_category (email.py).

Chains all three around one disposable, uniquely-tagged self-addressed
email: send -> assign two categories -> find by one of them (verify
present) -> remove one category (verify gone, other kept) -> cleanup
(permanently delete the email). No category_* cleanup is needed --
these tools just tag the item's Categories field with plain strings,
they never touch the master category list (see categories.py).

Repeatable: the subject/category tags include a timestamp, so re-runs
never collide with a leftover message from a prior run.

Run standalone:
    python -m tests.smoke.tests.test_email_category_tagging
"""

import asyncio
import sys
import time

from tests.smoke.mcp_client import call, run, session
from tests.smoke.results import is_error_payload, record

SELF_EMAIL = "lucio.tarantino@unipol.it"
TAG = f"[email-cat-smoke-{int(time.time())}]"
SUBJECT = f"{TAG} Exchange MCP category smoke test"
CAT_A = f"SmokeTestCatA-{int(time.time())}"
CAT_B = f"SmokeTestCatB-{int(time.time())}"

FIND_ATTEMPTS = 6
FIND_DELAY_SECONDS = 10


async def _find_item_id(s, folder: str, subject_substr: str):
    for attempt in range(FIND_ATTEMPTS):
        info = await call(s, "get_emails", folder=folder, limit=10, ids_only=True)
        if isinstance(info, dict):
            for item in info.get("item_ids", []):
                if subject_substr in item.get("subject", ""):
                    return item["item_id"]
        if attempt < FIND_ATTEMPTS - 1:
            await asyncio.sleep(FIND_DELAY_SECONDS)
    return None


async def main() -> bool:
    async with session() as s:
        # --- setup: send a disposable tagged email to self ---
        send_args = {
            "to": SELF_EMAIL,
            "subject": SUBJECT,
            "body": "Automated smoke-test message for category tagging. Safe to ignore/delete.",
        }
        send_info = await call(s, "send_email", **send_args)
        err = is_error_payload(send_info)
        if err:
            record("send_email (setup)", send_args, "EXCEPTION" if "_exception" in str(send_info) else "TOOL_ERROR", err)
            return False

        item_id = await _find_item_id(s, "Inbox", TAG)
        if not item_id:
            record("send_email (locate)", {"tag": TAG}, "EXCEPTION",
                   f"sent message not found in Inbox after {FIND_ATTEMPTS * FIND_DELAY_SECONDS}s")
            return False

        # 1. assign_email_categories
        assign_args = {"item_ids": [item_id], "categories": [CAT_A, CAT_B]}
        assign_info = await call(s, "assign_email_categories", **assign_args)
        err = is_error_payload(assign_info)
        if err or not isinstance(assign_info, dict) or not assign_info.get("success"):
            record("assign_email_categories", assign_args, "EXCEPTION" if "_exception" in str(assign_info) else "TOOL_ERROR",
                   err or f"unexpected shape: {assign_info}")
            return False
        record("assign_email_categories", assign_args, "OK", assign_info.get("message", ""))

        # 2. find_emails_by_category -- should find the tagged email under CAT_A
        find_args = {"category": CAT_A, "folder": "Inbox", "limit": 10}
        find_info = await call(s, "find_emails_by_category", **find_args)
        err = is_error_payload(find_info)
        if err or not isinstance(find_info, dict) or "emails" not in find_info:
            record("find_emails_by_category", find_args, "EXCEPTION" if "_exception" in str(find_info) else "TOOL_ERROR",
                   err or f"unexpected shape: {find_info}")
            return False
        if find_info["count"] < 1:
            record("find_emails_by_category", find_args, "TOOL_ERROR",
                   f"expected at least 1 match for '{CAT_A}', got {find_info['count']}")
            return False
        record("find_emails_by_category", find_args, "OK", f"{find_info['count']} match(es) found")

        # 3. remove_email_categories -- remove CAT_A, keep CAT_B
        remove_args = {"item_ids": [item_id], "categories": [CAT_A]}
        remove_info = await call(s, "remove_email_categories", **remove_args)
        err = is_error_payload(remove_info)
        if err or not isinstance(remove_info, dict) or not remove_info.get("success"):
            record("remove_email_categories", remove_args, "EXCEPTION" if "_exception" in str(remove_info) else "TOOL_ERROR",
                   err or f"unexpected shape: {remove_info}")
            return False

        # Verify: CAT_A gone, CAT_B still there
        recheck_info = await call(s, "find_emails_by_category", category=CAT_A, folder="Inbox", limit=10)
        if is_error_payload(recheck_info):
            record("remove_email_categories (verify)", {"category": CAT_A}, "EXCEPTION" if "_exception" in str(recheck_info) else "TOOL_ERROR",
                   is_error_payload(recheck_info))
            return False
        if recheck_info.get("count", 0) > 0:
            record("remove_email_categories", remove_args, "TOOL_ERROR",
                   f"'{CAT_A}' still found after removal ({recheck_info['count']} match(es))")
            return False

        keep_info = await call(s, "find_emails_by_category", category=CAT_B, folder="Inbox", limit=10)
        if is_error_payload(keep_info) or keep_info.get("count", 0) < 1:
            record("remove_email_categories", remove_args, "TOOL_ERROR",
                   f"'{CAT_B}' unexpectedly missing after removing only '{CAT_A}'")
            return False
        record("remove_email_categories", remove_args, "OK", f"'{CAT_A}' removed, '{CAT_B}' kept, both verified")

        # --- cleanup: permanently delete the disposable test email ---
        del_args = {"item_ids": [item_id], "permanent": True}
        del_info = await call(s, "delete_email", **del_args)
        if is_error_payload(del_info):
            record("delete_email (cleanup)", del_args, "TOOL_ERROR", is_error_payload(del_info))
        else:
            record("delete_email (cleanup)", del_args, "OK", "test email cleaned up")

        return True


if __name__ == "__main__":
    ok = run(main())
    sys.exit(0 if ok else 1)
