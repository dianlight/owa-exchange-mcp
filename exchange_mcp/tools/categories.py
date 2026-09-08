"""Category (Outlook "Master Category List") tools for the Exchange MCP server.

The master category list is not exposed through the classic EWS
GetUserConfiguration/UpdateUserConfiguration UserConfiguration-object
pattern used elsewhere in this codebase (that action pair 500s with a
NullReferenceException for the "CategoryList" config name on this
backend). Instead OWA's own web client manages it through a dedicated,
non-EWS action, `UpdateMasterCategoryList`, sent like every other OWA
call through `OWAClient.request_header_payload()` (JSON in the
X-OWA-UrlPostData header) but with a flat `{"request": {...}}` body -
no `Header`/`Body` JSON-request envelope. A single call both reads (all
list arguments empty) and writes (Add/Remove) the list, always
returning the complete resulting `MasterList`. There is no rename
primitive - reusing an existing category's Id in AddCategoryList mints
a brand-new entry rather than updating it in place - so rename is
implemented as a Remove + Add pair in one request.
"""

import json
import uuid
from datetime import datetime, timezone

from mcp.server.fastmcp import Context

from exchange_mcp.server import mcp, AppContext
from exchange_mcp.owa_client import OWAClient, SessionExpiredError

_ACTION = "UpdateMasterCategoryList"


def _get_client(ctx: Context) -> OWAClient:
    """Extract the OWAClient from the MCP lifespan context."""
    app_ctx: AppContext = ctx.request_context.lifespan_context
    return app_ctx.client


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _mutate(client: OWAClient, add: list[dict] | None = None, remove: list[str] | None = None) -> list[dict]:
    """Call UpdateMasterCategoryList and return the resulting MasterList.

    `add` entries are full {Name, Color, Id, LastTimeUsed, KeyboardShortcut}
    dicts; `remove` is a list of plain category name strings. Passing
    neither performs a pure read.
    """
    payload = {
        "request": {
            "__type": "UpdateMasterCategoryListRequest:#Exchange",
            "AddCategoryList": add or [],
            "RemoveCategoryList": remove or [],
            "ChangeCategoryColorList": [],
            "UpdateCategoryLastTimeUsedList": [],
            "ChangeCategoryKeyboardShortcutList": [],
        }
    }
    data = client.request_header_payload(_ACTION, payload)
    return data.get("MasterList", [])


def _to_public(entry: dict) -> dict:
    return {"name": entry.get("Name", ""), "color": entry.get("Color", 0)}


@mcp.tool()
def list_categories(ctx: Context = None) -> str:
    """List every category in the mailbox's master category list.

    Returns:
        JSON array of {"name": str, "color": int} objects. `color` is a
        0-24 preset color index (0 = None); see create_category for the
        preset name table.
    """
    try:
        client = _get_client(ctx)
        master_list = _mutate(client)
        return json.dumps([_to_public(c) for c in master_list], ensure_ascii=False)
    except SessionExpiredError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": f"Failed to list categories: {e}"})


@mcp.tool()
def create_category(
    name: str,
    color: int = 0,
    ctx: Context = None,
) -> str:
    """Create a new category in the mailbox's master category list.

    Args:
        name: Category display name. Must be unique (case-insensitive).
        color: Preset color index 0-24 (default 0 = None). Preset map:
            1 Red, 2 Orange, 3 Brown, 4 Yellow, 5 Green, 6 Teal, 7 Olive,
            8 Blue, 9 Purple, 10 Cranberry, 11 Steel, 12 DarkSteel, 13 Gray,
            14 DarkGray, 15 Black, 16 DarkRed, 17 DarkOrange, 18 DarkBrown,
            19 DarkYellow, 20 DarkGreen, 21 DarkTeal, 22 DarkOlive,
            23 DarkBlue, 24 DarkPurple, 25 DarkCranberry.

    Returns:
        JSON object with success status and the updated category list.
    """
    try:
        client = _get_client(ctx)
        existing = _mutate(client)

        if any(c["Name"].lower() == name.lower() for c in existing):
            return json.dumps({"error": f"Category '{name}' already exists."})

        new_entry = {
            "Name": name,
            "Color": color,
            "Id": str(uuid.uuid4()),
            "LastTimeUsed": _now_iso(),
            "KeyboardShortcut": 0,
        }
        updated = _mutate(client, add=[new_entry])
        return json.dumps({"success": True, "categories": [_to_public(c) for c in updated]}, ensure_ascii=False)
    except SessionExpiredError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": f"Failed to create category: {e}"})


@mcp.tool()
def rename_category(
    old_name: str,
    new_name: str,
    ctx: Context = None,
) -> str:
    """Rename an existing category in the master category list.

    Note: this only renames the entry in the master list - items already
    tagged with `old_name` keep that string in their Categories field and
    will not automatically show `new_name` (Outlook itself has the same
    limitation; categories are just strings on each item).

    Args:
        old_name: Current category name (case-insensitive match).
        new_name: New category name.

    Returns:
        JSON object with success status and the updated category list.
    """
    try:
        client = _get_client(ctx)
        existing = _mutate(client)

        match = next((c for c in existing if c["Name"].lower() == old_name.lower()), None)
        if not match:
            return json.dumps({"error": f"Category '{old_name}' not found."})
        if any(c["Name"].lower() == new_name.lower() for c in existing if c is not match):
            return json.dumps({"error": f"Category '{new_name}' already exists."})

        new_entry = {
            "Name": new_name,
            "Color": match.get("Color", 0),
            "Id": str(uuid.uuid4()),
            "LastTimeUsed": _now_iso(),
            "KeyboardShortcut": match.get("KeyboardShortcut", 0),
        }
        updated = _mutate(client, add=[new_entry], remove=[match["Name"]])
        return json.dumps({"success": True, "categories": [_to_public(c) for c in updated]}, ensure_ascii=False)
    except SessionExpiredError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": f"Failed to rename category: {e}"})


@mcp.tool()
def delete_category(
    name: str,
    ctx: Context = None,
) -> str:
    """Delete a category from the master category list.

    Note: this only removes the entry from the master list - items already
    tagged with this category keep the string in their Categories field
    (same limitation as Outlook itself). Use find_emails_by_category /
    find_events_by_category first if you want to untag items too.

    Args:
        name: Category name to delete (case-insensitive match).

    Returns:
        JSON object with success status and the updated category list.
    """
    try:
        client = _get_client(ctx)
        existing = _mutate(client)

        match = next((c for c in existing if c["Name"].lower() == name.lower()), None)
        if not match:
            return json.dumps({"error": f"Category '{name}' not found."})

        updated = _mutate(client, remove=[match["Name"]])
        return json.dumps({"success": True, "categories": [_to_public(c) for c in updated]}, ensure_ascii=False)
    except SessionExpiredError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": f"Failed to delete category: {e}"})
