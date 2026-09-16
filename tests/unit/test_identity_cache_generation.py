"""Pure-logic tests for the identity/timezone caches surviving an account switch.
No mailbox, no browser: a fake transport blocks mid-request on an event, so the
interleaving is deterministic rather than timing-dependent.

The bug these pin: `forget_mailbox_identity()` clears four caches, but each is
written *after* the request that fills it returns. A clear landing in that gap
was simply overwritten by the previous account's result — so after
`login(force=True)` switched accounts, `_timezone` held the old account's zone
and `mailbox_address()` returned the old account's address, looking freshly
resolved. That is the state `resolve_own_mailbox()`'s own docstring calls worse
than not knowing the address at all: `get_meeting_contacts` excludes "self" by
comparing against it, so the user silently stays in their own ranking, and every
write in the process carries the wrong zone.

Why a generation marker and not the locks: two of the three writers hold one
while they wait (`_timezone_lock`, `_user_configuration_lock`) but
`resolve_own_mailbox()` holds none, so for the address there is nothing for a
clear to serialise against. Taking the two that exist would also block `login`
behind an in-flight round-trip. The marker covers all three uniformly.

Reachable in ordinary use: `login(force=True)` *is* the account-switch path, and
under `--transport http` the process is long-lived and shared across clients, so
another tool call being mid-probe is the normal case rather than a contrived one.
A lock held across a round-trip widens the window from nanoseconds to seconds.

Both directions are asserted. A guard that discards too eagerly would be just as
wrong — it would stop the caches working at all, turning one probe per process
into one per call — so `test_the_unraced_path_still_caches` pins that the marker
only bites when the account actually changed.

Run:
    python -m tests.unit.test_identity_cache_generation
"""

import sys
import threading

from exchange_mcp.owa_client import OWAClient

FAILURES: list[str] = []

# The reply the in-flight probe is about to return: account 1's.
ACCOUNT_ONE = {"Body": {"UserOptions": {
    "TimeZone": "Russian Standard Time",
    "UserEmailAddress": "account.one@example.com",
}}}
ACCOUNT_TWO = {"Body": {"UserOptions": {
    "TimeZone": "W. Europe Standard Time",
    "UserEmailAddress": "account.two@example.com",
}}}


def check(label: str, actual, expected) -> None:
    if actual != expected:
        FAILURES.append(f"{label}: expected {expected!r}, got {actual!r}")


class FakeBrowser:
    """Only the surface OWAClient touches. No identity hints, so both lookups
    have to reach the configuration request — which is the path that blocks."""

    owa_url = "https://owa.example.com"
    profile_dir = "/tmp/profile"
    auth_mode = "canary"

    def identity_hints(self) -> dict:
        return {}


def switch_mid_probe(entry_point: str) -> OWAClient:
    """Run `entry_point` on a thread, hold it inside its request, switch accounts.

    The reply is built before the switch, so the thread genuinely carries account
    1's answer past the clear — which is the interleaving that used to lose.
    """
    entered, release = threading.Event(), threading.Event()

    class Blocking(OWAClient):
        def request(self, action, payload, *, timeout=30):
            reply = ACCOUNT_ONE
            entered.set()
            release.wait(5)
            return reply

    client = Blocking(FakeBrowser())
    worker = threading.Thread(target=getattr(client, entry_point), daemon=True)
    worker.start()
    if not entered.wait(5):
        FAILURES.append(f"{entry_point}: never reached its request")
        return client

    client.forget_mailbox_identity()   # login(force=True) for account 2
    release.set()
    worker.join(5)
    return client


def test_a_switch_during_the_timezone_probe_is_not_overwritten() -> None:
    """`mailbox_timezone_detail()` assigns after its probe returns, holding
    `_timezone_lock` throughout — so the clear cannot serialise against it and
    the marker is what makes the assignment conditional."""
    client = switch_mid_probe("mailbox_timezone_detail")
    check("no timezone left cached for the new account", client._timezone, None)
    check("nor the blob it was read from", client._user_configuration, None)


def test_a_switch_during_the_address_probe_is_not_overwritten() -> None:
    """The case no lock could have covered: `resolve_own_mailbox()` holds none
    while it probes. Both the address and the signal fingerprint have to go, or
    the next call would trust a fingerprint written for the old account."""
    client = switch_mid_probe("resolve_own_mailbox")
    check("no address left cached for the new account", client._mailbox_address, None)
    check("nor the signal fingerprint it was resolved against",
          client._mailbox_signals, "")


def test_the_unraced_path_still_caches() -> None:
    """The other direction, and the reason this isn't just "discard more often":
    with no switch, one probe must still serve the whole process. A marker that
    bit unconditionally would turn one request per process into one per call."""
    client = OWAClient(FakeBrowser())
    calls: list[str] = []

    def request(action, payload, *, timeout=30):
        calls.append(action)
        return ACCOUNT_TWO

    client.request = request

    check("timezone resolves", client.mailbox_timezone(), "W. Europe Standard Time")
    check("address resolves", client.mailbox_address(), "account.two@example.com")
    check("both came out of one request", calls, ["GetOwaUserConfiguration"])

    # Repeat reads must not re-probe.
    client.mailbox_timezone()
    client.mailbox_address()
    check("repeats are served from cache", calls, ["GetOwaUserConfiguration"])


def test_a_switch_between_calls_re_resolves() -> None:
    """The ordinary, unraced account switch still has to work: after the clear,
    the next reads must come from the *new* account rather than being stuck on
    the discarded generation."""
    client = OWAClient(FakeBrowser())
    reply = {"value": ACCOUNT_ONE}
    client.request = lambda a, p, *, timeout=30: reply["value"]

    check("account 1 resolves", client.mailbox_address(), "account.one@example.com")
    check("account 1's zone", client.mailbox_timezone(), "Russian Standard Time")

    reply["value"] = ACCOUNT_TWO
    client.forget_mailbox_identity()

    check("account 2 resolves after the switch",
          client.mailbox_address(), "account.two@example.com")
    check("account 2's zone", client.mailbox_timezone(), "W. Europe Standard Time")


def main() -> bool:
    for test in (
        test_a_switch_during_the_timezone_probe_is_not_overwritten,
        test_a_switch_during_the_address_probe_is_not_overwritten,
        test_the_unraced_path_still_caches,
        test_a_switch_between_calls_re_resolves,
    ):
        test()

    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for failure in FAILURES:
            print(f"  - {failure}")
        return False
    print("test_identity_cache_generation: all checks passed")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
