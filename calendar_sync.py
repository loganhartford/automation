"""Sync non-primary calendar events to primary as busy blocks, and add availability blockers."""
import json
import os
import random
import logging
import datetime
import pytz

STATE_FILE = os.path.join(os.path.dirname(__file__), "logs", "calendar_sync_state.json")
TIMEZONE = "America/Vancouver"
SYNC_TAG = "[auto-sync]"
BLOCKER_TAG = "[auto-blocker]"

log = logging.getLogger(__name__)


def _load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"synced": {}, "blockers": {}}


def _save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def _sync_busy_events(service, state):
    """Copy timed events from non-primary calendars to primary as 'Busy' blocks."""
    tz = pytz.timezone(TIMEZONE)
    now = datetime.datetime.now(tz)
    time_min = now.isoformat()
    time_max = (now + datetime.timedelta(days=45)).isoformat()

    cal_list = service.calendarList().list().execute().get("items", [])
    primary_id = next((c["id"] for c in cal_list if c.get("primary")), "primary")
    other_cals = [c for c in cal_list if not c.get("primary")]

    synced = state.get("synced", {})
    active_keys = set()

    for cal in other_cals:
        try:
            result = service.events().list(
                calendarId=cal["id"],
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
            ).execute()
        except Exception as e:
            log.warning("calendar_sync: skipping %s: %s", cal.get("summary", cal["id"]), e)
            continue

        for event in result.get("items", []):
            if "dateTime" not in event.get("start", {}):
                continue

            key = event["id"]
            active_keys.add(key)

            if event.get("status") == "cancelled":
                if key in synced:
                    try:
                        service.events().delete(calendarId=primary_id, eventId=synced[key]).execute()
                    except Exception:
                        pass
                    del synced[key]
                continue

            if key in synced:
                try:
                    service.events().patch(
                        calendarId=primary_id,
                        eventId=synced[key],
                        body={"start": event["start"], "end": event["end"]},
                    ).execute()
                    continue
                except Exception:
                    del synced[key]

            try:
                created = service.events().insert(
                    calendarId=primary_id,
                    body={
                        "summary": "Busy",
                        "start": event["start"],
                        "end": event["end"],
                        "transparency": "opaque",
                        "description": SYNC_TAG,
                    },
                ).execute()
                synced[key] = created["id"]
                log.info("calendar_sync: synced event %s", key)
            except Exception as e:
                log.warning("calendar_sync: failed to create sync event: %s", e)

    # Remove copies whose source event has passed or been deleted
    for key in [k for k in synced if k not in active_keys]:
        try:
            service.events().delete(calendarId=primary_id, eventId=synced[key]).execute()
        except Exception:
            pass
        del synced[key]

    state["synced"] = synced


def _add_availability_blockers(service, state):
    """Add a 1-hour busy block on weekdays in the next 7 days that have no events."""
    tz = pytz.timezone(TIMEZONE)
    now = datetime.datetime.now(tz)
    blockers = state.get("blockers", {})

    # Prune past entries
    today = now.date().isoformat()
    blockers = {d: eid for d, eid in blockers.items() if d >= today}

    # Random start slots: 11:00–14:00 in 30-min steps (1hr block ends by 15:00)
    slots = [(h, m) for h in range(11, 15) for m in (0, 30) if not (h == 14 and m == 30)]

    for i in range(1, 15):
        day = now.date() + datetime.timedelta(days=i)
        if day.weekday() >= 5:
            continue

        day_str = day.isoformat()

        # Verify existing blocker still exists in Google Calendar
        if day_str in blockers:
            try:
                service.events().get(calendarId="primary", eventId=blockers[day_str]).execute()
                continue
            except Exception:
                del blockers[day_str]

        # Check primary calendar for any events that day (includes synced busy blocks)
        day_start = tz.localize(datetime.datetime(day.year, day.month, day.day, 0, 0, 0))
        day_end = tz.localize(datetime.datetime(day.year, day.month, day.day, 23, 59, 59))
        result = service.events().list(
            calendarId="primary",
            timeMin=day_start.isoformat(),
            timeMax=day_end.isoformat(),
            singleEvents=True,
        ).execute()

        has_events = any(
            e.get("status") != "cancelled" and "dateTime" in e.get("start", {})
            for e in result.get("items", [])
        )
        if has_events:
            continue

        hour, minute = random.choice(slots)
        start_dt = tz.localize(datetime.datetime(day.year, day.month, day.day, hour, minute))
        end_dt = start_dt + datetime.timedelta(hours=1)

        try:
            created = service.events().insert(
                calendarId="primary",
                body={
                    "summary": "Busy",
                    "start": {"dateTime": start_dt.isoformat(), "timeZone": TIMEZONE},
                    "end": {"dateTime": end_dt.isoformat(), "timeZone": TIMEZONE},
                    "transparency": "opaque",
                    "description": BLOCKER_TAG,
                },
            ).execute()
            blockers[day_str] = created["id"]
            log.info("calendar_sync: added blocker on %s at %s", day_str, start_dt.strftime("%-I:%M %p"))
        except Exception as e:
            log.warning("calendar_sync: failed to add blocker: %s", e)

    state["blockers"] = blockers


def run(service):
    state = _load_state()
    _sync_busy_events(service, state)
    _add_availability_blockers(service, state)
    _save_state(state)
