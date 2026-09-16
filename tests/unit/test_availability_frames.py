"""Pure-logic tests for the timezone frames the availability/analytics tools
send and parse. No mailbox, no browser: a fake transport answers
`GetUserAvailability` / `GetSchedule` from canned data and records every
request, so the *payload* is assertable and not just the parsed result — the
same technique as tests/unit/test_folder_resolution.py.

Why this suite exists rather than a smoke test: **the classic-OWA branches
cannot be exercised against a live mailbox at all.** `GetUserAvailability`
answers a permanent `{ErrorCode: 500, ExceptionName: NotImplementedException}`
on this tenant (PROJECT_STATUS.md #602), so the legacy fallbacks in
`availability._get_availability_events`, `find_meeting_time` and
`analytics._get_availability_events` are unreachable from any live run. The
timezone fix of 2026-09-16 (#601/#602) changed all three of them — it made the
`TimeZoneContext` the mailbox's own instead of a hardcoded `"Russian Standard
Time"`, and it put the responses through a conversion. Shipping that with no
way to check it was the gap §4 recorded; this closes it by checking the
branches against the *documented* EWS contract instead of against a server that
refuses to answer.

The load-bearing assertion is the double-conversion guard
(`test_legacy_unqualified_times_are_not_converted_twice`). These requests carry
a `TimeZoneContext`, so EWS answers `CalendarEventArray` times as unqualified
wall clock **in that timezone** — already mailbox-local. `from_utc()` there
would shift them a second time, and the symptom would be indistinguishable from
the original #601 bug: plausible-looking slots, silently offset. That is exactly
what `MailboxTimezone.from_wire_timestamp` exists to prevent (aware = a real
instant, naive = wire wall clock) and it is unfalsifiable without a fake
transport.

Two invariants are asserted across *both* timezone configurations rather than
just the good one, because the fix deliberately keeps two live paths:

1. **`mailbox_configuration`** — the mailbox's Windows id goes on the wire, so
   the wire frame already *is* the local frame and the conversions are
   identities. This is the normal case.
2. **`host_local`** — the id is unknown, so `"UTC"` goes on the wire and every
   value is converted here. The window shifts (local midnight is 22:00Z the day
   before, in W. Europe summer) but the *answer must not*:
   `test_the_same_freebusy_string_yields_the_same_local_slots` pins that. If it
   ever fails, the wire window and the parsing base have drifted apart, which is
   the failure mode that produced #602 in the first place.

Also pinned here: that a `GetSchedule` failure is *reported* rather than
crashing or being swallowed, including the narrow-exception rule
(`test_analytics_does_not_swallow_an_authentication_error`) —
`AuthenticationRequiredError` is a plain `Exception`, so a broad `except
Exception` in the availability helpers would answer "no meetings" to a mailbox
that merely needs a human to sign in.

Run:
    python -m tests.unit.test_availability_frames
"""

import datetime as dt
import json
import sys
import types

from exchange_mcp.auth_errors import AuthenticationRequiredError
from exchange_mcp.browser_session import BearerModeRequiredError
from exchange_mcp.mailbox_timezone import resolve_mailbox_timezone
from exchange_mcp.tools import analytics as an
from exchange_mcp.tools import availability as av

FAILURES: list[str] = []

W_EUROPE = "W. Europe Standard Time"
DAY = dt.date(2026, 9, 17)


def check(label: str, actual, expected) -> None:
    if actual != expected:
        FAILURES.append(f"{label}: expected {expected!r}, got {actual!r}")


def local_tz():
    """The normal case: the probe read the mailbox's timezone off the server, so
    it goes on the wire and the wire frame *is* the local frame."""
    tz = resolve_mailbox_timezone(W_EUROPE)
    if tz.source != "mailbox_configuration":  # no tzdata -> skip, see main()
        return None
    return tz


def utc_wire_tz():
    """The degraded case: no id known, so "UTC" goes on the wire and every value
    is converted here. Built by hand rather than via `resolve_mailbox_timezone`
    so the test does not depend on what timezone the *host* happens to be in."""
    known = resolve_mailbox_timezone(W_EUROPE)
    if known.source != "mailbox_configuration":
        return None
    # windows_id empty -> wire_id falls through to "UTC", tz still W. Europe.
    return known.__class__(
        source="mailbox_configuration", windows_id="", iana_id=known.iana_id, tz=known.tz
    )


# ------------------------------------------------------------------
# Fake transport
# ------------------------------------------------------------------

class FakeClient:
    """Stands in for OWAClient. Records every request; serves canned responses.

    `bearer=False` is what puts the tools onto the classic-OWA path: it makes
    `get_schedule` raise `BearerModeRequiredError`, exactly as the real client
    does on a tenant with no substrate surface. That is the only way to reach
    the legacy branches at all.
    """

    def __init__(self, tz, *, bearer=False, schedule=None, availability=None,
                 schedule_raises=None):
        self._tz = tz
        self.bearer = bearer
        self.schedule = schedule or []
        self.availability = availability or {"Body": {"FreeBusyResponseArray": []}}
        self.schedule_raises = schedule_raises
        self.requests: list[tuple] = []
        self.schedule_calls: list[dict] = []
        self.user_email = "mailbox@example.com"

    def mailbox_timezone(self):
        return self._tz

    def get_schedule(self, emails, start, end, *, tz_id=None, interval_minutes=30):
        self.schedule_calls.append({"emails": list(emails), "start": start,
                                    "end": end, "tz_id": tz_id})
        if self.schedule_raises is not None:
            raise self.schedule_raises
        if not self.bearer:
            raise BearerModeRequiredError("classic OWA has no substrate surface")
        return self.schedule

    def request(self, action, payload, *, timeout=30):
        self.requests.append((action, payload))
        if action == "GetUserAvailability":
            return self.availability
        raise AssertionError(f"unexpected action {action}")

    # Only reached by find_free_time's no-user_email fallback, which these
    # tests do not use; present so an accidental call is loud rather than an
    # AttributeError deep inside a tool.
    def get_folder_id(self, name):
        raise AssertionError("these tests should not reach the FindItem fallback")


def fake_ctx(client):
    """The two attribute hops `_get_client` makes: ctx.request_context
    .lifespan_context.client. The tool functions are plain functions (the
    @mcp.tool() decorator returns them unchanged), so they can be called
    directly with this."""
    return types.SimpleNamespace(
        request_context=types.SimpleNamespace(
            lifespan_context=types.SimpleNamespace(client=client)
        )
    )


def cal_event(start: str, end: str, busy: str = "Busy", subject: str = "") -> dict:
    ev = {"BusyType": busy, "StartTime": start, "EndTime": end}
    if subject:
        ev["CalendarEventDetails"] = {"Subject": subject}
    return ev


def availability_response(*events: dict) -> dict:
    return {"Body": {"FreeBusyResponseArray": [
        {"FreeBusyView": {"CalendarEventArray": {"Items": list(events)}}}
    ]}}


def merged_freebusy_response(merged: str) -> dict:
    return {"Body": {"FreeBusyResponseArray": [
        {"FreeBusyView": {"MergedFreeBusy": merged}}
    ]}}


def tz_context_id(payload: dict) -> str:
    return (payload["Header"]["TimeZoneContext"]["TimeZoneDefinition"]["Id"])


def time_window(payload: dict) -> tuple:
    win = payload["Body"]["FreeBusyViewOptions"]["TimeWindow"]
    return win["StartTime"], win["EndTime"]


# ------------------------------------------------------------------
# The legacy GetUserAvailability branch in availability.py
# ------------------------------------------------------------------

def test_legacy_sends_the_mailbox_timezone_and_a_local_day_window() -> None:
    """The `TimeZoneContext` used to be the literal "Russian Standard Time" — a
    stray UTC+3 that also meant the *window* covered the wrong 24 hours. Both
    halves are asserted, because sending the right timezone with the wrong
    window would still return the wrong day's meetings."""
    tz = local_tz()
    client = FakeClient(tz, availability=availability_response())
    av._get_availability_events(client, "someone@example.com", DAY, DAY, tz)

    check("one GetUserAvailability request", [a for a, _ in client.requests],
          ["GetUserAvailability"])
    payload = client.requests[0][1]
    check("TimeZoneContext is the mailbox's own zone", tz_context_id(payload), W_EUROPE)
    check("window is local midnight to local midnight", time_window(payload),
          ("2026-09-17T00:00:00", "2026-09-18T00:00:00"))


def test_legacy_unqualified_times_are_not_converted_twice() -> None:
    """**The guard this suite exists for.** The request carries a
    `TimeZoneContext`, so an unqualified `CalendarEvent` time is already wall
    clock in it — i.e. already mailbox-local. Converting it again with
    `from_utc()` would move a 14:30 meeting to 16:30, and the resulting free
    slots would look every bit as plausible as the ones #601 shipped."""
    tz = local_tz()
    client = FakeClient(tz, availability=availability_response(
        cal_event("2026-09-17T14:30:00", "2026-09-17T15:30:00"),
    ))
    busy = av._get_availability_events(client, "someone@example.com", DAY, DAY, tz)

    check("one busy period", len(busy), 1)
    check("start is left where the server put it", busy[0]["start"],
          dt.datetime(2026, 9, 17, 14, 30))
    check("end is left where the server put it", busy[0]["end"],
          dt.datetime(2026, 9, 17, 15, 30))


def test_legacy_offset_bearing_times_are_converted() -> None:
    """The other half of the same rule: a value that *does* carry an offset is a
    real instant whatever the TimeZoneContext said, so it must be converted.
    Same 14:30 local meeting, expressed the other way."""
    tz = local_tz()
    client = FakeClient(tz, availability=availability_response(
        cal_event("2026-09-17T12:30:00Z", "2026-09-17T13:30:00Z"),
    ))
    busy = av._get_availability_events(client, "someone@example.com", DAY, DAY, tz)

    check("one busy period", len(busy), 1)
    check("12:30Z became 14:30 local", busy[0]["start"], dt.datetime(2026, 9, 17, 14, 30))
    check("13:30Z became 15:30 local", busy[0]["end"], dt.datetime(2026, 9, 17, 15, 30))


def test_legacy_free_and_nodata_are_skipped() -> None:
    """A Free/NoData block is not a busy period. Asserted alongside the frame
    tests because both are read off the same loop, and a conversion change is
    exactly when a filter gets moved by accident."""
    tz = local_tz()
    client = FakeClient(tz, availability=availability_response(
        cal_event("2026-09-17T09:00:00", "2026-09-17T10:00:00", busy="Free"),
        cal_event("2026-09-17T10:00:00", "2026-09-17T11:00:00", busy="NoData"),
        cal_event("2026-09-17T14:30:00", "2026-09-17T15:30:00", busy="OOF"),
    ))
    busy = av._get_availability_events(client, "someone@example.com", DAY, DAY, tz)
    check("only the non-free block survives", [b["status"] for b in busy], ["OOF"])


def test_legacy_on_a_utc_wire_converts_and_shifts_the_window() -> None:
    """The degraded configuration: no Windows id known, so "UTC" goes on the wire
    and the conversions stop being identities. The window has to shift with it —
    local midnight is 22:00Z the previous day in W. Europe summer — or the
    request would cover a different day than the caller asked for."""
    tz = utc_wire_tz()
    client = FakeClient(tz, availability=availability_response(
        cal_event("2026-09-17T12:30:00", "2026-09-17T13:30:00"),
    ))
    busy = av._get_availability_events(client, "someone@example.com", DAY, DAY, tz)

    payload = client.requests[0][1]
    check("TimeZoneContext is UTC", tz_context_id(payload), "UTC")
    check("window is the local day expressed in UTC", time_window(payload),
          ("2026-09-16T22:00:00", "2026-09-17T22:00:00"))
    check("an unqualified time is now wire wall clock == UTC, so converted",
          busy[0]["start"], dt.datetime(2026, 9, 17, 14, 30))


# ------------------------------------------------------------------
# The GetSchedule branch, for contrast
# ------------------------------------------------------------------

def test_schedule_branch_sends_the_tz_and_converts_from_utc() -> None:
    """The modern path, asserted next to the legacy one because the whole point
    of the three input-named conversions is that these two branches do *not*
    convert the same way: `scheduleItems` are UTC instants regardless of the
    requested timezone."""
    tz = local_tz()
    client = FakeClient(tz, bearer=True, schedule=[{
        "email": "someone@example.com",
        "availability_view": "",
        "error": None,
        "events": [{
            "start": dt.datetime(2026, 9, 17, 12, 30),   # naive UTC, per _parse_schedule_dt
            "end": dt.datetime(2026, 9, 17, 13, 30),
            "subject": "", "status": "Busy", "is_recurring": False,
        }],
    }])
    busy = av._get_availability_events(client, "someone@example.com", DAY, DAY, tz)

    check("no legacy request was made", client.requests, [])
    call = client.schedule_calls[0]
    check("tz_id is the mailbox's own zone", call["tz_id"], W_EUROPE)
    check("window start is local midnight", call["start"], dt.datetime(2026, 9, 17, 0, 0))
    check("window end is the next local midnight", call["end"],
          dt.datetime(2026, 9, 18, 0, 0))
    check("12:30Z became 14:30 local", busy[0]["start"], dt.datetime(2026, 9, 17, 14, 30))


# ------------------------------------------------------------------
# find_meeting_time end-to-end: the MergedFreeBusy base (#602)
# ------------------------------------------------------------------

def freebusy_string(busy_ranges: list[tuple]) -> str:
    """A 48-character MergedFreeBusy for one day at 30-minute intervals.

    Index 0 is the start of the *requested window* expressed in the *requested*
    timezone — which is what the base passed to `_parse_freebusy_string` has to
    agree with. `busy_ranges` are (start_hour_float, end_hour_float) in that
    same frame.
    """
    chars = ["0"] * 48
    for start_h, end_h in busy_ranges:
        for i in range(int(start_h * 2), int(end_h * 2)):
            chars[i] = "2"
    return "".join(chars)


def run_find_meeting_time(tz, merged: str) -> dict:
    client = FakeClient(tz, availability=merged_freebusy_response(merged))
    out = av.find_meeting_time(
        emails="someone@example.com", start_date="2026-09-17",
        ctx=fake_ctx(client),
    )
    return json.loads(out), client


def test_find_meeting_time_maps_index_zero_to_local_midnight() -> None:
    """#602 end-to-end. A busy run at indices 29-31 is 14:30-16:00 *if and only
    if* index 0 is local midnight; if the base were read as anything else the
    free slots would come out shifted by that difference, which is precisely the
    bug that shipped."""
    tz = local_tz()
    result, client = run_find_meeting_time(tz, freebusy_string([(14.5, 16.0)]))

    check("TimeZoneContext is the mailbox's own zone",
          tz_context_id(client.requests[0][1]), W_EUROPE)
    check("free slots straddle the 14:30-16:00 block",
          result["free_slots"]["2026-09-17"],
          [{"start": "09:00", "end": "14:30", "duration_minutes": 330},
           {"start": "16:00", "end": "18:00", "duration_minutes": 120}])


def test_the_same_freebusy_string_yields_the_same_local_slots() -> None:
    """The invariant that ties the wire window to the parsing base.

    In the `mailbox_configuration` case index 0 is local midnight; in the
    `host_local` case we asked for the same instant expressed as 22:00Z, so
    index 0 is *still* local midnight. The two must therefore produce identical
    free slots from an identical string. When they don't, the window we send and
    the base we parse against have drifted apart — the #602 failure mode, and
    not something either configuration's own test would catch alone.
    """
    local_result, local_client = run_find_meeting_time(
        local_tz(), freebusy_string([(14.5, 16.0)]))
    utc_result, utc_client = run_find_meeting_time(
        utc_wire_tz(), freebusy_string([(14.5, 16.0)]))

    check("the two configurations really did send different windows",
          time_window(local_client.requests[0][1])
          != time_window(utc_client.requests[0][1]), True)
    check("...but agree on the local free slots",
          utc_result["free_slots"], local_result["free_slots"])


def test_find_meeting_time_reports_the_timezone_block() -> None:
    """The frame has to be answerable from the response, not inferred from the
    source — a two-hour shift was silent once already."""
    tz = local_tz()
    result, _ = run_find_meeting_time(tz, freebusy_string([(14.5, 16.0)]))
    block = result.get("timezone", {})
    check("source is reported", block.get("source"), "mailbox_configuration")
    check("offset is the one applied", block.get("utc_offset"), "+02:00")
    check("windows id is reported", block.get("windows_id"), W_EUROPE)


def test_find_meeting_time_reports_both_failures_not_just_the_last() -> None:
    """A whole-operation GetSchedule failure used to crash `get_schedule` with
    `AttributeError: 'NoneType' object has no attribute 'get'`. Now it raises
    with the reason — and the legacy action is tried next, so the reason must
    survive into the error rather than being replaced by
    GetUserAvailability's unrelated NotImplementedException."""
    tz = local_tz()
    client = FakeClient(
        tz,
        schedule_raises=RuntimeError("GetSchedule returned no data: mailbox not found"),
        availability={"Body": {"ErrorCode": 500, "ExceptionName": "NotImplementedException",
                               "FaultMessage": None}},
    )
    result = json.loads(av.find_meeting_time(
        emails="someone@example.com", start_date="2026-09-17", ctx=fake_ctx(client)))

    error = result.get("error", "")
    check("the legacy failure is named", "NotImplementedException" in error, True)
    check("the GetSchedule reason survives", "mailbox not found" in error, True)


def test_find_meeting_time_lets_an_authentication_error_through() -> None:
    """`AuthenticationRequiredError` is a plain `Exception`, so the RuntimeError
    handler must not catch it: "a human must sign in" is not "this attendee has
    no free/busy", and demoting it would answer a whole-day free slot for a
    mailbox nobody can read."""
    tz = local_tz()
    client = FakeClient(tz, schedule_raises=AuthenticationRequiredError("signed out"))
    try:
        av.find_meeting_time(emails="someone@example.com", start_date="2026-09-17",
                             ctx=fake_ctx(client))
    except AuthenticationRequiredError:
        return
    except Exception as exc:  # noqa: BLE001
        FAILURES.append(f"authentication error became {type(exc).__name__}: {exc}")
        return
    FAILURES.append("an authentication error was swallowed into a normal result")


# ------------------------------------------------------------------
# analytics.py's legacy branch
# ------------------------------------------------------------------

def test_analytics_event_date_uses_the_mailbox_day() -> None:
    """Day granularity is still a frame question: 22:30Z is 00:30 *the next day*
    in W. Europe summer, so the old `raw[:10]` filed a meeting under the wrong
    date. Both spellings of the same instant must land on the same day."""
    tz = local_tz()
    check("an offset-bearing late-evening instant rolls over",
          an._event_date(tz, "2026-09-16T22:30:00Z"), "2026-09-17")
    check("an unqualified time is already local and does not move",
          an._event_date(tz, "2026-09-17T00:30:00"), "2026-09-17")
    check("an unparseable value degrades to the old slice rather than vanishing",
          an._event_date(tz, "not-a-timestamp"), "not-a-time")


def test_analytics_legacy_sends_the_mailbox_timezone_and_local_window() -> None:
    """Same two halves as availability.py's legacy branch, in the other module —
    they are separate code paths with separately hardcoded timezone ids, which
    is how the stray UTC+3 reached five modules to begin with."""
    tz = local_tz()
    client = FakeClient(tz, availability=availability_response(
        cal_event("2026-09-17T14:30:00", "2026-09-17T15:30:00", subject="Standup"),
    ))
    results, errors = an._get_availability_events(
        client, ["someone@example.com"], DAY, dt.date(2026, 9, 18))

    check("no errors", errors, [])
    payload = client.requests[0][1]
    check("TimeZoneContext is the mailbox's own zone", tz_context_id(payload), W_EUROPE)
    check("window starts at local midnight", time_window(payload)[0],
          "2026-09-17T00:00:00")
    check("the event is dated in the mailbox's day",
          [e["start_date"] for e in results["someone@example.com"]], ["2026-09-17"])
    check("the subject survives",
          [e["subject"] for e in results["someone@example.com"]], ["Standup"])


def test_analytics_records_a_schedule_failure_as_a_warning() -> None:
    """This helper's whole contract is that a failed availability query becomes a
    reportable warning instead of looking like "no meetings" — and before the fix
    only its legacy branch upheld that, so a GetSchedule failure propagated out
    of get_meeting_stats as a 500."""
    tz = local_tz()
    client = FakeClient(
        tz,
        schedule_raises=RuntimeError("GetSchedule returned no data: gateway error"),
        availability=availability_response(),
    )
    results, errors = an._get_availability_events(
        client, ["someone@example.com"], DAY, dt.date(2026, 9, 18))

    check("the reason is recorded", any("gateway error" in e for e in errors), True)
    check("and it did not become a fabricated empty success",
          results["someone@example.com"], [])
    check("the legacy branch still ran (so the chunk advanced)",
          [a for a, _ in client.requests], ["GetUserAvailability"])


def test_analytics_does_not_swallow_an_authentication_error() -> None:
    """The narrow-exception rule, in the module where it was nearly lost: the
    handler added for the GetSchedule failure was briefly `except Exception`,
    which would have turned "sign in" into a per-batch warning and reported a
    confident zero meetings for an unreadable mailbox."""
    tz = local_tz()
    client = FakeClient(tz, schedule_raises=AuthenticationRequiredError("signed out"))
    try:
        an._get_availability_events(
            client, ["someone@example.com"], DAY, dt.date(2026, 9, 18))
    except AuthenticationRequiredError:
        return
    except Exception as exc:  # noqa: BLE001
        FAILURES.append(f"authentication error became {type(exc).__name__}: {exc}")
        return
    FAILURES.append("an authentication error was swallowed into the errors list")


def main() -> bool:
    if local_tz() is None:
        # Every test here needs a real UTC offset to assert against, and without
        # a timezone database `resolve_mailbox_timezone` degrades to the host's
        # zone -- which would make these assertions test the CI runner's
        # timezone rather than the code. Reported, not silently passed.
        print("test_availability_frames: FAILED - no timezone database available, "
              "so the mailbox timezone cannot be resolved (is `tzdata` installed?)")
        return False

    for test in (
        test_legacy_sends_the_mailbox_timezone_and_a_local_day_window,
        test_legacy_unqualified_times_are_not_converted_twice,
        test_legacy_offset_bearing_times_are_converted,
        test_legacy_free_and_nodata_are_skipped,
        test_legacy_on_a_utc_wire_converts_and_shifts_the_window,
        test_schedule_branch_sends_the_tz_and_converts_from_utc,
        test_find_meeting_time_maps_index_zero_to_local_midnight,
        test_the_same_freebusy_string_yields_the_same_local_slots,
        test_find_meeting_time_reports_the_timezone_block,
        test_find_meeting_time_reports_both_failures_not_just_the_last,
        test_find_meeting_time_lets_an_authentication_error_through,
        test_analytics_event_date_uses_the_mailbox_day,
        test_analytics_legacy_sends_the_mailbox_timezone_and_local_window,
        test_analytics_records_a_schedule_failure_as_a_warning,
        test_analytics_does_not_swallow_an_authentication_error,
    ):
        test()

    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for failure in FAILURES:
            print(f"  - {failure}")
        return False
    print("test_availability_frames: all checks passed")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
