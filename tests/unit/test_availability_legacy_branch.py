"""Pure-logic tests for the classic-OWA `GetUserAvailability` branch, which **no
live run can reach**. No mailbox, no browser: a fake transport answers the action
and records the request.

That unreachability is the whole reason this file exists. On this tenant the
action answers a permanent `{ErrorCode: 500, ExceptionName:
NotImplementedException}` (PROJECT_STATUS.md #602), so `tests/smoke/` cannot
exercise the fallback in `availability._get_availability_events` at all — not as
a gap in the smoke suite, but as a property of the backend. Every other branch of
the availability tools has a live run behind it; this one has only whatever a
unit test asserts.

And it is the branch where the frame rule is subtlest, because it is the one
request in the package that both *sends* a `TimeZoneContext` and *reads*
timestamps back:

- An **unqualified** `CalendarEvent` time is already wall clock in the zone we
  asked for, so converting it would shift it a second time.
- An **offset-bearing** one is a real instant and must be converted.

`availability_frame.wire_to_wall_clock` decides that per value. Get it wrong in
either direction and the result is a plausible-looking wrong answer — the #601
symptom — on the one code path no live run can contradict. So the test asserts
the two spellings of a single instant land on the same wall clock, which is a
property neither branch of that rule can satisfy alone.

`test_availability_frame.py` covers the frame logic itself, thoroughly. What it
does not do is drive the tool helper, so it cannot see which zone reaches the
payload or how the response is read back; that is what these two tests add.

Run:
    python -m tests.unit.test_availability_legacy_branch
"""

import datetime as dt
import sys

from exchange_mcp import availability_frame as frame
from exchange_mcp import mailbox_timezone as mtz
from exchange_mcp.browser_session import BearerModeRequiredError
from exchange_mcp.tools import availability as av

FAILURES: list[str] = []

W_EUROPE = "W. Europe Standard Time"
DAY = dt.date(2026, 9, 17)


def check(label: str, actual, expected) -> None:
    if actual != expected:
        FAILURES.append(f"{label}: expected {expected!r}, got {actual!r}")


def zone():
    """A resolved W. Europe zone, or None when there is no IANA database."""
    z = frame.resolve_zone(W_EUROPE, source=frame.SOURCE_MAILBOX)
    return z if z.tz is not None else None


class FakeClient:
    """Only the surface the legacy branch touches. Records every request.

    `get_schedule` raising `BearerModeRequiredError` is what the real client does
    on a tenant with no substrate surface, and it is the only way into the branch
    under test.
    """

    def __init__(self, availability):
        self.availability = availability
        self.requests: list[tuple] = []

    def request_header(self, server_version, *, with_timezone=True):
        # Mirrors OWAClient.request_header, so a change to the real builder's
        # contract shows up here rather than being papered over by a stub.
        return mtz.request_header(server_version, W_EUROPE if with_timezone else None)

    def mailbox_timezone(self):
        return W_EUROPE

    def get_schedule(self, emails, start, end, *, tz_id=None, interval_minutes=30):
        raise BearerModeRequiredError("classic OWA has no substrate surface")

    def request(self, action, payload, *, timeout=30):
        self.requests.append((action, payload))
        if action == "GetUserAvailability":
            return self.availability
        raise AssertionError(f"unexpected action {action}")


def cal_event(start: str, end: str, busy: str = "Busy") -> dict:
    return {"BusyType": busy, "StartTime": start, "EndTime": end}


def availability_response(*events: dict) -> dict:
    return {"Body": {"FreeBusyResponseArray": [
        {"FreeBusyView": {"CalendarEventArray": {"Items": list(events)}}}
    ]}}


def tz_id(header: dict):
    ctx = header.get("TimeZoneContext")
    return None if ctx is None else ctx["TimeZoneDefinition"]["Id"]


def test_legacy_branch_sends_the_zone_and_reads_it_back_once() -> None:
    """The branch sends the mailbox's zone, and converts a timestamp exactly
    once — not zero times, not twice.

    Both halves matter and they pull in opposite directions, which is why they
    are asserted together: the unqualified value must survive untouched, the
    offset-bearing one must move, and the two must agree because they name the
    same instant. A conversion applied to both would satisfy neither.
    """
    z = zone()

    unqualified = FakeClient(availability_response(
        cal_event("2026-09-17T14:30:00", "2026-09-17T15:30:00"),
    ))
    busy = av._get_availability_events(unqualified, "someone@example.com", DAY, DAY, z)

    check("the legacy action was the one reached",
          [a for a, _ in unqualified.requests], ["GetUserAvailability"])
    check("the request carries the mailbox's zone",
          tz_id(unqualified.requests[0][1]["Header"]), W_EUROPE)
    check("one busy period parsed", len(busy), 1)
    check("an unqualified time is left where the server put it",
          busy[0]["start"], dt.datetime(2026, 9, 17, 14, 30))

    aware = FakeClient(availability_response(
        cal_event("2026-09-17T12:30:00Z", "2026-09-17T13:30:00Z"),
    ))
    busy_aware = av._get_availability_events(aware, "someone@example.com", DAY, DAY, z)
    check("an offset-bearing time is converted",
          busy_aware[0]["start"], dt.datetime(2026, 9, 17, 14, 30))
    check("so both spellings of one instant agree",
          busy_aware[0]["start"], busy[0]["start"])


def test_legacy_branch_skips_free_and_nodata() -> None:
    """Free/NoData are not busy periods. Asserted here because this filter sits
    in the same loop as the conversion above, so a change to one is exactly when
    the other gets moved by accident — and on this branch no live run would
    notice."""
    client = FakeClient(availability_response(
        cal_event("2026-09-17T09:00:00", "2026-09-17T10:00:00", busy="Free"),
        cal_event("2026-09-17T10:00:00", "2026-09-17T11:00:00", busy="NoData"),
        cal_event("2026-09-17T14:30:00", "2026-09-17T15:30:00", busy="OOF"),
    ))
    busy = av._get_availability_events(client, "someone@example.com", DAY, DAY, zone())
    check("only the non-free block survives", [b["status"] for b in busy], ["OOF"])


def main() -> bool:
    if zone() is None:
        print("test_availability_legacy_branch: FAILED - no IANA timezone database, "
              "so the mailbox zone cannot be resolved (is `tzdata` installed?)")
        return False

    for test in (
        test_legacy_branch_sends_the_zone_and_reads_it_back_once,
        test_legacy_branch_skips_free_and_nodata,
    ):
        test()

    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for failure in FAILURES:
            print(f"  - {failure}")
        return False
    print("test_availability_legacy_branch: all checks passed")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
