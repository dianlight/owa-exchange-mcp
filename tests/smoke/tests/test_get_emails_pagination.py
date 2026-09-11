"""Smoke test: get_emails deep pagination (email.py).

Pins the one backend fact the paging rewrite rests on and that no unit test can
establish: FindConversation honours a server-side IndexedPageView.Offset on this
tenant, and it agrees with a from-zero enumeration.

Before the rewrite, get_emails applied `offset` by slicing a single response
window whose server Offset was hardcoded to 0, so any offset past that window
(80 for limit=20, 50 for limit=5) returned {"emails": [], "count": 0} —
indistinguishable from the end of the folder.

Read-only, no mailbox state is created or changed, so it's naturally repeatable.
New mail arriving mid-run shifts every conversation's position by one, so the
alignment check below tolerates a small drift rather than failing on it.

Run standalone:
    python -m tests.smoke.tests.test_get_emails_pagination
"""

import sys

from tests.smoke.mcp_client import call, run, session
from tests.smoke.results import is_error_payload, record

FOLDER = "Inbox"
PAGE = 20
DEEP_OFFSET = 240      # the offset the bug report saw return zero rows
DRIFT_TOLERANCE = 3    # conversations that may arrive/move during the run

ARGS = {"folder": FOLDER, "limit": PAGE, "ids_only": True}


def cids(payload) -> list[str]:
    return [row.get("conversation_id", "") for row in payload.get("item_ids", [])]


async def fetch(s, offset, limit=PAGE):
    args = {"folder": FOLDER, "limit": limit, "offset": offset, "ids_only": True}
    payload = await call(s, "get_emails", **args)
    err = is_error_payload(payload)
    if err:
        return None, args, err
    if not isinstance(payload, dict) or "pagination" not in payload:
        return None, args, f"missing pagination block: {str(payload)[:200]}"
    return payload, args, None


async def main() -> bool:
    async with session() as s:
        # ---- 1. A server-side offset agrees with a from-zero enumeration ----
        wide, args, err = await fetch(s, 0, limit=PAGE * 2)
        if err:
            record("get_emails", args, "TOOL_ERROR", err)
            return False

        wide_ids = cids(wide)
        if len(wide_ids) < PAGE * 2:
            record("get_emails", args, "OK",
                   f"Inbox has only {len(wide_ids)} conversations - too few to "
                   f"exercise deep pagination; nothing asserted")
            return True

        second, args, err = await fetch(s, PAGE)
        if err:
            record("get_emails", args, "TOOL_ERROR", err)
            return False

        second_ids = cids(second)
        if not second_ids:
            record("get_emails", args, "TOOL_ERROR",
                   f"offset={PAGE} returned nothing while offset=0 returned "
                   f"{len(wide_ids)} conversations")
            return False

        # Where does the offset=20 page actually start inside the 40-row read?
        if second_ids[0] in wide_ids:
            start = wide_ids.index(second_ids[0])
        else:
            start = None

        if start is None or abs(start - PAGE) > DRIFT_TOLERANCE:
            record("get_emails", args, "TOOL_ERROR",
                   f"offset={PAGE} page starts at index {start} of the offset=0 "
                   f"read, expected ~{PAGE}: server-side Offset is not honoured")
            return False

        overlap = wide_ids[start:start + PAGE]
        if second_ids[:len(overlap)] != overlap[:len(second_ids)]:
            record("get_emails", args, "TOOL_ERROR",
                   f"offset={PAGE} page is not the matching slice of the "
                   f"offset=0 read (aligned at {start})")
            return False

        record("get_emails", args, "OK",
               f"server-side offset aligns with from-zero enumeration "
               f"(offset={PAGE} page found at index {start})")

        # ---- 2. A deep offset either returns rows or says why it doesn't ----
        deep, args, err = await fetch(s, DEEP_OFFSET)
        if err:
            record("get_emails", args, "TOOL_ERROR", err)
            return False

        meta = deep["pagination"]
        deep_ids = cids(deep)

        if meta.get("error_code"):
            record("get_emails", args, "TOOL_ERROR",
                   f"paging stopped early: {meta['error_code']} - {meta.get('error')}")
            return False

        if deep_ids:
            if set(deep_ids) & set(wide_ids):
                record("get_emails", args, "TOOL_ERROR",
                       f"offset={DEEP_OFFSET} returned conversations that are "
                       f"also in the offset=0 page - offset ignored")
                return False
            record("get_emails", args, "OK",
                   f"offset={DEEP_OFFSET} returned {len(deep_ids)} conversation(s), "
                   f"none overlapping page 0; has_more={meta['has_more']}")
        elif meta.get("reached_end_of_folder"):
            record("get_emails", args, "OK",
                   f"offset={DEEP_OFFSET} empty, reported as genuine end of folder "
                   f"after scanning {meta['conversations_scanned']} conversation(s)")
        else:
            record("get_emails", args, "TOOL_ERROR",
                   f"offset={DEEP_OFFSET} empty with neither an error_code nor "
                   f"reached_end_of_folder - the ambiguity this fix removes: {meta}")
            return False

        # ---- 3. The empty page past the end is unambiguous ----
        far, args, err = await fetch(s, 100000)
        if err:
            record("get_emails", args, "TOOL_ERROR", err)
            return False

        far_meta = far["pagination"]
        if cids(far):
            record("get_emails", args, "TOOL_ERROR",
                   f"offset=100000 returned conversations: {len(cids(far))}")
            return False
        if not (far_meta.get("reached_end_of_folder") or far_meta.get("error_code")):
            record("get_emails", args, "TOOL_ERROR",
                   f"empty page past the end explains nothing: {far_meta}")
            return False

        record("get_emails", args, "OK",
               f"empty page past the end is self-describing "
               f"(reached_end_of_folder={far_meta.get('reached_end_of_folder')}, "
               f"error_code={far_meta.get('error_code')})")

        # ---- 4. Walking the folder page by page yields no repeats ----
        seen: set[str] = set()
        offset = 0
        pages = 0
        while pages < 6:
            page, args, err = await fetch(s, offset)
            if err:
                record("get_emails", args, "TOOL_ERROR", err)
                return False
            page_ids = cids(page)
            dupes = seen & set(page_ids)
            if dupes:
                record("get_emails", args, "TOOL_ERROR",
                       f"offset={offset} repeated {len(dupes)} conversation(s) "
                       f"from an earlier page")
                return False
            seen.update(page_ids)
            pages += 1

            meta = page["pagination"]
            if meta.get("error_code"):
                record("get_emails", args, "TOOL_ERROR",
                       f"walk stopped at offset={offset}: {meta['error_code']}")
                return False
            if not meta["has_more"]:
                break
            offset = meta["next_offset"]

        record("get_emails", {"folder": FOLDER, "limit": PAGE, "walk": pages},
               "OK", f"walked {pages} page(s), {len(seen)} distinct conversations, "
                     f"no repeats")
        return True


if __name__ == "__main__":
    ok = run(main())
    sys.exit(0 if ok else 1)
