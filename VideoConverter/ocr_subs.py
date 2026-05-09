"""
ocr_subs.py
===========
Standalone PGS subtitle OCR tool.

Extracts every English PGS (bitmap) subtitle track from one or more MKV/MP4
files, OCRs each one with Tesseract, and saves the result as an SRT file
alongside the source video.

Output naming convention (understood by the VideoConverter pipeline):
    {video_stem}.pgs1.srt   — first OCR'd PGS track
    {video_stem}.pgs2.srt   — second OCR'd PGS track
    ...

When these files are present, VideoConverter's remux_to_mp4 step will use them
directly and skip its own OCR entirely, making conversions faster and more
reliable.

Usage
-----
    # OCR one file
    python ocr_subs.py "S01E01 About the Time I First Met Her.mkv"

    # OCR every file in a folder
    python ocr_subs.py "C:/Downloads/Anime/_3D Kanojo Real Girl/"

    # Output to a different directory
    python ocr_subs.py video.mkv --out-dir "C:/Temp/subs"

    # All streams, not just English
    python ocr_subs.py video.mkv --all-streams

    # Force re-OCR even if .pgs*.srt files already exist
    python ocr_subs.py video.mkv --force

    # Print each subtitle line as it is recognised
    python ocr_subs.py video.mkv --verbose

Requirements
------------
    pip install pytesseract Pillow pysubs2
    Tesseract binary: winget install UB-Mannheim.TesseractOCR
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


# ---------------------------------------------------------------------------
# Dependency check
# ---------------------------------------------------------------------------

_TESSERACT_EXE = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# Limit Tesseract to one OpenMP thread per call.  This prevents each spawned
# tesseract.exe from saturating all CPU cores and reduces memory-bus pressure,
# which matters when processing hundreds of subtitle frames in sequence.
# Must be set before the first subprocess is spawned so it is inherited.
os.environ.setdefault("OMP_THREAD_LIMIT", "1")


def _check_deps() -> None:
    missing = []
    for pkg in ("pytesseract", "PIL", "pysubs2"):
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg if pkg != "PIL" else "Pillow")
    if missing:
        print(f"ERROR: missing required packages: {', '.join(missing)}")
        print(f"  Install with:  pip install {' '.join(missing)}")
        sys.exit(1)
    import os as _os
    if not _os.path.exists(_TESSERACT_EXE):
        print(f"ERROR: Tesseract binary not found at: {_TESSERACT_EXE}")
        print("  Install with:  winget install UB-Mannheim.TesseractOCR")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Video probing
# ---------------------------------------------------------------------------

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".m2ts"}
PGS_CODECS = {"hdmv_pgs_subtitle", "pgssub"}


def _ffprobe_streams(path: str) -> list[dict]:
    r = subprocess.run(
        ["ffprobe", "-v", "quiet",
         "-probesize", "100M", "-analyzeduration", "100M",
         "-print_format", "json", "-show_streams", path],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    if r.returncode != 0:
        return []
    return json.loads(r.stdout).get("streams", [])


def _pgs_streams(streams: list[dict], all_streams: bool, skip_indices: frozenset = frozenset()) -> list[dict]:
    """Return PGS subtitle streams to process."""
    bitmap = [
        s for s in streams
        if s.get("codec_type") == "subtitle"
        and s.get("codec_name", "").lower() in PGS_CODECS
        and s.get("index", -1) not in skip_indices
    ]
    if not bitmap:
        return []
    if all_streams:
        return bitmap
    eng = [
        s for s in bitmap
        if s.get("tags", {}).get("language", "").lower() in ("en", "eng", "")
    ]
    return eng if eng else bitmap[:1]


# ---------------------------------------------------------------------------
# OCR helpers
# ---------------------------------------------------------------------------

def _preprocess(rgba_bytes: bytes, w: int, h: int) -> object:
    """
    Convert RGBA bytes to a greyscale PIL Image ready for Tesseract.
    1. Composite onto black  — white subtitle text stays visible
    2. Convert to greyscale
    3. Invert               — gives black text on white (Tesseract's sweet spot)
    4. Upscale if too small
    """
    from PIL import Image, ImageOps
    img = Image.frombytes("RGBA", (w, h), rgba_bytes)
    bg = Image.new("RGBA", img.size, (0, 0, 0, 255))
    bg.paste(img, mask=img.split()[3])
    grey = ImageOps.invert(bg.convert("L"))
    gw, gh = grey.size
    if gh < 60:
        scale = max(2, 60 // gh)
        grey = grey.resize((gw * scale, gh * scale), Image.LANCZOS)
        gw, gh = grey.size
    if gw > 1920:
        grey = grey.resize((1920, max(1, round(gh * 1920 / gw))), Image.LANCZOS)
    return grey


def _ocr_image(img: object, h_original: int) -> str:
    """Run Tesseract on a preprocessed PIL Image, return stripped text."""
    import pytesseract
    pytesseract.pytesseract.tesseract_cmd = _TESSERACT_EXE
    psm = 3 if h_original > 300 else 6
    raw = pytesseract.image_to_string(img, config=f"--psm {psm} --oem 1", lang="eng", timeout=30)
    import re as _re
    raw = _re.sub(r"(?<![A-Za-z])\|(?![A-Za-z])", "I", raw)
    return raw.strip()


def _fmt_ms(ms: int) -> str:
    s, ms = divmod(ms, 1000)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# ---------------------------------------------------------------------------
# PGS parser (self-contained copy so this script has no import from bitmap_subs)
# ---------------------------------------------------------------------------

import hashlib, struct

SEG_PDS = 0x14
SEG_ODS = 0x15
SEG_PCS = 0x16
SEG_WDS = 0x17
SEG_END = 0x80
PGS_MAGIC = b"PG"


def _parse_pgs(data: bytes):
    """Yield (start_ms, end_ms, w, h, rgba_bytes) for each PGS frame."""
    pos = 0
    n = len(data)
    palettes: dict = {}
    objects: dict = {}
    obj_dims: dict = {}
    current_pts_ms = 0
    pending_start: dict[int, int] = {}

    while pos + 13 <= n:
        if data[pos:pos + 2] != PGS_MAGIC:
            pos += 1
            continue
        pts_ticks = struct.unpack_from(">I", data, pos + 2)[0]
        seg_type  = data[pos + 10]
        seg_len   = struct.unpack_from(">H", data, pos + 11)[0]
        seg_data  = data[pos + 13: pos + 13 + seg_len]
        pos += 13 + seg_len
        pts_ms = pts_ticks // 90

        if seg_type == SEG_PCS:
            current_pts_ms = pts_ms
            if len(seg_data) >= 11 and seg_data[7] == 0x80:
                palettes.clear(); objects.clear(); obj_dims.clear()

        elif seg_type == SEG_PDS:
            if len(seg_data) < 2:
                continue
            pal_id = seg_data[0]
            entries: dict = {}
            j = 2
            try:
                while j + 4 <= len(seg_data) - 1:
                    eid = seg_data[j]
                    Y, Cr, Cb, A = seg_data[j+1], seg_data[j+2], seg_data[j+3], seg_data[j+4]
                    r = max(0, min(255, int(Y + 1.402 * (Cr - 128))))
                    g = max(0, min(255, int(Y - 0.344 * (Cb - 128) - 0.714 * (Cr - 128))))
                    b = max(0, min(255, int(Y + 1.772 * (Cb - 128))))
                    entries[eid] = (r, g, b, A)
                    j += 5
            except Exception:
                pass
            palettes[pal_id] = entries

        elif seg_type == SEG_ODS:
            if len(seg_data) < 4:
                continue
            obj_id = struct.unpack_from(">H", seg_data, 0)[0]
            seq_flag = seg_data[3]
            if seq_flag & 0x80:
                if len(seg_data) < 11:
                    continue
                w = struct.unpack_from(">H", seg_data, 7)[0]
                h = struct.unpack_from(">H", seg_data, 9)[0]
                obj_dims[obj_id] = (w, h)
                objects[obj_id] = bytearray(seg_data[11:])
            else:
                objects.setdefault(obj_id, bytearray())
                objects[obj_id].extend(seg_data[4:])

        elif seg_type == SEG_END:
            for obj_id, rle in list(objects.items()):
                dims = obj_dims.get(obj_id)
                if not dims:
                    continue
                w, h = dims
                if obj_id not in pending_start:
                    pending_start[obj_id] = current_pts_ms
                else:
                    start_ms = pending_start.pop(obj_id)
                    end_ms = current_pts_ms
                    if end_ms > start_ms:
                        rgba = _decode_rle(rle, w, h, palettes)
                        if rgba:
                            yield start_ms, end_ms, w, h, rgba

    for obj_id, start_ms in pending_start.items():
        rle  = objects.get(obj_id)
        dims = obj_dims.get(obj_id)
        if rle and dims:
            w, h = dims
            rgba = _decode_rle(rle, w, h, palettes)
            if rgba:
                yield start_ms, start_ms + 3000, w, h, rgba


def _put_pixel(pixels: bytearray, px: int, py: int, width: int, rgba: tuple) -> None:
    off = (py * width + px) * 4
    if off + 3 < len(pixels):
        pixels[off]   = rgba[0]; pixels[off+1] = rgba[1]
        pixels[off+2] = rgba[2]; pixels[off+3] = rgba[3]


def _decode_rle(data: bytes, width: int, height: int, palettes: dict) -> bytes | None:
    if width <= 0 or height <= 0 or width > 4096 or height > 4096:
        return None
    lookup: dict = {}
    for pal in palettes.values():
        lookup.update(pal)
    pixels = bytearray(width * height * 4)
    x = y = 0
    i = 0
    n = len(data)

    while i < n and y < height:
        b = data[i]; i += 1
        if b != 0:
            _put_pixel(pixels, x, y, width, lookup.get(b, (0, 0, 0, 0)))
            x += 1
        else:
            if i >= n:
                break
            b2 = data[i]; i += 1
            if b2 == 0:
                x = 0; y += 1
            elif (b2 & 0xC0) == 0x00:
                x += b2 & 0x3F
            elif (b2 & 0xC0) == 0x40:
                if i >= n: break
                b3 = data[i]; i += 1
                x += ((b2 & 0x3F) << 8) | b3
            elif (b2 & 0xC0) == 0x80:
                if i >= n: break
                color_idx = data[i]; i += 1
                count = b2 & 0x3F
                color = lookup.get(color_idx, (0, 0, 0, 0))
                for _ in range(count):
                    if x < width: _put_pixel(pixels, x, y, width, color)
                    x += 1
            else:
                if i + 1 >= n: break
                b3 = data[i]; i += 1
                color_idx = data[i]; i += 1
                count = ((b2 & 0x3F) << 8) | b3
                color = lookup.get(color_idx, (0, 0, 0, 0))
                for _ in range(count):
                    if x < width: _put_pixel(pixels, x, y, width, color)
                    x += 1

    return bytes(pixels)


# ---------------------------------------------------------------------------
# Core OCR
# ---------------------------------------------------------------------------

def ocr_sup(
    sup_path: str,
    verbose: bool = False,
) -> object:
    """OCR a .sup file with Tesseract. Returns a pysubs2.SSAFile."""
    import pysubs2
    import hashlib as _hashlib

    with open(sup_path, "rb") as f:
        data = f.read()

    frames = list(_parse_pgs(data))
    total  = len(frames)
    subs   = pysubs2.SSAFile()
    cache: dict[bytes, str] = {}
    done   = skipped = deduped = 0
    t0     = time.monotonic()

    for idx, (start_ms, end_ms, w, h, rgba) in enumerate(frames, 1):
        img_hash = _hashlib.md5(rgba, usedforsecurity=False).digest()
        try:
            if img_hash in cache:
                text = cache[img_hash]
                deduped += 1
            else:
                img  = _preprocess(rgba, w, h)
                text = _ocr_image(img, h)
                cache[img_hash] = text

            if text:
                subs.append(pysubs2.SSAEvent(start=start_ms, end=end_ms, text=text))
                done += 1
                if verbose:
                    preview = text.replace("\n", " / ")
                    print(f"    [{_fmt_ms(start_ms)}] {preview[:80]}")
            else:
                skipped += 1
        except Exception as exc:
            skipped += 1
            if verbose:
                print(f"    OCR error at {_fmt_ms(start_ms)}: {exc}")

        # Progress: update every 10 frames or on last frame
        if idx % 10 == 0 or idx == total:
            elapsed  = time.monotonic() - t0
            rate     = idx / elapsed if elapsed > 0 else 0
            eta      = (total - idx) / rate if rate > 0 else 0
            eta_str  = f"{int(eta // 60)}m{int(eta % 60):02d}s" if eta > 0 else "done"
            pct      = int(100 * idx / total)
            print(f"\r    {pct:3d}% ({idx}/{total})  {rate:.1f} fr/s  ETA {eta_str}  "
                  f"{done} lines, {skipped} empty, {deduped} dupes", end="", flush=True)

    print()  # newline after progress line
    return subs


# ---------------------------------------------------------------------------
# Single-file processor
# ---------------------------------------------------------------------------

def process_video(
    video_path: str,
    out_dir: str | None,
    all_streams: bool,
    force: bool,
    verbose: bool,
    lang: str = "en",
    skip_indices: frozenset = frozenset(),
) -> tuple[int, int]:
    """
    OCR the PGS streams in video_path.
    Returns (tracks_written, tracks_skipped).
    """
    stem     = Path(video_path).stem
    out_base = out_dir or str(Path(video_path).parent)
    os.makedirs(out_base, exist_ok=True)

    # Probe streams
    try:
        streams = _ffprobe_streams(video_path)
    except Exception as exc:
        print(f"ERROR: ffprobe failed: {exc}")
        return 0, 0

    candidates = _pgs_streams(streams, all_streams, skip_indices)
    if not candidates:
        print("No PGS subtitle streams found.")
        return 0, 0

    written = skipped_existing = 0

    with tempfile.TemporaryDirectory(prefix="ocr_subs_") as tmpdir:
        for track_num, stream in enumerate(candidates, 1):
            stream_idx = stream["index"]
            lang_tag   = stream.get("tags", {}).get("language", "eng") or "eng"
            title      = stream.get("tags", {}).get("title", "")
            n_frames   = int(stream.get("tags", {}).get("NUMBER_OF_FRAMES", 0))

            out_srt = os.path.join(out_base, f"{stem}.pgs{track_num}.srt")

            # Build a short label for this track used in all status lines
            _track_label = (f"PGS {track_num}/{len(candidates)}"
                            f" [stream #{stream_idx}, {lang_tag}]"
                            + (f" '{title}'" if title else "")
                            + (f" ~{n_frames} frames" if n_frames else ""))

            # Skip if already done
            if os.path.exists(out_srt) and not force:
                print(f"{_track_label}: already exists ({Path(out_srt).name}) — use --force to redo")
                skipped_existing += 1
                continue

            print(f"{_track_label}: extracting...", end=" ", flush=True)

            # Extract .sup
            sup_path = os.path.join(tmpdir, f"sub_{stream_idx}.sup")
            ext_r = subprocess.run(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-probesize", "100M", "-analyzeduration", "100M",
                 "-i", video_path,
                 "-map", f"0:{stream_idx}", "-c:s", "copy", sup_path],
                capture_output=True, text=True, timeout=300,
            )
            if ext_r.returncode != 0 or not os.path.exists(sup_path) or os.path.getsize(sup_path) == 0:
                print("FAILED")
                if ext_r.stderr.strip():
                    print(f"  ffmpeg: {ext_r.stderr.strip()[-200:]}")
                continue
            sup_size_kb = os.path.getsize(sup_path) / 1024
            print(f"OK ({sup_size_kb:.0f} KB) | OCR-ing...", end=" ", flush=True)

            # OCR
            try:
                subs = ocr_sup(sup_path, verbose=verbose)
            except Exception as exc:
                print(f"FAILED")
                print(f"  ERROR: {exc}")
                import traceback
                traceback.print_exc()
                continue

            if len(subs) == 0:
                print("FAILED (no lines recognised)")
                continue

            subs.save(out_srt, format_="srt")
            size_kb = os.path.getsize(out_srt) / 1024
            print(f"OK: {len(subs)} lines -> {Path(out_srt).name} ({size_kb:.1f} KB)")
            written += 1

    return written, skipped_existing


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _collect_files(paths: list[str]) -> list[str]:
    """Expand paths: files are returned directly; directories are searched."""
    result: list[str] = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            for f in sorted(path.rglob("*")):
                if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS:
                    result.append(str(f))
        elif path.is_file():
            result.append(str(path))
        else:
            print(f"WARNING: path not found: {p}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="OCR PGS (bitmap) subtitles from MKV/MP4 files to SRT.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
        fromfile_prefix_chars="@",
    )
    parser.add_argument("inputs", nargs="+", metavar="FILE_OR_DIR",
                        help="Video file(s) or folder(s) to process")
    parser.add_argument("--out-dir", metavar="DIR",
                        help="Write .srt files here instead of alongside the source")
    parser.add_argument("--lang", default="en",
                        help="Tesseract language code (default: en)")
    parser.add_argument("--all-streams", action="store_true",
                        help="Process all PGS streams, not just English ones")
    parser.add_argument("--force", action="store_true",
                        help="Re-OCR even if .pgsN.srt files already exist")
    parser.add_argument("--verbose", action="store_true",
                        help="Print each recognised subtitle line")
    parser.add_argument("--skip-manifest", metavar="FILE",
                        help="JSON file mapping video paths to lists of stream indices to skip")
    args = parser.parse_args()

    _check_deps()

    # Build per-file skip index map from manifest (if provided)
    _skip_map: dict[str, frozenset[int]] = {}
    if args.skip_manifest:
        try:
            import json as _json
            with open(args.skip_manifest, "r", encoding="utf-8") as _mf:
                _raw = _json.load(_mf)
            for _p, _idxs in (_raw or {}).items():
                _skip_map[str(Path(_p))] = frozenset(int(i) for i in (_idxs or []))
        except Exception as _exc:
            print(f"WARNING: could not read skip manifest: {_exc}")

    files = _collect_files(args.inputs)
    if not files:
        print("No video files found.")
        sys.exit(1)

    multi_file = len(files) > 1
    if multi_file:
        print(f"Found {len(files)} file(s) to process.")
    t_start    = time.monotonic()
    total_written = total_skipped = total_files = 0

    for video_path in files:
        total_files += 1
        if multi_file:
            print(f"\n{'-' * 60}")
            print(f"  {Path(video_path).name}")
            print(f"{'-' * 60}")
        written, skipped = process_video(
            video_path=video_path,
            out_dir=args.out_dir,
            all_streams=args.all_streams,
            force=args.force,
            verbose=args.verbose,
            lang=args.lang,
            skip_indices=_skip_map.get(str(Path(video_path)), frozenset()),
        )
        total_written += written
        total_skipped += skipped

    if multi_file:
        elapsed = time.monotonic() - t_start
        print(f"\n{'=' * 60}")
        print(f"  Done in {int(elapsed // 60)}m{int(elapsed % 60):02d}s")
        print(f"  {total_written} SRT file(s) written, {total_skipped} already existed")
        print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
