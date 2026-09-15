"""Pure-logic tests for `_MEETING_RESPONSES` — the table `respond_to_meeting`
reads both its EWS item type and its human-readable wording from. No mailbox,
no browser, no EXCHANGE_OWA_URL (the table is a module-level dict; importing
the tool module is enough, and no test here calls the tool).

Why this table deserves pinning, despite the bug that prompted it being
cosmetic (issue #13: the message was built as `f"Meeting {response.lower()}ed"`,
which reads correctly for Accept/Decline and produced "tentativeed" for
Tentative):

1. **The wire column is the high-stakes one.** A wrong `__type` here does not
   look like a bug from the outside — it sends a *different RSVP than the user
   asked for* to a real organizer, and reports `{"success": true}` for it.
   Accept/Decline transposed is the worst case and the easiest to introduce by
   editing one line of a three-line dict, so the spellings are asserted
   literally rather than by pattern.
2. **The two facts about a verb must not drift.** They live in one tuple per
   verb precisely so a fourth response can't be added to the wire mapping and
   forgotten in the wording (which is what a separate past-tense table would
   have allowed). These tests hold that shape: every row carries both halves.
3. **Both columns are asserted as whole-dict equalities**, not per-key, so a
   fourth response added to the table fails these tests until it is given a
   deliberate wording — and until the tool's own "Must be Accept, Decline, or
   Tentative." error text and docstring are updated to match. A generalised
   "no phrase may be verb+'ed'" rule was considered and rejected: it cannot
   distinguish the reported bug ("tentativeed") from a verb whose past tense
   legitimately is verb+"ed" (as Accept's and Decline's are).

The tool renders its message as `f"Meeting {past_tense}"`, so the phrases
asserted below are the sentences "Meeting accepted" / "Meeting declined" /
"Meeting marked tentative" minus that shared prefix. The prefix itself lives in
the tool and is not reachable without a client.

Run:
    python -m tests.unit.test_meeting_response
"""

import sys

from exchange_mcp.tools.calendar import _MEETING_RESPONSES

FAILURES: list[str] = []


def check(label: str, actual, expected) -> None:
    if actual != expected:
        FAILURES.append(f"{label}: expected {expected!r}, got {actual!r}")


def test_every_response_has_wire_type_and_phrase() -> None:
    """Both halves present, both non-empty strings — the shape the tool unpacks
    (`response_type, past_tense = mapped`), so a one-element row is a TypeError
    at call time, on a live RSVP."""
    for verb, row in sorted(_MEETING_RESPONSES.items()):
        check(f"{verb}: row is a 2-tuple", isinstance(row, tuple) and len(row) == 2, True)
        if not (isinstance(row, tuple) and len(row) == 2):
            continue
        wire, phrase = row
        check(f"{verb}: wire type is a non-empty str",
              isinstance(wire, str) and bool(wire), True)
        check(f"{verb}: phrase is a non-empty str",
              isinstance(phrase, str) and bool(phrase), True)


def test_wire_types_are_the_ews_spellings() -> None:
    """Literal, not pattern-matched: a transposition here sends the wrong RSVP
    and still reports success."""
    expected = {
        "Accept": "AcceptItem:#Exchange",
        "Decline": "DeclineItem:#Exchange",
        "Tentative": "TentativelyAcceptItem:#Exchange",
    }
    check("wire types", {v: row[0] for v, row in _MEETING_RESPONSES.items()}, expected)


def test_phrases_are_the_expected_wording() -> None:
    """The user-visible half. "marked tentative" rather than a past participle
    because Tentative has none that reads as a sentence after "Meeting "."""
    expected = {
        "Accept": "accepted",
        "Decline": "declined",
        "Tentative": "marked tentative",
    }
    check("phrases", {v: row[1] for v, row in _MEETING_RESPONSES.items()}, expected)


def test_lookup_is_exact_and_case_sensitive() -> None:
    """`respond_to_meeting` does a plain `.get(response)`, so anything but the
    documented capitalisation must miss the table and be rejected with the
    "Invalid response" error — not silently guessed into an RSVP."""
    for wrong in ("accept", "ACCEPT", "TENTATIVE", "tentative", " Accept",
                  "Accept ", "Accepted", "Maybe", ""):
        check(f"{wrong!r} is not a valid response", wrong in _MEETING_RESPONSES, False)


def main() -> bool:
    for test in (
        test_every_response_has_wire_type_and_phrase,
        test_wire_types_are_the_ews_spellings,
        test_phrases_are_the_expected_wording,
        test_lookup_is_exact_and_case_sensitive,
    ):
        test()

    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for failure in FAILURES:
            print(f"  - {failure}")
        return False
    print("test_meeting_response: all checks passed")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
