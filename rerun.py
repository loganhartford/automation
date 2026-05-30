"""Re-evaluate companies already in the database.

Usage:
    python3 rerun.py                                    # re-run all companies
    python3 rerun.py 25                                 # re-run 25 most recent
    python3 rerun.py 25 --summary                       # include per-company breakdown
    python3 rerun.py 25 --email                         # send dealbreaker report email when done
    python3 rerun.py --before 2026-05-01                # re-run all companies added before May 1
    python3 rerun.py --before 2026-05-01 --email --summary
    python3 rerun.py 500 --guard                        # enable cost guardrails (default budget: $70)
    python3 rerun.py 500 --guard --budget 90            # guardrails with custom expected budget
"""
import json
import sys
from db import init_db, get_recent_companies, update_company
from evaluate import research_company, check_dealbreakers, _reset_usage, _current_cost, CreditExhaustedError


class GuardAbort(Exception):
    pass


def check_guards(records, total_companies, budget):
    """Check cost guardrails after each company. Returns warning strings; raises GuardAbort to stop the run."""
    warnings = []
    if not records:
        return warnings

    name, outcome, cost = records[-1]

    # Rule 1: per-company runaway — 2.5x the most expensive legitimate case (~$0.42)
    if cost > 1.00:
        raise GuardAbort(f"{name} cost ${cost:.4f} — exceeds $1.00 per-company cap (possible runaway loop)")

    # Rule 2: silent zero — completed but no API calls recorded
    if cost < 0.02 and outcome not in ("credit_exhausted", "error"):
        warnings.append(f"{name} completed with ${cost:.4f} — may have errored before any API call")

    # Rule 3: trailing 10-company average drift
    recent = records[-10:]
    recent_avg = sum(c for _, _, c in recent) / len(recent)
    if recent_avg > 0.40:
        warnings.append(f"Trailing {len(recent)}-company average is ${recent_avg:.4f} — exceeds $0.40 (3x normal)")

    # Rule 4: budget projection (needs ≥20 samples to have a meaningful average)
    processed = len(records)
    if processed >= 20:
        remaining = total_companies - processed
        total_so_far = sum(c for _, _, c in records)
        avg_so_far = total_so_far / processed
        projected = total_so_far + avg_so_far * remaining
        if projected > budget * 1.50:
            raise GuardAbort(
                f"Projected total ${projected:.2f} exceeds 150% of expected ${budget:.2f} — aborting"
            )
        if projected > budget * 1.20:
            warnings.append(f"Projected total ${projected:.2f} exceeds 120% of expected ${budget:.2f}")

    return warnings


def rerun(limit=None, before=None, summary=False, email=False, guard=False, budget=70.0):
    init_db()
    companies = get_recent_companies(limit=limit, before=before)
    if not companies:
        print("No companies in database.")
        return

    total_companies = len(companies)
    print(f"Re-evaluating {total_companies} companies...\n")
    if guard:
        print(f"  [GUARD] Active — budget ${budget:.2f} | per-company cap $1.00 | avg drift $0.40\n")

    records = []  # (name, outcome, cost)
    skipped = []  # names not reached

    for i, (name, source, existing_report_json) in enumerate(companies):
        print(f"=== {name} ===")
        _reset_usage()
        outcome = "error"
        try:
            description_hint = ""
            if existing_report_json:
                existing = json.loads(existing_report_json)
                hint = existing.get("_description", "")
                if hint and not hint.lstrip().startswith("#"):
                    description_hint = hint

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
                update_company(name, passed=False, cost=_current_cost())
            else:
                report = {
                    "_description": description,
                    "_discovery_context": research_context,
                    "dealbreakers": dealbreaker_results,
                }
                outcome = "passed"
                update_company(name, passed=True, report=json.dumps(report), cost=_current_cost())

                location = report.get("location", {}).get("answer", "dealbreaker-only")
                mission = report.get("mission", {}).get("answer", "")
                print(f"  -> Passed | {location}")
                if mission:
                    print(f"     {mission}")
                print()

        except CreditExhaustedError as e:
            print(f"\n  *** CREDITS EXHAUSTED: {e} ***\n")
            outcome = "credit_exhausted"
            records.append((name, outcome, _current_cost()))
            skipped = [row[0] for row in companies[i + 1:]]
            break

        except Exception as e:
            print(f"  ERROR: {e}\n")

        records.append((name, outcome, _current_cost()))

        if guard:
            try:
                guard_warnings = check_guards(records, total_companies, budget)
                for w in guard_warnings:
                    print(f"  [GUARD WARNING] {w}")
            except GuardAbort as e:
                print(f"\n  *** GUARD ABORT: {e} ***\n")
                skipped = [row[0] for row in companies[i + 1:]]
                break

    total = sum(cost for _, _, cost in records)
    processed = len(records)
    not_reached = len(skipped)
    stop_reason = ""
    if skipped:
        last_outcome = records[-1][1] if records else ""
        if last_outcome == "credit_exhausted":
            stop_reason = " (credits exhausted)"
        else:
            stop_reason = " (guard abort)" if guard else " (stopped early)"

    print(f"\nDone. Processed {processed}/{total_companies} companies{stop_reason}.  Total cost: ~${total:.4f}")

    if skipped:
        print(f"\n{not_reached} companies NOT reached:")
        for n in skipped:
            print(f"  - {n}")

    if summary or skipped:
        label = "=== Session Summary ===" if not skipped else "=== Session Summary (partial run) ==="
        print(f"\n{label}")
        name_w = max(len(r[0]) for r in records)
        outcome_w = max(len(r[1]) for r in records)
        for n, outcome, cost in records:
            print(f"  {n:<{name_w}}  {outcome:<{outcome_w}}  ${cost:.4f}")
        print(f"  {'TOTAL':<{name_w}}  {'':<{outcome_w}}  ${total:.4f}")

    if email:
        print("\nSending report email...")
        from report import generate_weekly_report
        generate_weekly_report()


if __name__ == "__main__":
    args = sys.argv[1:]
    show_summary = "--summary" in args or "-s" in args
    send_email   = "--email"   in args or "-e" in args
    use_guard    = "--guard"   in args or "-g" in args
    args = [a for a in args if a not in ("--summary", "-s", "--email", "-e", "--guard", "-g")]

    before = None
    if "--before" in args:
        idx = args.index("--before")
        before = args[idx + 1]
        args = args[:idx] + args[idx + 2:]

    budget = 70.0
    if "--budget" in args:
        idx = args.index("--budget")
        budget = float(args[idx + 1])
        args = args[:idx] + args[idx + 2:]

    limit = int(args[0]) if args else None
    rerun(limit=limit, before=before, summary=show_summary, email=send_email, guard=use_guard, budget=budget)
