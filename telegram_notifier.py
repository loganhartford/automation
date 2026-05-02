import os
import requests
from dotenv import load_dotenv

load_dotenv()


def notify(message):
    """Send a Telegram message. Silently no-ops if env vars are not set."""
    token = os.getenv("TELEGRAM_SCHEDULER_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_SCHEDULER_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=5,
        )
    except Exception:
        pass
