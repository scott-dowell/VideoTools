"""
quality_compare.py — encode one source file at multiple QSV quality levels.

Usage:
    python quality_compare.py <source_file> [output_folder]

Produces:
    output_folder/
        original_copy.mp4          — bit-for-bit copy (for reference)
        q18_very_high.mp4
        q22_high.mp4
        q26_medium.mp4
        q30_low.mp4
        q34_very_low.mp4
"""

import os, shutil, subprocess, sys, time
from pathlib import Path

SOURCE = r"C:/Users/scott/Downloads/Anime/Gushing Over Magical Girls/Gushing Over Magical Girls - S01E01.mp4"
OUT_DIR = r"C:/Users/scott/Downloads/Anime/Gushing Over Magical Girls/quality_compare"

LEVELS = [
    (26, "medium"),
    (30, "low"),
    (34, "very_low"),
]

def run_encode(src: str, dst: str, quality: int) -> None:
    cmd = [
        "ffmpeg", "-y",
        "-i", src,
        "-c:v", "hevc_qsv",
        "-global_quality", str(quality),
        "-look_ahead", "1",
        "-c:a", "copy",
        "-c:s", "copy",
        dst,
    ]
    print(f"  Encoding q={quality} → {Path(dst).name} ...")
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.time() - t0
    if r.returncode != 0:
        print(f"  ERROR (rc={r.returncode}):")
        for line in r.stderr.splitlines()[-8:]:
            print("   ", line)
    else:
        size_mb = os.path.getsize(dst) / 1024 / 1024
        src_mb  = os.path.getsize(src)  / 1024 / 1024
        ratio   = size_mb / src_mb * 100
        print(f"  Done in {elapsed:.0f}s — {size_mb:.1f} MB ({ratio:.0f}% of source)")


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else SOURCE
    out = sys.argv[2] if len(sys.argv) > 2 else OUT_DIR

    src = os.path.normpath(src)
    out = os.path.normpath(out)

    if not os.path.isfile(src):
        print(f"Source not found: {src}")
        sys.exit(1)

    os.makedirs(out, exist_ok=True)

    # Copy original for side-by-side comparison
    orig_dst = os.path.join(out, "q00_original.mp4")
    if not os.path.exists(orig_dst):
        print(f"Copying original → {Path(orig_dst).name} ...")
        shutil.copy2(src, orig_dst)
        print(f"  {os.path.getsize(orig_dst)/1024/1024:.1f} MB")
    else:
        print(f"Original already copied, skipping.")

    for quality, label in LEVELS:
        dst = os.path.join(out, f"q{quality:02d}_{label}.mp4")
        if os.path.exists(dst):
            print(f"  Skipping q={quality} — already exists")
            continue
        run_encode(src, dst, quality)

    print("\nAll done. Files in:", out)
    # Show summary
    for f in sorted(os.listdir(out)):
        p = os.path.join(out, f)
        print(f"  {f:35s}  {os.path.getsize(p)/1024/1024:7.1f} MB")


if __name__ == "__main__":
    main()
