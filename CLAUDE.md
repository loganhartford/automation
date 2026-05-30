# CLAUDE.md

This file provides guidance to Claude Code when working with this repository. Read this before making changes — it documents non-obvious design decisions, model choices, and invariants that aren't obvious from reading the code alone.

## Project Overview

Three automation tools on a local Ubuntu 24.04 desktop (`/home/logan/automation`, user `logan`):

1. **Startup Scout** — monitors forwarded newsletters, evaluates hardware startups using Claude, emails a daily HTML report
2. **Scheduler** — Telegram-controlled Google Calendar management (Claude agent with 4 tools)
3. **Email Triage** — Outlook inbox automation using local Ollama LLM, with Telegram surfacing for urgent items

The Scout evaluation is from the perspective of a **senior firmware/embedded engineer considering companies to join** — not an investor. Target company: hardware startup, 30–100 people, pre-Series B, novel technology, strong timing.

---

## Deployment

Local always-on Linux desktop, `logan` user, `loginctl enable-linger logan` so systemd user services survive logout.

**Systemd units** (`~/.config/systemd/user/`):
- `scout-ingest.timer` → `ingest.py` (hourly)
- `scout-report.timer` → `report.py` (daily 7am)
- `scout-bot.service` → `scout_bot.py` (always-on long-polling)
- `scout-schedule.service` → `telegram_bot.py` (always-on long-polling)
- `scout-calendar-notify.timer` → `calendar_notify.py` (every 5 min)

**Networking:** UFW active, port 22 only. All Telegram bots use long-polling — no inbound ports needed. Cloudflare Tunnel handles any inbound web traffic.

**Secrets (all git-ignored):**
- `.env` — Anthropic API key, all Telegram bot tokens/chat IDs, webhook secret
- `credentials.json` — Google OAuth client secrets (startup-scout project)
- `token.pickle` — Gmail Scout OAuth (`loganhartford.scout@gmail.com`)
- `calendar_token.pickle` — Google Calendar OAuth
- `calendar_gmail_token.pickle` — Gmail Calendar OAuth (`loganhartford.calendar@gmail.com`)
- `outlook_token.pickle` — Microsoft Graph API (Outlook), device-flow auth via MSAL

---

## Application 1: Startup Scout

### Data flow

```
Gmail inbox → ingest.py → evaluate.py → scout.db → report.py → email
                                              ↑
                                        scout_bot.py (on-demand)
```

### evaluate.py — LLM Pipeline

The heart of the system. Every company from a newsletter passes through these stages in order:

**Stage 1 — Extraction** (`extract_companies`)
- Model: `claude-haiku-4-5-20251001`
- Forced tool use (`tool_choice={"type": "tool", "name": "extract_companies"}`)
- Returns list of `{name, description}` dicts
- `max_tokens` must be ≥ 4096 — lower values cause silent mid-response truncation with no error

**Stage 2 — Discovery** (`discover_company`)
- Agentic web search loop (`_agentic_search_loop`) using Claude's built-in `web_search_20250305` tool
- Up to 2 web searches to build a 300–400 word factual summary
- The discovery context is passed through all subsequent stages
- Continues on `pause_turn` or `max_tokens` stop reasons

**Stage 3 — Pre-filter** (`prefilter_hardware`)
- Model: `claude-haiku-4-5-20251001`
- Cheap binary check: is this obviously non-hardware? Returns "no" or "unknown" only (never "yes")
- "no" → skip immediately; "unknown" → continue to dealbreakers
- Exists to avoid burning API calls on SaaS companies that slipped through extraction

**Stage 4 — Dealbreakers** (`check_dealbreakers_sequential`)
- 5 criteria evaluated one at a time, each with its own mini agentic search loop (up to 2 searches)
- Each criterion: model `claude-haiku-4-5-20251001`, forced tool use
- Fails immediately if any criterion returns "no" or (for the first two) "unknown"
- Criteria in order:
  1. `developing_hardware` — is this physical hardware? ("unknown" = fail)
  2. `is_startup` — pre-Series C, <500 people? ("unknown" = fail)
  3. `billion_dollar_potential` — TAM, defensibility, real traction
  4. `growing_quickly` — recent funding, headcount growth, momentum
  5. `solves_real_problem` — named customers, contracts, validation
- Returns dict of `{criterion: {answer, reason}}` for all evaluated criteria

**Stage 5 — Report generation** (`generate_report`)
- Model: `claude-sonnet-4-6`
- Forced tool use, 13-field analysis
- Only called for companies that passed all 5 dealbreakers
- Fields: `location`, `mission`, `company_size`, `billion_dollar_potential`, `growing_quickly`, `solves_real_problem`, `monopoly_potential`, `novelty`, `breakthrough_vs_incremental`, `timing`, `unique_opportunity`, `learning_opportunities`, `transferable_skills`
- Each field: `{answer: str, assessment: "good"|"neutral"|"bad"}`

**Key helpers:**
- `call_with_retry()` — exponential backoff (10s → 20s → 40s → 80s) on HTTP 529 (overloaded_error), max 4 retries. Raises `CreditExhaustedError` on HTTP 402 (out of credits)
- `_reset_usage()` / `_current_cost()` — per-company cost tracking (~$3/1M input, $15/1M output for Sonnet)
- `_agentic_search_loop()` — handles web search tool calls, accumulates text output, continues on `pause_turn`

**CLI modes:**
```bash
python3 evaluate.py newsletters/foo.txt   # evaluate a newsletter file
python3 evaluate.py --company "Acme"     # look up a specific company
python3 evaluate.py                       # interactive paste mode (type text, end with "END")
```

**Backward-compatible wrapper functions** `check_dealbreakers()` and `research_company()` exist for `scout_bot.py` and `rerun.py` — don't remove them.

### ingest.py

Entry point for scheduled runs. Reads unread emails from `loganhartford.scout@gmail.com`, calls `evaluate.process_newsletter()` for each, marks them read. Catches `CreditExhaustedError` to send an alert email and stop processing (no point continuing if credits are gone).

### db.py

SQLite wrapper for `scout.db`. Single table `companies`:

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PK | autoincrement |
| `name` | TEXT UNIQUE | dedup key, enforced case-insensitively in code |
| `first_seen` | TEXT | ISO timestamp |
| `source` | TEXT | newsletter subject line |
| `passed_dealbreakers` | INTEGER | 1 = passed, 0 = failed |
| `report` | TEXT | JSON blob (see below) |
| `notified_date` | TEXT | NULL until included in a report |
| `cost` | REAL | per-company API cost in USD |

The `cost` column was added via migration — `init_db()` runs `ALTER TABLE ADD COLUMN cost REAL` if it's missing. Safe to call on existing DBs.

**Deduplication** is case-insensitive name matching only. "Acme Inc" and "Acme Corp" are treated as different companies. No fuzzy matching.

**The `report` JSON blob** contains:
- `_description` — description extracted from the newsletter
- `_discovery_context` — full discovery research summary
- `dealbreakers` — dict of criterion → `{answer, reason}` for all evaluated criteria
- The 13 report fields (only present for companies that passed dealbreakers), each `{answer, assessment}`

### report.py

Queries `companies` where `passed_dealbreakers = 1 AND notified_date IS NULL`, renders styled HTML, emails to `logan.hartford@outlook.com`, then calls `mark_as_reported()`.

**Key quirk:** The `markdown` library's `Markdown` class must be instantiated (`md = Markdown()`) before calling `md.convert(text)`. Calling `Markdown.convert()` directly on the class does not work — this is a known API pattern in this version.

Report structure:
- Per-company section with dealbreaker results and all 13 report fields
- Assessment badges: [GOOD] (blue), [OK] (blue), [BAD] (blue) — colors are CSS-controlled
- Per-company cost and total cost
- Source stats table (all-time pass/fail breakdown by newsletter source)

`send_single_company_report()` is a separate function for Telegram bot use — same styling, emails a single company immediately.

### scout_bot.py

Long-polling Telegram bot. User sends a company name → bot researches it, runs dealbreakers, saves to DB, generates full report (if passed), sends back via Telegram and emails it. Companies evaluated this way are marked notified immediately — they won't appear in the weekly report.

Output format uses HTML (not Markdown) with bold/italic tags. Assessment emojis: ✅/🟢 (good), ⚠️/🟡 (neutral), ❌/🔴 (bad).

### rerun.py

Batch re-evaluation of companies already in the DB. Useful for testing prompt changes. Has multi-layer cost guardrails:
1. Per-company cap: $1.00 (runaway detection)
2. Silent zero guard: cost < $0.02 with no error suggests pre-API failure
3. Trailing 10-company average: >$0.40 (3× normal)
4. Budget projection: abort if projected cost >150% of expected budget

```bash
python3 rerun.py                              # re-run all
python3 rerun.py 25                           # most recent 25
python3 rerun.py 25 --summary                 # include breakdown table
python3 rerun.py 25 --email                   # send report after
python3 rerun.py --before 2026-05-01 --guard --budget 90
```

### gmail.py

Gmail API wrapper. Two accounts, each with its own token file and service function:

| Account | Token file | Service function |
|---|---|---|
| `loganhartford.scout@gmail.com` | `token.pickle` | `get_service()` |
| `loganhartford.calendar@gmail.com` | `calendar_gmail_token.pickle` | `get_calendar_gmail_service()` |

`send_report()` sends multipart HTML email with plain-text fallback. `send_email()` is plain-text only (from scout account).

---

## Application 2: Scheduler

### telegram_bot.py

Long-polling Claude agent for calendar control. Model: `claude-opus-4-6`. Only responds to `TELEGRAM_SCHEDULER_CHAT_ID`. Conversation history: last 20 messages per chat (resets on restart).

4 tools:
- `list_bookings` — calls `calendar_api.get_upcoming_bookings()`
- `reschedule_booking` — calls `calendar_api.reschedule_booking()`, Google Calendar sends reschedule notification
- `cancel_booking` — calls `calendar_api.cancel_booking()`, Google Calendar sends cancellation
- `email_participant` — sends plain-text via `gmail.send_calendar_email()`

Deregisters any existing webhook at startup to ensure long-polling only.

### calendar_api.py

Google Calendar wrapper. Token: `calendar_token.pickle` (separate from Gmail, scope: `calendar`).

Settings: America/Vancouver (PT), Mon–Fri 10am–3pm availability, 30-minute slots.

`get_free_slots()` queries ALL calendars on the account for busy times, skips all-day events and marked-as-free events. `create_booking()` adds a 5-min popup reminder for the organizer only (attendee gets Google Calendar's standard invite). All insert/update/delete calls use `sendUpdates="all"` so Google Calendar handles invite emails automatically.

### calendar_notify.py

Oneshot script run every 5 minutes by `scout-calendar-notify.timer`. Tracks state in `logs/calendar_notify_state.json` (`last_checked`, `notified_ids`). First run sets baseline only — no notifications sent. Subsequent runs detect new events with external attendees and notify via `telegram_notifier`.

---

## Application 3: Email Triage

### triage.py

Outlook email classifier and executor. Uses Ollama (`http://localhost:11434`, Qwen 3 8B) with a 3-call, 2/3 consensus pattern for robustness against local LLM uncertainty.

**Decision flow for each email:**
1. `pre_filter()` — hard-coded rules (always-urgent/always-keep senders) bypass Ollama entirely
2. `ask_ollama()` — 3 calls, returns category only if 2/3 agree, otherwise defaults to "keep"
3. Execute action based on category:
   - `urgent` → send Telegram notification, save to `logs/triage_urgent_pending.json`
   - `keep` → mark as read only
   - `unsubscribe` → attempt one-click POST → URL GET → mailto, then mark read
   - `trash` → move to Deleted Items (recoverable)

**Hard-coded rules (always-urgent):** replies to user's own emails, government notices with deadlines, explicit requests. **Always-keep:** family senders, own Scout inbox.

State files:
- `logs/triage_seen.json` — set of already-processed email IDs (prevents re-processing)
- `logs/triage_urgent_pending.json` — urgent emails awaiting Telegram action
- `logs/triage_review_<date>_<time>.txt` — human-readable review file for `--execute-review` mode

### triage_bot.py

Telegram Claude agent for managing urgent pending emails. Model: `claude-opus-4-6`. Conversation history: last 30 messages per chat (resets on restart).

4 tools: `list_pending_emails`, `send_reply`, `trash_email`, `dismiss_email`.

**Critical safety invariant:** `send_reply` never sends without explicit user approval — the agent prompt checks for phrases like "send it", "yes", "looks good" before calling the tool. Don't loosen this constraint.

### outlook.py

Microsoft Graph API wrapper. Auth via MSAL device-flow (`PublicClientApplication`). Token serialized to `outlook_token.pickle` with automatic refresh.

`get_emails()` parses `List-Unsubscribe` headers to detect one-click vs URL vs mailto unsubscribe methods. `do_unsubscribe()` tries them in priority order: one-click POST → URL GET → mailto via Graph API.

`move_to_trash()` moves to Deleted Items (recoverable, not permanent delete). `create_reply_draft()` + `send_draft()` preserves thread context for replies.

---

## LLM Patterns (cross-cutting)

### Forced tool use
All structured Claude calls use `tool_choice={"type": "tool", "name": "<tool_name>"}`. This guarantees schema-conformant output and eliminates JSON parsing errors. Never ask the model to return raw JSON — always use a tool.

### Model selection by task
| Task | Model | Reason |
|---|---|---|
| Extraction, pre-filter, individual dealbreakers | `claude-haiku-4-5-20251001` | High volume, cheap, binary outputs |
| Report generation (13 fields) | `claude-sonnet-4-6` | Nuance matters, called once per company |
| Agentic loops (Scout bot, Scheduler) | `claude-opus-4-6` | Complex reasoning, conversation context |

### Retry and credit handling
`call_with_retry()` in `evaluate.py`: exponential backoff (10s, 20s, 40s, 80s) on HTTP 529, max 4 retries. HTTP 402 raises `CreditExhaustedError` immediately — never retry a 402.

### Ollama consensus (triage only)
3 independent calls to `_ask_ollama_once()`, require 2/3 agreement. If no consensus, default to "keep" (safe action). This compensates for local LLM nondeterminism without being expensive.

---

## Common Pitfalls

- **`max_tokens` on extraction calls** — must be ≥ 4096. Lower values silently truncate the response mid-JSON with no error raised.
- **`Markdown` instantiation** — `md = Markdown(); md.convert(text)` works. `Markdown.convert(text)` does not. Always instantiate.
- **DB cost column migration** — `init_db()` handles the `ALTER TABLE ADD COLUMN` migration automatically. Don't add it manually.
- **Separate OAuth tokens** — three different pickle files for Gmail Scout, Gmail Calendar, and Outlook. They cannot be reused across accounts/scopes.
- **Triage seen IDs** — `triage_seen.json` persists across runs. If re-testing, delete it or the emails won't be re-processed.
- **Long-polling vs webhooks** — both Telegram bots (scout_bot.py and telegram_bot.py) use long-polling. Confirming a webhook URL is irrelevant — there is none.
