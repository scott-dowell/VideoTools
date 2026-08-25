import os
import sys
from pathlib import Path

sys.path.insert(0, r"c:/VideoTools/VideoConverter")
import converter

ROOT = Path(r"C:/Users/scott/Downloads/Anime")
EXTS = {".mp4", ".mkv", ".avi", ".mov"}

count = 0
ok = 0
for p in ROOT.rglob("*"):
    if not p.is_file() or p.suffix.lower() not in EXTS:
        continue
    count += 1
    r = converter.estimate(str(p), quality=30)
    if r.get("error"):
        print(f"FAIL|{p.name}|{r.get('error')}")
    else:
        ok += 1
        print(f"OK|{p.name}|pct={r.get('estimated_saving_pct')}|cv={r.get('sample_cv_pct')}|n={r.get('sample_count')}")
    if count >= 20:
        break

print(f"SUMMARY total_tested={count} ok={ok} fail={count-ok}")
