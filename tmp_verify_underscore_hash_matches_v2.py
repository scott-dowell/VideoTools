import hashlib
import os
import sqlite3

DB = r"c:\VideoTools\VideoConverter\conversions.db"
CURRENT_DIR = r"C:/Users/scott/Downloads/Anime/The Pet Girl of Sakurasou"
UNDERSCORE_PREFIX = r"C:/Users/scott/Downloads/Anime/_The Pet Girl of Sakurasou/"


def hash_head(path: str, head_bytes: int = 2 * 1024 * 1024) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            h.update(f.read(head_bytes))
        return h.hexdigest()
    except Exception:
        return None


conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

files = sorted([f for f in os.listdir(CURRENT_DIR) if f.lower().endswith(".mp4")])

print(f"Checking {len(files)} files against underscore-origin done records")
print()

source_match = 0
output_match = 0
mismatch = 0
no_row = 0

for name in files:
    current_path = os.path.join(CURRENT_DIR, name)
    current_hash = hash_head(current_path)
    if not current_hash:
        print(f"{name}: ERROR hashing current file")
        continue

    # Treat a row as underscore-origin if either source_path or output_path is underscore path.
    cur.execute(
        """
        SELECT id, source_path, output_path, source_hash, output_hash, status
        FROM conversions
        WHERE status = 'done'
          AND (
                source_path = ?
                OR output_path = ?
              )
        ORDER BY id DESC
        LIMIT 1
        """,
        (f"{UNDERSCORE_PREFIX}{name}", f"{UNDERSCORE_PREFIX}{name}"),
    )
    row = cur.fetchone()

    if not row:
        no_row += 1
        print(f"{name}: NO underscore-origin done row")
        continue

    s_hash = row["source_hash"]
    o_hash = row["output_hash"]
    s_ok = bool(s_hash and s_hash == current_hash)
    o_ok = bool(o_hash and o_hash == current_hash)

    if s_ok:
        source_match += 1
    if o_ok:
        output_match += 1

    if s_ok or o_ok:
        via = "source_hash" if s_ok else "output_hash"
        print(f"{name}: MATCH via {via} (id={row['id']})")
    else:
        mismatch += 1
        print(f"{name}: NO MATCH (id={row['id']})")

print()
print("Summary:")
print(f"  source hash matches: {source_match}")
print(f"  output hash matches: {output_match}")
print(f"  no underscore-origin done row: {no_row}")
print(f"  explicit mismatches: {mismatch}")

conn.close()
