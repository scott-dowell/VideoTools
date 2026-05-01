import sqlite3, os, glob, shutil

db = r"C:\VideoTools\VideoConverter\conversions.db"
con = sqlite3.connect(db)
cur = con.cursor()

print("=== Resetting stuck 'running' records to pending ===")
cur.execute("UPDATE conversions SET status='pending', started_at=NULL, error_tail=NULL WHERE status='running'")
print(f"Reset {cur.rowcount} record(s)")
con.commit()

print()
print("=== Remaining stuck 'running' records ===")
cur.execute("SELECT id, source_path, status, started_at, completed_at FROM conversions WHERE status='running'")
rows = cur.fetchall()
print(f"Count: {len(rows)}")
for r in rows:
    print(r)

print()
print("=== Asobi ep 11 ===")
cur.execute("SELECT id, source_path, status, started_at, completed_at FROM conversions WHERE source_path LIKE '%Asobi%11%' ORDER BY id DESC LIMIT 3")
for r in cur.fetchall():
    print(r)

print()
print("=== Great Guardians 07 ===")
cur.execute("SELECT id, source_path, status, started_at, completed_at FROM conversions WHERE source_path LIKE '%Great Guardians%07%' ORDER BY id DESC LIMIT 3")
for r in cur.fetchall():
    print(r)

print()
print("=== Temp working dir cleanup ===")
for d in ["C:\\Temp\\vc_working", "D:\\Temp\\vc_working", "E:\\Temp\\vc_working"]:
    if os.path.isdir(d):
        items = glob.glob(os.path.join(d, "*"))
        total_mb = sum(os.path.getsize(f) / 1024 / 1024 for f in items if os.path.isfile(f))
        print(f"  {d}: {len(items)} item(s), {total_mb:.1f} MB — removing...")
        for item in items:
            try:
                if os.path.isdir(item):
                    shutil.rmtree(item)
                else:
                    os.remove(item)
                print(f"    deleted: {os.path.basename(item)}")
            except Exception as e:
                print(f"    FAILED {os.path.basename(item)}: {e}")
    else:
        print(f"  {d}: not found")

con.close()
