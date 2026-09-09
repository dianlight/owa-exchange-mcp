"""Regression test for a real bug report: move_email (and get_emails) couldn't
resolve destination folders nested more than one level deep, either by full
path (e.g. "Projects/ClientFolder") or by bare name (e.g. "ClientFolder") --
"Folder not found" on every attempt. Also seen with a folder nested one
level under Inbox ("Triage").

Root cause: OWAClient.get_folder_id() only ran a Shallow FindFolder rooted
at msgfolderroot with a literal string match, so it never saw folders that
aren't direct children of msgfolderroot, and had no path-splitting logic at
all for "/"-delimited paths. Fixed in owa_client.py by walking a "/"-delimited
path one Shallow FindFolder per segment (_resolve_folder_path /
_find_child_folder_id), starting from a distinguished folder (e.g. "Inbox")
when the first segment names one, otherwise from msgfolderroot.

This test reproduces both shapes from the report against disposable,
uniquely-tagged folders instead of the real folders from the report:

1. A folder nested under another custom folder (mirrors "Projects/ClientFolder"):
   [tag]-parent/[tag]-child, both created fresh under msgfolderroot/parent.
2. A folder nested under a distinguished folder (mirrors "Inbox/Triage"):
   Inbox/[tag]-inboxchild.

For each, a disposable tagged email is moved into the nested folder via its
full "/" path and the move is verified by finding the message there
afterward. Cleanup deletes the top-level folder in each case (HardDelete of
a folder deletes its subfolders and contents too), rather than deleting the
moved email separately.

Run standalone:
    python -m tests.smoke.tests.test_move_email_nested_folder
"""

import asyncio
import json
import sys
import time

from tests.smoke.mcp_client import call, run, session
from tests.smoke.results import is_error_payload, record

SELF_EMAIL = "lucio.tarantino@unipol.it"
TAG = f"nested-smoke-{int(time.time())}"
PARENT_NAME = f"[{TAG}]-parent"
CHILD_NAME = f"[{TAG}]-child"
INBOX_CHILD_NAME = f"[{TAG}]-inboxchild"
SUBJECT_A = f"[{TAG}] nested custom-folder move"
SUBJECT_B = f"[{TAG}] nested inbox-folder move"

FIND_ATTEMPTS = 6
FIND_DELAY_SECONDS = 10


async def _call_create_folder(s, args: dict):
    """create_folder's own `name` parameter collides with call()'s tool-name
    parameter when passed via **args -- call the tool directly instead."""
    result = await s.call_tool("create_folder", args)
    text = "".join(getattr(b, "text", "") for b in result.content)
    if result.isError:
        return {"_transport_error": True, "raw": text}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_non_json": True, "raw": text}


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


async def _send_and_locate(s, subject: str) -> str | None:
    send_args = {"to": SELF_EMAIL, "subject": subject, "body": "Automated smoke-test message for nested-folder move testing. Safe to ignore."}
    send_info = await call(s, "send_email", **send_args)
    err = is_error_payload(send_info)
    if err:
        record("send_email (setup)", send_args, "EXCEPTION" if "_exception" in str(send_info) else "TOOL_ERROR", err)
        return None
    return await _find_item_id(s, "Inbox", subject)


async def main() -> bool:
    async with session() as s:
        # --- setup: build the two nested-folder shapes from the report ---

        # 1a. [tag]-parent directly under msgfolderroot
        parent_args = {"name": PARENT_NAME, "parent_folder_id": "msgfolderroot"}
        parent_info = await _call_create_folder(s, parent_args)
        err = is_error_payload(parent_info)
        if err or not isinstance(parent_info, dict) or "id" not in parent_info:
            record("create_folder (parent)", parent_args, "EXCEPTION" if "_exception" in str(parent_info) else "TOOL_ERROR",
                   err or f"unexpected shape: {parent_info}")
            return False
        parent_id = parent_info["id"]
        record("create_folder (parent)", parent_args, "OK", f"id={parent_id}")

        # 1b. [tag]-child nested under [tag]-parent -- mirrors Projects/ClientFolder
        child_args = {"name": CHILD_NAME, "parent_folder_id": parent_id}
        child_info = await _call_create_folder(s, child_args)
        err = is_error_payload(child_info)
        if err or not isinstance(child_info, dict) or "id" not in child_info:
            record("create_folder (child)", child_args, "EXCEPTION" if "_exception" in str(child_info) else "TOOL_ERROR",
                   err or f"unexpected shape: {child_info}")
            return False
        record("create_folder (child)", child_args, "OK", f"id={child_info['id']}")

        # 1c. [tag]-inboxchild directly under Inbox -- mirrors Inbox/Triage
        inbox_child_args = {"name": INBOX_CHILD_NAME, "parent_folder_id": "inbox"}
        inbox_child_info = await _call_create_folder(s, inbox_child_args)
        err = is_error_payload(inbox_child_info)
        if err or not isinstance(inbox_child_info, dict) or "id" not in inbox_child_info:
            record("create_folder (inbox child)", inbox_child_args, "EXCEPTION" if "_exception" in str(inbox_child_info) else "TOOL_ERROR",
                   err or f"unexpected shape: {inbox_child_info}")
            return False
        inbox_child_id = inbox_child_info["id"]
        record("create_folder (inbox child)", inbox_child_args, "OK", f"id={inbox_child_id}")

        ok = True

        # --- case A: move_email to "[tag]-parent/[tag]-child" ---
        item_a = await _send_and_locate(s, SUBJECT_A)
        if not item_a:
            record("send_email (locate A)", {"tag": SUBJECT_A}, "EXCEPTION",
                   f"sent message not found in Inbox after {FIND_ATTEMPTS * FIND_DELAY_SECONDS}s")
            ok = False
        else:
            target_a = f"{PARENT_NAME}/{CHILD_NAME}"
            move_args_a = {"item_ids": [item_a], "target_folder": target_a}
            move_info_a = await call(s, "move_email", **move_args_a)
            err = is_error_payload(move_info_a)
            if err:
                record("move_email (nested custom path)", move_args_a,
                       "EXCEPTION" if "_exception" in str(move_info_a) else "TOOL_ERROR", err)
                ok = False
            else:
                landed_a = await _find_item_id(s, target_a, SUBJECT_A)
                if not landed_a:
                    record("move_email (nested custom path)", move_args_a, "TOOL_ERROR",
                           f"reported success but message not found in '{target_a}' afterward")
                    ok = False
                else:
                    record("move_email (nested custom path)", move_args_a, "OK",
                           f"moved into '{target_a}' and confirmed present")

        # --- case B: move_email to "Inbox/[tag]-inboxchild" ---
        item_b = await _send_and_locate(s, SUBJECT_B)
        if not item_b:
            record("send_email (locate B)", {"tag": SUBJECT_B}, "EXCEPTION",
                   f"sent message not found in Inbox after {FIND_ATTEMPTS * FIND_DELAY_SECONDS}s")
            ok = False
        else:
            target_b = f"Inbox/{INBOX_CHILD_NAME}"
            move_args_b = {"item_ids": [item_b], "target_folder": target_b}
            move_info_b = await call(s, "move_email", **move_args_b)
            err = is_error_payload(move_info_b)
            if err:
                record("move_email (nested inbox path)", move_args_b,
                       "EXCEPTION" if "_exception" in str(move_info_b) else "TOOL_ERROR", err)
                ok = False
            else:
                landed_b = await _find_item_id(s, target_b, SUBJECT_B)
                if not landed_b:
                    record("move_email (nested inbox path)", move_args_b, "TOOL_ERROR",
                           f"reported success but message not found in '{target_b}' afterward")
                    ok = False
                else:
                    record("move_email (nested inbox path)", move_args_b, "OK",
                           f"moved into '{target_b}' and confirmed present")

        # --- cleanup: delete the top-level folders (cascades to subfolders/contents) ---
        del_parent = await call(s, "delete_folder", folder_id=parent_id, permanent=True)
        err = is_error_payload(del_parent)
        if err:
            record("delete_folder (parent, cleanup)", {"folder_id": parent_id}, "TOOL_ERROR", err)
            ok = False
        else:
            record("delete_folder (parent, cleanup)", {"folder_id": parent_id}, "OK", "parent+child subtree deleted")

        del_inbox_child = await call(s, "delete_folder", folder_id=inbox_child_id, permanent=True)
        err = is_error_payload(del_inbox_child)
        if err:
            record("delete_folder (inbox child, cleanup)", {"folder_id": inbox_child_id}, "TOOL_ERROR", err)
            ok = False
        else:
            record("delete_folder (inbox child, cleanup)", {"folder_id": inbox_child_id}, "OK", "folder deleted")

        return ok


if __name__ == "__main__":
    ok = run(main())
    sys.exit(0 if ok else 1)
