"""Smoke test: get_emails (email.py).

Lists a few emails from Inbox. Read-only, no mailbox state is created or
changed, so it's naturally repeatable.

Run standalone:
    python -m tests.smoke.tests.test_get_emails
"""

import sys

from tests.smoke.mcp_client import call, run, session
from tests.smoke.results import is_error_payload, record

ARGS = {"folder": "Inbox", "limit": 5, "include_body": False}


async def main() -> bool:
    async with session() as s:
        info = await call(s, "get_emails", **ARGS)

        err = is_error_payload(info)
        if err:
            record("get_emails", ARGS, "EXCEPTION" if "_exception" in str(info) else "TOOL_ERROR", err)
            return False

        if not isinstance(info, dict) or "emails" not in info:
            record("get_emails", ARGS, "EXCEPTION", f"unexpected shape: {info}")
            return False

        record("get_emails", ARGS, "OK", f"{len(info['emails'])} conversation(s) returned")
        return True


if __name__ == "__main__":
    ok = run(main())
    sys.exit(0 if ok else 1)
