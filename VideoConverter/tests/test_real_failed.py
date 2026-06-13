"""
Test the aac_mf pre-encode fix against real failed files.
Backs up each source file before conversion and restores if conversion fails.
"""
import sys
import os
import shutil
import threading

import pytest

if __name__ != "__main__":
    pytest.skip("manual real-file regression script; not a pytest test module", allow_module_level=True)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import converter

TEST_FILES = [
    # (path, description)  — skip Night Shift Nurses (corrupt source)
    (
        r"C:\Users\scott\Downloads\Anime\Hentai\Vicious 2 [h264].mkv",
        "Vicious 2 — 2-track vorbis, aac_mf crash",
    ),
    (
        r"C:\Users\scott\Downloads\Anime\Hentai\[HS]Forbidden_Love_1[XviD].mkv",
        "Forbidden Love 1 — vorbis, aac_mf crash",
    ),
    (
        r"C:\Users\scott\Downloads\Anime\Hentai\Nymphs of the Stratosphere Ep.3.mkv",
        "Nymphs Ep.3 — 2-track vorbis, aac_mf crash",
    ),
]

PASS = 0
FAIL = 0

for src_path, desc in TEST_FILES:
    if not os.path.exists(src_path):
        print(f"\n[SKIP] {desc}\n       File not found: {src_path}")
        continue

    bak_path = src_path + ".bak"
    output_dir = os.path.dirname(src_path)

    print(f"\n{'='*70}")
    print(f"[TEST] {desc}")
    print(f"       {src_path}")
    src_mb = os.path.getsize(src_path) / 1024 / 1024
    print(f"       Source size: {src_mb:.1f} MB")

    # --- Backup ---
    print(f"       Backing up → {bak_path}")
    shutil.copy2(src_path, bak_path)

    # --- Run conversion ---
    logs = []
    stop = threading.Event()
    try:
        result = converter.convert_video(
            input_path=src_path,
            output_dir=output_dir,
            anime_mode=True,
            quality=None,
            progress_cb=None,
            stop_event=stop,
            log=lambda msg: logs.append(msg),
        )
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}

    # --- Report ---
    if result["ok"]:
        out_path = result.get("output_path", "")
        out_mb = result.get("output_size_mb", 0)
        saved_pct = result.get("saved_pct", 0)
        enc = result.get("encoder_used", "")
        print(f"       PASS  encoder={enc}  output={out_mb:.1f}MB  saved={saved_pct}%")
        print(f"       Output: {out_path}")
        PASS += 1
        # Restore backup (keep converted output in place)
        print(f"       Restoring source backup → {src_path}")
        os.replace(bak_path, src_path)
        # Remove the converted output so the folder is clean
        if out_path and os.path.exists(out_path):
            out_norm = os.path.normpath(out_path)
            src_norm = os.path.normpath(src_path)
            if out_norm != src_norm:
                os.remove(out_path)
                print(f"       Removed test output: {out_path}")
    else:
        err = result.get("error", "unknown")
        print(f"       FAIL  error={err}")
        FAIL += 1
        # Restore backup
        if os.path.exists(bak_path):
            os.replace(bak_path, src_path)
            print(f"       Source restored from backup.")
        # Print last 20 log lines for diagnosis
        print("       --- Last 20 log lines ---")
        for line in logs[-20:]:
            print(f"         {line}")

print(f"\n{'='*70}")
print(f"Results: {PASS} passed, {FAIL} failed")
