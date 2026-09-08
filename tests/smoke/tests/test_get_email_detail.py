"""Smoke test: get_email + get_email_links (email.py).

Fetches one real item_id from Inbox via get_emails(ids_only=True), then
reads it with get_email and get_email_links. Read-only, no mailbox state
is created or changed, so it's naturally repeatable. If Inbox is empty,
this test is skipped (reported OK with a note) rather than failed.

Run standalone:
    python -m tests.smoke.tests.test_get_email_detail
"""

import sys

from tests.smoke.mcp_client import call, run, session
from tests.smoke.results import is_error_payload, record


async def main() -> bool:
    async with session() as s:
        ids_info = await call(s, "get_emails", folder="Inbox", limit=1, ids_only=True)
        err = is_error_payload(ids_info)
        if err or not isinstance(ids_info, dict):
            record("get_emails", {"ids_only": True}, "EXCEPTION", err or str(ids_info))
            return False

        item_ids = ids_info.get("item_ids", [])
        if not item_ids:
            record("get_email", {}, "OK", "skipped: Inbox has no messages to sample")
            record("get_email_links", {}, "OK", "skipped: Inbox has no messages to sample")
            return True

        item_id = item_ids[0]["item_id"]

        email_info = await call(s, "get_email", item_id=item_id)
        err = is_error_payload(email_info)
        if err or not isinstance(email_info, dict) or "subject" not in email_info:
            record("get_email", {"item_id": item_id}, "EXCEPTION", err or f"unexpected shape: {email_info}")
            return False
        record("get_email", {"item_id": item_id}, "OK",
               f"subject={email_info.get('subject')!r} attachments={len(email_info.get('attachments', []))}")

        links_info = await call(s, "get_email_links", item_id=item_id)
        err = is_error_payload(links_info)
        if err or not isinstance(links_info, dict) or "links" not in links_info:
            record("get_email_links", {"item_id": item_id}, "EXCEPTION", err or f"unexpected shape: {links_info}")
            return False
        record("get_email_links", {"item_id": item_id}, "OK", f"{links_info['count']} link(s) found")

        return True


if __name__ == "__main__":
    ok = run(main())
    sys.exit(0 if ok else 1)
