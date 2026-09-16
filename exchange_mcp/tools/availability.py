"""Availability / free-time tools for the Exchange MCP server.

Ports find-free-time.py and find-meeting-time.py logic into MCP tools
using OWAClient.

**Everything in here happens on one grid: the mailbox's own wall clock.**
Working hours (`start_hour`/`end_hour`), the day buckets the results are
keyed by, the weekend skip and the `HH:MM` strings in the output are all
local by construction, so every busy period has to be on that grid before
it is subtracted from anything. The two availability surfaces do *not*
agree about which frame they answer in - `availabilityView` is wall-clock
in the requested zone, `scheduleItems` is UTC whatever you ask for - and
comparing the second one directly against the working-hours window slid
every meeting by the mailbox's UTC offset. `availability_frame` owns that
distinction and documents which path needs converting and why the other
must not be touched; this module resolves the zone once per call and
reports it, so a wrong grid is visible in the response instead of being
invisible in the arithmetic.
"""

import json
from datetime import datetime, timedelta

from mcp.server.mcpserver import Context

from exchange_mcp import availability_frame as frame
from exchange_mcp.server import mcp, AppContext
from exchange_mcp.owa_client import BearerModeRequiredError, OWAClient


def _get_client(ctx: Context) -> OWAClient:
    """Extract the OWAClient from the MCP lifespan context."""
    app_ctx: AppContext = ctx.request_context.lifespan_context
    return app_ctx.client


# ------------------------------------------------------------------
# Pure helper functions (preserved from find-meeting-time.py)
# ------------------------------------------------------------------

def _parse_freebusy_string(
    freebusy_str: str, start_time: datetime, interval_minutes: int = 30
) -> list[tuple]:
    """Parse the MergedFreeBusy string.

    Each character represents a time slot:
    0 = Free, 1 = Tentative, 2 = Busy, 3 = Out of Office, 4 = Working Elsewhere

    The periods come out in whatever frame `start_time` is in, and both
    callers pass the caller's own `start_date` at local midnight - which is
    correct precisely because this string (`MergedFreeBusy` /
    `availabilityView`) is wall-clock in the *requested* timezone, not UTC.
    Nothing here needs converting, and converting it would be the bug:
    see `availability_frame`'s module docstring on the asymmetry with
    `scheduleItems`.
    """
    busy_periods = []
    current_time = start_time

    for char in freebusy_str:
        next_time = current_time + timedelta(minutes=interval_minutes)
        if char in ['1', '2', '3', '4']:  # Not free
            busy_periods.append((current_time, next_time, char))
        current_time = next_time

    return busy_periods


def _merge_busy_periods(all_busy: list) -> list[tuple]:
    """Merge overlapping busy periods."""
    if not all_busy:
        return []

    # Sort by start time
    sorted_busy = sorted(all_busy, key=lambda x: x[0])
    merged = [(sorted_busy[0][0], sorted_busy[0][1])]

    for start, end, *_ in sorted_busy[1:]:
        if start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    return merged


def _find_free_slots(
    busy_periods: list, date, start_hour: int, end_hour: int, duration_minutes: int
) -> list[tuple]:
    """Find free slots on a given date within working hours.

    `busy_periods` must already be in the mailbox's wall clock, the frame
    this function's own day window is built in. That is not a formality: a
    caller that hands over `get_schedule`'s naive-UTC timestamps gets a
    plausible-looking answer with every meeting moved by the mailbox's UTC
    offset - free time offered on top of a booked slot, and genuinely free
    time hidden. Convert with `availability_frame.events_to_wall_clock`
    before calling; periods from `_parse_freebusy_string` are already
    wall-clock and must be passed through unchanged.
    """
    day_start = datetime.combine(date, datetime.min.time().replace(hour=start_hour))
    day_end = datetime.combine(date, datetime.min.time().replace(hour=end_hour))

    # Filter busy periods to this day
    day_busy = []
    for period in busy_periods:
        start, end = period[0], period[1]
        if end.date() < date or start.date() > date:
            continue
        start = max(start, day_start)
        end = min(end, day_end)
        if start < end:
            day_busy.append((start, end))

    # Merge overlapping
    merged = _merge_busy_periods([(s, e) for s, e in day_busy])

    # Find gaps
    free_slots = []
    current = day_start

    for busy_start, busy_end in merged:
        if current < busy_start:
            gap_duration = (busy_start - current).total_seconds() / 60
            if gap_duration >= duration_minutes:
                free_slots.append((current, busy_start))
        current = max(current, busy_end)

    # Check for time after last meeting
    if current < day_end:
        gap_duration = (day_end - current).total_seconds() / 60
        if gap_duration >= duration_minutes:
            free_slots.append((current, day_end))

    return free_slots


def _format_time(dt: datetime) -> str:
    """Format datetime as HH:MM."""
    return dt.strftime('%H:%M')


# ------------------------------------------------------------------
# Helper: get busy events via GetUserAvailability (includes recurring)
# ------------------------------------------------------------------

def _get_availability_events(
    client: OWAClient, email: str, start_date, end_date, zone: frame.ZoneResolution
) -> list[dict]:
    """Get busy events (expands recurring events), in the mailbox's wall clock.

    Prefers GetSchedule, the modern-backend GraphQL operation the
    Scheduling Assistant UI itself uses - GetUserAvailability returns a
    server-side NotImplementedException on this tenant (PROJECT_STATUS.md
    #602), confirmed unfixable client-side. Falls back to the legacy EWS
    action on classic OWA, where GetSchedule's bearer-only substrate
    surface doesn't exist.

    `zone` is what the two branches' differing frames are reconciled into:
    GetSchedule's `events` are naive UTC and always convert, while the EWS
    `CalendarEventArray` timestamps convert only if the wire actually
    carried an offset (`frame.wire_to_wall_clock`) - unsuffixed, they are
    already wall-clock in the `TimeZoneContext` zone this request asks for.
    """
    try:
        schedules = client.get_schedule(
            [email],
            datetime.combine(start_date, datetime.min.time()),
            datetime.combine(end_date + timedelta(days=1), datetime.min.time()),
            tz_id=frame.schedule_tz_id(zone),
        )
        sched = schedules[0] if schedules else {}
        if sched.get("error"):
            raise RuntimeError(sched["error"].get("message") or "GetSchedule failed")
        return [
            {"start": ev["start"], "end": ev["end"], "status": ev["status"]}
            for ev in frame.events_to_wall_clock(sched.get("events", []), zone)
            if ev["status"].lower() not in ("free", "nodata")
        ]
    except BearerModeRequiredError:
        pass

    payload = {
        '__type': 'GetUserAvailabilityJsonRequest:#Exchange',
        'Header': {
            '__type': 'JsonRequestHeaders:#Exchange',
            'RequestServerVersion': 'Exchange2013',
            'TimeZoneContext': {
                '__type': 'TimeZoneContext:#Exchange',
                'TimeZoneDefinition': {
                    '__type': 'TimeZoneDefinitionType:#Exchange',
                    'Id': 'Russian Standard Time',
                },
            },
        },
        'Body': {
            '__type': 'GetUserAvailabilityRequest:#Exchange',
            'MailboxDataArray': [{
                '__type': 'MailboxData:#Exchange',
                'Email': {'__type': 'EmailAddress:#Exchange', 'Address': email},
                'AttendeeType': 'Required',
            }],
            'FreeBusyViewOptions': {
                '__type': 'FreeBusyViewOptions:#Exchange',
                'TimeWindow': {
                    '__type': 'Duration:#Exchange',
                    'StartTime': f'{start_date}T00:00:00',
                    'EndTime': f'{end_date + timedelta(days=1)}T00:00:00',
                },
                'MergedFreeBusyIntervalInMinutes': 30,
                'RequestedView': 'DetailedMerged',
            },
        },
    }

    data = client.request('GetUserAvailability', payload)
    body = data.get('Body', {})

    events = []
    for fb_resp in body.get('FreeBusyResponseArray', []):
        fb_view = fb_resp.get('FreeBusyView', {})
        cal_events = fb_view.get('CalendarEventArray', {})
        items = (
            cal_events.get('Items', [])
            if isinstance(cal_events, dict)
            else (cal_events if isinstance(cal_events, list) else [])
        )
        for event in items:
            bt = event.get('BusyType', '')
            if bt in ('Free', 'NoData'):
                continue

            start = frame.wire_to_wall_clock(event.get('StartTime', ''), zone)
            end = frame.wire_to_wall_clock(event.get('EndTime', ''), zone)
            if not start or not end:
                continue
            events.append({'start': start, 'end': end, 'status': bt})

    return events


# ------------------------------------------------------------------
# Helper: get own calendar events (from find-free-time.py)
# ------------------------------------------------------------------

def _get_calendar_events(
    client: OWAClient, folder_id: str, start_date, end_date, zone: frame.ZoneResolution
) -> list[dict]:
    """Get calendar events within a date range. Returns list of busy period dicts.

    Times come back in the mailbox's wall clock, like every other producer
    of busy periods here. This request sends no `TimeZoneContext` at all, so
    `Start`/`End` arrive UTC-suffixed and `frame.wire_to_wall_clock`
    converts them; sending a zone instead and relying on the server to
    convert would work too, but it would put the frame in a place a reader
    of `_find_free_slots` cannot see.
    """
    events = []
    offset = 0
    batch_size = 100

    while True:
        payload = {
            '__type': 'FindItemJsonRequest:#Exchange',
            'Header': {
                '__type': 'JsonRequestHeaders:#Exchange',
                'RequestServerVersion': 'Exchange2013',
            },
            'Body': {
                '__type': 'FindItemRequest:#Exchange',
                'ItemShape': {
                    '__type': 'ItemResponseShape:#Exchange',
                    'BaseShape': 'AllProperties',
                },
                'ParentFolderIds': [
                    OWAClient.folder_id_dict(folder_id)
                ],
                'Traversal': 'Shallow',
                'Paging': {
                    '__type': 'IndexedPageView:#Exchange',
                    'BasePoint': 'Beginning',
                    'Offset': offset,
                    'MaxEntriesReturned': batch_size,
                },
                'SortOrder': [
                    {
                        '__type': 'SortResults:#Exchange',
                        'Order': 'Ascending',
                        'Path': {
                            '__type': 'PropertyUri:#Exchange',
                            'FieldURI': 'Start',
                        },
                    }
                ],
            },
        }

        data = client.request("FindItem", payload)
        items = client.extract_items(data)

        if not items:
            break

        folder_items = items[0].get('RootFolder', {}).get('Items', [])
        if not folder_items:
            break

        for item in folder_items:
            # Skip cancelled events
            if item.get('IsCancelled'):
                continue

            # Skip free/tentative slots
            fbt = item.get('FreeBusyType', 'Busy')
            if fbt in ['Free', 'NoData']:
                continue

            start = frame.wire_to_wall_clock(item.get('Start', ''), zone)
            end = frame.wire_to_wall_clock(item.get('End', ''), zone)

            if not start or not end:
                continue

            # Filter by date range - on the converted times, so an event that
            # is only in range in one of the two frames is judged by the one
            # the caller asked in.
            if end.date() < start_date or start.date() > end_date:
                continue

            events.append({
                'start': start,
                'end': end,
                'subject': item.get('Subject', ''),
                'status': fbt,
            })

        # Check if there are more items
        is_last = items[0].get('RootFolder', {}).get('IncludesLastItemInRange', True)
        if is_last:
            break

        offset += batch_size

    return events


# ------------------------------------------------------------------
# Tool: find_free_time
# ------------------------------------------------------------------

@mcp.tool()
def find_free_time(
    start_date: str,
    end_date: str = "",
    duration_minutes: int = 30,
    start_hour: int = 9,
    end_hour: int = 18,
    ctx: Context = None,
) -> str:
    """Find free time slots in your own calendar.

    Analyzes your calendar events and returns available time slots
    within working hours for each weekday in the range.

    Args:
        start_date: Start date in YYYY-MM-DD format.
        end_date: End date in YYYY-MM-DD format. Defaults to start_date
            if not provided (single-day search).
        duration_minutes: Minimum slot duration in minutes. Default 30.
        start_hour: Working day start hour (0-23). Default 9.
        end_hour: Working day end hour (0-23). Default 18.

    Returns:
        JSON object with free_slots keyed by date, each containing an
        array of {start, end, duration_minutes} objects, plus a timezone
        block naming the zone the times are in (and a warning if that
        zone could not be resolved, in which case they are UTC).
    """
    client = _get_client(ctx)

    try:
        sd = datetime.strptime(start_date, '%Y-%m-%d').date()
        ed = datetime.strptime(end_date, '%Y-%m-%d').date() if end_date else sd
    except ValueError as e:
        return json.dumps({"error": f"Invalid date format: {e}"})

    # Resolved once for the whole call: the working hours below are wall-clock
    # and the busy periods arrive UTC, so this is the only thing that puts
    # them on the same grid.
    zone = frame.mailbox_zone(client)

    try:
        # Use GetUserAvailability for accurate recurring event expansion
        if client.user_email:
            all_busy = _get_availability_events(client, client.user_email, sd, ed, zone)
        else:
            # Fallback to FindItem (misses recurring event occurrences)
            folder_id = client.get_folder_id("calendar")
            if not folder_id:
                return json.dumps({"error": "Could not find calendar folder. Session may have expired."})
            all_busy = _get_calendar_events(client, folder_id, sd, ed, zone)
    except Exception as e:
        return json.dumps({"error": str(e)})

    # Convert event dicts to (start, end) tuples for _find_free_slots
    busy_periods = [(ev['start'], ev['end']) for ev in all_busy]

    result = {}
    current_date = sd
    while current_date <= ed:
        # Skip weekends
        if current_date.weekday() < 5:
            free = _find_free_slots(
                busy_periods, current_date,
                start_hour, end_hour, duration_minutes,
            )
            if free:
                result[str(current_date)] = [
                    {
                        "start": _format_time(s),
                        "end": _format_time(e),
                        "duration_minutes": int((e - s).total_seconds() / 60),
                    }
                    for s, e in free
                ]
        current_date += timedelta(days=1)

    return json.dumps(
        {"free_slots": result, "timezone": zone.as_dict()}, ensure_ascii=False
    )


# ------------------------------------------------------------------
# Tool: find_meeting_time
# ------------------------------------------------------------------

@mcp.tool()
def find_meeting_time(
    emails: str,
    start_date: str,
    end_date: str = "",
    duration_minutes: int = 30,
    start_hour: int = 9,
    end_hour: int = 18,
    ctx: Context = None,
) -> str:
    """Find meeting times that work for multiple people.

    Uses the OWA GetUserAvailability API to check cross-mailbox
    availability and find common free slots for all attendees.
    Supports multi-day ranges — searches each weekday in the range.

    Args:
        emails: Comma-separated email addresses or names of attendees.
        start_date: Start date in YYYY-MM-DD format.
        end_date: End date in YYYY-MM-DD format. Defaults to start_date
            if not provided (single-day search).
        duration_minutes: Minimum slot duration in minutes. Default 30.
        start_hour: Working day start hour (0-23). Default 9.
        end_hour: Working day end hour (0-23). Default 18.

    Returns:
        JSON object with attendee info and free_slots keyed by date,
        each containing an array of {start, end, duration_minutes}, plus a
        timezone block naming the zone the times are in (and a warning if
        that zone could not be resolved, in which case they are UTC).
    """
    client = _get_client(ctx)

    raw_list = [e.strip() for e in emails.split(',') if e.strip()]
    if not raw_list:
        return json.dumps({"error": "No email addresses provided."})

    try:
        sd = datetime.strptime(start_date, '%Y-%m-%d').date()
        ed = datetime.strptime(end_date, '%Y-%m-%d').date() if end_date else sd
    except ValueError as e:
        return json.dumps({"error": f"Invalid date format: {e}"})

    # Resolve names to email addresses via ResolveNames
    email_list = []
    resolve_errors = []
    for entry in raw_list:
        if '@' in entry:
            email_list.append(entry)
        else:
            resolutions = client.resolve_names(entry, full_contact=False)
            if resolutions:
                addr = resolutions[0].get('Mailbox', {}).get('EmailAddress', '')
                if addr:
                    email_list.append(addr)
                else:
                    resolve_errors.append(entry)
            else:
                resolve_errors.append(entry)

    if not email_list:
        return json.dumps({"error": f"Could not resolve any names to email addresses: {resolve_errors}"})

    # Query availability: prefer GetSchedule, the modern-backend GraphQL
    # operation the Scheduling Assistant UI itself uses - GetUserAvailability
    # returns a server-side NotImplementedException on this tenant
    # (PROJECT_STATUS.md #602), confirmed unfixable client-side. Falls back
    # to the legacy EWS action on classic OWA, where GetSchedule's
    # bearer-only substrate surface doesn't exist. GetSchedule's
    # availabilityView uses the identical 0/1/2/3/4-per-interval encoding
    # as GetUserAvailability's MergedFreeBusy, so _parse_freebusy_string
    # applies unchanged.
    #
    # This function is where the two frames meet, and they are handled
    # differently on purpose: availabilityView is wall-clock in the requested
    # zone (so its periods, laid out from local midnight, are already on the
    # working-hours grid), while the scheduleItems fallback below is UTC and
    # is converted. See availability_frame's module docstring.
    zone = frame.mailbox_zone(client)
    all_busy = []
    attendee_info = []
    got_schedule = False

    try:
        schedules = client.get_schedule(
            email_list,
            datetime.combine(sd, datetime.min.time()),
            datetime.combine(ed + timedelta(days=1), datetime.min.time()),
            # The zone availabilityView is answered in, and therefore the one
            # thing that puts *that* path on the working-hours grid - it is
            # never converted after the fact. See frame.schedule_tz_id.
            tz_id=frame.schedule_tz_id(zone),
        )
        got_schedule = True
        start_time = datetime.combine(sd, datetime.min.time())

        for sched in schedules:
            email = sched["email"]
            if sched.get("error"):
                attendee_info.append({"email": email, "status": "no_data"})
                continue

            av = sched.get("availability_view", "")
            if av:
                busy_periods = _parse_freebusy_string(av, start_time)
                attendee_info.append({
                    "email": email,
                    "busy_slots": sum(1 for c in av if c != '0'),
                    "free_slots": sum(1 for c in av if c == '0'),
                })
                all_busy.extend(busy_periods)
                continue

            busy_events = [
                ev for ev in frame.events_to_wall_clock(sched.get("events", []), zone)
                if ev["status"].lower() not in ("free", "nodata")
            ]
            if busy_events:
                attendee_info.append({"email": email, "calendar_events": len(busy_events)})
                all_busy.extend((ev["start"], ev["end"]) for ev in busy_events)
            else:
                attendee_info.append({"email": email, "status": "no_data"})
    except BearerModeRequiredError:
        got_schedule = False

    if not got_schedule:
        # Build mailbox data (reused for each day chunk)
        mailbox_data = []
        for email in email_list:
            mailbox_data.append({
                '__type': 'MailboxData:#Exchange',
                'Email': {
                    '__type': 'EmailAddress:#Exchange',
                    'Address': email,
                },
                'AttendeeType': 'Required',
            })

        # Query the full date range at once (API handles multi-day windows)
        payload = {
            '__type': 'GetUserAvailabilityJsonRequest:#Exchange',
            'Header': {
                '__type': 'JsonRequestHeaders:#Exchange',
                'RequestServerVersion': 'Exchange2013',
                'TimeZoneContext': {
                    '__type': 'TimeZoneContext:#Exchange',
                    'TimeZoneDefinition': {
                        '__type': 'TimeZoneDefinitionType:#Exchange',
                        'Id': 'Russian Standard Time',
                    },
                },
            },
            'Body': {
                '__type': 'GetUserAvailabilityRequest:#Exchange',
                'MailboxDataArray': mailbox_data,
                'FreeBusyViewOptions': {
                    '__type': 'FreeBusyViewOptions:#Exchange',
                    'TimeWindow': {
                        '__type': 'Duration:#Exchange',
                        'StartTime': f'{sd}T00:00:00',
                        'EndTime': f'{ed + timedelta(days=1)}T00:00:00',
                    },
                    'MergedFreeBusyIntervalInMinutes': 30,
                    'RequestedView': 'DetailedMerged',
                },
            },
        }

        try:
            data = client.request("GetUserAvailability", payload)
        except Exception as e:
            return json.dumps({"error": str(e)})

        body = data.get('Body', {})
        if 'ErrorCode' in body:
            return json.dumps({"error": body.get('FaultMessage') or f"GetUserAvailability failed: {body.get('ExceptionName', 'Unknown error')}"})

        freebusy_responses = body.get('FreeBusyResponseArray', [])

        for i, fb_resp in enumerate(freebusy_responses):
            fb_view = fb_resp.get('FreeBusyView', {})
            merged_fb = fb_view.get('MergedFreeBusy', '')
            email = email_list[i] if i < len(email_list) else f"Person {i+1}"

            if merged_fb:
                start_time = datetime.combine(sd, datetime.min.time())
                busy_periods = _parse_freebusy_string(merged_fb, start_time)

                busy_count = sum(1 for c in merged_fb if c != '0')
                free_count = sum(1 for c in merged_fb if c == '0')
                attendee_info.append({
                    "email": email,
                    "busy_slots": busy_count,
                    "free_slots": free_count,
                })

                all_busy.extend(busy_periods)
            else:
                # Fallback: parse CalendarEventArray
                cal_events_raw = fb_view.get('CalendarEventArray', {})
                cal_events = cal_events_raw.get('Items', []) if isinstance(cal_events_raw, dict) else (cal_events_raw if isinstance(cal_events_raw, list) else [])
                if cal_events:
                    attendee_info.append({
                        "email": email,
                        "calendar_events": len(cal_events),
                    })
                    for event in cal_events:
                        # Converted only if the wire carried an offset: EWS
                        # answers this array in the request's TimeZoneContext
                        # zone, unsuffixed, which is already the right frame.
                        # (Before, these two kept their tzinfo and every
                        # comparison against a naive window raised.)
                        start = frame.wire_to_wall_clock(event.get('StartTime', ''), zone)
                        end = frame.wire_to_wall_clock(event.get('EndTime', ''), zone)
                        if start and end:
                            all_busy.append((start, end))
                else:
                    attendee_info.append({
                        "email": email,
                        "status": "no_data",
                    })

    merged_busy = _merge_busy_periods(all_busy)

    # Find free slots for each weekday in range
    free_by_date = {}
    current_date = sd
    while current_date <= ed:
        if current_date.weekday() < 5:  # Skip weekends
            free = _find_free_slots(
                merged_busy, current_date,
                start_hour, end_hour, duration_minutes,
            )
            if free:
                free_by_date[str(current_date)] = [
                    {
                        "start": _format_time(s),
                        "end": _format_time(e),
                        "duration_minutes": int((e - s).total_seconds() / 60),
                    }
                    for s, e in free
                ]
        current_date += timedelta(days=1)

    result = {
        "period": {"start": str(sd), "end": str(ed)},
        "attendees": attendee_info,
        "free_slots": free_by_date,
        "timezone": zone.as_dict(),
    }

    if resolve_errors:
        result["unresolved"] = resolve_errors

    return json.dumps(result, ensure_ascii=False)
