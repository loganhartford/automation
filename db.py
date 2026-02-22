import sqlite3
from datetime import datetime

DB_PATH = "scout.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE,
            first_seen TEXT,
            source TEXT,
            passed_dealbreakers INTEGER,
            report TEXT,
            notified_date TEXT
        )
    """)
    conn.commit()
    conn.close()

def already_seen(name: str) -> bool:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        row = conn.execute(
            "SELECT id FROM companies WHERE LOWER(name) = LOWER(?)", (name,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()

def save_company(name, source, passed, report=None):
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            """INSERT INTO companies (name, first_seen, source, passed_dealbreakers, report)
               VALUES (?, ?, ?, ?, ?)""",
            (name, datetime.now().isoformat(), source, int(passed), report)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        pass  # already exists, skip
    conn.close()

def get_unreported_companies():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """SELECT name, first_seen, source, report FROM companies
           WHERE passed_dealbreakers = 1 AND notified_date IS NULL"""
    ).fetchall()
    conn.close()
    return rows

def mark_as_reported(names: list):
    conn = sqlite3.connect(DB_PATH)
    now = datetime.now().isoformat()
    for name in names:
        conn.execute(
            "UPDATE companies SET notified_date = ? WHERE name = ?", (now, name)
        )
    conn.commit()
    conn.close()