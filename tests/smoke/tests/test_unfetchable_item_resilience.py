"""Smoke test: one unreadable-at-full-shape message must not break a listing.

Some real messages cannot be fetched with `BaseShape: "AllProperties"` — OWA's
own GetItem throws `System.Runtime.Serialization.SerializationException`
(HTTP 500) partway through serialising the response. That is a server-side
fault in the response path, so no request-side change makes *that shape* work,
and the thing this client controls is how much collateral damage it causes.
Before the fix (2026-09-10) a single such message made
`get_emails(include_body=True)` return nothing but an error for the entire page.

Note what this fault is *not*: it is not an unreachable item. Corrected
2026-09-11 — a narrow GetItem shape (IdOnly plus named properties) reads the
same item fine, which is how the category tools were fixed to work on it
instead of skipping it. That behaviour has its own test
(`test_meeting_request_categories.py`, which owns the read/write round-trip and
the mutation it implies); this module deliberately performs no writes at all
and stays focused on listing degradation plus the typed error `get_email`
returns when the full-property read is genuinely the only option.

This test is a no-op unless the mailbox actually contains such a message: it
discovers them via the `body_error` marker rather than hardcoding an id, and
skips (recording why) if the mailbox is currently healthy.

Run standalone:
    python -m tests.smoke.tests.test_unfetchable_item_resilience
"""

import sys

from tests.smoke.mcp_client import call, run, session
from tests.smoke.results import is_error_payload, record

LIST_ARGS = {"folder": "Inbox", "limit": 10, "include_body": True}


async def main() -> bool:
    ok = True
    async with session() as s:
        # 1. The listing must survive, whatever it contains.
        info = await call(s, "get_emails", **LIST_ARGS)
        err = is_error_payload(info)
        if err or not isinstance(info, dict):
            record("get_emails", LIST_ARGS,
                   "EXCEPTION" if "_exception" in str(info) else "TOOL_ERROR",
                   f"whole page failed instead of degrading individual rows: {err}")
            return False

        rows = info.get("emails", [])
        degraded = [r for r in rows if r.get("body_error")]
        healthy = [r for r in rows if not r.get("body_error")]

        if not rows:
            record("get_emails", LIST_ARGS, "TOOL_ERROR", "no conversations returned")
            return False

        # Healthy rows must still be fully populated - degradation must be per-row,
        # not a blanket "give up on bodies for this page".
        if healthy and not any(r.get("body") for r in healthy):
            record("get_emails", LIST_ARGS, "TOOL_ERROR",
                   f"{len(healthy)} row(s) reported no fetch error yet none has a body")
            ok = False

        record("get_emails", LIST_ARGS, "OK",
               f"{len(rows)} row(s) returned: {len(healthy)} with body, "
               f"{len(degraded)} degraded with body_error")

        if not degraded:
            record("get_emails (resilience)", LIST_ARGS, "OK",
                   "no unfetchable message in this page — resilience path not exercised "
                   "(this mailbox had 2 such messages on 2026-09-10; nothing to assert today)")
            return ok

        bad_ids = [r["item_id"] for r in degraded if r.get("item_id")]

        # 2. get_email on such an item must explain itself rather than look like
        #    a bad item_id or an expired session — and must say so in a form a
        #    client can branch on, since the whole reason this matters is that
        #    callers were reduced to substring-matching an HTTP 500 message.
        single = await call(s, "get_email", item_id=bad_ids[0])
        if isinstance(single, dict) and "SerializationException" in str(single.get("error", "")):
            problems = []
            if single.get("error_code") != "item_not_serializable":
                problems.append(f"error_code={single.get('error_code')!r}, "
                                "expected 'item_not_serializable'")
            if not single.get("hint"):
                problems.append("no explanatory hint")
            if problems:
                record("get_email", {"item_id": bad_ids[0]}, "TOOL_ERROR", "; ".join(problems))
                ok = False
            else:
                record("get_email", {"item_id": bad_ids[0]}, "OK",
                       "typed error_code=item_not_serializable plus a diagnostic hint")
        else:
            record("get_email", {"item_id": bad_ids[0]}, "OK",
                   f"item is fetchable at full shape again — nothing to assert: {str(single)[:100]}")

        return ok


if __name__ == "__main__":
    sys.exit(0 if run(main()) else 1)
