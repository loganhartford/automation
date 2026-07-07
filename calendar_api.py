import os
import uuid
import pickle
import datetime
import pytz
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar"]
CREDENTIALS_FILE = "credentials.json"
CALENDAR_TOKEN_FILE = "calendar_token.pickle"
TIMEZONE = "America/Vancouver"
# Booking window and rules
AVAILABLE_START = (11, 0)              # 11:00 AM — first possible start
AVAILABLE_END = (15, 30)              # 3:30 PM — meetings must end by this time
SLOT_GRID_MINUTES = 30                # start times offered on a 30-minute grid
MIN_LEAD_HOURS = 24                   # no bookings sooner than 24h from now
ALLOWED_DURATIONS = [15, 30, 45, 60]  # minutes the booker may choose
MIN_DURATION = min(ALLOWED_DURATIONS)
MAX_DURATION = max(ALLOWED_DURATIONS)

# This account (calendar_token.pickle) is a headless "booking robot": it reads
# availability across all subscribed calendars and issues invites. The real
# human-facing calendar/inbox is Outlook, added as an attendee on every booking
# so the meeting lands on the Outlook calendar and recruiters see this address.
MEETING_HOST_EMAIL = "logan.hartford@outlook.com"
MEETING_HOST_NAME = "Logan Hartford"


def get_calendar_service():
    """Authenticate and return a Google Calendar service object.

    Run this locally once to generate calendar_token.pickle, then scp it
    to the server. Uses a separate token file from the Gmail pipeline.
    Log in with the Google account that holds your personal calendar.
    """
    creds = None
    if os.path.exists(CALENDAR_TOKEN_FILE):
        with open(CALENDAR_TOKEN_FILE, "rb") as f:
            creds = pickle.load(f)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            raise RuntimeError(
                "Calendar auth token missing or invalid. Re-authorize: delete "
                "calendar_token.pickle and follow the SSH OAuth flow in the README."
            )
        with open(CALENDAR_TOKEN_FILE, "wb") as f:
            pickle.dump(creds, f)
    return build("calendar", "v3", credentials=creds)


def _fetch_busy_intervals(service, range_start_iso, range_end_iso):
    """Return [(start_utc, end_utc), ...] busy intervals across all calendars.

    Uses events.list per calendar so we can exclude all-day/multi-day events
    (which use 'date' not 'dateTime') and events explicitly shown as Free.
    """
    utc = pytz.utc
    cal_list = service.calendarList().list().execute()
    busy = []
    for cal in cal_list.get("items", []):
        events_result = service.events().list(
            calendarId=cal["id"],
            timeMin=range_start_iso,
            timeMax=range_end_iso,
            singleEvents=True,
        ).execute()
        for event in events_result.get("items", []):
            if "dateTime" not in event.get("start", {}):
                continue
            if event.get("transparency") == "transparent":
                continue
            b_start = datetime.datetime.fromisoformat(event["start"]["dateTime"])
            b_end = datetime.datetime.fromisoformat(event["end"]["dateTime"])
            busy.append((b_start.astimezone(utc), b_end.astimezone(utc)))
    return busy


def get_available_slots(days_ahead=14):
    """Return [(start_dt, max_minutes), ...] of bookable start times in PT.

    Start times sit on a SLOT_GRID_MINUTES grid inside the daily window
    (AVAILABLE_START..AVAILABLE_END), weekdays only, at least MIN_LEAD_HOURS
    from now and at most days_ahead out. max_minutes is the longest meeting
    that fits from that start before the next busy event or end-of-day (capped
    at MAX_DURATION) so the UI can offer only durations that actually fit.
    """
    tz = pytz.timezone(TIMEZONE)
    utc = pytz.utc
    now = datetime.datetime.now(tz)
    earliest = now + datetime.timedelta(hours=MIN_LEAD_HOURS)

    candidates = []  # (start_dt, window_end_dt)
    day = now.date()
    days_checked = 0
    while days_checked < days_ahead:
        day += datetime.timedelta(days=1)
        if day.weekday() >= 5:  # Skip Saturday (5) and Sunday (6)
            continue
        days_checked += 1
        win_start = tz.localize(datetime.datetime(day.year, day.month, day.day, *AVAILABLE_START))
        win_end = tz.localize(datetime.datetime(day.year, day.month, day.day, *AVAILABLE_END))
        t = win_start
        while t + datetime.timedelta(minutes=MIN_DURATION) <= win_end:
            candidates.append((t, win_end))
            t += datetime.timedelta(minutes=SLOT_GRID_MINUTES)

    if not candidates:
        return []

    service = get_calendar_service()
    busy = _fetch_busy_intervals(service, candidates[0][0].isoformat(), candidates[-1][1].isoformat())

    result = []
    for start, win_end in candidates:
        if start < earliest:
            continue
        s_utc = start.astimezone(utc)
        # Start must not fall inside a busy interval.
        if any(b_start <= s_utc < b_end for b_start, b_end in busy):
            continue
        # Longest meeting that fits = until the next busy interval or end-of-day.
        limit = win_end
        for b_start, _b_end in busy:
            if b_start > s_utc:
                b_local = b_start.astimezone(tz)
                if b_local < limit:
                    limit = b_local
        max_min = min(int((limit - start).total_seconds() // 60), MAX_DURATION)
        if max_min >= MIN_DURATION:
            result.append((start, max_min))
    return result


def duration_fits(start_dt, duration_minutes, days_ahead=14):
    """Server-side guard: True if a meeting of duration_minutes can start at start_dt.

    Re-verifies against live availability so crafted POSTs can't book an
    arbitrary/past/too-long/taken slot.
    """
    if duration_minutes not in ALLOWED_DURATIONS:
        return False
    for slot_start, max_min in get_available_slots(days_ahead=days_ahead):
        if abs((slot_start - start_dt).total_seconds()) < 1:
            return duration_minutes <= max_min
    return False


def create_booking(attendee_name, attendee_email, start_dt, end_dt, notes=""):
    """Create a Google Calendar event with a Meet link and invite attendee + host.

    The event is created on this (Google) account so it carries a real Google
    Meet link, then invited to both the booker and MEETING_HOST_EMAIL (Outlook),
    so it lands on the Outlook calendar and the booker sees the host address.
    """
    service = get_calendar_service()

    description = notes.strip() if notes else ""
    if description:
        description += "\n\n"
    description += f"Contact: {MEETING_HOST_EMAIL}"

    event = {
        "summary": f"Logan Hartford <> {attendee_name}",
        "description": description,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": TIMEZONE},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": TIMEZONE},
        "attendees": [
            {"email": attendee_email, "displayName": attendee_name},
            {"email": MEETING_HOST_EMAIL, "displayName": MEETING_HOST_NAME},
        ],
        "reminders": {"useDefault": False, "overrides": []},
        "conferenceData": {
            "createRequest": {
                "requestId": uuid.uuid4().hex,
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
    }

    created = service.events().insert(
        calendarId="primary",
        body=event,
        sendUpdates="all",
        conferenceDataVersion=1,  # required for Google Meet link creation
    ).execute()

    return created


def get_upcoming_bookings(days_ahead=30):
    """List upcoming meetings on the primary calendar that have external attendees."""
    service = get_calendar_service()
    tz = pytz.timezone(TIMEZONE)
    now = datetime.datetime.now(tz)
    end = now + datetime.timedelta(days=days_ahead)

    events_result = service.events().list(
        calendarId="primary",
        timeMin=now.isoformat(),
        timeMax=end.isoformat(),
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    bookings = []
    for event in events_result.get("items", []):
        if "dateTime" not in event.get("start", {}):
            continue
        external = [a for a in event.get("attendees", []) if not a.get("self")]
        if not external:
            continue
        start_dt = datetime.datetime.fromisoformat(event["start"]["dateTime"]).astimezone(tz)
        bookings.append({
            "event_id": event["id"],
            "summary": event.get("summary", "Meeting"),
            "start_pt": start_dt.strftime("%a %b %-d at %-I:%M %p"),
            "attendee_email": external[0]["email"],
            "attendee_name": external[0].get("displayName", ""),
        })

    return bookings


def get_todays_events():
    """Return all timed, non-cancelled events for today across all calendars in PT."""
    service = get_calendar_service()
    tz = pytz.timezone(TIMEZONE)
    now = datetime.datetime.now(tz)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = now.replace(hour=23, minute=59, second=59, microsecond=0)

    seen_ids = set()
    events = []
    cal_list = service.calendarList().list().execute()
    for cal in cal_list.get("items", []):
        result = service.events().list(
            calendarId=cal["id"],
            timeMin=day_start.isoformat(),
            timeMax=day_end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        for e in result.get("items", []):
            if e.get("status") == "cancelled":
                continue
            if "dateTime" not in e.get("start", {}):
                continue
            if e["id"] in seen_ids:
                continue
            if e.get("description", "").startswith("[auto-"):
                continue
            seen_ids.add(e["id"])
            start_dt = datetime.datetime.fromisoformat(e["start"]["dateTime"]).astimezone(tz)
            events.append({
                "summary": e.get("summary", "Untitled"),
                "start_pt": start_dt.strftime("%-I:%M %p"),
                "start_dt": start_dt,
                "attendees": e.get("attendees", []),
                "description": e.get("description", ""),
            })

    events.sort(key=lambda e: e["start_dt"])
    return events


def reschedule_booking(event_id, new_start_dt, new_end_dt):
    """Update an existing calendar event to a new time and notify attendees."""
    service = get_calendar_service()
    event = service.events().get(calendarId="primary", eventId=event_id).execute()
    event["start"] = {"dateTime": new_start_dt.isoformat(), "timeZone": TIMEZONE}
    event["end"] = {"dateTime": new_end_dt.isoformat(), "timeZone": TIMEZONE}
    service.events().update(
        calendarId="primary",
        eventId=event_id,
        body=event,
        sendUpdates="all",
    ).execute()


def cancel_booking(event_id):
    """Delete a calendar event and send cancellation notification to attendees."""
    service = get_calendar_service()
    service.events().delete(
        calendarId="primary",
        eventId=event_id,
        sendUpdates="all",
    ).execute()
