import json
import logging
import os
import re
import sys
from datetime import datetime

import requests
from dotenv import load_dotenv

import outlook

load_dotenv()

OLLAMA_BASE = "http://localhost:11434"
OLLAMA_MODEL = "qwen3:8b"
LOG_FILE = "logs/triage.log"
SEEN_IDS_FILE = "logs/triage_seen.json"
URGENT_STATE_FILE = "logs/triage_urgent_pending.json"
ARCHIVE_LOG_FILE = "logs/triage_archive_log.json"
ARCHIVE_DAYS = 7

# Hard rules — bypass Ollama entirely
ALWAYS_TIME_SENSITIVE_SENDERS = {
    "laurie.goren@therapysecure.com",
}

ALWAYS_KEEP_SENDERS = {
    # Family
    "hartfordam@gmail.com",
    "kbrayiannis@gmail.com",
    "leandrakelly04@gmail.com",
    "thegriz@rogers.com",
    "sahartford@gmail.com",
    "npjenn2011@gmail.com",
    # Always-read sources
    "loganhartford.scout@gmail.com",
    "newsletter@foundmyfitness.com",
    "pacificarunners@wildapricot.org",
    "chris@chriswillx.com",
}

DRY_RUN = "--dry-run" in sys.argv
EXECUTE_REVIEW = next((sys.argv[i + 1] for i, a in enumerate(sys.argv) if a == "--execute-review"), None)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are an email triage assistant. For each email choose one of four actions:

"time_sensitive" — Requires action or response soon. Use for:
                - Emails confirmed as replies to messages the user sent (indicated by
                  "Is a reply: yes" in the prompt — trust this signal even if the sender
                  looks like a business or the subject looks like spam)
                - Government/CRA/IRS notices with deadlines
                - Messages from real people explicitly asking for a response
                - Genuine fraud alerts (unauthorized charge detected, account locked)
                DO NOT use for: security notifications, login codes, informational newsletters,
                account update notices, or payment confirmations — those are KEEP or TRASH.
                DO NOT use "urgent" — the correct action string is "time_sensitive".

"keep"        — Worth reading but not time-sensitive. Use for:
                - Email from a real individual person
                - Security/login codes and confirmation codes (user is already looking for them)
                - Login/new-device notifications (informational, no action needed)
                - Flight and travel booking confirmations (not urgent unless same-day)
                - Travel insurance policy documents (contain plan details the user needs)
                - Non-Amazon shipping/tracking emails for parcels
                - Newsletters and content the user actively reads
                - Calendar invites and meeting updates

"unsubscribe" — Legitimate mailing list the user wants off. Unsubscribe AND delete.
                IMPORTANT: When deciding between trash and unsubscribe, if the email has an
                unsubscribe link and is from a real company (not phishing), prefer UNSUBSCRIBE
                over trash — it prevents future emails too.
                Use for: product marketing, software newsletters, tutorial series, healthcare
                clinic announcements, financial product promotions, utility company newsletters.
                DO NOT use for phishing.

"trash"       — Delete without unsubscribing. Use for:
                - All Instacart emails (orders, receipts, carts, delivery, notifications)
                - All Amazon emails (orders, shipping, delivery, confirmations)
                - All Venmo notifications (payments sent/received, requests, friend activity)
                - Receipts and invoices from any service
                - Bank/credit card statement notifications and account term changes
                - ALL routine security/account alerts from Microsoft, Google, Apple, Airbnb,
                  and similar services (new sign-in, app connected, recovery email, security
                  recommendations, account activity) — these are informational noise
                - Healthcare clinic booking system announcements and appointment reminders
                - Automated service notifications (address updated, subscription renewal)
                - App activity digests (daily highlights, summaries)
                - Cold outreach, SEO solicitations, job board spam
                - Phishing (fake urgency, credential harvesting, lookalike domains)\
"""


def _notify_telegram(msg):
    """Fire-and-forget Telegram notification. Never raises."""
    token = os.getenv("TELEGRAM_TRIAGE_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_TRIAGE_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg},
            timeout=10,
        )
    except Exception as e:
        log.warning(f"Telegram notification failed: {e}")


def _append_urgent_email(entry):
    """Add an urgent email to the pending state file."""
    emails = []
    if os.path.exists(URGENT_STATE_FILE):
        with open(URGENT_STATE_FILE) as f:
            try:
                emails = json.load(f)
            except Exception:
                emails = []
    emails.append(entry)
    with open(URGENT_STATE_FILE, "w") as f:
        json.dump(emails, f)


def check_ollama_health():
    try:
        resp = requests.get(f"{OLLAMA_BASE}/api/tags", timeout=5)
        resp.raise_for_status()
        models = [m["name"] for m in resp.json().get("models", [])]
        return any(OLLAMA_MODEL in m for m in models)
    except Exception:
        return False


def _load_archive_log():
    if os.path.exists(ARCHIVE_LOG_FILE):
        with open(ARCHIVE_LOG_FILE) as f:
            return json.load(f)
    return []


def _save_archive_log(entries):
    with open(ARCHIVE_LOG_FILE, "w") as f:
        json.dump(entries, f)


def _append_archive_log(email_id, sender, subject):
    entries = _load_archive_log()
    entries.append({"id": email_id, "sender": sender, "subject": subject, "archived_at": datetime.now().isoformat()})
    _save_archive_log(entries)


def _purge_stale_archive(session):
    """Move archive-staged emails older than ARCHIVE_DAYS to Deleted Items."""
    entries = _load_archive_log()
    if not entries:
        return
    cutoff = datetime.now().timestamp() - ARCHIVE_DAYS * 86400
    remaining = []
    for e in entries:
        if datetime.fromisoformat(e["archived_at"]).timestamp() < cutoff:
            success = outlook.move_to_trash(session, e["id"])
            log.info(f"[PURGE ] {e['sender']} | {e['subject']!r} | {'ok' if success else 'not found'}")
        else:
            remaining.append(e)
    _save_archive_log(remaining)


def _load_seen_ids():
    if os.path.exists(SEEN_IDS_FILE):
        with open(SEEN_IDS_FILE) as f:
            return set(json.load(f))
    return set()


def _save_seen_ids(seen):
    with open(SEEN_IDS_FILE, "w") as f:
        json.dump(list(seen), f)


def pre_filter(sender, subject):
    """Hard rules that bypass Ollama. Returns decision dict or None."""
    s = sender.lower()

    if s in ALWAYS_TIME_SENSITIVE_SENDERS:
        return {"action": "time_sensitive", "reason": "hard rule: always time-sensitive sender"}

    if s in ALWAYS_KEEP_SENDERS:
        return {"action": "keep", "reason": "hard rule: always keep sender"}

    # Catch relatives by Hartford surname in address
    if "hartford" in s:
        return {"action": "keep", "reason": "hard rule: sender appears to be a relative"}

    return None


def _ask_ollama_once(sender, subject, body_preview, has_unsubscribe, is_reply=False):
    """Single Ollama call. Returns {"action": ..., "reason": ...}."""
    user_msg = (
        f"From: {sender}\n"
        f"Subject: {subject}\n"
        f"Is a reply: {'yes' if is_reply else 'no'}\n"
        f"Has unsubscribe link: {'yes' if has_unsubscribe else 'no'}\n"
        f"Body preview:\n{body_preview}\n\n"
        'Respond ONLY with JSON: {"action": "time_sensitive|keep|trash|unsubscribe", "reason": "one sentence"}'
    )
    payload = {
        "model": OLLAMA_MODEL,
        "think": False,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.4, "num_predict": 256},
    }
    resp = requests.post(f"{OLLAMA_BASE}/api/chat", json=payload, timeout=60)
    resp.raise_for_status()
    result = json.loads(resp.json()["message"]["content"])
    if result.get("action") not in ("time_sensitive", "keep", "trash", "unsubscribe"):
        result["action"] = "keep"
    if result.get("action") == "unsubscribe" and not has_unsubscribe:
        result["action"] = "trash"
    return result


def ask_ollama(sender, subject, body_preview, has_unsubscribe, is_reply=False):
    """
    Call Ollama 3 times and require consensus of 2 to act.
    Falls back to keep if all 3 votes differ.
    Returns {"action": ..., "reason": ..., "votes": [...]}.
    """
    votes = []
    for _ in range(3):
        try:
            votes.append(_ask_ollama_once(sender, subject, body_preview, has_unsubscribe, is_reply))
        except Exception as e:
            log.warning(f"Ollama error for '{subject}': {e}")
            votes.append({"action": "keep", "reason": f"ollama error: {e}"})

    actions = [v.get("action", "keep") for v in votes]
    from collections import Counter
    counts = Counter(actions)
    top_action, top_count = counts.most_common(1)[0]

    if top_count >= 2:
        reason = next(v["reason"] for v in votes if v.get("action") == top_action)
        return {"action": top_action, "reason": reason, "votes": actions}
    else:
        return {"action": "keep", "reason": f"no consensus", "votes": actions}


def _execute_action(session, email_obj, action):
    """Execute a single action on an email. Returns True on success."""
    msg_id = email_obj["id"]
    if action == "unsubscribe":
        success, method = outlook.do_unsubscribe(session, email_obj)
        log.info(f"  Unsubscribed via {method}" if success else f"  Unsubscribe failed ({method}), archiving anyway")
        ok = outlook.move_to_archive(session, msg_id)
        if ok:
            _append_archive_log(msg_id, email_obj["sender"], email_obj["subject"])
        return ok
    elif action == "trash":
        ok = outlook.move_to_archive(session, msg_id)
        if ok:
            _append_archive_log(msg_id, email_obj["sender"], email_obj["subject"])
        return ok
    return True  # keep / time_sensitive — no action needed


def triage_outlook(review_entries, seen_ids):
    """Run triage on new emails. Updates seen_ids in place."""
    log.info("Starting Outlook triage...")
    session = outlook.get_session(interactive=False)
    if not DRY_RUN:
        _purge_stale_archive(session)
    emails = outlook.get_emails(session, max_emails=200, min_age_hours=24)
    new_emails = [e for e in emails if e["id"] not in seen_ids]
    log.info(f"Fetched {len(emails)} emails, {len(new_emails)} not yet processed")

    counts = {"time_sensitive": 0, "keep": 0, "trash": 0, "unsubscribe": 0, "error": 0}
    for email in new_emails:
        subject = email["subject"]
        sender = email["sender"]
        body = email["body_preview"]
        has_unsub = email["unsubscribe"] is not None
        is_reply = email.get("is_reply", False)

        try:
            decision = pre_filter(sender, subject) or ask_ollama(sender, subject, body, has_unsub, is_reply)
            action = decision.get("action", "keep")
            reason = decision.get("reason", "")

            labels = {"trash": "TRASH", "unsubscribe": "UNSUB ", "keep": "KEEP ", "time_sensitive": "TIME S"}
            label = labels.get(action, "KEEP ")
            votes = decision.get("votes")
            vote_str = f" [{','.join(votes)}]" if votes else ""
            log.info(f"[{label}]{vote_str} {sender} | {subject!r} | {reason}")

            review_entries.append({
                "label": label,
                "action": action,
                "id": email["id"],
                "sender": sender,
                "subject": subject,
                "reason": reason,
                "preview": body[:120],
                "has_unsub": has_unsub,
                "votes": votes,
            })

            if action == "time_sensitive":
                _notify_telegram(
                    f"📬 Time-sensitive: {sender}\n{subject}\n\n{body[:200]}"
                )
                _append_urgent_email({
                    "id": email["id"],
                    "sender": sender,
                    "subject": subject,
                    "preview": body[:400],
                })

            if not DRY_RUN:
                success = _execute_action(session, email, action)
                if not success:
                    log.warning(f"  Action failed for '{subject}'")
                    counts["error"] += 1
                    continue

            counts[action] = counts.get(action, 0) + 1
            seen_ids.add(email["id"])

        except Exception as e:
            log.error(f"Error processing '{subject}': {e}")
            counts["error"] += 1

    return counts


def parse_review_file(path):
    """Parse a review file into a list of entry dicts."""
    entries = []
    current = None
    with open(path) as f:
        for line in f:
            line = line.rstrip("\n")
            if re.match(r"^\[(TRASH|UNSUB |KEEP |TIME S)\]", line):
                if current:
                    entries.append(current)
                current = {"header": line, "id": None, "feedback": "", "action": "keep"}
                label_m = re.match(r"^\[([\w ]+)\]", line)
                if label_m:
                    label = label_m.group(1).strip()
                    current["action"] = {"TRASH": "trash", "UNSUB": "unsubscribe", "KEEP": "keep", "TIME S": "time_sensitive"}.get(label, "keep")
                sender_m = re.match(r"^\[[\w ]+\] (.+?)(?:\s\[has unsubscribe link\])?\s*\|\s*\"(.*)\"", line)
                if sender_m:
                    current["sender"] = sender_m.group(1).strip()
                    current["subject"] = sender_m.group(2).strip()
            elif current is not None:
                stripped = line.strip()
                if stripped.startswith("ID:"):
                    current["id"] = stripped[3:].strip()
                elif stripped.startswith("Feedback:"):
                    current["feedback"] = stripped[9:].strip()
    if current:
        entries.append(current)
    return entries


def execute_review(review_path):
    """Execute actions from a review file. Skips entries that have feedback written."""
    entries = parse_review_file(review_path)
    to_execute = [e for e in entries if not e["feedback"]]
    to_skip = [e for e in entries if e["feedback"]]
    log.info(f"Review file: {len(to_execute)} to execute, {len(to_skip)} skipped (have feedback)")

    session = outlook.get_session(interactive=False)

    # Build sender+subject lookup for entries without stored IDs
    all_emails = outlook.get_emails(session, max_emails=250)
    id_lookup = {e["id"]: e for e in all_emails}
    subject_lookup = {}
    for e in all_emails:
        key = (e["sender"].lower(), e["subject"].lower())
        if key not in subject_lookup:
            subject_lookup[key] = e

    executed, not_found, errors = 0, 0, 0
    for entry in to_execute:
        action = entry["action"]
        sender = entry.get("sender", "")
        subject = entry.get("subject", "")

        if action in ("keep", "time_sensitive"):
            _display = {"keep": "KEEP  ", "time_sensitive": "TIME S"}.get(action, action.upper())
            log.info(f"[{_display}] (no action needed) | {sender} | {subject!r}")
            executed += 1
            continue

        # Find the email object
        email_obj = id_lookup.get(entry["id"]) if entry["id"] else None
        if not email_obj:
            key = (sender.lower(), subject.lower())
            email_obj = subject_lookup.get(key)

        if not email_obj:
            log.warning(f"NOT FOUND | {sender} | {subject!r}")
            not_found += 1
            continue

        try:
            success = _execute_action(session, email_obj, action)
            if success:
                log.info(f"[{action.upper():<6}] DONE | {sender} | {subject!r}")
                executed += 1
            else:
                log.warning(f"[{action.upper():<6}] FAILED | {sender} | {subject!r}")
                errors += 1
        except Exception as e:
            log.error(f"Error on '{subject}': {e}")
            errors += 1

    log.info(f"Execute complete | Done: {executed} | Not found: {not_found} | Errors: {errors} | Skipped: {len(to_skip)}")
    return executed, not_found, errors


def write_review_file(review_entries, timestamp):
    """Write review file. Blank Feedback = approved. ID line enables reliable execute."""
    path = f"logs/triage_review_{timestamp}.txt"
    lines = [
        f"=== TRIAGE REVIEW: {timestamp} | {len(review_entries)} emails ===",
        "Instructions: Leave Feedback blank to approve. Write a correction to flag it.",
        "",
    ]
    for e in review_entries:
        unsub_note = " [has unsubscribe link]" if e["has_unsub"] else ""
        votes_str = ", ".join(e["votes"]) if e.get("votes") else "hard rule"
        lines.append(f"[{e['label']}] {e['sender']}{unsub_note} | \"{e['subject']}\"")
        lines.append(f"  ID:       {e['id']}")
        lines.append(f"  Votes:    {votes_str}")
        lines.append(f"  Reason:   {e['reason']}")
        lines.append(f"  Preview:  {e['preview']}")
        lines.append(f"  Feedback: ")
        lines.append("")
    with open(path, "w") as f:
        f.write("\n".join(lines))
    return path


def main():
    if EXECUTE_REVIEW:
        if not os.path.exists(EXECUTE_REVIEW):
            print(f"Review file not found: {EXECUTE_REVIEW}")
            sys.exit(1)
        log.info(f"=== Executing review file: {EXECUTE_REVIEW} ===")
        try:
            execute_review(EXECUTE_REVIEW)
        except Exception as e:
            log.error(f"Execute review failed: {e}")
            _notify_telegram(f"⚠️ Triage execute-review failed: {e}")
            sys.exit(1)
        return

    if not check_ollama_health():
        log.error(f"Ollama not running or model '{OLLAMA_MODEL}' not loaded. Exiting.")
        _notify_telegram(f"⚠️ Triage: Ollama not running or {OLLAMA_MODEL} not loaded.")
        sys.exit(1)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    mode = "DRY RUN" if DRY_RUN else "LIVE"
    log.info(f"=== Email triage started ({mode}) ===")

    seen_ids = _load_seen_ids()
    review_entries = []

    try:
        counts = triage_outlook(review_entries, seen_ids)
    except Exception as e:
        log.error(f"Triage failed: {e}")
        _notify_telegram(f"⚠️ Triage failed: {e}")
        sys.exit(1)

    if not DRY_RUN:
        _save_seen_ids(seen_ids)

    # Gmail triage (add later):
    # counts_gmail = triage_gmail(review_entries, seen_ids)

    log.info(
        f"=== Done | Time-sensitive: {counts.get('time_sensitive',0)} | Kept: {counts.get('keep',0)} | "
        f"Trashed: {counts.get('trash',0)} | Unsubbed: {counts.get('unsubscribe',0)} | "
        f"Errors: {counts.get('error',0)} ==="
    )
    if counts.get("error", 0) > 0:
        _notify_telegram(f"⚠️ Triage: {counts['error']} error(s) during run. Check logs/triage.log.")

    if DRY_RUN and review_entries:
        path = write_review_file(review_entries, timestamp)
        print(f"\nReview written to: {path}")
        print("Paste into chat to tune prompt, or run:")
        print(f"  python3 triage.py --execute-review {path}")


if __name__ == "__main__":
    main()
