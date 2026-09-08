"""Smoke test: list_categories, create_category, rename_category,
delete_category (categories.py).

Chains all four master-category-list CRUD tools around one disposable,
uniquely-tagged category: create -> verify present via list -> rename ->
verify renamed -> delete -> verify gone.

create_category/delete_category's own tool parameter is named `name`,
which collides with call()'s own `name` parameter (the tool's name) --
call them directly instead of through call() (see test_folder_lifecycle.py
for the same pattern with create_folder).

Repeatable: the category name includes a timestamp, so re-runs never
collide with a leftover category from a prior run.

Run standalone:
    python -m tests.smoke.tests.test_category_lifecycle
"""

import json
import sys
import time

from tests.smoke.mcp_client import call, run, session
from tests.smoke.results import is_error_payload, record

TAG = f"cat-smoke-{int(time.time())}"
CATEGORY_NAME = f"[{TAG}]"
RENAMED_NAME = f"[{TAG}]-renamed"


async def _call_direct(s, tool: str, args: dict):
    """Like call(), but for tools whose own parameter is named `name`."""
    result = await s.call_tool(tool, args)
    text = "".join(getattr(b, "text", "") for b in result.content)
    if result.isError:
        return {"_transport_error": True, "raw": text}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_non_json": True, "raw": text}


async def main() -> bool:
    async with session() as s:
        # 1. create_category
        create_args = {"name": CATEGORY_NAME, "color": 5}
        create_info = await _call_direct(s, "create_category", create_args)
        err = is_error_payload(create_info)
        if err or not isinstance(create_info, dict) or not create_info.get("success"):
            record("create_category", create_args, "EXCEPTION" if "_exception" in str(create_info) else "TOOL_ERROR",
                   err or f"unexpected shape: {create_info}")
            return False
        record("create_category", create_args, "OK", f"{len(create_info.get('categories', []))} categories total")

        # 2. list_categories -- verify the new category is present
        list_info = await call(s, "list_categories")
        err = is_error_payload(list_info)
        if err or not isinstance(list_info, list):
            record("list_categories", {}, "EXCEPTION" if "_exception" in str(list_info) else "TOOL_ERROR",
                   err or f"unexpected shape: {list_info}")
            return False
        if not any(c.get("name") == CATEGORY_NAME for c in list_info):
            record("list_categories", {}, "TOOL_ERROR", f"'{CATEGORY_NAME}' not found after create")
            return False
        record("list_categories", {}, "OK", f"{len(list_info)} categories, tagged one present")

        # 3. rename_category
        rename_args = {"old_name": CATEGORY_NAME, "new_name": RENAMED_NAME}
        rename_info = await call(s, "rename_category", **rename_args)
        err = is_error_payload(rename_info)
        if err or not isinstance(rename_info, dict) or not rename_info.get("success"):
            record("rename_category", rename_args, "EXCEPTION" if "_exception" in str(rename_info) else "TOOL_ERROR",
                   err or f"unexpected shape: {rename_info}")
            return False
        names = [c.get("name") for c in rename_info.get("categories", [])]
        if RENAMED_NAME not in names or CATEGORY_NAME in names:
            record("rename_category", rename_args, "TOOL_ERROR",
                   f"rename didn't take effect as expected: {names}")
            return False
        record("rename_category", rename_args, "OK", "renamed and verified")

        # 4. delete_category (final cleanup)
        delete_args = {"name": RENAMED_NAME}
        delete_info = await _call_direct(s, "delete_category", delete_args)
        err = is_error_payload(delete_info)
        if err or not isinstance(delete_info, dict) or not delete_info.get("success"):
            record("delete_category", delete_args, "EXCEPTION" if "_exception" in str(delete_info) else "TOOL_ERROR",
                   err or f"unexpected shape: {delete_info}")
            return False
        names = [c.get("name") for c in delete_info.get("categories", [])]
        if RENAMED_NAME in names:
            record("delete_category", delete_args, "TOOL_ERROR", "category still present after delete")
            return False
        record("delete_category", delete_args, "OK", "deleted and verified gone")

        return True


if __name__ == "__main__":
    ok = run(main())
    sys.exit(0 if ok else 1)
