"""Smoke test: set_email_flag (#115) + flag_status on get_email/get_emails (#101/#102).

The round-trip below (write a status, then read it back) is the real check: a
successful write call alone proves nothing if the read can't observe the result.
Both halves were unverified assumptions until 2026-09-10, and the write in
particular only works with one specific wire encoding -- FieldURI `item:Flag`
paired with `__type: "FlagType:#Exchange"`. The obvious spellings (`message:Flag`,
and PidLidFlagStatus 0x8530 as an ExtendedFieldURI) are all rejected by this
backend, so if someone "simplifies" the payload later this test is what catches
it. See _build_flag_update() in email.py.

Sends one disposable, uniquely-tagged message to the mailbox's own address,
cycles it through Flagged -> Complete -> NotFlagged, then hard-deletes every
item carrying the tag.

Run standalone:
    python -m tests.smoke.tests.test_email_flag
"""

import asyncio
import os
import sys
import time

from tests.smoke.mcp_client import call, run, session
from tests.smoke.results import is_error_payload, record

# The mailbox's own address is resolved at runtime rather than written down here
# (see _resolve_self_email): this is a public repository and the address is
# personal data, so it should not be added to it as a literal.
SELF_EMAIL_ENV = "EXCHANGE_SMOKE_SELF_EMAIL"
TAG = f"[flag-smoke-{int(time.time())}]"
SUBJECT = f"{TAG} Exchange MCP set_email_flag smoke test"
BODY = "Automated smoke-test message from the exchange-mcp test suite. Safe to ignore/delete."

FIND_ATTEMPTS = 6
FIND_DELAY_SECONDS = 10


async def _find_item_id(s, folder: str, subject_substr: str):
    for attempt in range(FIND_ATTEMPTS):
        info = await call(s, "get_emails", folder=folder, limit=10, ids_only=True)
        if isinstance(info, dict):
            for item in info.get("item_ids", []):
                if subject_substr in item.get("subject", ""):
                    return item["item_id"]
        if attempt < FIND_ATTEMPTS - 1:
            await asyncio.sleep(FIND_DELAY_SECONDS)
    return None


async def _find_all_tagged(s, folders: list[str], subject_substr: str) -> list[str]:
    ids = []
    for folder in folders:
        info = await call(s, "get_emails", folder=folder, limit=15, ids_only=True)
        if isinstance(info, dict):
            for item in info.get("item_ids", []):
                if subject_substr in item.get("subject", ""):
                    ids.append(item["item_id"])
    return ids


async def _resolve_self_email(s):
    """This mailbox's own SMTP address, discovered rather than hardcoded.

    Prefers $EXCHANGE_SMOKE_SELF_EMAIL; otherwise reads it back off a message in
    Sent Items, whose `from` is by definition this mailbox. Keeping it out of the
    source means this test carries no personal data into a public repo and works
    against any mailbox without editing.
    """
    configured = os.environ.get(SELF_EMAIL_ENV, "").strip()
    if configured:
        return configured
    listing = await call(s, "get_emails", folder="Sent", limit=3, include_body=True)
    if isinstance(listing, dict):
        for row in listing.get("emails", []):
            address = (row.get("from") or "").strip()
            if "@" in address:
                return address
    return None


async def _read_flag(s, item_id: str):
    """Return (flag_status, error_note) as reported by get_email."""
    info = await call(s, "get_email", item_id=item_id)
    err = is_error_payload(info)
    if err:
        return None, err
    if not isinstance(info, dict):
        return None, f"unexpected get_email shape: {str(info)[:150]}"
    if "flag_status" not in info:
        return None, "get_email returned no 'flag_status' key -- the #102 read fix is not in effect"
    return info["flag_status"], None


async def main() -> bool:
    ok = True
    async with session() as s:
        # 1. Client-side validation must reject a bad value with no network call.
        bad = await call(s, "set_email_flag", item_ids=["irrelevant"], flag_status="bogus")
        if isinstance(bad, dict) and "Invalid flag_status" in str(bad.get("error", "")):
            record("set_email_flag", {"flag_status": "bogus"}, "OK",
                   "invalid flag_status rejected client-side")
        else:
            record("set_email_flag", {"flag_status": "bogus"}, "TOOL_ERROR",
                   f"expected an 'Invalid flag_status' error, got: {str(bad)[:200]}")
            ok = False

        # 2. Create a disposable message, addressed to this mailbox itself.
        self_email = await _resolve_self_email(s)
        if not self_email:
            record("send_email (self address)", {"env": SELF_EMAIL_ENV}, "TOOL_ERROR",
                   f"could not determine this mailbox's own address from Sent Items; "
                   f"set {SELF_EMAIL_ENV} to run this test")
            return False

        send_args = {"to": self_email, "subject": SUBJECT, "body": BODY}
        send_info = await call(s, "send_email", **send_args)
        err = is_error_payload(send_info)
        if err:
            record("send_email", send_args,
                   "EXCEPTION" if "_exception" in str(send_info) else "TOOL_ERROR", err)
            return False

        item_id = await _find_item_id(s, "Inbox", TAG)
        if not item_id:
            record("send_email (locate)", {"tag": TAG}, "EXCEPTION",
                   f"sent message not found in Inbox after {FIND_ATTEMPTS * FIND_DELAY_SECONDS}s")
            return False

        try:
            # 3. Baseline read: a fresh message should be unflagged.
            status, err = await _read_flag(s, item_id)
            if err:
                record("get_email", {"item_id": item_id, "field": "flag_status"}, "TOOL_ERROR", err)
                return False
            record("get_email", {"item_id": item_id, "field": "flag_status"}, "OK",
                   f"baseline flag_status={status!r}")
            if status != "NotFlagged":
                record("get_email", {"item_id": item_id}, "TOOL_ERROR",
                       f"expected a new message to be 'NotFlagged', got {status!r}")
                ok = False

            # 4. The round-trip that actually validates the write path.
            for target in ("Flagged", "Complete", "NotFlagged"):
                set_args = {"item_ids": [item_id], "flag_status": target}
                set_info = await call(s, "set_email_flag", **set_args)
                err = is_error_payload(set_info)
                if err:
                    record("set_email_flag", set_args,
                           "EXCEPTION" if "_exception" in str(set_info) else "TOOL_ERROR",
                           f"{err}  <-- the accepted encoding is FieldURI 'item:Flag' with "
                           f"__type 'FlagType:#Exchange'; see _build_flag_update()")
                    ok = False
                    continue

                await asyncio.sleep(2)  # let the write settle before reading back
                got, err = await _read_flag(s, item_id)
                if err:
                    record("set_email_flag", set_args, "TOOL_ERROR", f"read-back failed: {err}")
                    ok = False
                elif got != target:
                    record("set_email_flag", set_args, "TOOL_ERROR",
                           f"wrote {target!r} but get_email reports {got!r} -- the write was "
                           f"accepted but did not take effect (or the read can't see Flag)")
                    ok = False
                else:
                    record("set_email_flag", set_args, "OK", f"round-tripped {target!r}")

            # 5. get_emails must expose flag_status too (#101).
            # Deliberately a small page: this Inbox contains at least one message
            # whose GetItem makes OWA throw a SerializationException, and
            # include_body=True fails the WHOLE listing if any single row hits it
            # (pre-existing bug, see PROJECT_STATUS.md section 4). A wider page
            # would make this assertion fail for a reason unrelated to flags.
            list_args = {"folder": "Inbox", "limit": 3, "include_body": True}
            emails = await call(s, "get_emails", **list_args)
            err = is_error_payload(emails)
            if err and "SerializationException" in str(err):
                record("get_emails", list_args, "TOOL_ERROR",
                       f"hit the known pre-existing GetItem SerializationException "
                       f"(not a flag defect; see PROJECT_STATUS.md section 4): {err[:120]}")
            elif err or not isinstance(emails, dict):
                record("get_emails", list_args,
                       "EXCEPTION" if "_exception" in str(emails) else "TOOL_ERROR",
                       err or f"unexpected shape: {str(emails)[:150]}")
                ok = False
            else:
                rows = emails.get("emails", [])
                without = [r for r in rows if "flag_status" not in r]
                if not rows:
                    record("get_emails", list_args, "TOOL_ERROR", "no conversations returned")
                    ok = False
                elif without:
                    record("get_emails", list_args, "TOOL_ERROR",
                           f"{len(without)}/{len(rows)} conversation(s) lack a 'flag_status' key")
                    ok = False
                else:
                    seen = sorted({r["flag_status"] for r in rows})
                    record("get_emails", list_args, "OK",
                           f"flag_status present on all {len(rows)} conversation(s); values seen: {seen}")

            return ok
        finally:
            # 6. Sweep every item carrying this run's tag.
            all_deleted = []
            for _ in range(FIND_ATTEMPTS):
                await asyncio.sleep(FIND_DELAY_SECONDS)
                cleanup_ids = await _find_all_tagged(s, ["Inbox", "Sent", "Deleted"], TAG)
                if not cleanup_ids:
                    break
                del_info = await call(s, "delete_email", item_ids=cleanup_ids, permanent=True)
                if is_error_payload(del_info):
                    record("delete_email", {"item_ids": cleanup_ids}, "TOOL_ERROR",
                           is_error_payload(del_info))
                    break
                all_deleted.extend(cleanup_ids)
            record("delete_email", {"tag": TAG}, "OK" if all_deleted else "TOOL_ERROR",
                   f"{len(all_deleted)} item(s) hard-deleted"
                   if all_deleted else "nothing cleaned up -- may need manual deletion")


if __name__ == "__main__":
    sys.exit(0 if run(main()) else 1)
