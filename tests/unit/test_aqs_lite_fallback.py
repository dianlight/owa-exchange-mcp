"""Pure-logic tests for `_parse_aqs_lite`/`_local_search_matches` — the reduced
AQS subset `search_emails`' client-side fallback checks when the server-side
`FindItem`/`QueryString` search is empty or fails. No mailbox, no browser, no
EXCHANGE_OWA_URL.

Why this deserves its own suite: `search_emails`'s docstring documents a wider
set of AQS keywords (`body:`, `to:`, `cc:`, `bcc:`, `participants:`, `sent:`,
`received:`, `size:`, `importance:`) than `_local_search_matches` actually
checks (`subject:`, `from:`, `category:`, `isread:`, `hasattachment:`). Before
this suite existed, `_parse_aqs_lite` treated any keyword outside its narrow
whitelist as ordinary free text — so `received:>2026-01-01` became a literal
substring search for the string `"received:>2026-01-01"` against
subject/preview/sender, which can never match anything. That is a *silent*
zero: no error, no warning, indistinguishable from "no emails matched" —
reported live against a real tenant, where `search_emails(query=
"received:>2026-01-01")` returned zero results in the same folder
`get_emails` showed same-day mail in. Two things are pinned here:

1. **A known-but-locally-unsupported keyword must never become a free-text
   term.** It has to be dropped (and named in the returned `dropped` list),
   never appended to `terms` — the whole point is that appending it there
   guarantees a false negative.
2. **An unrecognized `word:value` token that isn't a documented AQS keyword
   at all stays free text**, unchanged from before — e.g. a URL fragment like
   `http://example.com:8080` must not be misread as a filter attempt.

Run:
    python -m tests.unit.test_aqs_lite_fallback
"""

import sys

from exchange_mcp.tools.email import (
    _AQS_KNOWN_UNSUPPORTED_KEYWORDS,
    _local_search_matches,
    _parse_aqs_lite,
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
    """The exact bug report: received:>2026-01-01 must not survive as a term."""
    terms, filters, dropped = _parse_aqs_lite("received:>2026-01-01")
    check("received: dropped", dropped, ["received"])
    check("received: not a free-text term", terms, [])
    check("received: not a filter (no local field for it)", filters, {})


def test_every_documented_unsupported_keyword_is_dropped() -> None:
    for keyword in sorted(_AQS_KNOWN_UNSUPPORTED_KEYWORDS):
        terms, filters, dropped = _parse_aqs_lite(f"{keyword}:something")
        check(f"{keyword}: dropped", dropped, [keyword])
        check(f"{keyword}: not a free-text term", terms, [])


def test_dropped_keyword_alongside_a_real_term_leaves_the_term_intact() -> None:
    """Dropping the unsupported filter must not swallow the rest of the query -
    a mixed query still searches on what the fallback *can* check."""
    terms, filters, dropped = _parse_aqs_lite("quarterly received:>2026-01-01 subject:report")
    check("free word kept", terms, ["quarterly"])
    check("subject: kept as a filter", filters.get("subject"), ["report"])
    check("received: dropped, not merged into terms", dropped, ["received"])


def test_dropped_keyword_only_query_matches_everything_locally() -> None:
    """With the unsupported keyword dropped and nothing else in the query,
    _local_search_matches must fall back to "no filter" (match everything in
    the scanned page) rather than "match nothing" - an unfiltered scan with a
    warning beats a guaranteed, silent empty result."""
    terms, filters, dropped = _parse_aqs_lite("received:>2026-01-01")
    check("dropped, so this is what search_emails warns about", dropped, ["received"])
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
    terms, filters, dropped = _parse_aqs_lite("received:>2026-01-01 received:<2026-06-01")
    check("received: listed once even if repeated", dropped, ["received"])


def main() -> bool:
    for test in (
        test_recognized_filter_is_not_dropped,
        test_known_unsupported_keyword_is_dropped_not_free_text,
        test_every_documented_unsupported_keyword_is_dropped,
        test_dropped_keyword_alongside_a_real_term_leaves_the_term_intact,
        test_dropped_keyword_only_query_matches_everything_locally,
        test_unrecognized_word_colon_value_stays_free_text,
        test_dropped_list_has_no_duplicates,
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
