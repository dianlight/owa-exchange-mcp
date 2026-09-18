"""Pure-logic tests for the FindConversation paging helpers — no mailbox, no
browser. Covers `_page_conversations` (get_emails, #101) and
`_page_conversations_by_category` (find_emails_by_category, #113).

get_emails used to apply the caller's `offset` by slicing a *single*
FindConversation response whose server-side Offset was hardcoded to 0 and whose
window was `min(max(limit * 4, 50), 200)` rows. Every offset past that window
returned `{"emails": [], "count": 0}` — indistinguishable from the end of the
folder — and the cutoff moved with `limit` rather than with the mailbox, so the
same `offset` denoted different positions for different `limit`s. Verified on a
live Inbox on 2026-09-11: limit=20 worked at offset=79 and went empty at 80,
limit=5 worked at offset=45 and went empty at 50.

find_emails_by_category had the same shape with the offset part removed: one
FindConversation at Offset 0, MaxEntriesReturned 200, filtered client-side, so
a category applied to anything older than the newest 200 conversations could
never match and nothing said so (issue #5).

These cases pin the replacement's three guarantees, none of which a live smoke
test can provoke on demand: any offset resolves to a real folder position, an
empty page always says *why* it is empty, and a server that ignores `Offset`
outright is recovered via a scan-from-zero-skip-client-side fallback rather
than leaving the caller's data permanently unreachable (fixed 2026-09-17,
after that fallback itself was found still bounded by _CONV_MAX_SCAN rather
than silently answering wrong past the cap).

Run:
    python -m tests.unit.test_conversation_paging
"""

import sys

from exchange_mcp.tools.email import (
    _CONV_MAX_SCAN,
    _PAGINATION_OFFSET_UNSUPPORTED,
    _PAGINATION_SCAN_LIMIT,
    _page_conversations,
    _page_conversations_by_category,
)

FAILURES: list[str] = []


def check(label: str, actual, expected) -> None:
    if actual != expected:
        FAILURES.append(f"{label}: expected {expected!r}, got {actual!r}")


def conv(index: int, unread: int = 0, categories=None) -> dict:
    """A FindConversation row, identified by its position in the folder."""
    row = {
        "ConversationId": {"Id": f"conv-{index}"},
        "ConversationTopic": f"Subject {index}",
        "LastDeliveryTime": f"2026-01-01T00:{index:02d}:00Z",
        "UnreadCount": unread,
        "ItemIds": [{"Id": f"item-{index}"}],
    }
    if categories is not None:
        row["Categories"] = categories
    return row


class FakeClient:
    """Serves a synthetic folder, with the backend quirks worth simulating.

    honour_offset=False models a backend that ignores IndexedPageView.Offset
    unconditionally, on every request - the worst case, where no client-side
    technique can reach anything past its first response.
    offset_cap models a milder, more common quirk: the server serves any
    Offset up to its own cached window correctly, but a single request that
    jumps past that window gets silently reset to position 0. That is what
    lets a from-zero scan (which only ever asks for the *next* contiguous
    slice) reach data a direct deep jump cannot.
    max_per_call caps a response below MaxEntriesReturned, which this backend
    is already known to do (the reason a short page must not end the folder).
    honour_unread_filter=False models one that accepts ViewFilter: Unread and
    silently does nothing with it.
    """

    def __init__(self, folder, honour_offset=True, max_per_call=None,
                 honour_unread_filter=False, offset_cap=None):
        self.folder = folder
        self.honour_offset = honour_offset
        self.max_per_call = max_per_call
        self.honour_unread_filter = honour_unread_filter
        self.offset_cap = offset_cap
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

        if self.offset_cap is not None:
            start = 0 if offset > self.offset_cap else offset
        else:
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


def test_server_ignoring_offset_recovers_via_scan_and_skip_fallback():
    """A milder, more common quirk than total Offset-blindness: the server
    serves any Offset within its own cached window correctly, but a single
    request that jumps past that window resets to position 0. A direct
    request for offset=240 fails against a 200-row window - but the same
    from-zero-scan-and-skip technique unread_only already uses only ever asks
    for the *next* contiguous slice, so it never takes that jump and recovers
    the real page instead of leaving it permanently unreachable. Fixed
    2026-09-17.
    """
    client = FakeClient([conv(i) for i in range(300)], offset_cap=200)
    page, meta = _page_conversations(client, "inbox", offset=240, limit=20,
                                     unread_only=False)
    check("recovered ids", ids(page), [f"conv-{i}" for i in range(240, 260)])
    check("recovered error", meta.get("error_code"), None)
    check("recovered not ended", meta["reached_end_of_folder"], False)
    check("recovered has_more", meta["has_more"], True)
    check("recovered next_offset", meta["next_offset"], 260)


def test_offset_unsupported_fallback_still_bounded_by_scan_limit():
    """The fallback is a real scan, not a magic escape hatch: if even a
    from-zero scan can't reach the requested depth within _CONV_MAX_SCAN,
    that must still be named rather than silently answered wrong."""
    folder = [conv(i) for i in range(_CONV_MAX_SCAN + 500)]
    client = FakeClient(folder, offset_cap=_CONV_MAX_SCAN)
    page, meta = _page_conversations(client, "inbox",
                                     offset=_CONV_MAX_SCAN + 100, limit=20,
                                     unread_only=False)
    check("still-unreachable page", page, [])
    check("still-unreachable code", meta.get("error_code"),
          _PAGINATION_SCAN_LIMIT)
    check("still-unreachable not ended", meta["reached_end_of_folder"], False)
    check("still-unreachable has_more", meta["has_more"], True)


def test_totally_offset_blind_server_still_reported_not_silently_wrong():
    """The one failure mode worse than an empty page: page 1 labelled page 13.

    A backend that ignores Offset on *every* request, including the
    fallback's own from-zero scan, leaves no client-side technique able to
    reach anything past its first response - that must stay reported, not
    guessed at, and the retry must not turn into an infinite loop or a wrong
    answer.
    """
    client = FakeClient([conv(i) for i in range(300)], honour_offset=False)
    page, meta = _page_conversations(client, "inbox", offset=240, limit=20,
                                     unread_only=False)
    check("still-blind page", page, [])
    check("still-blind code", meta.get("error_code"),
          _PAGINATION_OFFSET_UNSUPPORTED)
    check("still-blind not ended", meta["reached_end_of_folder"], False)
    check("still-blind has_more", meta["has_more"], True)
    check("still-blind has remediation", bool(meta.get("error")), True)


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


# ------------------------------------------------------------------
# find_emails_by_category: the category filter is client-side too
# ------------------------------------------------------------------

CAT = "Progetto/Alpha"


def tagged_folder(size: int, tagged_at, category: str = CAT) -> list[dict]:
    """A folder where only the positions in `tagged_at` carry `category`."""
    tagged = set(tagged_at)
    return [
        conv(i, categories=[category] if i in tagged else None)
        for i in range(size)
    ]


def test_category_beyond_the_first_window_is_found():
    """The reported bug: a tag on conversation 250 was unreachable at all."""
    client = FakeClient(tagged_folder(300, [250, 251]))
    page, meta = _page_conversations_by_category(client, "inbox", CAT,
                                                 offset=0, limit=10)
    check("deep tag ids", ids(page), ["conv-250", "conv-251"])
    check("deep tag count", meta["returned"], 2)
    check("deep tag has_more", meta["has_more"], False)
    check("deep tag ended", meta["reached_end_of_folder"], True)
    check("deep tag error", meta.get("error_code"), None)
    check("deep tag scanned past 200", meta["conversations_scanned"] > 200, True)


def test_category_absent_is_the_end_not_an_error():
    """Zero matches after a full sweep is a real answer, not a failure."""
    client = FakeClient(tagged_folder(300, []))
    page, meta = _page_conversations_by_category(client, "inbox", CAT,
                                                 offset=0, limit=10)
    check("no-match page", page, [])
    check("no-match ended", meta["reached_end_of_folder"], True)
    check("no-match has_more", meta["has_more"], False)
    check("no-match error", meta.get("error_code"), None)


def test_category_match_is_case_insensitive():
    folder = tagged_folder(250, [240], category="progetto/ALPHA")
    page, _ = _page_conversations_by_category(FakeClient(folder), "inbox",
                                              "Progetto/alpha",
                                              offset=0, limit=10)
    check("case-insensitive ids", ids(page), ["conv-240"])


def test_category_stops_as_soon_as_the_page_is_full():
    """A common tag must not cost a full-folder sweep."""
    client = FakeClient(tagged_folder(2000, range(0, 2000)))
    page, meta = _page_conversations_by_category(client, "inbox", CAT,
                                                 offset=0, limit=10)
    check("full page", len(page), 10)
    check("full page has_more", meta["has_more"], True)
    check("full page next_offset", meta["next_offset"], 10)
    check("full page one request", len(client.calls), 1)


def test_category_offset_counts_matches_not_folder_rows():
    """offset=2 means the 3rd *matching* conversation, wherever it sits."""
    client = FakeClient(tagged_folder(400, [10, 120, 230, 340, 350]))
    page, meta = _page_conversations_by_category(client, "inbox", CAT,
                                                 offset=2, limit=2)
    check("offset ids", ids(page), ["conv-230", "conv-340"])
    check("offset has_more", meta["has_more"], True)
    check("offset next_offset", meta["next_offset"], 4)
    check("offset echoed", meta["offset"], 2)

    tail, tail_meta = _page_conversations_by_category(client, "inbox", CAT,
                                                      offset=4, limit=2)
    check("offset tail ids", ids(tail), ["conv-350"])
    check("offset tail has_more", tail_meta["has_more"], False)
    check("offset tail ended", tail_meta["reached_end_of_folder"], True)


def test_category_short_page_does_not_end_the_folder():
    """MaxEntriesReturned is unreliable here, so only an *empty* page ends it.

    A backend that caps every response at 50 rows must still find a tag at
    conversation 250 - reading the first short page as the end of the folder is
    precisely the silent truncation this replaces.
    """
    client = FakeClient(tagged_folder(300, [250]), max_per_call=50)
    page, meta = _page_conversations_by_category(client, "inbox", CAT,
                                                 offset=0, limit=10)
    check("short-page tag ids", ids(page), ["conv-250"])
    check("short-page tag error", meta.get("error_code"), None)
    check("short-page tag ended", meta["reached_end_of_folder"], True)


def test_category_scan_limit_is_reported_not_silently_truncated():
    """Stopping at the cap must never read as "no more matches"."""
    folder = tagged_folder(_CONV_MAX_SCAN + 500, [_CONV_MAX_SCAN + 100])
    client = FakeClient(folder)
    page, meta = _page_conversations_by_category(client, "inbox", CAT,
                                                 offset=0, limit=10)
    check("scan-cap tag page", page, [])
    check("scan-cap tag code", meta.get("error_code"), _PAGINATION_SCAN_LIMIT)
    check("scan-cap tag not ended", meta["reached_end_of_folder"], False)
    check("scan-cap tag has_more", meta["has_more"], True)
    check("scan-cap tag next_offset", meta["next_offset"], None)
    check("scan-cap tag remediation", bool(meta.get("error")), True)
    check("scan-cap tag bounded",
          meta["conversations_scanned"] <= _CONV_MAX_SCAN, True)


def test_category_server_ignoring_offset_is_reported_not_a_spin():
    """A backend that re-serves page 1 must be named, not scanned into the cap.

    Without the repeat check this loop would re-filter the same 200 rows until
    the scan cap and blame the cap - a different, misleading diagnosis.
    """
    client = FakeClient(tagged_folder(300, [250]), honour_offset=False)
    page, meta = _page_conversations_by_category(client, "inbox", CAT,
                                                 offset=0, limit=10)
    check("ignored-offset tag page", page, [])
    check("ignored-offset tag code", meta.get("error_code"),
          _PAGINATION_OFFSET_UNSUPPORTED)
    check("ignored-offset tag not ended", meta["reached_end_of_folder"], False)
    check("ignored-offset tag has_more", meta["has_more"], True)


def test_category_diagnostics_stay_nested():
    """A top-level `error` means the whole tool call failed - keep it inside.

    tests/smoke/results.py's is_error_payload reads a top-level `error` that
    way, so hoisting these would make a partial success look like a failure.
    """
    folder = tagged_folder(_CONV_MAX_SCAN + 500, [_CONV_MAX_SCAN + 100])
    page, meta = _page_conversations_by_category(FakeClient(folder), "inbox",
                                                 CAT, offset=0, limit=10)
    check("nested returns a page", page, [])
    check("nested error keys", sorted(k for k in meta if "error" in k),
          ["error", "error_code"])


TESTS = [
    test_deep_offset_returns_that_position,
    test_offset_is_a_position_not_a_window_index,
    test_offset_zero_costs_one_request,
    test_limit_above_one_page_is_assembled_from_several_calls,
    test_past_end_of_folder_is_reported_as_the_end,
    test_last_partial_page_is_the_end,
    test_empty_folder_is_the_end_not_an_error,
    test_short_page_does_not_end_the_folder,
    test_server_ignoring_offset_recovers_via_scan_and_skip_fallback,
    test_offset_unsupported_fallback_still_bounded_by_scan_limit,
    test_totally_offset_blind_server_still_reported_not_silently_wrong,
    test_scan_limit_is_reported_not_silently_truncated,
    test_unread_offset_counts_filtered_rows,
    test_unread_offset_starts_scan_at_zero,
    test_paging_never_asks_the_server_to_filter,
    test_unread_end_of_folder,
    test_category_beyond_the_first_window_is_found,
    test_category_absent_is_the_end_not_an_error,
    test_category_match_is_case_insensitive,
    test_category_stops_as_soon_as_the_page_is_full,
    test_category_offset_counts_matches_not_folder_rows,
    test_category_short_page_does_not_end_the_folder,
    test_category_scan_limit_is_reported_not_silently_truncated,
    test_category_server_ignoring_offset_is_reported_not_a_spin,
    test_category_diagnostics_stay_nested,
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
