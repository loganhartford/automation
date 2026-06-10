# Automation

Four tools running on a local always-on Linux desktop (Ubuntu 24.04) under the `logan` user at `/home/logan/automation`. All services are systemd user units with linger enabled (`loginctl enable-linger logan`) so they survive logout.

---

## Applications

### 1. Startup Scout

Automated pipeline that monitors forwarded tech newsletters, evaluates startups against hardware-company criteria, and emails a daily HTML report. Evaluation is from the perspective of a **senior firmware/embedded engineer considering companies to join** — not an investor. Target: hardware startup, 30–100 people, pre-Series B, novel technology.

**Data flow:**
```
Gmail inbox → ingest.py → evaluate.py → scout.db → report.py → email
                                              ↑
                                        scout_bot.py (on-demand)
```

**Files:**
```
evaluate.py        # LLM pipeline: extract → discover → prefilter → dealbreakers → report
ingest.py          # Reads unread scout emails, calls evaluate.process_newsletter()
report.py          # Queries DB, renders HTML, emails report, marks companies notified
scout_bot.py       # Telegram bot for on-demand company evaluation
rerun.py           # Batch re-evaluate companies already in DB (with cost guardrails)
db.py              # SQLite wrapper (single companies table, all read/write)
gmail.py           # Gmail API: auth, read emails, send plain/HTML emails
reset_reports.py   # Dev utility: clears notified_date so report re-sends
scout.db           # SQLite database (git-ignored)
```

**Evaluation pipeline stages (in order):**

1. **Extraction** — `claude-haiku-4-5-20251001`, forced tool use. Extracts `{name, description}` dicts. `max_tokens` must be ≥ 4096 — lower values silently truncate mid-JSON.
2. **Discovery** — Agentic web search loop, up to 2 searches, builds a 300–400 word factual summary. Continues on `pause_turn` or `max_tokens`.
3. **Pre-filter** — `claude-haiku-4-5-20251001`. Cheap binary check: obviously non-hardware? Returns `"no"` or `"unknown"` only. `"no"` skips immediately.
4. **Dealbreakers** — 2 criteria evaluated sequentially (`developing_hardware` using Sonnet, `is_startup` using Haiku), each with its own mini search loop. Fails immediately on `"no"` or `"unknown"`.
5. **Report generation** — `claude-sonnet-4-6`, 13-field analysis (includes `billion_dollar_potential`, `growing_quickly`, `solves_real_problem` as report dimensions rather than gates). Generated on-demand only via Telegram (`/full <id>` or the email deep link) — never during automated ingestion.

**Running manually:**
```bash
python3 evaluate.py newsletters/strictlyvc1.txt   # evaluate a newsletter file
python3 evaluate.py --company "Acme"              # look up specific company
python3 ingest.py                                 # process unread Gmail messages
python3 report.py                                 # generate and email the report
python3 rerun.py 25                               # re-evaluate 25 most recent companies
python3 reset_reports.py                          # reset notified companies for retesting
```

**Changing criteria:** All prompts live in `evaluate.py` — `EXTRACTION_PROMPT`, `DEALBREAKER_CRITERIA` dict, `REPORT_PROMPT`, and the `generate_report` tool schema. To add or remove a field, update the prompt and tool schema, then update `DEALBREAKER_LABELS` / `REPORT_LABELS` in `report.py`.

---

### 2. Scheduler

Telegram-based Google Calendar management. External attendees book via Google Calendar's native appointment page. Logan manages bookings via a Claude agent in Telegram.

**How it works:**
1. External attendees visit the Google Calendar appointment page and pick a slot
2. `calendar_notify.py` runs every 5 min — detects new bookings with external attendees and sends a Telegram notification (first run sets baseline only, no noise)
3. Logan messages the scheduling bot to reschedule, cancel, or email a participant

**Files:**
```
telegram_bot.py      # Claude agent (claude-opus-4-6) for calendar control — 4 tools
calendar_api.py      # Google Calendar: free slots, create/reschedule/cancel
calendar_notify.py   # Detects new bookings, notifies via Telegram
telegram_notifier.py # One-way Telegram notification helper
```

State: `logs/calendar_notify_state.json` — tracks `last_checked` and `notified_ids`.

Settings: America/Vancouver (PT), Mon–Fri 10am–3pm availability, 30-min slots. All calendar operations use `sendUpdates="all"` so Google handles invite emails automatically.

**Running:**
```bash
systemctl --user restart scout-schedule
systemctl --user status scout-calendar-notify.timer
```

---

### 3. Email Triage

Automated Outlook inbox management. Uses a local Ollama LLM (Qwen 3 8B) to classify emails, surfaces time-sensitive items via Telegram, and stages trash/unsubscribe actions with a 36-hour delay before execution.

**Decision flow per email:**
1. `pre_filter()` — hard-coded rules bypass Ollama (always-time-sensitive: replies to own emails, government deadlines; always-keep: family, scout inbox)
2. `ask_ollama()` — 3 independent calls at temperature 0.4, 2/3 consensus required; no consensus → default `"keep"` (safe)
3. Actions:
   - `time_sensitive` → immediate Telegram notification + appended to `logs/triage_urgent_pending.json` for bot access
   - `keep` → mark read, no further action
   - `trash` / `unsubscribe` → staged in `logs/triage_staged_actions.json` for 36 hours, then executed on the next hourly run after the delay expires
4. **Staged execution** (each hourly run): `_flush_staged()` executes any staged action older than 36 hours — unsubscribe attempts one-click POST → URL GET → mailto, then archives the email; trash archives directly. After 7 days in Archive, `_purge_stale_archive()` moves to Deleted Items (recoverable from there).

Runs hourly via systemd timer; `seen_ids` persisted in `logs/triage_seen.json` prevents re-processing across runs.

**Files:**
```
triage.py       # Email classifier (Ollama consensus), staging/flush logic
triage_bot.py   # Telegram Claude agent for managing time-sensitive pending emails (4 tools)
outlook.py      # Microsoft Graph API: auth, read, archive, unsubscribe, reply, trash
```

**State files:**
```
logs/triage_seen.json              # processed email IDs (prevents re-triage)
logs/triage_urgent_pending.json    # time-sensitive emails awaiting bot action
logs/triage_staged_actions.json    # pending trash/unsubscribe (36h staging queue)
logs/triage_archive_log.json       # archive timestamps for 7-day deletion tracking
logs/triage_review_<date>_<time>.txt  # dry-run output for prompt tuning
```

**Safety invariant:** `triage_bot.py`'s `send_reply` tool never sends without explicit user approval. The agent prompt enforces this — don't weaken it.

**Running:**
```bash
python3 triage.py              # classify and execute/stage actions
python3 triage.py --dry-run    # classify only, no actions, writes review file
python3 triage.py --execute-review logs/triage_review_2026-05-15_1700.txt
```

---

### 4. YNAB Finance Bot

Telegram bot for querying YNAB budgets, generating monthly financial reports, and autonomously categorizing uncategorized transactions. Covers two budgets: `america` (USD) and `canada` (CAD).

**Files:**
```
ynab_bot.py          # Telegram Claude agent for budget queries + keyword dispatchers
ynab_report.py       # Monthly financial report — dual Ollama + Claude Opus backends
ynab_categorizer.py  # Autonomous transaction categorizer using Ollama consensus
```

**ynab_bot.py** — Claude agent (`claude-opus-4-6`) with three YNAB tools: `get_spending_summary`, `list_transactions`, `get_budget_vs_actual`. Before hitting the agent, keyword detection dispatches to:
- Report keywords ("monthly report", "financial report", etc.) → `generate_both_reports()`
- Categorize keywords ("categorize", "uncategorized transactions", etc.) → `categorize_transactions()`

**ynab_report.py** — Generates a monthly report in 4 Ollama calls (overview, overspending, trends, net worth/actions). `generate_both_reports()` runs both Ollama and Claude Opus in sequence and returns both. The Claude version appends a cost line (token counts + USD). Trigger: `python3 ynab_report.py 2026-05` or via bot.

**ynab_categorizer.py** — Finds uncategorized spending (skips transfers, balance adjustments, credit card payments), classifies each transaction 3 times with Ollama at temperature 0.3, and applies high-confidence (3/3) results back to YNAB via `PATCH /budgets/{id}/transactions`. Medium-confidence (2/3) and no-consensus results are logged but not applied.

State: `logs/ynab_categorizer_state.json` — processed transaction IDs, pruned after 30 days. Activity log: `logs/ynab_categorizer_log.json` — all outcomes, pruned after 60 days. Weekly summary (Monday 8am) reports categorization counts via Telegram.

**Running:**
```bash
python3 ynab_report.py              # generate both reports for last month
python3 ynab_report.py 2026-05      # specific month
python3 ynab_categorizer.py         # categorize both budgets (7-day lookback)
python3 ynab_categorizer.py america # one budget only
python3 ynab_categorizer.py --weekly # send weekly summary via Telegram
```

---

## Architecture Notes

### LLM patterns

**Forced tool use** — All structured Claude calls use `tool_choice={"type": "tool", "name": "<tool>"}`. This guarantees schema-conformant output. Never ask Claude to return raw JSON — always use a tool.

**Model selection:**

| Task | Model | Reason |
|------|-------|--------|
| Extraction, pre-filter, `is_startup` dealbreaker | `claude-haiku-4-5-20251001` | High volume, cheap, binary output |
| `developing_hardware` dealbreaker | `claude-sonnet-4-6` | Most critical judgment — hardware vs. not |
| Report generation (13 fields) | `claude-sonnet-4-6` | Nuance matters, called once per company |
| Agentic loops (Scout bot, Scheduler, YNAB bot) | `claude-opus-4-6` | Complex reasoning, conversation context |
| Local classification (triage, categorizer) | Ollama `qwen3:8b` | Free, offline, 3-call consensus |

**Retry and credit handling** — `call_with_retry()` in `evaluate.py`: exponential backoff (10s → 20s → 40s → 80s) on HTTP 529, max 4 retries. HTTP 402 raises `CreditExhaustedError` — never retry a 402.

**Ollama consensus** — 3 independent calls, require 2/3 agreement. No consensus → default to safe action (`"keep"` in triage, no YNAB write in categorizer). Compensates for local LLM nondeterminism.

### Known gotchas

- **`max_tokens` on extraction calls** — must be ≥ 4096. Lower values silently truncate mid-JSON with no error.
- **`Markdown` instantiation** — `md = Markdown(); md.convert(text)` works. `Markdown.convert(text)` does not.
- **DB cost column** — `init_db()` runs `ALTER TABLE ADD COLUMN cost REAL` automatically if missing. Don't add it manually.
- **Separate OAuth tokens** — three pickle files: `token.pickle` (Gmail Scout), `calendar_token.pickle` (Calendar), `calendar_gmail_token.pickle` (Gmail Calendar). Not interchangeable.
- **Triage seen IDs** — `triage_seen.json` persists across runs. Delete it to reprocess emails when testing.
- **Long-polling only** — all Telegram bots deregister webhooks at startup. There are no inbound webhook URLs.
- **YNAB milliunits** — all amounts are milliunits (divide by 1000). Outflows are negative.

---

## Setup

### 1. Virtual environment

```bash
cd /home/logan/automation
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
mkdir -p logs
```

### 2. Environment variables

Create `.env`:

```
ANTHROPIC_API_KEY=

# Startup Scout
TELEGRAM_SCOUT_BOT_TOKEN=
TELEGRAM_SCOUT_CHAT_ID=
TELEGRAM_SCOUT_BOT_USERNAME=

# Scheduler
TELEGRAM_SCHEDULER_BOT_TOKEN=
TELEGRAM_SCHEDULER_CHAT_ID=

# Email Triage
OUTLOOK_CLIENT_ID=
TELEGRAM_TRIAGE_BOT_TOKEN=
TELEGRAM_TRIAGE_CHAT_ID=

# YNAB
YNAB_TOKEN=
TELEGRAM_YNAB_BOT_TOKEN=
TELEGRAM_YNAB_CHAT_ID=

WEBHOOK_SECRET=
```

### 3. Google OAuth

- Go to console.cloud.google.com → `startup-scout` project → APIs & Services → Credentials → download OAuth client JSON → save as `credentials.json`
- `token.pickle` (Gmail Scout): `python3 ingest.py` — browser required
- `calendar_token.pickle`: `python3 -c "from calendar_api import get_calendar_service; get_calendar_service()"`
- `calendar_gmail_token.pickle`: `python3 -c "from gmail import get_calendar_gmail_service; get_calendar_gmail_service()"`

To add a new Gmail account: copy an existing service function in `gmail.py`, update the token filename and email constant, run once to generate the token, then add the account to **Test users** in the OAuth consent screen (console.cloud.google.com → OAuth consent screen).

### 4. Outlook OAuth

```bash
python3 outlook.py   # triggers device-flow auth, writes outlook_token.pickle
```

---

## Systemd Services

Unit files at `~/.config/systemd/user/`. All enabled — start automatically on boot.

| Unit | Trigger | Script |
|------|---------|--------|
| `scout-ingest.timer` | hourly | `ingest.py` |
| `scout-report.timer` | daily 7am | `report.py` |
| `scout-bot.service` | always-on | `scout_bot.py` |
| `scout-schedule.service` | always-on | `telegram_bot.py` |
| `scout-calendar-notify.timer` | every 5 min | `calendar_notify.py` |
| `triage.timer` | hourly | `triage.py` |
| `triage-bot.service` | always-on | `triage_bot.py` |
| `ynab-bot.service` | always-on | `ynab_bot.py` |
| `ynab-report.timer` | 1st of month, noon | `ynab_report.py` |
| `ynab-categorize.timer` | daily midnight | `ynab_categorizer.py` |
| `ynab-weekly-summary.timer` | Monday 8am | `ynab_categorizer.py --weekly` |

```bash
# Status
systemctl --user status scout-bot scout-schedule ynab-bot.service
systemctl --user list-timers

# Restart
systemctl --user restart scout-bot
systemctl --user restart ynab-bot

# Logs
journalctl --user -u scout-bot -f
journalctl --user -u ynab-bot -n 50
```

Logs also written to `logs/`: `ingest.log`, `report.log`, `bot.log`, `schedule.log`, `triage.log`, `triage_bot.log`, `ynab_bot.log`, `ynab_categorizer.log`.

**Networking:** UFW active, port 22 only. All Telegram bots use long-polling (no inbound ports). Cloudflare Tunnel handles any inbound web traffic.

---

## Coding Style

- Python 3, 4-space indentation, no formatter/linter config — match nearby code
- `snake_case` functions/variables, `UPPER_CASE` constants, `_leading_underscore` private helpers
- Files are lowercase with underscores
- No comments unless the WHY is non-obvious (workaround, subtle invariant, non-visible constraint)
- No test suite exists — before changing pipeline behavior, run the relevant script manually and inspect output

---

## Security

**Never commit:**
- `.env`
- `credentials.json`
- `token.pickle`, `calendar_token.pickle`, `calendar_gmail_token.pickle`, `outlook_token.pickle`
- `scout.db`
- Anything under `logs/`

All are in `.gitignore`. If any token is exposed, rotate it immediately.

---

## Troubleshooting

**Gmail auth stopped working** — delete `token.pickle`, run `python3 ingest.py` (browser required).

**Calendar token expired** — delete `calendar_token.pickle`, re-run the OAuth flow.

**Outlook token expired** — delete `outlook_token.pickle`, run `python3 outlook.py`.

**Report shows no new companies** — nothing passed dealbreakers, or report already ran. Run `python3 reset_reports.py` to reset.

**Telegram bot not responding** — all bots use long-polling. Check `journalctl --user -u <service> -n 50`.

**Anthropic API errors** — check billing at console.anthropic.com. Pipeline raises `CreditExhaustedError` on HTTP 402 and sends an alert email.

**Ollama not running** — `ollama serve` to start, `ollama pull qwen3:8b` to ensure the model is present.

**YNAB token expired** — generate a new Personal Access Token at app.ynab.com/settings/developer and update `YNAB_TOKEN` in `.env`, then restart `ynab-bot`.
