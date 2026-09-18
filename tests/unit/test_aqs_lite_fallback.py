"""Pure-logic tests for `_parse_aqs_lite`/`_local_search_matches` — the reduced
AQS subset `search_emails`' client-side fallback checks when the server-side
`FindItem`/`QueryString` search is empty or fails. No mailbox, no browser, no
EXCHANGE_OWA_URL.

Why this deserves its own suite: `search_emails`'s docstring documents a wider
set of AQS keywords (`body:`, `to:`, `cc:`, `bcc:`, `participants:`, `size:`,
`importance:`, plus `received:`/`sent:`) than `_local_search_matches` actually
checks (`subject:`, `from:`, `category:`, `isread:`, `hasattachment:`, and -
since 2026-09-17 - `received:`/`sent:`). Before this suite existed,
`_parse_aqs_lite` treated any keyword outside its narrow whitelist as ordinary
free text — so `received:>2026-01-01` became a literal substring search for
the string `"received:>2026-01-01"` against subject/preview/sender, which can
never match anything. That is a *silent* zero: no error, no warning,
indistinguishable from "no emails matched" — reported live against a real
tenant, where `search_emails(query="received:>2026-01-01")` returned zero
results in the same folder `get_emails` showed same-day mail in. A first fix
moved `received:`/`sent:` into the *dropped* set (named in
`fallback_unsupported_filters` rather than silently misread as free text),
which stopped the false negative but still didn't filter by date at all — an
unfiltered scan reported as the fallback's answer. This suite's date tests
below pin the second fix: a real comparison against `DateTimeReceived`/
`DateTimeSent`. Three things are pinned here:

1. **A known-but-locally-unsupported keyword must never become a free-text
   term.** It has to be dropped (and named in the returned `dropped` list),
   never appended to `terms` — the whole point is that appending it there
   guarantees a false negative.
2. **An unrecognized `word:value` token that isn't a documented AQS keyword
   at all stays free text**, unchanged from before — e.g. a URL fragment like
   `http://example.com:8080` must not be misread as a filter attempt.
3. **`received:`/`sent:` are recognized filters that actually compare dates**,
   combine with other filters by AND (same as every other filter here), and
   degrade a single unparseable bound to "no match" rather than raising out
   of the whole search.

Run:
    python -m tests.unit.test_aqs_lite_fallback
"""

import sys
from datetime import datetime

from exchange_mcp.tools.email import (
    _AQS_KNOWN_UNSUPPORTED_KEYWORDS,
    _local_search_matches,
    _parse_aqs_lite,
    _parse_date_filter_value,
)

FAILURES: list[str] = []


def check(label: str, actual, expected) -> None:
    if actual != expected:
        FAILURES.append(f"{label}: expected {expected!r}, got {actual!r}")


def test_recognized_filter_is_not_dropped() -> None:
    terms, filters, dropped = _parse_aqs_lite("subject:budget from:alice")
    check("subject: -> filters, not dropped", filters.get("subject"), ["budget"])
    check("from: -> filters, not dropped", filters.get("from"), ["alice"])
    check("nothing dropped", dropped, [])
    check("no leftover free text", terms, [])


def test_known_unsupported_keyword_is_dropped_not_free_text() -> None:
    """The exact bug report shape (now for a keyword still unsupported):
    body:something must not survive as a free-text term."""
    terms, filters, dropped = _parse_aqs_lite("body:something")
    check("body: dropped", dropped, ["body"])
    check("body: not a free-text term", terms, [])
    check("body: not a filter (no local field for it)", filters, {})


def test_every_documented_unsupported_keyword_is_dropped() -> None:
    for keyword in sorted(_AQS_KNOWN_UNSUPPORTED_KEYWORDS):
        terms, filters, dropped = _parse_aqs_lite(f"{keyword}:something")
        check(f"{keyword}: dropped", dropped, [keyword])
        check(f"{keyword}: not a free-text term", terms, [])


def test_dropped_keyword_alongside_a_real_term_leaves_the_term_intact() -> None:
    """Dropping the unsupported filter must not swallow the rest of the query -
    a mixed query still searches on what the fallback *can* check."""
    terms, filters, dropped = _parse_aqs_lite("quarterly body:something subject:report")
    check("free word kept", terms, ["quarterly"])
    check("subject: kept as a filter", filters.get("subject"), ["report"])
    check("body: dropped, not merged into terms", dropped, ["body"])


def test_dropped_keyword_only_query_matches_everything_locally() -> None:
    """With the unsupported keyword dropped and nothing else in the query,
    _local_search_matches must fall back to "no filter" (match everything in
    the scanned page) rather than "match nothing" - an unfiltered scan with a
    warning beats a guaranteed, silent empty result."""
    terms, filters, dropped = _parse_aqs_lite("body:something")
    check("dropped, so this is what search_emails warns about", dropped, ["body"])
    item = {"Subject": "Q3 planning", "Preview": "", "IsRead": True, "HasAttachments": False}
    check("empty terms/filters match any item", _local_search_matches(item, terms, filters), True)


def test_unrecognized_word_colon_value_stays_free_text() -> None:
    """A colon that isn't one of the documented AQS keywords is not a filter
    attempt at all - e.g. a URL fragment - and must be searched as literal text,
    same as before this fix."""
    terms, filters, dropped = _parse_aqs_lite("see http://example.com:8080 report")
    check("no filters recognized", filters, {})
    check("nothing dropped", dropped, [])
    check("URL kept as free text", "http://example.com:8080" in terms, True)


def test_dropped_list_has_no_duplicates() -> None:
    terms, filters, dropped = _parse_aqs_lite("body:something body:other")
    check("body: listed once even if repeated", dropped, ["body"])


# ------------------------------------------------------------------
# received:/sent: are recognized filters with a real date comparison
# ------------------------------------------------------------------

def _item(received: str = "", sent: str = "", **extra) -> dict:
    row = {"Subject": "Q3 planning", "Preview": "", "IsRead": True, "HasAttachments": False}
    if received:
        row["DateTimeReceived"] = received
    if sent:
        row["DateTimeSent"] = sent
    row.update(extra)
    return row


def test_received_is_a_recognized_filter_not_dropped() -> None:
    """The exact bug report: received:>2026-01-01 must now be a real filter."""
    terms, filters, dropped = _parse_aqs_lite("received:>2026-01-01")
    check("received: is a filter", filters.get("received"), [">2026-01-01"])
    check("received: not dropped", dropped, [])
    check("received: not a free-text term", terms, [])


def test_sent_is_a_recognized_filter_not_dropped() -> None:
    terms, filters, dropped = _parse_aqs_lite("sent:<2026-06-01")
    check("sent: is a filter", filters.get("sent"), ["<2026-06-01"])
    check("sent: not dropped", dropped, [])


def test_received_after_excludes_earlier_item() -> None:
    terms, filters, _ = _parse_aqs_lite("received:>2026-08-18")
    early = _item(received="2026-08-01T09:00:00Z")
    late = _item(received="2026-08-19T09:00:00Z")
    check("earlier item excluded", _local_search_matches(early, terms, filters), False)
    check("later item included", _local_search_matches(late, terms, filters), True)


def test_received_after_includes_later_hours_of_the_bound_day() -> None:
    """The bound is midnight of that date, so ">" still includes the rest of
    that same day, not just the day after (see _item_date_matches)."""
    terms, filters, _ = _parse_aqs_lite("received:>2026-08-18")
    same_day_later = _item(received="2026-08-18T23:00:00Z")
    check("same-day-later included", _local_search_matches(same_day_later, terms, filters), True)


def test_received_before_excludes_later_item() -> None:
    terms, filters, _ = _parse_aqs_lite("received:<2026-08-18")
    before = _item(received="2026-08-01T09:00:00Z")
    after = _item(received="2026-08-19T09:00:00Z")
    check("earlier item included", _local_search_matches(before, terms, filters), True)
    check("later item excluded", _local_search_matches(after, terms, filters), False)


def test_bare_date_matches_that_calendar_day_only() -> None:
    terms, filters, _ = _parse_aqs_lite("received:2026-08-18")
    same_day = _item(received="2026-08-18T23:59:00Z")
    other_day = _item(received="2026-08-19T00:01:00Z")
    check("same day matches", _local_search_matches(same_day, terms, filters), True)
    check("other day excluded", _local_search_matches(other_day, terms, filters), False)


def test_sent_and_received_are_independent_fields() -> None:
    """A sent: filter must read DateTimeSent, not DateTimeReceived."""
    terms, filters, _ = _parse_aqs_lite("sent:>2026-08-18")
    item = _item(sent="2026-08-19T09:00:00Z", received="2026-01-01T09:00:00Z")
    check("sent: reads DateTimeSent, ignores the earlier DateTimeReceived",
          _local_search_matches(item, terms, filters), True)


def test_date_filter_combines_with_another_filter_by_and() -> None:
    """The brief's own combinability example: received:<DATE category:X."""
    terms, filters, _ = _parse_aqs_lite('received:<2026-08-18 category:"Prj-Foo"')
    matches_date_not_category = _item(received="2026-08-01T09:00:00Z", Categories=["Other"])
    matches_both = _item(received="2026-08-01T09:00:00Z", Categories=["Prj-Foo"])
    check("date ok, category not -> excluded",
          _local_search_matches(matches_date_not_category, terms, filters), False)
    check("both ok -> included",
          _local_search_matches(matches_both, terms, filters), True)


def test_two_date_bounds_form_a_range() -> None:
    """received:>START received:<END must AND together into a range, not
    have the second filters.setdefault() call silently overwrite the first
    (see _parse_aqs_lite appending to a list per keyword)."""
    terms, filters, _ = _parse_aqs_lite("received:>2026-08-01 received:<2026-08-31")
    inside = _item(received="2026-08-15T09:00:00Z")
    before_range = _item(received="2026-07-01T09:00:00Z")
    after_range = _item(received="2026-09-15T09:00:00Z")
    check("inside range included", _local_search_matches(inside, terms, filters), True)
    check("before range excluded", _local_search_matches(before_range, terms, filters), False)
    check("after range excluded", _local_search_matches(after_range, terms, filters), False)


def test_missing_date_field_degrades_to_no_match_not_a_crash() -> None:
    """An item with no DateTimeReceived at all (shape mismatch, not a bug in
    this filter) must be excluded, never raise out of the whole scan."""
    terms, filters, _ = _parse_aqs_lite("received:>2026-01-01")
    item = {"Subject": "no date field", "Preview": "", "IsRead": True, "HasAttachments": False}
    check("no date field -> excluded, not raised",
          _local_search_matches(item, terms, filters), False)


def test_malformed_date_bound_degrades_to_no_match_not_a_crash() -> None:
    """A bound that isn't a parseable date (typo, wrong format) must exclude
    rather than raise or silently match everything."""
    terms, filters, _ = _parse_aqs_lite("received:>not-a-date")
    item = _item(received="2026-08-18T09:00:00Z")
    check("unparseable bound -> excluded, not raised",
          _local_search_matches(item, terms, filters), False)


def test_parse_date_filter_value_recognizes_all_operators() -> None:
    check(">", _parse_date_filter_value(">2026-01-01"), (">", datetime(2026, 1, 1)))
    check("<", _parse_date_filter_value("<2026-01-01"), ("<", datetime(2026, 1, 1)))
    check(">=", _parse_date_filter_value(">=2026-01-01"), (">=", datetime(2026, 1, 1)))
    check("<=", _parse_date_filter_value("<=2026-01-01"), ("<=", datetime(2026, 1, 1)))
    check("bare", _parse_date_filter_value("2026-01-01"), ("=", datetime(2026, 1, 1)))
    check("malformed -> None", _parse_date_filter_value("not-a-date"), None)


def main() -> bool:
    for test in (
        test_recognized_filter_is_not_dropped,
        test_known_unsupported_keyword_is_dropped_not_free_text,
        test_every_documented_unsupported_keyword_is_dropped,
        test_dropped_keyword_alongside_a_real_term_leaves_the_term_intact,
        test_dropped_keyword_only_query_matches_everything_locally,
        test_unrecognized_word_colon_value_stays_free_text,
        test_dropped_list_has_no_duplicates,
        test_received_is_a_recognized_filter_not_dropped,
        test_sent_is_a_recognized_filter_not_dropped,
        test_received_after_excludes_earlier_item,
        test_received_after_includes_later_hours_of_the_bound_day,
        test_received_before_excludes_later_item,
        test_bare_date_matches_that_calendar_day_only,
        test_sent_and_received_are_independent_fields,
        test_date_filter_combines_with_another_filter_by_and,
        test_two_date_bounds_form_a_range,
        test_missing_date_field_degrades_to_no_match_not_a_crash,
        test_malformed_date_bound_degrades_to_no_match_not_a_crash,
        test_parse_date_filter_value_recognizes_all_operators,
    ):
        test()

    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for failure in FAILURES:
            print(f"  - {failure}")
        return False
    print("test_aqs_lite_fallback: all checks passed")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
