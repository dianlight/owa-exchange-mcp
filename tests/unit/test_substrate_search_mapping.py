"""Pure-logic tests for `_search_mailbox_substrate` — the Source-dict-to-
result-dict mapping `search_emails` uses when its search_all_folders=True
path is answered by Substrate Search (`OWAClient.search_conversations_substrate`)
instead of the per-folder FindItem/local-scan path. No mailbox, no browser,
no EXCHANGE_OWA_URL — `client` is a fake standing in for `OWAClient`.

Three things are pinned here because getting any of them wrong reads as a
quiet success, not an error:

1. **None vs. an empty list mean different things to the caller.** None means
   "the substrate backend didn't answer at all (classic OWA, or any other
   transport failure) — fall back to the old per-folder path", exactly like
   `_search_folder_aqs` returning `[]` signals its own caller to fall back.
   Collapsing that into `[]` would make search_emails silently skip its
   working fallback and report a false empty result on classic OWA.
2. **SessionExpiredError must not be swallowed into a fallback.** It means
   "the session actually expired", not "this backend doesn't exist here" —
   the same distinction `_search_folder_aqs` draws by re-raising it before
   its blanket `except Exception`.
3. **`ItemIds`/`GlobalItemIds` entries are `{"Id": ...}` dicts**, the same
   shape FindConversation's `_extract_conversation_summary` uses — confirmed
   live 2026-09-17 after an earlier draft assumed plain strings, which would
   have handed callers a dict where they expect an id string the moment
   `ItemId` was absent at the top level.

Run:
    python -m tests.unit.test_substrate_search_mapping
"""

import sys

from exchange_mcp.owa_client import BearerModeRequiredError, SessionExpiredError
from exchange_mcp.tools.email import _search_mailbox_substrate

FAILURES: list[str] = []


def check(label: str, actual, expected) -> None:
    if actual != expected:
        FAILURES.append(f"{label}: expected {expected!r}, got {actual!r}")


class _FakeClient:
    def __init__(self, sources=None, exc=None):
        self._sources = sources or []
        self._exc = exc

    def search_conversations_substrate(self, query, *, size=25):
        if self._exc is not None:
            raise self._exc
        return self._sources


_SOURCE = {
    "ConversationId": {"Id": "conv-1"},
    "ItemId": {"Id": "item-latest"},
    "ItemIds": [{"Id": "item-1"}, {"Id": "item-2"}, {"Id": "item-latest"}],
    "ParentFolderId": {"Id": "folder-1"},
    "ConversationTopic": "Budget review",
    "SenderSMTPAddress": "alice@example.com",
    "UniqueSenders": ["Alice Smith"],
    "LastDeliveryTime": "2026-09-17T10:00:00Z",
    "UnreadCount": 0,
    "MessageCount": 3,
    "HasAttachments": True,
    "Importance": "High",
    "Preview": "Here's the Q3 numbers...",
    "Categories": ["Finance"],
    "FlagStatus": "Flagged",
}


def test_bearer_mode_required_returns_none() -> None:
    client = _FakeClient(exc=BearerModeRequiredError("classic OWA"))
    check("None on BearerModeRequiredError", _search_mailbox_substrate(client, "q", 25), None)


def test_other_transport_failure_returns_none() -> None:
    client = _FakeClient(exc=RuntimeError("boom"))
    check("None on generic transport failure", _search_mailbox_substrate(client, "q", 25), None)


def test_session_expired_is_not_swallowed() -> None:
    client = _FakeClient(exc=SessionExpiredError("expired"))
    try:
        _search_mailbox_substrate(client, "q", 25)
        FAILURES.append("SessionExpiredError: expected it to propagate, but it didn't raise")
    except SessionExpiredError:
        pass


def test_empty_results_is_a_real_empty_list_not_none() -> None:
    client = _FakeClient(sources=[])
    check("empty results is [], not None", _search_mailbox_substrate(client, "q", 25), [])


def test_source_maps_to_conversation_level_result() -> None:
    client = _FakeClient(sources=[_SOURCE])
    results = _search_mailbox_substrate(client, "q", 25)
    check("one result", len(results), 1)
    r = results[0]
    check("conversation_id", r["conversation_id"], "conv-1")
    check("item_id prefers Source.ItemId over ItemIds[-1]", r["item_id"], "item-latest")
    check("item_ids normalized from {Id:...} dicts to plain strings", r["item_ids"], ["item-1", "item-2", "item-latest"])
    check("folder_id", r["folder_id"], "folder-1")
    check("subject", r["subject"], "Budget review")
    check("from", r["from"], "alice@example.com")
    check("senders", r["senders"], ["Alice Smith"])
    check("is_read derived from UnreadCount==0", r["is_read"], True)
    check("unread_count", r["unread_count"], 0)
    check("message_count", r["message_count"], 3)
    check("has_attachments", r["has_attachments"], True)
    check("importance", r["importance"], "High")
    check("preview", r["preview"], "Here's the Q3 numbers...")
    check("categories", r["categories"], ["Finance"])
    check("flag_status", r["flag_status"], "Flagged")


def test_item_id_falls_back_to_last_item_ids_entry_when_no_top_level_item_id() -> None:
    source = dict(_SOURCE)
    del source["ItemId"]
    client = _FakeClient(sources=[source])
    results = _search_mailbox_substrate(client, "q", 25)
    check("item_id falls back to ItemIds[-1]", results[0]["item_id"], "item-latest")


def test_missing_optional_fields_degrade_to_safe_defaults() -> None:
    client = _FakeClient(sources=[{}])
    results = _search_mailbox_substrate(client, "q", 25)
    r = results[0]
    check("subject defaults to (No subject)", r["subject"], "(No subject)")
    check("item_id defaults to empty string", r["item_id"], "")
    check("item_ids defaults to []", r["item_ids"], [])
    check("is_read defaults to True (UnreadCount 0)", r["is_read"], True)
    check("flag_status defaults to NotFlagged", r["flag_status"], "NotFlagged")
    check("importance defaults to Normal", r["importance"], "Normal")


def main() -> bool:
    for test in (
        test_bearer_mode_required_returns_none,
        test_other_transport_failure_returns_none,
        test_session_expired_is_not_swallowed,
        test_empty_results_is_a_real_empty_list_not_none,
        test_source_maps_to_conversation_level_result,
        test_item_id_falls_back_to_last_item_ids_entry_when_no_top_level_item_id,
        test_missing_optional_fields_degrade_to_safe_defaults,
    ):
        test()

    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for failure in FAILURES:
            print(f"  - {failure}")
        return False
    print("test_substrate_search_mapping: all checks passed")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
