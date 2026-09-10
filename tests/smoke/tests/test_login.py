"""Smoke test: login (tools/auth.py).

Exercises the repeatable half of the login tool: calling it while the session is
already valid, which should short-circuit to "Session is already active" without
opening a browser window. Read-only from the mailbox's perspective, so it's
naturally repeatable.

This deliberately does NOT exercise the interactive path (opening a visible
sign-in window and waiting for a human to type an address, a password and
approve 2FA) -- that needs a genuinely unauthenticated profile and a person at
the keyboard, so it isn't something an unattended test can drive. Nor does it
pass force=True, which would open that window and leave it open. Verify it
manually instead (see PROJECT_STATUS.md's login row) by pointing
EXCHANGE_BROWSER_PROFILE_DIR at an empty directory, starting the server, and
driving both calls of the real flow by hand.

Needs no credentials in the environment -- there are none anywhere in this
server anymore.

Run standalone:
    python -m tests.smoke.tests.test_login
"""

import sys

from tests.smoke.mcp_client import call, run, session
from tests.smoke.results import is_error_payload, record

ARGS: dict = {}


async def main() -> bool:
    async with session() as s:
        info = await call(s, "login")

        err = is_error_payload(info)
        if err:
            record("login", ARGS, "EXCEPTION" if "_exception" in str(info) else "TOOL_ERROR", err)
            return False

        if not isinstance(info, dict):
            record("login", ARGS, "TOOL_ERROR", f"unexpected response: {info}")
            return False

        if not info.get("success"):
            # A window-opening response means the profile wasn't authenticated,
            # so this run can't be the repeatable already-active check. Report it
            # as such rather than as a tool bug, and don't leave it hanging: the
            # server's own startup would have opened the same window.
            record("login", ARGS, "TOOL_ERROR",
                   f"profile is not authenticated, so the idempotent path wasn't exercised: {info}")
            return False

        record("login", ARGS, "OK", info.get("message", ""))
        return True


if __name__ == "__main__":
    ok = run(main())
    sys.exit(0 if ok else 1)
