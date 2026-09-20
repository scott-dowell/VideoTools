import os
import sqlite3

TARGET = r"C:/Users/scott/Downloads/Anime/_Ah! My Buddha/Ah! My Buddha 01 (CB8C790A).mp4"
DB = r"c:\VideoTools\VideoConverter\conversions.db"

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

norm = TARGET.replace('\\', '/')
print('target:', norm)
print('exists:', os.path.exists(norm.replace('/', '\\')))
if os.path.exists(norm.replace('/', '\\')):
    st = os.stat(norm.replace('/', '\\'))
    print('fs_mtime:', st.st_mtime)
    print('fs_size:', st.st_size)

print('\nrows by source_path exact:')
cur.execute("SELECT id, source_path, output_path, status, source_mtime, completed_at, source_hash, output_hash, saved_mb, saved_pct FROM conversions WHERE source_path = ? ORDER BY id", (norm,))
rows = cur.fetchall()
for r in rows:
    print(dict(r))
print('count:', len(rows))

print('\nrows by output_path exact:')
cur.execute("SELECT id, source_path, output_path, status, source_mtime, completed_at, source_hash, output_hash, saved_mb, saved_pct FROM conversions WHERE output_path = ? ORDER BY id", (norm,))
rows2 = cur.fetchall()
for r in rows2:
    print(dict(r))
print('count:', len(rows2))

print('\nrows by filename token:')
cur.execute("SELECT id, source_path, output_path, status, source_mtime, completed_at FROM conversions WHERE source_path LIKE ? OR output_path LIKE ? ORDER BY id DESC LIMIT 20", ("%Ah! My Buddha 01 (CB8C790A).mp4%", "%Ah! My Buddha 01 (CB8C790A).mp4%"))
rows3 = cur.fetchall()
for r in rows3:
    print(dict(r))

conn.close()
