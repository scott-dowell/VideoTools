"""
bitmap_subs.py
==============
Extract bitmap subtitle streams (PGS/VOBSUB) from an MKV, OCR each subtitle
image with EasyOCR, and return an SRT-compatible pysubs2 file.

Public API
----------
    ocr_bitmap_subs_to_srt(input_path, lang, out_dir, all_streams, verbose, log_fn)
        -> list[str]   # paths to generated .srt files

Requirements
------------
    pip install easyocr Pillow pysubs2

Ported from convert_bitmap_subs.py (VideoConversion project).
CLI entry-point removed; call ocr_bitmap_subs_to_srt() directly.
"""

import hashlib
import json
import os
import struct
import subprocess
import tempfile
from pathlib import Path

try:
    from PIL import Image
    import easyocr as _easyocr
    import numpy as _np
    import pysubs2
    DEPS_OK = True
except ImportError as _e:
    DEPS_OK = False
    _MISSING = str(_e)

_easyocr_reader = None  # lazy singleton — loading the model is expensive


def _get_easyocr_reader(lang: str = 'en', log_fn=print):
    global _easyocr_reader
    if _easyocr_reader is None:
        log_fn("  Loading EasyOCR model (first run downloads ~500 MB)...")
        import warnings
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message=".*pin_memory.*no accelerator.*",
                                    category=UserWarning)
            _easyocr_reader = _easyocr.Reader([lang], gpu=False, verbose=False)
    return _easyocr_reader


# ---------------------------------------------------------------------------
# PGS (.sup) parser — self-contained, no external library needed
# ---------------------------------------------------------------------------

SEG_PDS = 0x14   # Palette Definition
SEG_ODS = 0x15   # Object Definition
SEG_PCS = 0x16   # Presentation Composition
SEG_WDS = 0x17   # Window Definition
SEG_END = 0x80   # End of Display Set

PGS_MAGIC = b'PG'


def _parse_pgs(data: bytes):
    """
    Parse a PGS .sup binary blob.
    Yields (start_ms, end_ms, width, height, rgba_bytes) for each complete subtitle event.

    start_ms / end_ms are milliseconds.
    rgba_bytes is a flat RGBA bytes object of width*height*4 bytes.
    """
    pos = 0
    n = len(data)

    palettes = {}
    objects = {}
    obj_dims = {}
    current_pts_ms = 0
    pending_start: dict[int, int] = {}

    while pos + 13 <= n:
        if data[pos:pos+2] != PGS_MAGIC:
            pos += 1
            continue

        pts_ticks = struct.unpack_from('>I', data, pos + 2)[0]
        seg_type = data[pos + 10]
        seg_len = struct.unpack_from('>H', data, pos + 11)[0]
        seg_data = data[pos + 13: pos + 13 + seg_len]
        pos += 13 + seg_len

        pts_ms = pts_ticks // 90

        if seg_type == SEG_PCS:
            current_pts_ms = pts_ms
            if len(seg_data) < 11:
                continue
            comp_state = seg_data[7]
            if comp_state == 0x80:
                palettes.clear()
                objects.clear()
                obj_dims.clear()

        elif seg_type == SEG_PDS:
            if len(seg_data) < 2:
                continue
            pal_id = seg_data[0]
            entries = {}
            i = 2
            while i + 4 < len(seg_data):
                entry_id = seg_data[i]
                Y  = seg_data[i+1]
                Cr = seg_data[i+2]
                Cb = seg_data[i+3]
                A  = seg_data[i+4]
                r = int(Y + 1.40200 * (Cr - 128))
                g = int(Y - 0.34414 * (Cb - 128) - 0.71414 * (Cr - 128))
                b = int(Y + 1.77200 * (Cb - 128))
                r = max(0, min(255, r))
                g = max(0, min(255, g))
                b = max(0, min(255, b))
                entries[entry_id] = (r, g, b, A)
                i += 5
            palettes[pal_id] = entries

        elif seg_type == SEG_ODS:
            if len(seg_data) < 4:
                continue
            obj_id = struct.unpack_from('>H', seg_data, 0)[0]
            seq_flag = seg_data[3]
            is_first = bool(seq_flag & 0x80)
            if is_first:
                if len(seg_data) < 11:
                    continue
                w = struct.unpack_from('>H', seg_data, 7)[0]
                h = struct.unpack_from('>H', seg_data, 9)[0]
                obj_dims[obj_id] = (w, h)
                objects[obj_id] = bytearray(seg_data[11:])
            else:
                objects.setdefault(obj_id, bytearray())
                objects[obj_id].extend(seg_data[4:])

        elif seg_type == SEG_END:
            for obj_id, rle in objects.items():
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
        rle = objects.get(obj_id)
        dims = obj_dims.get(obj_id)
        if not rle or not dims:
            continue
        w, h = dims
        rgba = _decode_rle(rle, w, h, palettes)
        if rgba:
            yield start_ms, start_ms + 3000, w, h, rgba


def _decode_rle(data: bytes, width: int, height: int, palettes: dict) -> bytes | None:
    """Decode PGS run-length encoded pixel data to RGBA bytes."""
    if width <= 0 or height <= 0 or width > 4096 or height > 4096:
        return None
    lookup: dict[int, tuple] = {}
    for pal in palettes.values():
        lookup.update(pal)

    pixels = bytearray(width * height * 4)
    x = y = 0
    i = 0
    n = len(data)

    while i < n and y < height:
        b = data[i]; i += 1
        if b != 0:
            _put(pixels, x, y, width, lookup.get(b, (0, 0, 0, 0)))
            x += 1
        else:
            if i >= n:
                break
            b2 = data[i]; i += 1
            if b2 == 0:
                x = 0
                y += 1
            elif (b2 & 0xC0) == 0x00:
                count = b2 & 0x3F
                x += count
            elif (b2 & 0xC0) == 0x40:
                if i >= n:
                    break
                b3 = data[i]; i += 1
                count = ((b2 & 0x3F) << 8) | b3
                x += count
            elif (b2 & 0xC0) == 0x80:
                if i >= n:
                    break
                color_idx = data[i]; i += 1
                count = b2 & 0x3F
                color = lookup.get(color_idx, (0, 0, 0, 0))
                for _ in range(count):
                    if x < width:
                        _put(pixels, x, y, width, color)
                        x += 1
            else:
                if i + 1 >= n:
                    break
                b3 = data[i]; i += 1
                color_idx = data[i]; i += 1
                count = ((b2 & 0x3F) << 8) | b3
                color = lookup.get(color_idx, (0, 0, 0, 0))
                for _ in range(count):
                    if x < width:
                        _put(pixels, x, y, width, color)
                        x += 1

    return bytes(pixels)


def _put(buf: bytearray, x: int, y: int, width: int, rgba: tuple):
    offset = (y * width + x) * 4
    if offset + 3 >= len(buf):
        return
    buf[offset]   = rgba[0]; buf[offset+1] = rgba[1]
    buf[offset+2] = rgba[2]; buf[offset+3] = rgba[3] if len(rgba) > 3 else 255


# ---------------------------------------------------------------------------
# Reading-order reconstruction
# ---------------------------------------------------------------------------

def _reading_order(results: list, line_merge_threshold: float = 0.5) -> str:
    """
    Convert EasyOCR results (list of (bbox, text, conf)) to a properly ordered
    string, sorted top-to-bottom then left-to-right within each line.
    """
    if not results:
        return ''

    items = []
    for (bbox, text, _conf) in results:
        top_y  = min(pt[1] for pt in bbox)
        bot_y  = max(pt[1] for pt in bbox)
        left_x = min(pt[0] for pt in bbox)
        items.append((top_y, bot_y, left_x, text))

    items.sort(key=lambda t: (t[0], t[2]))

    lines: list[list[tuple]] = []
    for item in items:
        top_y, bot_y, left_x, text = item
        placed = False
        for line in lines:
            l_top, l_bot = line[0][0], line[0][1]
            overlap = min(bot_y, l_bot) - max(top_y, l_top)
            shorter = min(bot_y - top_y, l_bot - l_top)
            if shorter > 0 and overlap / shorter >= line_merge_threshold:
                line.append(item)
                placed = True
                break
        if not placed:
            lines.append([item])

    text_lines = []
    for line in lines:
        line.sort(key=lambda t: t[2])
        text_lines.append(' '.join(t[3] for t in line))

    return '\n'.join(text_lines)


# ---------------------------------------------------------------------------
# Image pre-processing
# ---------------------------------------------------------------------------

def _preprocess(img: 'Image.Image') -> '_np.ndarray':
    """
    Composite RGBA subtitle bitmap onto a black background and return as RGB
    numpy array for EasyOCR.  Upscales if the image height is very small.
    """
    bg = Image.new('RGBA', img.size, (0, 0, 0, 255))
    bg.paste(img, mask=img.split()[3])
    rgb = bg.convert('RGB')
    w, h = rgb.size
    if h < 64:
        scale = max(2, 64 // h)
        rgb = rgb.resize((w * scale, h * scale), Image.LANCZOS)
    return _np.array(rgb)


# ---------------------------------------------------------------------------
# Core OCR function
# ---------------------------------------------------------------------------

def ocr_sup_file(
    sup_path: str,
    lang: str = 'en',
    confidence: float = 0.3,
    verbose: bool = False,
    log_fn=print,
) -> 'pysubs2.SSAFile':
    """
    Parse a .sup PGS file and OCR every subtitle frame with EasyOCR.
    Returns a pysubs2.SSAFile ready to save as .srt.
    """
    reader = _get_easyocr_reader(lang, log_fn=log_fn)

    with open(sup_path, 'rb') as f:
        data = f.read()

    subs = pysubs2.SSAFile()
    count = skipped = deduped = 0
    _ocr_cache: dict[bytes, str] = {}

    for start_ms, end_ms, w, h, rgba in _parse_pgs(data):
        try:
            img_hash = hashlib.md5(rgba, usedforsecurity=False).digest()
            if img_hash in _ocr_cache:
                text = _ocr_cache[img_hash]
                deduped += 1
            else:
                img  = Image.frombytes('RGBA', (w, h), rgba)
                arr  = _preprocess(img)
                raw  = reader.readtext(arr, detail=1, paragraph=False)
                hits = [(bbox, txt, conf) for (bbox, txt, conf) in raw
                        if conf >= confidence and txt.strip()]
                text = _reading_order(hits)
                _ocr_cache[img_hash] = text
            if text:
                subs.append(pysubs2.SSAEvent(start=start_ms, end=end_ms, text=text))
                count += 1
                if verbose:
                    preview = text.replace('\n', ' / ')
                    log_fn(f"  [{_fmt_ms(start_ms)} --> {_fmt_ms(end_ms)}] {preview[:80]}")
            else:
                skipped += 1
        except Exception as e:
            skipped += 1
            if verbose:
                log_fn(f"  OCR error at {_fmt_ms(start_ms)}: {e}")

    if verbose:
        log_fn(f"  Parsed {count} subtitle lines, skipped {skipped} empty/low-confidence"
               + (f", {deduped} duplicate frames skipped (cache hits)" if deduped else ""))
    return subs


def _fmt_ms(ms: int) -> str:
    s, ms = divmod(ms, 1000)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# ---------------------------------------------------------------------------
# High-level entry point: process one video → all PGS tracks → SRT files
# ---------------------------------------------------------------------------

def ocr_bitmap_subs_to_srt(
    input_path: str,
    lang: str = 'en',
    out_dir: str | None = None,
    all_streams: bool = False,
    verbose: bool = False,
    log_fn=print,
) -> list[str]:
    """
    Extract and OCR all PGS subtitle streams from input_path using EasyOCR.

    Returns list of paths to generated .srt files (one per stream).
    Empty list means no bitmap subtitle streams found or all failed.
    """
    if not DEPS_OK:
        log_fn(f"ERROR: Missing dependencies: {_MISSING}")
        return []

    try:
        probe = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_streams', input_path],
            capture_output=True, text=True, timeout=30, check=True
        )
        streams = json.loads(probe.stdout).get('streams', [])
    except Exception as e:
        log_fn(f"ERROR: ffprobe failed: {e}")
        return []

    BITMAP_CODECS = {'hdmv_pgs_subtitle', 'pgssub', 'dvd_subtitle', 'dvdsub', 'xsub'}
    bitmap_streams = [
        s for s in streams
        if s.get('codec_type') == 'subtitle'
        and s.get('codec_name', '').lower() in BITMAP_CODECS
    ]

    if not bitmap_streams:
        if verbose:
            log_fn("No bitmap subtitle streams found.")
        return []

    if not all_streams:
        eng = [s for s in bitmap_streams
               if s.get('tags', {}).get('language', '').lower() in ('en', 'eng')]
        bitmap_streams = eng if eng else bitmap_streams[:1]

    if verbose:
        log_fn(f"Found {len(bitmap_streams)} bitmap subtitle stream(s) to process:")
        for s in bitmap_streams:
            lang_tag = s.get('tags', {}).get('language', 'unknown')
            log_fn(f"  Stream #{s['index']} ({s['codec_name']}) lang={lang_tag}")

    base_stem = Path(input_path).stem
    out_base  = out_dir or str(Path(input_path).parent)
    os.makedirs(out_base, exist_ok=True)
    srt_paths = []

    with tempfile.TemporaryDirectory(prefix='pgs_ocr_') as tmpdir:
        for i, stream in enumerate(bitmap_streams):
            stream_idx = stream['index']
            lang_tag   = stream.get('tags', {}).get('language', f'track{i}')
            sup_path   = os.path.join(tmpdir, f"sub_{stream_idx}.sup")

            if verbose:
                log_fn(f"\nExtracting stream #{stream_idx} ...")
            try:
                subprocess.run(
                    ['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
                     '-i', input_path, '-map', f'0:{stream_idx}', '-c:s', 'copy', sup_path],
                    check=True, timeout=300
                )
            except subprocess.CalledProcessError as e:
                log_fn(f"  ERROR: ffmpeg extraction failed for stream #{stream_idx}: {e}")
                continue

            if not os.path.exists(sup_path) or os.path.getsize(sup_path) == 0:
                log_fn(f"  ERROR: extracted file is empty for stream #{stream_idx}")
                continue

            if verbose:
                size_kb = os.path.getsize(sup_path) / 1024
                log_fn(f"  Extracted {size_kb:.0f} KB. Running EasyOCR (lang={lang}) ...")

            try:
                subs = ocr_sup_file(sup_path, lang=lang, verbose=verbose, log_fn=log_fn)
            except Exception as e:
                log_fn(f"  ERROR: OCR failed for stream #{stream_idx}: {e}")
                continue

            if len(subs) == 0:
                log_fn(f"  WARNING: no subtitle lines produced for stream #{stream_idx}")
                continue

            suffix   = f".{lang_tag}.s{stream_idx}" if len(bitmap_streams) > 1 else f".{lang_tag}"
            srt_path = os.path.join(out_base, f"{base_stem}{suffix}.srt")
            subs.save(srt_path, format_='srt')
            srt_paths.append(srt_path)
            if verbose:
                log_fn(f"  Saved {len(subs)} lines -> {srt_path}")

    return srt_paths


# ---------------------------------------------------------------------------
# Subprocess worker entry-point
#
# One-shot mode (legacy):
#   python bitmap_subs.py <input_path> <out_dir> <lang> <result_json>
#   Writes JSON list of SRT paths to <result_json> on success.
#
# Persistent server mode (preferred — loads model once, handles many jobs):
#   python bitmap_subs.py --server
#   Reads newline-delimited JSON job objects from stdin.
#   Writes newline-delimited JSON result objects to stdout.
#   Log/progress messages go to stderr so stdout stays clean.
#   Job schema:  {"input_path": str, "out_dir": str, "lang": str,
#                 "all_streams": bool, "verbose": bool}
#   Result schema: {"ok": true, "paths": [...]}
#              or  {"ok": false, "error": "..."}
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys as _sys

    if len(_sys.argv) == 2 and _sys.argv[1] == "--server":
        import json as _json
        _log = lambda msg: print(msg, file=_sys.stderr, flush=True)
        for _line in _sys.stdin:
            _line = _line.strip()
            if not _line:
                continue
            try:
                _job = _json.loads(_line)
                _paths = ocr_bitmap_subs_to_srt(
                    input_path=_job["input_path"],
                    lang=_job.get("lang", "en"),
                    out_dir=_job.get("out_dir"),
                    all_streams=_job.get("all_streams", False),
                    verbose=_job.get("verbose", False),
                    log_fn=_log,
                )
                print(_json.dumps({"ok": True, "paths": _paths}), flush=True)
            except Exception as _e:
                import traceback as _tb
                _tb.print_exc(file=_sys.stderr)
                print(_json.dumps({"ok": False, "error": str(_e)}), flush=True)
        _sys.exit(0)

    if len(_sys.argv) != 5:
        print("Usage: bitmap_subs.py <input_path> <out_dir> <lang> <result_json>",
              file=_sys.stderr)
        print("       bitmap_subs.py --server   (persistent worker mode)",
              file=_sys.stderr)
        _sys.exit(2)
    _input, _out_dir, _lang, _result_json = _sys.argv[1:]
    try:
        _paths = ocr_bitmap_subs_to_srt(
            input_path=_input,
            lang=_lang,
            out_dir=_out_dir,
            all_streams=False,
            verbose=False,
            log_fn=lambda msg: print(msg, flush=True),
        )
        import json as _json
        with open(_result_json, "w", encoding="utf-8") as _f:
            _json.dump(_paths, _f)
        _sys.exit(0)
    except Exception as _e:
        import traceback as _tb
        print(f"OCR worker error: {_e}", file=_sys.stderr)
        _tb.print_exc(file=_sys.stderr)
        _sys.exit(1)
