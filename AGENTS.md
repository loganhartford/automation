# Repository Guidelines

## Project Structure & Module Organization

This repository contains Python automation for two local services. Startup Scout modules live at the repo root: `evaluate.py` runs the LLM pipeline, `ingest.py` reads Gmail newsletters, `db.py` owns SQLite access, `report.py` generates reports, and `scout_bot.py` provides Telegram interaction. Scheduler code is also top-level: `schedule.py` is the Flask app, `calendar_api.py` handles Google Calendar, and `telegram_bot.py` / `telegram_notifier.py` handle Telegram. HTML templates are in `templates/`, sample newsletters are in `newsletters/`, generated reports are in `reports/`, and runtime logs are expected in `logs/`.

## Build, Test, and Development Commands

Create and activate the local environment before running scripts:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
mkdir -p logs
```

Common manual commands:

```bash
python3 evaluate.py newsletters/strictlyvc1.txt  # evaluate a saved newsletter
python3 ingest.py                                # process unread Gmail messages
python3 report.py                                # generate and email the report
python3 reset_reports.py                         # reset notified companies for retesting
python3 schedule.py                              # run the booking app on localhost:5001
```

For deployed services, use `systemctl --user status scout-ingest.timer scout-report.timer scout-bot scout-schedule` and `journalctl --user -u scout-schedule -n 50`.

## Coding Style & Naming Conventions

Use Python 3 with 4-space indentation. Keep modules script-friendly and use leading underscores for private helpers, as in `_group_slots`. Constants are uppercase (`DEALBREAKER_ORDER`), functions and variables are `snake_case`, and files are lowercase with underscores. No formatter or linter config is checked in, so match nearby code.

## Testing Guidelines

There is no formal test suite yet. Before changing pipeline behavior, run the relevant script against `newsletters/*.txt` and inspect database/report output. Before changing the scheduler, run `python3 schedule.py` and test booking locally. Add tests under `tests/` for reusable logic; name files `test_<module>.py`.

## Commit & Pull Request Guidelines

Recent commits use short, direct summaries such as `fix research extraction to collect all text blocks and handle max_tokens` and `add rerun script to re-evaluate companies from db`. Keep commit subjects concise, imperative, and specific. Pull requests should describe the workflow changed, list manual verification commands, mention required environment variables or tokens, and include screenshots for template/UI changes.

## Security & Configuration Tips

Do not commit `.env`, `credentials.json`, `token.pickle`, `calendar_token.pickle`, `scout.db`, or files from `logs/`. Rotate API keys or OAuth tokens if they are exposed. Keep production-only service and Cloudflare tunnel changes documented in `README.md`.
