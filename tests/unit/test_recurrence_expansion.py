"""Pure-logic tests for `_expand_recurrence_occurrences` — no mailbox, no browser.

Recurrence expansion is the one piece of calendar logic that is entirely
client-side arithmetic, so it is worth testing without a live mailbox: the smoke
suite can only assert "some plausible occurrences came back", whereas these
cases pin down exact dates, and pin down the *payload shape* this backend
actually sends.

That shape is not the plain-EWS JSON the docs describe, and the difference cost
a full debugging round the first time: the variant lives in a `__type` field
under fixed `RecurrencePattern`/`RecurrenceRange` wrapper keys, and range dates
arrive as date-plus-offset with no time (`"2024-01-09+01:00"`), which the shared
`parse_iso_datetime` rejects outright. Every payload below is either copied from
a real series in a live mailbox or built to that same shape.

Run:
    python -m tests.unit.test_recurrence_expansion
"""

import sys
from datetime import datetime

from exchange_mcp.tools.calendar import (
    _expand_recurrence_occurrences,
    _nth_weekday_of_month,
    _parse_recurrence_date,
)

FAILURES: list[str] = []


def check(label, got, want):
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
        FAILURES.append(label)


def dates(occurrences):
    return [d.strftime("%Y-%m-%d") for d in occurrences]


def pattern(ptype, **fields):
    return {"__type": f"{ptype}:#Exchange", **fields}


def rrange(rtype, **fields):
    return {"__type": f"{rtype}:#Exchange", **fields}


def test_date_parsing():
    print("date parsing (the formats this backend really sends)")
    check("date + positive offset", _parse_recurrence_date("2024-01-09+01:00"), datetime(2024, 1, 9))
    check("date + negative offset", _parse_recurrence_date("2024-01-09-05:00"), datetime(2024, 1, 9))
    check("bare date", _parse_recurrence_date("2024-01-09"), datetime(2024, 1, 9))
    check("full datetime", _parse_recurrence_date("2026-09-01T09:30:00"), datetime(2026, 9, 1, 9, 30))
    check("datetime + Z", _parse_recurrence_date("2026-09-01T09:30:00Z"), datetime(2026, 9, 1, 9, 30))
    for bad in ("", "nope", "09/01/2026"):
        try:
            _parse_recurrence_date(bad)
            check(f"rejects {bad!r}", "no error", "ValueError")
        except (ValueError, TypeError):
            check(f"rejects {bad!r}", "ValueError", "ValueError")


def test_nth_weekday():
    print("nth weekday of month (Wednesday=2, Friday=4)")
    check("1st Wed Feb 2024", _nth_weekday_of_month(2024, 2, 2, 0), 7)
    check("2nd Wed Oct 2024", _nth_weekday_of_month(2024, 10, 2, 1), 9)
    check("last Wed Oct 2024", _nth_weekday_of_month(2024, 10, 2, -1), 30)
    # A month with only four Fridays has no "5th Friday" -- EWS skips the month
    # rather than clamping back to the 4th.
    check("5th Fri Feb 2026 (absent)", _nth_weekday_of_month(2026, 2, 4, 4), None)
    check("last Fri Feb 2026", _nth_weekday_of_month(2026, 2, 4, -1), 27)


def test_weekly_real_payload():
    print("weekly (verbatim from a live series)")
    recurrence = {
        "RecurrencePattern": pattern("WeeklyRecurrence", Interval=1,
                                     DaysOfWeek="Tuesday Wednesday Thursday",
                                     FirstDayOfWeek="Monday"),
        "RecurrenceRange": rrange("EndDateRecurrence", StartDate="2024-01-09+01:00",
                                  EndDate="2024-01-13+01:00"),
    }
    got = _expand_recurrence_occurrences(recurrence, datetime(2024, 1, 1), datetime(2024, 2, 1))
    check("Tue/Wed/Thu within a 5-day range", dates(got),
          ["2024-01-09", "2024-01-10", "2024-01-11"])

    biweekly = {
        "RecurrencePattern": pattern("WeeklyRecurrence", Interval=2, DaysOfWeek="Monday"),
        "RecurrenceRange": rrange("NoEndRecurrence", StartDate="2026-09-07+02:00"),
    }
    got = _expand_recurrence_occurrences(biweekly, datetime(2026, 9, 1), datetime(2026, 10, 15))
    check("every other Monday", dates(got), ["2026-09-07", "2026-09-21", "2026-10-05"])


def test_numbered_and_window_bounds():
    print("ranges and window clipping")
    numbered = {
        "RecurrencePattern": pattern("DailyRecurrence", Interval=2),
        "RecurrenceRange": rrange("NumberedRecurrence", StartDate="2026-09-01+02:00",
                                  NumberOfOccurrences=5),
    }
    got = _expand_recurrence_occurrences(numbered, datetime(2026, 9, 1), datetime(2026, 10, 15))
    check("NumberOfOccurrences caps the series", dates(got),
          ["2026-09-01", "2026-09-03", "2026-09-05", "2026-09-07", "2026-09-09"])

    # NumberOfOccurrences counts from the series start, not from the window, so a
    # window that opens mid-series must not "restart" the count.
    got = _expand_recurrence_occurrences(numbered, datetime(2026, 9, 6), datetime(2026, 10, 15))
    check("count is series-relative, not window-relative", dates(got),
          ["2026-09-07", "2026-09-09"])

    daily = {
        "RecurrencePattern": pattern("DailyRecurrence", Interval=1),
        "RecurrenceRange": rrange("NoEndRecurrence", StartDate="2026-09-01"),
    }
    got = _expand_recurrence_occurrences(daily, datetime(2026, 9, 10), datetime(2026, 9, 13))
    check("end of window is exclusive", dates(got),
          ["2026-09-10", "2026-09-11", "2026-09-12"])


def test_absolute_monthly_clamping():
    print("absolute monthly day clamping")
    recurrence = {
        "RecurrencePattern": pattern("AbsoluteMonthlyRecurrence", Interval=1, DayOfMonth=31),
        "RecurrenceRange": rrange("NoEndRecurrence", StartDate="2026-09-30+02:00"),
    }
    got = _expand_recurrence_occurrences(recurrence, datetime(2026, 9, 1), datetime(2027, 1, 1))
    check("day 31 clamps to each month's length", dates(got),
          ["2026-09-30", "2026-10-31", "2026-11-30", "2026-12-31"])


def test_relative_monthly_real_payloads():
    print("relative monthly (verbatim from live series)")
    every_two = {
        "RecurrencePattern": pattern("RelativeMonthlyRecurrence", Interval=2,
                                     DaysOfWeek="Wednesday", DayOfWeekIndex="First"),
        "RecurrenceRange": rrange("EndDateRecurrence", StartDate="2024-02-07+01:00",
                                  EndDate="2024-12-31+01:00"),
    }
    got = _expand_recurrence_occurrences(every_two, datetime(2024, 1, 1), datetime(2025, 1, 1))
    check("first Wednesday, every 2 months", dates(got),
          ["2024-02-07", "2024-04-03", "2024-06-05",
           "2024-08-07", "2024-10-02", "2024-12-04"])

    monthly = {
        "RecurrencePattern": pattern("RelativeMonthlyRecurrence", Interval=1,
                                     DaysOfWeek="Wednesday", DayOfWeekIndex="Second"),
        "RecurrenceRange": rrange("EndDateRecurrence", StartDate="2024-10-09+02:00",
                                  EndDate="2025-04-30+02:00"),
    }
    got = _expand_recurrence_occurrences(monthly, datetime(2024, 1, 1), datetime(2026, 1, 1))
    check("second Wednesday, monthly", dates(got),
          ["2024-10-09", "2024-11-13", "2024-12-11",
           "2025-01-08", "2025-02-12", "2025-03-12", "2025-04-09"])

    last = {
        "RecurrencePattern": pattern("RelativeMonthlyRecurrence", Interval=1,
                                     DaysOfWeek="Friday", DayOfWeekIndex="Last"),
        "RecurrenceRange": rrange("NoEndRecurrence", StartDate="2026-01-30"),
    }
    got = _expand_recurrence_occurrences(last, datetime(2026, 1, 1), datetime(2026, 5, 1))
    check("last Friday of each month", dates(got),
          ["2026-01-30", "2026-02-27", "2026-03-27", "2026-04-24"])


def test_relative_yearly():
    print("relative yearly")
    recurrence = {
        "RecurrencePattern": pattern("RelativeYearlyRecurrence", Interval=1,
                                     DaysOfWeek="Thursday", DayOfWeekIndex="Third", Month=11),
        "RecurrenceRange": rrange("NoEndRecurrence", StartDate="2024-11-21+01:00"),
    }
    got = _expand_recurrence_occurrences(recurrence, datetime(2024, 1, 1), datetime(2028, 1, 1))
    check("third Thursday of November", dates(got),
          ["2024-11-21", "2025-11-20", "2026-11-19", "2027-11-18"])


def test_plain_ews_shape_still_accepted():
    print("plain-EWS shape (variant name as the key) still works")
    recurrence = {
        "WeeklyRecurrence": {"Interval": 1, "DaysOfWeek": "Tuesday"},
        "NoEndRecurrence": {"StartDate": "2026-09-01T09:00:00"},
    }
    got = _expand_recurrence_occurrences(recurrence, datetime(2026, 9, 1), datetime(2026, 9, 30))
    check("weekly Tuesdays", dates(got),
          ["2026-09-01", "2026-09-08", "2026-09-15", "2026-09-22", "2026-09-29"])


def test_degrades_instead_of_raising():
    print("unrecognised/malformed payloads degrade to no occurrences")
    window = (datetime(2026, 9, 1), datetime(2026, 10, 1))
    cases = {
        "empty dict": {},
        "unknown pattern type": {
            "RecurrencePattern": pattern("FooRecurrence"),
            "RecurrenceRange": rrange("NoEndRecurrence", StartDate="2026-09-01")},
        "missing range": {"RecurrencePattern": pattern("DailyRecurrence", Interval=1)},
        "null pattern": {"RecurrencePattern": None,
                         "RecurrenceRange": rrange("NoEndRecurrence", StartDate="2026-09-01")},
        "empty pattern": {"RecurrencePattern": {},
                          "RecurrenceRange": rrange("NoEndRecurrence", StartDate="2026-09-01")},
        "unparseable StartDate": {
            "RecurrencePattern": pattern("DailyRecurrence", Interval=1),
            "RecurrenceRange": rrange("NoEndRecurrence", StartDate="nope")},
        "weekly with no days": {
            "RecurrencePattern": pattern("WeeklyRecurrence", Interval=1, DaysOfWeek=""),
            "RecurrenceRange": rrange("NoEndRecurrence", StartDate="2026-09-01")},
        "zero occurrences": {
            "RecurrencePattern": pattern("DailyRecurrence", Interval=1),
            "RecurrenceRange": rrange("NumberedRecurrence", StartDate="2026-09-01",
                                      NumberOfOccurrences=0)},
        # EWS pseudo-days mean something other than a plain weekday; guessing
        # would silently invent wrong dates, so they degrade instead.
        "pseudo-day 'Weekday'": {
            "RecurrencePattern": pattern("RelativeMonthlyRecurrence", Interval=1,
                                         DaysOfWeek="Weekday", DayOfWeekIndex="First"),
            "RecurrenceRange": rrange("NoEndRecurrence", StartDate="2026-09-01")},
        "out-of-range DayOfWeekIndex": {
            "RecurrencePattern": pattern("RelativeMonthlyRecurrence", Interval=1,
                                         DaysOfWeek="Monday", DayOfWeekIndex="Fifth"),
            "RecurrenceRange": rrange("NoEndRecurrence", StartDate="2026-09-01")},
        "yearly with month 13": {
            "RecurrencePattern": pattern("RelativeYearlyRecurrence", Interval=1,
                                         DaysOfWeek="Monday", DayOfWeekIndex="First", Month=13),
            "RecurrenceRange": rrange("NoEndRecurrence", StartDate="2026-09-01")},
    }
    for label, recurrence in cases.items():
        check(label, _expand_recurrence_occurrences(recurrence, *window), [])


def test_unbounded_series_is_capped():
    print("an unbounded series is bounded, not hung")
    for ptype, fields in (("DailyRecurrence", {"Interval": 1}),
                          ("RelativeMonthlyRecurrence",
                           {"Interval": 1, "DaysOfWeek": "Monday", "DayOfWeekIndex": "Last"})):
        recurrence = {
            "RecurrencePattern": pattern(ptype, **fields),
            "RecurrenceRange": rrange("NoEndRecurrence", StartDate="2000-01-01"),
        }
        got = _expand_recurrence_occurrences(recurrence, datetime(2000, 1, 1), datetime(2400, 1, 1))
        check(f"{ptype} over 400 years stays bounded", len(got) <= 2000, True)


def main() -> bool:
    for test in (test_date_parsing, test_nth_weekday, test_weekly_real_payload,
                 test_numbered_and_window_bounds, test_absolute_monthly_clamping,
                 test_relative_monthly_real_payloads, test_relative_yearly,
                 test_plain_ews_shape_still_accepted, test_degrades_instead_of_raising,
                 test_unbounded_series_is_capped):
        test()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        return False
    print("all recurrence expansion tests passed")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
