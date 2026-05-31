"""Error context serialization and Claude Code repair runner."""
import json
import os
import subprocess
from datetime import datetime

WORKING_DIR = os.path.dirname(os.path.abspath(__file__))
ERROR_CONTEXT_FILE = os.path.join(WORKING_DIR, "logs", "last_error.json")

REPAIR_HINT = "Reply 'investigate' to analyze or 'apply' to auto-fix."


def save_error_context(script: str, error_type: str, error_msg: str, tb: str):
    """Persist error details so bots can reference them for repair."""
    data = {
        "script": script,
        "error_type": error_type,
        "error_msg": str(error_msg),
        "traceback": tb,
        "timestamp": datetime.now().isoformat(),
    }
    with open(ERROR_CONTEXT_FILE, "w") as f:
        json.dump(data, f, indent=2)


def load_error_context():
    """Return the last saved error context dict, or None."""
    if not os.path.exists(ERROR_CONTEXT_FILE):
        return None
    with open(ERROR_CONTEXT_FILE) as f:
        return json.load(f)


def _run_claude(prompt: str, *, allow_edits: bool, timeout: int) -> str:
    if allow_edits:
        cmd = [
            "claude", "-p", prompt,
            "--dangerously-skip-permissions",
            "--no-session-persistence",
            "--max-budget-usd", "1.50",
        ]
    else:
        cmd = [
            "claude", "-p", prompt,
            "--tools", "Read,Bash",
            "--no-session-persistence",
            "--max-budget-usd", "0.50",
        ]
    result = subprocess.run(
        cmd,
        cwd=WORKING_DIR,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )
    out = result.stdout.strip()
    err = result.stderr.strip()
    if out:
        return out
    if err:
        return f"(stderr) {err}"
    return "(no output)"


def investigate(ctx: dict) -> str:
    """Read-only analysis — returns proposed fix as plain text, no file edits."""
    prompt = (
        f"Analyze this error in the automation repo at {WORKING_DIR} and propose a fix.\n\n"
        f"Script: {ctx['script']}\n"
        f"Error: {ctx['error_type']}: {ctx['error_msg']}\n"
        f"Traceback:\n{ctx['traceback']}\n\n"
        "Read the relevant source files, understand the root cause, and describe exactly "
        "what change would fix it. Do not edit any files."
    )
    return _run_claude(prompt, allow_edits=False, timeout=180)


def apply_fix(ctx: dict) -> tuple[str, str]:
    """Edit files to fix the error. Returns (claude_output, git_diff)."""
    prompt = (
        f"Fix this error in the automation repo at {WORKING_DIR}.\n\n"
        f"Script: {ctx['script']}\n"
        f"Error: {ctx['error_type']}: {ctx['error_msg']}\n"
        f"Traceback:\n{ctx['traceback']}\n\n"
        "Read the relevant source files, identify the root cause, and edit the file(s) to "
        "fix it. Do not commit. Do not restart any services."
    )
    output = _run_claude(prompt, allow_edits=True, timeout=300)

    diff = subprocess.run(
        ["git", "diff"],
        cwd=WORKING_DIR,
        capture_output=True,
        text=True,
    ).stdout.strip()

    return output, diff
