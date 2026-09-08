"""Smoke test: create_folder, rename_folder, move_folder, empty_folder,
delete_folder (folders.py).

Chains the five remaining folder.py tools around one disposable,
uniquely-tagged folder:

1. create_folder   - created under Inbox (so move_folder has something
                      real to do).
2. rename_folder    - renamed in place.
3. move_folder      - moved to msgfolderroot. This also matters for
                      step 5: move_email/get_emails resolve a folder
                      *name* via a Shallow FindFolder search rooted at
                      msgfolderroot (see OWAClient.get_folder_id), so
                      the folder has to be a direct child of
                      msgfolderroot before it can be targeted by name.
4. (setup)          - send_email to self, then move_email (both
                      already-verified email.py tools) to drop one
                      disposable tagged message into the test folder,
                      so emptying it proves an item is actually removed
                      rather than emptying a folder that was already empty.
5. empty_folder     - HardDelete the message; verified via get_emails.
6. delete_folder    - HardDelete the now-empty folder (final cleanup).

Repeatable: the folder/email names include a timestamp tag, so re-runs
never collide with a leftover folder from a prior run.

Run standalone:
    python -m tests.smoke.tests.test_folder_lifecycle
"""

import asyncio
import json
import sys
import time

from tests.smoke.mcp_client import call, run, session
from tests.smoke.results import is_error_payload, record


async def _call_create_folder(s, args: dict):
    """Like call(), but for create_folder specifically.

    create_folder's own tool parameter is named `name`, which collides
    with call()'s own `name` parameter (the tool's name) when passed via
    **args -- call(s, "create_folder", **{"name": ...}) raises "got
    multiple values for argument 'name'". Call the tool directly instead.
    """
    result = await s.call_tool("create_folder", args)
    text = "".join(getattr(b, "text", "") for b in result.content)
    if result.isError:
        return {"_transport_error": True, "raw": text}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_non_json": True, "raw": text}

SELF_EMAIL = "lucio.tarantino@unipol.it"
TAG = f"folder-smoke-{int(time.time())}"
FOLDER_NAME = f"[{TAG}]"
RENAMED_FOLDER_NAME = f"[{TAG}]-renamed"
EMAIL_SUBJECT = f"[{TAG}] test email for folder move"

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
        # 1. create_folder (under Inbox)
        create_args = {"name": FOLDER_NAME, "parent_folder_id": "inbox"}
        create_info = await _call_create_folder(s, create_args)
        err = is_error_payload(create_info)
        if err or not isinstance(create_info, dict) or "id" not in create_info:
            record("create_folder", create_args, "EXCEPTION" if "_exception" in str(create_info) else "TOOL_ERROR",
                   err or f"unexpected shape: {create_info}")
            return False
        folder_id = create_info["id"]
        record("create_folder", create_args, "OK", f"id={folder_id}")

        # 2. rename_folder
        rename_args = {"folder_id": folder_id, "new_name": RENAMED_FOLDER_NAME}
        rename_info = await call(s, "rename_folder", **rename_args)
        err = is_error_payload(rename_info)
        if err or not isinstance(rename_info, dict) or "id" not in rename_info:
            record("rename_folder", rename_args, "EXCEPTION" if "_exception" in str(rename_info) else "TOOL_ERROR",
                   err or f"unexpected shape: {rename_info}")
            return False
        folder_id = rename_info["id"]
        record("rename_folder", rename_args, "OK", f"id={folder_id}")

        # 3. move_folder -> msgfolderroot (must be a direct child of
        # msgfolderroot for move_email/get_emails to find it by name below)
        move_folder_args = {"folder_id": folder_id, "target_parent_folder_id": "msgfolderroot"}
        move_folder_info = await call(s, "move_folder", **move_folder_args)
        err = is_error_payload(move_folder_info)
        if err or not isinstance(move_folder_info, dict) or "folder_id" not in move_folder_info:
            record("move_folder", move_folder_args, "EXCEPTION" if "_exception" in str(move_folder_info) else "TOOL_ERROR",
                   err or f"unexpected shape: {move_folder_info}")
            return False
        folder_id = move_folder_info["folder_id"]
        record("move_folder", move_folder_args, "OK", f"id={folder_id}")

        # --- setup: put one disposable tagged email into the test folder ---
        send_args = {"to": SELF_EMAIL, "subject": EMAIL_SUBJECT, "body": "Automated smoke-test message for folder lifecycle testing. Safe to ignore."}
        send_info = await call(s, "send_email", **send_args)
        err = is_error_payload(send_info)
        if err:
            record("send_email (setup)", send_args, "EXCEPTION" if "_exception" in str(send_info) else "TOOL_ERROR", err)
            return False

        item_id = await _find_item_id(s, "Inbox", EMAIL_SUBJECT)
        if not item_id:
            record("send_email (locate)", {"tag": EMAIL_SUBJECT}, "EXCEPTION",
                   f"sent message not found in Inbox after {FIND_ATTEMPTS * FIND_DELAY_SECONDS}s")
            return False

        move_email_args = {"item_ids": [item_id], "target_folder": RENAMED_FOLDER_NAME}
        move_email_info = await call(s, "move_email", **move_email_args)
        err = is_error_payload(move_email_info)
        if err:
            record("move_email (setup)", move_email_args, "EXCEPTION" if "_exception" in str(move_email_info) else "TOOL_ERROR", err)
            return False

        # Confirm the message actually landed in the test folder before
        # emptying it -- otherwise a "0 items" result after empty_folder
        # would prove nothing.
        placed_id = await _find_item_id(s, RENAMED_FOLDER_NAME, EMAIL_SUBJECT)
        if not placed_id:
            record("move_email (verify)", move_email_args, "EXCEPTION",
                   f"tagged message not found in '{RENAMED_FOLDER_NAME}' after move")
            return False

        # 4. empty_folder (permanent) -- should remove the message we just placed
        empty_args = {"folder_id": folder_id, "delete_sub_folders": False, "permanent": True}
        empty_info = await call(s, "empty_folder", **empty_args)
        err = is_error_payload(empty_info)
        if err:
            record("empty_folder", empty_args, "EXCEPTION" if "_exception" in str(empty_info) else "TOOL_ERROR", err)
            return False

        still_there = await _find_item_id(s, RENAMED_FOLDER_NAME, EMAIL_SUBJECT)
        if still_there:
            record("empty_folder", empty_args, "TOOL_ERROR",
                   f"message still present in '{RENAMED_FOLDER_NAME}' after empty_folder")
            return False
        record("empty_folder", empty_args, "OK", "folder emptied, tagged message confirmed gone")

        # 5. delete_folder (permanent, final cleanup)
        delete_args = {"folder_id": folder_id, "permanent": True}
        delete_info = await call(s, "delete_folder", **delete_args)
        err = is_error_payload(delete_info)
        if err:
            record("delete_folder", delete_args, "EXCEPTION" if "_exception" in str(delete_info) else "TOOL_ERROR", err)
            return False
        record("delete_folder", delete_args, "OK", "folder permanently deleted")

        return True


if __name__ == "__main__":
    ok = run(main())
    sys.exit(0 if ok else 1)
