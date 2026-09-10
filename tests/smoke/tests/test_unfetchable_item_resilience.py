"""Smoke test: one unfetchable message must not break a whole listing or batch.

Some real messages cannot be fetched at all — OWA's own GetItem throws
`System.Runtime.Serialization.SerializationException` (HTTP 500) on them. That
is a server-side fault with no client-side workaround, so the only thing this
client controls is how much collateral damage it causes. Before the fix
(2026-09-10) a single such message made `get_emails(include_body=True)` return
nothing but an error for the entire page, and made
`assign_email_categories`/`remove_email_categories` abort mid-batch while
silently keeping whatever they had already written.

This test is a no-op unless the mailbox actually contains such a message: it
discovers them via the `body_error` marker rather than hardcoding an id, and
skips (recording why) if the mailbox is currently healthy. The category calls
below are deliberately made with ONLY unfetchable ids, so they mutate nothing.

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

        # 2. Bulk category writes must skip the bad ids and say so, rather than
        #    aborting or claiming success. Passing ONLY bad ids mutates nothing.
        bad_ids = [r["item_id"] for r in degraded if r.get("item_id")]
        for tool, verb in (("assign_email_categories", "assign"),
                           ("remove_email_categories", "remove")):
            args = {"item_ids": bad_ids, "categories": ["Save The Date"]}
            res = await call(s, tool, **args)
            if not isinstance(res, dict) or "updated_count" not in res:
                record(tool, args, "TOOL_ERROR",
                       f"expected a partial-result payload, got: {str(res)[:180]}")
                ok = False
                continue
            if res.get("success") is not False or res.get("updated_count") != 0:
                record(tool, args, "TOOL_ERROR",
                       f"claimed success for items it could not read: {str(res)[:180]}")
                ok = False
            elif res.get("failed_count") != len(bad_ids):
                record(tool, args, "TOOL_ERROR",
                       f"expected failed_count={len(bad_ids)}, got {res.get('failed_count')}")
                ok = False
            else:
                record(tool, args, "OK",
                       f"{verb} skipped all {len(bad_ids)} unfetchable id(s) and reported them")

        # 3. get_email on such an item must explain itself rather than look like a
        #    bad item_id or an expired session.
        single = await call(s, "get_email", item_id=bad_ids[0])
        if isinstance(single, dict) and "SerializationException" in str(single.get("error", "")):
            if single.get("hint"):
                record("get_email", {"item_id": bad_ids[0]}, "OK",
                       "server-side serialization failure reported with a diagnostic hint")
            else:
                record("get_email", {"item_id": bad_ids[0]}, "TOOL_ERROR",
                       "serialization failure reported without the explanatory hint")
                ok = False
        else:
            record("get_email", {"item_id": bad_ids[0]}, "OK",
                   f"item is fetchable again — nothing to assert: {str(single)[:100]}")

        return ok


if __name__ == "__main__":
    sys.exit(0 if run(main()) else 1)
