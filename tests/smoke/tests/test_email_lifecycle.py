"""Smoke test: send_email, reply_email, forward_email, mark_email_read,
move_email, download_attachments, delete_email (email.py).

Chains all six mutating email tools around one disposable, uniquely-tagged
message sent to the mailbox's own address, so each run creates fresh state
and self-cleans at the end (HardDelete) rather than accumulating junk.

Repeatable: the subject tag includes a timestamp, so re-runs never collide
with a leftover message from a prior run; the final delete_email step
removes the message (and its replies/forwards) it created.

Run standalone:
    python -m tests.smoke.tests.test_email_lifecycle
"""

import asyncio
import sys
import time

from tests.smoke.mcp_client import call, run, session
from tests.smoke.results import is_error_payload, record

SELF_EMAIL = "lucio.tarantino@unipol.it"
TAG = f"[smoke-test-{int(time.time())}]"
SUBJECT = f"{TAG} Exchange MCP smoke test"
BODY = "Automated smoke-test message from the exchange-mcp test suite. Safe to ignore/delete."

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


async def _find_all_tagged(s, folders: list[str], subject_substr: str) -> list[str]:
    """Sweep every message matching the tag across the given folders.

    Sending a test message to yourself creates more than one physical item
    for one logical "test email" - the Inbox copy, the reply/forward Sent
    Items copies, etc. - so a single item_id from _find_item_id() is not
    enough to fully clean up; every tool call here can add another item
    under the same subject tag.
    """
    ids = []
    for folder in folders:
        info = await call(s, "get_emails", folder=folder, limit=15, ids_only=True)
        if isinstance(info, dict):
            for item in info.get("item_ids", []):
                if subject_substr in item.get("subject", ""):
                    ids.append(item["item_id"])
    return ids


async def main() -> bool:
    async with session() as s:
        # 1. send_email
        send_args = {"to": SELF_EMAIL, "subject": SUBJECT, "body": BODY}
        send_info = await call(s, "send_email", **send_args)
        err = is_error_payload(send_info)
        if err:
            record("send_email", send_args, "EXCEPTION" if "_exception" in str(send_info) else "TOOL_ERROR", err)
            return False
        record("send_email", send_args, "OK", "sent")

        # Locate the message we just sent in Inbox (self-delivery isn't instant).
        item_id = await _find_item_id(s, "Inbox", TAG)
        if not item_id:
            record("send_email (locate)", {"tag": TAG}, "EXCEPTION",
                   f"sent message not found in Inbox after {FIND_ATTEMPTS * FIND_DELAY_SECONDS}s")
            return False

        # 2. reply_email
        reply_args = {"item_id": item_id, "body": "Automated reply from smoke test.", "reply_all": False}
        reply_info = await call(s, "reply_email", **reply_args)
        err = is_error_payload(reply_info)
        if err:
            record("reply_email", reply_args, "EXCEPTION" if "_exception" in str(reply_info) else "TOOL_ERROR", err)
        else:
            record("reply_email", reply_args, "OK", "reply sent")

        # 3. forward_email
        fwd_args = {"item_id": item_id, "to": SELF_EMAIL, "body": "Automated forward from smoke test."}
        fwd_info = await call(s, "forward_email", **fwd_args)
        err = is_error_payload(fwd_info)
        if err:
            record("forward_email", fwd_args, "EXCEPTION" if "_exception" in str(fwd_info) else "TOOL_ERROR", err)
        else:
            record("forward_email", fwd_args, "OK", "forwarded")

        # 4. mark_email_read
        mark_args = {"item_ids": [item_id], "is_read": True}
        mark_info = await call(s, "mark_email_read", **mark_args)
        err = is_error_payload(mark_info)
        if err:
            record("mark_email_read", mark_args, "EXCEPTION" if "_exception" in str(mark_info) else "TOOL_ERROR", err)
        else:
            record("mark_email_read", mark_args, "OK", mark_info.get("message", ""))

        # 5. download_attachments (expect none -- that's a valid pass)
        dl_args = {"item_id": item_id, "target_folder": "tests/smoke/.state/attachments"}
        dl_info = await call(s, "download_attachments", **dl_args)
        err = is_error_payload(dl_info)
        if err:
            record("download_attachments", dl_args, "EXCEPTION" if "_exception" in str(dl_info) else "TOOL_ERROR", err)
        else:
            record("download_attachments", dl_args, "OK", dl_info.get("message", f"{dl_info.get('count', 0)} file(s)"))

        # 6. move_email -> Deleted Items (the "Deleted" alias, per DISTINGUISHED_FOLDERS)
        move_args = {"item_ids": [item_id], "target_folder": "Deleted"}
        move_info = await call(s, "move_email", **move_args)
        err = is_error_payload(move_info)
        if err:
            record("move_email", move_args, "EXCEPTION" if "_exception" in str(move_info) else "TOOL_ERROR", err)
            # Can't safely locate/clean up further; stop here.
            return False
        record("move_email", move_args, "OK", move_info.get("message", ""))

        # 7. delete_email (HardDelete) -- sweep every item tagged by this run
        # across Inbox/Sent/Deleted repeatedly: reply_email/forward_email
        # sent to ourselves round-trip back through mail delivery
        # asynchronously (unlike the synchronous Sent Items copy from
        # SendAndSaveCopy), so a late-arriving copy can still show up after
        # an earlier sweep already looked clean. Stop once a full pass finds
        # nothing left to delete.
        all_deleted = []
        sweeps_run = 0
        for _ in range(FIND_ATTEMPTS):
            await asyncio.sleep(FIND_DELAY_SECONDS)
            sweeps_run += 1
            cleanup_ids = await _find_all_tagged(s, ["Inbox", "Sent", "Deleted"], TAG)
            if not cleanup_ids:
                break
            del_args = {"item_ids": cleanup_ids, "permanent": True}
            del_info = await call(s, "delete_email", **del_args)
            err = is_error_payload(del_info)
            if err:
                record("delete_email", del_args, "EXCEPTION" if "_exception" in str(del_info) else "TOOL_ERROR", err)
                return False
            all_deleted.extend(cleanup_ids)
        else:
            record("delete_email (locate)", {"tag": TAG}, "TOOL_ERROR",
                   f"still finding tagged messages after {FIND_ATTEMPTS} sweeps -- may need manual cleanup")

        if not all_deleted:
            record("delete_email (locate)", {"tag": TAG}, "EXCEPTION",
                   "no tagged messages found to clean up -- left un-cleaned, please delete manually")
            return False

        record("delete_email", {"item_ids": all_deleted, "permanent": True}, "OK",
               f"{len(all_deleted)} email(s) permanently deleted across {sweeps_run} sweep(s)")

        return True


if __name__ == "__main__":
    ok = run(main())
    sys.exit(0 if ok else 1)
