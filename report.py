import json
from datetime import datetime
from db import get_unreported_companies, mark_as_reported

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

ASSESSMENT_EMOJI = {
    "good": "🟢",
    "neutral": "🟡",
    "bad": "🔴",
}

def generate_weekly_report():
    companies = get_unreported_companies()

    if not companies:
        print("No new companies to report this week.")
        return

    lines = []
    lines.append("# Startup Scout Weekly Report")
    lines.append(f"Generated: {datetime.now().strftime('%B %d, %Y')}\n")
    lines.append(f"{len(companies)} new {'company' if len(companies) == 1 else 'companies'} passed your filters this week.\n")
    lines.append("---\n")

    names_to_mark = []
    for name, first_seen, source, report_json in companies:
        report = json.loads(report_json)
        lines.append(f"## {name}")
        lines.append(f"*First seen: {first_seen[:10]} | Source: {source}*\n")

        # Dealbreakers section
        dealbreakers = report.pop("dealbreakers", None)
        if dealbreakers:
            lines.append("### ✅ Dealbreaker Check")
            for key, value in dealbreakers.items():
                label = DEALBREAKER_LABELS.get(key, key.replace("_", " ").title())
                answer = "Yes" if value.get("answer") else "No"
                reason = value.get("reason", "")
                lines.append(f"**{label}** {answer} — {reason}")
            lines.append("")

        # Report section
        lines.append("### 📊 Analysis")
        for key, value in report.items():
            label = REPORT_LABELS.get(key, key.replace("_", " ").title())
            assessment = value.get("assessment", "").lower()
            emoji = ASSESSMENT_EMOJI.get(assessment, "⚪")
            answer = value.get("answer", "")
            lines.append(f"**{label}** {emoji}")
            lines.append(f"{answer}\n")

        lines.append("---\n")
        names_to_mark.append(name)

    report_text = "\n".join(lines)

    filename = f"report_{datetime.now().strftime('%Y-%m-%d')}.md"
    with open(filename, "w") as f:
        f.write(report_text)

    mark_as_reported(names_to_mark)
    print(f"Report saved to {filename}")
    print(report_text)

if __name__ == "__main__":
    generate_weekly_report()