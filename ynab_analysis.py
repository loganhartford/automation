#!/usr/bin/env python3
"""Fetch and summarize YNAB transactions for the last 30 days."""

import os
import json
import requests
from datetime import date, timedelta
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("YNAB_TOKEN")
BASE_URL = "https://api.ynab.com/v1"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def get(path):
    r = requests.get(f"{BASE_URL}{path}", headers=HEADERS)
    r.raise_for_status()
    return r.json()["data"]


def milliunits_to_dollars(milliunits):
    return milliunits / 1000.0


def main():
    # Get budgets
    budgets = get("/budgets")["budgets"]
    if not budgets:
        print("No budgets found.")
        return

    # Find "America - 2026" budget
    budget = next((b for b in budgets if b["name"] == "America - 2026"), budgets[0])
    budget_id = budget["id"]
    print(f"Budget: {budget['name']}\n")

    # Date range: last 30 days
    since_date = (date.today() - timedelta(days=30)).isoformat()

    # Get transactions
    txns = get(f"/budgets/{budget_id}/transactions?since_date={since_date}")["transactions"]

    # Get category names
    category_groups = get(f"/budgets/{budget_id}/categories")["category_groups"]
    cat_names = {}
    for group in category_groups:
        for cat in group["categories"]:
            cat_names[cat["id"]] = (group["name"], cat["name"])

    # Filter to outflows only (spending), skip transfers and income
    spending = [t for t in txns if t["amount"] < 0 and not t.get("transfer_account_id")]

    # Group by category
    by_category = defaultdict(list)
    uncategorized = []
    for t in spending:
        if t.get("category_id") and t["category_id"] in cat_names:
            by_category[t["category_id"]].append(t)
        else:
            uncategorized.append(t)

    # Total spent
    total = sum(t["amount"] for t in spending)
    print(f"Period: {since_date} to {date.today().isoformat()}")
    print(f"Total spent: ${milliunits_to_dollars(abs(total)):,.2f}")
    print(f"Transactions: {len(spending)}\n")

    # Sort categories by total spend
    cat_totals = []
    for cat_id, txns_list in by_category.items():
        group_name, cat_name = cat_names[cat_id]
        total_amt = sum(t["amount"] for t in txns_list)
        cat_totals.append((group_name, cat_name, total_amt, txns_list))
    cat_totals.sort(key=lambda x: x[2])  # most negative first

    print("=" * 60)
    print(f"{'CATEGORY':<35} {'AMOUNT':>10}  {'#':>4}")
    print("=" * 60)

    current_group = None
    for group_name, cat_name, total_amt, txns_list in cat_totals:
        if group_name != current_group:
            print(f"\n{group_name.upper()}")
            current_group = group_name
        dollars = milliunits_to_dollars(abs(total_amt))
        print(f"  {cat_name:<33} ${dollars:>9,.2f}  {len(txns_list):>4}")

    if uncategorized:
        unc_total = sum(t["amount"] for t in uncategorized)
        print(f"\n  {'(Uncategorized)':<33} ${milliunits_to_dollars(abs(unc_total)):>9,.2f}  {len(uncategorized):>4}")

    print("\n" + "=" * 60)

    # Top 10 individual transactions
    spending_sorted = sorted(spending, key=lambda t: t["amount"])
    print("\nTOP 10 LARGEST TRANSACTIONS:")
    print("-" * 60)
    for t in spending_sorted[:10]:
        amt = milliunits_to_dollars(abs(t["amount"]))
        payee = (t.get("payee_name") or "Unknown")[:30]
        cat = ""
        if t.get("category_id") and t["category_id"] in cat_names:
            cat = cat_names[t["category_id"]][1]
        print(f"  {t['date']}  {payee:<30}  ${amt:>8,.2f}  {cat}")

    # Output raw JSON for further analysis
    output = {
        "budget_name": budget["name"],
        "since_date": since_date,
        "total_spent": milliunits_to_dollars(abs(total)),
        "transaction_count": len(spending),
        "categories": [
            {
                "group": g,
                "category": c,
                "total": milliunits_to_dollars(abs(amt)),
                "count": len(tl),
                "transactions": [
                    {
                        "date": t["date"],
                        "payee": t.get("payee_name"),
                        "amount": milliunits_to_dollars(abs(t["amount"])),
                        "memo": t.get("memo"),
                    }
                    for t in tl
                ],
            }
            for g, c, amt, tl in cat_totals
        ],
    }

    with open("/tmp/ynab_data.json", "w") as f:
        json.dump(output, f, indent=2)
    print("\n[Full data saved to /tmp/ynab_data.json]")


if __name__ == "__main__":
    main()
