import os
import sqlite3
import sys

sys.path.insert(0, r"c:\VideoTools\VideoConverter")
import db
import scanner

DB = r"c:\VideoTools\VideoConverter\conversions.db"
ROOT = r"C:\Users\scott\Downloads\Anime\The Pet Girl of Sakurasou"
FILE = r"C:\Users\scott\Downloads\Anime\The Pet Girl of Sakurasou\S01E01-Cat, White, Mashiro.mp4"

db.init_db(DB)
fp = FILE.replace('\\', '/')
fs_mtime = os.path.getmtime(FILE)

conn = sqlite3.connect(DB)
cur = conn.cursor()
cur.execute("SELECT id, source_mtime, source_hash, status FROM conversions WHERE source_path=?", (fp,))
row = cur.fetchone()
print('before row', row)
if not row:
    raise SystemExit('record missing')
rec_id, old_mtime, sh, status = row
forced = fs_mtime - 12345
cur.execute("UPDATE conversions SET source_mtime=? WHERE id=?", (forced, rec_id))
conn.commit()
conn.close()
print('forced mtime', forced, 'fs', fs_mtime)

events = list(scanner.walk(ROOT))
hm = [e for e in events if e.get('type')=='hash_match_done' and 'S01E01' in e.get('full_path','')]
print('hash_match_done', len(hm))

conn = sqlite3.connect(DB)
cur = conn.cursor()
cur.execute("SELECT source_mtime FROM conversions WHERE id=?", (rec_id,))
after = cur.fetchone()[0]
conn.close()
print('after mtime', after)
print('synced', abs(after - fs_mtime) < 1.0)

# restore not needed if synced; force restore when not synced
if abs(after - fs_mtime) >= 1.0:
    conn = sqlite3.connect(DB)
    conn.execute("UPDATE conversions SET source_mtime=? WHERE id=?", (fs_mtime, rec_id))
    conn.commit()
    conn.close()
