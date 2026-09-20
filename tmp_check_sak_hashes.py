import sqlite3

c = sqlite3.connect(r"c:\VideoTools\VideoConverter\conversions.db")
cur = c.cursor()
cur.execute("SELECT COUNT(*) FROM conversions WHERE source_path LIKE ? AND status='done'", ("%Sakurasou%",))
done_count = cur.fetchone()[0]
cur.execute("SELECT COUNT(*) FROM conversions WHERE source_path LIKE ? AND status='done' AND source_hash IS NOT NULL", ("%Sakurasou%",))
source_hash_count = cur.fetchone()[0]
cur.execute("SELECT COUNT(*) FROM conversions WHERE source_path LIKE ? AND status='done' AND output_hash IS NOT NULL", ("%Sakurasou%",))
output_hash_count = cur.fetchone()[0]
print(f"done={done_count} source_hash={source_hash_count} output_hash={output_hash_count}")

cur.execute("SELECT source_path, status, source_mtime FROM conversions WHERE source_path LIKE ? ORDER BY source_path", ("%Sakurasou%",))
rows = cur.fetchall()
print(f"rows={len(rows)}")
for row in rows[:5]:
    print(row)

c.close()
