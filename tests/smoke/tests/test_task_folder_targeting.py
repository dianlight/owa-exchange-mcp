"""Smoke test: `task_folder` resolution in get_tasks / create_task (tasks.py).

test_task_lifecycle.py only ever touches the default task list, which
exercises exactly one of the four things `_resolve_task_folder` has to
handle. This test covers the other three against a real named To Do list:

1. a bare list name, resolved as a *child of the `tasks` root* rather than
   of msgfolderroot (where OWAClient.get_folder_id would normally look, and
   where a To Do list simply isn't);
2. a "/"-delimited path ("tasks/<list>");
3. a raw opaque folder ID, which must be passed straight through - those
   IDs contain "/" characters and would otherwise be mangled into a
   folder path (see _looks_like_folder_id).

It also asserts the negative case that makes the whole thing meaningful:
a task created in a child list must NOT appear in the default list's
Shallow listing.

Needs at least one To Do list besides the default one. The folder tools
can't create one (create_folder hardcodes FolderClass "IPF.Note", i.e. a
mail folder), so rather than fake it this test SKIPS - reporting so
explicitly - when the mailbox has no child task folder. Create a list in
To Do/Outlook and re-run to get real coverage.

Run standalone:
    python -m tests.smoke.tests.test_task_folder_targeting
"""

import sys
import time
from datetime import date, timedelta

from tests.smoke.mcp_client import call, run, session
from tests.smoke.results import is_error_payload, record

TAG = f"task-folder-smoke-{int(time.time())}"
SUBJECT = f"[{TAG}] disposable list-targeting task"
DUE_DATE = (date.today() + timedelta(days=5)).isoformat()

LIST_LIMIT = 200


async def _tasks_in(s, task_folder: str):
    """get_tasks against one list -> (subjects, error-note)."""
    info = await call(s, "get_tasks", task_folder=task_folder, limit=LIST_LIMIT)
    err = is_error_payload(info)
    if err:
        return None, err
    if not isinstance(info, dict) or not isinstance(info.get("tasks"), list):
        return None, f"unexpected shape: {info}"
    return [t.get("subject") for t in info["tasks"]], None


async def main() -> bool:
    async with session() as s:
        # Find a real To Do list (child folder of the tasks root) to target.
        folders = await call(s, "get_folders", parent_folder_id="tasks")
        err = is_error_payload(folders)
        if err or not isinstance(folders, list):
            record("get_folders", {"parent_folder_id": "tasks"},
                   "EXCEPTION" if "_exception" in str(folders) else "TOOL_ERROR",
                   err or f"unexpected shape: {folders}")
            return False

        if not folders:
            record("get_tasks/create_task (task_folder)", {"parent_folder_id": "tasks"}, "OK",
                   "SKIPPED - mailbox has no To Do list besides the default one, and the "
                   "folder tools can only create mail folders (IPF.Note)")
            return True

        target = folders[0]
        list_name = target.get("name", "")
        list_id = target.get("id", "")
        record("get_folders", {"parent_folder_id": "tasks"}, "OK",
               f"{len(folders)} To Do list(s); targeting '{list_name}'")

        # 1. create_task into that list, addressed by bare name
        create_args = {"subject": SUBJECT, "due_date": DUE_DATE, "task_folder": list_name}
        create_info = await call(s, "create_task", **create_args)
        err = is_error_payload(create_info)
        if err or not isinstance(create_info, dict) or not create_info.get("item_id"):
            record("create_task", create_args,
                   "EXCEPTION" if "_exception" in str(create_info) else "TOOL_ERROR",
                   err or f"unexpected shape: {create_info}")
            return False
        item_id = create_info["item_id"]
        record("create_task", create_args, "OK",
               f"created in '{list_name}' by bare name, item_id={item_id[:24]}...")

        try:
            # 2. get_tasks by bare name, by path, and by raw folder ID
            for spelling, value in (
                ("bare name", list_name),
                ("path", f"tasks/{list_name}"),
                ("raw folder id", list_id),
            ):
                subjects, note = await _tasks_in(s, value)
                if subjects is None:
                    record("get_tasks", {"task_folder": spelling}, "TOOL_ERROR", note)
                    return False
                if SUBJECT not in subjects:
                    record("get_tasks", {"task_folder": spelling}, "TOOL_ERROR",
                           f"task not found addressing the list by {spelling} "
                           f"({len(subjects)} task(s) returned)")
                    return False
                record("get_tasks", {"task_folder": spelling}, "OK",
                       f"tagged task found addressing '{list_name}' by {spelling}")

            # 3. negative case: the default list's Shallow listing must not
            # include a task that lives in a child list.
            default_subjects, note = await _tasks_in(s, "tasks")
            if default_subjects is None:
                record("get_tasks", {"task_folder": "tasks"}, "TOOL_ERROR", note)
                return False
            if SUBJECT in default_subjects:
                record("get_tasks", {"task_folder": "tasks"}, "TOOL_ERROR",
                       "task created in a child To Do list also appears in the default "
                       "list's listing - task_folder is not scoping the read")
                return False
            record("get_tasks", {"task_folder": "tasks"}, "OK",
                   "child-list task correctly absent from the default list's listing")
        finally:
            # Cleanup runs on every path, including the early returns above:
            # this task lives in the user's real To Do list.
            delete_args = {"item_ids": [item_id], "permanent": True}
            delete_info = await call(s, "delete_task", **delete_args)
            derr = is_error_payload(delete_info)
            record("delete_task", delete_args, "OK" if not derr else "TOOL_ERROR",
                   derr or f"cleanup: task removed from '{list_name}'")

        return True


if __name__ == "__main__":
    ok = run(main())
    sys.exit(0 if ok else 1)
