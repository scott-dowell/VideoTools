import sqlite3, os, sys
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

show = sys.argv[1] if len(sys.argv) > 1 else 'Gushing'
conn = sqlite3.connect("conversions.db")
rows = conn.execute(
    "SELECT id, status, source_path FROM conversions WHERE source_path LIKE ? ORDER BY id",
    (f"%{show}%",)
).fetchall()
for r in rows:
    print(r[0], r[1], r[2][-70:])
conn.close()
