import sqlite3

c = sqlite3.connect(r"c:\VideoTools\VideoConverter\conversions.db")
c.row_factory = sqlite3.Row
cur = c.cursor()

cur.execute("""
SELECT id, source_path, status, source_mtime, output_path, output_size_mb, saved_mb, saved_pct, completed_at, source_hash, output_hash
FROM conversions
WHERE source_path LIKE ? AND status='pending'
ORDER BY source_path
""", ("%The Pet Girl of Sakurasou%",))
rows = cur.fetchall()
print(f"pending_rows={len(rows)}")
for r in rows:
    print(f"id={r['id']} file={r['source_path'].split('/')[-1]} status={r['status']} completed={r['completed_at']} output_path={r['output_path']} output_size={r['output_size_mb']} saved={r['saved_mb']} pct={r['saved_pct']} has_source_hash={bool(r['source_hash'])} has_output_hash={bool(r['output_hash'])}")

c.close()
