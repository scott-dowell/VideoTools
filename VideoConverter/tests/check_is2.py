import sqlite3
conn = sqlite3.connect(r'C:\VideoTools\VideoConverter\conversions.db')
rows = conn.execute(
    "SELECT id, status, anime_mode, encoder_used, source_path "
    "FROM conversions WHERE source_path LIKE '%Infinite Stratos%' AND status='failed' "
    "ORDER BY id LIMIT 5"
).fetchall()
for r in rows:
    print(f"id={r[0]} status={r[1]} anime_mode={r[2]} encoder={r[3]}")
    print(f"  {r[4]}")
conn.close()
