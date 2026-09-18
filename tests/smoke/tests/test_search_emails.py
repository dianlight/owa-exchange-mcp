"""Smoke test: search_emails (email.py).

Runs a plain-phrase AQS search and a keyword-qualified one against Inbox,
plus a search_all_folders=True call that exercises the Substrate Search
path (OWAClient.search_conversations_substrate) added 2026-09-17 - or its
BearerModeRequiredError/per-folder fallback on a classic-OWA tenant, since
either is a valid outcome depending on which backend the mailbox is on.
Read-only, no mailbox state is created or changed, so it's naturally repeatable.

Run standalone:
    python -m tests.smoke.tests.test_search_emails
"""

import sys

from tests.smoke.mcp_client import call, run, session
from tests.smoke.results import is_error_payload, record

PLAIN_ARGS = {"query": "the", "folder": "Inbox", "limit": 5}
KEYWORD_ARGS = {"query": "isread:true", "folder": "Inbox", "limit": 5}
ALL_FOLDERS_ARGS = {"query": "the", "search_all_folders": True, "limit": 5}


async def main() -> bool:
    async with session() as s:
        info = await call(s, "search_emails", **PLAIN_ARGS)

        err = is_error_payload(info)
        if err:
            record("search_emails", PLAIN_ARGS, "EXCEPTION" if "_exception" in str(info) else "TOOL_ERROR", err)
            return False

        if not isinstance(info, dict) or "emails" not in info:
            record("search_emails", PLAIN_ARGS, "EXCEPTION", f"unexpected shape: {info}")
            return False

        record("search_emails", PLAIN_ARGS, "OK", f"{len(info['emails'])} message(s) returned")

        info2 = await call(s, "search_emails", **KEYWORD_ARGS)
        err2 = is_error_payload(info2)
        if err2:
            record("search_emails", KEYWORD_ARGS, "EXCEPTION" if "_exception" in str(info2) else "TOOL_ERROR", err2)
            return False

        if not isinstance(info2, dict) or "emails" not in info2:
            record("search_emails", KEYWORD_ARGS, "EXCEPTION", f"unexpected shape: {info2}")
            return False

        record("search_emails", KEYWORD_ARGS, "OK", f"{len(info2['emails'])} message(s) returned")

        info3 = await call(s, "search_emails", **ALL_FOLDERS_ARGS)
        err3 = is_error_payload(info3)
        if err3:
            record("search_emails", ALL_FOLDERS_ARGS, "EXCEPTION" if "_exception" in str(info3) else "TOOL_ERROR", err3)
            return False

        if not isinstance(info3, dict) or "emails" not in info3:
            record("search_emails", ALL_FOLDERS_ARGS, "EXCEPTION", f"unexpected shape: {info3}")
            return False

        backend = "substrate" if info3.get("used_substrate_search") else "per-folder fallback"
        record(
            "search_emails", ALL_FOLDERS_ARGS, "OK",
            f"{len(info3['emails'])} result(s) returned via {backend}",
        )
        return True


if __name__ == "__main__":
    ok = run(main())
    sys.exit(0 if ok else 1)
