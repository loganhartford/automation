"""Weekly automation status report — emailed from scout account to logan.hartford@outlook.com."""
import json
import os
import re
import sqlite3
import subprocess
import traceback
from datetime import datetime, timedelta
from html import escape

import requests
from dotenv import load_dotenv

from gmail import send_report

load_dotenv()

DB_PATH = "scout.db"
TRIAGE_LOG = "logs/triage.log"
URGENT_STATE_FILE = "logs/triage_urgent_pending.json"

_SERVICES = {
    "scout-bot.service": "Scout Bot",
    "scout-schedule.service": "Scheduler Bot",
}

_TIMERS = {
    "scout-ingest.timer": "Scout Ingest",
    "scout-report.timer": "Scout Report",
    "scout-calendar-notify.timer": "Calendar Notify",
}


def _notify_telegram(msg):
    token = os.getenv("TELEGRAM_SCOUT_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_SCOUT_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg},
            timeout=10,
        )
    except Exception:
        pass


def _unit_status(unit):
    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-active", unit],
            capture_output=True, text=True, timeout=5,
        )
        return r.stdout.strip()
    except Exception:
        return "unknown"


def _status_badge(s):
    if s == "active":
        return '<span style="color:#2e7d32;font-weight:bold">● active</span>'
    if s in ("inactive", "failed"):
        return f'<span style="color:#c62828;font-weight:bold">● {escape(s)}</span>'
    return f'<span style="color:#888">{escape(s)}</span>'


def _scout_stats(since):
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        rows = conn.execute(
            "SELECT name, passed_dealbreakers, cost FROM companies WHERE first_seen >= ?",
            (since.isoformat(),),
        ).fetchall()
        conn.close()
    except Exception:
        return None
    passed = [r for r in rows if r[1]]
    failed = [r for r in rows if not r[1]]
    cost = sum(r[2] for r in rows if r[2] is not None)
    return {
        "total": len(rows),
        "passed": passed,
        "failed": failed,
        "cost": cost,
    }


def _triage_stats(since):
    if not os.path.exists(TRIAGE_LOG):
        return None
    totals = {"time_sensitive": 0, "keep": 0, "trash": 0, "unsubscribe": 0, "error": 0}
    runs = 0
    try:
        with open(TRIAGE_LOG) as f:
            for line in f:
                try:
                    ts = datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S,%f")
                except ValueError:
                    continue
                if ts < since:
                    continue
                if "=== Done |" not in line:
                    continue
                runs += 1
                for key in totals:
                    label = {
                        "time_sensitive": "Time-sensitive",
                        "keep": "Kept",
                        "trash": "Trashed",
                        "unsubscribe": "Unsubbed",
                        "error": "Errors",
                    }[key]
                    m = re.search(rf"{label}:\s*(\d+)", line)
                    if m:
                        totals[key] += int(m.group(1))
    except Exception:
        return None
    return {"runs": runs, **totals}


def _pending_urgent():
    try:
        if os.path.exists(URGENT_STATE_FILE):
            with open(URGENT_STATE_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return []


def generate_status_report():
    since = datetime.now() - timedelta(days=7)
    date_str = datetime.now().strftime("%B %d, %Y")

    scout = _scout_stats(since)
    triage = _triage_stats(since)
    pending = _pending_urgent()

    svc_statuses = {svc: _unit_status(svc) for svc in _SERVICES}
    timer_statuses = {t: _unit_status(t) for t in _TIMERS}

    html = f"""<html>
<body style="font-family:sans-serif;max-width:720px;margin:40px auto;color:#222;line-height:1.6;font-size:15px;">
<h1 style="border-bottom:2px solid #eee;padding-bottom:8px">Automation Status — {date_str}</h1>
<p style="color:#666;font-size:13px">Last 7 days. Generated {datetime.now().strftime("%Y-%m-%d %H:%M")}.</p>

<h2>Services</h2>
<table style="border-collapse:collapse;font-size:14px">
"""
    for svc, label in _SERVICES.items():
        html += f'<tr><td style="padding:3px 16px 3px 0"><b>{escape(label)}</b></td><td>{_status_badge(svc_statuses[svc])}</td></tr>\n'
    for timer, label in _TIMERS.items():
        html += f'<tr><td style="padding:3px 16px 3px 0">{escape(label)} (timer)</td><td>{_status_badge(timer_statuses[timer])}</td></tr>\n'
    html += "</table>\n<hr>\n"

    # --- Startup Scout ---
    html += "<h2>Startup Scout</h2>\n"
    if scout is None:
        html += "<p>Could not read scout database.</p>\n"
    elif scout["total"] == 0:
        html += "<p>No companies evaluated this week.</p>\n"
    else:
        html += (
            f"<p>Evaluated: <b>{scout['total']}</b> — "
            f"<b>{len(scout['passed'])}</b> passed, <b>{len(scout['failed'])}</b> failed | "
            f"API cost: <b>${scout['cost']:.4f}</b></p>\n"
        )
        if scout["passed"]:
            html += "<h3 style='margin-top:12px'>Passed</h3>\n<ul style='margin:0;padding-left:20px'>\n"
            for name, _, cost in scout["passed"]:
                cost_str = f" (${cost:.4f})" if cost else ""
                html += f"<li>{escape(name)}{escape(cost_str)}</li>\n"
            html += "</ul>\n"
        if scout["failed"]:
            html += "<h3 style='margin-top:12px'>Failed</h3>\n<ul style='margin:0;padding-left:20px;color:#666'>\n"
            for name, _, _ in scout["failed"]:
                html += f"<li>{escape(name)}</li>\n"
            html += "</ul>\n"
    html += "<hr>\n"

    # --- Email Triage ---
    html += "<h2>Email Triage</h2>\n"
    if triage is None:
        html += "<p>Could not read triage logs.</p>\n"
    elif triage["runs"] == 0:
        html += "<p>No triage runs recorded this week.</p>\n"
    else:
        html += f"<p>Runs: <b>{triage['runs']}</b></p>\n"
        html += """<table style="border-collapse:collapse;font-size:14px">
<tr><td style="padding:3px 16px 3px 0">Time-sensitive</td><td><b>{time_sensitive}</b></td></tr>
<tr><td style="padding:3px 16px 3px 0">Kept</td><td><b>{keep}</b></td></tr>
<tr><td style="padding:3px 16px 3px 0">Trashed</td><td><b>{trash}</b></td></tr>
<tr><td style="padding:3px 16px 3px 0">Unsubscribed</td><td><b>{unsubscribe}</b></td></tr>
<tr><td style="padding:3px 16px 3px 0">Errors</td><td><b>{error}</b></td></tr>
</table>\n""".format(**triage)
        if triage["error"] > 0:
            html += f'<p style="color:#c62828">⚠️ {triage["error"]} error(s) during triage. Check logs/triage.log.</p>\n'
    if pending:
        html += f"<p>📬 Pending urgent emails awaiting action: <b>{len(pending)}</b></p>\n"
    html += "<hr>\n"

    html += '<p style="color:#999;font-size:12px">Sent by status_report.py from loganhartford.scout@gmail.com</p>\n'
    html += "</body></html>"

    send_report(
        to_address="logan.hartford@outlook.com",
        subject=f"Automation Status — {date_str}",
        markdown_body="Weekly automation status report. See the HTML version.",
        html_body=html,
    )
    print(f"Status report sent for {date_str}.")


if __name__ == "__main__":
    try:
        generate_status_report()
    except Exception as e:
        tb = traceback.format_exc()
        _notify_telegram(f"⚠️ Status report failed: {type(e).__name__}: {e}")
        print(f"Error: {tb}")
        raise
