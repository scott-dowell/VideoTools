"""
End-to-end test: verify dvd_subtitle copy fix for Kounai ep2.
Backs up source, converts, ffprobes output to confirm bin_data track present.
"""
import sys, os, shutil, json, subprocess

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import converter

SRC = r"C:\Users\scott\Downloads\Anime\Hentai\Kounai_Shasei_-_2_-_Tales_of_Titillation_-_[MMMXXX](01d09390)(dub.sub_ru,en.ru).mkv"
BAK = SRC + ".bak"
OUT = SRC.replace(".mkv", ".mp4")

if not os.path.exists(SRC):
    pytest.skip(f"source not found: {SRC}", allow_module_level=True)

print(f"Backing up → {BAK}")
shutil.copy2(SRC, BAK)

import threading
try:
    result = converter.convert_video(
        input_path=SRC,
        output_dir=os.path.dirname(SRC),
        anime_mode=True,
        quality=None,
        progress_cb=None,
        stop_event=threading.Event(),
    )
finally:
    print(f"Restoring source backup → {SRC}")
    shutil.copy2(BAK, SRC)
    os.remove(BAK)

if not result["ok"]:
    print(f"FAIL: conversion failed: {result.get('error')}")
    sys.exit(1)

out_path = result["output_path"]
print(f"Output: {out_path}  ({result['output_size_mb']:.1f} MB, {result['saved_pct']:.0f}% saved)")

# Probe the output
r = subprocess.run(
    ["ffprobe", "-v", "quiet", "-print_format", "json",
     "-show_entries", "stream=index,codec_type,codec_name",
     "-show_entries", "stream_tags=language,title",
     out_path],
    capture_output=True, text=True, timeout=30,
)
streams = json.loads(r.stdout)["streams"]

print("\nOutput streams:")
for s in streams:
    tags = s.get("tags", {})
    print(f"  [{s['index']}] {s['codec_type']:<10} {s['codec_name']:<20} "
          f"lang={tags.get('language','')!r}  title={tags.get('title','')!r}")

sub_streams = [s for s in streams if s.get("codec_type") == "subtitle"]
data_streams = [s for s in streams if s.get("codec_type") == "data"]

eng_sub_present = any(
    s.get("tags", {}).get("language", "") in ("eng", "en")
    for s in sub_streams + data_streams
)

print()
if eng_sub_present:
    print(f"PASS: English subtitle/data track found in output")
else:
    print(f"FAIL: No English subtitle/data track in output — dvd_subtitle was dropped!")
    sys.exit(1)

# Clean up output
os.remove(out_path)
print(f"Removed test output.")
