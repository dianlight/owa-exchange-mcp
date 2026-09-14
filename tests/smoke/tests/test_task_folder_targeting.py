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

Needs at least one To Do list besides the default one. If the mailbox has
none, this test now creates a disposable one itself via
`create_folder(parent_folder_id="tasks", folder_class="IPF.Task")` and
deletes it again at the end - that argument exists precisely so this test
stops skipping itself (it used to, because create_folder hardcoded
FolderClass "IPF.Note", i.e. a mail folder). When the mailbox *does*
already have a list, the first one is targeted and left alone: it is the
user's real data, so only the tagged task inside it is cleaned up.

Run standalone:
    python -m tests.smoke.tests.test_task_folder_targeting
"""

import sys
import time
from datetime import date, timedelta

from tests.smoke.mcp_client import call, call_args, run, session
from tests.smoke.results import is_error_payload, record

TAG = f"task-folder-smoke-{int(time.time())}"
SUBJECT = f"[{TAG}] disposable list-targeting task"
LIST_NAME = f"[{TAG}] disposable list"
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

        # Only a list this test created is safe to delete afterwards.
        created_list_id = None

        if folders:
            target = folders[0]
            list_name = target.get("name", "")
            list_id = target.get("id", "")
            record("get_folders", {"parent_folder_id": "tasks"}, "OK",
                   f"{len(folders)} existing To Do list(s); targeting '{list_name}'")
        else:
            # No list in this mailbox - make one. This is the whole point of
            # create_folder's folder_class argument: a To Do list is a folder
            # with a task class under the tasks root, and until that argument
            # existed this test could only skip itself.
            create_list_args = {"name": LIST_NAME, "parent_folder_id": "tasks",
                                "folder_class": "IPF.Task"}
            list_info = await call_args(s, "create_folder", create_list_args)
            err = is_error_payload(list_info)
            if err or not isinstance(list_info, dict) or not list_info.get("id"):
                record("create_folder", create_list_args,
                       "EXCEPTION" if "_exception" in str(list_info) else "TOOL_ERROR",
                       err or f"unexpected shape: {list_info}")
                return False
            list_name = LIST_NAME
            list_id = created_list_id = list_info["id"]
            # The class is echoed from the server's own CreateFolder response,
            # so a silently-coerced one (any unrecognised prefix degrades to
            # IPF.Note, without an error) shows up here rather than later as
            # "get_tasks finds nothing in a folder that plainly exists".
            stored_class = list_info.get("folder_class")
            if stored_class != "IPF.Task":
                record("create_folder", create_list_args, "TOOL_ERROR",
                       f"asked for FolderClass IPF.Task, server stored '{stored_class}' "
                       "- that folder is not a To Do list")
                return False
            record("create_folder", create_list_args, "OK",
                   f"mailbox had no To Do list; created disposable '{list_name}' "
                   f"(FolderClass {stored_class}) under the tasks root")

        try:
            # 1. create_task into that list, addressed by bare name
            create_args = {"subject": SUBJECT, "due_date": DUE_DATE,
                           "task_folder": list_name}
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
                # this task may live in the user's real To Do list.
                delete_args = {"item_ids": [item_id], "permanent": True}
                delete_info = await call(s, "delete_task", **delete_args)
                derr = is_error_payload(delete_info)
                record("delete_task", delete_args, "OK" if not derr else "TOOL_ERROR",
                       derr or f"cleanup: task removed from '{list_name}'")
        finally:
            # Drop the list too, but only if this test made it - an existing
            # one is the user's own data and is left exactly as found.
            if created_list_id:
                folder_delete_args = {"folder_id": created_list_id, "permanent": True}
                fdel = await call(s, "delete_folder", **folder_delete_args)
                ferr = is_error_payload(fdel)
                record("delete_folder", folder_delete_args,
                       "OK" if not ferr else "TOOL_ERROR",
                       ferr or f"cleanup: disposable To Do list '{list_name}' removed")

        return True


if __name__ == "__main__":
    ok = run(main())
    sys.exit(0 if ok else 1)
