"""Smoke test: find_person (people.py).

Searches the corporate directory (ResolveNames) for the mailbox's own
address -- guaranteed to resolve, and read-only, so it's naturally
repeatable.

Needs the mailbox's own address in $EXCHANGE_SMOKE_SELF_EMAIL (see
tests/smoke/config.py for why it isn't written down here) -- it is the search
query, and using this mailbox's own address is what guarantees a hit.

Run standalone:
    EXCHANGE_SMOKE_SELF_EMAIL=you@example.com \
        python -m tests.smoke.tests.test_find_person
"""

import sys

from tests.smoke.config import require_self_email
from tests.smoke.mcp_client import call, run, session
from tests.smoke.results import is_error_payload, record


async def main() -> bool:
    # args is built here rather than at module level: the query comes from the
    # environment, and an import-time failure couldn't be recorded.
    self_email = require_self_email("find_person")
    if not self_email:
        return False

    args = {"query": self_email}

    async with session() as s:
        info = await call(s, "find_person", **args)

        err = is_error_payload(info)
        if err:
            record("find_person", args, "EXCEPTION" if "_exception" in str(info) else "TOOL_ERROR", err)
            return False

        if not isinstance(info, list):
            record("find_person", args, "EXCEPTION", f"unexpected shape: {info}")
            return False

        record("find_person", args, "OK", f"{len(info)} match(es) returned")
        return True


if __name__ == "__main__":
    ok = run(main())
    sys.exit(0 if ok else 1)
