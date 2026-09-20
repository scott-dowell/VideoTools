import sqlite3

c = sqlite3.connect(r"c:\VideoTools\VideoConverter\conversions.db")
c.row_factory = sqlite3.Row
cur = c.cursor()

for ep in [19,20,21,22,23,24]:
    tag = f"S01E{ep:02d}"
    cur.execute("""
    SELECT id, source_path, status, completed_at, output_path
    FROM conversions
    WHERE source_path LIKE ? OR output_path LIKE ?
    ORDER BY id DESC
    """, (f"%{tag}%", f"%{tag}%"))
    rows = cur.fetchall()
    print(f"\n{tag}: {len(rows)} rows")
    for r in rows[:5]:
        print(f"  id={r['id']} status={r['status']} source={r['source_path']} output={r['output_path']} completed={r['completed_at']}")

c.close()
