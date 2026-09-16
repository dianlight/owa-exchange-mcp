"""Pure-logic tests for the request-header invariants that hold *at the call
sites*, as opposed to inside the builder. No mailbox, no browser.

`test_mailbox_timezone.py` already covers `mailbox_timezone.request_header()`
itself — that it stamps the resolved zone, that `with_timezone=False` omits the
context, and that the issue-#8 literal is gone from every module (with a
meta-check that the guard can still fire). This file covers what that one
cannot: whether each *caller* asks for the right thing. Every invariant below is
currently satisfied and none of them is pinned, so each one is a change someone
could make while tidying, that no existing test would catch, and whose symptom
is a plausible-looking wrong answer rather than an error.

Four of them:

1. **Task reads must omit the `TimeZoneContext`** (`tasks._read_header`). Its
   docstring says the omission is load-bearing and why; nothing checks that the
   call keeps `with_timezone=False`. Dropping that argument makes Exchange
   convert the UTC-midnight `DueDate`/`StartDate` this module writes, returning
   the previous day for any mailbox west of UTC — the classic off-by-one-day task
   date. `test_task_lifecycle.py` would catch it, but only against a live
   mailbox, and only from a mailbox whose offset is negative.
2. **`calendar._resolve_attendee` must send no context.** `ResolveNames` carries
   no timestamps, so one is meaningless there. Pinned because it is the header a
   bulk migration hits by accident: a blanket replace over
   `"RequestServerVersion": "V2017_08_18"` matches it, and it is the one header
   at that version which never had a context. (Observed: an earlier attempt at
   the #8 migration did exactly this. Harmless in effect, caught before commit.)
3. **A reminder must stay unqualified.** `_reminder_datetime` emits
   `...T09:30:00.000` with no `Z` and no offset *precisely so* the write's
   `TimeZoneContext` decides which instant it means. Give it a `Z` and the
   context stops applying and the reminder is stored in UTC whatever zone is
   sent — issue #8's symptom back, by the other door, with the fix still visibly
   in place. The zone and the format are one mechanism; only one half of it is
   currently tested.
4. **Only `mailbox_timezone.py` may construct a `TimeZoneContext`.**
   `test_no_hardcoded_zone_remains` catches a module reintroducing the *legacy
   literal*; it does not catch one that builds its own context from some other
   id. That is the shape the next copy would take, now that the builder exists
   to copy from.

The classic-OWA `GetUserAvailability` branch that this file originally also
covered has its own suite now, `test_availability_legacy_branch.py` — same
change, separated because "what a caller asks the header builder for" and
"what an unreachable branch does with a response" fail for unrelated reasons
and are read by different people.

Run:
    python -m tests.unit.test_header_invariants
"""

import datetime as dt
import pathlib
import re
import sys

from exchange_mcp import availability_frame as frame
from exchange_mcp import mailbox_timezone as mtz
from exchange_mcp.browser_session import BearerModeRequiredError
from exchange_mcp.tools import availability as av
from exchange_mcp.tools import tasks as tk

FAILURES: list[str] = []

W_EUROPE = "W. Europe Standard Time"
DAY = dt.date(2026, 9, 17)


def check(label: str, actual, expected) -> None:
    if actual != expected:
        FAILURES.append(f"{label}: expected {expected!r}, got {actual!r}")


def tz_id(header: dict):
    ctx = header.get("TimeZoneContext")
    return None if ctx is None else ctx["TimeZoneDefinition"]["Id"]


def zone():
    """A resolved W. Europe zone, or None when there is no IANA database."""
    z = frame.resolve_zone(W_EUROPE, source=frame.SOURCE_MAILBOX)
    return z if z.tz is not None else None


class FakeClient:
    """Only the surface these call sites touch. Records every request."""

    def __init__(self, *, timezone_id=W_EUROPE, availability=None, bearer=False):
        self.timezone_id = timezone_id
        self.availability = availability or {"Body": {"FreeBusyResponseArray": []}}
        self.bearer = bearer
        self.requests: list[tuple] = []
        self.schedule_calls: list[dict] = []

    # Matches OWAClient.request_header, so a change to the real builder's
    # contract shows up here rather than being papered over by a stub.
    def request_header(self, server_version, *, with_timezone=True):
        return mtz.request_header(server_version, self.timezone_id if with_timezone else None)

    def mailbox_timezone(self):
        return self.timezone_id

    def get_schedule(self, emails, start, end, *, tz_id=None, interval_minutes=30):
        self.schedule_calls.append({"tz_id": tz_id, "start": start, "end": end})
        if not self.bearer:
            # What the real client raises on a tenant with no substrate surface.
            # This is the only way into the legacy branch.
            raise BearerModeRequiredError("classic OWA has no substrate surface")
        return []

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


# ------------------------------------------------------------------
# 1. Task reads must omit the context
# ------------------------------------------------------------------

def test_task_reads_omit_the_timezone_context() -> None:
    """`tasks._read_header` explains why in its docstring; this is the check.

    Losing the `with_timezone=False` would have Exchange convert the
    UTC-midnight dates this module writes, so `get_task` reports the previous
    day for any mailbox west of UTC. Nothing else in the unit suite notices:
    the builder's own omission test passes, because the builder still omits when
    asked — the change is at the call site.
    """
    header = tk._read_header(FakeClient())
    check("no TimeZoneContext on a task read", tz_id(header), None)
    check("...and no such key at all", "TimeZoneContext" in header, False)
    check("still the read server version", header["RequestServerVersion"], "Exchange2013")

    # The write header is the deliberate opposite, asserted alongside so the
    # asymmetry reads as intentional rather than as one of them being stale.
    write = tk._write_header(FakeClient())
    check("a task write does carry the zone", tz_id(write), W_EUROPE)
    check("on the write server version", write["RequestServerVersion"], "V2017_08_18")


# ------------------------------------------------------------------
# 2. _resolve_attendee must send no context
# ------------------------------------------------------------------

def test_resolve_attendee_sends_no_timezone_context() -> None:
    """Read out of the source because the header is built inline in the function
    and `_resolve_attendee` cannot be called without a transport. A source check
    is enough here: the invariant is "this payload contains no TimeZoneContext",
    which is exactly what the text says."""
    source = pathlib.Path(av.__file__).with_name("calendar.py").read_text(encoding="utf-8")
    start = source.index("def _resolve_attendee(")
    body = source[start:source.index("\ndef ", start + 1)]
    check("no TimeZoneContext in _resolve_attendee", "TimeZoneContext" in body, False)
    check("and it does not call the builder (which would add one)",
          "request_header" in body, False)


# ------------------------------------------------------------------
# 3. A reminder must stay unqualified
# ------------------------------------------------------------------

def test_reminder_is_written_unqualified() -> None:
    """The other half of the reminder fix. The zone on the header only governs
    the value because the value carries no zone of its own."""
    written = tk._reminder_datetime("2026-09-17 09:30")
    check("the requested wall clock survives", written[:19], "2026-09-17T09:30:00")
    check("no Z suffix", written.endswith("Z"), False)
    check("no explicit offset", bool(re.search(r"[+-]\d\d:?\d\d$", written)), False)

    # Seconds-optional input, same rule.
    check("no Z on the seconds form",
          tk._reminder_datetime("2026-09-17 09:30:15").endswith("Z"), False)


# ------------------------------------------------------------------
# 4. Only one module may build a TimeZoneContext
# ------------------------------------------------------------------

def test_only_mailbox_timezone_builds_a_timezone_context() -> None:
    """The structural half of issue #8's rule.

    `test_no_hardcoded_zone_remains` catches a module reintroducing the legacy
    *literal*. This catches one that builds its own context at all — which is
    the shape the next copy takes now that there is a builder to copy from, and
    whose id could be wrong in some new way the literal check knows nothing
    about.
    """
    package = pathlib.Path(mtz.__file__).parent
    builders: list[str] = []
    for path in sorted(package.rglob("*.py")):
        if path.name == "mailbox_timezone.py":
            continue
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r'"TimeZoneContext"\s*:', text):
            line = text[: match.start()].count("\n") + 1
            builders.append(f"{path.relative_to(package)}:{line}")
    check("only mailbox_timezone.py constructs a TimeZoneContext", builders, [])

    # Prove the guard can fire, in the manner test_no_hardcoded_zone_remains
    # established: a check that cannot fail is worthless.
    synthetic = 'H = {"TimeZoneContext": {"TimeZoneDefinition": {"Id": x}}}'
    check("a dynamic private copy would be caught",
          bool(re.search(r'"TimeZoneContext"\s*:', synthetic)), True)


def main() -> bool:
    if zone() is None:
        print("test_header_invariants: FAILED - no IANA timezone database, so the "
              "mailbox zone cannot be resolved (is `tzdata` installed?)")
        return False

    for test in (
        test_task_reads_omit_the_timezone_context,
        test_resolve_attendee_sends_no_timezone_context,
        test_reminder_is_written_unqualified,
        test_only_mailbox_timezone_builds_a_timezone_context,
    ):
        test()

    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for failure in FAILURES:
            print(f"  - {failure}")
        return False
    print("test_header_invariants: all checks passed")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
