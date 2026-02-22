# Startup Scout

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
cd startup-scout
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