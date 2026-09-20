import sqlite3
DB = r'c:\VideoTools\VideoConverter\conversions.db'
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
cur = conn.cursor()
files = [
    'Ah! My Buddha 01 (CB8C790A).mp4',
    'Ah! My Buddha 02 (53E3FCD6).mp4',
    'Ah! My Buddha 03 (6333BECD).mp4',
]
for name in files:
    print('\n===', name, '===')
    cur.execute('''
        SELECT id, source_path, source_mtime, status, started_at, completed_at,
               source_bitrate_kbps, output_bitrate_kbps, source_duration_secs,
               output_size_mb, source_size_mb, output_path, source_hash, output_hash,
               saved_mb, saved_pct
        FROM conversions
        WHERE source_path LIKE ?
        ORDER BY id DESC
    ''', ('%' + name,))
    rows = cur.fetchall()
    print('rows:', len(rows))
    for r in rows[:10]:
        d = dict(r)
        print({k: d[k] for k in ['id','status','source_path','source_mtime','completed_at','source_bitrate_kbps','output_bitrate_kbps','source_duration_secs','output_size_mb','output_path','saved_mb','saved_pct']})

conn.close()
