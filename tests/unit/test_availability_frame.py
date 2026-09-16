"""Pure-logic tests for the availability timezone frame — no mailbox, no browser.

Covers `exchange_mcp/availability_frame.py` and the two availability helpers
that depend on it: `_find_free_slots` (#601, #602) and `_parse_freebusy_string`
(#602).

The bug these pin down: `OWAClient.get_schedule`'s `events` are naive **UTC**
(`scheduleItems` timestamps arrive UTC-offset whatever `tz_id` is requested,
and `_parse_schedule_dt` strips them), while `_find_free_slots` builds its
working-hours window with `datetime.combine(date, time(hour=start_hour))` — a
naive **local wall clock**. Comparing them slid every busy period across the
grid by the mailbox's UTC offset, so a 09:00–10:00 CEST meeting was subtracted
from the 07:00 slot: `find_free_time` offered a booked hour and hid a free one.

Three properties are worth a suite of their own, because none of them is
reachable from a live smoke test:

- **The shift is invisible on a UTC mailbox**, which is exactly the case the
  existing smoke tests ran against once issue #8's fallback made the zone UTC.
  A test can pick the zone instead of inheriting it.
- **The other path must NOT be converted.** `availabilityView` is wall-clock in
  the requested zone, so double-converting it would introduce the same error in
  the opposite direction — and a smoke test that only checks the response shape
  cannot tell the two apart. `test_freebusy_string_needs_no_conversion` and
  `test_both_frames_agree_after_conversion` state the asymmetry as an
  executable claim.
- **Neither failure branch can be provoked here.** The Windows→IANA table and
  the missing-`tzdata` degradation are both platform-dependent; `resolve_zone`
  takes an injectable `loader` so both are tested deterministically on any
  machine, the same way `test_folder_resolution.py` fakes a transport.

Run:
    python -m tests.unit.test_availability_frame
"""

import sys
from datetime import datetime, timedelta, timezone, tzinfo

from exchange_mcp import availability_frame as frame
from exchange_mcp.owa_client import OWAClient
from exchange_mcp.tools.availability import (
    _find_free_slots,
    _merge_busy_periods,
    _parse_freebusy_string,
)

FAILURES: list[str] = []


def check(label: str, actual, expected) -> None:
    if actual != expected:
        FAILURES.append(f"{label}: expected {expected!r}, got {actual!r}")


def check_true(label: str, value) -> None:
    if not value:
        FAILURES.append(f"{label}: expected a truthy value, got {value!r}")


def check_in(label: str, needle: str, haystack: str) -> None:
    if needle not in haystack:
        FAILURES.append(f"{label}: expected {needle!r} in {haystack!r}")


class FakeCEST(tzinfo):
    """Europe/Rome without needing a tz database: +2h in summer, +1h in winter.

    A hand-rolled zone rather than `ZoneInfo("Europe/Rome")` so the conversion
    tests run identically on a machine with no IANA data — which is every stock
    Windows machine, and was this one before `tzdata` became a dependency. The
    transition months are deliberately crude (April–October); nothing here
    tests the tz database's own correctness, only that the conversion asks it
    per instant instead of applying one constant.
    """

    def utcoffset(self, dt):
        return timedelta(hours=2 if dt and 4 <= dt.month <= 10 else 1)

    def dst(self, dt):
        return timedelta(hours=1 if dt and 4 <= dt.month <= 10 else 0)

    def tzname(self, dt):
        return "CEST" if dt and 4 <= dt.month <= 10 else "CET"


SUMMER = frame.ZoneResolution(FakeCEST(), "W. Europe Standard Time", frame.SOURCE_MAILBOX)
UTC_ZONE = frame.ZoneResolution(timezone.utc, "UTC", frame.SOURCE_MAILBOX)
UNRESOLVED = frame.ZoneResolution(None, "Mars/Olympus", frame.SOURCE_MAILBOX, "no such zone")


class FakeClient:
    """Just enough OWAClient surface for `mailbox_zone` — a timezone id, or none.

    `has_method=False` models this branch's own repository state: the client
    predates issue #8 and has no `mailbox_timezone()` at all, which is the case
    the `EXCHANGE_TIMEZONE` bridge exists for.
    """

    def __init__(self, timezone_id=None, has_method=True, raises=False):
        self._timezone_id = timezone_id
        self._raises = raises
        if not has_method:
            # Shadow the method with nothing: getattr must not find it.
            self.mailbox_timezone = None

    def mailbox_timezone(self):  # noqa: F811 - deliberately shadowed above
        if self._raises:
            raise RuntimeError("configuration action faulted")
        return self._timezone_id


# ------------------------------------------------------------------
# The bug itself
# ------------------------------------------------------------------

def test_busy_period_lands_on_the_working_hours_grid() -> None:
    """A 09:00–10:00 CEST meeting must remove the 09:00 slot, not the 07:00 one."""
    day = datetime(2026, 7, 15).date()
    # What get_schedule returns for that meeting: naive UTC.
    events = [{"start": datetime(2026, 7, 15, 7, 0), "end": datetime(2026, 7, 15, 8, 0),
               "status": "Busy"}]

    unconverted = [(ev["start"], ev["end"]) for ev in events]
    before = _find_free_slots(unconverted, day, 9, 18, 30)
    # The pre-fix behaviour, kept as a check so the test fails if the bug is
    # ever reintroduced in a way that merely *looks* different: the meeting
    # falls entirely outside 09–18 and the whole day reads as free.
    check("pre-fix slots", [(s.strftime("%H:%M"), e.strftime("%H:%M")) for s, e in before],
          [("09:00", "18:00")])

    moved = frame.events_to_wall_clock(events, SUMMER)
    check("converted start", moved[0]["start"], datetime(2026, 7, 15, 9, 0))
    check("converted end", moved[0]["end"], datetime(2026, 7, 15, 10, 0))

    after = _find_free_slots([(ev["start"], ev["end"]) for ev in moved], day, 9, 18, 30)
    check("post-fix slots", [(s.strftime("%H:%M"), e.strftime("%H:%M")) for s, e in after],
          [("10:00", "18:00")])


def test_utc_mailbox_is_unaffected() -> None:
    """The same conversion is a no-op on a UTC mailbox — why the bug hid so long."""
    events = [{"start": datetime(2026, 7, 15, 7, 0), "end": datetime(2026, 7, 15, 8, 0)}]
    moved = frame.events_to_wall_clock(events, UTC_ZONE)
    check("utc start", moved[0]["start"], datetime(2026, 7, 15, 7, 0))
    check("utc shifts", UTC_ZONE.shifts, False)
    check("cest shifts", SUMMER.shifts, True)


def test_unresolved_zone_does_not_shift() -> None:
    """An unresolvable zone leaves the times exactly as they were, never guesses."""
    events = [{"start": datetime(2026, 7, 15, 7, 0), "end": datetime(2026, 7, 15, 8, 0)}]
    moved = frame.events_to_wall_clock(events, UNRESOLVED)
    check("unresolved start", moved[0]["start"], datetime(2026, 7, 15, 7, 0))
    check("unresolved shifts", UNRESOLVED.shifts, False)


def test_conversion_is_per_instant_not_a_constant() -> None:
    """A range spanning a DST change shifts by the offset in force on each side."""
    summer = frame.to_wall_clock(datetime(2026, 7, 15, 7, 0), SUMMER)
    winter = frame.to_wall_clock(datetime(2026, 1, 15, 7, 0), SUMMER)
    check("summer offset", summer.hour, 9)
    check("winter offset", winter.hour, 8)


def test_events_are_copied_not_mutated() -> None:
    """The caller's list comes straight out of OWAClient; converting twice would double the shift."""
    original = {"start": datetime(2026, 7, 15, 7, 0), "end": datetime(2026, 7, 15, 8, 0),
                "status": "Busy", "subject": "Standup"}
    events = [original]
    moved = frame.events_to_wall_clock(events, SUMMER)
    check("original untouched", original["start"], datetime(2026, 7, 15, 7, 0))
    check("other keys carried", moved[0]["subject"], "Standup")
    twice = frame.events_to_wall_clock(moved, SUMMER)
    check("a second conversion would double-shift", twice[0]["start"].hour, 11)


def test_local_date_attribution() -> None:
    """The analytics bug: a late meeting is counted on the right local day.

    23:30 CEST on the 15th is 21:30 UTC on the 15th, but 00:30 CEST on the
    16th is 22:30 UTC on the *15th* — so per-day and per-weekday counts were
    attributing it to the previous day (and, for a Monday, the previous week).
    """
    late = {"start": datetime(2026, 7, 15, 22, 30), "end": datetime(2026, 7, 15, 23, 30)}
    moved = frame.events_to_wall_clock([late], SUMMER)[0]
    check("utc date", late["start"].strftime("%Y-%m-%d"), "2026-07-15")
    check("local date", moved["start"].strftime("%Y-%m-%d"), "2026-07-16")


# ------------------------------------------------------------------
# The path that must NOT be converted
# ------------------------------------------------------------------

def test_freebusy_string_needs_no_conversion() -> None:
    """availabilityView is wall-clock already: laid out from local midnight, it lands right."""
    day = datetime(2026, 7, 15)
    # 48 half-hour slots; the 09:00-10:00 pair (indexes 18, 19) busy.
    view = "".join("2" if i in (18, 19) else "0" for i in range(48))
    busy = _parse_freebusy_string(view, day)
    check("busy pair count", len(busy), 2)
    check("first busy slot", busy[0][0], datetime(2026, 7, 15, 9, 0))

    slots = _find_free_slots(_merge_busy_periods(busy), day.date(), 9, 18, 30)
    check("view-derived slots", [(s.strftime("%H:%M"), e.strftime("%H:%M")) for s, e in slots],
          [("10:00", "18:00")])


def test_schedule_tz_id_asks_for_the_grid_it_compares_against() -> None:
    """The view can only be right if it was *requested* in the zone we compare in.

    Caught live on 2026-09-16: with the events converted to the mailbox's zone
    but the window still requested in the old hardcoded UTC+3,
    `find_free_time` (events) and `find_meeting_time` (view) answered two
    different grids for the same mailbox and the same day.
    """
    check("resolved zone is requested", frame.schedule_tz_id(SUMMER), "W. Europe Standard Time")
    check("utc is requested explicitly", frame.schedule_tz_id(UTC_ZONE), "UTC")
    # Unresolved: send nothing rather than an id we could not make sense of
    # locally, and let the call keep its own default.
    check("unresolved sends nothing", frame.schedule_tz_id(UNRESOLVED), None)
    check("unknown sends nothing",
          frame.schedule_tz_id(frame.ZoneResolution(None, "", frame.SOURCE_UNKNOWN, "w")), None)


def test_both_frames_agree_after_conversion() -> None:
    """The same meeting, seen through both surfaces, must produce the same answer.

    This is the asymmetry stated as an equation: the view says "slot 18",
    the schedule item says "07:00Z", and only the second one needs moving.
    A fix that converted both would break this test, which is precisely the
    mistake it exists to catch.
    """
    day = datetime(2026, 7, 15)
    view = "".join("2" if i in (18, 19) else "0" for i in range(48))
    from_view = _find_free_slots(_merge_busy_periods(_parse_freebusy_string(view, day)),
                                day.date(), 9, 18, 30)

    events = [{"start": datetime(2026, 7, 15, 7, 0), "end": datetime(2026, 7, 15, 8, 0)}]
    moved = frame.events_to_wall_clock(events, SUMMER)
    from_items = _find_free_slots([(ev["start"], ev["end"]) for ev in moved],
                                  day.date(), 9, 18, 30)
    check("both frames", from_items, from_view)


# ------------------------------------------------------------------
# Reading the frame off the wire
# ------------------------------------------------------------------

def test_wire_offset_decides_the_conversion() -> None:
    """An offset on the wire is converted; no offset means "already wall-clock"."""
    check("Z suffix converted",
          frame.wire_to_wall_clock("2026-07-15T07:00:00Z", SUMMER), datetime(2026, 7, 15, 9, 0))
    check("+00:00 converted",
          frame.wire_to_wall_clock("2026-07-15T07:00:00+00:00", SUMMER), datetime(2026, 7, 15, 9, 0))
    check("non-zero offset converted",
          frame.wire_to_wall_clock("2026-07-15T09:00:00+02:00", SUMMER), datetime(2026, 7, 15, 9, 0))
    # EWS CalendarEventArray: no suffix, already in the requested TimeZoneContext zone.
    check("naive left alone",
          frame.wire_to_wall_clock("2026-07-15T09:00:00", SUMMER), datetime(2026, 7, 15, 9, 0))
    check("unparseable", frame.wire_to_wall_clock("not a date", SUMMER), None)
    check("empty", frame.wire_to_wall_clock("", SUMMER), None)
    check("non-string", frame.wire_to_wall_clock(None, SUMMER), None)


def test_naive_utc_convention_normalises() -> None:
    """to_utc_naive keeps the *instant*, where stripping tzinfo kept the wall clock."""
    check("Z", frame.to_utc_naive("2026-07-15T07:00:00Z"), datetime(2026, 7, 15, 7, 0))
    check("offset normalised to UTC",
          frame.to_utc_naive("2026-07-15T09:00:00+02:00"), datetime(2026, 7, 15, 7, 0))
    # 7 fractional digits: .NET ticks, one more than fromisoformat accepts.
    check("dotnet ticks", frame.to_utc_naive("2026-07-15T07:00:00.1234567Z"),
          datetime(2026, 7, 15, 7, 0, 0, 123456))
    check("bad value", frame.to_utc_naive("2026-13-45"), None)


def test_parse_schedule_dt_still_returns_naive_utc() -> None:
    """OWAClient's documented convention is unchanged — every caller assumes it."""
    parsed = OWAClient._parse_schedule_dt({"dateTime": "2026-07-15T07:00:00.0000000Z"})
    check("naive", parsed.tzinfo, None)
    check("value", parsed, datetime(2026, 7, 15, 7, 0))
    check("no node", OWAClient._parse_schedule_dt(None), None)
    check("no dateTime", OWAClient._parse_schedule_dt({}), None)


# ------------------------------------------------------------------
# Resolving an id to a zone
# ------------------------------------------------------------------

def test_iana_key_mapping() -> None:
    check("windows id", frame.iana_key("W. Europe Standard Time"), "Europe/Berlin")
    check("the old hardcode", frame.iana_key("Russian Standard Time"), "Europe/Moscow")
    check("iana passthrough", frame.iana_key("Europe/Rome"), "Europe/Rome")
    check("utc", frame.iana_key("UTC"), "UTC")
    check("utc lowercase", frame.iana_key("utc"), "UTC")
    check("padded", frame.iana_key("  Romance Standard Time  "), "Europe/Paris")
    check("unknown", frame.iana_key("Mars Standard Time"), None)
    check("empty", frame.iana_key(""), None)
    check("non-string", frame.iana_key(42), None)
    # The POSIX sign inversion on the Etc/* keys is the kind of thing that is
    # silently wrong by a whole day, so it is asserted rather than trusted.
    check("UTC+12 inverts", frame.iana_key("UTC+12"), "Etc/GMT-12")
    check("Dateline", frame.iana_key("Dateline Standard Time"), "Etc/GMT+12")


def test_resolve_zone_success() -> None:
    """A mapped id resolves through the loader, and carries its source."""
    asked: list[str] = []

    def loader(key):
        asked.append(key)
        return FakeCEST()

    resolved = frame.resolve_zone("W. Europe Standard Time",
                                  source=frame.SOURCE_MAILBOX, loader=loader)
    check("loader key", asked, ["Europe/Berlin"])
    check("no warning", resolved.warning, "")
    check("source", resolved.source, frame.SOURCE_MAILBOX)
    check("id kept as asked", resolved.timezone_id, "W. Europe Standard Time")
    check("shifts", resolved.shifts, True)


def test_resolve_zone_utc_needs_no_database() -> None:
    """UTC — issue #8's own fallback — must resolve with no tz data at all."""
    def loader(key):
        raise AssertionError(f"the loader must not be consulted for UTC (got {key!r})")

    resolved = frame.resolve_zone("UTC", loader=loader)
    check("utc tz", resolved.tz, timezone.utc)
    check("utc no warning", resolved.warning, "")


def test_resolve_zone_failures_are_distinguishable() -> None:
    """The two ways this can fail need different fixes, so they read differently."""
    unmapped = frame.resolve_zone("Mars Standard Time", loader=lambda key: FakeCEST())
    check("unmapped tz", unmapped.tz, None)
    check_in("unmapped names the override", frame.ENV_VAR, unmapped.warning)
    check_in("unmapped names the value", "Mars Standard Time", unmapped.warning)

    def missing_data(key):
        raise KeyError(f"no tzdata for {key}")

    no_data = frame.resolve_zone("Europe/Rome", loader=missing_data)
    check("no-data tz", no_data.tz, None)
    check_in("no-data names the package", "tzdata", no_data.warning)
    check_in("no-data names the key", "Europe/Rome", no_data.warning)

    unknown = frame.resolve_zone("")
    check("empty tz", unknown.tz, None)
    check("empty source", unknown.source, frame.SOURCE_UNKNOWN)
    check_true("empty warns", unknown.warning)


def test_zone_from_client() -> None:
    """Precedence: the client's own resolution, else the EXCHANGE_TIMEZONE bridge."""
    resolved = frame.mailbox_zone(FakeClient("Europe/Rome"), loader=lambda key: FakeCEST())
    check("from mailbox", (resolved.timezone_id, resolved.source),
          ("Europe/Rome", frame.SOURCE_MAILBOX))

    # A client method that faults must degrade, never propagate: this is called
    # on the way to answering an availability query.
    faulted = frame.mailbox_zone(FakeClient(raises=True), loader=lambda key: FakeCEST())
    check("faulting client degrades", faulted.tz, None)
    check("faulting client source", faulted.source, frame.SOURCE_UNKNOWN)

    # Nothing anywhere: no shift, and a warning that says what to set.
    silent = frame.mailbox_zone(FakeClient(None), loader=lambda key: FakeCEST())
    check("no id, no shift", silent.tz, None)
    check_in("no id names the override", frame.ENV_VAR, silent.warning)


def test_env_bridge_for_a_client_without_the_method() -> None:
    """On a build predating issue #8 the override is the only source there is."""
    import os

    previous = os.environ.get(frame.ENV_VAR)
    os.environ[frame.ENV_VAR] = "Europe/Rome"
    try:
        resolved = frame.mailbox_zone(FakeClient(has_method=False), loader=lambda key: FakeCEST())
        check("from env", (resolved.timezone_id, resolved.source),
              ("Europe/Rome", frame.SOURCE_ENV))
    finally:
        if previous is None:
            del os.environ[frame.ENV_VAR]
        else:
            os.environ[frame.ENV_VAR] = previous


def test_as_dict_is_what_the_tools_report() -> None:
    check("clean", UTC_ZONE.as_dict(), {"id": "UTC", "source": frame.SOURCE_MAILBOX})
    check("warned", UNRESOLVED.as_dict(),
          {"id": "Mars/Olympus", "source": frame.SOURCE_MAILBOX, "warning": "no such zone"})
    check("unknown id", frame.ZoneResolution(None, "", frame.SOURCE_UNKNOWN, "w").as_dict(),
          {"id": "unknown", "source": frame.SOURCE_UNKNOWN, "warning": "w"})


# ------------------------------------------------------------------
# Real zone data, when it is installed
# ------------------------------------------------------------------

def test_real_zone_data_if_available() -> None:
    """With `tzdata` installed (a declared dependency), the mapping really loads.

    Reported rather than asserted when the data is absent: the point of the
    injectable loader is that this suite does not depend on the platform, and
    a hard failure here would make CI's outcome depend on which OS ran it.
    """
    resolved = frame.resolve_zone("W. Europe Standard Time")
    if resolved.tz is None:
        print(f"  note: no IANA tz data on this machine - {resolved.warning}")
        return
    summer = frame.to_wall_clock(datetime(2026, 7, 15, 7, 0), resolved)
    winter = frame.to_wall_clock(datetime(2026, 1, 15, 7, 0), resolved)
    check("real summer offset", summer.hour, 9)
    check("real winter offset", winter.hour, 8)


TESTS = [
    test_busy_period_lands_on_the_working_hours_grid,
    test_utc_mailbox_is_unaffected,
    test_unresolved_zone_does_not_shift,
    test_conversion_is_per_instant_not_a_constant,
    test_events_are_copied_not_mutated,
    test_local_date_attribution,
    test_freebusy_string_needs_no_conversion,
    test_schedule_tz_id_asks_for_the_grid_it_compares_against,
    test_both_frames_agree_after_conversion,
    test_wire_offset_decides_the_conversion,
    test_naive_utc_convention_normalises,
    test_parse_schedule_dt_still_returns_naive_utc,
    test_iana_key_mapping,
    test_resolve_zone_success,
    test_resolve_zone_utc_needs_no_database,
    test_resolve_zone_failures_are_distinguishable,
    test_zone_from_client,
    test_env_bridge_for_a_client_without_the_method,
    test_as_dict_is_what_the_tools_report,
    test_real_zone_data_if_available,
]


def main() -> bool:
    for test in TESTS:
        before = len(FAILURES)
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - report, don't abort the suite
            FAILURES.append(f"{test.__name__}: raised {type(exc).__name__}: {exc}")
        status = "PASS" if len(FAILURES) == before else "FAIL"
        print(f"  {status}  {test.__name__}")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed:")
        for failure in FAILURES:
            print(f"  - {failure}")
        return False
    print(f"All {len(TESTS)} availability-frame test(s) passed.")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
