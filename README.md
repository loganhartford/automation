# Automation

Three tools running on a local always-on Linux desktop (Ubuntu 24.04) under the `logan` user at `/home/logan/automation`.

---

## 1. Startup Scout

Automated pipeline that monitors forwarded tech newsletters, evaluates startups against hardware-company criteria, and emails a daily report.

### How It Works

1. Forward newsletters to `loganhartford.scout@gmail.com`
2. `ingest.py` runs hourly via systemd timer — reads unread emails, runs each through the evaluation pipeline
3. Each company is web-searched, pre-filtered, checked against 5 pass/fail dealbreakers, and (if passing) gets a 13-field analysis report
4. Results land in `scout.db` (SQLite) with case-insensitive deduplication — a company is never evaluated twice
5. `report.py` runs daily at 7am, emails a styled HTML report of all new passing companies; full per-company reports are on-demand via Telegram

### Project Structure

```
evaluate.py        # LLM pipeline: extract → discover → prefilter → dealbreakers → report
ingest.py          # Reads unread scout emails and triggers evaluate.py
report.py          # Generates and emails the weekly HTML report
scout_bot.py       # Telegram bot for interactive company evaluation
rerun.py           # Batch re-evaluate companies already in the DB
db.py              # All SQLite logic (read/write/dedup)
gmail.py           # Gmail API auth, reading emails, sending HTML emails
reset_reports.py   # Dev utility: resets notified_date so report re-sends
scout.db           # SQLite database (git-ignored)
```

### Running Manually

```bash
source venv/bin/activate

# Evaluate a newsletter from a saved text file
python3 evaluate.py newsletters/strictlyvc1.txt

# Look up a specific company by name
python3 evaluate.py --company "Ulysses"

# Process all unread scout emails
python3 ingest.py

# Generate and send the report now
python3 report.py

# Re-evaluate recent companies (with cost guardrails)
python3 rerun.py 25

# Reset report state for re-testing
python3 reset_reports.py
```

### Changing Evaluation Criteria

All prompts and logic live in `evaluate.py`:

- **`EXTRACTION_PROMPT`** — controls what counts as a startup worth extracting
- **`DEALBREAKER_CRITERIA`** dict — the 5 sequential pass/fail questions and their prompts
- **`REPORT_PROMPT`** and the `generate_report` tool schema — the 13 analysis fields

To add or remove a dealbreaker or report field, update the prompt and corresponding tool schema entry. Then update `DEALBREAKER_LABELS` or `REPORT_LABELS` in `report.py` so it renders cleanly.

---

## 2. Scheduler

Telegram-based calendar management. External attendees book via Google Calendar's native appointment scheduling. Logan manages bookings entirely through a Telegram bot powered by a Claude agent.

### How It Works

1. External attendees visit Logan's Google Calendar appointment page and pick a slot
2. Google Calendar sends them an invite automatically
3. `calendar_notify.py` runs every 5 minutes — detects new bookings and sends a Telegram notification
4. Logan messages the scheduling bot to reschedule, cancel, or email the participant; the Claude agent handles it

### Project Structure

```
telegram_bot.py      # Claude agent for calendar control via Telegram (4 tools)
calendar_api.py      # Google Calendar helpers: free slots, create/reschedule/cancel
calendar_notify.py   # Periodic new-booking notifier (runs every 5 min via timer)
telegram_notifier.py # One-way Telegram notification helper
```

### Running

```bash
systemctl --user restart scout-schedule         # restart the Telegram bot
systemctl --user status scout-calendar-notify.timer
```

---

## 3. Email Triage

Automated Outlook inbox management. Uses a local Ollama LLM (Qwen 3 8B) to classify emails as `urgent`, `keep`, `unsubscribe`, or `trash` — then executes actions (unsubscribe, trash) and surfaces urgent items via Telegram.

### How It Works

1. `triage.py` fetches unread Outlook emails via Microsoft Graph API
2. Each email passes through hard-coded rules first (always-urgent senders, always-keep senders)
3. Remaining emails are classified by Ollama with 3 calls and 2/3 consensus
4. `unsubscribe` and `trash` actions are executed automatically; `urgent` emails are sent to Telegram
5. `triage_bot.py` gives a Telegram Claude agent tools to reply, trash, or dismiss urgent emails

### Project Structure

```
triage.py       # Email classification with Ollama consensus, unsubscribe/trash execution
triage_bot.py   # Telegram bot for managing urgent pending emails
outlook.py      # Microsoft Graph API wrapper (auth, read, unsubscribe, reply, trash)
```

### Running

```bash
python3 triage.py              # run triage and write a review file
python3 triage.py --dry-run    # classify only, no actions
python3 triage.py --execute-review logs/triage_review_2026-05-15_1700.txt
```

---

## Setup

### 1. Create virtual environment

```bash
cd /home/logan/automation
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
mkdir -p logs
```

### 2. Environment variables

Create a `.env` file:

```
ANTHROPIC_API_KEY=

TELEGRAM_SCOUT_BOT_TOKEN=
TELEGRAM_SCOUT_CHAT_ID=
TELEGRAM_SCOUT_BOT_USERNAME=

TELEGRAM_SCHEDULER_BOT_TOKEN=
TELEGRAM_SCHEDULER_CHAT_ID=

TELEGRAM_TRIAGE_BOT_TOKEN=
TELEGRAM_TRIAGE_CHAT_ID=

WEBHOOK_SECRET=
```

### 3. Google OAuth (Gmail + Calendar)

- Go to console.cloud.google.com → select the `startup-scout` project
- APIs & Services → Credentials → download the OAuth client JSON → save as `credentials.json`
- Generate `token.pickle` (Gmail Scout): `python3 ingest.py` (browser required)
- Generate `calendar_token.pickle`: `python3 -c "from calendar_api import get_calendar_service; get_calendar_service()"`
- Generate `calendar_gmail_token.pickle` (for sending from `loganhartford.calendar@gmail.com`): `python3 -c "from gmail import get_calendar_gmail_service; get_calendar_gmail_service()"`

### 4. Outlook OAuth

Run `python3 outlook.py` — it triggers a device-flow auth prompt, opens a browser, and writes `outlook_token.pickle`.

### Adding a New Gmail Account

Each Gmail account needs its own token file and service function in `gmail.py`. The pattern is identical to the existing `get_service()` / `get_calendar_gmail_service()` functions — copy one, update the token filename and email constant, then run it once to generate the token.

Add the new Gmail to the **Test users** list in the OAuth consent screen (console.cloud.google.com → `startup-scout` → APIs & Services → OAuth consent screen) or the auth flow will reject it.

---

## Systemd Services

Unit files at `~/.config/systemd/user/`. Linger is enabled so services run without an active login.

| Timer / Service | Trigger | Script |
|---|---|---|
| `scout-ingest.timer` | hourly | `ingest.py` |
| `scout-report.timer` | daily 7am | `report.py` |
| `scout-bot.service` | always-on | `scout_bot.py` |
| `scout-schedule.service` | always-on | `telegram_bot.py` |
| `scout-calendar-notify.timer` | every 5 min | `calendar_notify.py` |

```bash
# Check status
systemctl --user status scout-ingest.timer scout-report.timer scout-bot scout-schedule

# List timers
systemctl --user list-timers

# Restart
systemctl --user restart scout-bot
systemctl --user restart scout-schedule

# Logs
journalctl --user -u scout-bot -f
journalctl --user -u scout-schedule -n 50
```

Logs also go to `logs/`: `ingest.log`, `report.log`, `bot.log`, `schedule.log`, `triage.log`, `triage_bot.log`.

**Networking:** UFW is active, only port 22 open. All Telegram bots use long-polling (no inbound ports). Cloudflare Tunnel handles any inbound web traffic (outbound connection from `cloudflared`).

---

## Troubleshooting

**Gmail auth stopped working** — delete `token.pickle` and run `python3 ingest.py` to re-authenticate (browser required).

**Calendar token expired** — delete `calendar_token.pickle` and re-run the OAuth flow.

**Outlook token expired** — delete `outlook_token.pickle` and run `python3 outlook.py`.

**Report shows no new companies** — nothing passed dealbreakers, or report already ran. Run `python3 reset_reports.py` to reset.

**Telegram bot not responding** — these bots use long-polling (not webhooks). Check `journalctl --user -u scout-bot -n 50` or `journalctl --user -u scout-schedule -n 50`.

**API errors in logs** — check Anthropic billing at console.anthropic.com. The pipeline raises `CreditExhaustedError` on HTTP 402 and sends an alert email.

**Ollama not running** — triage.py calls `check_ollama_health()` at startup. Start Ollama: `ollama serve`, then ensure the Qwen model is pulled: `ollama pull qwen3:8b`.
