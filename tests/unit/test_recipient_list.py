"""Pure-logic tests for `_build_recipient_list` — the recipient wire shape used
by `send_email` (#103), `reply_email` (#104) and `forward_email` (#105). No
mailbox, no browser, no EXCHANGE_OWA_URL.

Two things here are worth a test rather than a comment, and both fail in the
"looks fine until it reaches Exchange" way that only a live send would catch:

1. **A blank recipient must vanish, not become an empty Mailbox.** The input is
   a single comma-separated string typed by an LLM, so `"a@b.com, "`,
   `"a@b.com,,c@d.com"` and `""` are all routine. An empty `EmailAddress` in the
   list doesn't fail locally — `CreateItem` rejects the *whole* message, so one
   stray comma loses the entire send, and the caller sees a wire-level fault
   with nothing pointing at the comma.
2. **No `__type` on a recipient dict.** Every other payload in this codebase
   carries EWS `__type` annotations, so adding one here looks like a
   consistency fix; OWA rejects `CreateItem` (Message) when recipient Mailbox
   dicts have one. That's recorded in the function's docstring, which is exactly
   the kind of note a refactor overwrites, so it's pinned as a check.

Run:
    python -m tests.unit.test_recipient_list
"""

import sys

from exchange_mcp.tools.email import _build_recipient_list

FAILURES: list[str] = []


def check(label: str, actual, expected) -> None:
    if actual != expected:
        FAILURES.append(f"{label}: expected {expected!r}, got {actual!r}")


def mailbox(addr: str) -> dict:
    """The exact dict OWA accepts for one recipient."""
    return {"Name": addr, "EmailAddress": addr, "RoutingType": "SMTP"}


def test_empty_inputs_produce_no_recipients() -> None:
    """Nothing addressable in, nothing on the wire out."""
    for raw in ("", " ", "\t", "\n", ",", ",,", " , , ", "\n,\t"):
        check(f"{raw!r} -> []", _build_recipient_list(raw), [])


def test_single_address() -> None:
    check(
        "one address -> one Mailbox",
        _build_recipient_list("alice@example.com"),
        [mailbox("alice@example.com")],
    )


def test_surrounding_whitespace_is_stripped() -> None:
    """Whitespace around an address is presentation, never part of the address."""
    for raw in (" alice@example.com", "alice@example.com ", "\talice@example.com\n"):
        check(f"{raw!r} -> stripped", _build_recipient_list(raw), [mailbox("alice@example.com")])


def test_blank_slots_are_dropped_order_preserved() -> None:
    """The failure mode this function exists for: a stray comma must not send an
    empty recipient, and the surviving recipients must keep their order."""
    cases = {
        "alice@example.com,bob@example.com": ["alice@example.com", "bob@example.com"],
        "alice@example.com, bob@example.com": ["alice@example.com", "bob@example.com"],
        "alice@example.com,,bob@example.com": ["alice@example.com", "bob@example.com"],
        "alice@example.com, , bob@example.com": ["alice@example.com", "bob@example.com"],
        "alice@example.com,": ["alice@example.com"],
        ",alice@example.com": ["alice@example.com"],
        " ,alice@example.com, ,bob@example.com, ": ["alice@example.com", "bob@example.com"],
    }
    for raw, expected in cases.items():
        check(f"{raw!r} -> {len(expected)} recipient(s)",
              _build_recipient_list(raw), [mailbox(addr) for addr in expected])


def test_no_type_annotation_on_recipients() -> None:
    """OWA rejects CreateItem (Message) if a recipient Mailbox carries __type.

    Also pins the key set: an unexpected extra key is the same class of bug.
    """
    recipients = _build_recipient_list("alice@example.com, bob@example.com")
    for index, entry in enumerate(recipients):
        check(f"recipient {index} has no __type", "__type" in entry, False)
        check(f"recipient {index} keys", sorted(entry), ["EmailAddress", "Name", "RoutingType"])
        check(f"recipient {index} RoutingType", entry["RoutingType"], "SMTP")


def test_name_defaults_to_the_address() -> None:
    """`Name` is the address itself — there is no display-name parsing here.

    A `"Alice <alice@example.com>"` input is passed through verbatim into
    `EmailAddress` rather than being split, so it is *not* a supported form.
    Pinned as a known limitation: if display-name parsing is ever added, this
    check is the thing that should be updated deliberately instead of the
    behaviour changing unnoticed under callers who pre-format their addresses.
    """
    recipients = _build_recipient_list("alice@example.com")
    check("Name == EmailAddress", recipients[0]["Name"], recipients[0]["EmailAddress"])

    display_form = _build_recipient_list("Alice <alice@example.com>")
    check("display-name form is not parsed",
          display_form, [mailbox("Alice <alice@example.com>")])


def test_duplicates_are_not_deduplicated() -> None:
    """Deduplication is the server's job — dropping a repeat locally would make
    the returned count disagree with what the caller asked for."""
    check("duplicate kept",
          _build_recipient_list("alice@example.com, alice@example.com"),
          [mailbox("alice@example.com"), mailbox("alice@example.com")])


def main() -> bool:
    for test in (
        test_empty_inputs_produce_no_recipients,
        test_single_address,
        test_surrounding_whitespace_is_stripped,
        test_blank_slots_are_dropped_order_preserved,
        test_no_type_annotation_on_recipients,
        test_name_defaults_to_the_address,
        test_duplicates_are_not_deduplicated,
    ):
        test()

    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for failure in FAILURES:
            print(f"  - {failure}")
        return False
    print("test_recipient_list: all checks passed")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
