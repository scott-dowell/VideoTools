import sqlite3
conn = sqlite3.connect(r'C:\VideoTools\VideoConverter\conversions.db')

rows = conn.execute(
    "SELECT id, status, source_path, started_at, completed_at FROM conversions "
    "WHERE source_path LIKE '%Infinite Stratos 01%' ORDER BY id"
).fetchall()
for r in rows:
    print(f'id={r[0]} status={r[1]} started={r[3]} completed={r[4]}')
    print(f'  {r[2]}')

print()

rows2 = conn.execute(
    "SELECT status, COUNT(*) FROM conversions "
    "WHERE source_path LIKE '%Infinite Stratos%' GROUP BY status"
).fetchall()
print('All statuses for Infinite Stratos:')
for r in rows2:
    print(f'  {r}')

print()

# Check for duplicate source_paths
dups = conn.execute(
    "SELECT source_path, COUNT(*) as cnt FROM conversions "
    "WHERE source_path LIKE '%Infinite Stratos%' "
    "GROUP BY source_path HAVING cnt > 1"
).fetchall()
print(f'Paths with duplicates: {len(dups)}')
for r in dups[:5]:
    print(f'  count={r[1]}: {r[0]}')

conn.close()
