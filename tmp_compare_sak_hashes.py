import sqlite3
import os
import hashlib


def hash_head(path, head_bytes=2*1024*1024):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        h.update(f.read(head_bytes))
    return h.hexdigest()

c = sqlite3.connect(r"c:\VideoTools\VideoConverter\conversions.db")
c.row_factory = sqlite3.Row
cur = c.cursor()

pairs = [19,20,21,22,23,24]
for ep in pairs:
    tag = f"S01E{ep:02d}"
    cur.execute("SELECT source_path, status, source_hash FROM conversions WHERE source_path LIKE ? AND source_path LIKE ? ORDER BY id DESC", ("%Sakurasou%", f"%{tag}%"))
    rows = cur.fetchall()
    print(f"\n{tag}")
    for r in rows:
        path = r['source_path']
        status = r['status']
        sh = r['source_hash']
        exists = os.path.exists(path.replace('/', '\\'))
        actual = hash_head(path.replace('/', '\\')) if exists else None
        print(f"  {status:7} exists={exists} db_hash={'yes' if sh else 'no'} actual={'yes' if actual else 'no'} path={path}")
        if sh and actual:
            print(f"    db==actual? {sh==actual}")

c.close()
