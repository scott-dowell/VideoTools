import sqlite3
conn = sqlite3.connect(r'C:\VideoTools\VideoConverter\conversions.db')

rows = conn.execute(
    "SELECT status, COUNT(*) FROM conversions "
    "WHERE source_path LIKE '%Stratos%' OR source_path LIKE '%Yandere%' "
    "GROUP BY status"
).fetchall()
print('IS + Yandere statuses:')
for r in rows:
    print(' ', r)

cnt = conn.execute("SELECT COUNT(*) FROM conversions WHERE status='no_saving'").fetchone()[0]
print(f'Total no_saving records in DB: {cnt}')

row = conn.execute("SELECT id, source_path FROM conversions WHERE status='no_saving' LIMIT 1").fetchone()
if row:
    print(f'Sample no_saving: id={row[0]}, {row[1]}')
conn.close()
