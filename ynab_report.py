import logging
import os
import sys
from datetime import date, timedelta

import anthropic
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
BUDGET_NAMES = {"america": "America - 2026 (USD)", "canada": "Canada - 2026 (CAD)"}
BUDGET_CURRENCIES = {"america": "USD", "canada": "CAD"}

SKIP_GROUPS = {"Internal Master Category", "Credit Card Payments"}
ASSET_TYPES = {"checking", "savings", "cash", "otherAsset", "investmentAccount"}
LIABILITY_TYPES = {"creditCard", "mortgage", "lineOfCredit", "otherLiability"}

log = logging.getLogger(__name__)
_anthropic_client = None

NARRATIVE_SYSTEM_OLLAMA = """\
/no_think
You are Logan's personal finance analyst writing one section of a monthly report.
Plain text only — no asterisks, no markdown, no bullet symbols. Use blank lines between paragraphs. Be specific with numbers. Keep it concise.\
"""

NARRATIVE_SYSTEM_CLAUDE = """\
You are Logan's personal finance analyst writing one section of a monthly report.
Plain text only — no asterisks, no markdown, no bullet symbols. Use blank lines between paragraphs. Be specific with numbers. Keep it concise.\
"""

# claude-opus-4-6 pricing
OPUS_INPUT_COST_PER_TOKEN = 15.0 / 1_000_000
OPUS_OUTPUT_COST_PER_TOKEN = 75.0 / 1_000_000


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _dollars(milliunits):
    return abs(milliunits) / 1000.0


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


def _ollama_narrative(user_prompt: str, timeout: int = 90) -> str:
    try:
        r = requests.post(
            f"{OLLAMA_BASE}/v1/chat/completions",
            json={
                "model": OLLAMA_MODEL,
                "messages": [
                    {"role": "system", "content": NARRATIVE_SYSTEM_OLLAMA},
                    {"role": "user", "content": user_prompt},
                ],
                "stream": False,
            },
            timeout=timeout,
        )
    except requests.exceptions.ConnectionError:
        raise RuntimeError("Ollama is not running.")
    except requests.exceptions.Timeout:
        raise RuntimeError(f"Ollama timed out after {timeout}s.")
    if r.status_code != 200:
        raise RuntimeError(f"Ollama returned HTTP {r.status_code}: {r.text[:200]}")
    content = r.json()["choices"][0]["message"]["content"] or ""
    return content.replace("**", "").replace("*", "").strip()


def _claude_narrative(user_prompt: str, usage: dict, timeout: int = 90) -> str:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic()
    resp = _anthropic_client.messages.create(
        model="claude-opus-4-6",
        max_tokens=512,
        system=NARRATIVE_SYSTEM_CLAUDE,
        messages=[{"role": "user", "content": user_prompt}],
    )
    usage["input_tokens"] += resp.usage.input_tokens
    usage["output_tokens"] += resp.usage.output_tokens
    return resp.content[0].text.strip()


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _prev_month_date() -> date:
    return (date.today().replace(day=1) - timedelta(days=1)).replace(day=1)


def _step_back(d: date, n: int) -> date:
    for _ in range(n):
        d = (d.replace(day=1) - timedelta(days=1)).replace(day=1)
    return d


def _month_str(d: date) -> str:
    return d.strftime("%Y-%m-01")


def _month_label(d: date) -> str:
    return d.strftime("%B %Y")


# ---------------------------------------------------------------------------
# YNAB data fetching
# ---------------------------------------------------------------------------

def _fetch_month_data(budget_id: str, month_date: date) -> dict:
    data = _ynab_get(f"/budgets/{budget_id}/months/{_month_str(month_date)}")["month"]

    income = _dollars(data.get("income", 0))
    activity = data.get("activity", 0)   # negative in YNAB
    spending = _dollars(abs(activity))
    savings_rate = round((income - spending) / income * 100, 1) if income > 0 else 0.0
    budgeted = _dollars(data.get("budgeted", 0))

    categories = []
    for c in data.get("categories", []):
        if c.get("category_group_name") in SKIP_GROUPS:
            continue
        b = _dollars(c.get("budgeted", 0))
        s = _dollars(abs(c.get("activity", 0)))
        if b == 0 and s == 0:
            continue
        over_by = round(s - b, 2) if s > b + 1.0 else 0.0
        categories.append({
            "group": c.get("category_group_name", "Other"),
            "name": c["name"],
            "budgeted": b,
            "spent": s,
            "over_by": over_by,
        })

    overspent = [c for c in categories if c["over_by"] > 1.0]

    return {
        "month": month_date.strftime("%Y-%m"),
        "label": _month_label(month_date),
        "income": income,
        "spending": spending,
        "savings_rate": savings_rate,
        "budgeted": budgeted,
        "categories": categories,
        "overspent": overspent,
    }


def _fetch_accounts(budget_id: str) -> dict:
    accounts = _ynab_get(f"/budgets/{budget_id}/accounts")["accounts"]
    assets, liabilities = [], []
    for a in accounts:
        if a.get("closed") or a.get("deleted"):
            continue
        bal = a["balance"] / 1000.0
        entry = {"name": a["name"], "type": a["type"], "balance": abs(bal)}
        if a["type"] in ASSET_TYPES or bal > 0:
            assets.append(entry)
        elif a["type"] in LIABILITY_TYPES or bal < 0:
            liabilities.append(entry)

    total_assets = sum(x["balance"] for x in assets)
    total_liabilities = sum(x["balance"] for x in liabilities)
    return {
        "assets": assets,
        "liabilities": liabilities,
        "total_assets": round(total_assets, 2),
        "total_liabilities": round(total_liabilities, 2),
        "net_worth": round(total_assets - total_liabilities, 2),
    }


def _fetch_budget_data(budget_key: str, target_month: date) -> dict:
    budget_id = BUDGET_IDS[budget_key]
    current = _fetch_month_data(budget_id, target_month)
    prior1 = _fetch_month_data(budget_id, _step_back(target_month, 1))
    prior2 = _fetch_month_data(budget_id, _step_back(target_month, 2))
    accounts = _fetch_accounts(budget_id)
    return {
        "key": budget_key,
        "name": BUDGET_NAMES[budget_key],
        "currency": BUDGET_CURRENCIES[budget_key],
        "current": current,
        "prior_months": [prior1, prior2],
        "accounts": accounts,
    }


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

def _fmt_budget_top(bd: dict) -> str:
    c = bd["current"]
    return (
        f"{bd['name']}:\n"
        f"  Income: ${c['income']:,.2f}\n"
        f"  Spending: ${c['spending']:,.2f}\n"
        f"  Savings rate: {c['savings_rate']}%\n"
        f"  Budgeted: ${c['budgeted']:,.2f}"
    )


def _fmt_overspent(bd: dict) -> str:
    c = bd["current"]
    currency = bd["currency"]
    if not c["overspent"]:
        return f"{bd['name']}: No categories overspent this month."

    lines = [f"{bd['name']} ({currency}) — overspent categories:"]
    for cat in sorted(c["overspent"], key=lambda x: -x["over_by"]):
        lines.append(f"  {cat['name']} ({cat['group']}): budgeted ${cat['budgeted']:,.0f}, spent ${cat['spent']:,.0f} (over by ${cat['over_by']:,.0f})")

    # Add 3-month history for each overspent category
    lines.append(f"\n  3-month history for overspent categories:")
    for cat in c["overspent"]:
        name = cat["name"]
        history = [c]
        history += bd["prior_months"]
        parts = []
        for m in history:
            match = next((x for x in m["categories"] if x["name"] == name), None)
            parts.append(f"{m['label'][:3]} ${match['spent']:,.0f}" if match else f"{m['label'][:3]} n/a")
        lines.append(f"  {name}: {' | '.join(parts)}")

    return "\n".join(lines)


def _fmt_trends(bd: dict) -> str:
    months = [bd["current"]] + bd["prior_months"]
    lines = [f"{bd['name']}:"]
    for m in months:
        lines.append(f"  {m['label']}: income ${m['income']:,.0f} / spending ${m['spending']:,.0f} / savings {m['savings_rate']}%")

    # Top 5 categories by spend for current month
    top5 = sorted(bd["current"]["categories"], key=lambda x: -x["spent"])[:5]
    lines.append(f"\n  Top spending categories ({bd['current']['label']}):")
    for cat in top5:
        lines.append(f"    {cat['name']}: ${cat['spent']:,.0f}")

    return "\n".join(lines)


def _fmt_accounts(bd: dict) -> str:
    acc = bd["accounts"]
    currency = bd["currency"]
    lines = [f"{bd['name']} ({currency}):"]
    lines.append("  Assets:")
    for a in acc["assets"]:
        lines.append(f"    {a['name']}: ${a['balance']:,.2f}")
    lines.append(f"  Total assets: ${acc['total_assets']:,.2f}")
    lines.append("  Liabilities:")
    for l in acc["liabilities"]:
        lines.append(f"    {l['name']}: ${l['balance']:,.2f}")
    lines.append(f"  Total liabilities: ${acc['total_liabilities']:,.2f}")
    lines.append(f"  Net worth: ${acc['net_worth']:,.2f}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Raw data renderer (deterministic, no Ollama)
# ---------------------------------------------------------------------------

def _render_raw_data(bd: dict) -> str:
    c = bd["current"]
    lines = [f"{bd['name']} — {c['label']}", ""]
    current_group = None
    for cat in c["categories"]:
        if cat["group"] != current_group:
            lines.append(f"{cat['group']}:")
            current_group = cat["group"]
        status = f"OVER ${cat['over_by']:,.0f}" if cat["over_by"] > 1.0 else "OK"
        lines.append(f"  {cat['name']:<30} budgeted ${cat['budgeted']:>8,.2f}  spent ${cat['spent']:>8,.2f}  {status}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main report generator
# ---------------------------------------------------------------------------

def generate_monthly_report(month: str | None = None, backend: str = "ollama") -> str:
    if month:
        y, m = map(int, month.split("-"))
        target = date(y, m, 1)
    else:
        target = _prev_month_date()

    label = _month_label(target)
    from datetime import datetime
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    log.info(f"Generating monthly report for {label} via {backend}")

    america = _fetch_budget_data("america", target)
    canada = _fetch_budget_data("canada", target)

    usage = {"input_tokens": 0, "output_tokens": 0}

    def narrative(prompt: str, timeout: int = 90) -> str:
        if backend == "claude":
            return _claude_narrative(prompt, usage, timeout)
        return _ollama_narrative(prompt, timeout)

    # --- Call 1: Overview ---
    overview_prompt = (
        f"Write the budget overview section of Logan's monthly financial report for {label}.\n\n"
        + _fmt_budget_top(america) + "\n\n"
        + _fmt_budget_top(canada) + "\n\n"
        "Write 2-3 sentences covering: how the month went overall, whether savings rates are healthy "
        "(target is >20%), and any notable difference between the two plans. Do not use bullet points."
    )
    try:
        overview = narrative(overview_prompt)
    except RuntimeError as e:
        overview = f"[Section unavailable: {e}]"

    # --- Call 2: Overspending ---
    overspend_prompt = (
        f"Write the overspending analysis section of Logan's monthly financial report for {label}.\n\n"
        + _fmt_overspent(america) + "\n\n"
        + _fmt_overspent(canada) + "\n\n"
        "For each overspent category write one sentence with the dollar amounts. "
        "If a category has been overspent 2 or more months in a row, explicitly flag it as a recurring pattern. "
        "If a budget had no overspending, say so in one sentence."
    )
    try:
        overspend = narrative(overspend_prompt)
    except RuntimeError as e:
        overspend = f"[Section unavailable: {e}]"

    # --- Call 3: Trends ---
    trends_prompt = (
        f"Write the spending trends section of Logan's monthly financial report for {label}.\n\n"
        + _fmt_trends(america) + "\n\n"
        + _fmt_trends(canada) + "\n\n"
        "Identify 2-3 meaningful trends across both plans. Look for: categories that are consistently "
        "growing month over month, whether the savings rate is improving or declining, any notable "
        "changes from the previous month. Write 2-3 sentences per observation."
    )
    try:
        trends = narrative(trends_prompt)
    except RuntimeError as e:
        trends = f"[Section unavailable: {e}]"

    # --- Call 4: Net Worth & Actions ---
    networth_prompt = (
        f"Write the net worth and actions section of Logan's monthly financial report for {label}.\n\n"
        + _fmt_accounts(america) + "\n\n"
        + _fmt_accounts(canada) + "\n\n"
        f"America savings rate: {america['current']['savings_rate']}%\n"
        f"Canada savings rate: {canada['current']['savings_rate']}%\n\n"
        "Write two clearly labelled sections:\n"
        "NET WORTH: 2-3 sentences on the combined asset/liability picture, noting any concerns "
        "(e.g. high credit card balances, low cash reserves).\n"
        "ACTIONS FOR NEXT MONTH: 2-3 specific, concrete actions based on this month's data. "
        "Use actual numbers (e.g. 'Cut Dining Out budget from $200 to $150'). No bullet points."
    )
    try:
        networth = narrative(networth_prompt, timeout=120)
    except RuntimeError as e:
        networth = f"[Section unavailable: {e}]"

    backend_label = "Ollama (qwen3:8b)" if backend == "ollama" else "Claude Opus"
    footer = f"Generated with {backend_label}"
    if backend == "claude":
        cost = usage["input_tokens"] * OPUS_INPUT_COST_PER_TOKEN + usage["output_tokens"] * OPUS_OUTPUT_COST_PER_TOKEN
        footer += f" — Cost: ${cost:.4f} ({usage['input_tokens']:,} input / {usage['output_tokens']:,} output tokens)"

    report = "\n\n".join([
        f"MONTHLY FINANCIAL REPORT — {label.upper()}\nGenerated {generated_at}",
        "=== OVERVIEW ===\n" + overview,
        "=== SPENDING ANALYSIS ===\n" + overspend,
        "=== TRENDS ===\n" + trends,
        "=== NET WORTH & ACTIONS ===\n" + networth,
        footer,
    ])

    log.info(f"Monthly report ({backend}) generated successfully.")
    return report


def generate_both_reports(month: str | None = None) -> tuple[str, str]:
    """Generate the monthly report with both Ollama and Claude Opus backends."""
    if month:
        y, m = map(int, month.split("-"))
        target = date(y, m, 1)
    else:
        target = _prev_month_date()

    label = _month_label(target)
    log.info(f"Generating dual monthly reports for {label}")

    ollama_report = generate_monthly_report(month, backend="ollama")
    claude_report = generate_monthly_report(month, backend="claude")
    return ollama_report, claude_report


# ---------------------------------------------------------------------------
# CLI / systemd timer entry point
# ---------------------------------------------------------------------------

def _send_telegram(token: str, chat_id: str, text: str) -> None:
    chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)]
    for chunk in chunks:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": int(chat_id), "text": chunk},
            timeout=15,
        )
        r.raise_for_status()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    month_arg = sys.argv[1] if len(sys.argv) > 1 else None

    try:
        ollama_report, claude_report = generate_both_reports(month_arg)
    except Exception as e:
        log.error(f"Failed to generate reports: {e}")
        sys.exit(1)

    token = os.getenv("TELEGRAM_YNAB_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_YNAB_CHAT_ID")

    if token and chat_id:
        preamble = (
            "Sending your monthly report twice — once via Ollama (local, free) and once via "
            "Claude Opus (cloud, paid). Compare quality and choose which to keep going forward."
        )
        try:
            _send_telegram(token, chat_id, preamble)
            _send_telegram(token, chat_id, "--- OLLAMA REPORT ---\n\n" + ollama_report)
            _send_telegram(token, chat_id, "--- CLAUDE OPUS REPORT ---\n\n" + claude_report)
        except Exception as e:
            log.error(f"Failed to send Telegram message: {e}")
            sys.exit(1)
        log.info("Dual reports delivered via Telegram.")
    else:
        print("=== OLLAMA REPORT ===\n")
        print(ollama_report)
        print("\n=== CLAUDE OPUS REPORT ===\n")
        print(claude_report)
