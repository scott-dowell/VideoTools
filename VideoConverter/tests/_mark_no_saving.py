import sqlite3, sys, os
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
path = sys.argv[1]
conn = sqlite3.connect("conversions.db")
sql = "UPDATE conversions SET status='no_saving', error_tail=NULL WHERE source_path=? AND status NOT IN ('done','skipped')"
cur = conn.execute(sql, (path,))
print(f"Updated {cur.rowcount} row(s) -> no_saving")
conn.commit()
conn.close()
