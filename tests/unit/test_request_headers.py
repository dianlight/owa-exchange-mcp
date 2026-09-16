"""Pure-logic tests for the one request-header builder, and for the two headers
that must *not* look like the others. No mailbox, no browser.

Issue #8's own acceptance criterion was "grep for the literal `Russian Standard
Time`; there should be no remaining hardcoded occurrence when this is done", so
`test_no_module_hardcodes_a_timezone_id` is that grep, as a test. It is the only
assertion here that would catch the *next* copy — a tool module added later with
its own inline `TimeZoneContext` passes every other test in this file.

The rest defend the two asymmetries, both of which look like untidiness and are
not:

1. **`tasks.py`'s `_READ_HEADER` carries no `TimeZoneContext`, on purpose.** Task
   `DueDate`/`StartDate` are stored as UTC midnight and written `Z`-qualified, so
   a read that carried a context would have Exchange convert them and hand back
   the previous day for any mailbox west of UTC. Making the read and write
   headers symmetrical is the obvious tidying change and it silently breaks task
   dates by a day, which is why it is pinned rather than commented.
2. **`calendar._resolve_attendee`'s header carries none either.** `ResolveNames`
   has no timestamps, so a context there is meaningless — and this suite exists
   partly because the first pass of the #8 migration added one to it by accident
   (a blanket string replace matched a header that had never had one). Harmless
   in effect, but an unintended change to a working call is worth a guard.

What is deliberately *not* asserted: the exact `RequestServerVersion` each tool
sends. CLAUDE.md fixes those (`Exchange2013` for reads, `V2017_08_18` for
writes) and they are visible in the payloads; pinning every one here would make
this file a second copy of that table.

Run:
    python -m tests.unit.test_request_headers
"""

import pathlib
import re
import sys

from exchange_mcp.mailbox_timezone import (
    UTC_TIMEZONE,
    request_header,
    resolve_mailbox_timezone,
)
from exchange_mcp.tools import folders as fo
from exchange_mcp.tools import tasks as tk

FAILURES: list[str] = []

W_EUROPE = "W. Europe Standard Time"


def check(label: str, actual, expected) -> None:
    if actual != expected:
        FAILURES.append(f"{label}: expected {expected!r}, got {actual!r}")


class FakeClient:
    """Only what a header builder touches."""

    def __init__(self, tz):
        self._tz = tz

    def mailbox_timezone(self):
        return self._tz

    def request_header(self, server_version, *, with_timezone=True):
        return request_header(server_version, self._tz if with_timezone else None)


def tz_id(header: dict):
    ctx = header.get("TimeZoneContext")
    if ctx is None:
        return None
    return ctx["TimeZoneDefinition"]["Id"]


def local_tz():
    tz = resolve_mailbox_timezone(W_EUROPE, override="")
    return tz if tz.source == "mailbox_configuration" else None


# ------------------------------------------------------------------
# The builder
# ------------------------------------------------------------------

def test_builder_stamps_the_resolved_zone() -> None:
    """The wire id, not the IANA one: EWS speaks Windows ids, and `wire_id`
    already encodes the decision about which of the two is safe to send."""
    tz = local_tz()
    header = request_header("V2017_08_18", tz)
    check("the server version is carried through", header["RequestServerVersion"], "V2017_08_18")
    check("the type annotation is present",
          header["__type"], "JsonRequestHeaders:#Exchange")
    check("the mailbox's zone is stamped", tz_id(header), W_EUROPE)

    # A zone with no Windows id (an IANA override, or a degraded resolution)
    # sends UTC rather than an id this backend has not been tested with.
    iana_only = resolve_mailbox_timezone("", override="Europe/Rome")
    check("an IANA-only zone sends UTC on the wire",
          tz_id(request_header("V2017_08_18", iana_only)), "UTC")


def test_builder_omits_the_context_when_asked() -> None:
    """`tz=None` is a requirement, not a convenience — see the task reads."""
    header = request_header("Exchange2013", None)
    check("no TimeZoneContext key at all", "TimeZoneContext" in header, False)
    check("still a valid header", header["RequestServerVersion"], "Exchange2013")


def test_builder_returns_a_fresh_dict_each_call() -> None:
    """The constants this replaced were shared mutable dicts handed straight to
    the transport, so a builder that returned a cached one would let any caller
    that mutated its header leak into the next request."""
    tz = local_tz()
    first = request_header("V2017_08_18", tz)
    second = request_header("V2017_08_18", tz)
    check("not the same object", first is second, False)
    check("nor the same nested context",
          first["TimeZoneContext"] is second["TimeZoneContext"], False)

    first["TimeZoneContext"]["TimeZoneDefinition"]["Id"] = "Tampered"
    check("mutating one does not affect the next",
          tz_id(request_header("V2017_08_18", tz)), W_EUROPE)


def test_utc_floor_still_produces_a_usable_header() -> None:
    """The degradation floor must not produce a header with an empty id — that
    would be a malformed request rather than a UTC one."""
    check("the floor sends UTC", tz_id(request_header("Exchange2013", UTC_TIMEZONE)), "UTC")


# ------------------------------------------------------------------
# The two headers that must stay context-free
# ------------------------------------------------------------------

def test_task_read_header_has_no_timezone_context() -> None:
    """**The load-bearing asymmetry.** Task dates are stored as UTC midnight and
    written `Z`-qualified; a read carrying a `TimeZoneContext` would have
    Exchange convert them into the mailbox's zone and return the previous day for
    any mailbox west of UTC. Making the read header match the write header is the
    obvious tidy-up and it breaks task dates by a day."""
    check("no context on the read header", tz_id(tk._READ_HEADER), None)
    check("...and none by any spelling", "TimeZoneContext" in tk._READ_HEADER, False)
    check("it is still a read-version header",
          tk._READ_HEADER["RequestServerVersion"], "Exchange2013")


def test_resolve_attendee_header_has_no_timezone_context() -> None:
    """`ResolveNames` carries no timestamps, so a context there means nothing —
    and the first pass of this migration added one to it by accident, via a
    blanket replace that matched a header which had never had one. Read out of
    the source because the header is built inline inside the function."""
    source = pathlib.Path("exchange_mcp/tools/calendar.py").read_text(encoding="utf-8")
    start = source.index("def _resolve_attendee(")
    body = source[start:start + 1200]
    check("no TimeZoneContext in _resolve_attendee's payload",
          "TimeZoneContext" in body, False)
    check("no request_header call either (it would add one)",
          "request_header" in body, False)


# ------------------------------------------------------------------
# The write headers now follow the mailbox
# ------------------------------------------------------------------

def test_task_write_header_carries_the_mailbox_zone() -> None:
    """Issue #8's user-visible symptom lived here: `ReminderDueBy` is written as
    an *unqualified* wall clock, so this context is what decides which instant
    "09:30" means. Hardcoded to UTC+3 it stored 09:30 Moscow — three hours early
    anywhere else."""
    tz = local_tz()
    header = tk._write_header(FakeClient(tz))
    check("the mailbox's zone is sent", tz_id(header), W_EUROPE)
    check("on the write version", header["RequestServerVersion"], "V2017_08_18")


def test_reminder_is_written_unqualified_so_the_context_governs_it() -> None:
    """The other half of that fix, and the part that would silently undo it: if
    `_reminder_datetime` ever gained a `Z` or an offset, the `TimeZoneContext`
    would stop applying and the reminder would be stored in UTC no matter which
    zone we sent. The two only work together."""
    written = tk._reminder_datetime("2026-09-17 09:30")
    check("the requested wall clock is preserved", written.startswith("2026-09-17T09:30:00"), True)
    check("no Z suffix", written.endswith("Z"), False)
    check("no explicit offset", bool(re.search(r"[+-]\d\d:?\d\d$", written)), False)


def test_folder_header_carries_the_mailbox_zone() -> None:
    """Folder actions have no timestamps, so this is cosmetic in effect — but it
    was a module-level constant, and a copy no per-call value can reach is what
    made #8 a codebase-wide change instead of a one-line fix."""
    tz = local_tz()
    header = fo._header(FakeClient(tz))
    check("the mailbox's zone is sent", tz_id(header), W_EUROPE)
    check("on the read version", header["RequestServerVersion"], "Exchange2013")


# ------------------------------------------------------------------
# Issue #8's own acceptance criterion
# ------------------------------------------------------------------

_TZ_ID_IN_PAYLOAD = re.compile(r'"Id"\s*:\s*"(?!\{)[A-Z][^"]*"')


def test_no_module_hardcodes_a_timezone_id() -> None:
    """Issue #8: "grep for the literal ... there should be no remaining hardcoded
    occurrence when this is done."

    This is the only test here that catches the *next* copy — a module added
    later with its own inline `TimeZoneContext` passes everything else in this
    file. Scoped to `TimeZoneDefinition` blocks so an unrelated `"Id"` (a folder
    id, an item id) is not a false positive, and `mailbox_timezone.py` is exempt
    because the `WINDOWS_TO_IANA` row for Moscow is a legitimate table entry.
    """
    offenders = []
    for path in sorted(pathlib.Path("exchange_mcp").rglob("*.py")):
        if path.name == "mailbox_timezone.py":
            continue
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r'"TimeZoneDefinition"\s*:\s*\{(.{0,200}?)\}', text, re.S):
            block = match.group(1)
            literal = re.search(r'"Id"\s*:\s*"([^"]+)"', block)
            if literal:
                line = text[: match.start()].count("\n") + 1
                offenders.append(f"{path}:{line} hardcodes Id={literal.group(1)!r}")

    check("no hardcoded TimeZoneDefinition id anywhere in the package", offenders, [])


def test_the_builder_is_the_only_place_that_writes_a_timezone_context() -> None:
    """The structural half of the same rule: a `TimeZoneContext` key should only
    be *constructed* in `mailbox_timezone.request_header`. Anywhere else means a
    module has grown a private copy again, even if its id happens to be dynamic.
    """
    writers = []
    for path in sorted(pathlib.Path("exchange_mcp").rglob("*.py")):
        if path.name == "mailbox_timezone.py":
            continue
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r'"TimeZoneContext"\s*:', text):
            line = text[: match.start()].count("\n") + 1
            writers.append(f"{path}:{line}")
    check("only mailbox_timezone.py builds a TimeZoneContext", writers, [])


def main() -> bool:
    if local_tz() is None:
        print("test_request_headers: FAILED - no timezone database available, so the "
              "mailbox timezone cannot be resolved (is `tzdata` installed?)")
        return False

    for test in (
        test_builder_stamps_the_resolved_zone,
        test_builder_omits_the_context_when_asked,
        test_builder_returns_a_fresh_dict_each_call,
        test_utc_floor_still_produces_a_usable_header,
        test_task_read_header_has_no_timezone_context,
        test_resolve_attendee_header_has_no_timezone_context,
        test_task_write_header_carries_the_mailbox_zone,
        test_reminder_is_written_unqualified_so_the_context_governs_it,
        test_folder_header_carries_the_mailbox_zone,
        test_no_module_hardcodes_a_timezone_id,
        test_the_builder_is_the_only_place_that_writes_a_timezone_context,
    ):
        test()

    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for failure in FAILURES:
            print(f"  - {failure}")
        return False
    print("test_request_headers: all checks passed")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
