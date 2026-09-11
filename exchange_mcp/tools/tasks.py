"""Task tools for the Exchange MCP server — full CRUD over `Task` items.

"Task" is Exchange's own name for the item class (`Task:#Exchange`,
`IPM.Task`) living in the `tasks` distinguished folder; the modern web UI
surfaces the same items as **Microsoft To Do**
(`https://outlook.cloud.microsoft/host/<app-guid>/ToDoId`, the hosted To Do
app inside Outlook), and each To Do *list* is just a child folder of that
`tasks` root. This module therefore drives the plain EWS item actions —
`FindItem` / `GetItem` / `CreateItem` / `UpdateItem` / `DeleteItem` — the
same way [email.py](email.py) does for messages, rather than the To Do
app's own private REST surface: the EWS path exists on both the classic
canary-cookie and the modern bearer backend, needs no new transport, and
is what Outlook desktop reads/writes too.

Two things it deliberately does *not* do:

- **No task-list (folder) CRUD.** A To Do list is an ordinary folder, so
  `get_folders(parent_folder_id="tasks")` already enumerates them and the
  `*_folder` tools in [folders.py](folders.py) already create/rename/
  delete them. `task_folder` below accepts whatever those return.
- **No flagged-email tasks.** To Do's "Flagged Email" list is a *view*
  over flagged messages, not Task items in the Tasks folder, so it isn't
  visible to `FindItem` here. Use `set_email_flag` (#115) for those.

Wire-format notes, learned the hard way elsewhere in this codebase and
applied here up front:

- **Reads use `BaseShape: "AllProperties"` with no `AdditionalProperties`.**
  A single mis-spelled `FieldURI` fails the whole request on this backend
  (see PROJECT_STATUS.md #114/#115), and `AllProperties` returns every
  task field we render without naming any of them. Bodies are the one
  exception - `FindItem` never returns them - so `include_body=True`
  costs one extra `GetItem` per row, exactly like `get_emails`.
- **Writes must name fields, so every spelling lives in `_FIELD` below**
  rather than inline, keeping a live-test correction to one line each.
  The namespaced form (`item:Subject`, `task:DueDate`) is used because
  that's what's confirmed working for the fussiest field found so far
  (`item:Flag`, #115) and for `item:ParentFolderId` (#114).
- **`DueDate`/`StartDate` are written as UTC midnight**
  (`YYYY-MM-DDT00:00:00.000Z`). Exchange stores task dates that way, and
  sending a local-midnight wall-clock time instead is the classic source
  of off-by-one-day task dates. Reads go out on `Exchange2013` with no
  `TimeZoneContext`, so the date part comes back in the same UTC frame it
  was written in and round-trips exactly.
- **Never set more than one of `Status` / `PercentComplete` /
  `CompleteDate` in one request.** Per the EWS reference they are three
  spellings of the same state and the *last one processed* wins, so a
  request carrying two of them silently resolves to whichever the server
  happened to read last. `complete_task` writes `Status` only, and
  `update_task` rejects the combination client-side.

Three things confirmed against a live mailbox 2026-09-11, each of which
would otherwise be a trap for a caller:

- **A deleted task's ItemId stays resolvable.** After `delete_task` -
  soft *or* `permanent` - `get_task` still returns the item (with a bumped
  ChangeKey), because this backend resolves an ItemId to the item in its
  new location rather than 404-ing. So "did the delete work?" must be
  answered by whether the task still appears in the folder listing, never
  by a by-ID read. Both smoke tests verify deletion that way.
- **`PercentComplete` comes back as a *string*** (`"100"`), not a number,
  so `_to_public` coerces it - a caller comparing `== 100` would silently
  never match otherwise.
- **Reminders are written in `Russian Standard Time` (UTC+3)** - the
  timezone this whole codebase hardcodes into `TimeZoneContext` on every
  write - while reads come back in UTC. A reminder asked for at 09:30
  therefore reads back as `06:30`. The reminder fires at the moment
  Exchange stored, which is 09:30 *Moscow* time, not 09:30 in the
  caller's own timezone: correct for a UTC+3 mailbox, three hours early
  anywhere else. Fixing it properly means resolving the mailbox's real
  timezone, which is a codebase-wide change (see PROJECT_STATUS.md §4),
  not a Task-module one.
"""

import json
from datetime import datetime

from mcp.server.fastmcp import Context

from exchange_mcp.server import mcp, AppContext
from exchange_mcp.owa_client import OWAClient, SessionExpiredError
from exchange_mcp.utils import format_date, format_datetime, html_to_text, parse_date

# Names that mean "the mailbox's default task list" (the `tasks`
# distinguished folder) rather than a named To Do list.
_TASK_ROOT_ALIASES = {"", "tasks", "task", "todo", "to do", "to-do", "задачи"}

_VALID_STATUSES = ("NotStarted", "InProgress", "Completed", "WaitingOnOthers", "Deferred")
_VALID_IMPORTANCE = ("Low", "Normal", "High")

# How many items get_tasks will pull from the folder before giving up on
# filling `limit`, since include_completed=False filters client-side.
_MAX_SCAN = 500
_PAGE_SIZE = 100

_READ_HEADER = {
    "__type": "JsonRequestHeaders:#Exchange",
    "RequestServerVersion": "Exchange2013",
}

# Writes go out on V2017_08_18 (per CLAUDE.md) with a TimeZoneContext, which
# ReminderDueBy needs: it's a real point in time, unlike DueDate/StartDate.
_WRITE_HEADER = {
    "__type": "JsonRequestHeaders:#Exchange",
    "RequestServerVersion": "V2017_08_18",
    "TimeZoneContext": {
        "__type": "TimeZoneContext:#Exchange",
        "TimeZoneDefinition": {
            "__type": "TimeZoneDefinitionType:#Exchange",
            "Id": "Russian Standard Time",
        },
    },
}

# UpdateItem/DeleteItemField property paths. One wrong spelling fails the
# entire request, so they're collected here to be corrected in one place -
# see the module docstring.
_FIELD = {
    "subject": "item:Subject",
    "body": "item:Body",
    "categories": "item:Categories",
    "importance": "item:Importance",
    "reminder_is_set": "item:ReminderIsSet",
    "reminder_due_by": "item:ReminderDueBy",
    "due_date": "task:DueDate",
    "start_date": "task:StartDate",
    "status": "task:Status",
    "percent_complete": "task:PercentComplete",
}


def _get_client(ctx: Context) -> OWAClient:
    """Extract the OWAClient from the MCP lifespan context."""
    app_ctx: AppContext = ctx.request_context.lifespan_context
    return app_ctx.client


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


def _looks_like_folder_id(value: str) -> bool:
    """True if `value` is an opaque EWS folder ID rather than a folder name.

    Checked *before* any name lookup because opaque IDs are long base64
    blobs that routinely contain "/" - which `get_folder_id()` would
    otherwise read as a path separator and try to walk.
    """
    return len(value) > 80 and "=" in value


def _resolve_task_folder(client: OWAClient, task_folder: str) -> str | None:
    """Resolve a To Do list name / path / ID to something folder_id_dict() accepts.

    A To Do list is a child of the `tasks` root, not of `msgfolderroot`,
    so the tasks-root lookup is tried first: a bare name is far more
    likely to mean "my 'Groceries' list" than a same-named mail folder.
    Returns None when nothing matches.

    A bare *distinguished* ID ("deleteditems", "msgfolderroot", ...) is
    accepted only as a last resort, after every name lookup has missed:
    `get_folder_id()` maps user-facing names ("deleted"), not the
    distinguished IDs themselves, so without this a caller couldn't point
    these tools at a folder the way the `*_folder` tools allow - but
    checking it any earlier would let a technical ID shadow a To Do list
    that happens to share the name.
    """
    name = (task_folder or "").strip()
    if name.lower() in _TASK_ROOT_ALIASES:
        return "tasks"
    if _looks_like_folder_id(name):
        return name
    if "/" not in name:
        nested = client.get_folder_id(f"tasks/{name}")
        if nested:
            return nested
    resolved = client.get_folder_id(name)
    if resolved:
        return resolved
    # folder_id_dict() is the authority on what counts as distinguished, so
    # ask it rather than keeping a second copy of that list here.
    if OWAClient.folder_id_dict(name.lower())["__type"].startswith("DistinguishedFolderId"):
        return name.lower()
    return None


def _task_date(value: str) -> str:
    """Normalize a task date to the UTC-midnight form Exchange stores.

    Accepts every format `utils.parse_date` does (YYYY-MM-DD, DD.MM.YYYY,
    DD/MM/YYYY, MM/DD/YYYY). Any time-of-day in the input is dropped:
    Exchange truncates task DueDate/StartDate to a date anyway.
    """
    date_part = value.strip().replace("T", " ").split(" ")[0]
    return parse_date(date_part).strftime("%Y-%m-%dT00:00:00.000Z")


def _reminder_datetime(value: str) -> str:
    """Normalize a reminder to a local wall-clock timestamp (see _WRITE_HEADER).

    Requires an explicit time - "YYYY-MM-DD HH:MM" or "YYYY-MM-DDTHH:MM"
    (seconds optional). A bare date is rejected rather than silently
    assigned some invented hour.
    """
    raw = value.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%dT%H:%M:%S.000")
        except ValueError:
            continue
    raise ValueError(
        f"Invalid reminder datetime '{value}'. Use 'YYYY-MM-DD HH:MM' "
        "(a date alone has no reminder time)."
    )


def _get_change_key(client: OWAClient, item_id: str) -> str | None:
    """Fetch the ChangeKey for a task via GetItem (IdOnly) - required on writes."""
    payload = {
        "__type": "GetItemJsonRequest:#Exchange",
        "Header": _READ_HEADER,
        "Body": {
            "__type": "GetItemRequest:#Exchange",
            "ItemShape": {
                "__type": "ItemResponseShape:#Exchange",
                "BaseShape": "IdOnly",
            },
            "ItemIds": [{"__type": "ItemId:#Exchange", "Id": item_id}],
        },
    }
    data = client.request("GetItem", payload)
    for msg in client.extract_items(data):
        for item in msg.get("Items", []):
            return item.get("ItemId", {}).get("ChangeKey")
    return None


def _to_public(item: dict) -> dict:
    """Map a raw Task item onto this module's response shape.

    `is_complete` is taken from IsComplete when present and derived from
    Status otherwise: this backend doesn't always return IsComplete, and
    a caller shouldn't have to know which of the three completion
    spellings the server chose to send.
    """
    status = item.get("Status", "")
    is_complete = item.get("IsComplete")
    if is_complete is None:
        is_complete = status == "Completed"

    # This backend sends PercentComplete as a string ("100"), confirmed live
    # 2026-09-11 - coerce it so a caller can compare it as the number the
    # field obviously is.
    try:
        percent_complete = int(float(item.get("PercentComplete", 0)))
    except (TypeError, ValueError):
        percent_complete = 0

    return {
        "item_id": item.get("ItemId", {}).get("Id", ""),
        "change_key": item.get("ItemId", {}).get("ChangeKey", ""),
        "subject": item.get("Subject", "(No subject)"),
        "status": status,
        "is_complete": bool(is_complete),
        "percent_complete": percent_complete,
        "due_date": format_date(item.get("DueDate", "")),
        "start_date": format_date(item.get("StartDate", "")),
        "complete_date": format_date(item.get("CompleteDate", "")),
        "reminder_is_set": item.get("ReminderIsSet", False),
        "reminder_due_by": format_datetime(item.get("ReminderDueBy", "")),
        "importance": item.get("Importance", "Normal"),
        "sensitivity": item.get("Sensitivity", "Normal"),
        "categories": item.get("Categories", []),
        "owner": item.get("Owner", ""),
        "is_recurring": item.get("IsRecurring", False),
        "has_attachments": item.get("HasAttachments", False),
        "last_modified": format_datetime(item.get("LastModifiedTime", "")),
        "parent_folder_id": item.get("ParentFolderId", {}).get("Id", ""),
    }


def _is_task_item(item: dict) -> bool:
    """True for Task items, so a non-task folder doesn't yield pseudo-tasks.

    `task_folder` accepts any folder, including a mail folder (a raw ID, or
    a distinguished name like "deleteditems"), and `FindItem` there returns
    messages - which `_to_public` would happily map into the task shape,
    inventing a subject-only "task" with no status or dates. Filtering on
    the response's own `__type` is what tells them apart.

    An item with no `__type` at all is treated as a task: on a backend that
    omits it this degrades to the previous unfiltered behavior instead of
    silently returning nothing.
    """
    item_type = str(item.get("__type", ""))
    return not item_type or item_type.startswith("Task")


def _due_sort_key(task: dict) -> tuple[int, str]:
    """Sort by due date ascending, undated tasks last (0/1 keeps them apart)."""
    due = task.get("due_date") or ""
    return (1, "") if not due else (0, due)


def _get_task_body(client: OWAClient, item_id: str) -> tuple[str, str | None]:
    """Fetch one task's body as plain text. Returns (body, error).

    Degrades per item rather than failing a whole page: some items make
    OWA's own GetItem throw (PROJECT_STATUS.md §4), and one bad row
    shouldn't cost the caller the other 24.
    """
    payload = {
        "__type": "GetItemJsonRequest:#Exchange",
        "Header": _READ_HEADER,
        "Body": {
            "__type": "GetItemRequest:#Exchange",
            "ItemShape": {
                "__type": "ItemResponseShape:#Exchange",
                "BaseShape": "IdOnly",
                "BodyType": "HTML",
                "AdditionalProperties": [
                    {"__type": "PropertyUri:#Exchange", "FieldURI": _FIELD["body"]},
                ],
            },
            "ItemIds": [{"__type": "ItemId:#Exchange", "Id": item_id}],
        },
    }
    try:
        data = client.request("GetItem", payload)
    except SessionExpiredError:
        raise
    except Exception as exc:
        return "", str(exc)

    for msg in client.extract_items(data):
        if msg.get("ResponseClass") == "Error":
            return "", msg.get("MessageText", "GetItem failed.")
        for item in msg.get("Items", []):
            return html_to_text(item.get("Body", {}).get("Value", "")), None
    return "", None


def _find_tasks(client: OWAClient, folder_ref: dict) -> list[dict]:
    """Page through every Task in one folder (up to _MAX_SCAN), unfiltered.

    No SortOrder is sent: sorting server-side on DueDate would put the
    undated tasks wherever this backend feels like, and it's another
    FieldURI that could fail the whole request. `_due_sort_key` handles
    ordering client-side instead.
    """
    items: list[dict] = []
    offset = 0

    while offset < _MAX_SCAN:
        payload = {
            "__type": "FindItemJsonRequest:#Exchange",
            "Header": _READ_HEADER,
            "Body": {
                "__type": "FindItemRequest:#Exchange",
                "ItemShape": {
                    "__type": "ItemResponseShape:#Exchange",
                    "BaseShape": "AllProperties",
                },
                "ParentFolderIds": [folder_ref],
                "Traversal": "Shallow",
                "Paging": {
                    "__type": "IndexedPageView:#Exchange",
                    "BasePoint": "Beginning",
                    "Offset": offset,
                    "MaxEntriesReturned": _PAGE_SIZE,
                },
            },
        }

        data = client.request("FindItem", payload)

        page: list[dict] = []
        includes_last = True
        for msg in client.extract_items(data):
            if msg.get("ResponseClass") == "Error":
                raise RuntimeError(msg.get("MessageText", "FindItem failed."))
            if "RootFolder" in msg:
                root = msg["RootFolder"]
                page = root.get("Items", [])
                includes_last = root.get("IncludesLastItemInRange", True)
                break

        items.extend(page)
        if includes_last or not page:
            break
        offset += _PAGE_SIZE

    return items


def _apply_updates(
    client: OWAClient, item_id: str, updates: list[dict]
) -> tuple[str, str]:
    """Send one UpdateItem for a task. Returns the resulting (item_id, change_key).

    The returned ID is not always the one passed in: completing an
    occurrence of a *recurring* task makes Exchange mint a brand-new
    one-off task for the completed occurrence and roll the original ID
    forward to the next occurrence (EWS "UpdateItem operation (task)").
    Callers surface both so the distinction isn't silently lost.
    """
    item_id_dict = {"__type": "ItemId:#Exchange", "Id": item_id}
    change_key = _get_change_key(client, item_id)
    if change_key:
        item_id_dict["ChangeKey"] = change_key

    payload = {
        "__type": "UpdateItemJsonRequest:#Exchange",
        "Header": _WRITE_HEADER,
        "Body": {
            "__type": "UpdateItemRequest:#Exchange",
            "ItemChanges": [
                {
                    "__type": "ItemChange:#Exchange",
                    "ItemId": item_id_dict,
                    "Updates": updates,
                }
            ],
            "ConflictResolution": "AutoResolve",
        },
    }

    data = client.request("UpdateItem", payload)

    for msg in client.extract_items(data):
        if msg.get("ResponseClass") == "Error":
            raise RuntimeError(msg.get("MessageText", "UpdateItem failed."))
        for item in msg.get("Items", []):
            returned = item.get("ItemId", {})
            return returned.get("Id", item_id), returned.get("ChangeKey", "")
    return item_id, ""


def _set_field(field: str, value, *, task_key: str) -> dict:
    """Build one SetItemField update for a Task property."""
    return {
        "__type": "SetItemField:#Exchange",
        "Path": {"__type": "PropertyUri:#Exchange", "FieldURI": _FIELD[field]},
        "Item": {"__type": "Task:#Exchange", task_key: value},
    }


def _delete_field(field: str) -> dict:
    """Build one DeleteItemField update, clearing a Task property."""
    return {
        "__type": "DeleteItemField:#Exchange",
        "Path": {"__type": "PropertyUri:#Exchange", "FieldURI": _FIELD[field]},
    }


# ------------------------------------------------------------------
# Tools
# ------------------------------------------------------------------


@mcp.tool()
def get_tasks(
    task_folder: str = "tasks",
    limit: int = 25,
    offset: int = 0,
    include_completed: bool = False,
    include_body: bool = False,
    ctx: Context = None,
) -> str:
    """List tasks (Microsoft To Do items) from a task folder / To Do list.

    Args:
        task_folder: Which list to read. "tasks" (default) is the mailbox's
            default task list; a bare name is looked up as a To Do list
            under it (e.g. "Groceries"), and a "/"-delimited path or a raw
            opaque folder ID from get_folders(parent_folder_id="tasks")
            also works.
        limit: Maximum tasks to return after filtering (default 25, max 200).
        offset: How many matching tasks to skip - applied *after* the
            include_completed filter, not to the raw folder listing.
        include_completed: Include tasks whose status is Completed
            (default False - the useful default for "what's on my plate").
        include_body: Fetch each task's note/body as plain text. Costs one
            extra request per task; a task whose body can't be fetched gets
            `body_error` instead of failing the whole page.

    Returns:
        JSON {"tasks": [...], "count": n, "folder": ..., "scanned": n},
        due-date ascending with undated tasks last. `scanned` is how many
        raw items were read, so a caller can tell "no matches" apart from
        "hit the 500-item scan ceiling". `skipped_non_task_items` appears
        only when the folder held items that aren't tasks at all - which
        normally means `task_folder` is pointing at a mail folder.
    """
    try:
        client = _get_client(ctx)
        limit = max(1, min(limit, 200))
        offset = max(0, offset)

        folder_id = _resolve_task_folder(client, task_folder)
        if not folder_id:
            return json.dumps({"error": f"Task folder '{task_folder}' not found."})

        raw_items = _find_tasks(client, OWAClient.folder_id_dict(folder_id))
        task_items = [i for i in raw_items if _is_task_item(i)]
        tasks = [_to_public(i) for i in task_items]
        if not include_completed:
            tasks = [t for t in tasks if not t["is_complete"]]
        tasks.sort(key=_due_sort_key)
        page = tasks[offset : offset + limit]

        if include_body:
            for task in page:
                body, body_error = _get_task_body(client, task["item_id"])
                task["body"] = body
                if body_error:
                    task["body_error"] = body_error

        result = {
            "tasks": page,
            "count": len(page),
            "folder": task_folder,
            "scanned": len(raw_items),
        }
        skipped = len(raw_items) - len(task_items)
        if skipped:
            # Only reported when it happens, and it means one thing: the
            # folder holds items that aren't tasks, i.e. task_folder is
            # probably pointing at a mail folder.
            result["skipped_non_task_items"] = skipped
        return json.dumps(result, ensure_ascii=False)

    except SessionExpiredError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": f"Failed to list tasks: {e}"})


@mcp.tool()
def get_task(
    item_id: str,
    ctx: Context = None,
) -> str:
    """Get a single task's full details, including its note/body.

    Args:
        item_id: The Exchange ItemId of the task (from get_tasks/create_task).

    Returns:
        JSON task object: subject, status, is_complete, percent_complete,
        due_date, start_date, complete_date, reminder, importance,
        sensitivity, categories, owner, body, change_key, and
        parent_folder_id.
    """
    try:
        client = _get_client(ctx)

        payload = {
            "__type": "GetItemJsonRequest:#Exchange",
            "Header": _READ_HEADER,
            "Body": {
                "__type": "GetItemRequest:#Exchange",
                "ItemShape": {
                    "__type": "ItemResponseShape:#Exchange",
                    "BaseShape": "AllProperties",
                    "BodyType": "HTML",
                },
                "ItemIds": [{"__type": "ItemId:#Exchange", "Id": item_id}],
            },
        }

        data = client.request("GetItem", payload)

        for msg in client.extract_items(data):
            if msg.get("ResponseClass") == "Error":
                return json.dumps({"error": msg.get("MessageText", "GetItem failed.")})
            for item in msg.get("Items", []):
                task = _to_public(item)
                task["body"] = html_to_text(item.get("Body", {}).get("Value", ""))
                return json.dumps(task, ensure_ascii=False)

        return json.dumps({"error": f"Task '{item_id}' not found."})

    except SessionExpiredError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": f"Failed to get task: {e}"})


@mcp.tool()
def create_task(
    subject: str,
    due_date: str | None = None,
    start_date: str | None = None,
    body: str | None = None,
    status: str = "NotStarted",
    importance: str = "Normal",
    categories: list[str] | None = None,
    reminder: str | None = None,
    task_folder: str = "tasks",
    ctx: Context = None,
) -> str:
    """Create a new task (Microsoft To Do item).

    Args:
        subject: Task title. Required.
        due_date: Due date, YYYY-MM-DD (time-of-day is ignored - Exchange
            stores task dates as dates).
        start_date: Start date, same format.
        body: Free-text note stored as the task's body.
        status: NotStarted (default), InProgress, Completed,
            WaitingOnOthers, or Deferred.
        importance: Low, Normal (default), or High.
        categories: Category names to tag the task with. Any string works;
            see create_category to register one in the master list.
        reminder: Reminder time as "YYYY-MM-DD HH:MM" (an explicit time is
            required). Sets ReminderIsSet automatically. Interpreted in
            Russian Standard Time (UTC+3), which this codebase sends on
            every write, and read back in UTC - so a 09:30 reminder reads
            as 06:30. See the module docstring.
        task_folder: Which To Do list to create it in - see get_tasks.

    Returns:
        JSON object with the new task's item_id and change_key.
    """
    if status not in _VALID_STATUSES:
        return json.dumps({"error": f"Invalid status: {status}. Must be one of {list(_VALID_STATUSES)}."})
    if importance not in _VALID_IMPORTANCE:
        return json.dumps({"error": f"Invalid importance: {importance}. Must be one of {list(_VALID_IMPORTANCE)}."})

    try:
        client = _get_client(ctx)

        task_item = {
            "__type": "Task:#Exchange",
            "Subject": subject,
            "Status": status,
        }
        if importance != "Normal":
            task_item["Importance"] = importance
        if body:
            task_item["Body"] = {
                "__type": "BodyContentType:#Exchange",
                "BodyType": "Text",
                "Value": body,
            }
        if categories:
            task_item["Categories"] = categories

        try:
            if due_date:
                task_item["DueDate"] = _task_date(due_date)
            if start_date:
                task_item["StartDate"] = _task_date(start_date)
            if reminder:
                task_item["ReminderDueBy"] = _reminder_datetime(reminder)
                task_item["ReminderIsSet"] = True
        except ValueError as e:
            return json.dumps({"error": str(e)})

        folder_id = _resolve_task_folder(client, task_folder)
        if not folder_id:
            return json.dumps({"error": f"Task folder '{task_folder}' not found."})

        payload = {
            "__type": "CreateItemJsonRequest:#Exchange",
            "Header": _WRITE_HEADER,
            "Body": {
                "__type": "CreateItemRequest:#Exchange",
                "Items": [task_item],
                "SavedItemFolderId": {
                    "__type": "TargetFolderId:#Exchange",
                    "BaseFolderId": OWAClient.folder_id_dict(folder_id),
                },
            },
        }

        data = client.request("CreateItem", payload)

        for msg in client.extract_items(data):
            if msg.get("ResponseClass") == "Error":
                return json.dumps({
                    "error": msg.get("MessageText", "CreateItem failed."),
                    "response_code": msg.get("ResponseCode", ""),
                })
            for item in msg.get("Items", []):
                item_id = item.get("ItemId", {})
                return json.dumps({
                    "success": True,
                    "item_id": item_id.get("Id", ""),
                    "change_key": item_id.get("ChangeKey", ""),
                    "subject": subject,
                    "folder": task_folder,
                }, ensure_ascii=False)

        return json.dumps({"error": "Unexpected response", "raw": str(data)[:300]})

    except SessionExpiredError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": f"Failed to create task: {e}"})


@mcp.tool()
def update_task(
    item_id: str,
    subject: str | None = None,
    due_date: str | None = None,
    start_date: str | None = None,
    body: str | None = None,
    status: str | None = None,
    percent_complete: int | None = None,
    importance: str | None = None,
    categories: list[str] | None = None,
    reminder: str | None = None,
    clear_due_date: bool = False,
    clear_start_date: bool = False,
    clear_reminder: bool = False,
    ctx: Context = None,
) -> str:
    """Update an existing task. Only the arguments you pass are changed.

    Omitted arguments are left alone; the `clear_*` flags exist because
    "leave unchanged" and "erase" can't both be spelled `None`.

    `categories` replaces the whole list (pass the merged list you want).

    Args:
        item_id: The Exchange ItemId of the task to update.
        subject: New title.
        due_date / start_date: New dates, YYYY-MM-DD.
        body: New note text (replaces the existing body).
        status: NotStarted, InProgress, Completed, WaitingOnOthers, Deferred.
            Setting Completed here is equivalent to complete_task.
        percent_complete: 0-100. Mutually exclusive with `status` - Exchange
            treats them as the same underlying state and lets whichever it
            processes last win, so passing both is rejected rather than
            resolved arbitrarily. 100 marks the task complete.
        importance: Low, Normal, or High.
        categories: Replacement category list.
        reminder: New reminder time, "YYYY-MM-DD HH:MM" - same UTC+3
            interpretation as create_task's, see the module docstring.
        clear_due_date / clear_start_date: Remove that date entirely.
        clear_reminder: Turn the reminder off.

    Returns:
        JSON object with success, the resulting item_id/change_key, and the
        list of fields written. Note the item_id can differ from the one you
        passed when completing a recurring task - Exchange splits off a
        one-off item for the completed occurrence.
    """
    if status is not None and percent_complete is not None:
        return json.dumps({
            "error": "Pass either status or percent_complete, not both - Exchange treats "
                     "them as the same state and the last one processed wins."
        })
    if status is not None and status not in _VALID_STATUSES:
        return json.dumps({"error": f"Invalid status: {status}. Must be one of {list(_VALID_STATUSES)}."})
    if importance is not None and importance not in _VALID_IMPORTANCE:
        return json.dumps({"error": f"Invalid importance: {importance}. Must be one of {list(_VALID_IMPORTANCE)}."})
    if percent_complete is not None and not 0 <= percent_complete <= 100:
        return json.dumps({"error": f"Invalid percent_complete: {percent_complete}. Must be 0-100."})
    if due_date and clear_due_date:
        return json.dumps({"error": "Pass either due_date or clear_due_date, not both."})
    if start_date and clear_start_date:
        return json.dumps({"error": "Pass either start_date or clear_start_date, not both."})
    if reminder and clear_reminder:
        return json.dumps({"error": "Pass either reminder or clear_reminder, not both."})

    updates: list[dict] = []
    written: list[str] = []

    try:
        if subject is not None:
            updates.append(_set_field("subject", subject, task_key="Subject"))
            written.append("subject")
        if body is not None:
            updates.append(_set_field(
                "body",
                {"__type": "BodyContentType:#Exchange", "BodyType": "Text", "Value": body},
                task_key="Body",
            ))
            written.append("body")
        if categories is not None:
            updates.append(_set_field("categories", categories, task_key="Categories"))
            written.append("categories")
        if importance is not None:
            updates.append(_set_field("importance", importance, task_key="Importance"))
            written.append("importance")
        if status is not None:
            updates.append(_set_field("status", status, task_key="Status"))
            written.append("status")
        if percent_complete is not None:
            updates.append(_set_field("percent_complete", percent_complete, task_key="PercentComplete"))
            written.append("percent_complete")
        if due_date:
            updates.append(_set_field("due_date", _task_date(due_date), task_key="DueDate"))
            written.append("due_date")
        if start_date:
            updates.append(_set_field("start_date", _task_date(start_date), task_key="StartDate"))
            written.append("start_date")
        if reminder:
            updates.append(_set_field("reminder_due_by", _reminder_datetime(reminder), task_key="ReminderDueBy"))
            updates.append(_set_field("reminder_is_set", True, task_key="ReminderIsSet"))
            written.append("reminder")
        if clear_due_date:
            updates.append(_delete_field("due_date"))
            written.append("clear_due_date")
        if clear_start_date:
            updates.append(_delete_field("start_date"))
            written.append("clear_start_date")
        if clear_reminder:
            updates.append(_set_field("reminder_is_set", False, task_key="ReminderIsSet"))
            written.append("clear_reminder")
    except ValueError as e:
        return json.dumps({"error": str(e)})

    if not updates:
        return json.dumps({"error": "Nothing to update - pass at least one field to change."})

    try:
        client = _get_client(ctx)
        new_id, new_change_key = _apply_updates(client, item_id, updates)
        return json.dumps({
            "success": True,
            "item_id": new_id,
            "change_key": new_change_key,
            "requested_item_id": item_id,
            "updated_fields": written,
        }, ensure_ascii=False)
    except SessionExpiredError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        # Report which paths were attempted: an "Invalid argument used to call
        # method UpdateItem" here means one FieldURI spelling in _FIELD is
        # wrong for this backend, and this is the only clue to which.
        return json.dumps({
            "error": f"Failed to update task: {e}",
            "attempted_fields": written,
        })


@mcp.tool()
def complete_task(
    item_ids: list[str],
    completed: bool = True,
    ctx: Context = None,
) -> str:
    """Mark one or more tasks complete (or reopen them).

    Writes `Status` only - never PercentComplete or CompleteDate alongside
    it, since Exchange resolves the three against each other by whichever
    it processes last.

    Args:
        item_ids: Exchange ItemIds of the tasks to update.
        completed: True (default) sets Status=Completed; False reopens the
            task with Status=NotStarted.

    Returns:
        JSON object with per-item results. For a recurring task, Exchange
        creates a *new* one-off item for the completed occurrence and rolls
        the original ItemId forward to the next occurrence, so `item_id` in
        a result row can differ from the `requested_item_id`.
    """
    status = "Completed" if completed else "NotStarted"
    updates = [_set_field("status", status, task_key="Status")]

    try:
        client = _get_client(ctx)
        updated, failed = [], []
        for iid in item_ids:
            try:
                new_id, new_change_key = _apply_updates(client, iid, updates)
                updated.append({
                    "requested_item_id": iid,
                    "item_id": new_id,
                    "change_key": new_change_key,
                })
            except SessionExpiredError:
                raise
            except Exception as exc:
                failed.append({"item_id": iid, "error": str(exc)})

        return json.dumps({
            "success": not failed,
            "status": status,
            "updated": updated,
            "updated_count": len(updated),
            "failed": failed,
            "failed_count": len(failed),
        }, ensure_ascii=False)
    except SessionExpiredError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": f"Failed to complete tasks: {e}"})


@mcp.tool()
def delete_task(
    item_ids: list[str],
    permanent: bool = False,
    ctx: Context = None,
) -> str:
    """Delete one or more tasks.

    Note: a deleted task's ItemId stays resolvable on this backend, so
    get_task will still return the item afterwards (with a bumped
    change_key). Confirm a deletion via get_tasks on its folder, not by a
    by-ID read - see the module docstring.

    Args:
        item_ids: Exchange ItemIds of the tasks to delete.
        permanent: If True, HardDelete. Otherwise move to Deleted Items
            (default), where the task can still be recovered.

    Returns:
        JSON object with success status and a summary message.
    """
    try:
        client = _get_client(ctx)

        payload = {
            "__type": "DeleteItemJsonRequest:#Exchange",
            "Header": _WRITE_HEADER,
            "Body": {
                "__type": "DeleteItemRequest:#Exchange",
                "ItemIds": [{"__type": "ItemId:#Exchange", "Id": iid} for iid in item_ids],
                "DeleteType": "HardDelete" if permanent else "MoveToDeletedItems",
                "AffectedTaskOccurrences": "AllOccurrences",
            },
        }

        data = client.request("DeleteItem", payload)

        errors = []
        for msg in client.extract_items(data):
            if msg.get("ResponseClass") == "Error":
                errors.append(msg.get("MessageText", "Unknown error"))

        if errors:
            return json.dumps({"error": "; ".join(errors)})

        action = "permanently deleted" if permanent else "moved to Deleted Items"
        return json.dumps({
            "success": True,
            "message": f"{len(item_ids)} task(s) {action}.",
        })

    except SessionExpiredError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": f"Failed to delete tasks: {e}"})
