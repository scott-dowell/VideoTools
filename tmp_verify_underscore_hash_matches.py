import hashlib
import os
import sqlite3

DB = r"c:\VideoTools\VideoConverter\conversions.db"
CURRENT_DIR = r"C:/Users/scott/Downloads/Anime/The Pet Girl of Sakurasou"
UNDERSCORE_DIR_TOKEN = r"/Anime/_The Pet Girl of Sakurasou/"


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

print(f"Checking {len(files)} files in: {CURRENT_DIR}")
print()

matches_source = 0
matches_output = 0
no_done_row = 0
no_hash = 0
mismatch = 0

for name in files:
    current_path = os.path.join(CURRENT_DIR, name)
    current_hash = hash_head(current_path)
    if not current_hash:
        print(f"{name}: ERROR hashing current file")
        continue

    # Done row from underscore folder for same filename
    cur.execute(
        """
        SELECT id, source_path, source_hash, output_hash
        FROM conversions
        WHERE status = 'done'
          AND source_path = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (f"C:/Users/scott/Downloads/Anime/_The Pet Girl of Sakurasou/{name}",),
    )
    row = cur.fetchone()

    if not row:
        no_done_row += 1
        print(f"{name}: NO underscore done row")
        continue

    s_hash = row["source_hash"]
    o_hash = row["output_hash"]

    if not s_hash and not o_hash:
        no_hash += 1
        print(f"{name}: underscore done row has no stored hashes (id={row['id']})")
        continue

    source_match = (s_hash == current_hash) if s_hash else False
    output_match = (o_hash == current_hash) if o_hash else False

    if source_match:
        matches_source += 1
    if output_match:
        matches_output += 1

    if source_match or output_match:
        via = "source_hash" if source_match else "output_hash"
        print(f"{name}: MATCH via {via} (id={row['id']})")
    else:
        mismatch += 1
        print(f"{name}: NO MATCH (id={row['id']})")

print()
print("Summary:")
print(f"  source hash matches: {matches_source}")
print(f"  output hash matches: {matches_output}")
print(f"  no underscore done row: {no_done_row}")
print(f"  underscore row missing hashes: {no_hash}")
print(f"  explicit mismatches: {mismatch}")

conn.close()
