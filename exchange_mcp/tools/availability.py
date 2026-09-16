"""Availability / free-time tools for the Exchange MCP server.

Ports find-free-time.py and find-meeting-time.py logic into MCP tools
using OWAClient.

**Every datetime in this module is naive mailbox-local wall clock**, and that
sentence is the module's main invariant rather than a convention. `start_hour` /
`end_hour` (default 9 to 18) can only mean hours of the working day where the
mailbox lives, `_format_time` renders bare `%H:%M` with no offset, and
`_find_free_slots` subtracts busy periods from a window built out of those
hours -- so a busy period in any other frame produces free slots that are
silently shifted, which is exactly what shipped (PROJECT_STATUS.md #601/#602,
fixed 2026-09-16: a W. Europe mailbox in DST had every slot reported two hours
early, because both busy-period sources hand back naive *UTC*).

The conversion therefore happens at the **edge**: `_get_availability_events`,
`_get_calendar_events` and the `availabilityView` parsing all convert as soon
as they parse, via the `MailboxTimezone` from `client.mailbox_timezone()`, and
nothing downstream of them converts anything. `_find_free_slots` stays a pure
function over one frame -- it has four datetime inputs that must agree and a
signature that cannot say so, so the agreement is established before it is
called, not inside it.

Note that the two `GetSchedule` shapes arrive in *different* frames
(`scheduleItems` UTC, `availabilityView` wall-clock in the requested `tz_id`),
which is why the conversion is several differently-named calls rather than one
helper: `from_utc()` for an instant, `from_wire_wallclock()` for a wall clock in
the timezone we asked for, and `from_wire_timestamp()` for an EWS value that
decides between those two off its own `tzinfo`. They are named after the frame of
their *input* because that is the fact a call site can get wrong. See
exchange_mcp/mailbox_timezone.py for the full frame contract.
"""

import json
from datetime import datetime, timedelta

from mcp.server.mcpserver import Context

from exchange_mcp.server import mcp, AppContext
from exchange_mcp.mailbox_timezone import MailboxTimezone
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

    Frame-agnostic on purpose: the returned periods are in whatever frame
    `start_time` is in, because the string carries no timezone of its own -- its
    index 0 is simply the start of the window that was requested, expressed in
    the timezone that was requested with it. So the caller's job is to pass a
    `start_time` already converted to mailbox-local (via
    `MailboxTimezone.from_wire_wallclock`), not to convert the output.
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

    **`busy_periods` must already be naive mailbox-local wall clock.** The day
    window below is built from `start_hour`/`end_hour`, which are local hours by
    definition, so this function compares its two inputs directly and cannot
    detect a frame mismatch -- it just returns confidently wrong slots, which is
    how #601/#602 stayed hidden. Convert at the source (see the module
    docstring); do not add a timezone argument here, because a second place that
    knows about timezones is a second place they can disagree.
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
    client: OWAClient, email: str, start_date, end_date, tz: MailboxTimezone
) -> list[dict]:
    """Get busy events (expands recurring events), in mailbox-local wall clock.

    Prefers GetSchedule, the modern-backend GraphQL operation the
    Scheduling Assistant UI itself uses - GetUserAvailability returns a
    server-side NotImplementedException on this tenant (PROJECT_STATUS.md
    #602), confirmed unfixable client-side. Falls back to the legacy EWS
    action on classic OWA, where GetSchedule's bearer-only substrate
    surface doesn't exist.

    Both branches return mailbox-local dicts, per the module docstring's
    invariant, but they do *not* convert the same way -- which is the reason the
    conversions are named after their input frame. GetSchedule's `scheduleItems`
    are UTC instants, so `tz.from_utc()`. The legacy branch sends a
    `TimeZoneContext`, so an unqualified `CalendarEvent` time is already wall
    clock in `tz.wire_id` and only an offset-bearing one is an instant, hence
    `tz.from_wire_timestamp()`, which decides per value.

    The window is sent as `tz.to_wire_wallclock()` of local midnight so that
    "these dates" means the mailbox's days rather than UTC's: before the #601 fix
    a whole-day window was requested at midnight in a hardcoded UTC+3, i.e. the
    wrong 24 hours as well as the wrong labels.
    """
    win_start = tz.to_wire_wallclock(datetime.combine(start_date, datetime.min.time()))
    win_end = tz.to_wire_wallclock(
        datetime.combine(end_date + timedelta(days=1), datetime.min.time())
    )

    try:
        schedules = client.get_schedule([email], win_start, win_end, tz_id=tz.wire_id)
        sched = schedules[0] if schedules else {}
        if sched.get("error"):
            raise RuntimeError(sched["error"].get("message") or "GetSchedule failed")
        return [
            {
                "start": tz.from_utc(ev["start"]),
                "end": tz.from_utc(ev["end"]),
                "status": ev["status"],
            }
            for ev in sched.get("events", [])
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
                    'Id': tz.wire_id,
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
                    'StartTime': win_start.strftime('%Y-%m-%dT%H:%M:%S'),
                    'EndTime': win_end.strftime('%Y-%m-%dT%H:%M:%S'),
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

            start_str = event.get('StartTime', '')
            end_str = event.get('EndTime', '')
            if not start_str or not end_str:
                continue
            try:
                # from_wire_timestamp, not from_utc: this request carries a
                # TimeZoneContext, so an unqualified CalendarEvent time is
                # already wall clock in tz.wire_id and only an offset-bearing
                # one is a UTC instant. See MailboxTimezone.from_wire_timestamp.
                start = tz.from_wire_timestamp(
                    datetime.fromisoformat(start_str.replace('Z', '+00:00'))
                )
                end = tz.from_wire_timestamp(
                    datetime.fromisoformat(end_str.replace('Z', '+00:00'))
                )
                events.append({'start': start, 'end': end, 'status': bt})
            except (ValueError, AttributeError):
                continue

    return events


# ------------------------------------------------------------------
# Helper: get own calendar events (from find-free-time.py)
# ------------------------------------------------------------------

def _get_calendar_events(
    client: OWAClient, folder_id: str, start_date, end_date, tz: MailboxTimezone
) -> list[dict]:
    """Get calendar events within a date range, in mailbox-local wall clock.

    Returns list of busy period dicts. This payload sends no `TimeZoneContext`,
    so EWS answers in `Z`-suffixed UTC and `tz.from_wire_timestamp()` converts
    on the aware branch -- the same call as the availability path so the two
    sources cannot drift apart, rather than a `from_utc()` here that would go
    wrong the day someone adds a `TimeZoneContext` to this payload.
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

            start_str = item.get('Start', '')
            end_str = item.get('End', '')

            if not start_str or not end_str:
                continue

            try:
                # Converted to mailbox-local before the date filter below, not
                # after: an event at 00:30 local is 22:30 UTC the previous day,
                # so filtering on the unconverted value drops events off the
                # first day of the range and keeps ones past the last.
                start = tz.from_wire_timestamp(
                    datetime.fromisoformat(start_str.replace('Z', '+00:00'))
                )
                end = tz.from_wire_timestamp(
                    datetime.fromisoformat(end_str.replace('Z', '+00:00'))
                )

                # Filter by date range
                if end.date() < start_date or start.date() > end_date:
                    continue

                events.append({
                    'start': start,
                    'end': end,
                    'subject': item.get('Subject', ''),
                    'status': fbt,
                })
            except (ValueError, AttributeError):
                continue

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
        start_hour: Working day start hour (0-23). Default 9, and interpreted
            in the *mailbox's own* timezone — as are the returned times.
        end_hour: Working day end hour (0-23). Default 18, same timezone.

    Returns:
        JSON object with free_slots keyed by date, each containing an
        array of {start, end, duration_minutes} objects, plus a `timezone`
        block naming the frame those times are in and where it was determined
        from. Times are mailbox-local wall clock with no offset suffix: until
        2026-09-16 they were UTC instants rendered the same way, which put
        every slot out by the mailbox's UTC offset (PROJECT_STATUS.md #601),
        so the frame is now reported rather than assumed.
    """
    client = _get_client(ctx)

    try:
        sd = datetime.strptime(start_date, '%Y-%m-%d').date()
        ed = datetime.strptime(end_date, '%Y-%m-%d').date() if end_date else sd
    except ValueError as e:
        return json.dumps({"error": f"Invalid date format: {e}"})

    try:
        tz = client.mailbox_timezone()
        # Use GetUserAvailability for accurate recurring event expansion
        if client.user_email:
            all_busy = _get_availability_events(client, client.user_email, sd, ed, tz)
        else:
            # Fallback to FindItem (misses recurring event occurrences)
            folder_id = client.get_folder_id("calendar")
            if not folder_id:
                return json.dumps({"error": "Could not find calendar folder. Session may have expired."})
            all_busy = _get_calendar_events(client, folder_id, sd, ed, tz)
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
        {
            "free_slots": result,
            # Described at the start of the queried range, not at "now": the
            # offset reported has to be the one actually applied to these slots,
            # and a range a few months out can be on the other side of a DST
            # transition from today.
            "timezone": tz.describe(tz.to_utc(datetime.combine(sd, datetime.min.time()))),
        },
        ensure_ascii=False,
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
        start_hour: Working day start hour (0-23). Default 9, and interpreted
            in the *mailbox's own* timezone — as are the returned times. Note
            that this is the requesting mailbox's timezone, not each attendee's:
            a slot is reported in one frame, and it is this one.
        end_hour: Working day end hour (0-23). Default 18, same timezone.

    Returns:
        JSON object with attendee info and free_slots keyed by date,
        each containing an array of {start, end, duration_minutes}, plus a
        `timezone` block naming the frame those times are in.

        Until 2026-09-16 those times were shifted, and by *different* amounts
        per attendee: the two shapes this data arrives in are in different
        frames (`availabilityView` is wall clock in the requested timezone,
        `scheduleItems` are UTC) and both were merged into one busy list
        untouched, against a timezone id hardcoded to UTC+3. See
        PROJECT_STATUS.md #602 and exchange_mcp/mailbox_timezone.py.
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
    all_busy = []
    attendee_info = []
    got_schedule = False
    schedule_error = ""
    tz = client.mailbox_timezone()

    # Local midnight on the first/last day, so "these dates" means the mailbox's
    # days; sent through to_wire_wallclock because the window has to be
    # expressed in the same timezone id the request declares.
    local_window_start = datetime.combine(sd, datetime.min.time())
    local_window_end = datetime.combine(ed + timedelta(days=1), datetime.min.time())

    try:
        schedules = client.get_schedule(
            email_list,
            tz.to_wire_wallclock(local_window_start),
            tz.to_wire_wallclock(local_window_end),
            tz_id=tz.wire_id,
        )
        got_schedule = True

        for sched in schedules:
            email = sched["email"]
            if sched.get("error"):
                attendee_info.append({"email": email, "status": "no_data"})
                continue

            av = sched.get("availability_view", "")
            if av:
                # availabilityView index 0 is the requested window start
                # expressed in the requested timezone, and the window we
                # requested is `local_window_start` put through
                # to_wire_wallclock -- so converting the base back with
                # from_wire_wallclock lands on `local_window_start` itself, and
                # passing it directly says that without a no-op round trip.
                #
                # Note this is *not* the same conversion as the events branch
                # below, which is UTC (from_utc). Merging the two shapes without
                # distinguishing them is #602: the same attendee list could come
                # back partly in one frame and partly in the other.
                busy_periods = _parse_freebusy_string(av, local_window_start)
                attendee_info.append({
                    "email": email,
                    "busy_slots": sum(1 for c in av if c != '0'),
                    "free_slots": sum(1 for c in av if c == '0'),
                })
                all_busy.extend(busy_periods)
                continue

            busy_events = [
                ev for ev in sched.get("events", [])
                if ev["status"].lower() not in ("free", "nodata")
            ]
            if busy_events:
                attendee_info.append({"email": email, "calendar_events": len(busy_events)})
                all_busy.extend(
                    (tz.from_utc(ev["start"]), tz.from_utc(ev["end"])) for ev in busy_events
                )
            else:
                attendee_info.append({"email": email, "status": "no_data"})
    except BearerModeRequiredError:
        got_schedule = False
    except RuntimeError as exc:
        # GetSchedule failed as a whole operation (GraphQL `"data": null`) rather
        # than per-mailbox -- get_schedule turns that into a RuntimeError naming
        # the server's own reason. Before that it was an AttributeError on the
        # null, i.e. an opaque 500 from this tool with the explanation unread.
        # Fall through to the legacy action rather than returning here: it is
        # broken on *this* tenant but not on classic OWA, and the reason is
        # carried along so a legacy failure reports both halves instead of
        # replacing the informative message with an unrelated one.
        got_schedule = False
        schedule_error = str(exc)

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
                        'Id': tz.wire_id,
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
                        'StartTime': tz.to_wire_wallclock(local_window_start).strftime('%Y-%m-%dT%H:%M:%S'),
                        'EndTime': tz.to_wire_wallclock(local_window_end).strftime('%Y-%m-%dT%H:%M:%S'),
                    },
                    'MergedFreeBusyIntervalInMinutes': 30,
                    'RequestedView': 'DetailedMerged',
                },
            },
        }

        def _both_failed(legacy: str) -> str:
            """Report the legacy failure, plus the GetSchedule one if there was
            one. On a modern tenant GetUserAvailability answers a permanent
            NotImplementedException (#602), so its message alone would bury the
            only informative half of the story."""
            if schedule_error:
                return f"{legacy} (GetSchedule was tried first: {schedule_error})"
            return legacy

        try:
            data = client.request("GetUserAvailability", payload)
        except Exception as e:
            return json.dumps({"error": _both_failed(str(e))})

        body = data.get('Body', {})
        if 'ErrorCode' in body:
            return json.dumps({"error": _both_failed(
                body.get('FaultMessage')
                or f"GetUserAvailability failed: {body.get('ExceptionName', 'Unknown error')}"
            )})

        freebusy_responses = body.get('FreeBusyResponseArray', [])

        for i, fb_resp in enumerate(freebusy_responses):
            fb_view = fb_resp.get('FreeBusyView', {})
            merged_fb = fb_view.get('MergedFreeBusy', '')
            email = email_list[i] if i < len(email_list) else f"Person {i+1}"

            if merged_fb:
                # Same base as the GetSchedule availabilityView branch above,
                # for the same reason: MergedFreeBusy's index 0 is the requested
                # window start in the requested TimeZoneContext, which is
                # `local_window_start` by construction.
                busy_periods = _parse_freebusy_string(merged_fb, local_window_start)

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
                        start_str = event.get('StartTime', '')
                        end_str = event.get('EndTime', '')
                        if start_str and end_str:
                            try:
                                # Same frame rule as _get_availability_events'
                                # legacy branch: this request carries a
                                # TimeZoneContext, so an unqualified time is
                                # already wire wall clock and only an
                                # offset-bearing one is a UTC instant.
                                start = tz.from_wire_timestamp(
                                    datetime.fromisoformat(start_str.replace('Z', '+00:00'))
                                )
                                end = tz.from_wire_timestamp(
                                    datetime.fromisoformat(end_str.replace('Z', '+00:00'))
                                )
                                all_busy.append((start, end))
                            except Exception:
                                pass
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
        "timezone": tz.describe(tz.to_utc(local_window_start)),
    }

    if resolve_errors:
        result["unresolved"] = resolve_errors

    return json.dumps(result, ensure_ascii=False)
