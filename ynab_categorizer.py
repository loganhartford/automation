import json
import logging
import os
import sys
from collections import Counter
from datetime import date, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

YNAB_TOKEN = os.getenv("YNAB_TOKEN")
YNAB_BASE = "https://api.ynab.com/v1"
YNAB_HEADERS = {"Authorization": f"Bearer {YNAB_TOKEN}"}
OLLAMA_BASE = "http://localhost:11434"
OLLAMA_MODEL = "qwen3:8b"
BUDGET_IDS = {
    "america": "4afa6eeb-9676-44f7-8776-61c9bbab0ceb",
    "canada": "6021be64-d47c-4e35-a7c6-60d708a2b1c8",
}
BUDGET_NAMES = {"america": "America", "canada": "Canada"}
SKIP_GROUPS = {"Internal Master Category", "Credit Card Payments"}

STATE_FILE = "logs/ynab_categorizer_state.json"
ACTIVITY_LOG = "logs/ynab_categorizer_log.json"

SYSTEM_PROMPT = """\
You are a personal finance assistant. Categorize transactions based on payee name, amount, and memo.
Respond ONLY with valid JSON: {"category": "exact category name from the list", "reason": "one sentence"}
Pick the single best matching category. If nothing fits well, pick "Misc Spend".\
"""

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State and activity log
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"processed": {"america": [], "canada": []}}


def _save_state(state: dict) -> None:
    cutoff = (date.today() - timedelta(days=30)).isoformat()
    for budget in state["processed"]:
        state["processed"][budget] = [
            e for e in state["processed"][budget]
            if e.get("date", "9999") >= cutoff
        ]
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def _append_activity(entries: list[dict]) -> None:
    existing = []
    if os.path.exists(ACTIVITY_LOG):
        with open(ACTIVITY_LOG) as f:
            try:
                existing = json.load(f)
            except Exception:
                existing = []
    existing.extend(entries)
    # Prune entries older than 60 days
    cutoff = (date.today() - timedelta(days=60)).isoformat()
    existing = [e for e in existing if e.get("date", "9999") >= cutoff]
    with open(ACTIVITY_LOG, "w") as f:
        json.dump(existing, f, indent=2)


# ---------------------------------------------------------------------------
# YNAB helpers
# ---------------------------------------------------------------------------

def _ynab_get(path):
    try:
        r = requests.get(f"{YNAB_BASE}{path}", headers=YNAB_HEADERS, timeout=15)
    except requests.exceptions.ConnectionError:
        raise RuntimeError("Cannot reach YNAB API — check internet connection.")
    except requests.exceptions.Timeout:
        raise RuntimeError("YNAB API timed out.")
    if r.status_code == 401:
        raise RuntimeError("YNAB authentication failed — YNAB_TOKEN may be expired.")
    if r.status_code == 429:
        raise RuntimeError("YNAB rate limit hit — try again in a moment.")
    r.raise_for_status()
    return r.json()["data"]


def _ynab_patch(path, payload):
    try:
        r = requests.patch(f"{YNAB_BASE}{path}", headers=YNAB_HEADERS, json=payload, timeout=15)
    except requests.exceptions.ConnectionError:
        raise RuntimeError("Cannot reach YNAB API.")
    except requests.exceptions.Timeout:
        raise RuntimeError("YNAB API timed out.")
    if r.status_code == 401:
        raise RuntimeError("YNAB authentication failed.")
    r.raise_for_status()
    return r.json()["data"]


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def _fetch_transactions(budget_id: str, since_date: str) -> list[dict]:
    return _ynab_get(f"/budgets/{budget_id}/transactions?since_date={since_date}")["transactions"]


def _get_uncategorized(all_txns: list[dict], skip_ids: set[str]) -> list[dict]:
    result = []
    for t in all_txns:
        if t["id"] in skip_ids:
            continue
        if t.get("amount", 0) >= 0:
            continue
        if t.get("transfer_account_id"):
            continue
        payee = t.get("payee_name") or ""
        if payee.startswith("Transfer :"):
            continue
        if payee == "Manual Balance Adjustment":
            continue
        # Credit card payments (e.g. "Payment to Chase card ending in 9804")
        payee_lower = payee.lower()
        if "payment to" in payee_lower and ("card" in payee_lower or "chase" in payee_lower or "amex" in payee_lower or "apple" in payee_lower):
            continue
        cat = t.get("category_name")
        if cat not in (None, "Uncategorized"):
            continue
        result.append(t)
    return result


def _build_payee_history(all_txns: list[dict]) -> dict[str, str]:
    by_payee: dict[str, list[tuple[str, str]]] = {}
    for t in all_txns:
        if t.get("transfer_account_id"):
            continue
        cat = t.get("category_name")
        if not cat or cat in ("Uncategorized", "Inflow: Ready to Assign"):
            continue
        payee = t.get("payee_name") or ""
        if not payee:
            continue
        by_payee.setdefault(payee, []).append((t["date"], cat))
    return {
        payee: sorted(entries, reverse=True)[0][1]
        for payee, entries in by_payee.items()
    }


def _get_categories(budget_id: str) -> list[dict]:
    groups = _ynab_get(f"/budgets/{budget_id}/categories")["category_groups"]
    cats = []
    for g in groups:
        if g["name"] in SKIP_GROUPS:
            continue
        for c in g["categories"]:
            if not c.get("deleted") and not c.get("hidden"):
                cats.append({"id": c["id"], "group": g["name"], "name": c["name"]})
    return cats


# ---------------------------------------------------------------------------
# Ollama classification
# ---------------------------------------------------------------------------

def _classify_once(payee: str, amount_dollars: float, memo: str | None,
                   categories: list[dict], payee_history: dict[str, str]) -> dict:
    cat_list = "\n".join(f"  {c['group']} / {c['name']}" for c in categories)
    sorted_history = sorted(
        payee_history.items(),
        key=lambda kv: abs(ord(kv[0][0].lower()) - ord(payee[0].lower())) if kv[0] and payee else 99
    )
    examples = "\n".join(f"  {p} → {c}" for p, c in sorted_history[:30])
    user_msg = (
        f"Categorize this transaction:\n"
        f"Payee: {payee}\n"
        f"Amount: ${amount_dollars:.2f}\n"
        f"Memo: {memo or 'none'}\n\n"
        f"Available categories:\n{cat_list}\n\n"
        f"Examples from past transactions:\n{examples}\n\n"
        "Respond with JSON only."
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
        "options": {"temperature": 0.3, "num_predict": 128},
    }
    try:
        r = requests.post(f"{OLLAMA_BASE}/api/chat", json=payload, timeout=60)
    except requests.exceptions.ConnectionError:
        raise RuntimeError("Ollama is not running.")
    except requests.exceptions.Timeout:
        raise RuntimeError("Ollama timed out.")
    r.raise_for_status()
    content = r.json()["message"]["content"]
    result = json.loads(content)
    if "category" not in result:
        raise ValueError(f"Missing 'category' in response: {content}")
    return result


def _classify_transaction(payee: str, amount_dollars: float, memo: str | None,
                           categories: list[dict], payee_history: dict[str, str]) -> dict:
    votes, reasons = [], []
    for _ in range(3):
        try:
            r = _classify_once(payee, amount_dollars, memo, categories, payee_history)
            votes.append(r["category"])
            reasons.append(r.get("reason", ""))
        except Exception as e:
            log.warning(f"Classification error for '{payee}': {e}")
            votes.append(None)
            reasons.append("")

    valid_votes = [v for v in votes if v is not None]
    if not valid_votes:
        return {"category": None, "confidence": "none", "reason": "all Ollama calls failed"}

    counts = Counter(valid_votes)
    top_cat, top_count = counts.most_common(1)[0]
    reason = next((r for v, r in zip(votes, reasons) if v == top_cat), "")

    if top_count == 3:
        return {"category": top_cat, "confidence": "high", "reason": reason}
    if top_count == 2:
        return {"category": top_cat, "confidence": "medium", "reason": reason}
    return {"category": None, "confidence": "none", "reason": "no consensus"}


# ---------------------------------------------------------------------------
# YNAB write-back
# ---------------------------------------------------------------------------

def _apply_categories(budget_id: str, updates: list[dict]) -> None:
    if not updates:
        return
    _ynab_patch(
        f"/budgets/{budget_id}/transactions",
        {"transactions": [{"id": u["id"], "category_id": u["category_id"]} for u in updates]},
    )


# ---------------------------------------------------------------------------
# Main categorization entry point
# ---------------------------------------------------------------------------

def categorize_transactions(budget_key: str, since_date: str | None = None) -> str:
    if since_date is None:
        since_date = (date.today() - timedelta(days=7)).isoformat()

    budget_id = BUDGET_IDS[budget_key]
    name = BUDGET_NAMES[budget_key]

    state = _load_state()
    processed_ids = {e["id"] for e in state["processed"].get(budget_key, [])}

    log.info(f"Fetching transactions for {name} since {since_date}")
    recent_txns = _fetch_transactions(budget_id, since_date)
    uncategorized = _get_uncategorized(recent_txns, processed_ids)

    if not uncategorized:
        log.info(f"{name}: nothing new to categorize")
        return f"{name}: no new uncategorized transactions since {since_date}."

    log.info(f"{name}: {len(uncategorized)} new uncategorized transaction(s)")

    history_since = (date.today() - timedelta(days=180)).isoformat()
    history_txns = _fetch_transactions(budget_id, history_since)
    payee_history = _build_payee_history(history_txns)
    categories = _get_categories(budget_id)
    cat_by_name = {c["name"]: c["id"] for c in categories}

    auto_apply, review, skipped = [], [], []
    activity_entries = []
    today_str = date.today().isoformat()

    for t in uncategorized:
        payee = t.get("payee_name") or "Unknown"
        amount = abs(t["amount"]) / 1000.0
        memo = t.get("memo")

        result = _classify_transaction(payee, amount, memo, categories, payee_history)
        cat_name = result["category"]
        confidence = result["confidence"]

        activity_entries.append({
            "date": today_str,
            "budget": budget_key,
            "txn_date": t["date"],
            "payee": payee,
            "amount": amount,
            "category": cat_name,
            "confidence": confidence,
        })

        if confidence == "high" and cat_name and cat_name in cat_by_name:
            auto_apply.append({
                "id": t["id"],
                "category_id": cat_by_name[cat_name],
                "payee": payee,
                "amount": amount,
                "category_name": cat_name,
            })
        elif confidence == "medium" and cat_name:
            review.append({"payee": payee, "amount": amount, "category_name": cat_name})
        else:
            skipped.append({"payee": payee, "amount": amount})

    if auto_apply:
        _apply_categories(budget_id, auto_apply)
        log.info(f"{name}: applied {len(auto_apply)} categories")

    # Mark all processed transactions so they're skipped on future runs
    new_entries = [{"id": t["id"], "date": t["date"]} for t in uncategorized]
    state["processed"].setdefault(budget_key, []).extend(new_entries)
    _save_state(state)
    _append_activity(activity_entries)

    # Build summary string (used by bot and weekly report)
    lines = [f"{name}:"]
    if auto_apply:
        lines.append(f"  Auto-categorized: {len(auto_apply)}")
        for u in auto_apply:
            lines.append(f"    {u['payee']} → {u['category_name']} (${u['amount']:,.2f})")
    if review:
        lines.append(f"  Medium confidence (2/3): {len(review)}")
        for u in review:
            lines.append(f"    {u['payee']} → {u['category_name']} (${u['amount']:,.2f})")
    if skipped:
        lines.append(f"  No consensus: {len(skipped)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Weekly summary
# ---------------------------------------------------------------------------

def generate_weekly_summary() -> str:
    cutoff = (date.today() - timedelta(days=7)).isoformat()

    if not os.path.exists(ACTIVITY_LOG):
        return "YNAB Categorizer — Weekly Summary\n\nNo activity recorded this week."

    with open(ACTIVITY_LOG) as f:
        entries = json.load(f)

    week_entries = [e for e in entries if e.get("date", "") >= cutoff]

    if not week_entries:
        return "YNAB Categorizer — Weekly Summary\n\nNo transactions processed this week."

    lines = [f"YNAB Categorizer — Weekly Summary ({cutoff} to {date.today().isoformat()})"]

    for budget_key, name in BUDGET_NAMES.items():
        budget_entries = [e for e in week_entries if e.get("budget") == budget_key]
        if not budget_entries:
            continue

        high = [e for e in budget_entries if e["confidence"] == "high"]
        medium = [e for e in budget_entries if e["confidence"] == "medium"]
        none_ = [e for e in budget_entries if e["confidence"] == "none"]

        lines.append(f"\n{name}:")
        lines.append(f"  Processed: {len(budget_entries)} transactions")
        lines.append(f"  Auto-categorized (3/3): {len(high)}")
        lines.append(f"  Medium confidence (2/3): {len(medium)}")
        lines.append(f"  No consensus: {len(none_)}")

        if high:
            lines.append("  Applied:")
            for e in high:
                lines.append(f"    {e['txn_date']}  {e['payee']} → {e['category']} (${e['amount']:,.2f})")

        if none_:
            lines.append("  Still uncategorized:")
            for e in none_:
                lines.append(f"    {e['txn_date']}  {e['payee']} (${e['amount']:,.2f})")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI / systemd entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if "--weekly" in sys.argv:
        summary = generate_weekly_summary()
        token = os.getenv("TELEGRAM_YNAB_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_YNAB_CHAT_ID")
        if token and chat_id:
            for i in range(0, len(summary), 4000):
                try:
                    r = requests.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id": int(chat_id), "text": summary[i:i + 4000]},
                        timeout=15,
                    )
                    r.raise_for_status()
                except Exception as e:
                    log.error(f"Failed to send weekly summary: {e}")
                    sys.exit(1)
            log.info("Weekly summary sent via Telegram.")
        else:
            print(summary)
        sys.exit(0)

    budget_arg = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] in BUDGET_IDS else None
    since_arg = sys.argv[2] if len(sys.argv) > 2 else None
    budgets_to_run = [budget_arg] if budget_arg else list(BUDGET_IDS.keys())

    for bkey in budgets_to_run:
        try:
            summary = categorize_transactions(bkey, since_arg)
            log.info(summary.replace("\n", " | "))
        except Exception as e:
            log.error(f"Failed to categorize {BUDGET_NAMES[bkey]}: {e}")
