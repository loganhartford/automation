"""Send a morning Telegram summary if there are meetings today."""
import json
import requests
from google.auth.exceptions import RefreshError
from calendar_api import get_todays_events
from telegram_notifier import notify

OLLAMA_BASE = "http://localhost:11434"
OLLAMA_MODEL = "qwen3:8b"

SYSTEM_PROMPT = """\
You classify calendar events. Reply ONLY with JSON: {"is_meeting": true} or {"is_meeting": false}.
A "meeting" is any event involving coordination or interaction with other people:
calls, standups, interviews, 1:1s, demos, reviews, syncs, etc.
NOT a meeting: personal reminders, focus blocks, OOO, appointments (dentist, gym), travel."""


def _is_meeting(event: dict) -> bool:
    user_msg = f"Event title: {event['summary']}"
    if event["attendees"]:
        user_msg += f"\nAttendees: {len(event['attendees'])}"
    try:
        resp = requests.post(
            f"{OLLAMA_BASE}/api/chat",
            json={
                "model": OLLAMA_MODEL,
                "think": False,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                "stream": False,
                "format": "json",
                "options": {"temperature": 0.1, "num_predict": 32},
            },
            timeout=15,
        )
        data = json.loads(resp.json()["message"]["content"])
        return bool(data.get("is_meeting", False))
    except Exception:
        return len(event["attendees"]) > 1


def main():
    events = get_todays_events()
    meetings = [e for e in events if _is_meeting(e)]
    if not meetings:
        return
    count = len(meetings)
    lines = [f"You have {count} meeting{'s' if count != 1 else ''} today:"]
    for m in meetings:
        lines.append(f"  {m['start_pt']} — {m['summary']}")
    notify("\n".join(lines))


if __name__ == "__main__":
    try:
        main()
    except RefreshError:
        notify("Morning meeting notify: calendar auth token expired — re-authorization required.\nDelete calendar_token.pickle and run the OAuth flow again.")
    except Exception as e:
        notify(f"Morning meeting notify error: {e}")
