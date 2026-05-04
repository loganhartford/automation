"""Re-evaluate companies already in the database.

Usage:
    python3 rerun.py              # re-run all companies
    python3 rerun.py 10           # re-run 10 most recent
    python3 rerun.py 10 --summary # include per-company breakdown at the end
"""
import json
import sys
from db import init_db, get_recent_companies, update_company
from evaluate import research_company, check_dealbreakers, generate_report, _reset_usage, _current_cost


def rerun(limit=None, summary=False):
    init_db()
    companies = get_recent_companies(limit=limit)
    if not companies:
        print("No companies in database.")
        return

    print(f"Re-evaluating {len(companies)} companies...\n")

    records = []  # (name, outcome, cost)

    for name, source, existing_report_json in companies:
        print(f"=== {name} ===")
        _reset_usage()
        outcome = "error"
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
                failed_key = next((k for k, v in dealbreaker_results.items() if v["answer"] == "no"), "unknown")
                outcome = f"failed:{failed_key}"
                print(f"  -> Failed dealbreakers, updating db.\n")
                update_company(name, passed=False)
            else:
                print(f"  Generating report...")
                report = generate_report(name, description, dealbreaker_results, research_context)
                report["_description"] = description
                update_company(name, passed=True, report=json.dumps(report))

                location = report.get("location", {}).get("answer", "?")
                mission = report.get("mission", {}).get("answer", "")
                outcome = "passed"
                print(f"  -> Passed | {location}")
                if mission:
                    print(f"     {mission}")
                print()

        except Exception as e:
            print(f"  ERROR: {e}\n")

        records.append((name, outcome, _current_cost()))

    total = sum(cost for _, _, cost in records)
    print(f"Done. Total cost: ~${total:.4f}")

    if summary:
        print("\n=== Session Summary ===")
        name_w = max(len(r[0]) for r in records)
        outcome_w = max(len(r[1]) for r in records)
        for name, outcome, cost in records:
            print(f"  {name:<{name_w}}  {outcome:<{outcome_w}}  ${cost:.4f}")
        print(f"  {'TOTAL':<{name_w}}  {'':<{outcome_w}}  ${total:.4f}")


if __name__ == "__main__":
    args = sys.argv[1:]
    show_summary = "--summary" in args or "-s" in args
    args = [a for a in args if a not in ("--summary", "-s")]
    limit = int(args[0]) if args else None
    rerun(limit=limit, summary=show_summary)
