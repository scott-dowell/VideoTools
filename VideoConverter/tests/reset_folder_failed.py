"""Reset all 'failed' records in a given folder path to 'pending'."""
import sqlite3
import sys

folder = sys.argv[1] if len(sys.argv) > 1 else "C:/Users/scott/Downloads/Anime/Ikki Tousen/01. Ikkitousen/"
folder = folder.replace("\\", "/").rstrip("/") + "/"
db_path = "conversions.db"

with sqlite3.connect(db_path) as conn:
    rows = conn.execute(
        "SELECT id, source_path FROM conversions WHERE status='failed' AND source_path LIKE ?",
        (folder + "%",),
    ).fetchall()
    if not rows:
        print("No failed records found for:", folder)
        sys.exit(0)
    print("Resetting:")
    for r in rows:
        print(f"  id={r[0]}  {r[1]}")
    conn.execute(
        """UPDATE conversions
           SET status='pending', error_tail=NULL, started_at=NULL, completed_at=NULL
           WHERE status='failed' AND source_path LIKE ?""",
        (folder + "%",),
    )
    print(f"Done — {len(rows)} record(s) reset to pending.")
