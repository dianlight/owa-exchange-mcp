"""Regression test for a bug report from the unattended `inbox-maintenance-hourly`
task: `move_email` could not reach a custom folder either way round.

    move_email(item_ids=[...], target_folder="Quarantena")
        -> {"error": "Folder 'Quarantena' not found."}
    move_email(item_ids=[...], target_folder="<exact id from get_folders>")
        -> {"error": "Folder '<that id>' not found."}

Two independent causes, confirmed live on the reporting mailbox:

1. "Quarantena" is a child of the **Inbox**, not a top-level folder — it only
   looked top-level because `get_folders(parent_folder_id="msgfolderroot",
   recursive=True)` flattens the tree. `get_folder_id()`'s bare-name lookup
   searched direct children of `msgfolderroot` only, so it never saw it.
2. EWS folder ids are base64 and routinely contain "/", and the resolver split
   on "/" to walk a path *before* testing whether the string was an id — so a
   perfectly valid id was chopped into path segments that match nothing.

Fixed in owa_client.py by `resolve_folder()`: id first (before any "/"
handling), then path, then distinguished name, then top-level name, then Inbox
child, then a unique match anywhere in the mailbox — with several matches
reported as `folder_name_ambiguous` plus candidate ids instead of guessed at.

This test covers each accepted form against disposable, uniquely-tagged
folders rather than the real ones from the report:

A. top-level custom folder, by **name**
B. the same folder, by **id** (the form the report needed most: it is the one
   that can't be defeated by naming or nesting)
C. Inbox-child custom folder, by bare **name** — the reported shape
D. two folders sharing a name, by that name -> refused with
   `error_code: "folder_name_ambiguous"` and both candidate ids, and then
   reachable via one of those ids

Cleanup hard-deletes the disposable top-level folders (which takes their
subfolders and contents with them) plus the Inbox child.

Needs the signed-in mailbox's own address, since each case moves a message it
sends to itself. That comes from `EXCHANGE_SMOKE_SELF_EMAIL` rather than a
constant in the file: this repo is public, and a mailbox address is the one
thing in a smoke test that has no business being committed to it.

Run standalone:
    EXCHANGE_SMOKE_SELF_EMAIL=you@example.com \
        python -m tests.smoke.tests.test_move_email_custom_folder
"""

import asyncio
import os
import sys
import time

from tests.smoke.mcp_client import call, call_args, run, session
from tests.smoke.results import is_error_payload, record

SELF_EMAIL = os.environ.get("EXCHANGE_SMOKE_SELF_EMAIL", "")
TAG = f"custom-folder-smoke-{int(time.time())}"

TOP_LEVEL_NAME = f"[{TAG}]-toplevel"
INBOX_CHILD_NAME = f"[{TAG}]-inboxchild"
DUP_PARENT_A = f"[{TAG}]-dupA"
DUP_PARENT_B = f"[{TAG}]-dupB"
DUP_CHILD_NAME = f"[{TAG}]-shared"

FIND_ATTEMPTS = 6
FIND_DELAY_SECONDS = 10


async def _find_item_id(s, folder: str, subject_substr: str):
    """Locate a message by subject in `folder` (which may be a name, path or id)."""
    for attempt in range(FIND_ATTEMPTS):
        info = await call(s, "get_emails", folder=folder, limit=20, ids_only=True)
        if isinstance(info, dict):
            for item in info.get("item_ids", []):
                if subject_substr in item.get("subject", ""):
                    return item["item_id"]
        if attempt < FIND_ATTEMPTS - 1:
            await asyncio.sleep(FIND_DELAY_SECONDS)
    return None


async def _send_and_locate(s, subject: str) -> str | None:
    send_args = {
        "to": SELF_EMAIL,
        "subject": subject,
        "body": "Automated smoke-test message for custom-folder move testing. Safe to ignore.",
    }
    send_info = await call(s, "send_email", **send_args)
    err = is_error_payload(send_info)
    if err:
        record("send_email (setup)", send_args,
               "EXCEPTION" if "_exception" in str(send_info) else "TOOL_ERROR", err)
        return None
    return await _find_item_id(s, "Inbox", subject)


async def _create_folder(s, name: str, parent: str):
    """create_folder's own `name` parameter collides with call()'s, hence call_args."""
    info = await call_args(s, "create_folder", {"name": name, "parent_folder_id": parent})
    err = is_error_payload(info)
    if err or not isinstance(info, dict) or "id" not in info:
        record("create_folder (setup)", {"name": name, "parent_folder_id": parent},
               "EXCEPTION" if "_exception" in str(info) else "TOOL_ERROR",
               err or f"unexpected shape: {info}")
        return None
    return info["id"]


async def _move_case(s, label: str, subject: str, target_folder: str,
                     verify_in: str, expect_matched_by: str) -> bool:
    """Send a tagged message, move it with `target_folder`, verify it landed."""
    item_id = await _send_and_locate(s, subject)
    if not item_id:
        record(f"send_email (locate: {label})", {"subject": subject}, "EXCEPTION",
               f"sent message not found in Inbox after {FIND_ATTEMPTS * FIND_DELAY_SECONDS}s")
        return False

    move_args = {"item_ids": [item_id], "target_folder": target_folder}
    move_info = await call(s, "move_email", **move_args)
    err = is_error_payload(move_info)
    if err:
        record(f"move_email ({label})", move_args,
               "EXCEPTION" if "_exception" in str(move_info) else "TOOL_ERROR", err)
        return False

    # matched_by pins *how* it resolved, not just that it did: a name silently
    # resolving via the deep-search fallback instead of the tier under test
    # would otherwise pass while the tier is broken.
    matched_by = move_info.get("matched_by")
    if matched_by != expect_matched_by:
        record(f"move_email ({label})", move_args, "TOOL_ERROR",
               f"resolved via matched_by={matched_by!r}, expected {expect_matched_by!r}")
        return False

    landed = await _find_item_id(s, verify_in, subject)
    if not landed:
        record(f"move_email ({label})", move_args, "TOOL_ERROR",
               f"reported success (matched_by={matched_by}) but message not found in "
               f"the target folder afterward")
        return False

    record(f"move_email ({label})", move_args, "OK",
           f"moved and confirmed present; matched_by={matched_by}")
    return True


async def _ambiguity_case(s, dup_child_ids: list[str]) -> bool:
    """A shared name must be refused with candidate ids, then work via an id."""
    subject = f"[{TAG}] ambiguous name then id"
    item_id = await _send_and_locate(s, subject)
    if not item_id:
        record("send_email (locate: ambiguity)", {"subject": subject}, "EXCEPTION",
               "sent message not found in Inbox")
        return False

    args = {"item_ids": [item_id], "target_folder": DUP_CHILD_NAME}
    info = await call(s, "move_email", **args)
    if not isinstance(info, dict) or info.get("error_code") != "folder_name_ambiguous":
        record("move_email (ambiguous name refused)", args, "TOOL_ERROR",
               f"expected error_code=folder_name_ambiguous, got: {info}")
        return False
    candidate_ids = {c.get("id") for c in info.get("candidates", [])}
    if not set(dup_child_ids).issubset(candidate_ids):
        record("move_email (ambiguous name refused)", args, "TOOL_ERROR",
               f"candidates {candidate_ids} miss one of the two created folders "
               f"{dup_child_ids}")
        return False
    record("move_email (ambiguous name refused)", args, "OK",
           f"refused with {len(candidate_ids)} candidate id(s) instead of guessing")

    # ...and the candidate id it handed back must then work.
    return await _move_case(
        s, "ambiguous name resolved by id", f"[{TAG}] ambiguous resolved by id",
        dup_child_ids[0], dup_child_ids[0], "folder_id",
    )


async def main() -> bool:
    if not SELF_EMAIL:
        # Fail loudly rather than sending to "" and reporting a mysterious
        # tool error four calls later.
        record("test_move_email_custom_folder", {}, "EXCEPTION",
               "set EXCHANGE_SMOKE_SELF_EMAIL to the signed-in mailbox's address "
               "(this test moves messages it sends to itself)")
        return False

    async with session() as s:
        top_level_id = await _create_folder(s, TOP_LEVEL_NAME, "msgfolderroot")
        inbox_child_id = await _create_folder(s, INBOX_CHILD_NAME, "inbox")
        dup_parent_a = await _create_folder(s, DUP_PARENT_A, "msgfolderroot")
        dup_parent_b = await _create_folder(s, DUP_PARENT_B, "msgfolderroot")
        if not all((top_level_id, inbox_child_id, dup_parent_a, dup_parent_b)):
            return False

        dup_child_a = await _create_folder(s, DUP_CHILD_NAME, dup_parent_a)
        dup_child_b = await _create_folder(s, DUP_CHILD_NAME, dup_parent_b)
        if not (dup_child_a and dup_child_b):
            return False
        record("create_folder (setup)", {"tag": TAG}, "OK",
               "6 disposable folders created (top-level, inbox child, 2 duplicate-name)")

        ok = True

        # A. top-level custom folder by name
        ok &= await _move_case(
            s, "custom top-level folder by name", f"[{TAG}] by name",
            TOP_LEVEL_NAME, TOP_LEVEL_NAME, "name",
        )

        # B. the same folder by id
        ok &= await _move_case(
            s, "custom top-level folder by id", f"[{TAG}] by id",
            top_level_id, top_level_id, "folder_id",
        )

        # C. Inbox child by bare name -- the reported "Quarantena" shape
        ok &= await _move_case(
            s, "inbox child by bare name", f"[{TAG}] inbox child by name",
            INBOX_CHILD_NAME, INBOX_CHILD_NAME, "inbox_child",
        )

        # D. duplicate name refused, then reachable by id
        ok &= await _ambiguity_case(s, [dup_child_a, dup_child_b])

        # --- cleanup ---
        for label, folder_id in (
            ("top-level", top_level_id),
            ("inbox child", inbox_child_id),
            ("dup parent A", dup_parent_a),
            ("dup parent B", dup_parent_b),
        ):
            info = await call(s, "delete_folder", folder_id=folder_id, permanent=True)
            err = is_error_payload(info)
            if err:
                record(f"delete_folder ({label}, cleanup)", {"folder_id": folder_id},
                       "TOOL_ERROR", err)
                ok = False
            else:
                record(f"delete_folder ({label}, cleanup)", {"folder_id": folder_id},
                       "OK", "deleted with its contents")

        return ok


if __name__ == "__main__":
    ok = run(main())
    sys.exit(0 if ok else 1)
