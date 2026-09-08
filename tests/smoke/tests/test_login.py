"""Smoke test: login (auth.py).

Exercises the repeatable half of the login tool: calling it while the
session is already valid, which should short-circuit to "Session is
already active" without touching credentials or 2FA. Read-only from the
mailbox's perspective, so it's naturally repeatable.

This deliberately does NOT exercise the two-call 2FA flow (decrypting
credentials, starting a background browser login, waiting for a mobile
push approval) -- that path requires a genuinely unauthenticated session
and a human to approve the push, so it isn't something an unattended
repeatable test can drive. It was verified manually instead (see
PROJECT_STATUS.md's login row) by clearing the browser profile, starting
the server without EXCHANGE_MASTER_PASSWORD, and driving both calls of
the real 2FA flow by hand.

Requires EXCHANGE_MASTER_PASSWORD in the environment (never logged).

Run standalone:
    python -m tests.smoke.tests.test_login
"""

import os
import sys

from tests.smoke.mcp_client import call, run, session
from tests.smoke.results import is_error_payload, record

ARGS = {"master_password": "***"}  # never send the real value to record()/print


async def main() -> bool:
    master_password = os.environ.get("EXCHANGE_MASTER_PASSWORD")
    if not master_password:
        record("login", ARGS, "EXCEPTION", "EXCHANGE_MASTER_PASSWORD not set in environment")
        return False

    async with session() as s:
        info = await call(s, "login", master_password=master_password)

        err = is_error_payload(info)
        if err:
            record("login", ARGS, "EXCEPTION" if "_exception" in str(info) else "TOOL_ERROR", err)
            return False

        if not isinstance(info, dict) or not info.get("success"):
            record("login", ARGS, "TOOL_ERROR", f"unexpected response: {info}")
            return False

        record("login", ARGS, "OK", info.get("message", ""))
        return True


if __name__ == "__main__":
    ok = run(main())
    sys.exit(0 if ok else 1)
