import json
from datetime import datetime
from db import get_unreported_companies, mark_as_reported
from gmail import send_report

DEALBREAKER_LABELS = {
    "developing_hardware": "Developing Hardware?",
    "is_startup": "Is a Startup?",
    "solves_real_problem": "Solves Real Problem?",
    "growing_quickly": "Growing Quickly?",
    "billion_dollar_potential": "Billion Dollar Potential?",
}

REPORT_LABELS = {
    "company_size": "Company Size",
    "monopoly_potential": "Monopoly Potential",
    "novelty": "Novelty",
    "breakthrough_vs_incremental": "Breakthrough vs Incremental",
    "timing": "Timing",
    "unique_opportunity": "Unique Opportunity",
}

ASSESSMENT_LABEL = {
    "good": "[GOOD]",
    "neutral": "[OK]",
    "bad": "[BAD]",
}

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
    <style>
        h1 {{ font-size: 26px; border-bottom: 2px solid #eee; padding-bottom: 10px; }}
        h2 {{ font-size: 22px; margin-top: 48px; margin-bottom: 4px; }}
        h3 {{ font-size: 16px; margin-top: 24px; margin-bottom: 12px; color: #444; }}
        .meta {{ color: #888; font-size: 13px; margin-bottom: 4px; }}
        .description {{ color: #555; font-style: italic; margin-bottom: 16px; font-size: 14px; }}
        .dealbreaker-item {{ margin-bottom: 8px; }}
        .report-item {{ margin-bottom: 20px; }}
        .report-label {{ font-weight: bold; margin-bottom: 4px; }}
        .report-answer {{ margin: 0; color: #333; }}
        .assessment {{ font-size: 12px; font-weight: bold; color: #666; margin-left: 6px; }}
        hr {{ border: none; border-top: 1px solid #eee; margin: 40px 0; }}
    </style>

    <h1>Startup Scout Weekly Report</h1>
    <p>Generated: {date_str}</p>
    <p><strong>{count}</strong> new {'company' if count == 1 else 'companies'} passed your filters this week.</p>
    <hr>
    """

    names_to_mark = []
    for name, first_seen, source, report_json in companies:
        report = json.loads(report_json)
        description = report.pop("_description", None)
        dealbreakers = report.pop("dealbreakers", None)

        html += f"<h2>{name}</h2>"
        html += f'<p class="meta">First seen: {first_seen[:10]} | Source: {source}</p>'
        if description:
            html += f'<p class="description">{description}</p>'

        # Dealbreaker section
        if dealbreakers:
            html += "<h3>Dealbreaker Check</h3>"
            for key, value in dealbreakers.items():
                label = DEALBREAKER_LABELS.get(key, key.replace("_", " ").title())
                answer = "Yes" if value.get("answer") else "No"
                reason = value.get("reason", "")
                html += f'<div class="dealbreaker-item"><strong>{label}</strong> {answer} — {reason}</div>'

        # Analysis section
        html += "<h3>Analysis</h3>"
        for key, value in report.items():
            if not isinstance(value, dict):
                continue
            label = REPORT_LABELS.get(key, key.replace("_", " ").title())
            assessment = value.get("assessment", "").lower()
            assessment_label = ASSESSMENT_LABEL.get(assessment, "")
            answer = value.get("answer", "")
            html += f"""
            <div class="report-item">
                <div class="report-label">{label} <span class="assessment">{assessment_label}</span></div>
                <p class="report-answer">{answer}</p>
            </div>
            """

        html += "<hr>"
        names_to_mark.append(name)

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

if __name__ == "__main__":
    generate_weekly_report()