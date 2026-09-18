"""Pure-logic tests for OWAClient.search_conversations_substrate's retry-on-empty
behaviour (issue brief item 9, 2026-09-18). No mailbox, no browser: `request_substrate`
is overridden on a fake subclass, and `time.sleep` is monkeypatched so the test doesn't
actually wait.

The bug this pins: Substrate Search has been observed answering an identical query
with a confident, unflagged, empty result on one call and the correct non-empty result
on an immediate retry with nothing else changed - indistinguishable from a genuine
zero-match search unless the caller re-issues the same query. `search_conversations_substrate`
now retries a bare empty result up to `_SUBSTRATE_EMPTY_RETRY_ATTEMPTS` times before
trusting it, the same asymmetric-retry shape as BrowserSession's `_SESSION_PROBE_ATTEMPTS`:
a false empty is expensive (indistinguishable from a real zero), a false non-empty can't
happen (any non-empty answer came straight from the server), so only the empty case pays
for a second look.

Four things are asserted:
1. Empty then non-empty -> the non-empty result wins, and exactly 2 requests were made.
2. Non-empty on the first try -> returned immediately, exactly 1 request, no sleep.
3. Empty on every attempt -> a genuine (retried) empty list, not None, after exactly
   `_SUBSTRATE_EMPTY_RETRY_ATTEMPTS` requests.
4. An exception on the first attempt (BearerModeRequiredError, SessionExpiredError, or
   any other transport failure) propagates immediately and is never retried - only a
   bare *empty list* is suspect here, not a failure signal.

Run:
    python -m tests.unit.test_substrate_search_retry
"""

import sys

import exchange_mcp.owa_client as owa_client_module
from exchange_mcp.owa_client import BearerModeRequiredError, OWAClient, SessionExpiredError

FAILURES: list[str] = []


def check(label: str, actual, expected) -> None:
    if actual != expected:
        FAILURES.append(f"{label}: expected {expected!r}, got {actual!r}")


class FakeBrowser:
    owa_url = "https://owa.example.com"
    profile_dir = "/tmp/profile"
    auth_mode = "bearer"

    def identity_hints(self) -> dict:
        return {}


_SOURCE = {"ConversationId": {"Id": "conv-1"}}


def _make_client(responses):
    """An OWAClient whose request_substrate pops from `responses` in order,
    and whose plain request() always fails so mailbox_timezone_detail() just
    degrades to UTC rather than needing its own fake response."""

    calls: list[None] = []

    class FakeClient(OWAClient):
        def request(self, action, payload, *, timeout=30):
            raise RuntimeError("no config in this fake")

        def request_substrate(self, path, headers, payload, *, timeout=30):
            calls.append(None)
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

    return FakeClient(FakeBrowser()), calls


def _no_sleep_calls() -> list[float]:
    """Monkeypatch owa_client.time.sleep to record calls instead of blocking."""
    recorded: list[float] = []
    owa_client_module.time.sleep = lambda seconds: recorded.append(seconds)
    return recorded


def test_empty_then_nonempty_returns_the_nonempty_result() -> None:
    sleeps = _no_sleep_calls()
    client, calls = _make_client([
        {"EntitySets": [{"ResultSets": [{"Results": []}]}]},
        {"EntitySets": [{"ResultSets": [{"Results": [{"Source": _SOURCE}]}]}]},
    ])
    results = client.search_conversations_substrate("q", size=25)
    check("second attempt's result wins", results, [_SOURCE])
    check("exactly 2 requests made", len(calls), 2)
    check("slept once between attempts", len(sleeps), 1)


def test_nonempty_on_first_try_short_circuits() -> None:
    sleeps = _no_sleep_calls()
    client, calls = _make_client([
        {"EntitySets": [{"ResultSets": [{"Results": [{"Source": _SOURCE}]}]}]},
        {"EntitySets": [{"ResultSets": [{"Results": [{"Source": _SOURCE}]}]}]},
    ])
    results = client.search_conversations_substrate("q", size=25)
    check("first attempt's result used", results, [_SOURCE])
    check("no retry needed", len(calls), 1)
    check("never slept", len(sleeps), 0)


def test_empty_on_every_attempt_returns_a_genuine_empty_list() -> None:
    _no_sleep_calls()
    client, calls = _make_client([
        {"EntitySets": [{"ResultSets": [{"Results": []}]}]},
        {"EntitySets": [{"ResultSets": [{"Results": []}]}]},
    ])
    results = client.search_conversations_substrate("q", size=25)
    check("still an empty list, not None", results, [])
    check("stopped at the attempt cap", len(calls), owa_client_module._SUBSTRATE_EMPTY_RETRY_ATTEMPTS)


def test_exception_on_first_attempt_is_not_retried() -> None:
    _no_sleep_calls()
    client, calls = _make_client([BearerModeRequiredError("classic OWA")])
    try:
        client.search_conversations_substrate("q", size=25)
        FAILURES.append("BearerModeRequiredError: expected it to propagate, but it didn't raise")
    except BearerModeRequiredError:
        pass
    check("exactly 1 request made, no retry on a real failure", len(calls), 1)


def test_session_expired_on_first_attempt_is_not_retried() -> None:
    _no_sleep_calls()
    client, calls = _make_client([SessionExpiredError("expired")])
    try:
        client.search_conversations_substrate("q", size=25)
        FAILURES.append("SessionExpiredError: expected it to propagate, but it didn't raise")
    except SessionExpiredError:
        pass
    check("exactly 1 request made, no retry on a real failure", len(calls), 1)


def main() -> bool:
    for test in (
        test_empty_then_nonempty_returns_the_nonempty_result,
        test_nonempty_on_first_try_short_circuits,
        test_empty_on_every_attempt_returns_a_genuine_empty_list,
        test_exception_on_first_attempt_is_not_retried,
        test_session_expired_on_first_attempt_is_not_retried,
    ):
        test()

    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for failure in FAILURES:
            print(f"  - {failure}")
        return False
    print("test_substrate_search_retry: all checks passed")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
