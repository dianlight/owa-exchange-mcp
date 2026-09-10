"""Calendar tools for the Exchange MCP server.

Provides MCP tools for calendar event retrieval, meeting creation,
update, cancellation, and meeting response management via OWA API.
"""

import calendar as _cal
import html as html_mod
import json
import re
import uuid
from datetime import datetime, timedelta

from mcp.server.fastmcp import Context

from exchange_mcp.server import mcp, AppContext
from exchange_mcp.owa_client import OWAClient, SessionExpiredError
from exchange_mcp.utils import html_to_text, parse_iso_datetime, extract_links_from_html


def _get_client(ctx: Context) -> OWAClient:
    """Extract the OWAClient from the MCP lifespan context."""
    app_ctx: AppContext = ctx.request_context.lifespan_context
    return app_ctx.client


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


def _get_event_details(client: OWAClient, item_id: str) -> dict:
    """Get full event details (body, organizer, attendees) via GetItem."""
    payload = {
        "__type": "GetItemJsonRequest:#Exchange",
        "Header": {
            "__type": "JsonRequestHeaders:#Exchange",
            "RequestServerVersion": "Exchange2013",
        },
        "Body": {
            "__type": "GetItemRequest:#Exchange",
            "ItemShape": {
                "__type": "ItemResponseShape:#Exchange",
                "BaseShape": "AllProperties",
            },
            "ItemIds": [
                {"__type": "ItemId:#Exchange", "Id": item_id}
            ],
        },
    }

    data = client.request("GetItem", payload)
    result = {
        "organizer": "",
        "organizer_email": "",
        "location": "",
        "body": "",
        "attendees_required": [],
        "attendees_optional": [],
        "categories": [],
        "subject": "",
        "start": "",
        "end": "",
        "is_all_day": False,
        "is_cancelled": False,
        "is_recurring": False,
        "calendar_item_type": "",
        "sensitivity": "Normal",
        "my_response": "",
        "change_key": "",
        "recurrence": {},
    }

    for msg in client.extract_items(data):
        if "Items" not in msg:
            continue
        for item in msg["Items"]:
            result["categories"] = item.get("Categories", [])
            result["subject"] = item.get("Subject", "") or "(No subject)"
            result["start"] = item.get("Start", "")
            result["end"] = item.get("End", "")
            result["is_all_day"] = item.get("IsAllDayEvent", False)
            result["is_cancelled"] = item.get("IsCancelled", False)
            result["calendar_item_type"] = item.get("CalendarItemType", "")
            result["is_recurring"] = result["calendar_item_type"] == "RecurringMaster"
            result["sensitivity"] = item.get("Sensitivity", "Normal")
            result["my_response"] = item.get("MyResponseType", "")
            result["change_key"] = item.get("ItemId", {}).get("ChangeKey", "")
            result["recurrence"] = item.get("Recurrence", {}) or {}
            # Location
            result["location"] = item.get("Location", "")
            if not result["location"]:
                enhanced = item.get("EnhancedLocation", {})
                if enhanced:
                    result["location"] = enhanced.get("DisplayName", "")

            # Body
            body_data = item.get("Body", {})
            if body_data:
                body_text = body_data.get("Value", "")
                if body_data.get("BodyType") == "HTML":
                    body_text = html_to_text(body_text)
                result["body"] = body_text.strip()

            # Organizer with SMTP email
            organizer = item.get("Organizer", {}).get("Mailbox", {})
            if organizer:
                name = organizer.get("Name", "")
                addr = organizer.get("EmailAddress", "")
                if addr and not addr.startswith("/O="):
                    result["organizer"] = f"{name} <{addr}>" if name else addr
                    result["organizer_email"] = addr
                else:
                    result["organizer"] = name

            # Required attendees with SMTP emails
            for a in item.get("RequiredAttendees", []) or []:
                mailbox = a.get("Mailbox", {})
                name = mailbox.get("Name", "")
                addr = mailbox.get("EmailAddress", "")
                response = a.get("ResponseType", "")

                if name or addr:
                    if addr and not addr.startswith("/O="):
                        entry = f"{name} <{addr}>" if name else addr
                    else:
                        entry = name
                    if response and response not in ("Unknown", "Organizer"):
                        entry += f" [{response}]"
                    result["attendees_required"].append(entry)

            # Optional attendees with SMTP emails
            for a in item.get("OptionalAttendees", []) or []:
                mailbox = a.get("Mailbox", {})
                name = mailbox.get("Name", "")
                addr = mailbox.get("EmailAddress", "")
                response = a.get("ResponseType", "")

                if name or addr:
                    if addr and not addr.startswith("/O="):
                        entry = f"{name} <{addr}>" if name else addr
                    else:
                        entry = name
                    if response and response not in ("Unknown", "Organizer"):
                        entry += f" [{response}]"
                    result["attendees_optional"].append(entry)

            return result

    return result


def _get_full_event(client: OWAClient, item_id: str) -> dict:
    """Get full event details for update_meeting preservation."""
    payload = {
        "__type": "GetItemJsonRequest:#Exchange",
        "Header": {
            "__type": "JsonRequestHeaders:#Exchange",
            "RequestServerVersion": "Exchange2013",
        },
        "Body": {
            "__type": "GetItemRequest:#Exchange",
            "ItemShape": {
                "__type": "ItemResponseShape:#Exchange",
                "BaseShape": "AllProperties",
            },
            "ItemIds": [
                {"__type": "ItemId:#Exchange", "Id": item_id}
            ],
        },
    }

    data = client.request("GetItem", payload)
    for msg in client.extract_items(data):
        if "Items" not in msg:
            continue
        for item in msg["Items"]:
            result = {
                "subject": item.get("Subject", ""),
                "start": item.get("Start", ""),
                "end": item.get("End", ""),
                "is_all_day": item.get("IsAllDayEvent", False),
                "sensitivity": item.get("Sensitivity", "Normal"),
                "location": "",
                "body_html": "",
                "resolved_required": [],
                "resolved_optional": [],
            }

            # Location
            loc = item.get("Location", "")
            if not loc:
                enhanced = item.get("EnhancedLocation", {})
                if enhanced:
                    loc = enhanced.get("DisplayName", "")
            result["location"] = loc

            # Body (keep HTML)
            body_data = item.get("Body", {})
            if body_data:
                result["body_html"] = body_data.get("Value", "")

            # Required attendees as resolved dicts
            for a in item.get("RequiredAttendees", []) or []:
                mailbox = a.get("Mailbox", {})
                name = mailbox.get("Name", "")
                addr = mailbox.get("EmailAddress", "")
                if addr and not addr.startswith("/O="):
                    result["resolved_required"].append({
                        "Mailbox": {
                            "Name": name,
                            "EmailAddress": addr,
                            "RoutingType": "SMTP",
                        }
                    })

            # Optional attendees
            for a in item.get("OptionalAttendees", []) or []:
                mailbox = a.get("Mailbox", {})
                name = mailbox.get("Name", "")
                addr = mailbox.get("EmailAddress", "")
                if addr and not addr.startswith("/O="):
                    result["resolved_optional"].append({
                        "Mailbox": {
                            "Name": name,
                            "EmailAddress": addr,
                            "RoutingType": "SMTP",
                        }
                    })

            return result

    return {"error": "Meeting not found"}


def _resolve_attendee(client: OWAClient, email: str) -> dict:
    """Resolve an email to attendee details via ResolveNames.

    Uses V2017_08_18 RequestServerVersion to match the original
    create-meeting.py behaviour.
    """
    payload = {
        "Header": {
            "__type": "JsonRequestHeaders:#Exchange",
            "RequestServerVersion": "V2017_08_18",
        },
        "Body": {
            "__type": "ResolveNamesRequest:#Exchange",
            "UnresolvedEntry": email,
            "ReturnFullContactData": True,
            "ContactDataShape": "Default",
        },
    }

    try:
        data = client.request("ResolveNames", payload)
        items = client.extract_items(data)
        if items and "ResolutionSet" in items[0]:
            resolutions = items[0]["ResolutionSet"].get("Resolutions", [])
            if resolutions:
                mailbox = resolutions[0].get("Mailbox", {})
                return {
                    "Mailbox": {
                        "Name": mailbox.get("Name", email),
                        "EmailAddress": mailbox.get("EmailAddress", email),
                        "RoutingType": "SMTP",
                    }
                }
    except Exception:
        pass

    # Fallback
    return {
        "Mailbox": {
            "Name": email,
            "EmailAddress": email,
            "RoutingType": "SMTP",
        }
    }


def _resolve_attendee_list(client: OWAClient, emails: list[str]) -> list[dict]:
    """Resolve a list of email strings into attendee dicts."""
    attendees = []
    for email in emails:
        email = email.strip()
        if email:
            attendees.append(_resolve_attendee(client, email))
    return attendees


def _build_html_body(description: str | None) -> str:
    """Build the HTML body for a calendar item, matching create-meeting.py."""
    body = (
        '<html><head><meta http-equiv="Content-Type" '
        'content="text/html; charset=UTF-8"></head><body dir="ltr">'
    )
    if description:
        desc_escaped = html_mod.escape(description).replace("\n", "<br>")
        body += (
            '<div style="font-size:12pt;color:#000000;'
            f'font-family:Calibri,Helvetica,sans-serif;">{desc_escaped}</div>'
        )
    else:
        body += (
            '<div style="font-size:12pt;color:#000000;'
            'font-family:Calibri,Helvetica,sans-serif;"><p><br></p></div>'
        )
    body += "</body></html>"
    return body


# ------------------------------------------------------------------
# Tool 1: get_calendar_events
# ------------------------------------------------------------------


def _filter_items_by_date_range(
    items: list[dict], start_dt: datetime, end_dt_exclusive: datetime
) -> list[dict]:
    """Keep only items overlapping [start_dt, end_dt_exclusive).

    FindItem's CalendarView StartDate/EndDate has no filtering effect on this
    OWA backend -- it always returns every item in the folder regardless of
    the requested window (confirmed empirically: a 1-day window and a 200-year
    window returned the identical item count). This filters client-side using
    each item's own Start/End instead.
    """
    kept = []
    for item in items:
        try:
            item_start = parse_iso_datetime(item.get("Start", ""))
            item_end = parse_iso_datetime(item.get("End", "") or item.get("Start", ""))
        except (ValueError, TypeError):
            continue
        if item_start < end_dt_exclusive and item_end > start_dt:
            kept.append(item)
    return kept


_MAX_RECURRENCE_ITERATIONS = 2000

_WEEKDAY_NAMES = {
    "Sunday": 6, "Monday": 0, "Tuesday": 1, "Wednesday": 2,
    "Thursday": 3, "Friday": 4, "Saturday": 5,
}


_PATTERN_KEYS = (
    "DailyRecurrence", "WeeklyRecurrence",
    "AbsoluteMonthlyRecurrence", "RelativeMonthlyRecurrence",
    "AbsoluteYearlyRecurrence", "RelativeYearlyRecurrence",
)
_RANGE_KEYS = ("NoEndRecurrence", "EndDateRecurrence", "NumberedRecurrence")

# Recurrence dates on this backend come as date + UTC offset and no time
# component ("2024-01-09+01:00"), which parse_iso_datetime() rejects outright
# (it only strips a tz suffix when a "T" is present). Match the leading date,
# take an optional time, and ignore the offset -- consistent with the naive
# datetimes used everywhere else in this module.
_RECURRENCE_DATE_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}):(\d{2}))?"
)


def _parse_recurrence_date(value: str) -> datetime:
    """Parse a Recurrence StartDate/EndDate to a naive datetime."""
    m = _RECURRENCE_DATE_RE.match((value or "").strip())
    if not m:
        raise ValueError(f"unrecognized recurrence date: {value!r}")
    return datetime(
        int(m.group(1)), int(m.group(2)), int(m.group(3)),
        int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0),
    )


def _recurrence_block(recurrence: dict, wrapper: str, variants: tuple) -> tuple:
    """Return (variant_name, block) for a Recurrence pattern or range.

    Handles both shapes seen in the wild:
      * this OWA backend's -- a fixed wrapper key whose variant is carried in
        `__type` ({"RecurrencePattern": {"__type": "WeeklyRecurrence:#Exchange"}})
      * plain EWS-style JSON -- the variant name used as the key itself
        ({"WeeklyRecurrence": {...}})
    """
    block = recurrence.get(wrapper)
    if isinstance(block, dict) and block:
        variant = str(block.get("__type", "")).split(":")[0]
        if variant in variants:
            return variant, block
    for key in variants:
        candidate = recurrence.get(key)
        if isinstance(candidate, dict) and candidate:
            return key, candidate
    return None, None


_DAY_OF_WEEK_INDEX = {"First": 0, "Second": 1, "Third": 2, "Fourth": 3, "Last": -1}


def _nth_weekday_of_month(year: int, month: int, weekday: int, ordinal: int) -> int | None:
    """Day-of-month for the `ordinal`-th `weekday` of a month.

    `ordinal` is 0-based (0 = first), or -1 for "last". Returns None when the
    month has no such occurrence (a 5th Friday, say) -- EWS treats that month
    as simply having no occurrence rather than clamping to the 4th.
    """
    days_in_month = _cal.monthrange(year, month)[1]
    offset = (weekday - datetime(year, month, 1).weekday()) % 7
    if ordinal >= 0:
        day = 1 + offset + ordinal * 7
        return day if day <= days_in_month else None
    day = 1 + offset
    while day + 7 <= days_in_month:
        day += 7
    return day


def _relative_pattern_weekday(pattern: dict) -> tuple[int | None, int | None]:
    """(weekday, ordinal) for a Relative{Monthly,Yearly}Recurrence, or (None, None).

    EWS also allows the pseudo-days "Day", "Weekday" and "WeekendDay" in
    DaysOfWeek. None appear in this mailbox, and each means something different
    from a plain weekday, so they degrade to "no occurrences" rather than being
    guessed at.
    """
    weekday = _WEEKDAY_NAMES.get((pattern.get("DaysOfWeek") or "").strip())
    ordinal = _DAY_OF_WEEK_INDEX.get((pattern.get("DayOfWeekIndex") or "").strip())
    if weekday is None or ordinal is None:
        return None, None
    return weekday, ordinal


def _add_months(dt: datetime, months: int) -> datetime:
    """Shift dt by `months`, clamping the day to the target month's length."""
    month_index = dt.month - 1 + months
    year = dt.year + month_index // 12
    month = month_index % 12 + 1
    day = min(dt.day, _cal.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def _expand_recurrence_occurrences(
    recurrence: dict,
    start_dt: datetime,
    end_dt_exclusive: datetime,
) -> list[datetime]:
    """Compute occurrence start datetimes for an EWS Recurrence dict,
    clipped to [start_dt, end_dt_exclusive).

    Verified against this OWA backend 2026-09-10, which does NOT use the
    plain-EWS JSON shape: the variant lives in a `__type` field under fixed
    `RecurrencePattern`/`RecurrenceRange` wrapper keys, and dates arrive as
    date + offset with no time ("2024-01-09+01:00") --

        {"RecurrencePattern": {"__type": "WeeklyRecurrence:#Exchange",
                               "Interval": 1, "DaysOfWeek": "Tuesday Thursday",
                               "FirstDayOfWeek": "Monday"},
         "RecurrenceRange":   {"__type": "EndDateRecurrence:#Exchange",
                               "StartDate": "2024-01-09+01:00",
                               "EndDate": "2024-01-13+01:00"}}

    _recurrence_block() accepts that shape and the plain-EWS one; because the
    range carries no time-of-day, callers must overlay the series master's
    own Start time (get_calendar_events does).

    Still defensive: every sub-field access uses .get() with a safe default,
    and an unrecognized pattern/range variant returns [] rather than raising,
    so an unexpected payload degrades to "no synthesized occurrences for this
    series" instead of crashing the whole get_calendar_events call.
    """
    if not recurrence:
        return []

    pattern_key, pattern = _recurrence_block(
        recurrence, "RecurrencePattern", _PATTERN_KEYS
    )
    if not pattern_key:
        return []

    range_key, rrange = _recurrence_block(
        recurrence, "RecurrenceRange", _RANGE_KEYS
    )
    if not range_key:
        return []

    try:
        series_start = _parse_recurrence_date(rrange.get("StartDate", ""))
    except (ValueError, TypeError):
        return []

    series_end = None
    max_occurrences = None
    if range_key == "EndDateRecurrence":
        try:
            series_end = _parse_recurrence_date(rrange.get("EndDate", ""))
        except (ValueError, TypeError):
            return []
    elif range_key == "NumberedRecurrence":
        try:
            max_occurrences = int(rrange.get("NumberOfOccurrences", 0))
        except (ValueError, TypeError):
            return []
        if max_occurrences <= 0:
            return []

    try:
        interval = int(pattern.get("Interval", 1)) or 1
    except (ValueError, TypeError):
        interval = 1

    occurrences: list[datetime] = []
    current = series_start
    count = 0

    if pattern_key == "DailyRecurrence":
        step = timedelta(days=interval)
        while count < _MAX_RECURRENCE_ITERATIONS:
            if series_end and current > series_end:
                break
            if max_occurrences is not None and count >= max_occurrences:
                break
            if current >= end_dt_exclusive:
                break
            if current >= start_dt:
                occurrences.append(current)
            current += step
            count += 1

    elif pattern_key == "WeeklyRecurrence":
        days_str = pattern.get("DaysOfWeek", "") or ""
        day_names = [d for d in days_str.split() if d in _WEEKDAY_NAMES]
        if not day_names:
            return []
        target_weekdays = {_WEEKDAY_NAMES[d] for d in day_names}
        week_start = current - timedelta(days=current.weekday())
        while count < _MAX_RECURRENCE_ITERATIONS:
            week_done = False
            for offset in range(7):
                candidate = week_start + timedelta(days=offset)
                if candidate.weekday() not in target_weekdays:
                    continue
                if candidate < series_start:
                    continue
                if series_end and candidate > series_end:
                    week_done = True
                    break
                if max_occurrences is not None and count >= max_occurrences:
                    week_done = True
                    break
                if candidate >= end_dt_exclusive:
                    week_done = True
                    break
                if candidate >= start_dt:
                    occurrences.append(candidate)
                count += 1
            if week_done:
                break
            week_start += timedelta(weeks=interval)

    elif pattern_key == "AbsoluteMonthlyRecurrence":
        try:
            day_of_month = int(pattern.get("DayOfMonth", current.day))
        except (ValueError, TypeError):
            day_of_month = current.day
        month_cursor = current.replace(day=1)
        while count < _MAX_RECURRENCE_ITERATIONS:
            days_in_month = _cal.monthrange(month_cursor.year, month_cursor.month)[1]
            candidate = month_cursor.replace(day=min(day_of_month, days_in_month))
            if candidate >= series_start:
                if series_end and candidate > series_end:
                    break
                if max_occurrences is not None and count >= max_occurrences:
                    break
                if candidate >= end_dt_exclusive:
                    break
                if candidate >= start_dt:
                    occurrences.append(candidate)
                count += 1
            month_cursor = _add_months(month_cursor, interval)

    elif pattern_key == "AbsoluteYearlyRecurrence":
        try:
            day_of_month = int(pattern.get("DayOfMonth", current.day))
            month = int(pattern.get("Month", current.month))
        except (ValueError, TypeError):
            return []
        year_cursor = current.year
        while count < _MAX_RECURRENCE_ITERATIONS:
            days_in_month = _cal.monthrange(year_cursor, month)[1]
            try:
                candidate = current.replace(
                    year=year_cursor, month=month, day=min(day_of_month, days_in_month)
                )
            except ValueError:
                break
            if candidate >= series_start:
                if series_end and candidate > series_end:
                    break
                if max_occurrences is not None and count >= max_occurrences:
                    break
                if candidate >= end_dt_exclusive:
                    break
                if candidate >= start_dt:
                    occurrences.append(candidate)
                count += 1
            year_cursor += interval

    elif pattern_key == "RelativeMonthlyRecurrence":
        # "every N months, on the <First|...|Last> <weekday>".
        weekday, ordinal = _relative_pattern_weekday(pattern)
        if weekday is None:
            return []
        month_cursor = current.replace(day=1)
        # Bounded by iterations rather than by `count`: a month with no matching
        # occurrence advances the cursor without producing one, so counting
        # occurrences alone would not bound the loop.
        for _ in range(_MAX_RECURRENCE_ITERATIONS):
            day = _nth_weekday_of_month(
                month_cursor.year, month_cursor.month, weekday, ordinal
            )
            if day is not None:
                candidate = month_cursor.replace(day=day)
                if candidate >= series_start:
                    if series_end and candidate > series_end:
                        break
                    if max_occurrences is not None and count >= max_occurrences:
                        break
                    if candidate >= end_dt_exclusive:
                        break
                    if candidate >= start_dt:
                        occurrences.append(candidate)
                    count += 1
            month_cursor = _add_months(month_cursor, interval)

    elif pattern_key == "RelativeYearlyRecurrence":
        # "every N years, in <Month>, on the <First|...|Last> <weekday>".
        weekday, ordinal = _relative_pattern_weekday(pattern)
        if weekday is None:
            return []
        try:
            month = int(pattern.get("Month", current.month))
        except (ValueError, TypeError):
            return []
        if not 1 <= month <= 12:
            return []
        year_cursor = current.year
        for _ in range(_MAX_RECURRENCE_ITERATIONS):
            day = _nth_weekday_of_month(year_cursor, month, weekday, ordinal)
            if day is not None:
                candidate = current.replace(year=year_cursor, month=month, day=day)
                if candidate >= series_start:
                    if series_end and candidate > series_end:
                        break
                    if max_occurrences is not None and count >= max_occurrences:
                        break
                    if candidate >= end_dt_exclusive:
                        break
                    if candidate >= start_dt:
                        occurrences.append(candidate)
                    count += 1
            year_cursor += interval

    else:
        # Unrecognized pattern variant -- degrade to no synthesized occurrences
        # rather than guessing at its semantics.
        return []

    return occurrences


@mcp.tool()
def get_calendar_events(
    start_date: str,
    end_date: str,
    include_body: bool = True,
    expand_recurrences: bool = False,
    ctx: Context = None,
) -> str:
    """Get calendar events within a date range.

    Args:
        start_date: Start date in YYYY-MM-DD format.
        end_date: End date in YYYY-MM-DD format.
        include_body: If True, fetch full event details (organizer, attendees, body)
                      via GetItem for each event. Slower but more complete.
        expand_recurrences: If True, additionally synthesize one entry per
                      occurrence of each recurring series in the window (see
                      the caveats below).

    Returns:
        JSON array of event objects with subject, start, end, location,
        categories, attendees, etc.
        By default a recurring series appears once, as its master item
        (calendar_item_type "RecurringMaster"), not expanded into one entry per
        occurrence -- this OWA deployment's CalendarView does not perform
        occurrence expansion, and GetUserAvailability (which would normally
        provide that expansion) returns a permanent NotImplementedException on
        this backend.

        With expand_recurrences=True, each series master's own entry is still
        returned, plus one synthesized entry per computed occurrence, marked
        `is_synthesized_occurrence: true`. Those synthesized entries are
        computed client-side from the master's Recurrence pattern and have an
        EMPTY item_id on purpose: they have no real distinct ItemId in Exchange,
        and reusing the master's id would let a caller pass one to
        assign_event_categories/cancel_meeting/etc. and silently mutate the
        WHOLE series. To act on a series, use the master entry's item_id.
        Relative patterns ("2nd Tuesday of the month") are not expanded.
    """
    client = _get_client(ctx)

    try:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError as e:
        return json.dumps({"error": f"Invalid date format: {e}"})

    try:
        folder_id = client.get_folder_id("calendar")
        if not folder_id:
            return json.dumps({"error": "Calendar folder not found."})

        cv_start = start_dt.strftime("%Y-%m-%dT00:00:00")
        cv_end = (end_dt + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")

        payload = {
            "__type": "FindItemJsonRequest:#Exchange",
            "Header": {
                "__type": "JsonRequestHeaders:#Exchange",
                "RequestServerVersion": "Exchange2013",
            },
            "Body": {
                "__type": "FindItemRequest:#Exchange",
                "ItemShape": {
                    "__type": "ItemResponseShape:#Exchange",
                    "BaseShape": "AllProperties",
                },
                "ParentFolderIds": [OWAClient.folder_id_dict(folder_id)],
                "Traversal": "Shallow",
                "CalendarView": {
                    "__type": "CalendarView:#Exchange",
                    "StartDate": cv_start,
                    "EndDate": cv_end,
                },
            },
        }

        data = client.request("FindItem", payload)

        all_items = []
        for msg in client.extract_items(data):
            if "RootFolder" in msg:
                all_items = msg["RootFolder"].get("Items", [])
                break

        end_dt_exclusive = end_dt + timedelta(days=1)
        matching_items = _filter_items_by_date_range(all_items, start_dt, end_dt_exclusive)

        # A RecurringMaster's own Start/End describe its FIRST occurrence, so an
        # ongoing series that began before the requested window is dropped by the
        # client-side filter above even though it has occurrences inside the
        # window. When expanding, reconsider every RecurringMaster regardless of
        # the window; each one's master row is then only emitted if the master
        # itself overlaps the window or it actually yielded an occurrence.
        in_window_ids = {id(i) for i in matching_items}
        items_to_process = list(matching_items)
        if expand_recurrences:
            items_to_process += [
                i
                for i in all_items
                if i.get("CalendarItemType", "") == "RecurringMaster"
                and id(i) not in in_window_ids
            ]

        events = []
        for item in items_to_process:
            in_window = id(item) in in_window_ids
            item_id = item.get("ItemId", {}).get("Id", "")

            event = {
                "subject": item.get("Subject", "") or "(No subject)",
                "start": item.get("Start", ""),
                "end": item.get("End", ""),
                "location": item.get("Location", ""),
                "is_all_day": item.get("IsAllDayEvent", False),
                "is_cancelled": item.get("IsCancelled", False),
                "is_meeting": item.get("IsMeeting", False),
                "is_recurring": item.get("CalendarItemType", "") == "RecurringMaster",
                "calendar_item_type": item.get("CalendarItemType", ""),
                "categories": item.get("Categories", []) or [],
                "is_synthesized_occurrence": False,
                "organizer": "",
                "my_response": item.get("MyResponseType", ""),
                "item_id": item_id,
                "body": "",
                "attendees_required": [],
                "attendees_optional": [],
            }

            details = None
            if (include_body or (expand_recurrences and event["is_recurring"])) and item_id:
                details = _get_event_details(client, item_id)

            if include_body and details:
                event["organizer"] = details["organizer"]
                event["location"] = details["location"] or event["location"]
                event["body"] = details["body"]
                event["attendees_required"] = details["attendees_required"]
                event["attendees_optional"] = details["attendees_optional"]

            if in_window:
                events.append(event)

            # Synthesize per-occurrence entries for recurring series.
            if expand_recurrences and event["is_recurring"] and details:
                try:
                    occ_starts = _expand_recurrence_occurrences(
                        details.get("recurrence") or {}, start_dt, end_dt_exclusive
                    )
                except Exception:
                    # A malformed/unexpected Recurrence payload must not take
                    # down the whole listing -- skip expansion for this series.
                    occ_starts = []

                # An out-of-window master that does yield occurrences still gets
                # its own row, so the caller has an actionable series item_id.
                if occ_starts and not in_window:
                    events.append(event)

                duration = timedelta(0)
                master_start = None
                try:
                    master_start = parse_iso_datetime(item.get("Start", ""))
                    master_end = parse_iso_datetime(item.get("End", "") or item.get("Start", ""))
                    duration = master_end - master_start
                except (ValueError, TypeError):
                    pass

                for occ_start in occ_starts:
                    # Recurrence ranges often carry a date-only StartDate, which
                    # would put every occurrence at midnight -- take the
                    # time-of-day from the series master instead.
                    if master_start is not None:
                        occ_start = occ_start.replace(
                            hour=master_start.hour,
                            minute=master_start.minute,
                            second=master_start.second,
                            microsecond=master_start.microsecond,
                        )
                    occurrence = dict(event)
                    occurrence["start"] = occ_start.isoformat()
                    occurrence["end"] = (occ_start + duration).isoformat()
                    occurrence["is_synthesized_occurrence"] = True
                    # No real distinct ItemId exists for a synthesized
                    # occurrence; see this tool's docstring for why reusing the
                    # master's id would be unsafe.
                    occurrence["item_id"] = ""
                    events.append(occurrence)

        events.sort(key=lambda e: e["start"])
        return json.dumps(events, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"Failed to get calendar events: {e}"})


@mcp.tool()
def get_calendar_event(item_id: str, ctx: Context = None) -> str:
    """Get full details for a single calendar event by ItemId.

    Args:
        item_id: The Exchange ItemId of the calendar event (from get_calendar_events).

    Returns:
        JSON object with subject, start, end, location, body, organizer,
        attendees, categories, calendar_item_type, and related fields.
        For a RecurringMaster item this describes the series master only --
        it has no per-occurrence start/end.
    """
    client = _get_client(ctx)
    try:
        details = _get_event_details(client, item_id)
        details["item_id"] = item_id
        return json.dumps(details, ensure_ascii=False)
    except SessionExpiredError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": f"Failed to get calendar event: {e}"})


# ------------------------------------------------------------------
# Tool 2: create_meeting
# ------------------------------------------------------------------


@mcp.tool()
def create_meeting(
    subject: str,
    date: str,
    start_time: str,
    duration_minutes: int = 30,
    required_attendees: list[str] | None = None,
    optional_attendees: list[str] | None = None,
    location: str | None = None,
    description: str | None = None,
    is_all_day: bool = False,
    reminder_minutes: int = 15,
    importance: str = "Normal",
    sensitivity: str = "Normal",
    ctx: Context = None,
) -> str:
    """Create a new calendar meeting.

    Args:
        subject: Meeting subject/topic.
        date: Meeting date in YYYY-MM-DD format.
        start_time: Start time in HH:MM format.
        duration_minutes: Duration in minutes (default 30).
        required_attendees: List of email addresses for required attendees.
        optional_attendees: List of email addresses for optional attendees.
        location: Location or video link.
        description: Meeting description/body text.
        is_all_day: Whether this is an all-day event.
        reminder_minutes: Minutes before start for reminder (default 15).
        importance: Importance level: Low, Normal, or High.
        sensitivity: Sensitivity: Normal, Personal, Private, or Confidential.

    Returns:
        JSON object with creation result including item_id on success.
    """
    client = _get_client(ctx)

    # Parse date and time
    try:
        start_dt = datetime.strptime(f"{date} {start_time}", "%Y-%m-%d %H:%M")
        end_dt = start_dt + timedelta(minutes=duration_minutes)
    except ValueError as e:
        return json.dumps({"error": f"Invalid date/time: {e}"})

    # Resolve attendees
    resolved_required = _resolve_attendee_list(client, required_attendees or [])
    resolved_optional = _resolve_attendee_list(client, optional_attendees or [])

    # Build HTML body
    html_body = _build_html_body(description)

    # Build location object
    location_obj = {
        "__type": "EnhancedLocation:#Exchange",
        "Annotation": "",
        "DisplayName": location or "",
        "PostalAddress": {
            "__type": "PersonaPostalAddress:#Exchange",
            "Type": "Business",
            "LocationSource": "None",
        },
    }

    # Build calendar item
    calendar_item = {
        "__type": "CalendarItem:#Exchange",
        "ClientSeriesId": str(uuid.uuid4()),
        "Subject": subject,
        "Body": {
            "__type": "BodyContentType:#Exchange",
            "BodyType": "HTML",
            "Value": html_body,
        },
        "Sensitivity": sensitivity,
        "ReminderIsSet": True,
        "ReminderMinutesBeforeStart": reminder_minutes,
        "IsResponseRequested": True,
        "DoNotForwardMeeting": False,
        "IsAllDayEvent": is_all_day,
        "Start": start_dt.strftime("%Y-%m-%dT%H:%M:%S.000"),
        "End": end_dt.strftime("%Y-%m-%dT%H:%M:%S.000"),
        "FreeBusyType": "Busy",
        "Location": location_obj,
        "unfoldedIndex": 0,
    }

    if importance != "Normal":
        calendar_item["Importance"] = importance

    if resolved_required:
        calendar_item["RequiredAttendees"] = resolved_required
    if resolved_optional:
        calendar_item["OptionalAttendees"] = resolved_optional

    # Build request - uses CreateCalendarEvent action and V2017_08_18
    payload = {
        "__type": "CreateItemJsonRequest:#Exchange",
        "Header": {
            "__type": "JsonRequestHeaders:#Exchange",
            "RequestServerVersion": "V2017_08_18",
            "TimeZoneContext": {
                "__type": "TimeZoneContext:#Exchange",
                "TimeZoneDefinition": {
                    "__type": "TimeZoneDefinitionType:#Exchange",
                    "Id": "Russian Standard Time",
                },
            },
        },
        "Body": {
            "__type": "CreateItemRequest:#Exchange",
            "Items": [calendar_item],
            "ClientSupportsIrm": True,
            "SavedItemFolderId": {
                "__type": "TargetFolderId:#Exchange",
                "BaseFolderId": {
                    "__type": "DistinguishedFolderId:#Exchange",
                    "Id": "calendar",
                },
            },
        },
    }

    # Send invitations if there are attendees
    if resolved_required or resolved_optional:
        payload["Body"]["SendMeetingInvitations"] = "SendToAllAndSaveCopy"

    data = client.request("CreateCalendarEvent", payload)

    # Check for top-level error
    body = data.get("Body", {})
    if "ErrorCode" in body:
        return json.dumps({"error": body.get("FaultMessage", "Unknown error")})

    # Check response messages
    items = body.get("ResponseMessages", {}).get("Items", [])
    if items:
        item = items[0]
        if item.get("ResponseClass") == "Success":
            result = {
                "success": True,
                "subject": subject,
                "date": date,
                "start_time": start_time,
                "end_time": end_dt.strftime("%H:%M"),
                "duration_minutes": duration_minutes,
            }
            # Extract created item ID if available
            created_items = item.get("Items", [])
            if created_items:
                item_id = created_items[0].get("ItemId", {})
                if item_id:
                    result["item_id"] = item_id.get("Id", "")
                    result["change_key"] = item_id.get("ChangeKey", "")
            if location:
                result["location"] = location
            if resolved_required:
                result["required_attendees"] = [
                    a["Mailbox"]["EmailAddress"] for a in resolved_required
                ]
            if resolved_optional:
                result["optional_attendees"] = [
                    a["Mailbox"]["EmailAddress"] for a in resolved_optional
                ]
            return json.dumps(result, ensure_ascii=False)
        else:
            return json.dumps({
                "error": item.get("MessageText", "Unknown error"),
                "response_code": item.get("ResponseCode", ""),
            })

    return json.dumps({"success": True, "subject": subject, "note": "No confirmation details"})


# ------------------------------------------------------------------
# Tool 3: update_meeting
# ------------------------------------------------------------------


@mcp.tool()
def update_meeting(
    item_id: str,
    subject: str | None = None,
    date: str | None = None,
    start_time: str | None = None,
    duration_minutes: int | None = None,
    location: str | None = None,
    description: str | None = None,
    required_attendees: list[str] | None = None,
    optional_attendees: list[str] | None = None,
    change_key: str = "",
    ctx: Context = None,
) -> str:
    """Update an existing calendar meeting.

    Internally cancels the old meeting and creates a new one with
    updated fields, because OWA's JSON API does not support UpdateItem
    for calendar items reliably. Unchanged fields are preserved from
    the original meeting.

    Args:
        item_id: The ItemId of the meeting to update (from get_calendar_events).
        subject: New subject (omit to keep original).
        date: New date in YYYY-MM-DD format (omit to keep original).
        start_time: New start time in HH:MM format (omit to keep original).
        duration_minutes: New duration in minutes (omit to keep original).
        location: New location (omit to keep original).
        description: New description/body text (omit to keep original).
        required_attendees: Email addresses for required attendees.
            Replaces existing list. Omit to keep original attendees.
        optional_attendees: Email addresses for optional attendees.
            Replaces existing list. Omit to keep original attendees.
        change_key: Ignored (kept for backward compatibility).

    Returns:
        JSON object with update result including new item_id.
    """
    client = _get_client(ctx)

    # Step 1: Get the original meeting details
    try:
        orig = _get_full_event(client, item_id)
    except Exception as e:
        return json.dumps({"error": f"Could not fetch original meeting: {e}"})

    if "error" in orig:
        return json.dumps(orig)

    # Step 2: Merge original values with updates
    new_subject = subject if subject is not None else orig.get("subject", "")

    # Parse original start/end for date and time defaults
    orig_start_str = orig.get("start", "")
    orig_end_str = orig.get("end", "")
    try:
        orig_start = datetime.fromisoformat(orig_start_str.replace("Z", "+00:00")).replace(tzinfo=None)
        orig_end = datetime.fromisoformat(orig_end_str.replace("Z", "+00:00")).replace(tzinfo=None)
        orig_duration = int((orig_end - orig_start).total_seconds() / 60)
    except (ValueError, AttributeError):
        orig_start = None
        orig_end = None
        orig_duration = 30

    if date is not None and start_time is not None:
        new_start = datetime.strptime(f"{date} {start_time}", "%Y-%m-%d %H:%M")
        dur = duration_minutes if duration_minutes is not None else orig_duration
        new_end = new_start + timedelta(minutes=dur)
    elif date is not None and orig_start is not None:
        new_start = datetime.strptime(date, "%Y-%m-%d").replace(
            hour=orig_start.hour, minute=orig_start.minute
        )
        dur = duration_minutes if duration_minutes is not None else orig_duration
        new_end = new_start + timedelta(minutes=dur)
    elif start_time is not None and orig_start is not None:
        parts = start_time.split(":")
        new_start = orig_start.replace(hour=int(parts[0]), minute=int(parts[1]))
        dur = duration_minutes if duration_minutes is not None else orig_duration
        new_end = new_start + timedelta(minutes=dur)
    elif duration_minutes is not None and orig_start is not None:
        new_start = orig_start
        new_end = new_start + timedelta(minutes=duration_minutes)
    elif orig_start is not None:
        new_start = orig_start
        new_end = orig_end
    else:
        return json.dumps({"error": "Cannot determine meeting time. Provide date and start_time."})

    new_location = location if location is not None else orig.get("location", "")

    # Resolve attendees
    if required_attendees is not None:
        resolved_required = _resolve_attendee_list(client, required_attendees)
    else:
        resolved_required = orig.get("resolved_required", [])

    if optional_attendees is not None:
        resolved_optional = _resolve_attendee_list(client, optional_attendees)
    else:
        resolved_optional = orig.get("resolved_optional", [])

    # Step 3: Cancel the original meeting
    cancel_payload = {
        "__type": "DeleteItemJsonRequest:#Exchange",
        "Header": {
            "__type": "JsonRequestHeaders:#Exchange",
            "RequestServerVersion": "Exchange2013",
        },
        "Body": {
            "__type": "DeleteItemRequest:#Exchange",
            "ItemIds": [
                {"__type": "ItemId:#Exchange", "Id": item_id}
            ],
            "DeleteType": "MoveToDeletedItems",
            "SendMeetingCancellations": "SendToAllAndSaveCopy",
            "SuppressReadReceipts": True,
        },
    }

    try:
        client.request("DeleteItem", cancel_payload)
    except Exception as e:
        return json.dumps({"error": f"Failed to cancel original meeting: {e}"})

    # Step 4: Create the new meeting
    new_body = description if description is not None else orig.get("body_html", "")
    if description is not None:
        new_body = _build_html_body(description)

    location_obj = {
        "__type": "EnhancedLocation:#Exchange",
        "Annotation": "",
        "DisplayName": new_location,
        "PostalAddress": {
            "__type": "PersonaPostalAddress:#Exchange",
            "Type": "Business",
            "LocationSource": "None",
        },
    }

    calendar_item = {
        "__type": "CalendarItem:#Exchange",
        "ClientSeriesId": str(uuid.uuid4()),
        "Subject": new_subject,
        "Body": {
            "__type": "BodyContentType:#Exchange",
            "BodyType": "HTML",
            "Value": new_body,
        },
        "Sensitivity": orig.get("sensitivity", "Normal"),
        "ReminderIsSet": True,
        "ReminderMinutesBeforeStart": 15,
        "IsResponseRequested": True,
        "DoNotForwardMeeting": False,
        "IsAllDayEvent": orig.get("is_all_day", False),
        "Start": new_start.strftime("%Y-%m-%dT%H:%M:%S.000"),
        "End": new_end.strftime("%Y-%m-%dT%H:%M:%S.000"),
        "FreeBusyType": "Busy",
        "Location": location_obj,
        "unfoldedIndex": 0,
    }

    if resolved_required:
        calendar_item["RequiredAttendees"] = resolved_required
    if resolved_optional:
        calendar_item["OptionalAttendees"] = resolved_optional

    create_payload = {
        "__type": "CreateItemJsonRequest:#Exchange",
        "Header": {
            "__type": "JsonRequestHeaders:#Exchange",
            "RequestServerVersion": "V2017_08_18",
            "TimeZoneContext": {
                "__type": "TimeZoneContext:#Exchange",
                "TimeZoneDefinition": {
                    "__type": "TimeZoneDefinitionType:#Exchange",
                    "Id": "Russian Standard Time",
                },
            },
        },
        "Body": {
            "__type": "CreateItemRequest:#Exchange",
            "Items": [calendar_item],
            "ClientSupportsIrm": True,
            "SavedItemFolderId": {
                "__type": "TargetFolderId:#Exchange",
                "BaseFolderId": {
                    "__type": "DistinguishedFolderId:#Exchange",
                    "Id": "calendar",
                },
            },
        },
    }

    if resolved_required or resolved_optional:
        create_payload["Body"]["SendMeetingInvitations"] = "SendToAllAndSaveCopy"

    try:
        data = client.request("CreateCalendarEvent", create_payload)
    except Exception as e:
        return json.dumps({"error": f"Original cancelled but failed to create new: {e}"})

    body = data.get("Body", {})
    if "ErrorCode" in body:
        return json.dumps({"error": body.get("FaultMessage", "Unknown error")})

    resp_items = body.get("ResponseMessages", {}).get("Items", [])
    if resp_items and resp_items[0].get("ResponseClass") == "Success":
        result = {
            "success": True,
            "subject": new_subject,
            "start": new_start.strftime("%Y-%m-%d %H:%M"),
            "end": new_end.strftime("%Y-%m-%d %H:%M"),
            "duration_minutes": int((new_end - new_start).total_seconds() / 60),
        }
        created_items = resp_items[0].get("Items", [])
        if created_items:
            new_item_id = created_items[0].get("ItemId", {})
            if new_item_id:
                result["item_id"] = new_item_id.get("Id", "")
                result["change_key"] = new_item_id.get("ChangeKey", "")
        return json.dumps(result, ensure_ascii=False)

    if resp_items:
        return json.dumps({
            "error": resp_items[0].get("MessageText", "Unknown error"),
            "response_code": resp_items[0].get("ResponseCode", ""),
        })

    return json.dumps({"success": True, "subject": new_subject, "note": "No confirmation details"})


# ------------------------------------------------------------------
# Tool 4: cancel_meeting
# ------------------------------------------------------------------


@mcp.tool()
def cancel_meeting(
    item_id: str,
    message: str | None = None,
    ctx: Context = None,
) -> str:
    """Cancel (delete) a calendar meeting and notify attendees.

    Args:
        item_id: The ItemId of the meeting to cancel.
        message: Optional cancellation message to attendees.

    Returns:
        JSON object with cancellation result.
    """
    client = _get_client(ctx)

    payload = {
        "__type": "DeleteItemJsonRequest:#Exchange",
        "Header": {
            "__type": "JsonRequestHeaders:#Exchange",
            "RequestServerVersion": "Exchange2013",
        },
        "Body": {
            "__type": "DeleteItemRequest:#Exchange",
            "ItemIds": [
                {"__type": "ItemId:#Exchange", "Id": item_id}
            ],
            "DeleteType": "MoveToDeletedItems",
            "SendMeetingCancellations": "SendToAllAndSaveCopy",
            "SuppressReadReceipts": True,
        },
    }

    data = client.request("DeleteItem", payload)

    items = client.extract_items(data)
    if items:
        item = items[0]
        if item.get("ResponseClass") == "Success":
            return json.dumps({"success": True, "message": "Meeting cancelled"})
        else:
            return json.dumps({
                "error": item.get("MessageText", "Unknown error"),
                "response_code": item.get("ResponseCode", ""),
            })

    # DeleteItem may return empty on success
    body = data.get("Body", {})
    if "ErrorCode" in body:
        return json.dumps({"error": body.get("FaultMessage", "Unknown error")})

    return json.dumps({"success": True, "message": "Meeting cancelled"})


# ------------------------------------------------------------------
# Tool 5: respond_to_meeting
# ------------------------------------------------------------------


@mcp.tool()
def respond_to_meeting(
    item_id: str,
    response: str,
    message: str | None = None,
    ctx: Context = None,
) -> str:
    """Respond to a meeting invitation (accept, decline, or tentative).

    Args:
        item_id: The ItemId of the meeting to respond to.
        response: Response type: "Accept", "Decline", or "Tentative".
        message: Optional message to include with the response.

    Returns:
        JSON object with response result.
    """
    client = _get_client(ctx)

    # Map response to the correct __type
    response_types = {
        "Accept": "AcceptItem:#Exchange",
        "Decline": "DeclineItem:#Exchange",
        "Tentative": "TentativelyAcceptItem:#Exchange",
    }

    response_type = response_types.get(response)
    if not response_type:
        return json.dumps({
            "error": f"Invalid response: {response}. Must be Accept, Decline, or Tentative."
        })

    response_item = {
        "__type": response_type,
        "ReferenceItemId": {
            "__type": "ItemId:#Exchange",
            "Id": item_id,
        },
    }

    if message:
        response_item["Body"] = {
            "__type": "BodyContentType:#Exchange",
            "BodyType": "Text",
            "Value": message,
        }

    payload = {
        "__type": "CreateItemJsonRequest:#Exchange",
        "Header": {
            "__type": "JsonRequestHeaders:#Exchange",
            "RequestServerVersion": "Exchange2013",
        },
        "Body": {
            "__type": "CreateItemRequest:#Exchange",
            "Items": [response_item],
            "MessageDisposition": "SendAndSaveCopy",
        },
    }

    data = client.request("CreateItem", payload)

    items = client.extract_items(data)
    if items:
        item = items[0]
        if item.get("ResponseClass") == "Success":
            return json.dumps({
                "success": True,
                "response": response,
                "message": f"Meeting {response.lower()}ed",
            })
        else:
            return json.dumps({
                "error": item.get("MessageText", "Unknown error"),
                "response_code": item.get("ResponseCode", ""),
            })

    return json.dumps({"error": "No response from server"})


# ------------------------------------------------------------------
# Tool 6: download_event_attachments
# ------------------------------------------------------------------


@mcp.tool()
def download_event_attachments(
    item_id: str,
    target_folder: str = "/tmp/attachments",
    ctx: Context = None,
) -> str:
    """Download all file attachments from a calendar event to disk.

    Args:
        item_id: The Exchange ItemId of the calendar event.
        target_folder: Local directory to save files (default /tmp/attachments).
    """
    import os

    try:
        client = _get_client(ctx)

        # Get full event details (AllProperties includes Attachments)
        payload = {
            "__type": "GetItemJsonRequest:#Exchange",
            "Header": {
                "__type": "JsonRequestHeaders:#Exchange",
                "RequestServerVersion": "Exchange2013",
            },
            "Body": {
                "__type": "GetItemRequest:#Exchange",
                "ItemShape": {
                    "__type": "ItemResponseShape:#Exchange",
                    "BaseShape": "AllProperties",
                },
                "ItemIds": [
                    {"__type": "ItemId:#Exchange", "Id": item_id}
                ],
            },
        }

        data = client.request("GetItem", payload)

        # Extract attachments from the item
        attachments = []
        for msg in client.extract_items(data):
            if "Items" not in msg:
                continue
            for item in msg["Items"]:
                for att in item.get("Attachments", []):
                    attachments.append({
                        "name": att.get("Name", ""),
                        "size": att.get("Size", 0),
                        "content_type": att.get("ContentType", ""),
                        "attachment_id": att.get("AttachmentId", {}).get("Id", ""),
                        "is_inline": att.get("IsInline", False),
                    })
                break

        if not attachments:
            return json.dumps({"success": True, "downloaded": [], "count": 0,
                               "message": "No attachments found on this event."})

        # Filter to non-inline file attachments
        file_attachments = [
            a for a in attachments
            if a.get("attachment_id") and not a.get("is_inline", False)
        ]

        if not file_attachments:
            return json.dumps({"success": True, "downloaded": [], "count": 0,
                               "message": "No downloadable file attachments."})

        os.makedirs(target_folder, exist_ok=True)

        downloaded = []
        errors = []
        used_names: set[str] = set()

        for att in file_attachments:
            try:
                content, filename, content_type = client.download_file(
                    att["attachment_id"]
                )

                # Sanitize filename
                filename = os.path.basename(filename)
                if not filename:
                    filename = att.get("name", "attachment") or "attachment"

                # Handle collisions
                base_name = filename
                name_part, _, ext_part = base_name.rpartition(".")
                if not name_part:
                    name_part = base_name
                    ext_part = ""

                counter = 1
                while filename.lower() in used_names:
                    if ext_part:
                        filename = f"{name_part}_{counter}.{ext_part}"
                    else:
                        filename = f"{name_part}_{counter}"
                    counter += 1

                used_names.add(filename.lower())

                filepath = os.path.join(target_folder, filename)
                with open(filepath, "wb") as f:
                    f.write(content)

                downloaded.append({
                    "name": filename,
                    "path": filepath,
                    "size": len(content),
                    "content_type": content_type,
                })
            except Exception as e:
                errors.append({
                    "name": att.get("name", "unknown"),
                    "error": str(e),
                })

        result = {
            "success": len(errors) == 0,
            "downloaded": downloaded,
            "count": len(downloaded),
        }
        if errors:
            result["errors"] = errors

        return json.dumps(result)

    except Exception as e:
        return json.dumps({"error": f"Failed to download event attachments: {e}"})


# ------------------------------------------------------------------
# Tool 7: get_event_links
# ------------------------------------------------------------------


@mcp.tool()
def get_event_links(
    item_id: str,
    ctx: Context = None,
) -> str:
    """Extract all hyperlinks from a calendar event's HTML description.

    Args:
        item_id: The Exchange ItemId of the calendar event to extract links from.
    """
    try:
        client = _get_client(ctx)

        payload = {
            "__type": "GetItemJsonRequest:#Exchange",
            "Header": {
                "__type": "JsonRequestHeaders:#Exchange",
                "RequestServerVersion": "Exchange2013",
            },
            "Body": {
                "__type": "GetItemRequest:#Exchange",
                "ItemShape": {
                    "__type": "ItemResponseShape:#Exchange",
                    "BaseShape": "IdOnly",
                    "BodyType": "HTML",
                    "AdditionalProperties": [
                        {
                            "__type": "PropertyUri:#Exchange",
                            "FieldURI": "Subject",
                        },
                        {
                            "__type": "PropertyUri:#Exchange",
                            "FieldURI": "Body",
                        },
                    ],
                },
                "ItemIds": [{"__type": "ItemId:#Exchange", "Id": item_id}],
            },
        }

        data = client.request("GetItem", payload)

        subject = ""
        links = []

        for msg in client.extract_items(data):
            if "Items" not in msg:
                continue
            for item in msg["Items"]:
                subject = item.get("Subject", "")
                body_val = item.get("Body", {}).get("Value", "")
                links = extract_links_from_html(body_val)
                break

        return json.dumps({
            "item_id": item_id,
            "subject": subject,
            "links": links,
            "count": len(links),
        })

    except Exception as e:
        return json.dumps({"error": f"Failed to extract event links: {e}"})


# ------------------------------------------------------------------
# Category tools
# ------------------------------------------------------------------


def _set_event_categories(client: OWAClient, item_ids: list[str], categories: list[str]) -> None:
    """Overwrite the Categories field on each event via the bespoke UpdateCalendarEvent action.

    Standard EWS UpdateItem/SetItemField always fails on this OWA build with
    ErrorSendMeetingInvitationsOrCancellationsRequired, no matter what
    meeting-notification attribute/value is sent alongside it. OWA's own web
    client doesn't use UpdateItem for this at all -- captured from the real
    browser network traffic, it POSTs a non-standard UpdateCalendarEvent
    action (payload in the X-OWA-UrlPostData header, not the POST body) with
    a singular ItemChange, a top-level EventId mirroring ItemChange.ItemId,
    no ChangeKey/ConflictResolution, and ShouldSendUpdateToAttendees/
    EventScope/TargetAudience in place of SendMeetingInvitationsOrCancellations.
    """
    for iid in item_ids:
        item_id_dict = {"__type": "ItemId:#Exchange", "Id": iid}
        payload = {
            "__type": "UpdateCalendarEventJsonRequest:#Exchange",
            "Header": {
                "__type": "JsonRequestHeaders:#Exchange",
                "RequestServerVersion": "V2018_01_08",
                "TimeZoneContext": {
                    "__type": "TimeZoneContext:#Exchange",
                    "TimeZoneDefinition": {
                        "__type": "TimeZoneDefinitionType:#Exchange",
                        "Id": "Russian Standard Time",
                    },
                },
            },
            "Body": {
                "__type": "UpdateCalendarEventRequest:#Exchange",
                "EventId": item_id_dict,
                "ItemChange": {
                    "__type": "ItemChange:#Exchange",
                    "Updates": [
                        {
                            "__type": "SetItemField:#Exchange",
                            "Path": {"__type": "PropertyUri:#Exchange", "FieldURI": "Categories"},
                            "Item": {"__type": "CalendarItem:#Exchange", "Categories": categories},
                        }
                    ],
                    "ItemId": item_id_dict,
                },
                "EventScope": 0,
                "ShouldSendUpdateToAttendees": False,
                "TargetAudience": 0,
                "ClientSupportsIrm": True,
            },
        }

        data = client.request_header_payload("UpdateCalendarEvent", payload)
        for msg in client.extract_items(data):
            if msg.get("ResponseClass") == "Error":
                raise RuntimeError(msg.get("MessageText", "UpdateCalendarEvent failed."))


@mcp.tool()
def assign_event_categories(
    item_ids: list[str],
    categories: list[str],
    ctx: Context = None,
) -> str:
    """Add one or more categories to calendar events, keeping any categories already present.

    Categories are just strings on the item (standard EWS behavior) - any
    name works, including ones not present in the mailbox's master category
    list (see the category_* tools). Assigning a brand-new name does not
    register it in the master list or give it a color.

    Args:
        item_ids: List of Exchange ItemIds to tag (from get_calendar_events).
        categories: Category names to add.
    """
    try:
        client = _get_client(ctx)
        for iid in item_ids:
            existing = _get_event_details(client, iid).get("categories", [])
            merged = list(dict.fromkeys(existing + categories))
            _set_event_categories(client, [iid], merged)
        return json.dumps({
            "success": True,
            "message": f"Added categories to {len(item_ids)} event(s).",
        })
    except SessionExpiredError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": f"Failed to assign categories: {e}"})


@mcp.tool()
def remove_event_categories(
    item_ids: list[str],
    categories: list[str],
    ctx: Context = None,
) -> str:
    """Remove one or more categories from calendar events, keeping any others present.

    Args:
        item_ids: List of Exchange ItemIds to untag (from get_calendar_events).
        categories: Category names to remove (case-insensitive match).
    """
    try:
        client = _get_client(ctx)
        lowered = {c.lower() for c in categories}
        for iid in item_ids:
            existing = _get_event_details(client, iid).get("categories", [])
            remaining = [c for c in existing if c.lower() not in lowered]
            _set_event_categories(client, [iid], remaining)
        return json.dumps({
            "success": True,
            "message": f"Removed categories from {len(item_ids)} event(s).",
        })
    except SessionExpiredError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": f"Failed to remove categories: {e}"})


@mcp.tool()
def find_events_by_category(
    category: str,
    start_date: str,
    end_date: str,
    limit: int = 10,
    ctx: Context = None,
) -> str:
    """Find calendar events tagged with a given category within a date range.

    Args:
        category: Category name to search for (case-insensitive match).
        start_date: Start date in YYYY-MM-DD format.
        end_date: End date in YYYY-MM-DD format.
        limit: Maximum number of matching events to return (default 10, max 50).
    """
    try:
        client = _get_client(ctx)
        max_limit = 50
        if limit > max_limit:
            limit = max_limit

        try:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        except ValueError as e:
            return json.dumps({"error": f"Invalid date format: {e}"})

        folder_id = client.get_folder_id("calendar")
        if not folder_id:
            return json.dumps({"error": "Calendar folder not found."})

        cv_start = start_dt.strftime("%Y-%m-%dT00:00:00")
        cv_end = (end_dt + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")

        payload = {
            "__type": "FindItemJsonRequest:#Exchange",
            "Header": {
                "__type": "JsonRequestHeaders:#Exchange",
                "RequestServerVersion": "Exchange2013",
            },
            "Body": {
                "__type": "FindItemRequest:#Exchange",
                "ItemShape": {
                    "__type": "ItemResponseShape:#Exchange",
                    "BaseShape": "AllProperties",
                },
                "ParentFolderIds": [OWAClient.folder_id_dict(folder_id)],
                "Traversal": "Shallow",
                "CalendarView": {
                    "__type": "CalendarView:#Exchange",
                    "StartDate": cv_start,
                    "EndDate": cv_end,
                },
            },
        }

        data = client.request("FindItem", payload)

        all_items = []
        for msg in client.extract_items(data):
            if "RootFolder" in msg:
                all_items = msg["RootFolder"].get("Items", [])
                break

        end_dt_exclusive = end_dt + timedelta(days=1)
        in_range_items = _filter_items_by_date_range(all_items, start_dt, end_dt_exclusive)

        category_lower = category.lower()
        matches = [
            item for item in in_range_items
            if any(cat.lower() == category_lower for cat in (item.get("Categories") or []))
        ]

        events = []
        for item in matches[:limit]:
            events.append({
                "item_id": item.get("ItemId", {}).get("Id", ""),
                "subject": item.get("Subject", ""),
                "start": item.get("Start", ""),
                "end": item.get("End", ""),
                "location": item.get("Location", ""),
                "categories": item.get("Categories", []),
            })

        return json.dumps({"events": events, "count": len(events)})

    except SessionExpiredError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": f"Failed to find events by category: {e}"})
