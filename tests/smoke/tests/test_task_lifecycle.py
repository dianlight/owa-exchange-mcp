"""Smoke test: create_task, get_task, get_tasks, update_task,
complete_task, delete_task (tasks.py).

Chains all six Task tools around one disposable, uniquely-tagged task in
the mailbox's default task list (the `tasks` distinguished folder, which
is what Microsoft To Do shows as its default list):

1. create_task   - with due date, note body, category and reminder, so the
                   read steps have every field shape to verify.
2. get_task      - full detail read; verifies each field round-tripped,
                   in particular that due_date comes back as the same
                   calendar day it was written (task dates are stored as
                   UTC midnight - a local-midnight write would come back
                   off by one).
3. get_tasks     - the new task must appear in the open-tasks listing.
4. update_task   - retitle, push the due date out, set InProgress, drop
                   the reminder; verified by a second get_task.
5. complete_task - verified via get_task (status/is_complete) *and* via
                   get_tasks: gone from the default listing, present with
                   include_completed=True. That pair is the real test of
                   the client-side completion filter.
6. delete_task   - HardDelete (final cleanup), verified by the task being
                   gone from the folder listing. Deliberately *not* by a
                   by-ID read: a deleted task's ItemId stays resolvable
                   here, so get_task keeps returning the item afterwards
                   (see tasks.py's module docstring).

Repeatable: the subject includes a timestamp tag, so re-runs never
collide with a leftover task from a prior run. Everything it creates is
permanently deleted in step 6, including on most failure paths (see
_cleanup) - a leaked task would otherwise sit in the user's real To Do
list forever.

Run standalone:
    python -m tests.smoke.tests.test_task_lifecycle
"""

import sys
import time
from datetime import date, datetime, timedelta

from tests.smoke.mcp_client import call, run, session
from tests.smoke.results import is_error_payload, record

TAG = f"task-smoke-{int(time.time())}"
SUBJECT = f"[{TAG}] disposable smoke-test task"
RENAMED_SUBJECT = f"[{TAG}] renamed smoke-test task"
BODY = "Automated smoke-test task for tasks.py lifecycle testing. Safe to ignore."
CATEGORY = "Smoke Test"

DUE_DATE = (date.today() + timedelta(days=3)).isoformat()
NEW_DUE_DATE = (date.today() + timedelta(days=10)).isoformat()
REMINDER = f"{DUE_DATE} 09:30"

# get_tasks' own maximum. Needed because it sorts due-date ascending, so the
# 25-row default page would be filled by older real tasks before reaching a
# task due days from now. If this mailbox's default list ever holds more than
# 200 *open* tasks due sooner than DUE_DATE, the listing steps would fail for
# that reason rather than a tool bug - check `scanned` in the recorded note.
LIST_LIMIT = 200


async def _find_in_list(s, *, include_completed: bool, subject: str):
    """Return the tagged task from get_tasks, or (None, error-note)."""
    info = await call(
        s, "get_tasks", limit=LIST_LIMIT, include_completed=include_completed
    )
    err = is_error_payload(info)
    if err:
        return None, err
    if not isinstance(info, dict) or not isinstance(info.get("tasks"), list):
        return None, f"unexpected shape: {info}"
    for task in info["tasks"]:
        if task.get("subject") == subject:
            return task, None
    return None, None


async def _cleanup(s, item_id: str) -> None:
    """Best-effort permanent delete, for the failure paths after create_task."""
    await call(s, "delete_task", item_ids=[item_id], permanent=True)


async def main() -> bool:
    async with session() as s:
        # 1. create_task
        create_args = {
            "subject": SUBJECT,
            "due_date": DUE_DATE,
            "body": BODY,
            "importance": "High",
            "categories": [CATEGORY],
            "reminder": REMINDER,
        }
        create_info = await call(s, "create_task", **create_args)
        err = is_error_payload(create_info)
        if err or not isinstance(create_info, dict) or not create_info.get("item_id"):
            record("create_task", create_args,
                   "EXCEPTION" if "_exception" in str(create_info) else "TOOL_ERROR",
                   err or f"unexpected shape: {create_info}")
            return False
        item_id = create_info["item_id"]
        record("create_task", create_args, "OK", f"item_id={item_id[:24]}...")

        # 2. get_task -- every field written above must come back
        get_args = {"item_id": item_id}
        task = await call(s, "get_task", **get_args)
        err = is_error_payload(task)
        if err or not isinstance(task, dict):
            record("get_task", get_args, "EXCEPTION" if "_exception" in str(task) else "TOOL_ERROR",
                   err or f"unexpected shape: {task}")
            await _cleanup(s, item_id)
            return False

        mismatches = []
        if task.get("subject") != SUBJECT:
            mismatches.append(f"subject={task.get('subject')!r}")
        if task.get("due_date") != DUE_DATE:
            mismatches.append(f"due_date={task.get('due_date')!r} (wrote {DUE_DATE})")
        if task.get("status") != "NotStarted":
            mismatches.append(f"status={task.get('status')!r}")
        if task.get("is_complete"):
            mismatches.append("is_complete=True on a fresh task")
        body_read = task.get("body") or ""
        if BODY not in body_read:
            mismatches.append(f"body={body_read[:60]!r}")
        if CATEGORY not in (task.get("categories") or []):
            mismatches.append(f"categories={task.get('categories')!r}")
        if task.get("importance") != "High":
            mismatches.append(f"importance={task.get('importance')!r}")
        if not task.get("reminder_is_set"):
            mismatches.append("reminder_is_set=False after writing a reminder")
        if mismatches:
            record("get_task", get_args, "TOOL_ERROR", f"field mismatches: {'; '.join(mismatches)}")
            await _cleanup(s, item_id)
            return False
        record("get_task", get_args, "OK",
               f"due_date={task['due_date']}, reminder_due_by={task.get('reminder_due_by')}, "
               f"all written fields round-tripped")

        # 3. get_tasks -- must list the new (open) task
        listed, note = await _find_in_list(s, include_completed=False, subject=SUBJECT)
        if listed is None:
            record("get_tasks", {"limit": LIST_LIMIT, "include_completed": False},
                   "TOOL_ERROR", note or "new task not present in the open-task listing")
            await _cleanup(s, item_id)
            return False
        record("get_tasks", {"limit": LIST_LIMIT, "include_completed": False}, "OK",
               "new task present in the open-task listing")

        # 4. update_task -- retitle, move the due date, start it, drop the reminder
        update_args = {
            "item_id": item_id,
            "subject": RENAMED_SUBJECT,
            "due_date": NEW_DUE_DATE,
            "status": "InProgress",
            "clear_reminder": True,
        }
        update_info = await call(s, "update_task", **update_args)
        err = is_error_payload(update_info)
        if err or not isinstance(update_info, dict) or not update_info.get("success"):
            record("update_task", update_args,
                   "EXCEPTION" if "_exception" in str(update_info) else "TOOL_ERROR",
                   err or f"unexpected shape: {update_info}")
            await _cleanup(s, item_id)
            return False
        # UpdateItem can return a different ItemId (recurring tasks); follow it.
        item_id = update_info.get("item_id") or item_id

        task = await call(s, "get_task", item_id=item_id)
        err = is_error_payload(task)
        if err or not isinstance(task, dict):
            record("update_task", update_args, "TOOL_ERROR",
                   err or f"could not re-read the task after update: {task}")
            await _cleanup(s, item_id)
            return False
        mismatches = []
        if task.get("subject") != RENAMED_SUBJECT:
            mismatches.append(f"subject={task.get('subject')!r}")
        if task.get("due_date") != NEW_DUE_DATE:
            mismatches.append(f"due_date={task.get('due_date')!r} (wrote {NEW_DUE_DATE})")
        if task.get("status") != "InProgress":
            mismatches.append(f"status={task.get('status')!r}")
        if task.get("reminder_is_set"):
            mismatches.append("reminder_is_set=True after clear_reminder")
        if mismatches:
            record("update_task", update_args, "TOOL_ERROR",
                   f"update didn't take effect: {'; '.join(mismatches)}")
            await _cleanup(s, item_id)
            return False
        record("update_task", update_args, "OK",
               f"fields {update_info.get('updated_fields')} written and verified")

        # 5. complete_task -- verified on the item, then through both listings
        complete_args = {"item_ids": [item_id], "completed": True}
        complete_info = await call(s, "complete_task", **complete_args)
        err = is_error_payload(complete_info)
        if err or not isinstance(complete_info, dict) or not complete_info.get("success"):
            record("complete_task", complete_args,
                   "EXCEPTION" if "_exception" in str(complete_info) else "TOOL_ERROR",
                   err or f"unexpected shape: {complete_info}")
            await _cleanup(s, item_id)
            return False
        updated_rows = complete_info.get("updated") or []
        if updated_rows and updated_rows[0].get("item_id"):
            item_id = updated_rows[0]["item_id"]

        task = await call(s, "get_task", item_id=item_id)
        if not isinstance(task, dict) or not task.get("is_complete") or task.get("status") != "Completed":
            record("complete_task", complete_args, "TOOL_ERROR",
                   f"task not Completed after complete_task: status={task.get('status') if isinstance(task, dict) else task}")
            await _cleanup(s, item_id)
            return False

        still_open, note = await _find_in_list(s, include_completed=False, subject=RENAMED_SUBJECT)
        if note:
            record("get_tasks", {"include_completed": False}, "TOOL_ERROR", note)
            await _cleanup(s, item_id)
            return False
        if still_open is not None:
            record("complete_task", complete_args, "TOOL_ERROR",
                   "completed task still listed by get_tasks(include_completed=False)")
            await _cleanup(s, item_id)
            return False

        listed_completed, note = await _find_in_list(s, include_completed=True, subject=RENAMED_SUBJECT)
        if listed_completed is None:
            record("get_tasks", {"include_completed": True}, "TOOL_ERROR",
                   note or "completed task missing from get_tasks(include_completed=True)")
            await _cleanup(s, item_id)
            return False
        record("complete_task", complete_args, "OK",
               f"status=Completed, complete_date={listed_completed.get('complete_date')}, "
               "filtered out of the open listing and present with include_completed=True")

        # 6. delete_task (permanent, final cleanup) -- verified by get_task failing
        delete_args = {"item_ids": [item_id], "permanent": True}
        delete_info = await call(s, "delete_task", **delete_args)
        err = is_error_payload(delete_info)
        if err or not isinstance(delete_info, dict) or not delete_info.get("success"):
            record("delete_task", delete_args,
                   "EXCEPTION" if "_exception" in str(delete_info) else "TOOL_ERROR",
                   err or f"unexpected shape: {delete_info}")
            return False

        # Verified by absence from the folder listing, NOT by get_task
        # failing: a deleted task's ItemId stays resolvable on this backend
        # (confirmed live 2026-09-11 - get_task returns the item with a
        # bumped change_key after both a soft and a permanent delete), so a
        # by-ID read proves nothing either way.
        still_listed, note = await _find_in_list(s, include_completed=True, subject=RENAMED_SUBJECT)
        if note:
            record("delete_task", delete_args, "TOOL_ERROR", note)
            return False
        if still_listed is not None:
            record("delete_task", delete_args, "TOOL_ERROR",
                   "task still listed by get_tasks after permanent delete")
            return False
        record("delete_task", delete_args, "OK",
               "task permanently deleted and confirmed gone from the folder listing")

        return True


if __name__ == "__main__":
    started = datetime.now()
    ok = run(main())
    print(f"[test_task_lifecycle] {'PASS' if ok else 'FAIL'} in {(datetime.now() - started).seconds}s")
    sys.exit(0 if ok else 1)
