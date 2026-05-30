# Repository Guidelines

Guidelines for working in this repo — read CLAUDE.md first for architecture and design context.

## Project Structure

All source files are at the repo root. No subdirectory structure for code.

```
# Startup Scout
evaluate.py          # LLM pipeline: extract → discover → prefilter → dealbreakers → report
ingest.py            # Reads unread scout emails, calls evaluate.process_newsletter()
report.py            # Queries DB, renders HTML, emails report, marks companies notified
scout_bot.py         # Telegram bot for on-demand company evaluation
rerun.py             # Batch re-evaluate companies already in DB
db.py                # SQLite wrapper (single companies table, all read/write)
gmail.py             # Gmail API: auth, read emails, send plain/HTML emails
reset_reports.py     # Dev utility: clears notified_date so report re-sends

# Scheduler
telegram_bot.py      # Claude agent for calendar control (4 tools, long-polling)
calendar_api.py      # Google Calendar: free slots, create/reschedule/cancel
calendar_notify.py   # Oneshot: detect new bookings, notify via Telegram
telegram_notifier.py # One-way Telegram notification helper

# Email Triage
triage.py            # Email classifier (Ollama 3-call consensus), unsubscribe/trash
triage_bot.py        # Telegram Claude agent for managing urgent emails
outlook.py           # Microsoft Graph API: auth, read, unsubscribe, reply, trash

# Data / runtime
scout.db             # SQLite database (git-ignored)
logs/                # ingest.log, report.log, bot.log, schedule.log, triage.log, triage_bot.log
newsletters/         # Sample newsletter text files for manual testing
reports/             # Generated report HTML files (if saved)
```

**Does not exist (don't reference):** `schedule.py`, `templates/` — these appeared in earlier docs but were never implemented. The Scheduler uses Google Calendar's native booking page; there is no custom Flask booking app.

## Build, Test, and Development

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
mkdir -p logs
```

Common manual invocations:

```bash
python3 evaluate.py newsletters/strictlyvc1.txt  # evaluate a saved newsletter
python3 evaluate.py --company "Acme"             # look up specific company
python3 ingest.py                                # process unread Gmail messages
python3 report.py                                # generate and email the report
python3 rerun.py 25                              # re-evaluate 25 most recent companies
python3 reset_reports.py                         # reset notified companies for retesting
python3 triage.py --dry-run                      # classify emails, no actions taken
python3 scout_bot.py                             # run scout Telegram bot (long-polling)
python3 telegram_bot.py                          # run scheduler Telegram bot (long-polling)
```

For deployed services:

```bash
systemctl --user status scout-ingest.timer scout-report.timer scout-bot scout-schedule
journalctl --user -u scout-bot -f
journalctl --user -u scout-schedule -n 50
```

**No test suite exists yet.** Before changing pipeline behavior, run the relevant script against `newsletters/*.txt` and inspect DB/report output. Before changing a Telegram bot, run it directly and interact with it. If you add reusable logic, add tests under `tests/test_<module>.py`.

## Coding Style

- Python 3, 4-space indentation, no formatter/linter config checked in — match nearby code
- `snake_case` for functions and variables; `UPPER_CASE` for constants; `_leading_underscore` for private helpers
- Files are lowercase with underscores
- No comments unless the WHY is non-obvious (a workaround, a subtle invariant, a constraint not visible in the code)

## Commit Guidelines

Keep subjects concise, imperative, and specific — describe the behavior change, not the file touched. Examples from recent history:

```
email agent done, scout works
fix research extraction to collect all text blocks and handle max_tokens
add rerun script to re-evaluate companies from db
```

Pull requests should describe: what workflow changed, how to manually verify it, which environment variables or tokens are required, and screenshots for any UI changes.

## Security

**Never commit:**
- `.env`
- `credentials.json`
- `token.pickle`, `calendar_token.pickle`, `calendar_gmail_token.pickle`, `outlook_token.pickle`
- `scout.db`
- Anything under `logs/`

These are all in `.gitignore`. If any token or key is exposed, rotate it immediately.

**Triage bot safety:** `triage_bot.py`'s `send_reply` tool must never send without explicit user approval. The agent prompt enforces this — don't weaken or remove that constraint.

**Outlook actions are mostly reversible:** `move_to_trash()` moves to Deleted Items (recoverable). Unsubscribes are not reversible. Treat unsubscribe execution paths with care when modifying `triage.py`.
