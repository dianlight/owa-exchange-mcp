"""Smoke test: find_person (people.py).

Searches the corporate directory (ResolveNames) for the mailbox's own
address -- guaranteed to resolve, and read-only, so it's naturally
repeatable.

Run standalone:
    python -m tests.smoke.tests.test_find_person
"""

import sys

from tests.smoke.mcp_client import call, run, session
from tests.smoke.results import is_error_payload, record

ARGS = {"query": "lucio.tarantino@unipol.it"}


async def main() -> bool:
    async with session() as s:
        info = await call(s, "find_person", **ARGS)

        err = is_error_payload(info)
        if err:
            record("find_person", ARGS, "EXCEPTION" if "_exception" in str(info) else "TOOL_ERROR", err)
            return False

        if not isinstance(info, list):
            record("find_person", ARGS, "EXCEPTION", f"unexpected shape: {info}")
            return False

        record("find_person", ARGS, "OK", f"{len(info)} match(es) returned")
        return True


if __name__ == "__main__":
    ok = run(main())
    sys.exit(0 if ok else 1)
