# Automation

Two tools running on a shared DigitalOcean droplet (Ubuntu 24.04, 1GB RAM, `167.172.204.113`) under the `scout` user.

---

## 1. Startup Scout

Automated pipeline that monitors forwarded newsletters, evaluates mentioned startups against a set of criteria, and emails a weekly report every Saturday at 7am.

---

## How It Works

1. You forward newsletters to `loganhartford.scout@gmail.com`
2. `ingest.py` runs hourly via cron, reads unread emails, and runs each through the evaluation pipeline
3. Companies are checked against dealbreakers, then if they pass, a full report is generated
4. Results are stored in `scout.db` (SQLite) with deduplication — the same company is never evaluated twice
5. `report.py` runs every Saturday at 7am, emails a report of all new companies since the last report

---

## Project Structure

```
startup-scout/
├── evaluate.py       # LLM pipeline: extract → dealbreakers → report
├── ingest.py         # Reads unread emails and triggers evaluate.py
├── report.py         # Generates and emails the weekly HTML report
├── db.py             # All SQLite logic (read/write/dedup)
├── gmail.py          # Gmail API auth, email reading, email sending
├── reset_reports.py  # Dev utility: resets notified_date so report re-sends
├── scout.db          # SQLite database (auto-created on first run)
├── credentials.json  # Google OAuth credentials (never commit)
├── token.pickle      # Cached Gmail auth token (never commit)
├── .env              # API keys (never commit)
└── logs/
    ├── ingest.log
    └── report.log
```

---

## Setup

### 1. Clone and create virtual environment

```bash
cd automation
python3 -m venv venv
source venv/bin/activate
pip install anthropic python-dotenv google-auth google-auth-oauthlib google-api-python-client markdown
```

### 2. Environment variables

Create a `.env` file in the project root:

```
ANTHROPIC_API_KEY=your-key-here
```

Get your API key from console.anthropic.com. You'll need a funded account (billing → add credits).

### 3. Gmail API credentials

- Go to console.cloud.google.com → select the `startup-scout` project
- APIs & Services → Credentials → download the OAuth client JSON
- Save it as `credentials.json` in the project root
- APIs & Services → OAuth consent screen → Test Users → confirm `loganhartford.scout@gmail.com` is listed

### 4. Authenticate Gmail (first run only)

```bash
source venv/bin/activate
python3 ingest.py
```

A browser window will open asking you to sign in with the scout Gmail account. After approving, a `token.pickle` file is created and future runs are silent.

### 5. Create logs directory

```bash
mkdir -p logs
```

### 6. Cron jobs

Run `crontab -e` and add:

```
0 * * * * cd /Users/loganhartford/Documents/GitHub/automation && /Users/loganhartford/Documents/GitHub/automation/venv/bin/python3 ingest.py >> logs/ingest.log 2>&1

0 7 * * 6 cd /Users/loganhartford/Documents/GitHub/automation && /Users/loganhartford/Documents/GitHub/automation/venv/bin/python3 report.py >> logs/report.log 2>&1
```

---

## Running Manually

```bash
source venv/bin/activate

# Process a newsletter from a text file
python3 evaluate.py my_newsletter.txt

# Check for new emails and process them
python3 ingest.py

# Generate and send the weekly report now
python3 report.py

# Reset report state (re-send last report to a new address, testing, etc.)
python3 reset_reports.py
```

---

## Changing the Evaluation Criteria

All prompts and evaluation logic live in `evaluate.py`:

- **`EXTRACTION_PROMPT`** — controls what counts as a startup worth extracting
- **`DEALBREAKER_PROMPT`** and the `check_dealbreakers` tool schema — the 5 pass/fail questions
- **`REPORT_PROMPT`** and the `generate_report` tool schema — the 6 analysis questions

To add or change a dealbreaker or report field, update both the prompt description and the corresponding entry in the tool `input_schema`. Then update `DEALBREAKER_LABELS` or `REPORT_LABELS` in `report.py` so it renders with a clean label.

---

## Troubleshooting

**Gmail auth stopped working** — delete `token.pickle` and run `python3 ingest.py` to re-authenticate.

**Cron jobs not running** — check `logs/ingest.log`. If empty after an hour, verify cron is enabled: `crontab -l`. On macOS you may need to grant Terminal full disk access under System Settings → Privacy & Security.

**Report shows no new companies** — either nothing passed the dealbreakers this week, or the report already ran and marked everything as notified. Run `python3 reset_reports.py` to reset and re-send.

**API errors in logs** — check your Anthropic billing at console.anthropic.com. Credits may have run out.

---

## 2. Scheduler (meet.lhartford.com)

Self-hosted Calendly replacement. Visitors pick a 30-minute slot from real availability (Mon–Fri 10am–3pm PT, checked against all Google calendars), and a Google Calendar event is created automatically with an invite sent to the booker.

### How It Works

1. Someone visits `https://meet.lhartford.com` and picks an available slot
2. They fill in their name, email, and optional notes
3. A Google Calendar event (`Logan Hartford <> Name`) is created with a 5-min popup reminder on Logan's side only
4. The booker receives a calendar invite; Logan gets a Telegram notification
5. Logan can reply to the Telegram bot to reschedule, cancel, or email the participant — powered by a Claude agent

### Project Structure

```
├── schedule.py           # Flask app: booking page, form handler, Telegram webhook
├── calendar_api.py       # Google Calendar: free slots, create/reschedule/cancel bookings
├── telegram_bot.py       # Claude agent for two-way Telegram control
├── telegram_notifier.py  # One-way Telegram notification on new bookings
├── templates/
│   ├── book.html         # Booking page UI
│   └── booked.html       # Confirmation page
└── calendar_token.pickle # Google Calendar OAuth token (never commit)
```

### Required Secrets

Add to `.env`:
```
ANTHROPIC_API_KEY=...
TELEGRAM_SCHEDULER_BOT_TOKEN=...
TELEGRAM_SCHEDULER_CHAT_ID=...
WEBHOOK_SECRET=...
```

`calendar_token.pickle` — generated locally via OAuth flow, then scp'd to server:
```bash
python3 -c "from calendar_api import get_calendar_service; get_calendar_service()"
scp calendar_token.pickle scout@167.172.204.113:~/automation/
```

### Deployment

Runs as a systemd user service (`scout-schedule`) via gunicorn on port 5001, proxied through Nginx with Let's Encrypt HTTPS.

```bash
systemctl --user restart scout-schedule
journalctl --user -u scout-schedule -n 50  # logs
```

### Telegram Webhook Registration (run once after token changes)

```bash
python3 -c "
import os, requests
from dotenv import load_dotenv
load_dotenv()
r = requests.post(
    f'https://api.telegram.org/bot{os.getenv(\"TELEGRAM_SCHEDULER_BOT_TOKEN\")}/setWebhook',
    data={'url': 'https://meet.lhartford.com/telegram-webhook', 'secret_token': os.getenv('WEBHOOK_SECRET')}
)
print(r.json())
"
```

### Troubleshooting

**Booking page shows no slots** — Calendar token may have expired. Re-run the OAuth flow locally and scp the new `calendar_token.pickle` to the server.

**Telegram bot not responding** — Check logs: `journalctl --user -u scout-schedule -n 50`. Confirm webhook is registered: `https://api.telegram.org/bot<TOKEN>/getWebhookInfo`.

**Google Calendar API disabled** — Enable it at console.cloud.google.com → startup-scout project → APIs & Services.
