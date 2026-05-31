import asyncio
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import requests
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_YNAB_BOT_TOKEN")
CHAT_ID = int(os.getenv("TELEGRAM_YNAB_CHAT_ID") or "0")
YNAB_TOKEN = os.getenv("YNAB_TOKEN")
YNAB_BASE = "https://api.ynab.com/v1"
YNAB_HEADERS = {"Authorization": f"Bearer {YNAB_TOKEN}"}
OLLAMA_BASE = "http://localhost:11434"
OLLAMA_MODEL = "qwen3:8b"
LOG_FILE = "logs/ynab_bot.log"

BUDGET_IDS = {
    "america": "4afa6eeb-9676-44f7-8776-61c9bbab0ceb",
    "canada": "6021be64-d47c-4e35-a7c6-60d708a2b1c8",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

executor = ThreadPoolExecutor(max_workers=1)
conversations: dict[int, list] = {}

SYSTEM_PROMPT = """\
/no_think
You are Logan's personal finance assistant. You have access to his YNAB budgets:
- "america" = America - 2026 (USD, current US budget)
- "canada" = Canada - 2026 (CAD, Canadian budget)

When he asks about spending, budgets, or transactions, use your tools to fetch the data and give him a clear, concise answer. Default to "america" unless he specifies otherwise.

Default date range: current month (or last 30 days if not specified).

When presenting spending data:
- If any uncategorized transaction is labeled [INVESTMENT/SAVINGS], subtract it from the total and say "Real spending ex-investments: $X"
- List every category — do not omit any, no matter how small
- Group by category group, sorted largest to smallest
- Flag anything that looks unusual or high
- Keep it conversational — he's asking on his phone

Plain text only. No asterisks, no markdown, no special symbols. Use blank lines to separate sections.\
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_spending_summary",
            "description": "Get spending totals grouped by category for a date range",
            "parameters": {
                "type": "object",
                "properties": {
                    "budget": {"type": "string", "enum": ["america", "canada"], "description": "Which budget to query"},
                    "since_date": {"type": "string", "description": "Start date YYYY-MM-DD (inclusive)"},
                    "until_date": {"type": "string", "description": "End date YYYY-MM-DD (inclusive), defaults to today"},
                },
                "required": ["budget", "since_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_transactions",
            "description": "List individual transactions, optionally filtered by category",
            "parameters": {
                "type": "object",
                "properties": {
                    "budget": {"type": "string", "enum": ["america", "canada"]},
                    "since_date": {"type": "string", "description": "Start date YYYY-MM-DD"},
                    "until_date": {"type": "string", "description": "End date YYYY-MM-DD, defaults to today"},
                    "category": {"type": "string", "description": "Optional category name to filter by (case-insensitive substring match)"},
                },
                "required": ["budget", "since_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_budget_vs_actual",
            "description": "Get budgeted amount vs actual spending for each category in a given month",
            "parameters": {
                "type": "object",
                "properties": {
                    "budget": {"type": "string", "enum": ["america", "canada"]},
                    "month": {"type": "string", "description": "Month in YYYY-MM-01 format (e.g. 2026-05-01)"},
                },
                "required": ["budget", "month"],
            },
        },
    },
]


def _ynab_get(path):
    r = requests.get(f"{YNAB_BASE}{path}", headers=YNAB_HEADERS)
    r.raise_for_status()
    return r.json()["data"]


def _dollars(milliunits):
    return abs(milliunits) / 1000.0


def _get_category_map(budget_id):
    groups = _ynab_get(f"/budgets/{budget_id}/categories")["category_groups"]
    cat_map = {}
    for g in groups:
        for c in g["categories"]:
            cat_map[c["id"]] = (g["name"], c["name"])
    return cat_map


def _execute_tool(name, inp):
    budget_id = BUDGET_IDS[inp["budget"]]
    today = date.today().isoformat()

    if name == "get_spending_summary":
        since = inp["since_date"]
        until = inp.get("until_date", today)
        txns = _ynab_get(f"/budgets/{budget_id}/transactions?since_date={since}")["transactions"]
        cat_map = _get_category_map(budget_id)

        # Filter: outflows only, no transfers, within date range, exclude YNAB internal categories
        spending = [
            t for t in txns
            if t["amount"] < 0
            and not t.get("transfer_account_id")
            and t["date"] <= until
            and t.get("category_name") != "Inflow: Ready to Assign"
        ]

        if not spending:
            return f"No spending found between {since} and {until}."

        # Group by category — use cat_map for group name, fall back to transaction's own category_name
        from collections import defaultdict
        by_cat = defaultdict(list)
        uncategorized = []
        for t in spending:
            cid = t.get("category_id")
            if cid and cid in cat_map:
                by_cat[cid].append(t)
            elif t.get("category_name") and t["category_name"] != "Uncategorized":
                # Category exists in YNAB but not in our map (e.g. hidden/savings categories)
                synthetic_key = f"__{t['category_name']}"
                cat_map[synthetic_key] = ("Other", t["category_name"])
                by_cat[synthetic_key].append(t)
            else:
                uncategorized.append(t)

        total = sum(t["amount"] for t in spending)
        lines = [f"Total spent ({since} to {until}): ${_dollars(total):,.2f}  ({len(spending)} transactions)\n"]

        # Build rows and sort: keep groups together, largest group first, largest category first within group
        rows = [
            (cat_map[cid][0], cat_map[cid][1], sum(t["amount"] for t in tl), len(tl))
            for cid, tl in by_cat.items()
        ]
        group_totals = defaultdict(int)
        for group, _, amt, _ in rows:
            group_totals[group] += amt
        rows.sort(key=lambda x: (group_totals[x[0]], x[2]))

        current_group = None
        for group, cat, amt, count in rows:
            if group != current_group:
                lines.append(f"\n{group}:")
                current_group = group
            lines.append(f"  {cat}: ${_dollars(amt):,.2f} ({count} txns)")

        if uncategorized:
            unc_total = sum(t["amount"] for t in uncategorized)
            lines.append(f"\nUncategorized: ${_dollars(unc_total):,.2f} ({len(uncategorized)} txns)")
            investment_keywords = ("mutual fund", "investment", "brokerage", "etf", "vanguard", "fidelity", "schwab", "wealthsimple", "robinhood", "401k", "ira", "rsp", "rrsp")
            for t in sorted(uncategorized, key=lambda t: t["amount"]):
                payee = t.get("payee_name") or "Unknown"
                note = " [INVESTMENT/SAVINGS — not real spending]" if any(k in payee.lower() for k in investment_keywords) else ""
                lines.append(f"  - {t['date']}  {payee}  ${_dollars(t['amount']):,.2f}{note}")

        return "\n".join(lines)

    if name == "list_transactions":
        since = inp["since_date"]
        until = inp.get("until_date", today)
        category_filter = inp.get("category", "").lower()
        txns = _ynab_get(f"/budgets/{budget_id}/transactions?since_date={since}")["transactions"]
        cat_map = _get_category_map(budget_id)

        spending = [
            t for t in txns
            if t["amount"] < 0
            and not t.get("transfer_account_id")
            and t["date"] <= until
        ]

        if category_filter:
            spending = [
                t for t in spending
                if t.get("category_id") and t["category_id"] in cat_map
                and category_filter in cat_map[t["category_id"]][1].lower()
            ]

        if not spending:
            return "No matching transactions found."

        spending.sort(key=lambda t: t["amount"])
        lines = []
        for t in spending:
            cat = cat_map.get(t.get("category_id", ""), ("", "Unknown"))[1]
            payee = (t.get("payee_name") or "Unknown")
            memo = f" — {t['memo']}" if t.get("memo") else ""
            lines.append(f"{t['date']}  {payee}  ${_dollars(t['amount']):,.2f}  [{cat}]{memo}")
        return "\n".join(lines)

    if name == "get_budget_vs_actual":
        month = inp["month"]
        data = _ynab_get(f"/budgets/{budget_id}/months/{month}")["month"]
        categories = data.get("categories", [])
        groups = _ynab_get(f"/budgets/{budget_id}/categories")["category_groups"]
        group_name = {c["id"]: g["name"] for g in groups for c in g["categories"]}

        lines = [f"Budget vs Actual — {month[:7]}\n"]
        current_group = None
        over_budget = []

        cats_sorted = sorted(categories, key=lambda c: group_name.get(c["id"], ""))
        for c in cats_sorted:
            if c["budgeted"] == 0 and c["activity"] == 0:
                continue
            gname = group_name.get(c["id"], "Other")
            if gname != current_group:
                lines.append(f"\n{gname}:")
                current_group = gname
            budgeted = _dollars(c["budgeted"])
            spent = _dollars(abs(c["activity"]))
            diff = budgeted - spent
            flag = " *** OVER" if diff < -0.01 else ""
            lines.append(f"  {c['name']}: ${spent:,.2f} / ${budgeted:,.2f}{flag}")
            if diff < -0.01:
                over_budget.append(f"{c['name']} (over by ${abs(diff):,.2f})")

        if over_budget:
            lines.append(f"\nOver budget: {', '.join(over_budget)}")

        return "\n".join(lines)

    return f"Unknown tool: {name}"


def _ollama_chat(messages):
    r = requests.post(
        f"{OLLAMA_BASE}/v1/chat/completions",
        json={"model": OLLAMA_MODEL, "messages": messages, "tools": TOOLS, "stream": False},
        timeout=120,
    )
    r.raise_for_status()
    return r.json()["choices"][0]


def _run_agent(history: list) -> str:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + list(history)
    while True:
        choice = _ollama_chat(messages)
        msg = choice["message"]
        finish = choice["finish_reason"]

        if finish == "tool_calls":
            messages.append(msg)
            for tc in msg.get("tool_calls", []):
                fn = tc["function"]
                try:
                    inp = json.loads(fn["arguments"]) if isinstance(fn["arguments"], str) else fn["arguments"]
                    result = _execute_tool(fn["name"], inp)
                except Exception as e:
                    result = f"Tool error: {e}"
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
            continue

        text = msg.get("content") or ""
        history.append({"role": "assistant", "content": text})
        return text


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if not CHAT_ID:
        log.info(f"TELEGRAM_YNAB_CHAT_ID not set. Message from chat_id={chat_id}")
        await update.message.reply_text(f"Your chat ID is: {chat_id}\nSet TELEGRAM_YNAB_CHAT_ID={chat_id} in .env and restart.")
        return

    if chat_id != CHAT_ID:
        log.warning(f"Ignored message from unauthorized chat_id {chat_id}")
        return

    text = update.message.text
    if chat_id not in conversations:
        conversations[chat_id] = []

    await update.message.reply_text("Working on it...")

    history = conversations[chat_id]
    history.append({"role": "user", "content": text})
    if len(history) > 20:
        conversations[chat_id] = history[-20:]

    try:
        loop = asyncio.get_event_loop()
        reply = await loop.run_in_executor(executor, _run_agent, conversations[chat_id])
        if reply:
            reply = reply.replace("**", "").replace("*", "")
            for i in range(0, len(reply), 4000):
                await update.message.reply_text(reply[i:i + 4000])
    except Exception as e:
        log.error(f"Agent error: {e}")
        await update.message.reply_text(f"Error: {e}")


def main():
    if not BOT_TOKEN:
        log.error("TELEGRAM_YNAB_BOT_TOKEN not set")
        sys.exit(1)
    if not YNAB_TOKEN:
        log.error("YNAB_TOKEN not set")
        sys.exit(1)

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    log.info(f"YNAB bot started (chat_id={CHAT_ID or 'NOT SET — send a message to get yours'})")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
