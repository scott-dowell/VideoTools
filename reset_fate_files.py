"""Reset 3 failed Fate files to pending and enable pretrim_to_video_end setting."""

import sqlite3
import json

# 1. Reset the 3 failed files to pending
conn = sqlite3.connect('VideoConverter/conversions.db')
cur = conn.cursor()

failed_ids = [91755, 91754, 91753]
for fid in failed_ids:
    cur.execute("UPDATE conversions SET status = 'pending' WHERE id = ?", (fid,))

conn.commit()
print(f"✓ Reset {len(failed_ids)} files to pending status: {failed_ids}")

# Verify
rows = cur.execute(
    "SELECT id, source_path, status FROM conversions WHERE id IN (?, ?, ?)",
    tuple(failed_ids)
).fetchall()
for r in rows:
    fname = r[1].split('\\')[-1] if '\\' in r[1] else r[1].split('/')[-1]
    print(f"  ID {r[0]}: {fname} → status={r[2]}")

conn.close()

# 2. Enable pretrim_to_video_end in settings
settings_path = 'VideoConverter/settings.json'
with open(settings_path, 'r') as f:
    settings = json.load(f)

old_value = settings.get('pretrim_to_video_end')
settings['pretrim_to_video_end'] = True

with open(settings_path, 'w') as f:
    json.dump(settings, f, indent=2)

print(f"\n✓ Updated settings: pretrim_to_video_end {old_value} → True")
print(f"  Files are now pending and will reconvert with audio trim enabled")
