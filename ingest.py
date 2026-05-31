import os
import requests
import traceback

import anthropic
from dotenv import load_dotenv
from google.auth.exceptions import RefreshError

from gmail import get_service, get_unread_emails, mark_as_read, send_report
from evaluate import process_newsletter, CreditExhaustedError
from repair import save_error_context, REPAIR_HINT

load_dotenv()


def _notify_telegram(msg):
    """Fire-and-forget Telegram alert via the scout bot."""
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


def ingest():
    print("Checking for new newsletters...")
    service = get_service()
    emails = get_unread_emails()

    if not emails:
        print("No new emails.")
        return

    for subject, sender_email, body, message_id in emails:
        if not body.strip():
            print(f"  Skipping {subject} — empty body.")
            mark_as_read(service, message_id)
            continue
        print(f"Processing: {subject} (from {sender_email})")
        process_newsletter(body, source=sender_email)
        mark_as_read(service, message_id)
        print(f"Done: {subject}")

if __name__ == "__main__":
    try:
        ingest()
    except RefreshError:
        # Gmail auth is broken — can't send email, so Telegram only.
        _notify_telegram(
            "⚠️ Scout ingest: Gmail auth token expired.\n"
            "Delete token.pickle and re-run the OAuth flow to restore."
        )
        print("Gmail auth token expired — Telegram alert sent.")
    except CreditExhaustedError as e:
        _notify_telegram(f"⚠️ Scout ingest: Anthropic API credits exhausted. Top up to resume.\n{e}")
        try:
            send_report(
                to_address="logan.hartford@outlook.com",
                subject="Startup Scout - Out of API Credits",
                markdown_body="Anthropic API credit balance is too low. Top up at console.anthropic.com/settings/billing to resume processing.",
            )
        except Exception:
            pass
        print("Out of Anthropic credits — alert sent.")
    except anthropic.BadRequestError as e:
        if "credit balance" in str(e):
            _notify_telegram("⚠️ Scout ingest: Anthropic API credits exhausted. Top up to resume.")
            try:
                send_report(
                    to_address="logan.hartford@outlook.com",
                    subject="Startup Scout - Out of API Credits",
                    markdown_body="Anthropic API credit balance is too low. Top up at console.anthropic.com/settings/billing to resume processing.",
                )
            except Exception:
                pass
            print("Out of Anthropic credits — alert sent.")
        else:
            tb = traceback.format_exc()
            _notify_telegram(f"⚠️ Scout ingest failed: {type(e).__name__}: {e}")
            try:
                send_report(
                    to_address="logan.hartford@outlook.com",
                    subject="Startup Scout - Ingest Failed",
                    markdown_body=f"ingest.py crashed with the following error:\n\n{tb}",
                )
            except Exception:
                pass
            raise
    except Exception as e:
        tb = traceback.format_exc()
        save_error_context("ingest.py", type(e).__name__, str(e), tb)
        _notify_telegram(f"⚠️ Scout ingest failed: {type(e).__name__}: {e}\n\n{REPAIR_HINT}")
        try:
            send_report(
                to_address="logan.hartford@outlook.com",
                subject="Startup Scout - Ingest Failed",
                markdown_body=f"ingest.py crashed with the following error:\n\n{tb}",
            )
        except Exception:
            pass
        raise