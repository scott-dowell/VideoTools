import sqlite3
conn = sqlite3.connect('VideoConverter/conversions.db')
conn.execute("DELETE FROM conversions WHERE source_path LIKE '%fixtures%'")
conn.commit()
remaining = conn.execute('SELECT COUNT(*) FROM conversions').fetchone()[0]
print(f'DB cleared: {remaining} records remaining')
conn.close()
