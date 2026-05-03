import sqlite3
conn = sqlite3.connect(r'C:\VideoTools\VideoConverter\conversions.db')
row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='conversions'").fetchone()
print(row[0])
print()
# Also check indexes
for idx in conn.execute("SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='conversions'"):
    print(f"INDEX: {idx[0]}")
    print(idx[1])
    print()
conn.close()
