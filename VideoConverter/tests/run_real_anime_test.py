"""
Real anime video test — Phase 3b checkpoint.
Run: .venv\Scripts\python.exe VideoConverter\tests\run_real_anime_test.py
"""
import sys, os, threading
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import converter

SRC    = r"C:\Users\scott\Downloads\Anime\Rosario to Vampire\Rosario to Vampire\[Anime Time] Rosario to Vampire - 01.mkv"
OUTDIR = r"C:\Temp\vc_test_out"

os.makedirs(OUTDIR, exist_ok=True)

stop = threading.Event()

def progress(pct, fps, eta):
    print(f"  {pct:.0f}%  fps={fps:.0f}  eta={eta}s", end="\r", flush=True)

print(f"Source : {SRC}")
print(f"is_hi10: {converter.is_hi10(SRC)}")
print("Running anime mode convert_video() ...")

result = converter.convert_video(
    input_path  = SRC,
    output_dir  = OUTDIR,
    anime_mode  = True,
    quality     = None,
    progress_cb = progress,
    stop_event  = stop,
    log         = lambda m: print(f"  LOG: {m}"),
)

print()
print("Result:", result)
