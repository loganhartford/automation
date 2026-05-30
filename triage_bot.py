import asyncio
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import anthropic
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

import outlook

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_TRIAGE_BOT_TOKEN")
CHAT_ID = int(os.getenv("TELEGRAM_TRIAGE_CHAT_ID", "0"))
URGENT_STATE_FILE = "logs/triage_urgent_pending.json"
LOG_FILE = "logs/triage_bot.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

client = anthropic.Anthropic()
executor = ThreadPoolExecutor(max_workers=1)

conversations: dict[int, list] = {}

SYSTEM_PROMPT = """\
You are Logan's email assistant on Telegram. You help him manage time-sensitive emails that need his attention.

When he messages you:
- List pending time-sensitive emails if he asks or says something like "what's up" / "any emails"
- When he says what to reply (e.g. "tell them I'll be there", "say I'm not interested"), compose a full reply draft
- Show the draft clearly and wait for explicit approval ("send it", "yes", "looks good", "send") before calling send_reply
- Revise the draft if he asks for changes
- Trash or dismiss emails when asked

When composing reply drafts:
- Write as Logan, in first person
- Match the tone of the conversation (casual if casual, professional if professional)
- Keep replies concise — Logan doesn't write long emails
- After showing the draft, ask: "Send this, or any changes?"

Never call send_reply without explicit approval. Use plain text only — no markdown.\
"""

TOOLS = [
    {
        "name": "list_pending_emails",
        "description": "List all pending time-sensitive emails awaiting action",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "send_reply",
        "description": "Send a reply to an email. Only call after Logan explicitly approves the draft.",
        "input_schema": {
            "type": "object",
            "properties": {
                "email_id": {"type": "string", "description": "The ID of the email to reply to"},
                "body": {"type": "string", "description": "The full reply text to send"},
            },
            "required": ["email_id", "body"],
        },
    },
    {
        "name": "trash_email",
        "description": "Move an email to trash and remove it from the pending list",
        "input_schema": {
            "type": "object",
            "properties": {"email_id": {"type": "string"}},
            "required": ["email_id"],
        },
    },
    {
        "name": "dismiss_email",
        "description": "Remove an email from the pending list without taking action (mark as handled)",
        "input_schema": {
            "type": "object",
            "properties": {"email_id": {"type": "string"}},
            "required": ["email_id"],
        },
    },
]


def _load_pending():
    if os.path.exists(URGENT_STATE_FILE):
        with open(URGENT_STATE_FILE) as f:
            try:
                return json.load(f)
            except Exception:
                return []
    return []


def _save_pending(emails):
    with open(URGENT_STATE_FILE, "w") as f:
        json.dump(emails, f)


def _remove_pending(email_id):
    _save_pending([e for e in _load_pending() if e["id"] != email_id])


def _execute_tool(name, inp):
    if name == "list_pending_emails":
        emails = _load_pending()
        if not emails:
            return "No pending time-sensitive emails."
        parts = []
        for i, e in enumerate(emails, 1):
            parts.append(
                f"{i}. From: {e['sender']}\n"
                f"   Subject: {e['subject']}\n"
                f"   ID: {e['id']}\n"
                f"   Preview: {e['preview'][:150]}"
            )
        return "\n\n".join(parts)

    if name == "send_reply":
        try:
            session = outlook.get_session(interactive=False)
            draft_id = outlook.create_reply_draft(session, inp["email_id"], inp["body"])
            if outlook.send_draft(session, draft_id):
                _remove_pending(inp["email_id"])
                return "Reply sent successfully."
            return "Failed to send reply (send_draft returned false)."
        except RuntimeError as e:
            if "authentication" in str(e).lower():
                return "Outlook re-authentication needed. Run: python3 -c \"import outlook; outlook.get_session()\" on the server."
            return f"Error: {e}"
        except Exception as e:
            return f"Error sending reply: {e}"

    if name == "trash_email":
        try:
            session = outlook.get_session(interactive=False)
            outlook.move_to_trash(session, inp["email_id"])
            _remove_pending(inp["email_id"])
            return "Email trashed."
        except RuntimeError as e:
            if "authentication" in str(e).lower():
                return "Outlook re-authentication needed. Run: python3 -c \"import outlook; outlook.get_session()\" on the server."
            return f"Error: {e}"
        except Exception as e:
            return f"Error: {e}"

    if name == "dismiss_email":
        _remove_pending(inp["email_id"])
        return "Dismissed from pending list."

    return f"Unknown tool: {name}"


def _run_agent(history: list) -> str:
    messages = list(history)
    while True:
        resp = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        text = "\n".join(b.text for b in resp.content if hasattr(b, "text"))

        if resp.stop_reason == "end_turn":
            history.append({"role": "assistant", "content": text})
            return text

        if resp.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": resp.content})
            results = [
                {
                    "type": "tool_result",
                    "tool_use_id": b.id,
                    "content": _execute_tool(b.name, b.input),
                }
                for b in resp.content
                if b.type == "tool_use"
            ]
            messages.append({"role": "user", "content": results})
            continue

        break

    return "Unexpected issue. Please try again."


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id != CHAT_ID:
        log.warning(f"Ignored message from unauthorized chat_id {chat_id}")
        return

    text = update.message.text
    if chat_id not in conversations:
        conversations[chat_id] = []

    history = conversations[chat_id]
    history.append({"role": "user", "content": text})
    if len(history) > 30:
        conversations[chat_id] = history[-30:]

    try:
        loop = asyncio.get_event_loop()
        reply = await loop.run_in_executor(executor, _run_agent, conversations[chat_id])
        await update.message.reply_text(reply)
    except Exception as e:
        log.error(f"Agent error: {e}")
        await update.message.reply_text(f"Error: {e}")


def main():
    if not BOT_TOKEN:
        log.error("TELEGRAM_TRIAGE_BOT_TOKEN not set")
        sys.exit(1)
    if not CHAT_ID:
        log.error("TELEGRAM_TRIAGE_CHAT_ID not set")
        sys.exit(1)

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    log.info(f"Triage bot started (chat_id={CHAT_ID})")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
