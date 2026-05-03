import sqlite3, os, collections

db = os.path.join(os.path.dirname(__file__), "..", "conversions.db")
con = sqlite3.connect(db)
cur = con.cursor()

cur.execute("SELECT status, COUNT(*) FROM conversions GROUP BY status ORDER BY COUNT(*) DESC")
print("=== Status counts ===")
for row in cur.fetchall():
    print(f"  {row[0]}: {row[1]}")

cur.execute("SELECT source_path, error_tail FROM conversions WHERE status='failed' ORDER BY source_path")
rows = cur.fetchall()
print(f"\n=== {len(rows)} failed records ===")

buckets = collections.defaultdict(list)
for path, tail in rows:
    if not tail:
        buckets["(no error_tail)"].append(path)
        continue
    lines = [l.strip() for l in tail.strip().splitlines() if l.strip()]
    last = next(
        (l for l in reversed(lines) if not l.startswith("frame=") and "[out#" not in l),
        lines[-1] if lines else "(empty)"
    )
    buckets[last[:120]].append(path)

for msg, paths in sorted(buckets.items(), key=lambda x: -len(x[1])):
    print(f"\n[{len(paths)}x] {msg}")
    for p in paths:
        print(f"      {os.path.basename(p)}")

# Full tail for Hooligan (ffprobe crash) and aac failures
DETAIL = ["Hooligan", "La Blue Girl Volume 2", "Vicious 2"]
for keyword in DETAIL:
    cur.execute("SELECT source_path, error_tail FROM conversions WHERE status='failed' AND source_path LIKE ?", (f'%{keyword}%',))
    for path, tail in cur.fetchall():
        print(f"\n{'='*60}\n{os.path.basename(path)}\n{tail}")

con.close()
