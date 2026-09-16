"""The mailbox's own timezone, and the one place UTC becomes wall-clock time.

Every busy-period source in this codebase hands back a **UTC instant stripped to
a naive datetime** -- `OWAClient._parse_schedule_dt` says so in as many words,
and calls it "the established (if imprecise) convention every caller downstream
already assumes". That convention is fine for anything that only ever compares
two instants to each other. It is wrong the moment a *wall-clock* number enters
the same arithmetic, and `find_free_time` (#601) / `find_meeting_time` (#602) do
exactly that: they subtract naive-UTC busy periods from a working-day window
built as `datetime.combine(date, min.time().replace(hour=start_hour))`, where
`start_hour` defaults to 9 and can only mean 9 in the morning *where the mailbox
lives*. On a W. Europe mailbox in DST that mixes two frames two hours apart, so
a meeting at 10:00 local was subtracted from the 08:00 position of the window
and every reported slot came out shifted.

Verified live on 2026-09-16 against a W. Europe (UTC+2, DST) mailbox: three
meetings at 14:30-15:30, 15:00-16:00 and 17:00-17:30 local were reported by
`get_calendar_events` as `12:30Z`/`13:00Z`/`15:00Z` -- correct, and *labelled* --
while `find_free_time` returned free slots `09:00-12:30`, `14:00-15:00`,
`15:30-18:00`, i.e. the same UTC numbers rendered as bare `%H:%M` next to a
local-only `start_hour`. **The label is where the information was lost, not the
arithmetic**, which is why this module exists instead of a `+2h` somewhere.

What this module is for
-----------------------

`_find_free_slots` is a pure function whose four datetime inputs must share one
frame, and whose signature cannot say which. So the frame is declared here and
established at the *edge* of the availability module: every busy period is
converted to **naive mailbox-local wall clock** as soon as it is parsed, and the
day window is built in that same frame. Nothing downstream converts anything.

Two conversion entry points, named after the frame of their *input*, because
that is the fact a call site can get wrong:

- `from_utc()`      -- a UTC instant (aware, or naive-UTC per the convention
                       above) -> naive mailbox-local. This is the one for
                       `GetSchedule`'s `scheduleItems` and for any EWS
                       timestamp that came back `Z`-suffixed.
- `from_wire_wallclock()` -- naive wall clock *in the timezone we asked the
                       server to answer in* -> naive mailbox-local. This is the
                       one for `availabilityView` / `MergedFreeBusy`, whose
                       character positions are wall-clock offsets from the
                       requested window start in the requested `tz_id`.

`to_utc()` goes the other way, for expressing a local window on the wire.

Why both of those exist rather than one
---------------------------------------

The two free/busy shapes `GetSchedule` returns are in **different frames at the
same time**: `scheduleItems` come back UTC-offset regardless of the requested
`tz_id`, while `availabilityView` is wall-clock *in* that `tz_id` (both stated
in `_parse_schedule_dt`'s docstring, from a live capture). `find_meeting_time`
merged both into one busy list. So there was a second, independent bug on top of
the reported one: with `tz_id` hardcoded to `"Russian Standard Time"` (UTC+3 --
a stray default that spread to nine call sites across five modules), an
`availabilityView` attendee was three hours off while a `scheduleItems`
attendee was two, and merging them produced busy blocks that were in no
timezone at all. Naming the input frame at each call site is what makes that
kind of mixture impossible to write by accident.

`wire_id` and why it is sometimes "UTC"
--------------------------------------

`wire_id` is the timezone id we send to the server, and the invariant this
module keeps is that **`wire_id` and the local frame agree**:

- Timezone known from the mailbox's own configuration -> send it. That is what
  OWA's own client does, it is the highest-fidelity option, and it makes
  `from_wire_wallclock()` a pure identity: `availabilityView` arrives already
  in the frame we want, with no conversion to get wrong.
- Timezone *not* known -> send `"UTC"` and convert every instant ourselves.
  Deliberately not "send the host's Windows timezone id": that would need an
  IANA->Windows reverse mapping whose failures are silent and whose result the
  server might not accept, to buy nothing over a conversion we can do exactly.

So a call site never has to ask which of the two happened -- it passes
`tz.wire_id` on the wire and pipes the answer through the matching entry point.

Resolution order, and why every step is allowed to fail
-------------------------------------------------------

`resolve_mailbox_timezone()` degrades in three steps, and records which one it
took in `source` -- surfaced in the tool output as `timezone.source`, because a
two-hour shift that was silent once must not be able to go silent again:

1. `mailbox_configuration` -- a Windows timezone id read off
   `GetOwaUserConfiguration` and present in `WINDOWS_TO_IANA`. The good case.
2. `host_local` -- the machine's own timezone, via `datetime.astimezone()` with
   no argument so the OS resolves the offset per instant (DST-correct, and
   needs no timezone database of our own). Nearly always right for a desktop
   MCP server, and a `warning` says it is a fallback. Taken when the probe
   found nothing, when it found an id this module does not know, or when the
   `zoneinfo` lookup fails.
3. `utc` -- identity. Only reachable when the host has no usable local
   timezone. Explicitly the *pre-fix* behaviour, kept as the floor because a
   fabricated offset is worse than a documented absent one.

None of these raise. A timezone probe that fails must not be able to take
`find_free_time` offline -- the tool was useful-but-shifted before, and
useful-but-shifted with a warning attached is strictly better than an error.

`WINDOWS_TO_IANA` is the correctable-in-one-place table this codebase uses for
domain knowledge everywhere else (`auth_errors.py`'s AADSTS codes,
`profile_lock.py`'s launch hints, `utils.py`'s item-error signals): Exchange
speaks Windows timezone ids ("W. Europe Standard Time") and `zoneinfo` speaks
IANA ("Europe/Berlin"), and the mapping is CLDR's `windowsZones` territory-001
default. An id missing from it produces the `host_local` fallback *plus* a
warning naming the id, so the fix is one row here rather than a code change.

Pure logic, no Playwright and no OWAClient import, so it stays unit-testable --
see tests/unit/test_mailbox_timezone.py. The transport half is
`OWAClient.mailbox_timezone()`, which probes once per process and caches.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Any

# ------------------------------------------------------------------
# Where the timezone came from (MailboxTimezone.source)
# ------------------------------------------------------------------

SOURCE_MAILBOX = "mailbox_configuration"
SOURCE_HOST = "host_local"
SOURCE_UTC = "utc"

# ------------------------------------------------------------------
# Keys that carry a timezone id in a GetOwaUserConfiguration response
# ------------------------------------------------------------------

# Searched recursively and in this order of preference, because the response is
# a deep nest (UserOptions / SessionSettings / ...) whose exact shape differs
# between the classic canary-cookie backend and the modern bearer one, and
# because more than one of these can be present at once. `TimeZone` under
# `UserOptions` is OWA's own spelling and the one to trust; the rest are the
# spellings seen on adjacent Exchange surfaces, kept so a backend that uses one
# of them is a table row rather than a bug report.
#
# Add a new spelling here, not to the walker.
TZ_ID_KEYS: tuple[str, ...] = (
    "TimeZone",
    "TimeZoneId",
    "UserTimeZoneId",
    "TimeZoneKeyName",
    "WorkingHoursTimeZone",
    "MailboxTimeZone",
    "DefaultTimeZone",
)

# ------------------------------------------------------------------
# Windows timezone id -> IANA zone (CLDR windowsZones, territory "001")
# ------------------------------------------------------------------

# Only the territory-001 default per Windows id: this maps *from* Windows *to*
# IANA, and for that direction the territory variants are all the same offset
# and DST rule, so the default is the whole answer. A missing id is a warned
# `host_local` fallback, never a wrong offset -- so growing this table is safe
# and never urgent.
WINDOWS_TO_IANA: dict[str, str] = {
    "UTC": "UTC",
    "UTC-11": "Etc/GMT+11",
    "UTC-09": "Etc/GMT+9",
    "UTC-08": "Etc/GMT+8",
    "UTC-02": "Etc/GMT+2",
    "UTC+12": "Etc/GMT-12",
    "UTC+13": "Etc/GMT-13",
    "Dateline Standard Time": "Etc/GMT+12",
    "Aleutian Standard Time": "America/Adak",
    "Hawaiian Standard Time": "Pacific/Honolulu",
    "Marquesas Standard Time": "Pacific/Marquesas",
    "Alaskan Standard Time": "America/Anchorage",
    "Pacific Standard Time (Mexico)": "America/Tijuana",
    "Pacific Standard Time": "America/Los_Angeles",
    "US Mountain Standard Time": "America/Phoenix",
    "Mountain Standard Time (Mexico)": "America/Mazatlan",
    "Mountain Standard Time": "America/Denver",
    "Yukon Standard Time": "America/Whitehorse",
    "Central America Standard Time": "America/Guatemala",
    "Central Standard Time": "America/Chicago",
    "Easter Island Standard Time": "Pacific/Easter",
    "Central Standard Time (Mexico)": "America/Mexico_City",
    "Canada Central Standard Time": "America/Regina",
    "SA Pacific Standard Time": "America/Bogota",
    "Eastern Standard Time (Mexico)": "America/Cancun",
    "Eastern Standard Time": "America/New_York",
    "Haiti Standard Time": "America/Port-au-Prince",
    "Cuba Standard Time": "America/Havana",
    "US Eastern Standard Time": "America/Indianapolis",
    "Turks And Caicos Standard Time": "America/Grand_Turk",
    "Paraguay Standard Time": "America/Asuncion",
    "Atlantic Standard Time": "America/Halifax",
    "Venezuela Standard Time": "America/Caracas",
    "Central Brazilian Standard Time": "America/Cuiaba",
    "SA Western Standard Time": "America/La_Paz",
    "Pacific SA Standard Time": "America/Santiago",
    "Newfoundland Standard Time": "America/St_Johns",
    "Tocantins Standard Time": "America/Araguaina",
    "E. South America Standard Time": "America/Sao_Paulo",
    "SA Eastern Standard Time": "America/Cayenne",
    "Argentina Standard Time": "America/Buenos_Aires",
    "Montevideo Standard Time": "America/Montevideo",
    "Magallanes Standard Time": "America/Punta_Arenas",
    "Saint Pierre Standard Time": "America/Miquelon",
    "Bahia Standard Time": "America/Bahia",
    "Mid-Atlantic Standard Time": "Etc/GMT+2",
    "Azores Standard Time": "Atlantic/Azores",
    "Cape Verde Standard Time": "Atlantic/Cape_Verde",
    "GMT Standard Time": "Europe/London",
    "Greenwich Standard Time": "Atlantic/Reykjavik",
    "Sao Tome Standard Time": "Africa/Sao_Tome",
    "Morocco Standard Time": "Africa/Casablanca",
    "W. Europe Standard Time": "Europe/Berlin",
    "Central Europe Standard Time": "Europe/Budapest",
    "Romance Standard Time": "Europe/Paris",
    "Central European Standard Time": "Europe/Warsaw",
    "W. Central Africa Standard Time": "Africa/Lagos",
    "GTB Standard Time": "Europe/Bucharest",
    "Middle East Standard Time": "Asia/Beirut",
    "Egypt Standard Time": "Africa/Cairo",
    "E. Europe Standard Time": "Europe/Chisinau",
    "West Bank Standard Time": "Asia/Hebron",
    "South Africa Standard Time": "Africa/Johannesburg",
    "FLE Standard Time": "Europe/Kiev",
    "Israel Standard Time": "Asia/Jerusalem",
    "South Sudan Standard Time": "Africa/Juba",
    "Kaliningrad Standard Time": "Europe/Kaliningrad",
    "Sudan Standard Time": "Africa/Khartoum",
    "Libya Standard Time": "Africa/Tripoli",
    "Namibia Standard Time": "Africa/Windhoek",
    "Jordan Standard Time": "Asia/Amman",
    "Arabic Standard Time": "Asia/Baghdad",
    "Syria Standard Time": "Asia/Damascus",
    "Turkey Standard Time": "Europe/Istanbul",
    "Arab Standard Time": "Asia/Riyadh",
    "Belarus Standard Time": "Europe/Minsk",
    "Russian Standard Time": "Europe/Moscow",
    "E. Africa Standard Time": "Africa/Nairobi",
    "Volgograd Standard Time": "Europe/Volgograd",
    "Iran Standard Time": "Asia/Tehran",
    "Arabian Standard Time": "Asia/Dubai",
    "Astrakhan Standard Time": "Europe/Astrakhan",
    "Azerbaijan Standard Time": "Asia/Baku",
    "Russia Time Zone 3": "Europe/Samara",
    "Mauritius Standard Time": "Indian/Mauritius",
    "Saratov Standard Time": "Europe/Saratov",
    "Georgian Standard Time": "Asia/Tbilisi",
    "Caucasus Standard Time": "Asia/Yerevan",
    "Afghanistan Standard Time": "Asia/Kabul",
    "West Asia Standard Time": "Asia/Tashkent",
    "Qyzylorda Standard Time": "Asia/Qyzylorda",
    "Ekaterinburg Standard Time": "Asia/Yekaterinburg",
    "Pakistan Standard Time": "Asia/Karachi",
    "India Standard Time": "Asia/Calcutta",
    "Sri Lanka Standard Time": "Asia/Colombo",
    "Nepal Standard Time": "Asia/Katmandu",
    "Central Asia Standard Time": "Asia/Bishkek",
    "Bangladesh Standard Time": "Asia/Dhaka",
    "Omsk Standard Time": "Asia/Omsk",
    "Myanmar Standard Time": "Asia/Rangoon",
    "SE Asia Standard Time": "Asia/Bangkok",
    "Altai Standard Time": "Asia/Barnaul",
    "W. Mongolia Standard Time": "Asia/Hovd",
    "North Asia Standard Time": "Asia/Krasnoyarsk",
    "N. Central Asia Standard Time": "Asia/Novosibirsk",
    "Tomsk Standard Time": "Asia/Tomsk",
    "China Standard Time": "Asia/Shanghai",
    "North Asia East Standard Time": "Asia/Irkutsk",
    "Singapore Standard Time": "Asia/Singapore",
    "W. Australia Standard Time": "Australia/Perth",
    "Taipei Standard Time": "Asia/Taipei",
    "Ulaanbaatar Standard Time": "Asia/Ulaanbaatar",
    "Aus Central W. Standard Time": "Australia/Eucla",
    "Transbaikal Standard Time": "Asia/Chita",
    "Tokyo Standard Time": "Asia/Tokyo",
    "North Korea Standard Time": "Asia/Pyongyang",
    "Korea Standard Time": "Asia/Seoul",
    "Yakutsk Standard Time": "Asia/Yakutsk",
    "Cen. Australia Standard Time": "Australia/Adelaide",
    "AUS Central Standard Time": "Australia/Darwin",
    "E. Australia Standard Time": "Australia/Brisbane",
    "AUS Eastern Standard Time": "Australia/Sydney",
    "West Pacific Standard Time": "Pacific/Port_Moresby",
    "Tasmania Standard Time": "Australia/Hobart",
    "Vladivostok Standard Time": "Asia/Vladivostok",
    "Lord Howe Standard Time": "Australia/Lord_Howe",
    "Bougainville Standard Time": "Pacific/Bougainville",
    "Russia Time Zone 10": "Asia/Srednekolymsk",
    "Magadan Standard Time": "Asia/Magadan",
    "Norfolk Standard Time": "Pacific/Norfolk",
    "Sakhalin Standard Time": "Asia/Sakhalin",
    "Central Pacific Standard Time": "Pacific/Guadalcanal",
    "Russia Time Zone 11": "Asia/Kamchatka",
    "New Zealand Standard Time": "Pacific/Auckland",
    "Fiji Standard Time": "Pacific/Fiji",
    "Chatham Islands Standard Time": "Pacific/Chatham",
    "Tonga Standard Time": "Pacific/Tongatapu",
    "Samoa Standard Time": "Pacific/Apia",
    "Line Islands Standard Time": "Pacific/Kiritimati",
}

# The id sent on the wire when the mailbox's own timezone is unknown. "UTC" is a
# valid Windows timezone key name and EWS/`GetSchedule` both accept it, so this
# needs no special-casing at the call sites -- it just makes every server answer
# arrive in one frame we can convert exactly.
_UTC_WIRE_ID = "UTC"

_UTC = _dt.timezone.utc


# ------------------------------------------------------------------
# The resolved timezone
# ------------------------------------------------------------------

@dataclass(frozen=True)
class MailboxTimezone:
    """The mailbox's timezone, plus how confident we are about it.

    `windows_id`/`iana_id` are informational (and empty on the degraded paths).
    The load-bearing members are `wire_id`, `from_utc()`,
    `from_wire_wallclock()` and `to_utc()` -- see the module docstring for the
    frame contract those four keep between them.

    Constructed via `resolve_mailbox_timezone()`, not directly: the fallback
    chain and the `source`/`warning` bookkeeping live there.
    """

    source: str
    windows_id: str = ""
    iana_id: str = ""
    tz: _dt.tzinfo | None = None
    warning: str | None = None

    # -- the wire ---------------------------------------------------

    @property
    def wire_id(self) -> str:
        """The timezone id to send to the server.

        The mailbox's own Windows id when we have one (what OWA's own client
        sends, and it makes `from_wire_wallclock()` an identity), else "UTC" --
        so that the frame we ask for is always one we can convert out of
        exactly. Never empty.
        """
        if self.source == SOURCE_MAILBOX and self.windows_id:
            return self.windows_id
        return _UTC_WIRE_ID

    @property
    def wire_is_utc(self) -> bool:
        """True when the server is being asked to answer in UTC, i.e. when
        `from_wire_wallclock()` has real work to do. Exposed so a call site can
        assert the frame it thinks it is in rather than infer it."""
        return self.wire_id == _UTC_WIRE_ID and self.source != SOURCE_UTC

    # -- conversions ------------------------------------------------

    def from_utc(self, dt: _dt.datetime | None) -> _dt.datetime | None:
        """A UTC instant -> naive mailbox-local wall clock.

        Accepts either an aware datetime or a naive one that is UTC by the
        convention `_parse_schedule_dt` documents; an aware input in some other
        zone is normalised, so this is safe on an EWS timestamp that arrived
        with a real offset rather than a bare "Z". `None` passes through, so a
        caller that is already skipping unparseable timestamps does not grow a
        second guard.

        On `SOURCE_UTC` this is the identity -- that path is the pre-fix
        behaviour on purpose (see the module docstring).
        """
        if dt is None:
            return None
        if self.source == SOURCE_UTC:
            return dt.replace(tzinfo=None)
        aware = dt.replace(tzinfo=_UTC) if dt.tzinfo is None else dt
        if self.tz is not None:
            return aware.astimezone(self.tz).replace(tzinfo=None)
        # SOURCE_HOST: no argument, so the OS resolves the offset for *this*
        # instant. That is what makes the fallback DST-correct without this
        # module carrying a timezone database.
        return aware.astimezone().replace(tzinfo=None)

    def from_wire_wallclock(self, dt: _dt.datetime | None) -> _dt.datetime | None:
        """Naive wall clock in `wire_id`'s timezone -> naive mailbox-local.

        For `availabilityView` / `MergedFreeBusy`, whose character positions are
        wall-clock offsets from the requested window start *in the requested
        timezone id*. Identity whenever `wire_id` is already the mailbox's own
        timezone, which is the normal case -- the conversion exists for the
        degraded one, where we asked for UTC.
        """
        if dt is None:
            return None
        if not self.wire_is_utc:
            return dt
        return self.from_utc(dt)

    def from_wire_timestamp(self, dt: _dt.datetime | None) -> _dt.datetime | None:
        """An EWS timestamp -> naive mailbox-local, deciding the frame from the
        value itself: **aware means a real instant, naive means wall clock in
        the timezone we asked for.**

        For the EWS shapes whose frame depends on the request rather than the
        action. `FindItem` without a `TimeZoneContext` returns `Z`-suffixed UTC;
        `GetUserAvailability` *with* one returns `CalendarEventArray` times as
        unqualified wall clock in that timezone. Both go through here, and the
        offset suffix -- which `datetime.fromisoformat` has already turned into
        `tzinfo` or the absence of it by the time this is called -- is what tells
        them apart.

        This is not a convenience wrapper over the other two, it is the rule
        that stops the classic-OWA fallback double-converting. That branch sends
        a `TimeZoneContext` and always has, so once that context became the
        mailbox's own timezone its unqualified timestamps arrived *already*
        local; running them through `from_utc()` would have moved them a second
        time. It cannot be checked live on a tenant where
        `GetUserAvailability` answers `NotImplementedException`
        (PROJECT_STATUS.md #602), so it is written to need no conversion at all
        in the configuration we can reason about: when the mailbox timezone is
        known, this is the identity for a naive input.
        """
        if dt is None:
            return None
        if dt.tzinfo is not None:
            return self.from_utc(dt)
        return self.from_wire_wallclock(dt)

    def to_utc(self, dt: _dt.datetime | None) -> _dt.datetime | None:
        """Naive mailbox-local wall clock -> naive UTC.

        The inverse of `from_utc()`, for expressing a locally-meaningful window
        (a day boundary, a working-hours edge) on the wire when `wire_id` is
        "UTC".

        A wall-clock time that a DST transition skips or repeats is resolved
        with `fold=0`, i.e. the first of the two instants. Deliberately not
        raised as an error: this is used on window *edges*, where the worst case
        is that a window starts an hour early twice a year, and refusing to
        return a window at all would be a strictly worse answer than a slightly
        wide one.
        """
        if dt is None:
            return None
        if self.source == SOURCE_UTC:
            return dt.replace(tzinfo=None)
        naive = dt.replace(tzinfo=None) if dt.tzinfo is not None else dt
        if self.tz is not None:
            return naive.replace(tzinfo=self.tz).astimezone(_UTC).replace(tzinfo=None)
        return naive.astimezone(_UTC).replace(tzinfo=None)

    def to_wire_wallclock(self, dt: _dt.datetime | None) -> _dt.datetime | None:
        """Naive mailbox-local wall clock -> naive wall clock in `wire_id`.

        The inverse of `from_wire_wallclock()`, for the window sent alongside an
        `availabilityView` request so that the string's index 0 lands where the
        caller meant it to.
        """
        if dt is None:
            return None
        if not self.wire_is_utc:
            return dt
        return self.to_utc(dt)

    # -- reporting --------------------------------------------------

    def utc_offset(self, at: _dt.datetime | None = None) -> _dt.timedelta:
        """The mailbox's UTC offset at a given instant (naive-UTC or aware).

        Takes an instant rather than being a property because that is the whole
        point: the reported bug was two hours on a zone whose offset is one hour
        for four months of the year.
        """
        reference = at or _dt.datetime.now(_UTC)
        local = self.from_utc(reference)
        base = reference.replace(tzinfo=None) if reference.tzinfo else reference
        return (local or base) - base

    def describe(self, at: _dt.datetime | None = None) -> dict[str, Any]:
        """The `timezone` block the availability tools attach to their output.

        Reported unconditionally, not only on the fallback paths: a shift this
        size was invisible for long enough that "which frame are these numbers
        in" should be answerable from the response itself, not from the source.
        """
        offset = self.utc_offset(at)
        total_minutes = int(offset.total_seconds() // 60)
        sign = "+" if total_minutes >= 0 else "-"
        block: dict[str, Any] = {
            "source": self.source,
            "utc_offset": f"{sign}{abs(total_minutes) // 60:02d}:{abs(total_minutes) % 60:02d}",
            # The note has to tell the truth on the floor too. On SOURCE_UTC no
            # conversion happened, so claiming "mailbox-local" there would put
            # the exact misdescription this whole change is about back into the
            # response, one field over.
            "note": (
                "times are UTC: no mailbox timezone could be determined"
                if self.source == SOURCE_UTC
                else "times are mailbox-local wall clock"
            ),
        }
        if self.windows_id:
            block["windows_id"] = self.windows_id
        if self.iana_id:
            block["iana_id"] = self.iana_id
        if self.warning:
            block["warning"] = self.warning
        return block


# ------------------------------------------------------------------
# Parsing GetOwaUserConfiguration
# ------------------------------------------------------------------

def _looks_like_timezone_id(value: Any) -> bool:
    """Is this string plausibly a timezone id at all?

    Guards against the two things a `TimeZone` key legitimately holds *other*
    than an id: a nested object (the EWS `TimeZoneDefinition` shape), and a
    localised display name like "(UTC+01:00) Amsterdam, Berlin, ...". Neither is
    something `resolve_mailbox_timezone` could use, and letting a display name
    through would spend the mailbox's real timezone on a warning about an
    unmappable one.
    """
    if not isinstance(value, str):
        return False
    value = value.strip()
    if not value or len(value) > 80:
        return False
    # A display name, not an id.
    return not value.startswith("(")


def find_timezone_candidates(payload: Any) -> tuple[str, ...]:
    """Every plausible timezone id in a `GetOwaUserConfiguration` response.

    Walks the whole response rather than a fixed path on purpose: the shape
    differs between the classic canary-cookie backend and the modern bearer one
    (`UserOptions` vs `SessionSettings` vs neither), and a probe that misses
    because the nesting moved is indistinguishable, downstream, from a mailbox
    that has no timezone set. Returned de-duplicated, ordered by `TZ_ID_KEYS`
    preference first and document order second, so `parse_mailbox_timezone_id`
    can pick without re-walking.
    """
    found: dict[str, int] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in TZ_ID_KEYS and _looks_like_timezone_id(value):
                    candidate = value.strip()
                    rank = TZ_ID_KEYS.index(key)
                    if candidate not in found or rank < found[candidate]:
                        found[candidate] = rank
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return tuple(sorted(found, key=lambda c: found[c]))


def parse_mailbox_timezone_id(payload: Any) -> str:
    """The best timezone id in a `GetOwaUserConfiguration` response, or "".

    "Best" means: a candidate this module can actually map, if there is one --
    otherwise the highest-preference candidate as-is, so that
    `resolve_mailbox_timezone` can name it in its warning. Returning the
    unmappable id rather than "" is the difference between "add this row to
    WINDOWS_TO_IANA" and "the probe found nothing", which are very different
    things to act on.
    """
    candidates = find_timezone_candidates(payload)
    for candidate in candidates:
        if candidate in WINDOWS_TO_IANA:
            return candidate
    return candidates[0] if candidates else ""


# ------------------------------------------------------------------
# Resolution
# ------------------------------------------------------------------

def _host_or_utc(warning: str) -> MailboxTimezone:
    """The `host_local` fallback, degrading to `utc` if the host has none.

    `datetime.now().astimezone()` is the probe: it is how `from_utc` will do the
    conversion, so if it cannot produce an offset here it would not produce one
    there either, and claiming `host_local` would be a lie in the `source`
    field. Any exception counts -- a machine with an unusable local timezone is
    rare enough that enumerating the ways is not worth guessing at.
    """
    try:
        if _dt.datetime.now().astimezone().utcoffset() is not None:
            return MailboxTimezone(source=SOURCE_HOST, warning=warning)
    except (OSError, ValueError, OverflowError):
        pass
    return MailboxTimezone(
        source=SOURCE_UTC,
        warning=f"{warning} The host has no usable local timezone either, so times are UTC.",
    )


def resolve_mailbox_timezone(windows_id: str | None) -> MailboxTimezone:
    """Turn a probed Windows timezone id into a usable `MailboxTimezone`.

    Never raises and never returns None: see the module docstring for why the
    three-step degradation is the contract rather than an error. Pass "" or None
    for "the probe found nothing" -- that is a normal input, not a mistake.
    """
    candidate = (windows_id or "").strip()

    if not candidate:
        return _host_or_utc(
            "The mailbox timezone could not be read from the server, so this "
            "machine's local timezone was assumed."
        )

    iana = WINDOWS_TO_IANA.get(candidate)
    if iana is None:
        return _host_or_utc(
            f"Unknown timezone id {candidate!r} (add it to "
            f"exchange_mcp.mailbox_timezone.WINDOWS_TO_IANA); this machine's "
            f"local timezone was assumed."
        )

    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(iana)
    except Exception:
        # Almost always a missing timezone database -- `zoneinfo` has no
        # bundled one, and Windows and slim Linux images have no system one
        # either, hence the `tzdata` dependency in pyproject.toml. Caught
        # broadly because every reason lands in the same fallback.
        return _host_or_utc(
            f"The mailbox timezone is {candidate!r} ({iana}) but no timezone "
            f"database is available to interpret it (is `tzdata` installed?); "
            f"this machine's local timezone was assumed."
        )

    return MailboxTimezone(
        source=SOURCE_MAILBOX, windows_id=candidate, iana_id=iana, tz=tz
    )


# The floor, exposed so a caller with no client at all (a unit test, an
# offline code path) has the same object shape to work with.
UTC_TIMEZONE = MailboxTimezone(source=SOURCE_UTC, windows_id="UTC", iana_id="UTC")
