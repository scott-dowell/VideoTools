import sqlite3
db = r"C:\VideoTools\VideoConverter\conversions.db"
con = sqlite3.connect(db)
cur = con.cursor()
cur.execute("UPDATE conversions SET status='skipped' WHERE source_path LIKE '%Great Guardians%07%'")
print("Updated:", cur.rowcount, "record(s)")
con.commit()
cur.execute("SELECT id, source_path, status FROM conversions WHERE source_path LIKE '%Great Guardians%07%'")
for r in cur.fetchall():
    print(r)
con.close()
