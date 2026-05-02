import os
import pickle
import datetime
import pytz
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar"]
CREDENTIALS_FILE = "credentials.json"
CALENDAR_TOKEN_FILE = "calendar_token.pickle"
CALENDAR_ID = "primary"
TIMEZONE = "America/Vancouver"
SLOT_DURATION_MINUTES = 30
AVAILABLE_START_HOUR = 10  # 10:00 AM
AVAILABLE_END_HOUR = 15    # 3:00 PM — last slot starts at 2:30


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
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(CALENDAR_TOKEN_FILE, "wb") as f:
            pickle.dump(creds, f)
    return build("calendar", "v3", credentials=creds)


def get_free_slots(days_ahead=14):
    """Return list of (start_dt, end_dt) available 30-min slots in PT.

    Checks Mon-Fri 10am-3pm PT against Google Calendar freebusy, returning
    only slots with no overlapping events.
    """
    tz = pytz.timezone(TIMEZONE)
    now = datetime.datetime.now(tz)

    candidate_slots = []
    day = now.date()
    days_checked = 0
    while days_checked < days_ahead:
        day += datetime.timedelta(days=1)
        if day.weekday() >= 5:  # Skip Saturday (5) and Sunday (6)
            continue
        days_checked += 1

        hour = AVAILABLE_START_HOUR
        minute = 0
        while hour < AVAILABLE_END_HOUR:
            start = tz.localize(datetime.datetime(day.year, day.month, day.day, hour, minute))
            end = start + datetime.timedelta(minutes=SLOT_DURATION_MINUTES)
            candidate_slots.append((start, end))
            total = hour * 60 + minute + SLOT_DURATION_MINUTES
            hour = total // 60
            minute = total % 60

    if not candidate_slots:
        return []

    service = get_calendar_service()
    range_start = candidate_slots[0][0].isoformat()
    range_end = candidate_slots[-1][1].isoformat()

    result = service.freebusy().query(body={
        "timeMin": range_start,
        "timeMax": range_end,
        "items": [{"id": CALENDAR_ID}],
    }).execute()

    utc = pytz.utc
    busy = []
    for interval in result["calendars"][CALENDAR_ID]["busy"]:
        b_start = datetime.datetime.fromisoformat(interval["start"].replace("Z", "+00:00"))
        b_end = datetime.datetime.fromisoformat(interval["end"].replace("Z", "+00:00"))
        busy.append((b_start.astimezone(utc), b_end.astimezone(utc)))

    free = []
    for slot_start, slot_end in candidate_slots:
        s_utc = slot_start.astimezone(utc)
        e_utc = slot_end.astimezone(utc)
        if not any(s_utc < b_end and e_utc > b_start for b_start, b_end in busy):
            free.append((slot_start, slot_end))

    return free


def create_booking(attendee_name, attendee_email, start_dt, end_dt, notes=""):
    """Create a Google Calendar event and send an invite to the attendee."""
    service = get_calendar_service()

    event = {
        "summary": f"Meeting with {attendee_name}",
        "description": notes or "",
        "start": {"dateTime": start_dt.isoformat(), "timeZone": TIMEZONE},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": TIMEZONE},
        "attendees": [{"email": attendee_email, "displayName": attendee_name}],
        "reminders": {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": 5}],
        },
    }

    return service.events().insert(
        calendarId=CALENDAR_ID,
        body=event,
        sendUpdates="all",
    ).execute()
