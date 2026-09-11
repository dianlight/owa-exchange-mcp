"""Pure-logic tests for `_page_conversations` — no mailbox, no browser.

get_emails used to apply the caller's `offset` by slicing a *single*
FindConversation response whose server-side Offset was hardcoded to 0 and whose
window was `min(max(limit * 4, 50), 200)` rows. Every offset past that window
returned `{"emails": [], "count": 0}` — indistinguishable from the end of the
folder — and the cutoff moved with `limit` rather than with the mailbox, so the
same `offset` denoted different positions for different `limit`s. Verified on a
live Inbox on 2026-09-11: limit=20 worked at offset=79 and went empty at 80,
limit=5 worked at offset=45 and went empty at 50.

These cases pin the replacement's two guarantees, neither of which a live smoke
test can provoke on demand: any offset resolves to a real folder position, and
an empty page always says *why* it is empty.

Run:
    python -m tests.unit.test_conversation_paging
"""

import sys

from exchange_mcp.tools.email import (
    _CONV_MAX_SCAN,
    _PAGINATION_OFFSET_UNSUPPORTED,
    _PAGINATION_SCAN_LIMIT,
    _page_conversations,
)

FAILURES: list[str] = []


def check(label: str, actual, expected) -> None:
    if actual != expected:
        FAILURES.append(f"{label}: expected {expected!r}, got {actual!r}")


def conv(index: int, unread: int = 0) -> dict:
    """A FindConversation row, identified by its position in the folder."""
    return {
        "ConversationId": {"Id": f"conv-{index}"},
        "ConversationTopic": f"Subject {index}",
        "LastDeliveryTime": f"2026-01-01T00:{index:02d}:00Z",
        "UnreadCount": unread,
        "ItemIds": [{"Id": f"item-{index}"}],
    }


class FakeClient:
    """Serves a synthetic folder, with the backend quirks worth simulating.

    honour_offset=False models a backend that ignores IndexedPageView.Offset.
    max_per_call caps a response below MaxEntriesReturned, which this backend
    is already known to do (the reason a short page must not end the folder).
    honour_unread_filter=False models one that accepts ViewFilter: Unread and
    silently does nothing with it.
    """

    def __init__(self, folder, honour_offset=True, max_per_call=None,
                 honour_unread_filter=False):
        self.folder = folder
        self.honour_offset = honour_offset
        self.max_per_call = max_per_call
        self.honour_unread_filter = honour_unread_filter
        self.calls: list[tuple[int, int, str]] = []

    def request(self, action, payload):
        check("action", action, "FindConversation")
        body = payload["Body"]
        paging = body["Paging"]
        offset, count = paging["Offset"], paging["MaxEntriesReturned"]
        view_filter = body["ViewFilter"]
        self.calls.append((offset, count, view_filter))

        source = self.folder
        if view_filter == "Unread" and self.honour_unread_filter:
            source = [c for c in self.folder if c.get("UnreadCount", 0) > 0]

        start = offset if self.honour_offset else 0
        if self.max_per_call is not None:
            count = min(count, self.max_per_call)
        rows = source[start:start + count]
        return {"Body": {"Conversations": rows}}


def ids(page: list[dict]) -> list[str]:
    return [c["ConversationId"]["Id"] for c in page]


# ------------------------------------------------------------------
# The reported bug: deep offsets must reach real conversations
# ------------------------------------------------------------------

def test_deep_offset_returns_that_position():
    """offset=240 was the report's failing case; it must return rows 240-259."""
    client = FakeClient([conv(i) for i in range(300)])
    page, meta = _page_conversations(client, "inbox", offset=240, limit=20,
                                     unread_only=False)

    check("deep ids", ids(page), [f"conv-{i}" for i in range(240, 260)])
    check("deep count", meta["returned"], 20)
    check("deep has_more", meta["has_more"], True)
    check("deep next_offset", meta["next_offset"], 260)
    check("deep end flag", meta["reached_end_of_folder"], False)
    check("deep error", meta.get("error_code"), None)


def test_offset_is_a_position_not_a_window_index():
    """The old window made `offset` mean different things per `limit`."""
    folder = [conv(i) for i in range(300)]
    small, _ = _page_conversations(FakeClient(folder), "inbox", offset=100,
                                   limit=5, unread_only=False)
    large, _ = _page_conversations(FakeClient(folder), "inbox", offset=100,
                                   limit=50, unread_only=False)
    check("limit-independent start", ids(small)[0], "conv-100")
    check("limit-independent start (large)", ids(large)[0], "conv-100")


def test_offset_zero_costs_one_request():
    """The common case must not pay for the deep-offset anchor probe."""
    client = FakeClient([conv(i) for i in range(300)])
    _page_conversations(client, "inbox", offset=0, limit=10, unread_only=False)
    check("offset-0 request count", len(client.calls), 1)


def test_limit_above_one_page_is_assembled_from_several_calls():
    """ids_only allows limit=500; a 200-row window can't serve that alone."""
    client = FakeClient([conv(i) for i in range(600)])
    page, meta = _page_conversations(client, "inbox", offset=0, limit=500,
                                     unread_only=False)
    check("500 rows", len(page), 500)
    check("500 rows distinct", len(set(ids(page))), 500)
    check("500 rows in order", ids(page)[499], "conv-499")
    check("500 has_more", meta["has_more"], True)


# ------------------------------------------------------------------
# An empty page must always say why
# ------------------------------------------------------------------

def test_past_end_of_folder_is_reported_as_the_end():
    client = FakeClient([conv(i) for i in range(50)])
    page, meta = _page_conversations(client, "inbox", offset=400, limit=20,
                                     unread_only=False)
    check("past-end page", page, [])
    check("past-end flag", meta["reached_end_of_folder"], True)
    check("past-end has_more", meta["has_more"], False)
    check("past-end next_offset", meta["next_offset"], None)
    check("past-end error", meta.get("error_code"), None)


def test_last_partial_page_is_the_end():
    client = FakeClient([conv(i) for i in range(300)])
    page, meta = _page_conversations(client, "inbox", offset=290, limit=20,
                                     unread_only=False)
    check("tail ids", ids(page), [f"conv-{i}" for i in range(290, 300)])
    check("tail has_more", meta["has_more"], False)
    check("tail end flag", meta["reached_end_of_folder"], True)
    check("tail next_offset", meta["next_offset"], None)


def test_empty_folder_is_the_end_not_an_error():
    client = FakeClient([])
    page, meta = _page_conversations(client, "inbox", offset=0, limit=10,
                                     unread_only=False)
    check("empty page", page, [])
    check("empty end flag", meta["reached_end_of_folder"], True)
    check("empty error", meta.get("error_code"), None)


def test_short_page_does_not_end_the_folder():
    """MaxEntriesReturned is unreliable here, so only an *empty* page ends it.

    Reading a short page as the end is how the original bug looked from the
    outside; a backend that caps every response at 50 rows must still be able
    to serve a 100-row request and a deep offset.
    """
    client = FakeClient([conv(i) for i in range(300)], max_per_call=50)
    page, meta = _page_conversations(client, "inbox", offset=0, limit=100,
                                     unread_only=False)
    check("short-page rows", len(page), 100)
    check("short-page distinct", len(set(ids(page))), 100)
    check("short-page has_more", meta["has_more"], True)
    check("short-page not ended", meta["reached_end_of_folder"], False)

    deep, deep_meta = _page_conversations(client, "inbox", offset=240, limit=20,
                                          unread_only=False)
    check("short-page deep ids", ids(deep), [f"conv-{i}" for i in range(240, 260)])
    check("short-page deep error", deep_meta.get("error_code"), None)


def test_server_ignoring_offset_is_reported_not_silently_wrong():
    """The one failure mode worse than an empty page: page 1 labelled page 13.

    A single response cannot reveal it, which is why a deep page identifies
    row 0 first and compares.
    """
    client = FakeClient([conv(i) for i in range(300)], honour_offset=False)
    page, meta = _page_conversations(client, "inbox", offset=240, limit=20,
                                     unread_only=False)
    check("ignored-offset page", page, [])
    check("ignored-offset code", meta.get("error_code"),
          _PAGINATION_OFFSET_UNSUPPORTED)
    check("ignored-offset not ended", meta["reached_end_of_folder"], False)
    check("ignored-offset has_more", meta["has_more"], True)
    check("ignored-offset has remediation", bool(meta.get("error")), True)


def test_scan_limit_is_reported_not_silently_truncated():
    """unread_only filters client-side, so a deep offset can outrun the cap."""
    folder = [conv(i, unread=1 if i % 500 == 0 else 0)
              for i in range(_CONV_MAX_SCAN + 500)]
    client = FakeClient(folder)
    page, meta = _page_conversations(client, "inbox", offset=20, limit=10,
                                     unread_only=True)
    check("scan-cap page", page, [])
    check("scan-cap code", meta.get("error_code"), _PAGINATION_SCAN_LIMIT)
    check("scan-cap not ended", meta["reached_end_of_folder"], False)
    check("scan-cap has_more", meta["has_more"], True)
    check("scan-cap bounded", meta["conversations_scanned"] <= _CONV_MAX_SCAN, True)


# ------------------------------------------------------------------
# unread_only: offset applies to the filtered sequence
# ------------------------------------------------------------------

def test_unread_offset_counts_filtered_rows():
    """offset=5 must mean the 6th *unread* conversation, not folder row 5."""
    folder = [conv(i, unread=1 if i % 2 == 0 else 0) for i in range(300)]
    client = FakeClient(folder)
    page, meta = _page_conversations(client, "inbox", offset=5, limit=3,
                                     unread_only=True)
    check("unread ids", ids(page), ["conv-10", "conv-12", "conv-14"])
    check("unread has_more", meta["has_more"], True)
    check("unread next_offset", meta["next_offset"], 8)


def test_unread_offset_starts_scan_at_zero():
    """A filtered page has no server-side address, so it must not send one."""
    folder = [conv(i, unread=1) for i in range(300)]
    client = FakeClient(folder)
    _page_conversations(client, "inbox", offset=100, limit=10, unread_only=True)
    check("filtered first offset", client.calls[0][0], 0)


def test_paging_never_asks_the_server_to_filter():
    """ViewFilter: "Unread" was tried and rejected - see _page_conversations.

    An ignored filter would leave `offset` addressing the unfiltered sequence,
    so every returned row would be genuinely unread but from the wrong
    position, and it cannot be probed for reliably. Pinning "All" here keeps
    that from being reintroduced as an apparently free optimisation.
    """
    folder = [conv(i, unread=1 if i % 2 == 0 else 0) for i in range(300)]
    client = FakeClient(folder, honour_unread_filter=True)
    _page_conversations(client, "inbox", offset=5, limit=3, unread_only=True)
    check("view filters sent", sorted({c[2] for c in client.calls}), ["All"])


def test_unread_end_of_folder():
    folder = [conv(i, unread=1 if i < 4 else 0) for i in range(100)]
    client = FakeClient(folder)
    page, meta = _page_conversations(client, "inbox", offset=0, limit=10,
                                     unread_only=True)
    check("few unread", ids(page), ["conv-0", "conv-1", "conv-2", "conv-3"])
    check("few unread has_more", meta["has_more"], False)
    check("few unread ended", meta["reached_end_of_folder"], True)


TESTS = [
    test_deep_offset_returns_that_position,
    test_offset_is_a_position_not_a_window_index,
    test_offset_zero_costs_one_request,
    test_limit_above_one_page_is_assembled_from_several_calls,
    test_past_end_of_folder_is_reported_as_the_end,
    test_last_partial_page_is_the_end,
    test_empty_folder_is_the_end_not_an_error,
    test_short_page_does_not_end_the_folder,
    test_server_ignoring_offset_is_reported_not_silently_wrong,
    test_scan_limit_is_reported_not_silently_truncated,
    test_unread_offset_counts_filtered_rows,
    test_unread_offset_starts_scan_at_zero,
    test_paging_never_asks_the_server_to_filter,
    test_unread_end_of_folder,
]


def main() -> bool:
    for test in TESTS:
        try:
            test()
        except Exception as e:  # noqa: BLE001 - report, don't abort the suite
            FAILURES.append(f"{test.__name__}: raised {type(e).__name__}: {e}")

    if FAILURES:
        print(f"FAIL ({len(FAILURES)} problem(s)):")
        for f in FAILURES:
            print(f"  - {f}")
        return False

    print(f"OK - {len(TESTS)} conversation-paging tests passed")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
