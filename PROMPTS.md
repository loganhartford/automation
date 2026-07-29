# Prompts

Reusable prompts for one-off or fresh-chat agent runs.

---

## Manual Scout Pipeline Run (no API calls)

Use this when you want to run the Startup Scout pipeline by hand — e.g. the
automated pipeline's API budget/access is unavailable, or you just want a
human-reviewable batch run. Paste into a fresh chat opened in this repo
(`/home/logan/automation`) with WebSearch and file/Bash tools available.

```
Read SCOUT_PLAYBOOK.md in full and follow it exactly. You are manually executing the
Startup Scout pipeline (normally run by evaluate.py) by hand — do NOT run evaluate.py,
scout_bot.py, report.py, or make any call to an LLM API (OpenAI/Anthropic/etc). All
reasoning (extraction, pre-filter, dealbreaker judgment, exclusions, scoring) is done by
you directly. The only outside tool you may use is WebSearch, for Stage 4 discovery
research (max 2 searches per company, per the playbook).

Newsletter source: [PASTE NEWSLETTER TEXT OR GIVE PATH, e.g. newsletters/foo.txt]

Steps:

1. Stage 1 — extract the list of candidate companies (name + one-sentence description)
   from the newsletter per the playbook's rules.

2. For each company, in order:
   a. Stage 2 — dedup check: query scout.db (read-only) for a case-insensitive name
      match:
      sqlite3 scout.db "SELECT id FROM companies WHERE LOWER(name)=LOWER('...')"
      Skip immediately if found.
   b. Stage 3 — description-only pre-filter.
   c. Stage 4 — discovery research (WebSearch, up to 2 queries), write the
      300–400 word plain-prose summary per the spec.
   d. Stage 5 — developing_hardware dealbreaker.
   e. Stage 6 — is_startup dealbreaker.
   f. Stage 7 — category exclusions (ITAR, quantum, medical).
   g. Immediately persist the result to scout.db so dedup stays correct for the rest
      of the run and for future automated runs. Use a small python3 -c snippet (via
      Bash) with sqlite3 and parameterized queries — never string-format the company
      name/description into SQL. Match evaluate.py's schema exactly:
      - Failed at any stage: passed_dealbreakers=0, report=NULL
      - Passed all stages: passed_dealbreakers=1, notified_date=NULL, cost=NULL,
        report = json.dumps({
          "_description": <stage1 description>,
          "_discovery_context": <stage4 summary>,
          "dealbreakers": {
            "developing_hardware": {"answer": "yes", "reason": "..."},
            "is_startup": {"answer": "yes", "reason": "..."}
          }
        })
      - source = a short identifier for this newsletter (e.g. its filename)
      - first_seen = current ISO timestamp
      Leave notified_date NULL on passes — that's what lets the real weekly
      report.py pick these up and email/mark them later; don't mark them reported
      yourself.

3. Checkpoint reporting: keep a running count of companies that PASS all stages
   (failures don't count toward the batch). Every time that count reaches a multiple
   of 10, write reports/manual_report_batch_<N>.md (N = 1, 2, 3...) containing just
   that batch of 10 passing companies, in the same format as existing files in
   reports/ (e.g. reports/report_2026-02-22.md) — header, then per company: name,
   "*First seen: ... | Source: ...*", italic description, "### Dealbreaker Check"
   with the Developing Hardware / Is a Startup answers and reasons, then the
   discovery summary prose. Do this immediately when the threshold is hit, don't
   wait until the end — this is the checkpoint that protects progress on a long run.

4. If the run ends with a partial batch (fewer than 10 passes since the last
   checkpoint), write it to reports/manual_report_batch_<N>.md anyway (final
   partial batch), clearly labeled as partial.

5. At the very end, print a plain-text summary: companies extracted, skipped as
   duplicates, failed (broken down by which stage/exclusion), and passed.

Do not touch reports/report_*.html files or run gmail.py/send_report — those are the
automated pipeline's output; this run only writes the manual_report_batch_*.md files
and scout.db rows.
```
