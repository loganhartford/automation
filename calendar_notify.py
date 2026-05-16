import json
import os
import datetime
import logging
import pytz
from dotenv import load_dotenv
from googleapiclient.errors import HttpError
from calendar_api import get_calendar_service
from telegram_notifier import notify

load_dotenv()

STATE_FILE = os.path.join(os.path.dirname(__file__), "logs", "calendar_notify_state.json")
TIMEZONE = "America/Vancouver"

logging.basicConfig(
    filename=os.path.join(os.path.dirname(__file__), "logs", "schedule.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


def _load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"last_checked": None, "notified_ids": []}


def _save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def run():
    state = _load_state()
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    tz = pytz.timezone(TIMEZONE)

    if state["last_checked"] is None:
        logging.info("calendar_notify: first run — setting baseline, no notifications sent.")
        state["last_checked"] = now_utc.isoformat()
        _save_state(state)
        return

    updated_min = state["last_checked"]
    notified_ids = set(state.get("notified_ids", []))

    try:
        service = get_calendar_service()
        result = service.events().list(
            calendarId="primary",
            updatedMin=updated_min,
            singleEvents=True,
            orderBy="updated",
        ).execute()
    except HttpError as e:
        logging.error("calendar_notify: Calendar API error: %s", e)
        return

    for event in result.get("items", []):
        event_id = event["id"]
        if event_id in notified_ids:
            continue
        if "dateTime" not in event.get("start", {}):
            continue
        if event.get("status") == "cancelled":
            notified_ids.add(event_id)
            continue
        external = [a for a in event.get("attendees", []) if not a.get("self")]
        if not external:
            continue
        start_dt = datetime.datetime.fromisoformat(event["start"]["dateTime"])
        if start_dt.astimezone(datetime.timezone.utc) <= now_utc:
            continue

        start_local = start_dt.astimezone(tz)
        time_str = start_local.strftime("%a %b %-d at %-I:%M %p PT")
        attendee = external[0]
        attendee_str = attendee.get("displayName") or attendee.get("email", "Unknown")
        summary = event.get("summary", "Meeting")

        message = f"New calendar event: {summary}\n{attendee_str} — {time_str}"
        notify(message)
        notified_ids.add(event_id)
        logging.info("calendar_notify: notified: %s", message)

    state["last_checked"] = now_utc.isoformat()
    state["notified_ids"] = list(notified_ids)
    _save_state(state)


if __name__ == "__main__":
    run()
