"""Weekly automation status report — delivered via the scout Telegram bot."""
import json
import os
import re
import sqlite3
import subprocess
import traceback
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

DB_PATH = "scout.db"
TRIAGE_LOG = "logs/triage.log"
URGENT_STATE_FILE = "logs/triage_urgent_pending.json"
YNAB_LOG = "logs/ynab_categorizer_log.json"

_SERVICES = {
    "scout-bot.service": "Scout Bot",
    "triage-bot.service": "Triage Bot",
    "scout-schedule.service": "Scheduler Bot",
    "ynab-bot.service": "YNAB Bot",
}

_TIMERS = {
    "scout-ingest.timer": "Scout Ingest",
    "scout-report.timer": "Scout Report",
    "scout-status-report.timer": "Status Report",
    "scout-calendar-notify.timer": "Calendar Notify",
    "scout-morning-meetings.timer": "Morning Meetings",
    "triage.timer": "Email Triage",
    "ynab-report.timer": "YNAB Report",
    "ynab-categorize.timer": "YNAB Categorize",
    "ynab-weekly-summary.timer": "YNAB Weekly Summary",
}


def _send_telegram(msg):
    """Send a (potentially long) message to the scout bot chat in 4000-char chunks."""
    token = os.getenv("TELEGRAM_SCOUT_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_SCOUT_CHAT_ID")
    if not token or not chat_id:
        print("Telegram credentials not set.")
        return
    for i in range(0, len(msg), 4000):
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": msg[i:i + 4000]},
                timeout=10,
            )
        except Exception as e:
            print(f"Telegram send failed: {e}")


def _unit_status(unit):
    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-active", unit],
            capture_output=True, text=True, timeout=5,
        )
        return r.stdout.strip()
    except Exception:
        return "unknown"


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
    return {"total": len(rows), "passed": passed, "failed": failed, "cost": cost}


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


def _ynab_stats(since):
    if not os.path.exists(YNAB_LOG):
        return None
    try:
        with open(YNAB_LOG) as f:
            entries = json.load(f)
    except Exception:
        return None
    since_str = since.strftime("%Y-%m-%d")
    recent = [e for e in entries if e.get("date", "") >= since_str]
    applied = [e for e in recent if e.get("confidence") == "high"]
    skipped = [e for e in recent if e.get("confidence") != "high"]
    budgets = {}
    for e in applied:
        b = e.get("budget", "unknown")
        budgets[b] = budgets.get(b, 0) + 1
    return {"total": len(recent), "applied": len(applied), "skipped": len(skipped), "budgets": budgets}


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
    ynab = _ynab_stats(since)
    pending = _pending_urgent()

    svc_statuses = {svc: _unit_status(svc) for svc in _SERVICES}
    timer_statuses = {t: _unit_status(t) for t in _TIMERS}

    lines = [f"Automation Status — {date_str}", f"Last 7 days\n"]

    # Services
    lines.append("── Services ──")
    for svc, label in _SERVICES.items():
        s = svc_statuses[svc]
        icon = "✓" if s == "active" else "✗"
        lines.append(f"{icon} {label}: {s}")
    lines.append("")
    for timer, label in _TIMERS.items():
        s = timer_statuses[timer]
        icon = "✓" if s == "active" else "✗"
        lines.append(f"{icon} {label}: {s}")

    # Scout
    lines.append("\n── Startup Scout ──")
    if scout is None:
        lines.append("Could not read scout database.")
    elif scout["total"] == 0:
        lines.append("No companies evaluated this week.")
    else:
        lines.append(
            f"Evaluated: {scout['total']} — {len(scout['passed'])} passed, "
            f"{len(scout['failed'])} failed | Cost: ${scout['cost']:.4f}"
        )
        if scout["passed"]:
            names = ", ".join(
                f"{r[0]} (${r[2]:.4f})" if r[2] else r[0]
                for r in scout["passed"]
            )
            lines.append(f"Passed: {names}")
        if scout["failed"]:
            lines.append(f"Failed: {', '.join(r[0] for r in scout['failed'])}")

    # Triage
    lines.append("\n── Email Triage ──")
    if triage is None:
        lines.append("Could not read triage logs.")
    elif triage["runs"] == 0:
        lines.append("No triage runs this week.")
    else:
        lines.append(f"Runs: {triage['runs']}")
        lines.append(
            f"Time-sensitive: {triage['time_sensitive']} | Kept: {triage['keep']} | "
            f"Trashed: {triage['trash']} | Unsubscribed: {triage['unsubscribe']} | "
            f"Errors: {triage['error']}"
        )
        if triage["error"] > 0:
            lines.append(f"⚠️ {triage['error']} error(s) — check logs/triage.log")
    if pending:
        lines.append(f"📬 Pending urgent: {len(pending)}")

    # YNAB
    lines.append("\n── YNAB ──")
    if ynab is None:
        lines.append("Could not read YNAB categorizer log.")
    elif ynab["total"] == 0:
        lines.append("No categorization activity this week.")
    else:
        budget_str = ", ".join(f"{b}: {n}" for b, n in sorted(ynab["budgets"].items()))
        lines.append(
            f"Categorized: {ynab['applied']} applied"
            f"{f' ({budget_str})' if budget_str else ''}"
            f", {ynab['skipped']} skipped"
        )

    _send_telegram("\n".join(lines))
    print(f"Status report sent for {date_str}.")


if __name__ == "__main__":
    try:
        generate_status_report()
    except Exception as e:
        tb = traceback.format_exc()
        _send_telegram(f"⚠️ Status report failed: {type(e).__name__}: {e}")
        print(f"Error: {tb}")
        raise
