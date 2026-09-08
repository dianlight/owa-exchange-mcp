"""Smoke test: get_folders (folders.py).

Lists top-level mail folders. Read-only, no mailbox state is created or
changed, so it's naturally repeatable.

Run standalone:
    python -m tests.smoke.tests.test_get_folders
"""

import sys

from tests.smoke.mcp_client import call, run, session
from tests.smoke.results import is_error_payload, record

ARGS = {"parent_folder_id": "msgfolderroot", "recursive": False}


async def main() -> bool:
    async with session() as s:
        info = await call(s, "get_folders", **ARGS)

        err = is_error_payload(info)
        if err:
            record("get_folders", ARGS, "EXCEPTION" if "_exception" in str(info) else "TOOL_ERROR", err)
            return False

        if not isinstance(info, list):
            record("get_folders", ARGS, "EXCEPTION", f"unexpected shape: {info}")
            return False

        record("get_folders", ARGS, "OK", f"{len(info)} folder(s) returned")
        return True


if __name__ == "__main__":
    ok = run(main())
    sys.exit(0 if ok else 1)
