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

for ep in [19,20,21,22,23,24]:
    tag = f"S01E{ep:02d}"
    cur.execute("SELECT source_path FROM conversions WHERE source_path LIKE ? AND source_path LIKE ? AND status='pending' ORDER BY id DESC LIMIT 1", ("%The Pet Girl of Sakurasou%", f"%{tag}%"))
    p = cur.fetchone()
    cur.execute("SELECT source_hash, output_hash FROM conversions WHERE source_path LIKE ? AND source_path LIKE ? AND status='done' ORDER BY id DESC LIMIT 1", ("%_The Pet Girl of Sakurasou%", f"%{tag}%"))
    d = cur.fetchone()
    print(f"\n{tag}")
    if not p or not d:
        print("  missing rows")
        continue
    ppath = p['source_path'].replace('/', '\\')
    ph = hash_head(ppath) if os.path.exists(ppath) else None
    dsh = d['source_hash']
    doh = d['output_hash']
    print(f"  pending hash: {ph}")
    print(f"  done source_hash match: {ph == dsh}")
    print(f"  done output_hash match: {ph == doh}")

c.close()
