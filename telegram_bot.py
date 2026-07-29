import os
import time
import datetime
import logging
import pytz
import anthropic
import requests
from dotenv import load_dotenv
from google.auth.exceptions import RefreshError
from calendar_api import get_upcoming_bookings, reschedule_booking, cancel_booking, create_event
from gmail import send_calendar_email

load_dotenv()

TIMEZONE = "America/Vancouver"
MAX_HISTORY = 20

_conversation_history = {}

SYSTEM_PROMPT = """You are Logan's scheduling assistant. You manage Logan's Google Calendar.

You can list upcoming bookings, reschedule them, cancel them, send a custom email to a participant, or create new events (blocks, holds, personal events, busy blocks, etc.). When a request is ambiguous (e.g. multiple bookings or unclear time), ask a clarifying question before acting. Keep replies brief. You are communicating via Telegram, so use plain text only — no markdown formatting.

Current time: {now} (Pacific Time)"""

TOOLS = [
    {
        "name": "list_bookings",
        "description": "List upcoming bookings (meetings with external attendees on Logan's primary calendar).",
        "input_schema": {
            "type": "object",
            "properties": {
                "days_ahead": {
                    "type": "integer",
                    "description": "How many days ahead to look. Default 30."
                }
            }
        }
    },
    {
        "name": "reschedule_booking",
        "description": "Move a booking to a new time. Duration stays 30 minutes. Google Calendar notifies the attendee automatically.",
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "new_start_iso": {
                    "type": "string",
                    "description": "New start time in ISO 8601 with UTC offset, e.g. 2026-05-05T10:00:00-07:00"
                }
            },
            "required": ["event_id", "new_start_iso"]
        }
    },
    {
        "name": "cancel_booking",
        "description": "Cancel a booking. Google Calendar sends a cancellation notification to the attendee.",
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string"}
            },
            "required": ["event_id"]
        }
    },
    {
        "name": "email_participant",
        "description": "Send a plain-text email to a booking participant.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to_email": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"}
            },
            "required": ["to_email", "subject", "body"]
        }
    },
    {
        "name": "create_event",
        "description": "Create a calendar event with no external attendees. Use for busy blocks, holds, personal events, or anything that doesn't involve inviting someone.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Event title"},
                "start_iso": {"type": "string", "description": "Start time in ISO 8601 with UTC offset, e.g. 2026-07-28T09:00:00-07:00"},
                "end_iso": {"type": "string", "description": "End time in ISO 8601 with UTC offset"},
                "description": {"type": "string", "description": "Optional event description"}
            },
            "required": ["title", "start_iso", "end_iso"]
        }
    }
]


def _execute_tool(name, tool_input):
    try:
        if name == "list_bookings":
            days = tool_input.get("days_ahead", 30)
            bookings = get_upcoming_bookings(days_ahead=days)
            if not bookings:
                return "No upcoming bookings."
            return "\n".join(
                f"- {b['summary']} | {b['start_pt']} | {b['attendee_email']} | id:{b['event_id']}"
                for b in bookings
            )

        if name == "reschedule_booking":
            new_start = datetime.datetime.fromisoformat(tool_input["new_start_iso"])
            new_end = new_start + datetime.timedelta(minutes=30)
            reschedule_booking(tool_input["event_id"], new_start, new_end)
            tz = pytz.timezone(TIMEZONE)
            label = new_start.astimezone(tz).strftime("%a %b %-d at %-I:%M %p PT")
            return f"Rescheduled to {label}. Calendar invite updated."

        if name == "cancel_booking":
            cancel_booking(tool_input["event_id"])
            return "Booking cancelled. Attendee notified via Google Calendar."

        if name == "email_participant":
            send_calendar_email(tool_input["to_email"], tool_input["subject"], tool_input["body"])
            return f"Email sent to {tool_input['to_email']}."

        if name == "create_event":
            start = datetime.datetime.fromisoformat(tool_input["start_iso"])
            end = datetime.datetime.fromisoformat(tool_input["end_iso"])
            tz = pytz.timezone(TIMEZONE)
            create_event(tool_input["title"], start, end, tool_input.get("description", ""))
            label = start.astimezone(tz).strftime("%a %b %-d at %-I:%M %p PT")
            return f"Created \"{tool_input['title']}\" on {label}."

        return f"Unknown tool: {name}"

    except RefreshError:
        return (
            "Google Calendar auth token expired. "
            "Delete calendar_token.pickle and re-run the OAuth flow on the server."
        )
    except Exception as e:
        logging.error("Tool %s error: %s", name, e)
        return f"Tool error: {e}"


def handle_message(chat_id, text):
    """Process an incoming Telegram message and return a reply string.

    Returns None if the message is from an unauthorized chat_id.
    """
    allowed = os.getenv("TELEGRAM_SCHEDULER_CHAT_ID", "")
    if str(chat_id) != str(allowed):
        return None

    history = _conversation_history.setdefault(str(chat_id), [])
    history.append({"role": "user", "content": text})

    tz = pytz.timezone(TIMEZONE)
    now_str = datetime.datetime.now(tz).strftime("%A, %B %-d %Y, %-I:%M %p")
    system = SYSTEM_PROMPT.format(now=now_str)

    client = anthropic.Anthropic()
    reply = "Sorry, something went wrong."

    while True:
        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=1024,
            system=system,
            tools=TOOLS,
            messages=history[-MAX_HISTORY:],
        )

        if response.stop_reason == "end_turn":
            reply = next(
                (b.text for b in response.content if hasattr(b, "text")),
                "Done."
            )
            history.append({"role": "assistant", "content": response.content})
            break

        if response.stop_reason == "tool_use":
            history.append({"role": "assistant", "content": response.content})
            results = [
                {
                    "type": "tool_result",
                    "tool_use_id": b.id,
                    "content": _execute_tool(b.name, b.input),
                }
                for b in response.content
                if b.type == "tool_use"
            ]
            history.append({"role": "user", "content": results})
            continue

        break

    if len(history) > MAX_HISTORY:
        _conversation_history[str(chat_id)] = history[-MAX_HISTORY:]

    return reply


def send_telegram_message(chat_id, text):
    token = os.getenv("TELEGRAM_SCHEDULER_BOT_TOKEN", "")
    if not token:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
    except Exception:
        pass


def _send_long(chat_id, text):
    """Send a potentially long message in 4000-char chunks."""
    for i in range(0, len(text), 4000):
        send_telegram_message(chat_id, text[i:i + 4000])


def _handle_repair(chat_id, command: str):
    """Synchronous repair handler for the scheduler bot's polling loop."""
    from repair import load_error_context, investigate, apply_fix
    ctx = load_error_context()
    if not ctx:
        send_telegram_message(chat_id, "No recent error context found. An error must occur first.")
        return

    age_h = (datetime.datetime.now() - datetime.datetime.fromisoformat(ctx["timestamp"])).total_seconds() / 3600
    header = f"Last error: {ctx['script']} — {ctx['error_type']} ({age_h:.1f}h ago)\n\n"

    if command == "investigate":
        send_telegram_message(chat_id, f"Investigating {ctx['script']} error... (~1 min)")
        try:
            result = investigate(ctx)
        except Exception as e:
            send_telegram_message(chat_id, f"Claude error: {e}")
            return
        _send_long(chat_id, header + result)

    elif command in ("apply", "apply fix"):
        send_telegram_message(chat_id, f"Fixing {ctx['script']} error... (~3 min)")
        try:
            output, diff = apply_fix(ctx)
        except Exception as e:
            send_telegram_message(chat_id, f"Claude error: {e}")
            return
        _send_long(chat_id, header + output)
        if diff:
            _send_long(chat_id, f"Changes:\n\n{diff}")
        else:
            send_telegram_message(chat_id, "No file changes were made.")


logging.basicConfig(
    filename=os.path.join(os.path.dirname(__file__), "logs", "schedule.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


def _deregister_webhook():
    token = os.getenv("TELEGRAM_SCHEDULER_BOT_TOKEN", "")
    if not token:
        return
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/deleteWebhook", timeout=10)
        logging.info("deleteWebhook: %s", r.json())
    except Exception as e:
        logging.warning("deleteWebhook failed: %s", e)


def run():
    token = os.getenv("TELEGRAM_SCHEDULER_BOT_TOKEN", "")
    if not token:
        logging.error("TELEGRAM_SCHEDULER_BOT_TOKEN not set, exiting.")
        return

    _deregister_webhook()
    logging.info("Scheduler bot starting long-polling loop.")

    offset = None
    while True:
        try:
            params = {"timeout": 30, "allowed_updates": ["message"]}
            if offset is not None:
                params["offset"] = offset
            r = requests.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                params=params,
                timeout=40,
            )
            data = r.json()
            if not data.get("ok"):
                logging.warning("getUpdates not ok: %s", data)
                time.sleep(5)
                continue
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message", {})
                chat_id = message.get("chat", {}).get("id")
                text = message.get("text", "").strip()
                if chat_id and text:
                    try:
                        cmd = text.strip().lower()
                        if cmd in ("investigate", "apply", "apply fix"):
                            _handle_repair(chat_id, cmd)
                        else:
                            reply = handle_message(chat_id, text)
                            if reply:
                                send_telegram_message(chat_id, reply)
                    except Exception as e:
                        logging.error("Error handling message from %s: %s", chat_id, e)
        except requests.exceptions.ReadTimeout:
            continue
        except Exception as e:
            logging.error("Polling error: %s", e)
            time.sleep(5)


if __name__ == "__main__":
    run()
