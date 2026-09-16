"""Pure-logic tests for `exchange_mcp/mailbox_timezone.py` — the mailbox's
timezone, and the one place a UTC instant becomes a wall-clock number. No
mailbox, no browser, no EXCHANGE_OWA_URL: the module imports nothing from the
package and its only outside dependency is `zoneinfo`.

What these tests are defending, in order of how expensive the failure is:

1. **The reported bug, as a number.** `find_free_time` (#601) reported free
   slots shifted by the mailbox's UTC offset — two hours for a W. Europe
   mailbox in DST — because naive-UTC busy periods were subtracted from a
   working-day window built out of local `start_hour`/`end_hour`. The live
   before/after case is pinned as an arithmetic test
   (`test_reported_bug_offsets_are_the_measured_ones`) so the fix cannot regress
   into a rounding-shaped no-op, and so the *summer/winter* pair is checked
   rather than one date: a fixed `+2` would pass a single-date test and be
   wrong for four months of the year.

2. **The frame contract between the three `from_*` entry points.** These exist
   as three functions because the data genuinely arrives in three frames
   (`GetSchedule`'s `scheduleItems` UTC, its `availabilityView` wall-clock in
   the requested timezone, and EWS timestamps whichever way the request asked
   for). #602 was the two being merged untouched. So the tests assert the
   *relationships* — `from_wire_wallclock` is the identity exactly when the
   wire timezone is the mailbox's own, `from_utc`/`to_utc` round-trip,
   `from_wire_timestamp` picks its branch off `tzinfo` and not off a guess —
   rather than only spot-checking values.

3. **Every degradation is reachable, labelled, and non-fatal.** A timezone
   probe that fails must not take `find_free_time` offline: it was
   useful-but-shifted before the fix, and useful-with-a-warning is strictly
   better than an exception. So `resolve_mailbox_timezone` is asserted to
   return a usable object for garbage, for an unknown id, and for nothing at
   all, and to say which in `source` — the field the tools now report, because
   a two-hour shift that was silent once must not be able to go silent again.

4. **The parser survives a shape change.** `GetOwaUserConfiguration` nests
   differently on the classic and modern backends, and a probe that misses
   because the nesting moved is indistinguishable downstream from a mailbox
   with no timezone set. Hence the recursive walk, and hence tests that feed it
   both shapes plus a display-name value (the one thing a `TimeZone` key
   legitimately holds that is *not* an id).

Deliberately not asserted here: that `WINDOWS_TO_IANA` is complete. It is a
CLDR snapshot, a missing row degrades to a warned `host_local` fallback rather
than a wrong offset, and pinning its size would make adding a row a test edit.
What *is* asserted is that every value in it resolves — a typo'd IANA name is
the failure mode that turns a known timezone into a silent fallback.

Run:
    python -m tests.unit.test_mailbox_timezone
"""

import datetime as dt
import sys

from exchange_mcp.mailbox_timezone import (
    SOURCE_HOST,
    SOURCE_MAILBOX,
    SOURCE_UTC,
    UTC_TIMEZONE,
    WINDOWS_TO_IANA,
    MailboxTimezone,
    find_timezone_candidates,
    parse_mailbox_timezone_id,
    resolve_mailbox_timezone,
)

FAILURES: list[str] = []


def check(label: str, actual, expected) -> None:
    if actual != expected:
        FAILURES.append(f"{label}: expected {expected!r}, got {actual!r}")


# The mailbox the bug was reported and measured on.
W_EUROPE = "W. Europe Standard Time"


# ------------------------------------------------------------------
# 1. The reported bug, as arithmetic
# ------------------------------------------------------------------

def test_reported_bug_offsets_are_the_measured_ones() -> None:
    """The live before/after from 2026-09-16, pinned.

    Three meetings whose UTC instants `get_calendar_events` reported as
    12:30Z/13:00Z/15:00Z were, in the mailbox, 14:30/15:00/17:00 — and
    `find_free_time` was subtracting the UTC numbers from a 09:00-18:00 local
    window. Both DST states are checked: the shift is +2 in September and +1 in
    January, so a constant would be wrong for a third of the year.
    """
    tz = resolve_mailbox_timezone(W_EUROPE)
    check("W. Europe resolves from the mailbox configuration", tz.source, SOURCE_MAILBOX)

    for utc_hhmm, local_hhmm in (("12:30", "14:30"), ("13:00", "15:00"), ("15:00", "17:00")):
        hour, minute = (int(part) for part in utc_hhmm.split(":"))
        local = tz.from_utc(dt.datetime(2026, 9, 17, hour, minute))
        check(
            f"summer: {utc_hhmm}Z is {local_hhmm} local",
            local,
            dt.datetime(2026, 9, 17, *(int(p) for p in local_hhmm.split(":"))),
        )

    check(
        "summer offset is +2 (DST)",
        tz.utc_offset(dt.datetime(2026, 9, 17, 12, 0)),
        dt.timedelta(hours=2),
    )
    check(
        "winter offset is +1 (standard time)",
        tz.utc_offset(dt.datetime(2026, 1, 17, 12, 0)),
        dt.timedelta(hours=1),
    )
    check(
        "winter: 12:30Z is 13:30 local",
        tz.from_utc(dt.datetime(2026, 1, 17, 12, 30)),
        dt.datetime(2026, 1, 17, 13, 30),
    )


def test_a_late_evening_event_changes_date_not_just_time() -> None:
    """Why analytics (#701/#702) needed the same conversion for a *date*.

    A 00:30 local meeting is 22:30 UTC the day before, so a day-granularity
    counter that reads the date off the unconverted instant files it under the
    wrong day — and a range filter drops it off the first day of the window.
    """
    tz = resolve_mailbox_timezone(W_EUROPE)
    local = tz.from_utc(dt.datetime(2026, 9, 16, 22, 30))
    check("22:30Z is 00:30 the next day", local, dt.datetime(2026, 9, 17, 0, 30))
    check("...and so lands on a different date", local.date(), dt.date(2026, 9, 17))


# ------------------------------------------------------------------
# 2. The frame contract
# ------------------------------------------------------------------

def test_wire_id_is_the_mailbox_zone_when_known_and_utc_otherwise() -> None:
    """`wire_id` is what goes on the wire, and it is never empty — a caller
    passes it straight into `tz_id`/`TimeZoneContext` without a fallback of its
    own, which is the point of the property existing."""
    known = resolve_mailbox_timezone(W_EUROPE)
    check("known mailbox zone is sent as-is", known.wire_id, W_EUROPE)
    check("...so the wire is not UTC", known.wire_is_utc, False)

    for degraded in (resolve_mailbox_timezone(""), resolve_mailbox_timezone("Nonsense/Zone")):
        check(f"{degraded.source}: wire id is UTC", degraded.wire_id, "UTC")
        check(f"{degraded.source}: wire id is non-empty", bool(degraded.wire_id), True)

    check("the UTC floor still names a wire id", UTC_TIMEZONE.wire_id, "UTC")


def test_from_wire_wallclock_is_identity_exactly_when_the_wire_is_local() -> None:
    """The property the fix leans on: when the mailbox timezone is known we ask
    the server for it, so `availabilityView` arrives already in the frame we
    want and this conversion does nothing. That means the path that used to be
    wrong now performs *no* arithmetic — there is nothing left to get wrong."""
    sample = dt.datetime(2026, 9, 17, 9, 0)

    known = resolve_mailbox_timezone(W_EUROPE)
    check("known: from_wire_wallclock is identity", known.from_wire_wallclock(sample), sample)
    check("known: to_wire_wallclock is identity", known.to_wire_wallclock(sample), sample)

    # And when we did have to ask for UTC, it is emphatically not the identity.
    utc_wire = MailboxTimezone(
        source=SOURCE_MAILBOX,
        windows_id="",  # empty -> wire_id falls through to UTC
        iana_id="Europe/Berlin",
        tz=resolve_mailbox_timezone(W_EUROPE).tz,
    )
    check("utc wire: is flagged as such", utc_wire.wire_is_utc, True)
    check(
        "utc wire: 09:00 wire wall clock is 11:00 local in summer",
        utc_wire.from_wire_wallclock(sample),
        dt.datetime(2026, 9, 17, 11, 0),
    )


def test_from_utc_and_to_utc_round_trip() -> None:
    """`to_utc` is used to express a local window on the wire and `from_utc` to
    read the answer back, so a discrepancy between them silently widens or
    narrows the queried window rather than erroring."""
    tz = resolve_mailbox_timezone(W_EUROPE)
    for moment in (
        dt.datetime(2026, 9, 17, 0, 0),
        dt.datetime(2026, 9, 17, 9, 30),
        dt.datetime(2026, 1, 17, 0, 0),
        dt.datetime(2026, 6, 30, 23, 59),
    ):
        check(f"round trip {moment}", tz.from_utc(tz.to_utc(moment)), moment)


def test_from_utc_normalises_an_aware_input() -> None:
    """EWS sometimes sends a real offset instead of a bare "Z". An aware value
    is an instant whatever zone it names, so it must convert the same as its
    UTC equivalent — not be read as if it were already local."""
    tz = resolve_mailbox_timezone(W_EUROPE)
    expected = dt.datetime(2026, 9, 17, 14, 30)
    check(
        "naive UTC input",
        tz.from_utc(dt.datetime(2026, 9, 17, 12, 30)),
        expected,
    )
    check(
        "aware UTC input",
        tz.from_utc(dt.datetime(2026, 9, 17, 12, 30, tzinfo=dt.timezone.utc)),
        expected,
    )
    check(
        "aware non-UTC input naming the same instant",
        tz.from_utc(
            dt.datetime(2026, 9, 17, 15, 30, tzinfo=dt.timezone(dt.timedelta(hours=3)))
        ),
        expected,
    )


def test_from_wire_timestamp_branches_on_tzinfo() -> None:
    """The rule that stops the classic-OWA fallback double-converting: that
    request sends a `TimeZoneContext`, so an *unqualified* CalendarEvent time is
    already wire wall clock while an offset-bearing one is a UTC instant. The
    value itself decides, which is why this is checkable without the live
    tenant that answers NotImplementedException to the action."""
    tz = resolve_mailbox_timezone(W_EUROPE)

    naive = dt.datetime(2026, 9, 17, 14, 30)
    check(
        "naive is treated as wire wall clock (identity, wire == local here)",
        tz.from_wire_timestamp(naive),
        tz.from_wire_wallclock(naive),
    )
    check("...which for a known zone means unchanged", tz.from_wire_timestamp(naive), naive)

    aware = dt.datetime(2026, 9, 17, 12, 30, tzinfo=dt.timezone.utc)
    check(
        "aware is treated as an instant",
        tz.from_wire_timestamp(aware),
        tz.from_utc(aware),
    )
    check("...i.e. converted", tz.from_wire_timestamp(aware), dt.datetime(2026, 9, 17, 14, 30))


def test_none_passes_through_every_conversion() -> None:
    """Call sites already skip unparseable timestamps; a second guard at each
    conversion would be noise, and an AttributeError inside a busy-period loop
    would lose the whole day's events rather than the one bad row."""
    tz = resolve_mailbox_timezone(W_EUROPE)
    for name, fn in (
        ("from_utc", tz.from_utc),
        ("to_utc", tz.to_utc),
        ("from_wire_wallclock", tz.from_wire_wallclock),
        ("to_wire_wallclock", tz.to_wire_wallclock),
        ("from_wire_timestamp", tz.from_wire_timestamp),
    ):
        check(f"{name}(None) is None", fn(None), None)


def test_utc_source_is_exactly_the_pre_fix_behaviour() -> None:
    """The floor of the fallback chain is the identity, on purpose: it is what
    the tools did before this change. A fabricated offset would be worse than a
    documented absent one, so this path must not invent anything."""
    sample = dt.datetime(2026, 9, 17, 12, 30)
    check("from_utc is identity", UTC_TIMEZONE.from_utc(sample), sample)
    check("to_utc is identity", UTC_TIMEZONE.to_utc(sample), sample)
    check("from_wire_wallclock is identity", UTC_TIMEZONE.from_wire_wallclock(sample), sample)
    check("offset is zero", UTC_TIMEZONE.utc_offset(sample), dt.timedelta(0))
    check(
        "an aware input is stripped, not shifted",
        UTC_TIMEZONE.from_utc(sample.replace(tzinfo=dt.timezone(dt.timedelta(hours=5)))),
        sample,
    )


# ------------------------------------------------------------------
# 3. Degradation is reachable, labelled and non-fatal
# ------------------------------------------------------------------

def test_resolution_never_raises_and_always_returns_a_usable_object() -> None:
    """A timezone probe that fails must not be able to take `find_free_time`
    offline — see the module docstring. Every one of these inputs is something
    the probe can genuinely produce."""
    for label, value in (
        ("nothing found", ""),
        ("None", None),
        ("whitespace", "   "),
        ("unknown id", "Middle Of Nowhere Standard Time"),
        ("an IANA name Exchange would never send", "Europe/Rome"),
        ("a display name that slipped through", "(UTC+01:00) Amsterdam"),
    ):
        try:
            tz = resolve_mailbox_timezone(value)
        except Exception as exc:  # noqa: BLE001 - the assertion *is* "no exception"
            FAILURES.append(f"{label}: resolve raised {exc!r}")
            continue
        check(f"{label}: returns a MailboxTimezone", isinstance(tz, MailboxTimezone), True)
        check(f"{label}: source is a known one", tz.source in (SOURCE_MAILBOX, SOURCE_HOST, SOURCE_UTC), True)
        check(f"{label}: conversion works", tz.from_utc(dt.datetime(2026, 9, 17, 12, 0)) is not None, True)


def test_degraded_paths_carry_a_warning_and_a_falsifiable_source() -> None:
    """`source` and `warning` are what the tools report. The good path must not
    carry a warning (or every response would look degraded and the field would
    stop meaning anything), and the degraded ones must — naming the offending
    id where there was one, because that is the difference between "add a row
    to WINDOWS_TO_IANA" and "the probe found nothing"."""
    good = resolve_mailbox_timezone(W_EUROPE)
    check("known zone has no warning", good.warning, None)
    check("known zone reports its IANA name", good.iana_id, "Europe/Berlin")
    check("known zone reports its Windows id", good.windows_id, W_EUROPE)

    nothing = resolve_mailbox_timezone("")
    check("empty probe is a fallback", nothing.source in (SOURCE_HOST, SOURCE_UTC), True)
    check("empty probe warns", bool(nothing.warning), True)

    unknown = resolve_mailbox_timezone("Atlantis Standard Time")
    check("unknown id is a fallback", unknown.source in (SOURCE_HOST, SOURCE_UTC), True)
    check("unknown id is named in the warning", "Atlantis Standard Time" in (unknown.warning or ""), True)
    check(
        "unknown id points at the table to edit",
        "WINDOWS_TO_IANA" in (unknown.warning or ""),
        True,
    )


def test_describe_reports_the_frame_and_the_offset_actually_applied() -> None:
    """The `timezone` block the tools attach. It exists because this shift was
    silent for long enough that "which frame are these numbers in" has to be
    answerable from the response — so the offset is per-instant, and the note
    says local rather than leaving `%H:%M` to be guessed at."""
    tz = resolve_mailbox_timezone(W_EUROPE)

    summer = tz.describe(dt.datetime(2026, 9, 17, 12, 0))
    check("summer offset string", summer["utc_offset"], "+02:00")
    check("source is reported", summer["source"], SOURCE_MAILBOX)
    check("windows id is reported", summer["windows_id"], W_EUROPE)
    check("iana id is reported", summer["iana_id"], "Europe/Berlin")
    check("the note names the frame", "mailbox-local" in summer["note"], True)
    check("no warning key on the good path", "warning" in summer, False)

    # The note must tell the truth on the floor too: no conversion happened
    # there, so claiming "mailbox-local" would reintroduce the exact
    # misdescription this change is about, one field over.
    floor_note = UTC_TIMEZONE.describe(dt.datetime(2026, 9, 17, 12, 0))["note"]
    check("the utc floor's note says UTC", "UTC" in floor_note, True)
    check("...and does not claim local", "mailbox-local" in floor_note, False)

    winter = tz.describe(dt.datetime(2026, 1, 17, 12, 0))
    check("winter offset string differs", winter["utc_offset"], "+01:00")

    degraded = resolve_mailbox_timezone("Atlantis Standard Time").describe(
        dt.datetime(2026, 9, 17, 12, 0)
    )
    check("a fallback surfaces its warning", "warning" in degraded, True)

    # A negative offset must not render as "+-05:00" or "-05:-00".
    negative = resolve_mailbox_timezone("Eastern Standard Time")
    if negative.source == SOURCE_MAILBOX:
        check(
            "negative offsets render correctly",
            negative.describe(dt.datetime(2026, 1, 17, 12, 0))["utc_offset"],
            "-05:00",
        )

    # A half-hour zone must not lose its minutes to integer division.
    half = resolve_mailbox_timezone("India Standard Time")
    if half.source == SOURCE_MAILBOX:
        check(
            "sub-hour offsets keep their minutes",
            half.describe(dt.datetime(2026, 1, 17, 12, 0))["utc_offset"],
            "+05:30",
        )


# ------------------------------------------------------------------
# 4. Parsing GetOwaUserConfiguration
# ------------------------------------------------------------------

def test_parser_finds_the_id_wherever_the_backend_nests_it() -> None:
    """The walk is recursive because the response shape differs between the
    classic canary-cookie backend and the modern bearer one, and a probe that
    misses because the nesting moved looks exactly like a mailbox with no
    timezone set."""
    classic = {
        "Body": {
            "UserOptions": {"TimeZone": W_EUROPE, "WorkingHoursStartTime": 32400000},
            "GlobalFolderIds": [{"Id": "abc"}],
        }
    }
    check("classic UserOptions shape", parse_mailbox_timezone_id(classic), W_EUROPE)

    modern = {
        "Body": {
            "SessionSettings": {"UserTimeZoneId": "Romance Standard Time"},
        }
    }
    check("modern SessionSettings shape", parse_mailbox_timezone_id(modern), "Romance Standard Time")

    inside_a_list = {"Body": {"Options": [{"Irrelevant": 1}, {"TimeZoneKeyName": "Tokyo Standard Time"}]}}
    check("inside a list", parse_mailbox_timezone_id(inside_a_list), "Tokyo Standard Time")


def test_parser_returns_empty_when_there_is_nothing_to_find() -> None:
    """"" is the documented "probe found nothing" input to
    `resolve_mailbox_timezone`, so these must not become a spurious id."""
    for label, payload in (
        ("empty dict", {}),
        ("no timezone key", {"Body": {"UserOptions": {"Theme": "dark"}}}),
        ("a nested object under TimeZone", {"Body": {"TimeZone": {"Id": W_EUROPE}}}),
        ("an empty string value", {"Body": {"UserOptions": {"TimeZone": ""}}}),
        ("a non-string value", {"Body": {"UserOptions": {"TimeZone": 60}}}),
        ("not a dict at all", "GetOwaUserConfiguration is not available"),
        ("None", None),
    ):
        check(f"{label} yields no id", parse_mailbox_timezone_id(payload), "")


def test_parser_rejects_a_display_name() -> None:
    """A localised display name is the one thing a `TimeZone` key legitimately
    holds that is not an id. Letting it through would spend the mailbox's real
    timezone on a warning about an unmappable one."""
    payload = {
        "Body": {
            "UserOptions": {
                "TimeZone": "(UTC+01:00) Amsterdam, Berlin, Bern, Rome, Stockholm, Vienna",
            }
        }
    }
    check("display name is not an id", parse_mailbox_timezone_id(payload), "")


def test_parser_prefers_a_mappable_candidate_over_a_higher_ranked_unknown() -> None:
    """A response can carry more than one of these keys. Preferring the one this
    module can actually use beats preferring the one that happens to sort first
    — the alternative is a warned fallback while the real answer sat two keys
    away."""
    payload = {
        "Body": {
            "UserOptions": {"TimeZone": "Some Unmapped Zone"},
            "SessionSettings": {"TimeZoneId": W_EUROPE},
        }
    }
    check("mappable candidate wins", parse_mailbox_timezone_id(payload), W_EUROPE)

    # But with nothing mappable, the highest-ranked raw candidate is returned
    # rather than "", so the warning can name it.
    only_unknown = {"Body": {"UserOptions": {"TimeZone": "Some Unmapped Zone"}}}
    check(
        "an unmappable id is still returned, to be named in the warning",
        parse_mailbox_timezone_id(only_unknown),
        "Some Unmapped Zone",
    )


def test_candidates_are_deduplicated_and_ranked() -> None:
    """`find_timezone_candidates` is the walk's own contract, tested separately
    from the picking so a change to one doesn't need the other re-read."""
    payload = {
        "A": {"TimeZoneId": "Tokyo Standard Time"},
        "B": {"TimeZone": W_EUROPE},
        "C": {"TimeZone": W_EUROPE},  # same value twice
    }
    candidates = find_timezone_candidates(payload)
    check("deduplicated", len(candidates), 2)
    check("TimeZone outranks TimeZoneId", candidates[0], W_EUROPE)
    check("no candidates in an empty payload", find_timezone_candidates({}), ())


# ------------------------------------------------------------------
# The table itself
# ------------------------------------------------------------------

def test_every_mapped_zone_actually_resolves() -> None:
    """A typo'd IANA name is the failure mode that turns a *known* timezone into
    a silent `host_local` fallback — the table would look right and the offset
    would be the host's. Completeness is deliberately not asserted (see the
    module docstring); correctness of what is there is."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:  # pragma: no cover - stdlib since 3.9, floor is 3.10
        FAILURES.append("zoneinfo is unavailable, which this module requires")
        return

    unresolvable = []
    for windows_id, iana in sorted(WINDOWS_TO_IANA.items()):
        try:
            ZoneInfo(iana)
        except Exception:  # noqa: BLE001 - any failure means the row is unusable
            unresolvable.append(f"{windows_id} -> {iana}")

    if unresolvable and len(unresolvable) == len(WINDOWS_TO_IANA):
        # No timezone database at all: that is an environment fact, not a table
        # bug, and the module handles it by falling back with a warning.
        print(
            "  note: no timezone database available, skipping per-zone resolution "
            "(is `tzdata` installed?)"
        )
        return
    check("every mapped IANA zone resolves", unresolvable, [])


def test_the_zone_the_bug_was_found_on_is_mapped() -> None:
    """Named explicitly rather than left to the bulk check: this is the row the
    reported failure depended on, and "W. Europe" mapping to Europe/Berlin
    rather than a Europe/Amsterdam-shaped guess is the kind of detail a future
    tidy-up might 'correct'."""
    check("W. Europe is mapped", WINDOWS_TO_IANA.get(W_EUROPE), "Europe/Berlin")
    check("UTC maps to itself", WINDOWS_TO_IANA.get("UTC"), "UTC")
    check(
        "the old hardcoded default is still mappable (it is a real zone)",
        WINDOWS_TO_IANA.get("Russian Standard Time"),
        "Europe/Moscow",
    )


def main() -> bool:
    for test in (
        test_reported_bug_offsets_are_the_measured_ones,
        test_a_late_evening_event_changes_date_not_just_time,
        test_wire_id_is_the_mailbox_zone_when_known_and_utc_otherwise,
        test_from_wire_wallclock_is_identity_exactly_when_the_wire_is_local,
        test_from_utc_and_to_utc_round_trip,
        test_from_utc_normalises_an_aware_input,
        test_from_wire_timestamp_branches_on_tzinfo,
        test_none_passes_through_every_conversion,
        test_utc_source_is_exactly_the_pre_fix_behaviour,
        test_resolution_never_raises_and_always_returns_a_usable_object,
        test_degraded_paths_carry_a_warning_and_a_falsifiable_source,
        test_describe_reports_the_frame_and_the_offset_actually_applied,
        test_parser_finds_the_id_wherever_the_backend_nests_it,
        test_parser_returns_empty_when_there_is_nothing_to_find,
        test_parser_rejects_a_display_name,
        test_parser_prefers_a_mappable_candidate_over_a_higher_ranked_unknown,
        test_candidates_are_deduplicated_and_ranked,
        test_every_mapped_zone_actually_resolves,
        test_the_zone_the_bug_was_found_on_is_mapped,
    ):
        test()

    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for failure in FAILURES:
            print(f"  - {failure}")
        return False
    print("test_mailbox_timezone: all checks passed")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
