"""Smoke test: session/login.

Verifies the shared browser profile is authenticated by calling check_session.
Startup (server.py's _startup) may still be mid-login in the background if
EXCHANGE_MASTER_PASSWORD is set -- that path is separate from the `login`
MCP tool's own pending-task tracking, so this test deliberately does NOT
call the `login` tool itself (calling it while a startup login is still
in flight would race two logins against the same browser context). It
just polls check_session, which is cheap and read-only.

Repeatable: read-only, no mailbox state is created or changed.

Run standalone:
    python -m tests.smoke.tests.test_check_session
"""

import asyncio
import sys

from tests.smoke.mcp_client import call, run, session
from tests.smoke.results import record

POLL_ATTEMPTS = 6
POLL_DELAY_SECONDS = 15


async def main() -> bool:
    async with session() as s:
        for attempt in range(POLL_ATTEMPTS):
            info = await call(s, "check_session")

            if not isinstance(info, dict) or "authenticated" not in info:
                # Transport-level failure (_exception/_transport_error/_non_json).
                record("check_session", {}, "EXCEPTION", str(info))
                return False

            if info.get("authenticated"):
                record("check_session", {}, "OK",
                       f"mailbox={info.get('mailbox')} unread={info.get('unread')}")
                return True

            note = info.get("error", "not authenticated")
            if attempt < POLL_ATTEMPTS - 1:
                record("check_session", {}, "TOOL_ERROR",
                       f"{note} -- retrying in {POLL_DELAY_SECONDS}s "
                       "(startup login may still be waiting on 2FA)")
                await asyncio.sleep(POLL_DELAY_SECONDS)
            else:
                record("check_session", {}, "TOOL_ERROR", note)

        return False


if __name__ == "__main__":
    ok = run(main())
    sys.exit(0 if ok else 1)
