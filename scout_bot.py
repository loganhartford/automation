import asyncio
import json
import os
import subprocess
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import anthropic

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_SCOUT_BOT_TOKEN")
ALLOWED_CHAT_ID = int(os.getenv("TELEGRAM_SCOUT_CHAT_ID"))

client = anthropic.Anthropic()

DEALBREAKER_LABELS = {
    "developing_hardware": "Developing Hardware",
    "is_startup": "Is a Startup",
    "solves_real_problem": "Solves Real Problem",
    "growing_quickly": "Growing Quickly",
    "billion_dollar_potential": "Billion Dollar Potential",
}

REPORT_LABELS = {
    "company_size": "Company Size",
    "monopoly_potential": "Monopoly Potential",
    "novelty": "Novelty",
    "breakthrough_vs_incremental": "Breakthrough vs Incremental",
    "timing": "Timing",
    "unique_opportunity": "Unique Opportunity",
    "learning_opportunities": "Learning Opportunities",
    "transferable_skills": "Transferable Skills",
}

ASSESSMENT_EMOJI = {"good": "🟢", "neutral": "🟡", "bad": "🔴"}


def _format_report(name: str, report: dict) -> str:
    location = report.pop("location", {}).get("answer", "Unknown")
    mission = report.pop("mission", {}).get("answer", "")
    report.pop("dealbreakers", None)
    report.pop("_description", None)

    lines = [f"<b>{name}</b> — {location}"]
    if mission:
        lines.append(f"<i>{mission}</i>")
    lines.append("")

    for key, value in report.items():
        if not isinstance(value, dict) or "answer" not in value:
            continue
        label = REPORT_LABELS.get(key, key.replace("_", " ").title())
        emoji = ASSESSMENT_EMOJI.get(value.get("assessment", ""), "")
        lines.append(f"{emoji} <b>{label}</b>")
        lines.append(value["answer"])
        lines.append("")

    return "\n".join(lines)


async def _reply(update: Update, text: str):
    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i:i + 4000], parse_mode="HTML")


async def _reply_plain(update: Update, text: str):
    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i:i + 4000])


async def _handle_repair(update: Update, command: str):
    from repair import load_error_context, investigate, apply_fix
    ctx = load_error_context()
    if not ctx:
        await update.message.reply_text("No recent error context found. An error must occur first.")
        return

    age_h = (datetime.now() - datetime.fromisoformat(ctx["timestamp"])).total_seconds() / 3600
    header = f"Last error: {ctx['script']} — {ctx['error_type']} ({age_h:.1f}h ago)\n\n"

    if command == "investigate":
        await update.message.reply_text(f"Investigating {ctx['script']} error... (~1 min)")
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, investigate, ctx)
        except subprocess.TimeoutExpired:
            await update.message.reply_text("Timed out (>3 min). Try 'apply' to go straight to a fix.")
            return
        except Exception as e:
            await update.message.reply_text(f"Claude error: {e}")
            return
        await _reply_plain(update, header + result)

    elif command in ("apply", "apply fix"):
        await update.message.reply_text(f"Fixing {ctx['script']} error... (~3 min)")
        try:
            loop = asyncio.get_event_loop()
            output, diff = await loop.run_in_executor(None, apply_fix, ctx)
        except subprocess.TimeoutExpired:
            await update.message.reply_text("Timed out (>5 min).")
            return
        except Exception as e:
            await update.message.reply_text(f"Claude error: {e}")
            return
        await _reply_plain(update, header + output)
        if diff:
            await _reply_plain(update, f"Changes:\n\n{diff}")
        else:
            await update.message.reply_text("No file changes were made.")


async def _handle_full_report_request(update: Update, company_id: int):
    try:
        from evaluate import generate_full_report_for_company_id
        from report import send_single_company_report

        await update.message.reply_text("Generating full report...")
        result, generated = generate_full_report_for_company_id(company_id)

        report = json.loads(result["report_json"])
        await _reply(update, _format_report(result["name"], report))
        send_single_company_report(
            result["name"],
            result["first_seen"],
            result["source"],
            result["report_json"],
        )

        if generated:
            await update.message.reply_text("Full report generated and emailed.")
        else:
            await update.message.reply_text("Existing full report emailed.")
    except ValueError as e:
        await update.message.reply_text(str(e))
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
        raise


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ALLOWED_CHAT_ID:
        return
    if context.args:
        payload = context.args[0]
        if payload.startswith("full_"):
            try:
                company_id = int(payload.removeprefix("full_"))
            except ValueError:
                await update.message.reply_text("Invalid full-report link.")
                return
            await _handle_full_report_request(update, company_id)
            return
    await update.message.reply_text("Scout bot ready. Send a company name to evaluate it.")


async def full(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ALLOWED_CHAT_ID:
        return
    if not context.args:
        await update.message.reply_text("Usage: /full <company_id>")
        return
    try:
        company_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Company id must be a number.")
        return
    await _handle_full_report_request(update, company_id)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ALLOWED_CHAT_ID:
        return

    name = update.message.text.strip()
    if not name:
        return

    cmd = name.lower()
    if cmd in ("investigate", "apply", "apply fix"):
        await _handle_repair(update, cmd)
        return

    if name.startswith("full_"):
        try:
            company_id = int(name.removeprefix("full_"))
        except ValueError:
            await update.message.reply_text("Invalid full-report command.")
            return
        await _handle_full_report_request(update, company_id)
        return

    await update.message.reply_text("On it...")

    try:
        from evaluate import check_dealbreakers, generate_report, research_company, _reset_usage, _current_cost
        from db import init_db, already_seen, save_company
        from report import send_single_company_report

        init_db()

        if already_seen(name):
            await _reply(update, f"⚠️ <b>{name}</b> is already in the database.")
            return

        _reset_usage()
        await update.message.reply_text("Researching...")
        research_context = research_company(name)
        if not research_context:
            await update.message.reply_text("⚠️ Research failed — evaluating without web data.")
        else:
            await update.message.reply_text(f"Research complete ({len(research_context)} chars)")
        paragraphs = [p.strip() for p in research_context.split("\n\n") if p.strip()] if research_context else []
        description = paragraphs[0] if paragraphs else f"No information found for {name}."
        await _reply(update, f"<i>{description}</i>")

        await update.message.reply_text("Checking dealbreakers...")
        passed, dealbreaker_results = check_dealbreakers(name, description, research_context)

        lines = []
        for key, value in dealbreaker_results.items():
            label = DEALBREAKER_LABELS.get(key, key.replace("_", " ").title())
            a = value["answer"]
            icon = "✅" if a == "yes" else ("⚠️" if a == "unknown" else "❌")
            lines.append(f"{icon} <b>{label}</b>: {value['reason']}")
        await _reply(update, "\n".join(lines))

        if not passed:
            save_company(name, source="telegram", passed=False, cost=_current_cost())
            await update.message.reply_text("❌ Did not pass dealbreakers.")
            return

        await update.message.reply_text("✅ Passed! Generating report...")
        report = generate_report(name, description, dealbreaker_results, research_context)
        report["_description"] = description
        save_company(name, source="telegram", passed=True, report=json.dumps(report), cost=_current_cost())

        telegram_report = json.loads(json.dumps(report))
        await _reply(update, _format_report(name, telegram_report))

        first_seen = datetime.now().strftime("%Y-%m-%d")
        send_single_company_report(name, first_seen, "telegram", json.dumps(report))
        await update.message.reply_text("📧 Report also sent to email.")

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
        raise


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("full", full))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Scout bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
