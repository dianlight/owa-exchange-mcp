"""Pure-logic tests for `_triage_emails_apply` in exchange_mcp/tools/email.py
(issue #44, feature request 7, 2026-09-18) - the per-item core `triage_emails`
delegates to once its own argument validation, folder resolution and
`_get_client(ctx)` are done. No mailbox, no browser, no MCP Context: only
`OWAClient.request`/`extract_items` are exercised, via a fake subclass that
scripts GetItem/UpdateItem/MoveItem responses per item id.

What this pins:
1. mark_read + flag_status + categories combine into exactly one UpdateItem
   request per item (not three), each carrying all three Updates entries.
2. add_categories/remove_categories merge against the item's *existing*
   categories (read via GetItem), preserving order and de-duplicating.
3. Per-item failure isolation: an UpdateItem-level error for one item lands in
   `failed` with a stable error_code and does not stop the remaining items
   from being processed, matching assign_email_categories's convention.
4. A move is only attempted when a folder was requested, is a separate
   MoveItem request, and is skipped for an item whose own field update already
   failed (reported once, not twice).
5. When no field update is requested at all (only move_to_folder), no
   UpdateItem request is made.

Run:
    python -m tests.unit.test_triage_emails
"""

import sys

from exchange_mcp.owa_client import OWAClient, SessionExpiredError
from exchange_mcp.tools.email import _triage_emails_apply

FAILURES: list[str] = []


def check(label: str, actual, expected) -> None:
    if actual != expected:
        FAILURES.append(f"{label}: expected {expected!r}, got {actual!r}")


class FakeBrowser:
    owa_url = "https://owa.example.com"
    profile_dir = "/tmp/profile"
    auth_mode = "bearer"

    def identity_hints(self) -> dict:
        return {}


def _success_message() -> dict:
    return {"Body": {"ResponseMessages": {"Items": [{"ResponseClass": "Success"}]}}}


def _error_message(text: str) -> dict:
    return {
        "Body": {
            "ResponseMessages": {
                "Items": [{"ResponseClass": "Error", "MessageText": text}]
            }
        }
    }


def _get_item_response(item_id: str, *, categories=None, change_key=None) -> dict:
    item = {"ItemId": {"Id": item_id, "ChangeKey": change_key or f"ck-{item_id}"}}
    if categories is not None:
        item["Categories"] = categories
    return {"Body": {"ResponseMessages": {"Items": [{"Items": [item]}]}}}


class FakeClient(OWAClient):
    """Scripts GetItem/UpdateItem/MoveItem per item id.

    `existing_categories` seeds what _get_item_categories reads back before a
    categories merge; `update_errors`/`move_errors` make a named item's write
    fail with the given message instead of succeeding.
    """

    def __init__(self, *, existing_categories=None, update_errors=None, move_errors=None):
        super().__init__(FakeBrowser())
        self.existing_categories = existing_categories or {}
        self.update_errors = update_errors or {}
        self.move_errors = move_errors or {}
        self.update_calls: list[tuple[str, list]] = []
        self.move_calls: list[tuple[str, str]] = []

    def request(self, action, payload, *, timeout=30):
        body = payload["Body"]
        if action == "GetItem":
            item_id = body["ItemIds"][0]["Id"]
            shape = body["ItemShape"]
            if "AdditionalProperties" in shape:
                return _get_item_response(
                    item_id, categories=self.existing_categories.get(item_id, [])
                )
            return _get_item_response(item_id)
        if action == "UpdateItem":
            change = body["ItemChanges"][0]
            item_id = change["ItemId"]["Id"]
            self.update_calls.append((item_id, change["Updates"]))
            if item_id in self.update_errors:
                return _error_message(self.update_errors[item_id])
            return _success_message()
        if action == "MoveItem":
            item_id = body["ItemIds"][0]["Id"]
            folder_id = body["ToFolderId"]["BaseFolderId"]
            self.move_calls.append((item_id, folder_id))
            if item_id in self.move_errors:
                return _error_message(self.move_errors[item_id])
            return _success_message()
        raise AssertionError(f"unexpected action in fake client: {action}")


def _updates_by_path(updates: list[dict]) -> dict[str, dict]:
    return {u["Path"]["FieldURI"]: u for u in updates}


def test_combines_mark_read_flag_and_categories_into_one_update() -> None:
    client = FakeClient(existing_categories={"id-1": ["Existing"]})
    succeeded, failed = _triage_emails_apply(
        client,
        ["id-1"],
        mark_read=True,
        add_categories=["Urgent"],
        remove_categories=None,
        flag_status="Flagged",
        resolved_folder_id=None,
    )
    check("one item succeeded", succeeded, ["id-1"])
    check("no failures", failed, [])
    check("exactly one UpdateItem request", len(client.update_calls), 1)
    check("no MoveItem request made", len(client.move_calls), 0)

    _, updates = client.update_calls[0]
    check("three fields updated in one request", len(updates), 3)
    by_path = _updates_by_path(updates)
    check("IsRead set", by_path["IsRead"]["Item"]["IsRead"], True)
    check(
        "Categories merged with existing",
        by_path["Categories"]["Item"]["Categories"],
        ["Existing", "Urgent"],
    )


def test_remove_categories_drops_from_existing() -> None:
    client = FakeClient(existing_categories={"id-1": ["Keep", "Drop"]})
    succeeded, failed = _triage_emails_apply(
        client,
        ["id-1"],
        mark_read=None,
        add_categories=None,
        remove_categories=["Drop"],
        flag_status=None,
        resolved_folder_id=None,
    )
    check("succeeded", succeeded, ["id-1"])
    check("no failures", failed, [])
    _, updates = client.update_calls[0]
    check("only Drop removed", updates[0]["Item"]["Categories"], ["Keep"])


def test_failed_item_does_not_stop_remaining_items() -> None:
    client = FakeClient(update_errors={"id-bad": "SomeServerFault"})
    succeeded, failed = _triage_emails_apply(
        client,
        ["id-bad", "id-good"],
        mark_read=True,
        add_categories=None,
        remove_categories=None,
        flag_status=None,
        resolved_folder_id=None,
    )
    check("only id-good succeeded", succeeded, ["id-good"])
    check("one failure recorded", len(failed), 1)
    check("failure is for id-bad", failed[0]["item_id"], "id-bad")
    check("failure carries a stable error_code", "error_code" in failed[0], True)
    check("both items were attempted", len(client.update_calls), 2)


def test_move_is_separate_request_and_skipped_after_a_failed_update() -> None:
    client = FakeClient(update_errors={"id-bad": "SomeServerFault"})
    succeeded, failed = _triage_emails_apply(
        client,
        ["id-bad", "id-good"],
        mark_read=True,
        add_categories=None,
        remove_categories=None,
        flag_status=None,
        resolved_folder_id="folder-abc",
    )
    check("only id-good succeeded", succeeded, ["id-good"])
    check("id-bad failed once, not reported twice", len(failed), 1)
    check(
        "only the successful item was moved",
        client.move_calls,
        [("id-good", OWAClient.folder_id_dict("folder-abc"))],
    )


def test_move_only_no_field_update_requested() -> None:
    client = FakeClient()
    succeeded, failed = _triage_emails_apply(
        client,
        ["id-1"],
        mark_read=None,
        add_categories=None,
        remove_categories=None,
        flag_status=None,
        resolved_folder_id="folder-abc",
    )
    check("succeeded", succeeded, ["id-1"])
    check("no failures", failed, [])
    check("no UpdateItem request made", len(client.update_calls), 0)
    check(
        "one MoveItem request made",
        client.move_calls,
        [("id-1", OWAClient.folder_id_dict("folder-abc"))],
    )


def test_move_failure_is_reported_per_item() -> None:
    client = FakeClient(move_errors={"id-1": "MoveFault"})
    succeeded, failed = _triage_emails_apply(
        client,
        ["id-1"],
        mark_read=True,
        add_categories=None,
        remove_categories=None,
        flag_status=None,
        resolved_folder_id="folder-abc",
    )
    check("no successes", succeeded, [])
    check("one failure recorded", len(failed), 1)
    check("failure is for id-1", failed[0]["item_id"], "id-1")
    check("the update itself was still attempted", len(client.update_calls), 1)


def test_session_expired_propagates_immediately() -> None:
    class ExpiringClient(FakeClient):
        def request(self, action, payload, *, timeout=30):
            if action == "UpdateItem":
                raise SessionExpiredError("expired")
            return super().request(action, payload, timeout=timeout)

    client = ExpiringClient()
    try:
        _triage_emails_apply(
            client,
            ["id-1", "id-2"],
            mark_read=True,
            add_categories=None,
            remove_categories=None,
            flag_status=None,
            resolved_folder_id=None,
        )
        FAILURES.append("SessionExpiredError: expected it to propagate, but it didn't raise")
    except SessionExpiredError:
        pass


def main() -> bool:
    for test in (
        test_combines_mark_read_flag_and_categories_into_one_update,
        test_remove_categories_drops_from_existing,
        test_failed_item_does_not_stop_remaining_items,
        test_move_is_separate_request_and_skipped_after_a_failed_update,
        test_move_only_no_field_update_requested,
        test_move_failure_is_reported_per_item,
        test_session_expired_propagates_immediately,
    ):
        test()

    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for failure in FAILURES:
            print(f"  - {failure}")
        return False
    print("test_triage_emails: all checks passed")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
