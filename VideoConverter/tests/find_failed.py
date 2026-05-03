import sqlite3
import os

conn = sqlite3.connect("conversions.db")
conn.row_factory = sqlite3.Row
failed = conn.execute(
    "SELECT id, source_path, source_size_bytes FROM conversions"
    " WHERE status = 'failed' ORDER BY source_size_bytes ASC LIMIT 20"
).fetchall()
for row in failed:
    path = row["source_path"].replace("/", os.sep)
    exists = os.path.exists(path)
    mb = (row["source_size_bytes"] or 0) / 1024 / 1024
    print(f"EXISTS={exists} | {mb:.0f}MB | {path}")
conn.close()
