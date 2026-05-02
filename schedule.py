import datetime
from flask import Flask, render_template, request, redirect, url_for
from dotenv import load_dotenv
from calendar_api import get_free_slots, create_booking
from telegram_notifier import notify

load_dotenv()

app = Flask(__name__)


def _group_slots(slots):
    """Group (start, end) slot tuples by date, return sorted list of (date, slots)."""
    grouped = {}
    for start, end in slots:
        key = start.date()
        grouped.setdefault(key, []).append((start, end))
    return sorted(grouped.items())


@app.route("/")
def index():
    slots = get_free_slots(days_ahead=14)
    days = _group_slots(slots)
    return render_template("book.html", days=days)


@app.route("/book", methods=["POST"])
def book():
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()
    slot_str = request.form.get("slot", "").strip()
    notes = request.form.get("notes", "").strip()

    if not name or not email or not slot_str:
        return redirect(url_for("index"))

    start_dt = datetime.datetime.fromisoformat(slot_str)
    end_dt = start_dt + datetime.timedelta(minutes=30)

    create_booking(name, email, start_dt, end_dt, notes)

    day_str = start_dt.strftime("%a %b %-d, %-I:%M %p")
    notify(f"\U0001f4c5 New booking: {name} ({email}) — {day_str} PT")

    return redirect(url_for("booked", name=name, email=email, slot=start_dt.isoformat()))


@app.route("/booked")
def booked():
    name = request.args.get("name", "")
    email = request.args.get("email", "")
    slot_str = request.args.get("slot", "")
    start_dt = datetime.datetime.fromisoformat(slot_str)
    end_dt = start_dt + datetime.timedelta(minutes=30)
    return render_template("booked.html", name=name, email=email, start_dt=start_dt, end_dt=end_dt)


if __name__ == "__main__":
    app.run(debug=True, port=5001)
