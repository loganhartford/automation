import sqlite3
from db import DB_PATH

conn = sqlite3.connect(DB_PATH)

# Show what will be reset
rows = conn.execute(
    "SELECT name, notified_date FROM companies WHERE notified_date IS NOT NULL"
).fetchall()

if not rows:
    print("No reported companies found.")
    conn.close()
    exit()

print("The following companies will be marked as unreported:")
for name, notified_date in rows:
    print(f"  {name} (reported: {notified_date[:10]})")

confirm = input("\nReset all of these? (y/n): ")
if confirm.lower() == "y":
    conn.execute("UPDATE companies SET notified_date = NULL WHERE notified_date IS NOT NULL")
    conn.commit()
    print(f"Done. {len(rows)} companies reset.")
else:
    print("Cancelled.")

conn.close()