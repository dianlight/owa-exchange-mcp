"""Pure-logic tests for `_resolve_ids_only_fields` - get_emails' `fields`
argument, added 2026-09-17 alongside the batched get_email_status fields.

get_emails(ids_only=True, limit=500) returns "item_id", "conversation_id",
"date" and "subject" for every one of 500 conversations - a payload observed
around 150KB, large enough to exceed some MCP hosts' own max-result-token
limit before the caller ever sees a "there is more" signal from `pagination`.
`fields` lets a caller that only needs item_id (e.g. to feed into
get_email_status or another batch tool) ask for just that, shrinking the
payload instead of being forced to lower `limit` and page more.

Run:
    python -m tests.unit.test_ids_only_fields
"""

import sys

from exchange_mcp.tools.email import _IDS_ONLY_FIELDS, _resolve_ids_only_fields

FAILURES: list[str] = []


def check(label: str, actual, expected) -> None:
    if actual != expected:
        FAILURES.append(f"{label}: expected {expected!r}, got {actual!r}")


def test_default_empty_fields_keeps_everything() -> None:
    selected, warning, error = _resolve_ids_only_fields("", ids_only=True)
    check("default selects all", selected, _IDS_ONLY_FIELDS)
    check("default no warning", warning, None)
    check("default no error", error, None)


def test_single_field_narrows_the_payload() -> None:
    selected, warning, error = _resolve_ids_only_fields("item_id", ids_only=True)
    check("single field", selected, ("item_id",))
    check("single field no warning", warning, None)
    check("single field no error", error, None)


def test_multiple_fields_keep_canonical_order_regardless_of_request_order() -> None:
    """The response shape must not depend on the order the caller listed
    fields in - "date,item_id" and "item_id,date" must produce the same
    key order, so a caller parsing positionally isn't surprised."""
    a, _, _ = _resolve_ids_only_fields("date,item_id", ids_only=True)
    b, _, _ = _resolve_ids_only_fields("item_id,date", ids_only=True)
    check("order a", a, ("item_id", "date"))
    check("order b", b, ("item_id", "date"))


def test_whitespace_and_empty_entries_are_tolerated() -> None:
    selected, _, error = _resolve_ids_only_fields(" item_id , , subject ", ids_only=True)
    check("tolerant selection", selected, ("item_id", "subject"))
    check("tolerant no error", error, None)


def test_unknown_field_is_reported_not_silently_dropped() -> None:
    """A typo'd field name must fail loudly, not just vanish from the output -
    same reasoning as the AQS-lite fallback's dropped-keyword handling."""
    selected, warning, error = _resolve_ids_only_fields("item_id,bogus", ids_only=True)
    check("unknown field selects nothing", selected, ())
    check("unknown field no warning", warning, None)
    check("unknown field named in error", "bogus" in (error or ""), True)


def test_fields_without_ids_only_is_a_warning_not_a_failure() -> None:
    """fields has nothing to narrow when ids_only=False - the call should
    still proceed (unnarrowed), just say why fields had no effect."""
    selected, warning, error = _resolve_ids_only_fields("item_id", ids_only=False)
    check("ignored selects everything", selected, _IDS_ONLY_FIELDS)
    check("ignored warns", "ids_only" in (warning or ""), True)
    check("ignored no error", error, None)


def test_empty_fields_without_ids_only_is_silent() -> None:
    """The common case (fields left at its default "") must not warn just
    because ids_only also happens to be False."""
    selected, warning, error = _resolve_ids_only_fields("", ids_only=False)
    check("silent default selects everything", selected, _IDS_ONLY_FIELDS)
    check("silent default no warning", warning, None)
    check("silent default no error", error, None)


def main() -> bool:
    for test in (
        test_default_empty_fields_keeps_everything,
        test_single_field_narrows_the_payload,
        test_multiple_fields_keep_canonical_order_regardless_of_request_order,
        test_whitespace_and_empty_entries_are_tolerated,
        test_unknown_field_is_reported_not_silently_dropped,
        test_fields_without_ids_only_is_a_warning_not_a_failure,
        test_empty_fields_without_ids_only_is_silent,
    ):
        test()

    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for failure in FAILURES:
            print(f"  - {failure}")
        return False
    print("test_ids_only_fields: all checks passed")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
