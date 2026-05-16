# Automation

Two tools running on a local always-on Linux desktop (Ubuntu 24.04) under the `logan` user.

---

## 1. Startup Scout

Automated pipeline that monitors forwarded newsletters, evaluates mentioned startups against a set of criteria, and emails a weekly report every day at 7am.

---

## How It Works

1. You forward newsletters to `loganhartford.scout@gmail.com`
2. `ingest.py` runs hourly via systemd timer, reads unread emails, and runs each through the evaluation pipeline
3. Companies are checked against dealbreakers, then if they pass, a lightweight dealbreaker report is saved
4. Results are stored in `scout.db` (SQLite) with deduplication — the same company is never evaluated twice
5. `report.py` runs daily at 7am, emails a report of all new companies since the last report, with Telegram links to generate full reports on demand

---

## Project Structure

```
automation/
├── evaluate.py           # LLM pipeline: extract → dealbreakers → report
├── ingest.py             # Reads unread emails and triggers evaluate.py
├── report.py             # Generates and emails the weekly HTML report
├── scout_bot.py          # Telegram bot for interactive company evaluation
├── db.py                 # All SQLite logic (read/write/dedup)
├── gmail.py              # Gmail API auth, email reading, email sending
├── reset_reports.py      # Dev utility: resets notified_date so report re-sends
├── scout.db              # SQLite database
├── credentials.json      # Google OAuth credentials (never commit)
├── token.pickle          # Cached Gmail auth token (never commit)
├── calendar_token.pickle # Cached Calendar auth token (never commit)
├── .env                  # API keys (never commit)
└── logs/
    ├── ingest.log
    ├── report.log
    ├── bot.log
    └── schedule.log
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
WEBHOOK_SECRET=
```

### 3. Google OAuth credentials

- Go to console.cloud.google.com → select the `startup-scout` project
- APIs & Services → Credentials → download the OAuth client JSON → save as `credentials.json`
- Run `python3 ingest.py` (browser required) to generate `token.pickle`
- Run `python3 -c "from calendar_api import get_calendar_service; get_calendar_service()"` to generate `calendar_token.pickle`

---

## Running Manually

```bash
source venv/bin/activate

# Process a newsletter from a text file
python3 evaluate.py newsletters/strictlyvc1.txt

# Check for new emails and process them
python3 ingest.py

# Generate and send the report now
python3 report.py

# Reset report state for re-testing
python3 reset_reports.py
```

---

## Systemd Services

Unit files are at `~/.config/systemd/user/`. Linger is enabled so services run without an active login.

```bash
# Check status
systemctl --user status scout-ingest.timer scout-report.timer scout-bot scout-schedule

# Check timers
systemctl --user list-timers

# Restart a service
systemctl --user restart scout-bot
systemctl --user restart scout-schedule

# View logs
journalctl --user -u scout-bot -f
journalctl --user -u scout-schedule -n 50
```

---

## Changing the Evaluation Criteria

All prompts and evaluation logic live in `evaluate.py`:

- **`EXTRACTION_PROMPT`** — controls what counts as a startup worth extracting
- **`DEALBREAKER_PROMPT`** and the `check_dealbreakers` tool schema — the 5 pass/fail questions
- **`REPORT_PROMPT`** and the `generate_report` tool schema — the 6 analysis questions

To add or change a dealbreaker or report field, update both the prompt description and the corresponding entry in the tool `input_schema`. Then update `DEALBREAKER_LABELS` or `REPORT_LABELS` in `report.py` so it renders with a clean label.

---

## Adding a New Gmail Account

Each Gmail account used by the automation needs its own OAuth token file and a corresponding service function in `gmail.py`. The pattern is always the same:

### 1. Create the Gmail account

Create the account at accounts.google.com.

### 2. Add it as a test user in Google Cloud

Go to console.cloud.google.com → `startup-scout` project → **APIs & Services → OAuth consent screen → Test users** → add the new address.

> Skip this if the app is ever published (not in testing mode).

### 3. Add constants and a service function to `gmail.py`

```python
MY_NEW_TOKEN_FILE = "my_new_token.pickle"
MY_NEW_EMAIL = "loganhartford.new@gmail.com"

def get_my_new_service():
    creds = None
    if os.path.exists(MY_NEW_TOKEN_FILE):
        with open(MY_NEW_TOKEN_FILE, "rb") as f:
            creds = pickle.load(f)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(MY_NEW_TOKEN_FILE, "wb") as f:
            pickle.dump(creds, f)
    return build("gmail", "v1", credentials=creds)
```

### 4. Generate the token (requires browser)

```bash
source venv/bin/activate
python3 -c "from gmail import get_my_new_service; get_my_new_service()"
```

When the browser opens, sign in as the new Gmail account. The token file is written automatically.

### 5. Verify

```python
svc = get_my_new_service()
profile = svc.users().getProfile(userId="me").execute()
print(profile["emailAddress"])  # should match MY_NEW_EMAIL
```

### 6. Re-authenticate an expired or wrong-account token

Delete the token file and re-run step 4:

```bash
rm my_new_token.pickle
python3 -c "from gmail import get_my_new_service; get_my_new_service()"
```

---

## Troubleshooting

**Gmail auth stopped working** — delete `token.pickle` and run `python3 ingest.py` to re-authenticate (requires browser).

**Report shows no new companies** — either nothing passed the dealbreakers, or the report already ran and marked everything as notified. Run `python3 reset_reports.py` to reset.

**API errors in logs** — check Anthropic billing at console.anthropic.com.

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
└── templates/
    ├── book.html         # Booking page UI
    └── booked.html       # Confirmation page
```

### Networking

Served via **Cloudflare Tunnel** — no ports are open on the router. `cloudflared` runs as a system service and makes an outbound connection to Cloudflare. Traffic flow: `user → Cloudflare → tunnel → localhost:5001`.

```bash
sudo systemctl status cloudflared
sudo systemctl restart cloudflared
```

### Troubleshooting

**Booking page shows no slots** — Calendar token may have expired. Re-run the OAuth flow and replace `calendar_token.pickle`.

**Telegram bot not responding** — Check logs: `journalctl --user -u scout-schedule -n 50`. Confirm webhook: `https://api.telegram.org/bot<TOKEN>/getWebhookInfo`.

**Site unreachable** — Check tunnel: `sudo systemctl status cloudflared`. Check app: `systemctl --user status scout-schedule`. Check locally: `curl http://127.0.0.1:5001/`.
