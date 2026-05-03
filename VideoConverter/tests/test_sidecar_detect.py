"""Quick smoke-test for sidecar subtitle detection logic."""
from pathlib import Path

TEST_DIR = Path(r"C:\Users\scott\Downloads\Anime\Queen's Blade\12. Queen's Blade - Unlimited")
EXT_SUB_EXTS = {".srt", ".ass", ".ssa"}

found_any = False
for mkv in sorted(TEST_DIR.glob("*.mkv")):
    stem_lower = mkv.stem.lower()
    sidecars = [
        p.name
        for p in sorted(TEST_DIR.iterdir())
        if p.suffix.lower() in EXT_SUB_EXTS and p.stem.lower() == stem_lower
    ]
    tag = "OK" if sidecars else "  "
    print(f"[{tag}] {mkv.name}  ->  {sidecars}")
    if sidecars:
        found_any = True

print()
if found_any:
    print("PASS: sidecar detection matched at least one file")
else:
    print("FAIL: no sidecars detected — check folder contents")
