"""Re-evaluate companies already in the database.

Usage:
    python3 rerun.py           # re-run all companies
    python3 rerun.py 10        # re-run 10 most recent
"""
import json
import sys
from db import init_db, get_recent_companies, update_company
from evaluate import research_company, check_dealbreakers, generate_report


def rerun(limit=None):
    init_db()
    companies = get_recent_companies(limit=limit)
    if not companies:
        print("No companies in database.")
        return

    print(f"Re-evaluating {len(companies)} companies...\n")

    for name, source, existing_report_json in companies:
        print(f"=== {name} ===")
        try:
            description_hint = ""
            if existing_report_json:
                existing = json.loads(existing_report_json)
                description_hint = existing.get("_description", "")

            print(f"  Researching...")
            research_context = research_company(name, description_hint)
            if not research_context:
                print(f"  Warning: research returned nothing.")

            description = description_hint or (research_context.split("\n\n")[0].strip() if research_context else name)

            print(f"  Checking dealbreakers...")
            passed, dealbreaker_results = check_dealbreakers(name, description, research_context)

            for key, value in dealbreaker_results.items():
                icon = "✅" if value["answer"] == "yes" else ("⚠️ " if value["answer"] == "unknown" else "❌")
                print(f"    {icon} {key}: {value['reason']}")

            if not passed:
                print(f"  -> Failed dealbreakers, updating db.\n")
                update_company(name, passed=False)
                continue

            print(f"  Generating report...")
            report = generate_report(name, description, dealbreaker_results, research_context)
            report["_description"] = description
            update_company(name, passed=True, report=json.dumps(report))

            location = report.get("location", {}).get("answer", "?")
            mission = report.get("mission", {}).get("answer", "")
            print(f"  -> Passed | {location}")
            if mission:
                print(f"     {mission}")
            print()

        except Exception as e:
            print(f"  ERROR: {e}\n")
            continue

    print("Done.")


if __name__ == "__main__":
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    rerun(limit=limit)
