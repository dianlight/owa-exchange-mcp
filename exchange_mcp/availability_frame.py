"""Which timezone an availability timestamp is in, and how to get it onto the
working-hours grid.

The availability tools compare two things that arrive in *different* frames and
never said so:

- `OWAClient.get_schedule()`'s `events` come from `scheduleItems`, whose
  timestamps arrive UTC-offset regardless of the requested `tz_id`, and are
  stripped to naive by `_parse_schedule_dt` -- i.e. naive **UTC**.
- `_find_free_slots()` builds its day window as
  `datetime.combine(date, time(hour=start_hour))` -- a naive **local wall
  clock** 9-to-18 window.

Comparing them directly slides every busy period across the working-hours grid
by the mailbox's offset from UTC: a 09:00-10:00 CEST meeting reads as
07:00-08:00 and is subtracted from the wrong slot, so `find_free_time` and
`find_meeting_time` offer time that is already booked and hide time that is
free. Two hours in summer for a Central European mailbox, and *zero* for a
mailbox on UTC -- which is why the bug survived a live smoke test that only
checked the response shape.

**Not every path is wrong, and that is the point of this module existing.**
`availabilityView` (the `0/1/2/3/4`-per-interval string) is documented and
confirmed to be wall-clock in the *requested* zone, so once that zone is the
mailbox's own (issue #8), the periods `_parse_freebusy_string()` lays out from
the caller's own `start_date` are already on the same grid as the working
hours. A fix that converted both frames would break the one that works. So:

- `scheduleItems`/`events`, and any wire timestamp that carries an offset, go
  through `to_wall_clock()`.
- `availabilityView`-derived periods, and any wire timestamp that carries no
  offset (EWS returns `CalendarEventArray` times in the request's
  `TimeZoneContext` zone, unsuffixed), are already wall-clock and must be left
  exactly as they are. `wire_to_wall_clock()` encodes that rule by *reading*
  the offset off the wire rather than assuming one, so neither path depends on
  a guess about the other.

Three deliberate design points:

- **The frame conversion happens in the tools, not in `OWAClient`.**
  `_parse_schedule_dt`'s naive-UTC return is, in its own words, "the
  established (if imprecise) convention every caller downstream already
  assumes" -- `analytics.py` and `availability.py` both read those dicts.
  Changing it would be a wider, less reviewable edit than converting at the
  two places that own a working-hours grid, and would leave the *other*
  frame (`availabilityView`) still undocumented in code.
- **Converting the events beats building the window in UTC.** The window is
  not the only local thing: the day bucketing (`str(current_date)`), the
  weekend skip (`weekday() < 5`) and the `HH:MM` output strings are all
  wall-clock too, and `_find_free_slots` is shared with the
  `availabilityView` path that is already wall-clock. Moving the events is
  one conversion at the edge; moving the window would be a conversion at
  every one of those points, plus a conversion back for output.
- **An unresolvable zone does not shift anything, and says so.** Same
  doctrine as `profile_lock.py`'s "only a positively-detected lock blocks":
  a fabricated offset is worse than the known-imprecise status quo, because
  a wrong-by-one-hour answer looks right. `ZoneResolution.warning` is
  non-empty exactly when the grid may still be off, and the tools report it
  in their own output next to the zone they used.

Timezone *ids* are the awkward part. Exchange speaks Windows ids
(`W. Europe Standard Time`); `zoneinfo` speaks IANA (`Europe/Rome`); the
modern backend has been observed handing IANA ids to its own web client. So
`_WINDOWS_TO_IANA` maps the former to the latter, an unmapped id degrades to
"not shifting" with a warning naming the `EXCHANGE_TIMEZONE` override (which
accepts an IANA id and therefore always resolves), and `UTC` is special-cased
to `datetime.timezone.utc` so the zero-shift case needs no tz database at
all. Add new spellings to the table, not to the resolver, and cover them in
`tests/unit/test_availability_frame.py`.

Imports only the standard library -- no Playwright, no transport -- so it is
unit-testable the way `auth_errors.py` and `profile_lock.py` are.
"""

import os
import re
from datetime import datetime, timezone, tzinfo
from typing import Any, Callable, NamedTuple

# Operator override, read only as a bridge: once issue #8 lands,
# `OWAClient.mailbox_timezone()` resolves `EXCHANGE_TIMEZONE` itself (with the
# same name and higher precedence than the mailbox's own zone) and this branch
# stops being reached. Kept because the availability grid is wrong *now*, and
# an operator whose mailbox zone can't be read needs one lever, not two.
ENV_VAR = "EXCHANGE_TIMEZONE"

# Where the id we resolved came from, reported by the tools so the frame stops
# being invisible the way it was while the two grids silently disagreed.
SOURCE_MAILBOX = "mailbox"
SOURCE_ENV = "env"
SOURCE_UNKNOWN = "unknown"

# Windows timezone id -> IANA key, sorted by Windows id so "is my zone here?"
# is one scan. From CLDR's windowsZones default-territory mappings; only the
# zone *identity* matters here, not the territory variants, because all we
# ever ask a resolved zone for is its UTC offset at an instant.
#
# The `Etc/GMT±N` keys invert their sign by design (POSIX, not ISO): a mailbox
# on `UTC+12` maps to `Etc/GMT-12`. Getting that backwards is a silent
# 24-hour-wide error, so those five entries are spelled out rather than
# derived.
_WINDOWS_TO_IANA = {
    "AUS Central Standard Time": "Australia/Darwin",
    "AUS Eastern Standard Time": "Australia/Sydney",
    "Afghanistan Standard Time": "Asia/Kabul",
    "Alaskan Standard Time": "America/Anchorage",
    "Aleutian Standard Time": "America/Adak",
    "Altai Standard Time": "Asia/Barnaul",
    "Arab Standard Time": "Asia/Riyadh",
    "Arabian Standard Time": "Asia/Dubai",
    "Arabic Standard Time": "Asia/Baghdad",
    "Argentina Standard Time": "America/Argentina/Buenos_Aires",
    "Astrakhan Standard Time": "Europe/Astrakhan",
    "Atlantic Standard Time": "America/Halifax",
    "Aus Central W. Standard Time": "Australia/Eucla",
    "Azerbaijan Standard Time": "Asia/Baku",
    "Azores Standard Time": "Atlantic/Azores",
    "Bahia Standard Time": "America/Bahia",
    "Bangladesh Standard Time": "Asia/Dhaka",
    "Belarus Standard Time": "Europe/Minsk",
    "Bougainville Standard Time": "Pacific/Bougainville",
    "Canada Central Standard Time": "America/Regina",
    "Cape Verde Standard Time": "Atlantic/Cape_Verde",
    "Caucasus Standard Time": "Asia/Yerevan",
    "Cen. Australia Standard Time": "Australia/Adelaide",
    "Central America Standard Time": "America/Guatemala",
    "Central Asia Standard Time": "Asia/Almaty",
    "Central Brazilian Standard Time": "America/Cuiaba",
    "Central Europe Standard Time": "Europe/Budapest",
    "Central European Standard Time": "Europe/Warsaw",
    "Central Pacific Standard Time": "Pacific/Guadalcanal",
    "Central Standard Time": "America/Chicago",
    "Central Standard Time (Mexico)": "America/Mexico_City",
    "Chatham Islands Standard Time": "Pacific/Chatham",
    "China Standard Time": "Asia/Shanghai",
    "Cuba Standard Time": "America/Havana",
    "Dateline Standard Time": "Etc/GMT+12",
    "E. Africa Standard Time": "Africa/Nairobi",
    "E. Australia Standard Time": "Australia/Brisbane",
    "E. Europe Standard Time": "Europe/Chisinau",
    "E. South America Standard Time": "America/Sao_Paulo",
    "Easter Island Standard Time": "Pacific/Easter",
    "Eastern Standard Time": "America/New_York",
    "Egypt Standard Time": "Africa/Cairo",
    "Ekaterinburg Standard Time": "Asia/Yekaterinburg",
    "FLE Standard Time": "Europe/Kiev",
    "Fiji Standard Time": "Pacific/Fiji",
    "GMT Standard Time": "Europe/London",
    "GTB Standard Time": "Europe/Bucharest",
    "Georgian Standard Time": "Asia/Tbilisi",
    "Greenland Standard Time": "America/Godthab",
    "Greenwich Standard Time": "Atlantic/Reykjavik",
    "Haiti Standard Time": "America/Port-au-Prince",
    "Hawaiian Standard Time": "Pacific/Honolulu",
    "India Standard Time": "Asia/Kolkata",
    "Iran Standard Time": "Asia/Tehran",
    "Israel Standard Time": "Asia/Jerusalem",
    "Jordan Standard Time": "Asia/Amman",
    "Kaliningrad Standard Time": "Europe/Kaliningrad",
    "Korea Standard Time": "Asia/Seoul",
    "Libya Standard Time": "Africa/Tripoli",
    "Line Islands Standard Time": "Pacific/Kiritimati",
    "Lord Howe Standard Time": "Australia/Lord_Howe",
    "Magadan Standard Time": "Asia/Magadan",
    "Magallanes Standard Time": "America/Punta_Arenas",
    "Marquesas Standard Time": "Pacific/Marquesas",
    "Mauritius Standard Time": "Indian/Mauritius",
    "Middle East Standard Time": "Asia/Beirut",
    "Montevideo Standard Time": "America/Montevideo",
    "Morocco Standard Time": "Africa/Casablanca",
    "Mountain Standard Time": "America/Denver",
    "Mountain Standard Time (Mexico)": "America/Chihuahua",
    "Myanmar Standard Time": "Asia/Yangon",
    "N. Central Asia Standard Time": "Asia/Novosibirsk",
    "Namibia Standard Time": "Africa/Windhoek",
    "Nepal Standard Time": "Asia/Kathmandu",
    "New Zealand Standard Time": "Pacific/Auckland",
    "Newfoundland Standard Time": "America/St_Johns",
    "Norfolk Standard Time": "Pacific/Norfolk",
    "North Asia East Standard Time": "Asia/Irkutsk",
    "North Asia Standard Time": "Asia/Krasnoyarsk",
    "North Korea Standard Time": "Asia/Pyongyang",
    "Omsk Standard Time": "Asia/Omsk",
    "Pacific SA Standard Time": "America/Santiago",
    "Pacific Standard Time": "America/Los_Angeles",
    "Pacific Standard Time (Mexico)": "America/Tijuana",
    "Pakistan Standard Time": "Asia/Karachi",
    "Paraguay Standard Time": "America/Asuncion",
    "Qyzylorda Standard Time": "Asia/Qyzylorda",
    "Romance Standard Time": "Europe/Paris",
    "Russia Time Zone 10": "Asia/Srednekolymsk",
    "Russia Time Zone 11": "Asia/Kamchatka",
    "Russia Time Zone 3": "Europe/Samara",
    "Russian Standard Time": "Europe/Moscow",
    "SA Eastern Standard Time": "America/Cayenne",
    "SA Pacific Standard Time": "America/Bogota",
    "SA Western Standard Time": "America/La_Paz",
    "SE Asia Standard Time": "Asia/Bangkok",
    "Saint Pierre Standard Time": "America/Miquelon",
    "Sakhalin Standard Time": "Asia/Sakhalin",
    "Samoa Standard Time": "Pacific/Apia",
    "Sao Tome Standard Time": "Africa/Sao_Tome",
    "Saratov Standard Time": "Europe/Saratov",
    "Singapore Standard Time": "Asia/Singapore",
    "South Africa Standard Time": "Africa/Johannesburg",
    "Sri Lanka Standard Time": "Asia/Colombo",
    "Sudan Standard Time": "Africa/Khartoum",
    "Syria Standard Time": "Asia/Damascus",
    "Taipei Standard Time": "Asia/Taipei",
    "Tasmania Standard Time": "Australia/Hobart",
    "Tocantins Standard Time": "America/Araguaina",
    "Tokyo Standard Time": "Asia/Tokyo",
    "Tomsk Standard Time": "Asia/Tomsk",
    "Tonga Standard Time": "Pacific/Tongatapu",
    "Transbaikal Standard Time": "Asia/Chita",
    "Turkey Standard Time": "Europe/Istanbul",
    "Turks And Caicos Standard Time": "America/Grand_Turk",
    "US Eastern Standard Time": "America/Indiana/Indianapolis",
    "US Mountain Standard Time": "America/Phoenix",
    "UTC": "UTC",
    "UTC+12": "Etc/GMT-12",
    "UTC+13": "Etc/GMT-13",
    "UTC-02": "Etc/GMT+2",
    "UTC-08": "Etc/GMT+8",
    "UTC-09": "Etc/GMT+9",
    "UTC-11": "Etc/GMT+11",
    "Ulaanbaatar Standard Time": "Asia/Ulaanbaatar",
    "Venezuela Standard Time": "America/Caracas",
    "Vladivostok Standard Time": "Asia/Vladivostok",
    "Volgograd Standard Time": "Europe/Volgograd",
    "W. Australia Standard Time": "Australia/Perth",
    "W. Central Africa Standard Time": "Africa/Lagos",
    "W. Europe Standard Time": "Europe/Berlin",
    "W. Mongolia Standard Time": "Asia/Hovd",
    "West Asia Standard Time": "Asia/Tashkent",
    "West Bank Standard Time": "Asia/Hebron",
    "West Pacific Standard Time": "Pacific/Port_Moresby",
    "Yakutsk Standard Time": "Asia/Yakutsk",
    "Yukon Standard Time": "America/Whitehorse",
}

# Spellings of "the zone is UTC", which resolve without any tz database. The
# fallback issue #8 chose is `UTC`, so this is the *common* path on a mailbox
# whose zone can't be read -- it must not depend on `tzdata` being installed.
_UTC_IDS = frozenset({"utc", "etc/utc", "etc/gmt", "gmt", "z", "universal", "zulu"})

# .NET can serialise 7 fractional-second digits, one more than
# datetime.fromisoformat accepts. Trimmed wherever the fraction falls, before
# the offset suffix, rather than at a fixed string position.
_FRACTION_RE = re.compile(r"(\.\d{6})\d+")


class ZoneResolution(NamedTuple):
    """The zone availability timestamps are converted into, and how it was found.

    `tz` is None when nothing could be resolved, which means "do not shift" --
    the pre-fix behaviour, kept deliberately (see the module docstring) and
    always accompanied by a non-empty `warning`. `source` is one of
    SOURCE_MAILBOX / SOURCE_ENV / SOURCE_UNKNOWN.
    """

    tz: tzinfo | None
    timezone_id: str
    source: str
    warning: str = ""

    @property
    def shifts(self) -> bool:
        """True if this resolution actually moves a UTC instant."""
        return self.tz is not None and self.tz is not timezone.utc

    def as_dict(self) -> dict:
        """The `timezone` block the availability tools put in their output."""
        out = {"id": self.timezone_id or "unknown", "source": self.source}
        if self.warning:
            out["warning"] = self.warning
        return out


def iana_key(timezone_id: Any) -> str | None:
    """The IANA key for a Windows or IANA timezone id, or None if unmapped.

    An id containing "/" is taken to be IANA already and passed through: no
    Windows id contains one, and the modern backend has been seen handing IANA
    ids to its own web client, so both have to be accepted from
    `mailbox_timezone()`.
    """
    if not isinstance(timezone_id, str):
        return None
    stripped = timezone_id.strip()
    if not stripped:
        return None
    if stripped.lower() in _UTC_IDS:
        return "UTC"
    if "/" in stripped:
        return stripped
    return _WINDOWS_TO_IANA.get(stripped)


def resolve_zone(
    timezone_id: Any,
    *,
    source: str = SOURCE_UNKNOWN,
    loader: Callable[[str], tzinfo] | None = None,
) -> ZoneResolution:
    """Turn a timezone id into something that can convert an instant.

    `loader` defaults to `zoneinfo.ZoneInfo` and exists so the two failure
    branches -- an id this table doesn't know, and a tz database that isn't
    installed -- are testable on any platform. They are separate warnings
    because they need different fixes: the first wants a table entry (or an
    IANA id in `EXCHANGE_TIMEZONE`), the second wants `pip install tzdata`.
    """
    if not isinstance(timezone_id, str) or not timezone_id.strip():
        return ZoneResolution(
            None, "", SOURCE_UNKNOWN,
            "the mailbox timezone is unknown, so busy times are compared as UTC against "
            f"local working hours and may be off by the mailbox's UTC offset; set {ENV_VAR}",
        )

    wanted = timezone_id.strip()
    key = iana_key(wanted)
    if key is None:
        return ZoneResolution(
            None, wanted, source,
            f"'{wanted}' is not a timezone id this build can map to an IANA zone, so busy "
            f"times are compared as UTC against local working hours; set {ENV_VAR} to an "
            "IANA id such as Europe/Rome",
        )
    if key == "UTC":
        # No tz database needed, and the common case when issue #8's own
        # resolution fell back.
        return ZoneResolution(timezone.utc, wanted, source)

    if loader is None:
        from zoneinfo import ZoneInfo  # noqa: PLC0415 - kept local so the module stays import-light

        loader = ZoneInfo
    try:
        return ZoneResolution(loader(key), wanted, source)
    except Exception as exc:  # noqa: BLE001 - any loader failure degrades, never raises
        return ZoneResolution(
            None, wanted, source,
            f"no IANA timezone data for '{key}' ({type(exc).__name__}), so busy times are "
            "compared as UTC against local working hours; install the 'tzdata' package",
        )


def timezone_id_from_client(client: Any) -> tuple[str | None, str]:
    """The mailbox's timezone id and where it came from.

    Prefers `OWAClient.mailbox_timezone()` -- which resolves
    `EXCHANGE_TIMEZONE` > the mailbox's own OWA configuration > UTC (issue #8)
    -- and reads `EXCHANGE_TIMEZONE` directly only on a build that predates
    it. `getattr` rather than a hard call so this module is usable from a
    branch on either side of that fix, and so a test can pass a stub.
    Anything the lookup raises is swallowed: a zone we could not read must
    degrade to "not shifting", never fail an availability query.
    """
    getter = getattr(client, "mailbox_timezone", None)
    if callable(getter):
        try:
            value = getter()
        except Exception:  # noqa: BLE001 - see the docstring
            value = None
        if isinstance(value, str) and value.strip():
            return value.strip(), SOURCE_MAILBOX

    raw = os.environ.get(ENV_VAR, "").strip()
    if raw:
        return raw, SOURCE_ENV
    return None, SOURCE_UNKNOWN


def mailbox_zone(client: Any, *, loader: Callable[[str], tzinfo] | None = None) -> ZoneResolution:
    """The zone this mailbox's availability timestamps should be read in.

    Resolved once per tool call rather than per event: it costs a request on
    the first call of the process (see `mailbox_timezone_detail`) and cannot
    change underneath one query.
    """
    timezone_id, source = timezone_id_from_client(client)
    return resolve_zone(timezone_id, source=source, loader=loader)


def schedule_tz_id(zone: ZoneResolution) -> str | None:
    """The `tz_id` to request a schedule in, or None to leave the call's default.

    `get_schedule`'s `tz_id` is what `availabilityView` is *answered* in, and
    that string is the one path here that must not be converted afterwards --
    so the only way to get it onto the working-hours grid is to ask for it in
    the right zone in the first place. Returning the same id the events are
    converted into is what keeps the two paths from disagreeing: a run where
    the window is Moscow (this codebase's old hardcode) and the events are
    Rome answers one grid for one attendee list and another for the next.

    Returns None whenever the zone could not be resolved, because an id we
    could not make sense of locally is not one to put on the wire either --
    the call keeps its own default and the tool's warning already says the
    grid may be off.
    """
    return zone.timezone_id if zone.tz is not None and zone.timezone_id else None


def to_wall_clock(dt: datetime, zone: ZoneResolution) -> datetime:
    """A UTC instant as naive wall-clock time in `zone`.

    A naive input is taken to be UTC, which is what `_parse_schedule_dt`
    guarantees for `get_schedule`'s events. The conversion is per *instant*,
    so a range spanning a DST transition shifts by the offset in force on each
    side of it rather than by one constant.
    """
    aware = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    return aware.astimezone(zone.tz or timezone.utc).replace(tzinfo=None)


def wire_to_wall_clock(raw: Any, zone: ZoneResolution) -> datetime | None:
    """Parse an OWA timestamp into wall-clock time, converting only if it says to.

    The offset on the wire *is* the frame declaration, and reading it is what
    lets one helper serve both availability paths: `scheduleItems` and
    `FindItem` send `Z`/`+00:00` (a UTC instant, converted), while EWS
    `CalendarEventArray` sends no suffix at all (already wall-clock in the
    request's `TimeZoneContext` zone, returned untouched). Assuming either one
    would corrupt the other. Returns None on an unparseable value, the same
    "skip this item" signal the callers already had.
    """
    if not isinstance(raw, str) or not raw:
        return None
    text = _FRACTION_RE.sub(r"\1", raw.strip()).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed
    return to_wall_clock(parsed, zone)


def to_utc_naive(raw: Any) -> datetime | None:
    """Parse an OWA timestamp to naive UTC -- the `get_schedule` convention.

    Kept here next to `wire_to_wall_clock` so the two conventions this
    codebase uses are one file apart and can be told apart by name.
    `OWAClient._parse_schedule_dt` is the caller; the normalisation to UTC
    matters for an offset that is not zero, where stripping `tzinfo` outright
    would keep somebody else's wall clock and call it UTC.
    """
    if not isinstance(raw, str) or not raw:
        return None
    text = _FRACTION_RE.sub(r"\1", raw.strip()).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def events_to_wall_clock(events: list[dict], zone: ZoneResolution) -> list[dict]:
    """Copies of `get_schedule`-shaped event dicts, with `start`/`end` converted.

    Copies rather than mutates: the caller's list comes straight out of
    `OWAClient`, and a helper that rewrote it in place would convert twice if
    a tool ever read the same schedule for two grids.
    """
    converted = []
    for event in events:
        moved = dict(event)
        for key in ("start", "end"):
            value = event.get(key)
            if isinstance(value, datetime):
                moved[key] = to_wall_clock(value, zone)
        converted.append(moved)
    return converted
