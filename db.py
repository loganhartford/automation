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
    try:
        conn.execute("ALTER TABLE companies ADD COLUMN cost REAL")
    except sqlite3.OperationalError:
        pass  # column already exists
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

def save_company(name, source, passed, report=None, cost=None):
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            """INSERT INTO companies (name, first_seen, source, passed_dealbreakers, report, cost)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (name, datetime.now().isoformat(), source, int(passed), report, cost)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        pass  # already exists, skip
    conn.close()

def get_unreported_companies():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """SELECT id, name, first_seen, source, report, cost FROM companies
           WHERE passed_dealbreakers = 1 AND notified_date IS NULL"""
    ).fetchall()
    conn.close()
    return rows


def get_company_by_id(company_id: int):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        """SELECT id, name, first_seen, source, passed_dealbreakers, report, notified_date
           FROM companies WHERE id = ?""",
        (company_id,)
    ).fetchone()
    conn.close()
    return row


def get_company_by_name(name: str):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        """SELECT id, name, first_seen, source, passed_dealbreakers, report, notified_date
           FROM companies WHERE LOWER(name) = LOWER(?)""",
        (name,)
    ).fetchone()
    conn.close()
    return row

def get_recent_companies(limit=None, before=None):
    conn = sqlite3.connect(DB_PATH)
    conditions, params = [], []
    if before:
        conditions.append("first_seen < ?")
        params.append(before)
    query = "SELECT name, source, report FROM companies"
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY id DESC"
    if limit:
        query += f" LIMIT {int(limit)}"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return rows


def update_company(name, passed, report=None, cost=None):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE companies SET passed_dealbreakers=?, report=?, notified_date=NULL, cost=? WHERE LOWER(name)=LOWER(?)",
        (int(passed), report, cost, name)
    )
    conn.commit()
    conn.close()


def update_company_report(company_id: int, report: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE companies SET report = ? WHERE id = ?",
        (report, company_id)
    )
    conn.commit()
    conn.close()


def mark_as_reported(names: list):
    conn = sqlite3.connect(DB_PATH)
    now = datetime.now().isoformat()
    for name in names:
        conn.execute(
            "UPDATE companies SET notified_date = ? WHERE name = ?", (now, name)
        )
    conn.commit()
    conn.close()


def get_source_stats():
    """Return all-time (source, passed, failed) rows sorted by total desc."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """SELECT source,
                  SUM(passed_dealbreakers) AS passed,
                  SUM(1 - passed_dealbreakers) AS failed
           FROM companies
           GROUP BY source
           ORDER BY (SUM(passed_dealbreakers) + SUM(1 - passed_dealbreakers)) DESC"""
    ).fetchall()
    conn.close()
    return rows
