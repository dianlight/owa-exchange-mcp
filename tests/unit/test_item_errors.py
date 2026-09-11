"""Unit test: per-item failure classification and the payload shape it feeds.

Unlike tests/smoke/, this needs no live mailbox, no browser and no
EXCHANGE_OWA_URL -- everything under test is pure logic.

Why this exists: client skills used to detect "this item can't be categorised"
by substring-matching an HTTP 500 message, then fall back to an Outlook-COM
connector. That is only fragile because the failure had no code. These checks
pin the codes down as a contract, and pin down the thing that made the contract
necessary: an unrecognised failure must classify as the generic
ITEM_READ_FAILED rather than being guessed into a specific code, because a
caller acting on a wrong specific code is worse off than one told "unknown".

Run standalone:
    python -m tests.unit.test_item_errors
"""

import sys

from exchange_mcp import utils

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  OK   {name}")
        return
    _failures.append(f"{name}{': ' + detail if detail else ''}")
    print(f"  FAIL {name}{': ' + detail if detail else ''}")


# The real message a live MeetingRequestMessage read produced on 2026-09-11 --
# note it is a *truncated* JSON body: OWA faulted partway through serialising
# its own response, which is why the snippet breaks off inside the item's
# __type marker.
LIVE_SERIALIZATION_500 = (
    "OWA request failed (HTTP 500) "
    "(System.Runtime.Serialization.SerializationException). Snippet: "
    '{"Header":{"ServerVersionInfo":{"MajorVersion":15,"MinorVersion":21,'
    '"MajorBuildNumber":406,"MinorBuildNumber":6,"Version":"V2018_01_18"}},'
    '"Body":{"__type":"GetItemResponse:#Exchange","ResponseMessages":{"Items":'
    '[{"__type":"ItemInfoResponseMessage:#Exchange","Items":[{"__type":'
    '"MeetingRequestMessageT'
)


def test_known_codes() -> None:
    print("Recognised failure kinds")
    cases = [
        (LIVE_SERIALIZATION_500, utils.ITEM_NOT_SERIALIZABLE),
        ("System.Runtime.Serialization.SerializationException", utils.ITEM_NOT_SERIALIZABLE),
        ("ErrorItemNotFound", utils.ITEM_NOT_FOUND),
        ("The specified object was not found in the store.", utils.ITEM_NOT_FOUND),
        ("ErrorAccessDenied", utils.ITEM_ACCESS_DENIED),
        ("Access is denied. Check credentials and try again.", utils.ITEM_ACCESS_DENIED),
    ]
    for message, expected in cases:
        actual = utils.classify_item_error(message)
        check(f"{message[:48]!r} -> {expected}", actual == expected, actual)


def test_case_insensitive() -> None:
    print("Matching is case-insensitive (server casing varies)")
    for variant in ("SERIALIZATIONEXCEPTION", "serializationexception", "SerializationException"):
        check(f"{variant} -> item_not_serializable",
              utils.classify_item_error(variant) == utils.ITEM_NOT_SERIALIZABLE)


def test_no_false_positives() -> None:
    print("Unrecognised failures are not guessed at")
    cases = [
        "",
        "Session expired (HTTP 440).",
        "ErrorInvalidPropertySet",
        "Invalid argument used to call method UpdateItem",
        "Brand new fault nobody has seen yet",
    ]
    for message in cases:
        actual = utils.classify_item_error(message)
        check(f"{message[:40]!r} -> item_read_failed", actual == utils.ITEM_READ_FAILED, actual)


def test_item_error_payload() -> None:
    print("item_error() payload shape")
    failure = utils.item_error("AAkALg==", LIVE_SERIALIZATION_500)
    check("carries item_id", failure.get("item_id") == "AAkALg==", repr(failure.get("item_id")))
    check("carries the raw error for humans/logs",
          failure.get("error") == LIVE_SERIALIZATION_500)
    check("carries the machine-readable code",
          failure.get("error_code") == utils.ITEM_NOT_SERIALIZABLE,
          repr(failure.get("error_code")))
    check("carries remediation for a known code", bool(failure.get("hint")))

    # An unknown code has nothing honest to say, so it must not invent a hint --
    # a confident-sounding wrong remediation is the failure mode to avoid.
    unknown = utils.item_error("AAkALg==", "Brand new fault nobody has seen yet")
    check("unknown code -> generic code", unknown["error_code"] == utils.ITEM_READ_FAILED)
    check("unknown code -> no invented hint", "hint" not in unknown, repr(unknown.get("hint")))


def test_codes_are_stable_strings() -> None:
    print("Codes are the literal strings callers branch on")
    expected = {
        "ITEM_NOT_SERIALIZABLE": "item_not_serializable",
        "ITEM_NOT_FOUND": "item_not_found",
        "ITEM_ACCESS_DENIED": "item_access_denied",
        "ITEM_READ_FAILED": "item_read_failed",
    }
    for name, literal in expected.items():
        check(f"{name} == {literal!r}", getattr(utils, name) == literal, getattr(utils, name))


def main() -> bool:
    for test in (
        test_known_codes,
        test_case_insensitive,
        test_no_false_positives,
        test_item_error_payload,
        test_codes_are_stable_strings,
    ):
        test()
    print()
    if _failures:
        print(f"{len(_failures)} check(s) failed:")
        for failure in _failures:
            print(f"  - {failure}")
        return False
    print("All checks passed.")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
