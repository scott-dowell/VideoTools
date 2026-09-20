import os
import sqlite3

DB = r"c:\VideoTools\VideoConverter\conversions.db"
FOLDER = r"C:/Users/scott/Downloads/Anime/The Pet Girl of Sakurasou"

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

cur.execute(
    """
    SELECT id, source_path, status, source_mtime, completed_at, output_path
    FROM conversions
    WHERE source_path LIKE ?
    ORDER BY source_path
    """,
    (FOLDER + "%",),
)
rows = cur.fetchall()

print(f"rows in folder path: {len(rows)}")
for r in rows:
    name = r["source_path"].split("/")[-1]
    print(f"{name} | id={r['id']} | status={r['status']} | completed={r['completed_at']} | output={r['output_path']}")

print("\nmatching done records by episode token (any path):")
for ep in range(19, 25):
    token = f"S01E{ep:02d}"
    cur.execute(
        """
        SELECT id, source_path, status, completed_at
        FROM conversions
        WHERE status='done' AND source_path LIKE ?
        ORDER BY id DESC
        LIMIT 5
        """,
        (f"%{token}%",),
    )
    done_rows = cur.fetchall()
    print(f"{token}: {len(done_rows)} done rows")
    for d in done_rows:
        exists = os.path.exists(d["source_path"].replace("/", "\\"))
        print(f"  id={d['id']} exists={exists} source={d['source_path']}")

conn.close()
