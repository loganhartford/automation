import os
import re
import time
import logging
import datetime
import threading
import requests
from flask import Flask, render_template, request, redirect, url_for, abort
from itsdangerous import URLSafeSerializer, BadSignature
from dotenv import load_dotenv
from calendar_api import (
    get_available_slots, create_booking, duration_fits, ALLOWED_DURATIONS,
    get_booking, reschedule_booking,
)
from telegram_notifier import notify
from telegram_bot import handle_message, send_telegram_message

load_dotenv()

log = logging.getLogger("schedule")

app = Flask(__name__)

DAYS_AHEAD = 14
DEFAULT_DURATION = 30
MAX_NAME_LEN = 120
MAX_NOTES_LEN = 1000
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Availability is expensive to compute (several sequential Google Calendar API
# calls), so we never do it on the request path. A background thread refreshes
# this cache every REFRESH_INTERVAL seconds; page loads just read it. A booking
# also kicks an immediate async refresh so the just-taken slot disappears fast.
# (Run under a single gunicorn worker so this cache is shared process-wide.)
REFRESH_INTERVAL = 300  # 5 minutes
_cache_lock = threading.Lock()
_slots_cache = {"grouped": [], "ts": 0.0}


def _refresh_slots():
    """Recompute available slots and atomically swap them into the cache."""
    grouped = _group_slots(get_available_slots(days_ahead=DAYS_AHEAD))
    with _cache_lock:
        _slots_cache["grouped"] = grouped
        _slots_cache["ts"] = time.time()


def _refresh_slots_async():
    """Fire-and-forget cache refresh (used after a booking; never blocks the response)."""
    threading.Thread(target=_safe_refresh, daemon=True).start()


def _safe_refresh():
    try:
        _refresh_slots()
    except Exception:
        log.exception("slot cache refresh failed")


def _background_refresher():
    """Refresh immediately on boot, then every REFRESH_INTERVAL seconds forever."""
    while True:
        _safe_refresh()
        time.sleep(REFRESH_INTERVAL)


def _slot_available_fast(start_dt, duration):
    """Validate a submitted (start, duration) against the in-memory cache (instant)
    instead of a fresh ~2s API fetch. Falls back to a live check only on a cold cache."""
    if duration not in ALLOWED_DURATIONS:
        return False
    with _cache_lock:
        grouped = _slots_cache["grouped"]
        ts = _slots_cache["ts"]
    if ts == 0.0:
        return duration_fits(start_dt, duration, days_ahead=DAYS_AHEAD)
    for _date, slots in grouped:
        for slot_start, max_min in slots:
            if abs((slot_start - start_dt).total_seconds()) < 1:
                return duration <= max_min
    return False

TURNSTILE_SITE_KEY = os.getenv("TURNSTILE_SITE_KEY", "")
TURNSTILE_SECRET_KEY = os.getenv("TURNSTILE_SECRET_KEY", "")
TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

# Reschedule tokens are signed (not encrypted) — the event id is derivable, so
# the signature is just tamper protection. Reuses WEBHOOK_SECRET as the signing
# key so we don't have to manage a second secret; salted separately.
_RESCHEDULE_SALT = "meet-reschedule-v1"
_SIGNING_KEY = os.getenv("WEBHOOK_SECRET") or "insecure-dev-key-set-WEBHOOK_SECRET"
_reschedule_signer = URLSafeSerializer(_SIGNING_KEY, salt=_RESCHEDULE_SALT)
MEET_BASE_URL = os.getenv("MEET_BASE_URL", "https://meet.lhartford.com")


def _make_reschedule_token(event_id):
    return _reschedule_signer.dumps(event_id)


def _reschedule_url_for(event_id):
    return f"{MEET_BASE_URL}/reschedule/{_make_reschedule_token(event_id)}"


def _event_id_from_token(token):
    try:
        return _reschedule_signer.loads(token)
    except BadSignature:
        return None


def _verify_turnstile(token, remote_ip=None):
    """Verify a Cloudflare Turnstile token. No-ops (returns True) when unconfigured
    so local/dev runs without keys still work; enforced as soon as the secret is set."""
    if not TURNSTILE_SECRET_KEY:
        return True
    if not token:
        return False
    try:
        resp = requests.post(
            TURNSTILE_VERIFY_URL,
            data={"secret": TURNSTILE_SECRET_KEY, "response": token, "remoteip": remote_ip or ""},
            timeout=5,
        )
        return bool(resp.json().get("success"))
    except Exception:
        return False


def _group_slots(slots):
    """Group (start, max_minutes) tuples by date -> sorted list of (date, slots)."""
    grouped = {}
    for start, max_min in slots:
        grouped.setdefault(start.date(), []).append((start, max_min))
    return sorted(grouped.items())


@app.route("/")
def index():
    with _cache_lock:
        grouped = _slots_cache["grouped"]
        ts = _slots_cache["ts"]
    # Cold start only: cache not populated yet (first request before the
    # background thread's first refresh lands) — fetch once inline.
    if ts == 0.0:
        _safe_refresh()
        with _cache_lock:
            grouped = _slots_cache["grouped"]
    return render_template(
        "book.html",
        days=grouped,
        durations=ALLOWED_DURATIONS,
        default_duration=DEFAULT_DURATION,
        turnstile_sitekey=TURNSTILE_SITE_KEY,
    )


@app.route("/book", methods=["POST"])
def book():
    name = (request.form.get("name") or "").strip()[:MAX_NAME_LEN]
    email = (request.form.get("email") or "").strip()
    slot_str = (request.form.get("slot") or "").strip()
    notes = (request.form.get("notes") or "").strip()[:MAX_NOTES_LEN]

    # Bot/spam gate — verify the Turnstile token before doing any work.
    if not _verify_turnstile(request.form.get("cf-turnstile-response", ""),
                             request.headers.get("CF-Connecting-IP")):
        return redirect(url_for("index"))

    # Validate inputs — reject anything malformed rather than creating junk events.
    if not name or not EMAIL_RE.match(email) or not slot_str:
        return redirect(url_for("index"))

    try:
        start_dt = datetime.datetime.fromisoformat(slot_str)
        duration = int(request.form.get("duration", ""))
    except ValueError:
        return redirect(url_for("index"))

    # Only allow a (start, duration) we currently offer (prevents crafted POSTs
    # booking arbitrary/past/too-long times). Validated against the fresh cache so
    # this stays instant; create_booking below is the only live call on this path.
    if not _slot_available_fast(start_dt, duration):
        return redirect(url_for("index"))

    end_dt = start_dt + datetime.timedelta(minutes=duration)
    create_booking(name, email, start_dt, end_dt, notes,
                   reschedule_url_fn=_reschedule_url_for)

    day_str = start_dt.strftime("%a %b %-d, %-I:%M %p")
    notify(f"\U0001f4c5 New booking: {name} ({email}) — {day_str} PT ({duration} min)")

    # Refresh availability in the background so the just-booked slot drops out
    # of the cache promptly, without delaying this response.
    _refresh_slots_async()

    return redirect(url_for("booked", name=name, email=email,
                            slot=start_dt.isoformat(), duration=duration))


@app.route("/booked")
def booked():
    name = request.args.get("name", "")
    email = request.args.get("email", "")
    slot_str = request.args.get("slot", "")
    try:
        start_dt = datetime.datetime.fromisoformat(slot_str)
        duration = int(request.args.get("duration", DEFAULT_DURATION))
    except ValueError:
        return redirect(url_for("index"))
    end_dt = start_dt + datetime.timedelta(minutes=duration)
    return render_template("booked.html", name=name, email=email,
                           start_dt=start_dt, end_dt=end_dt, duration=duration)


@app.route("/reschedule/<token>", methods=["GET"])
def reschedule(token):
    event_id = _event_id_from_token(token)
    if not event_id:
        abort(404)
    booking = get_booking(event_id)
    if not booking:
        # Event was cancelled or otherwise gone — nothing to reschedule.
        return render_template("reschedule_missing.html"), 404

    with _cache_lock:
        grouped = _slots_cache["grouped"]
        ts = _slots_cache["ts"]
    if ts == 0.0:
        _safe_refresh()
        with _cache_lock:
            grouped = _slots_cache["grouped"]

    return render_template(
        "reschedule.html",
        booking=booking,
        days=grouped,
        durations=ALLOWED_DURATIONS,
        default_duration=booking["duration"],
        token=token,
        turnstile_sitekey=TURNSTILE_SITE_KEY,
    )


@app.route("/reschedule/<token>", methods=["POST"])
def reschedule_submit(token):
    event_id = _event_id_from_token(token)
    if not event_id:
        abort(404)
    booking = get_booking(event_id)
    if not booking:
        return render_template("reschedule_missing.html"), 404

    if not _verify_turnstile(request.form.get("cf-turnstile-response", ""),
                             request.headers.get("CF-Connecting-IP")):
        return redirect(url_for("reschedule", token=token))

    slot_str = (request.form.get("slot") or "").strip()
    try:
        start_dt = datetime.datetime.fromisoformat(slot_str)
        duration = int(request.form.get("duration", ""))
    except ValueError:
        return redirect(url_for("reschedule", token=token))

    if not _slot_available_fast(start_dt, duration):
        return redirect(url_for("reschedule", token=token))

    end_dt = start_dt + datetime.timedelta(minutes=duration)
    reschedule_booking(event_id, start_dt, end_dt)

    day_str = start_dt.strftime("%a %b %-d, %-I:%M %p")
    old_day_str = booking["start_dt"].strftime("%a %b %-d, %-I:%M %p")
    notify(f"\U0001f504 Reschedule: {booking['attendee_name']} ({booking['attendee_email']}) "
           f"— {old_day_str} → {day_str} PT ({duration} min)")

    _refresh_slots_async()

    return redirect(url_for("rescheduled",
                            name=booking["attendee_name"],
                            email=booking["attendee_email"],
                            slot=start_dt.isoformat(),
                            duration=duration))


@app.route("/rescheduled")
def rescheduled():
    name = request.args.get("name", "")
    email = request.args.get("email", "")
    slot_str = request.args.get("slot", "")
    try:
        start_dt = datetime.datetime.fromisoformat(slot_str)
        duration = int(request.args.get("duration", DEFAULT_DURATION))
    except ValueError:
        return redirect(url_for("index"))
    end_dt = start_dt + datetime.timedelta(minutes=duration)
    return render_template("rescheduled.html", name=name, email=email,
                           start_dt=start_dt, end_dt=end_dt, duration=duration)


@app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if secret != os.getenv("WEBHOOK_SECRET", ""):
        return "", 403
    update = request.get_json(silent=True) or {}
    message = update.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    text = message.get("text", "").strip()
    if chat_id and text:
        reply = handle_message(chat_id, text)
        if reply:
            send_telegram_message(chat_id, reply)
    return "", 200


# Start the availability refresher as soon as the app is imported (works under
# gunicorn, which imports this module rather than running __main__). Daemon so it
# dies with the worker. Run with a single worker so there is one shared cache.
threading.Thread(target=_background_refresher, daemon=True).start()


if __name__ == "__main__":
    # Local testing only. In production this runs under gunicorn (see runbook),
    # never the Flask dev server, and never with debug=True.
    app.run(host="127.0.0.1", port=5001)
