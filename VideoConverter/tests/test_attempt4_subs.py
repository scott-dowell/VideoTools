"""
Simulate remux_to_mp4 attempt 4 for a given MKV:
  1. Extract each ASS text sub track to a temp SRT file (attempt 4 logic)
  2. Remux video+audio+extracted SRTs to a temp MP4 using -c copy / mov_text
  3. Probe the output and report subtitle streams

Usage: python test_attempt4_subs.py <path_to_mkv>
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

if __name__ != "__main__":
    pytest.skip("manual remux investigation script; not a pytest test module", allow_module_level=True)

if len(sys.argv) < 2:
    # Default to ep08
    INPUT = r"C:\Users\scott\Downloads\Anime\_Ao-chan Can't Study\Ao-chan Can't Study - 08 [6FA0FA94].mkv"
else:
    INPUT = sys.argv[1]

TEMP_DIR = r"C:\Temp\vc_test_attempt4"
os.makedirs(TEMP_DIR, exist_ok=True)

print(f"Input : {INPUT}")
print()

# ── Step 0: probe streams ────────────────────────────────────────────────────
probe = subprocess.run(
    ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", "-show_format", INPUT],
    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
)
probe_data = json.loads(probe.stdout)
streams = probe_data["streams"]
duration = float(probe_data.get("format", {}).get("duration", 0))

TEXT_SUB_CODECS = {"ass", "subrip", "srt", "webvtt", "mov_text"}
text_sub_indices = [
    s["index"] for s in streams
    if s.get("codec_type") == "subtitle"
    and s.get("codec_name", "").lower() in TEXT_SUB_CODECS
]
print(f"Text sub stream indices: {text_sub_indices}")
print(f"Video duration: {duration:.1f}s")
if not text_sub_indices:
    print("No text subs found — nothing to test.")
    sys.exit(0)

# ── Step 1: extract each ASS track to SRT ───────────────────────────────────
extracted_srts: list[str] = []
for i, si in enumerate(text_sub_indices):
    out_srt = os.path.join(TEMP_DIR, f"sub_{i}.srt")
    cmd = ["ffmpeg", "-y", "-i", INPUT, "-map", f"0:{si}", "-c:s", "srt", out_srt]
    print(f"Extracting stream #{si} → {out_srt}")
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=120)
    if r.returncode != 0 or not os.path.exists(out_srt) or os.path.getsize(out_srt) == 0:
        print(f"  FAILED (rc={r.returncode})")
        print(r.stderr[-500:])
        sys.exit(1)
    # Sanitize: spread same-timestamp cues by 1ms to prevent DTS loop
    import pysubs2
    subs = pysubs2.load(out_srt)
    subs.events.sort(key=lambda e: (e.start, e.end))
    prev_start = -1
    offset = 0
    for e in subs.events:
        if e.start == prev_start:
            offset += 1
            e.start += offset
            if e.end <= e.start:
                e.end = e.start + 1
        else:
            prev_start = e.start
            offset = 0
    subs.save(out_srt)
    if offset > 0:
        print(f"  Spread same-timestamp cues (max offset {offset} ms)")
    size_kb = os.path.getsize(out_srt) / 1024
    print(f"  OK  ({size_kb:.1f} KB)")
    extracted_srts.append(out_srt)

# ── Step 2: remux with extracted SRTs ────────────────────────────────────────
out_mp4 = os.path.join(TEMP_DIR, Path(INPUT).stem + "_attempt4.mp4")
cmd = ["ffmpeg", "-y", "-i", INPUT]
for srt in extracted_srts:
    cmd += ["-i", srt]

cmd += ["-map", "0:v:0", "-map", "0:a", "-c:v", "copy", "-c:a", "copy"]
for j in range(len(extracted_srts)):
    cmd += ["-map", f"{j+1}:s:0"]
cmd += ["-c:s", "mov_text", "-movflags", "+faststart", out_mp4]

print(f"\nRemuxing → {out_mp4}")
print(f"Command : {' '.join(cmd)}\n")
r2 = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=300)
print(r2.stdout[-1000:] if r2.stdout else "")
print(r2.stderr[-1000:] if r2.stderr else "")

if r2.returncode != 0 or not os.path.exists(out_mp4):
    print(f"\nFAILED (rc={r2.returncode})")
    sys.exit(1)

# ── Step 3: verify output streams ────────────────────────────────────────────
probe2 = subprocess.run(
    ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", out_mp4],
    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
)
out_streams = json.loads(probe2.stdout)["streams"]
out_subs = [s for s in out_streams if s.get("codec_type") == "subtitle"]

out_mb = os.path.getsize(out_mp4) / 1024 / 1024
print(f"\nOutput : {out_mp4}  ({out_mb:.1f} MB)")
print(f"Subtitle streams in output: {len(out_subs)}")
for s in out_subs:
    print(f"  #{s['index']} {s.get('codec_name','?')}  lang={s.get('tags',{}).get('language','')}")

if len(out_subs) == len(extracted_srts):
    print("\nPASS — attempt 4 would succeed for this file")
else:
    print(f"\nFAIL — expected {len(extracted_srts)} sub stream(s), got {len(out_subs)}")
