"""Pure resolution of the timezone this server sends on every OWA request.

Every write in this codebase used to send `TimeZoneContext` =
`Russian Standard Time` (UTC+3), a literal inherited from the original
scripts and copy-pasted into nine places across six modules. It was
invisible while it only shaped calendar *reads*; it stopped being
invisible when `create_task`/`update_task`'s `reminder` inherited it,
because a reminder asked for at 09:30 was stored as 09:30 *Moscow* and
therefore fired three hours early anywhere outside UTC+3 -- while the
tool reported success (issue #8).

This module owns the decision "which timezone id goes on the wire", and
nothing else: it imports only the standard library (no Playwright, no
transport), so it is unit-testable the same way `auth_errors.py` and
`profile_lock.py` are. `OWAClient.mailbox_timezone()` is the caller that
supplies it with a live answer from OWA; every request builder then goes
through `request_header()` rather than pasting a `TimeZoneContext` block
of its own.

Three deliberate design points:

- **Precedence is env > mailbox > UTC, and the fallback is never a
  regional zone.** `EXCHANGE_TIMEZONE` wins because an operator who has
  to set it is, by definition, in the case where discovery got it wrong,
  and a discovered value silently overriding them would leave them no
  way out. UTC is the fallback because it is the only zone that is
  *wrong in a self-evident way*: an hour that is off by the mailbox's
  own offset reads as a bug, whereas 09:30 Moscow looks like a correct
  answer and hid for months.
- **The value is validated for shape, not against a list of zones.**
  Exchange wants a Windows timezone id (`W. Europe Standard Time`), but
  the modern backend has been observed handing IANA ids
  (`Europe/Rome`) to its own web client, and no table of either kind
  can be kept current here. So `looks_like_timezone_id()` only rejects
  values that cannot be a timezone id at all, and a zone Exchange
  refuses surfaces as a request error the operator overrides with
  `EXCHANGE_TIMEZONE` -- which is precisely why that override exists.
- **Discovery is table-driven over key *names*.** Neither OWA
  configuration response shape is documented for this backend, and
  `GetUserConfiguration` is already known to fault here for another
  config name (see `tools/categories.py`), so the parser reads whichever
  of the two shapes answers rather than hard-coding a path into one.
  Add new spellings to `_TIMEZONE_KEY_HINTS` / `_DICTIONARY_KEY_HINTS`,
  not to the walk, and cover them in
  `tests/unit/test_mailbox_timezone.py`.
"""

import os
import re
from typing import Any, Mapping, NamedTuple

# Operator override. Documented in CLAUDE.md and .env.local.example; it is
# also the escape hatch for a discovered id Exchange won't accept.
ENV_VAR = "EXCHANGE_TIMEZONE"

# What we send when neither the operator nor the mailbox told us. Chosen for
# being obviously wrong rather than plausibly right -- see the module
# docstring. Never make this a regional zone again.
FALLBACK_TIMEZONE_ID = "UTC"

# Where a resolved id came from, reported by tools so the resolution stops
# being invisible the way the hardcode was.
SOURCE_ENV = "env"
SOURCE_MAILBOX = "mailbox"
SOURCE_FALLBACK = "fallback"

# The zone this codebase used to hardcode. Kept only so a test can assert it
# is gone from the wire, and so a stray copy pasted back in is recognisable.
LEGACY_HARDCODED_TIMEZONE_ID = "Russian Standard Time"

# Object keys whose value is a timezone id, in either configuration
# response. Matched case-insensitively on the *whole* key, never as a
# substring: "TimeZoneDefinition" and "timeZoneOffsets" are structural
# containers whose value is a dict, and treating them as hits would return
# the walk's own furniture.
_TIMEZONE_KEY_HINTS = frozenset({
    "timezone",
    "usertimezone",
    "defaulttimezone",
    "timezoneid",
    "timezonename",
    "tz",
})

# EWS `GetUserConfiguration` answers with a Dictionary of
# {DictionaryKey, DictionaryValue} pairs rather than named object keys, so
# the key we are looking for arrives as a *value*. Same whole-key matching.
_DICTIONARY_KEY_HINTS = _TIMEZONE_KEY_HINTS

# How deep the walk goes before giving up. A configuration response is a
# handful of levels; an unbounded walk over an unknown shape is how a
# tolerant parser turns into a hang.
_MAX_WALK_DEPTH = 8

# A timezone id is a short, printable, single-line label -- Windows ids carry
# spaces and dots ("W. Europe Standard Time"), IANA ids carry slashes and
# underscores ("America/Argentina/Buenos_Aires"), and UTC offsets in either
# style carry "+", "-" and ":". Anything else (a JSON fragment, a GUID-laden
# blob, a sentence) is not an id and must not reach the wire.
_TIMEZONE_ID_SHAPE = re.compile(r"^[A-Za-z0-9 .,'+:/_()-]{2,80}$")

# Values that pass the shape test but say "nothing was configured". Compared
# case-insensitively after stripping.
_EMPTY_SENTINELS = frozenset({"", "none", "null", "unspecified", "unknown", "default"})


class TimezoneResolution(NamedTuple):
    """Which timezone id goes on the wire, and how it was decided.

    `source` is one of SOURCE_ENV / SOURCE_MAILBOX / SOURCE_FALLBACK.
    `detail` explains a fallback (why discovery produced nothing) and is
    empty otherwise -- tools surface both so an unattended run's output
    records the zone it actually used, which is the part the hardcode
    never did.
    """

    timezone_id: str
    source: str
    detail: str = ""

    @property
    def is_fallback(self) -> bool:
        return self.source == SOURCE_FALLBACK


def looks_like_timezone_id(value: Any) -> bool:
    """True if `value` could be a timezone id Exchange might accept.

    Deliberately permissive about *which* zone and strict about being an
    id at all: see the module docstring on why no zone table lives here.
    """
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    if stripped.lower() in _EMPTY_SENTINELS:
        return False
    return bool(_TIMEZONE_ID_SHAPE.match(stripped))


def timezone_id_from_env(env: Mapping[str, str] | None = None) -> str | None:
    """The operator's `EXCHANGE_TIMEZONE`, or None if unset/unusable.

    An unusable value is treated as unset rather than as an error: this is
    read on the way to building a request, and refusing to serve because
    of a typo in an *optional* override would be a worse failure than
    falling through to discovery.
    """
    source = os.environ if env is None else env
    raw = source.get(ENV_VAR, "")
    return raw.strip() if looks_like_timezone_id(raw) else None


def timezone_id_from_config(payload: Any) -> str | None:
    """Pull a timezone id out of either OWA configuration response shape.

    Tries the EWS Dictionary form first (`GetUserConfiguration`, whose id
    arrives as a dictionary *value*), then the named-key form
    (`GetOwaUserConfiguration`). Returns None when the payload has no
    plausible id in it -- an unrecognised shape must read as "discovery
    found nothing", never as a guess.
    """
    return _from_dictionary_entries(payload, 0) or _from_named_keys(payload, 0)


def _from_dictionary_entries(node: Any, depth: int) -> str | None:
    """Find a {DictionaryKey: <hint>, DictionaryValue: <id>} pair anywhere in `node`."""
    if depth > _MAX_WALK_DEPTH:
        return None
    if isinstance(node, dict):
        key = _dictionary_entry_text(node.get("DictionaryKey"))
        if key and key.lower() in _DICTIONARY_KEY_HINTS:
            value = _dictionary_entry_text(node.get("DictionaryValue"))
            if value and looks_like_timezone_id(value):
                return value.strip()
        for child in node.values():
            found = _from_dictionary_entries(child, depth + 1)
            if found:
                return found
        return None
    if isinstance(node, list):
        for child in node:
            found = _from_dictionary_entries(child, depth + 1)
            if found:
                return found
    return None


def _dictionary_entry_text(node: Any) -> str | None:
    """Read the string out of an EWS dictionary key/value node.

    EWS wraps both sides in `{"Type": "String", "Value": [...]}`, where
    `Value` is a *list* even for a single-valued entry, but this backend
    has been seen flattening wire shapes elsewhere (`GetFolder` returns a
    non-EWS response entirely), so a bare string is accepted too.
    """
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return None
    value = node.get("Value")
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        for entry in value:
            if isinstance(entry, str):
                return entry
    return None


def _from_named_keys(node: Any, depth: int) -> str | None:
    """Find a `{"TimeZone": "<id>"}`-style entry anywhere in `node`."""
    if depth > _MAX_WALK_DEPTH:
        return None
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str) and key.lower() in _TIMEZONE_KEY_HINTS:
                # A hint key whose value is a container (TimeZone: {Id: ...})
                # still counts -- recurse into it rather than rejecting the
                # branch, since the id is one level down.
                if looks_like_timezone_id(value):
                    return value.strip()
                nested = _identifier_in(value, depth + 1)
                if nested:
                    return nested
        for value in node.values():
            found = _from_named_keys(value, depth + 1)
            if found:
                return found
        return None
    if isinstance(node, list):
        for child in node:
            found = _from_named_keys(child, depth + 1)
            if found:
                return found
    return None


def _identifier_in(node: Any, depth: int) -> str | None:
    """The `Id`/`Name`/`Value` of a timezone container node."""
    if depth > _MAX_WALK_DEPTH or not isinstance(node, dict):
        return None
    for key in ("Id", "Name", "Value", "id", "name", "value"):
        candidate = node.get(key)
        if looks_like_timezone_id(candidate):
            return candidate.strip()
    return None


def choose(env_value: str | None, mailbox_value: str | None, *, detail: str = "") -> TimezoneResolution:
    """Decide the zone from an override and a discovered value.

    Kept separate from any transport so the precedence rule -- the part
    that has to stay right -- is testable without a mailbox. `detail`
    describes why discovery came back empty and is only reported when the
    fallback is actually used.
    """
    if looks_like_timezone_id(env_value):
        return TimezoneResolution(env_value.strip(), SOURCE_ENV)
    if looks_like_timezone_id(mailbox_value):
        return TimezoneResolution(mailbox_value.strip(), SOURCE_MAILBOX)
    return TimezoneResolution(
        FALLBACK_TIMEZONE_ID,
        SOURCE_FALLBACK,
        detail or f"the mailbox timezone could not be read; set {ENV_VAR} to override",
    )


def time_zone_context(timezone_id: str) -> dict:
    """The `TimeZoneContext` block OWA request headers carry."""
    return {
        "__type": "TimeZoneContext:#Exchange",
        "TimeZoneDefinition": {
            "__type": "TimeZoneDefinitionType:#Exchange",
            "Id": timezone_id,
        },
    }


def request_header(server_version: str, timezone_id: str | None = None) -> dict:
    """A `JsonRequestHeaders` block, with a `TimeZoneContext` when given a zone.

    The single builder every request in this package goes through. Passing
    `timezone_id=None` omits the block entirely, which is what the task
    *reads* want: they rely on no conversion happening so a UTC-midnight
    date round-trips exactly (see `tools/tasks.py`).
    """
    header = {
        "__type": "JsonRequestHeaders:#Exchange",
        "RequestServerVersion": server_version,
    }
    if timezone_id:
        header["TimeZoneContext"] = time_zone_context(timezone_id)
    return header


def describe(resolution: TimezoneResolution) -> str:
    """One-line human-readable summary, for the startup banner and tool output."""
    if resolution.source == SOURCE_ENV:
        return f"{resolution.timezone_id} (from {ENV_VAR})"
    if resolution.source == SOURCE_MAILBOX:
        return f"{resolution.timezone_id} (from the mailbox's OWA configuration)"
    return f"{resolution.timezone_id} (fallback: {resolution.detail})"
