"""Smoke test: find_emails_by_category beyond the first 200 conversations.

NOT YET RUN. Written together with the fix for issue #5 and left for a human to
execute — the unit suite (tests/unit/test_conversation_paging.py) pins the paging
logic, but only a live mailbox can establish that FindConversation still reports
`Categories` on rows fetched at a deep server-side Offset, which is the one fact
the tool's client-side filter depends on and that no fake can prove.

Before the fix, find_emails_by_category issued a single FindConversation at
Offset 0 with MaxEntriesReturned 200 and filtered it client-side, so a category
applied to anything older than the newest 200 conversations could never match
and nothing said so.

**This test tags a real, pre-existing old message**, because a category can only
be deep in the folder if the *message* is — a freshly sent one is always newest,
which is exactly why the existing test_email_category_tagging.py could never
catch this bug. The tag is a unique throwaway name (`SmokeDeepCat-<epoch>`), it
is removed again at the end, and no other property of the message is touched;
still, this is the only smoke module here that writes to mail it did not create,
so read the cleanup section before running it against a mailbox you care about.
If it aborts between tagging and cleanup, remove the category by hand — the
subject and item_id are printed at every step.

Skips (recording OK with nothing asserted) if the folder holds fewer than
DEEP_OFFSET conversations, since there is then no "deep" to test.

Run standalone:
    python -m tests.smoke.tests.test_find_emails_by_category_pagination
"""

import sys
import time

from tests.smoke.mcp_client import call, run, session
from tests.smoke.results import is_error_payload, record

FOLDER = "Inbox"
DEEP_OFFSET = 250            # comfortably past the old 200-row window
CAT = f"SmokeDeepCat-{int(time.time())}"
ABSENT_CAT = f"SmokeAbsentCat-{int(time.time())}"


async def find(s, category, **extra):
    args = {"category": category, "folder": FOLDER, "limit": 10, **extra}
    payload = await call(s, "find_emails_by_category", **args)
    err = is_error_payload(payload)
    if err:
        return None, args, err
    if not isinstance(payload, dict) or "pagination" not in payload:
        return None, args, f"missing pagination block: {str(payload)[:200]}"
    return payload, args, None


async def main() -> bool:
    async with session() as s:
        # ---- 0. Locate a conversation deep in the folder -------------------
        probe_args = {"folder": FOLDER, "limit": 1, "offset": DEEP_OFFSET,
                      "ids_only": True}
        probe = await call(s, "get_emails", **probe_args)
        err = is_error_payload(probe)
        if err:
            record("get_emails (locate deep)", probe_args, "TOOL_ERROR", err)
            return False

        rows = probe.get("item_ids") or []
        if not rows:
            meta = probe.get("pagination", {})
            record("find_emails_by_category", probe_args, "OK",
                   f"{FOLDER} has fewer than {DEEP_OFFSET} conversations "
                   f"(pagination={meta}) - nothing deep to assert")
            return True

        item_id = rows[0].get("item_id")
        subject = rows[0].get("subject", "")
        if not item_id:
            record("get_emails (locate deep)", probe_args, "TOOL_ERROR",
                   f"conversation at offset {DEEP_OFFSET} carries no item_id")
            return False
        print(f"    deep target: offset={DEEP_OFFSET} subject={subject!r} "
              f"item_id={item_id[:40]}...")

        # ---- 1. Tag it, then require the tool to find it -------------------
        assign_args = {"item_ids": [item_id], "categories": [CAT]}
        assign = await call(s, "assign_email_categories", **assign_args)
        err = is_error_payload(assign)
        if err or not isinstance(assign, dict) or not assign.get("success"):
            record("assign_email_categories (setup)", assign_args, "TOOL_ERROR",
                   err or f"unexpected shape: {assign}")
            return False

        ok = True
        try:
            payload, args, err = await find(s, CAT)
            if err:
                record("find_emails_by_category", args, "TOOL_ERROR", err)
                return False

            meta = payload["pagination"]
            if payload["count"] < 1:
                record("find_emails_by_category", args, "TOOL_ERROR",
                       f"category tagged on the conversation at offset "
                       f"{DEEP_OFFSET} was not found: count=0, pagination={meta}"
                       + (" - the scan cap was hit, so raise _CONV_MAX_SCAN or "
                          "pick a shallower target rather than reading this as "
                          "the old bug" if meta.get("error_code") else ""))
                ok = False
            else:
                record("find_emails_by_category", args, "OK",
                       f"found a category on the conversation at offset "
                       f"{DEEP_OFFSET} after scanning "
                       f"{meta['conversations_scanned']} conversation(s) - past "
                       f"the old 200-row window")

            # ---- 2. An absent category is the end, not silence ------------
            payload, args, err = await find(s, ABSENT_CAT)
            if err:
                record("find_emails_by_category", args, "TOOL_ERROR", err)
                ok = False
            elif payload["count"]:
                record("find_emails_by_category", args, "TOOL_ERROR",
                       f"a category that was never assigned matched "
                       f"{payload['count']} conversation(s)")
                ok = False
            else:
                meta = payload["pagination"]
                if meta.get("reached_end_of_folder"):
                    record("find_emails_by_category", args, "OK",
                           f"absent category: empty result reported as a full "
                           f"folder sweep ({meta['conversations_scanned']} "
                           f"conversations, no further matches)")
                elif meta.get("error_code"):
                    record("find_emails_by_category", args, "OK",
                           f"absent category: empty result explained as "
                           f"{meta['error_code']} - stopped looking, not "
                           f"end of folder (folder is larger than the scan cap)")
                else:
                    record("find_emails_by_category", args, "TOOL_ERROR",
                           f"empty result explains nothing - the ambiguity this "
                           f"fix removes: {meta}")
                    ok = False

            # ---- 3. offset skips matches rather than restarting ------------
            payload, args, err = await find(s, CAT, offset=1)
            if err:
                record("find_emails_by_category", args, "TOOL_ERROR", err)
                ok = False
            elif payload["count"]:
                record("find_emails_by_category", args, "TOOL_ERROR",
                       f"offset=1 returned {payload['count']} row(s) for a "
                       f"category assigned to exactly one conversation")
                ok = False
            else:
                record("find_emails_by_category", args, "OK",
                       "offset=1 correctly skips the single match "
                       f"(pagination={payload['pagination']})")

        finally:
            # ---- cleanup: untag the borrowed message ----------------------
            remove_args = {"item_ids": [item_id], "categories": [CAT]}
            removed = await call(s, "remove_email_categories", **remove_args)
            err = is_error_payload(removed)
            if err or not isinstance(removed, dict) or not removed.get("success"):
                record("remove_email_categories (cleanup)", remove_args,
                       "TOOL_ERROR",
                       f"COULD NOT UNTAG - remove '{CAT}' from {subject!r} by "
                       f"hand: {err or removed}")
                ok = False
            else:
                record("remove_email_categories (cleanup)", remove_args, "OK",
                       f"'{CAT}' removed from the borrowed message")

        return ok


if __name__ == "__main__":
    ok = run(main())
    sys.exit(0 if ok else 1)
