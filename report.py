import json
import os
import requests
import traceback
from datetime import datetime
from html import escape
from dotenv import load_dotenv
from google.auth.exceptions import RefreshError
from db import get_unreported_companies, mark_as_reported, get_source_stats
from gmail import send_report
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

DEALBREAKER_LABELS = {
    "developing_hardware": "Developing Hardware?",
    "is_startup": "Is a Startup?",
    "solves_real_problem": "Solves Real Problem?",
    "growing_quickly": "Growing Quickly?",
    "billion_dollar_potential": "Billion Dollar Potential?",
}

REPORT_LABELS = {
    "company_size": "Company Size",
    "billion_dollar_potential": "Billion Dollar Potential",
    "growing_quickly": "Growing Quickly",
    "solves_real_problem": "Solves Real Problem",
    "monopoly_potential": "Monopoly Potential",
    "novelty": "Novelty",
    "breakthrough_vs_incremental": "Breakthrough vs Incremental",
    "timing": "Timing",
    "unique_opportunity": "Unique Opportunity",
    "learning_opportunities": "Learning Opportunities",
    "transferable_skills": "Transferable Skills",
}

ASSESSMENT_LABEL = {
    "good": "[GOOD]",
    "neutral": "[OK]",
    "bad": "[BAD]",
}

_STYLES = """
    <style>
        h1 { font-size: 26px; border-bottom: 2px solid #eee; padding-bottom: 10px; }
        h2 { font-size: 22px; margin-top: 48px; margin-bottom: 4px; }
        h3 { font-size: 16px; margin-top: 24px; margin-bottom: 12px; color: #444; }
        .meta { color: #888; font-size: 13px; margin-bottom: 4px; }
        .description { color: #555; font-style: italic; margin-bottom: 8px; font-size: 14px; }
        .mission { color: #333; margin-bottom: 16px; font-size: 14px; }
        .dealbreaker-item { margin-bottom: 8px; }
        .report-item { margin-bottom: 20px; }
        .report-label { font-weight: bold; margin-bottom: 4px; }
        .report-answer { margin: 0; color: #333; }
        .assessment { font-size: 12px; font-weight: bold; color: #666; margin-left: 6px; }
        .action-link { display: inline-block; margin-top: 16px; font-weight: bold; color: #1a73e8; text-decoration: none; }
        .discovery { color: #333; font-size: 14px; line-height: 1.7; margin-bottom: 16px; white-space: pre-line; }
        hr { border: none; border-top: 1px solid #eee; margin: 40px 0; }
    </style>
"""


def _full_report_link(company_id):
    bot_username = os.getenv("TELEGRAM_SCOUT_BOT_USERNAME")
    if not bot_username:
        return None
    bot_username = bot_username.lstrip("@")
    return f"https://t.me/{bot_username}?start=full_{company_id}"


def _render_company_html(company_id, name, first_seen, source, report_json, cost=None):
    report = json.loads(report_json)
    description = report.pop("_description", None)
    if description and description.lstrip().startswith("#"):
        description = None
    discovery_context = report.pop("_discovery_context", None)
    dealbreakers = report.pop("dealbreakers", None)
    location = report.pop("location", {}).get("answer", "Unknown")
    mission = report.pop("mission", {}).get("answer", None)

    cost_str = f" | Cost: ${cost:.4f}" if cost is not None else ""
    html = f"<h2>{escape(name)}</h2>"
    html += f'<p class="meta">First seen: {escape(first_seen[:10])} | Source: {escape(source)} | {escape(location)}{escape(cost_str)}</p>'
    if description:
        html += f'<p class="description">{escape(description)}</p>'
    if mission:
        html += f'<p class="mission">{escape(mission)}</p>'

    if dealbreakers:
        html += "<h3>Dealbreaker Check</h3>"
        for key, value in dealbreakers.items():
            label = DEALBREAKER_LABELS.get(key, key.replace("_", " ").title())
            raw = value.get("answer")
            answer = "Yes" if raw == "yes" else ("⚠️ Unknown" if raw == "unknown" else "No")
            reason = value.get("reason", "")
            html += f'<div class="dealbreaker-item"><strong>{escape(label)}</strong> {answer} — {escape(reason)}</div>'

    report_items = []
    for key, value in report.items():
        if not isinstance(value, dict):
            continue
        label = REPORT_LABELS.get(key, key.replace("_", " ").title())
        assessment = value.get("assessment", "").lower()
        assessment_label = ASSESSMENT_LABEL.get(assessment, "")
        answer = value.get("answer", "")
        report_items.append(f"""
        <div class="report-item">
            <div class="report-label">{escape(label)} <span class="assessment">{escape(assessment_label)}</span></div>
            <p class="report-answer">{escape(answer)}</p>
        </div>
        """)

    if report_items:
        html += "<h3>Analysis</h3>"
        html += "".join(report_items)
    else:
        if discovery_context:
            html += f'<div class="discovery">{escape(discovery_context)}</div>'
        link = _full_report_link(company_id)
        if link:
            html += (
                f'<p><a class="action-link" href="{escape(link)}">Generate full report in Telegram</a>'
                f'<br><span class="meta">If Telegram only opens the chat, send <code>/full {company_id}</code>.</span></p>'
            )
        else:
            html += '<p class="meta">Set TELEGRAM_SCOUT_BOT_USERNAME to enable full-report links.</p>'

    return html


def generate_weekly_report():
    companies = get_unreported_companies()

    if not companies:
        print("No new companies to report this week.")
        return

    date_str = datetime.now().strftime('%B %d, %Y')
    count = len(companies)

    html = f"""
    <html>
    <body style="font-family: sans-serif; max-width: 780px; margin: 40px auto; color: #222; line-height: 1.6; font-size: 15px;">
    {_STYLES}
    <h1>Startup Scout Weekly Report</h1>
    <p>Generated: {date_str}</p>
    <p><strong>{count}</strong> new {'company' if count == 1 else 'companies'} passed your filters this week.</p>
    <hr>
    """

    names_to_mark = []
    costs = []
    for company_id, name, first_seen, source, report_json, cost in companies:
        html += _render_company_html(company_id, name, first_seen, source, report_json, cost)
        html += "<hr>"
        names_to_mark.append(name)
        if cost is not None:
            costs.append(cost)

    # Cost summary
    if costs:
        total_cost = sum(costs)
        html += f'<h2>Report Cost</h2>'
        html += f'<p class="meta">Total: ${total_cost:.4f} across {len(costs)} evaluated {"company" if len(costs) == 1 else "companies"}</p>'
        html += "<hr>"

    # All-time source stats
    source_stats = get_source_stats()
    if source_stats:
        html += "<h2>Newsletter Source Stats (All Time)</h2>"
        html += """<table style="border-collapse:collapse;font-size:14px;width:100%">
        <thead><tr>
            <th style="text-align:left;padding:6px 12px;border-bottom:2px solid #eee">Source</th>
            <th style="text-align:right;padding:6px 12px;border-bottom:2px solid #eee">Passed</th>
            <th style="text-align:right;padding:6px 12px;border-bottom:2px solid #eee">Failed</th>
            <th style="text-align:right;padding:6px 12px;border-bottom:2px solid #eee">Total</th>
            <th style="text-align:right;padding:6px 12px;border-bottom:2px solid #eee">Pass %</th>
        </tr></thead><tbody>"""
        for src, passed, failed in source_stats:
            total = passed + failed
            pct = f"{passed / total * 100:.0f}%" if total else "—"
            html += f"""<tr>
            <td style="padding:5px 12px;border-bottom:1px solid #f0f0f0">{escape(src or "unknown")}</td>
            <td style="text-align:right;padding:5px 12px;border-bottom:1px solid #f0f0f0">{passed}</td>
            <td style="text-align:right;padding:5px 12px;border-bottom:1px solid #f0f0f0">{failed}</td>
            <td style="text-align:right;padding:5px 12px;border-bottom:1px solid #f0f0f0">{total}</td>
            <td style="text-align:right;padding:5px 12px;border-bottom:1px solid #f0f0f0">{pct}</td>
            </tr>"""
        html += "</tbody></table>"

    html += "</body></html>"

    filename = f"reports/report_{datetime.now().strftime('%Y-%m-%d')}.html"
    with open(filename, "w") as f:
        f.write(html)

    send_report(
        to_address="logan.hartford@outlook.com",
        subject=f"Startup Scout Report — {date_str}",
        markdown_body="See HTML version of this report.",
        html_body=html
    )

    mark_as_reported(names_to_mark)
    print(f"Report saved to {filename} and emailed.")


def send_single_company_report(name, first_seen, source, report_json):
    date_str = datetime.now().strftime('%B %d, %Y')
    company_html = _render_company_html(None, name, first_seen, source, report_json)

    html = f"""
    <html>
    <body style="font-family: sans-serif; max-width: 780px; margin: 40px auto; color: #222; line-height: 1.6; font-size: 15px;">
    {_STYLES}
    <h1>Startup Scout: {name}</h1>
    <p>Generated: {date_str}</p>
    <hr>
    {company_html}
    </body></html>
    """

    send_report(
        to_address="logan.hartford@outlook.com",
        subject=f"Startup Scout: {name} — {date_str}",
        markdown_body=f"Report for {name}. See HTML version.",
        html_body=html
    )
    mark_as_reported([name])


if __name__ == "__main__":
    try:
        generate_weekly_report()
    except RefreshError:
        # Gmail auth is broken — Telegram only, email won't work.
        _notify_telegram(
            "⚠️ Scout report: Gmail auth token expired.\n"
            "Delete token.pickle and re-run the OAuth flow to restore."
        )
        print("Gmail auth token expired — Telegram alert sent.")
    except Exception as e:
        tb = traceback.format_exc()
        save_error_context("report.py", type(e).__name__, str(e), tb)
        _notify_telegram(f"⚠️ Scout report failed: {type(e).__name__}: {e}\n\n{REPAIR_HINT}")
        try:
            send_report(
                to_address="logan.hartford@outlook.com",
                subject="Startup Scout - Report Failed",
                markdown_body=f"report.py crashed with the following error:\n\n{tb}",
            )
        except Exception:
            pass
        raise
