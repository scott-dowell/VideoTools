"""
test_tesseract_ocr.py
=====================
Side-by-side comparison of Tesseract vs EasyOCR for PGS subtitle extraction.

Extracts the first PGS stream from a test MKV, OCRs a sample of frames with
both engines, and prints the results + timing so we can evaluate quality.

Usage
-----
    # Default: Domestic Na Kanojo ep01
    python tests/test_tesseract_ocr.py

    # Any MKV with PGS subs
    python tests/test_tesseract_ocr.py "C:/path/to/video.mkv"

    # Limit frames to OCR (default 20)
    python tests/test_tesseract_ocr.py --frames 50

    # Tesseract only (skip EasyOCR to avoid loading PyTorch)
    python tests/test_tesseract_ocr.py --engine tesseract

    # EasyOCR only
    python tests/test_tesseract_ocr.py --engine easyocr
"""

from __future__ import annotations

import argparse
import hashlib
import os
import struct
import subprocess
import sys
import tempfile
import time

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(SCRIPT_DIR))

DEFAULT_MKV = r"C:\Users\scott\Downloads\Anime\_Domestic Girlfriend\Domestic Na Kanojo - 01.mkv"
TESSERACT_EXE = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# ---------------------------------------------------------------------------
# Dependency check
# ---------------------------------------------------------------------------
try:
    from PIL import Image
    import numpy as np
    import pysubs2
    _PILLOW_OK = True
except ImportError as e:
    print(f"ERROR: missing core deps: {e}")
    print("  pip install Pillow pysubs2 numpy")
    sys.exit(1)

try:
    import pytesseract
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_EXE
    _TESSERACT_OK = True
except ImportError:
    _TESSERACT_OK = False
    print("WARNING: pytesseract not installed — tesseract engine disabled")
    print("  pip install pytesseract")

try:
    import easyocr as _easyocr
    _EASYOCR_OK = True
except ImportError:
    _EASYOCR_OK = False


# ---------------------------------------------------------------------------
# Minimal PGS parser (copied from bitmap_subs.py)
# ---------------------------------------------------------------------------
SEG_PDS = 0x14
SEG_ODS = 0x15
SEG_PCS = 0x16
SEG_WDS = 0x17
SEG_END = 0x80
PGS_MAGIC = b'PG'


def _parse_pgs(data: bytes):
    """Yield (start_ms, end_ms, width, height, rgba_bytes) for each PGS frame."""
    pos = 0
    n = len(data)
    palettes: dict = {}
    objects: dict = {}
    obj_dims: dict = {}
    current_pts_ms = 0

    def _read_pts(raw: bytes) -> int:
        v = struct.unpack('>I', raw)[0]
        return int(v / 90)

    while pos + 13 <= n:
        if data[pos:pos+2] != PGS_MAGIC:
            pos += 1
            continue
        pts_ms = _read_pts(data[pos+2:pos+6])
        seg_type = data[pos+10]
        seg_len  = struct.unpack('>H', data[pos+11:pos+13])[0]
        payload  = data[pos+13:pos+13+seg_len]
        pos     += 13 + seg_len

        if seg_type == SEG_PCS:
            current_pts_ms = pts_ms
            palettes.clear(); objects.clear(); obj_dims.clear()

        elif seg_type == SEG_PDS:
            pal_id = payload[0]
            entries = {}
            i = 2
            while i + 4 < len(payload):
                idx = payload[i]
                y, cb, cr, a = payload[i+1], payload[i+2], payload[i+3], payload[i+4]
                r2 = int(max(0, min(255, y + 1.402   * (cr - 128))))
                g2 = int(max(0, min(255, y - 0.34414 * (cb - 128) - 0.71414 * (cr - 128))))
                b2 = int(max(0, min(255, y + 1.772   * (cb - 128))))
                entries[idx] = (r2, g2, b2, a)
                i += 5
            palettes[pal_id] = entries

        elif seg_type == SEG_ODS:
            if len(payload) < 7:
                continue
            obj_id = struct.unpack('>H', payload[0:2])[0]
            seq_flag = payload[3]
            if seq_flag & 0x80:
                if len(payload) < 11:
                    continue
                w = struct.unpack('>H', payload[7:9])[0]
                h = struct.unpack('>H', payload[9:11])[0]
                obj_dims[obj_id] = (w, h)
                objects[obj_id] = payload[11:]
            else:
                objects[obj_id] = objects.get(obj_id, b'') + payload[4:]

        elif seg_type == SEG_END:
            if not objects:
                continue
            for obj_id, rle_data in objects.items():
                if obj_id not in obj_dims:
                    continue
                w, h = obj_dims[obj_id]
                if w == 0 or h == 0 or w > 4096 or h > 2160:
                    continue
                # Find palette: use first available
                pal = next(iter(palettes.values()), {})
                rgba = _decode_rle(rle_data, w, h, pal)
                if rgba:
                    yield current_pts_ms, current_pts_ms + 3000, w, h, rgba


def _decode_rle(data: bytes, width: int, height: int, lookup: dict) -> bytes | None:
    pixels = bytearray(width * height * 4)
    x = y = 0
    i = 0
    n = len(data)

    def _put(buf, px, py, color):
        off = (py * width + px) * 4
        if off + 3 < len(buf):
            buf[off:off+4] = color

    while i < n and y < height:
        b1 = data[i]; i += 1
        if b1 != 0:
            color = lookup.get(b1, (0, 0, 0, 0))
            _put(pixels, x, y, color)
            x += 1
        else:
            if i >= n:
                break
            b2 = data[i]; i += 1
            if b2 == 0:
                x = 0; y += 1
            elif (b2 & 0xC0) == 0:
                x += b2 & 0x3F
            elif (b2 & 0xC0) == 0x40:
                b3 = data[i]; i += 1
                count = ((b2 & 0x3F) << 8) | b3
                x += count
            elif (b2 & 0xC0) == 0x80:
                b3 = data[i]; i += 1
                count = b2 & 0x3F
                color = lookup.get(b3, (0, 0, 0, 0))
                for _ in range(count):
                    if x < width:
                        _put(pixels, x, y, color); x += 1
            else:
                b3 = data[i]; i += 1
                b4 = data[i]; i += 1
                count = ((b2 & 0x3F) << 8) | b3
                color = lookup.get(b4, (0, 0, 0, 0))
                for _ in range(count):
                    if x < width:
                        _put(pixels, x, y, color); x += 1
    return bytes(pixels) if any(pixels) else None


# ---------------------------------------------------------------------------
# Image preprocessing
# ---------------------------------------------------------------------------
_MAX_W, _MAX_H = 480, 270


def _preprocess_for_easyocr(img: Image.Image) -> np.ndarray:
    """Black background, RGB numpy array — what EasyOCR expects."""
    bg = Image.new('RGBA', img.size, (0, 0, 0, 255))
    bg.paste(img, mask=img.split()[3])
    rgb = bg.convert('RGB')
    w, h = rgb.size
    scale = min(_MAX_W / w, _MAX_H / h, 1.0)
    if scale < 1.0:
        rgb = rgb.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
    w, h = rgb.size
    if h < 64:
        s = max(2, 64 // h)
        rgb = rgb.resize((w * s, h * s), Image.LANCZOS)
    return np.array(rgb)


def _preprocess_for_tesseract(img: Image.Image) -> Image.Image:
    """
    PGS text is white/yellow with a dark outline on a transparent background.
    1. Composite onto black  → white text visible, transparent becomes black
    2. Convert to greyscale
    3. Invert                → black text on white background (Tesseract's sweet spot)
    """
    bg = Image.new('RGBA', img.size, (0, 0, 0, 255))
    bg.paste(img, mask=img.split()[3])
    grey = bg.convert('L')
    from PIL import ImageOps
    grey = ImageOps.invert(grey)
    w, h = grey.size

    # Upscale to at least 60px tall for Tesseract accuracy
    if h < 60:
        scale = max(2, 60 // h)
        grey = grey.resize((w * scale, h * scale), Image.LANCZOS)
        w, h = grey.size

    # Cap at 1920px wide to avoid massive allocations
    if w > 1920:
        scale = 1920 / w
        grey = grey.resize((1920, max(1, round(h * scale))), Image.LANCZOS)

    return grey


# ---------------------------------------------------------------------------
# Tesseract OCR
# ---------------------------------------------------------------------------
def _ocr_tesseract(img: Image.Image) -> str:
    prepped = _preprocess_for_tesseract(img)
    w, h = img.size
    # PSM 6 = uniform block (good for 1-3 line subtitle bars)
    # PSM 3 = fully auto layout (better for multi-line lyric cards)
    # Heuristic: tall images (h > 2.5× the typical sub bar ~120px) are lyric cards
    psm = 3 if h > 300 else 6
    cfg = f'--psm {psm} --oem 1'
    raw = pytesseract.image_to_string(prepped, config=cfg, lang='eng')
    return raw.strip()


# ---------------------------------------------------------------------------
# EasyOCR OCR
# ---------------------------------------------------------------------------
_easyocr_reader = None

def _ocr_easyocr(img: Image.Image) -> str:
    global _easyocr_reader
    if _easyocr_reader is None:
        print("  [easyocr] Loading model (~500MB, may take 30s)...")
        import warnings
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore")
            _easyocr_reader = _easyocr.Reader(['en'], gpu=False, verbose=False)
    arr = _preprocess_for_easyocr(img)
    results = _easyocr_reader.readtext(arr, detail=1, paragraph=False)
    lines = [txt for (_bbox, txt, conf) in results if conf >= 0.3 and txt.strip()]
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Compare Tesseract vs EasyOCR on PGS subs")
    parser.add_argument("mkv", nargs="?", default=DEFAULT_MKV, help="Path to MKV with PGS subs")
    parser.add_argument("--frames", type=int, default=20, help="Max frames to OCR (default 20)")
    parser.add_argument("--engine", choices=["both", "tesseract", "easyocr"], default="both")
    args = parser.parse_args()

    if not os.path.exists(args.mkv):
        print(f"ERROR: file not found: {args.mkv}")
        sys.exit(1)

    run_tesseract = args.engine in ("both", "tesseract") and _TESSERACT_OK
    run_easyocr   = args.engine in ("both", "easyocr")   and _EASYOCR_OK

    if not run_tesseract and not run_easyocr:
        print("ERROR: no OCR engine available. Install pytesseract or easyocr.")
        sys.exit(1)

    print(f"Input : {args.mkv}")
    print(f"Engine: {args.engine}  (tesseract={run_tesseract}, easyocr={run_easyocr})")
    print(f"Frames: up to {args.frames}")
    print()

    # ── Probe for PGS streams ────────────────────────────────────────────────
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", args.mkv],
        capture_output=True, text=True, timeout=60,
    )
    import json
    streams = json.loads(probe.stdout).get("streams", [])
    PGS_CODECS = {"hdmv_pgs_subtitle", "pgssub"}
    pgs_streams = [
        s for s in streams
        if s.get("codec_type") == "subtitle"
        and s.get("codec_name", "").lower() in PGS_CODECS
    ]

    if not pgs_streams:
        print("No PGS subtitle streams found in this file.")
        sys.exit(0)

    stream = pgs_streams[0]
    idx    = stream["index"]
    lang   = stream.get("tags", {}).get("language", "?")
    print(f"Using stream #{idx}  codec={stream['codec_name']}  lang={lang}")
    print()

    # ── Extract .sup ─────────────────────────────────────────────────────────
    with tempfile.TemporaryDirectory(prefix="tess_test_") as tmpdir:
        sup_path = os.path.join(tmpdir, "sub.sup")
        print(f"Extracting stream #{idx} to .sup ...")
        r = subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-probesize", "100M", "-analyzeduration", "100M",
             "-i", args.mkv, "-map", f"0:{idx}", "-c:s", "copy", sup_path],
            timeout=300,
        )
        if r.returncode != 0 or not os.path.exists(sup_path):
            print(f"ERROR: ffmpeg extraction failed (rc={r.returncode})")
            sys.exit(1)

        size_kb = os.path.getsize(sup_path) / 1024
        print(f"Extracted {size_kb:.0f} KB")
        print()

        # ── Parse & sample frames ─────────────────────────────────────────────
        with open(sup_path, "rb") as f:
            sup_data = f.read()

        all_frames = list(_parse_pgs(sup_data))
        total = len(all_frames)
        print(f"Total frames in stream: {total}")

        # Sample evenly spaced frames across the stream
        if total <= args.frames:
            sample = all_frames
        else:
            step = total / args.frames
            sample = [all_frames[int(i * step)] for i in range(args.frames)]

        print(f"Sampling {len(sample)} frames\n")
        print("=" * 80)

        tess_time = easy_time = 0.0
        tess_empty = easy_empty = 0

        for frame_num, (start_ms, end_ms, w, h, rgba) in enumerate(sample):
            ts = f"{start_ms // 60000:02d}:{(start_ms % 60000) // 1000:02d}.{start_ms % 1000:03d}"
            img = Image.frombytes('RGBA', (w, h), rgba)

            tess_text = easy_text = None

            if run_tesseract:
                t0 = time.perf_counter()
                tess_text = _ocr_tesseract(img)
                tess_time += time.perf_counter() - t0
                if not tess_text:
                    tess_empty += 1

            if run_easyocr:
                t0 = time.perf_counter()
                easy_text = _ocr_easyocr(img)
                easy_time += time.perf_counter() - t0
                if not easy_text:
                    easy_empty += 1

            print(f"Frame {frame_num+1:3d}  t={ts}  size={w}x{h}")
            if run_tesseract:
                display = tess_text.replace('\n', ' / ') if tess_text else "<empty>"
                print(f"  TESS : {display}")
            if run_easyocr:
                display = easy_text.replace('\n', ' / ') if easy_text else "<empty>"
                print(f"  EASY : {display}")
            if run_tesseract and run_easyocr:
                match = "✓ match" if (tess_text or "").strip() == (easy_text or "").strip() else ""
                if match:
                    print(f"         {match}")
            print()

        print("=" * 80)
        print(f"SUMMARY  ({len(sample)} frames)")
        if run_tesseract:
            avg = tess_time / len(sample) if sample else 0
            print(f"  Tesseract : {tess_time:.2f}s total  {avg:.3f}s/frame  {tess_empty} empty")
        if run_easyocr:
            avg = easy_time / len(sample) if sample else 0
            print(f"  EasyOCR   : {easy_time:.2f}s total  {avg:.3f}s/frame  {easy_empty} empty")


if __name__ == "__main__":
    main()
