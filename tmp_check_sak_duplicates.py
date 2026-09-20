import sqlite3

c = sqlite3.connect(r"c:\VideoTools\VideoConverter\conversions.db")
cur = c.cursor()

cur.execute("""
SELECT source_path, COUNT(*) as n
FROM conversions
WHERE source_path LIKE ?
GROUP BY source_path
HAVING COUNT(*) > 1
ORDER BY source_path
""", ("%Sakurasou%",))
paths = cur.fetchall()
print(f"duplicate_paths={len(paths)}")
for source_path, n in paths:
    print(f"\n{source_path} ({n} rows)")
    cur.execute("""
    SELECT id, status, source_mtime, completed_at
    FROM conversions
    WHERE source_path = ?
    ORDER BY id
    """, (source_path,))
    rows = cur.fetchall()
    for r in rows:
        print(f"  id={r[0]} status={r[1]} mtime={r[2]} completed={r[3]}")

c.close()
