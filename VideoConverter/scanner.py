"""
scanner.py
==========
Recursive filesystem walker for VideoConverter.

Yields SSE-compatible dict events consumed by /api/scan:
  {"type": "folder",  "folder": "rel/path", "files": [...]}
  {"type": "done",    "total_files": N, "total_mb": X}
  {"type": "warning", "path": "...", "message": "..."}
  {"type": "error",   "message": "..."}

File dict shape (one entry per qualifying video file):
  {
    "full_path": str,           # absolute path, forward slashes
    "name":      str,           # basename
    "folder":    str,           # relative path from scan root (forward slashes)
    "size":      str,           # MB, comma-formatted, e.g. "1,234.5"
    "codec":     str,           # normalised display label, e.g. "H264", "HEVC"
    "duration":  str,           # "M:SS" or "H:MM:SS"
    "is_hi10":   bool,          # True for 10-bit H.264 → remux path
    "streams": {
      "video": {"codec", "profile", "resolution", "fps", "bitrate", "hdr"},
      "audio": [{"track", "codec", "channels", "language", "bitrate", "title"}],
      "subs":  [{"track", "codec", "language", "title"}],
    },
    "status": "pending",
  }

Skip rules:
  1. First video stream codec is av1 — already at optimal efficiency.
  2. DB record for (path, mtime) has status='done'  — already converted.
  3. DB record has status='running'                 — in progress (emits warning).

No converted/ folder name check — output files are HEVC (rule 1) or in the DB
(rule 2), so no directory-name heuristics required.
"""
from __future__ import annotations

import json
import os
import subprocess
from typing import Generator

import db

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".m2ts"}

_SKIP_CODECS = {"av1", "av1_cuvid"}  # AV1 only — HEVC files may still be inefficiently encoded

_CODEC_DISPLAY: dict[str, str] = {
    "h264":        "H264",
    "hevc":        "HEVC",
    "mpeg2video":  "MPEG2",
    "vc1":         "VC-1",
    "vp9":         "VP9",
    "vp8":         "VP8",
    "av1":         "AV1",
    "mpeg4":       "MPEG4",
    "msmpeg4v3":   "DivX",
    "wmv3":        "WMV3",
    "flv1":        "FLV",
    "theora":      "Theora",
    "prores":      "ProRes",
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _normalise_codec(codec_name: str) -> str:
    return _CODEC_DISPLAY.get(codec_name.lower(), codec_name.upper())


def _format_duration(seconds: float) -> str:
    secs = int(seconds)
    h = secs // 3600
    m = (secs % 3600) // 60
    s = secs % 60
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _parse_fps(r_frame_rate: str) -> str:
    """Convert fraction string '30000/1001' → '29.97'."""
    try:
        num, den = r_frame_rate.split("/")
        fps = int(num) / int(den)
        # Strip trailing zeros but preserve at least one decimal place when
        # the value is not a whole number.
        formatted = f"{fps:.3f}".rstrip("0")
        if formatted.endswith("."):
            formatted = formatted[:-1]
        return formatted
    except Exception:
        return r_frame_rate


def _ffprobe(file_path: str) -> dict | None:
    """Run ffprobe and return parsed JSON, or None on any failure."""
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        file_path,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except Exception:
        return None


def _parse_probe(probe: dict) -> dict:
    """
    Extract normalised metadata from an ffprobe JSON result.

    Returns:
      {
        "codec":         str,   # raw lowercase codec name of first video stream
        "is_hi10":       bool,
        "streams":       {...},
        "duration_secs": float,
      }
    """
    video_stream: dict | None = None
    audio_streams: list[dict] = []
    sub_streams:   list[dict] = []
    audio_idx = 0
    sub_idx   = 0

    for stream in probe.get("streams", []):
        codec_type = stream.get("codec_type", "")
        if codec_type == "video" and video_stream is None:
            video_stream = stream
        elif codec_type == "audio":
            tags = stream.get("tags", {})
            audio_streams.append({
                "track":    audio_idx,
                "codec":    _normalise_codec(stream.get("codec_name", "unknown")),
                "channels": stream.get("channels", 0),
                "language": tags.get("language", "und"),
                "bitrate":  int(stream.get("bit_rate", 0) or 0),
                "title":    tags.get("title", ""),
            })
            audio_idx += 1
        elif codec_type == "subtitle":
            tags = stream.get("tags", {})
            sub_streams.append({
                "track":    sub_idx,
                "codec":    _normalise_codec(stream.get("codec_name", "unknown")),
                "language": tags.get("language", "und"),
                "title":    tags.get("title", ""),
            })
            sub_idx += 1

    fmt            = probe.get("format", {})
    duration_secs  = float(fmt.get("duration", 0) or 0)

    if video_stream is None:
        return {
            "codec":         "unknown",
            "is_hi10":       False,
            "streams":       {"video": None, "audio": audio_streams, "subs": sub_streams},
            "duration_secs": duration_secs,
        }

    codec_name = video_stream.get("codec_name", "")
    bits       = int(video_stream.get("bits_per_raw_sample", 0) or 0)
    profile    = video_stream.get("profile", "")
    pix_fmt    = video_stream.get("pix_fmt", "")

    is_hi10 = (
        codec_name.lower() == "h264"
        and (bits >= 10 or "high 10" in profile.lower() or "yuv420p10" in pix_fmt)
    )

    w          = video_stream.get("width",  0)
    h          = video_stream.get("height", 0)
    resolution = f"{w}x{h}" if w and h else "unknown"

    # Prefer stream-level bitrate; fall back to container bitrate
    bitrate_raw = video_stream.get("bit_rate") or fmt.get("bit_rate") or 0
    bitrate     = int(bitrate_raw or 0)

    hdr_transfers = {"smpte2084", "arib-std-b67", "smpte428"}
    hdr = video_stream.get("color_transfer", "").lower() in hdr_transfers

    video_info = {
        "codec":      _normalise_codec(codec_name),
        "profile":    profile,
        "resolution": resolution,
        "fps":        _parse_fps(video_stream.get("r_frame_rate", "0/1")),
        "bitrate":    bitrate,
        "hdr":        hdr,
    }

    return {
        "codec":         codec_name.lower(),   # raw, used for _SKIP_CODECS check
        "is_hi10":       is_hi10,
        "streams":       {"video": video_info, "audio": audio_streams, "subs": sub_streams},
        "duration_secs": duration_secs,
    }


def _db_lookup(path: str, mtime: float) -> str | None:
    """
    Return the DB status for (path, mtime), or None if no record / DB unavailable.
    Also checks output_path so that files which changed extension (e.g. MKV→MP4
    in anime mode) are recognised as already converted.
    Defensive: any exception is treated as 'no record'.
    """
    try:
        record = db.get_record(path, mtime)
        if record:
            return record["status"]
        # Fallback: the file on disk may be the *output* of a completed conversion
        # (e.g. source was .mkv, output is .mp4 sitting in the same folder).
        out_record = db.get_record_by_output(path)
        if out_record:
            return out_record["status"]
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def walk(root: str) -> Generator[dict, None, None]:
    """
    Recursively walk *root*, yielding SSE-compatible dict events.

    Folder events are yielded depth-first, directory order alphabetical.
    The DB is consulted (if initialised) to skip already-converted files.
    """
    if not os.path.isdir(root):
        yield {"type": "error", "message": f"Not a directory: {root}"}
        return

    total_files  = 0
    total_bytes  = 0

    try:
        walk_iter = list(os.walk(root))
    except PermissionError as exc:
        yield {"type": "error", "message": str(exc)}
        return

    for dirpath, dirnames, filenames in walk_iter:
        # Alphabetical sub-directory traversal order
        dirnames.sort(key=str.lower)

        candidates = [
            f for f in filenames
            if os.path.splitext(f)[1].lower() in VIDEO_EXTENSIONS
        ]
        if not candidates:
            continue

        folder_files: list[dict] = []

        for filename in sorted(candidates, key=str.lower):
            full_path = os.path.join(dirpath, filename)

            # ---- file stats ----
            try:
                stat       = os.stat(full_path)
                mtime      = stat.st_mtime
                size_bytes = stat.st_size
            except OSError as exc:
                yield {"type": "warning", "path": full_path, "message": str(exc)}
                continue

            # ---- zero-byte guard ----
            if size_bytes == 0:
                yield {
                    "type":    "warning",
                    "path":    full_path,
                    "message": "Empty file — skipping",
                }
                continue

            # ---- DB skip check ----
            db_status = _db_lookup(full_path, mtime)
            if db_status == "done":
                continue                       # silently skip committed conversions
            if db_status == "running":
                yield {
                    "type":    "warning",
                    "path":    full_path,
                    "message": "Conversion already running in another session",
                }
                continue

            # ---- ffprobe ----
            probe_data = _ffprobe(full_path)
            if probe_data is None:
                yield {
                    "type":    "warning",
                    "path":    full_path,
                    "message": "ffprobe failed — skipping",
                }
                continue

            parsed = _parse_probe(probe_data)

            # ---- codec skip (AV1 — already at optimal efficiency) ----
            if parsed["codec"] in _SKIP_CODECS:
                continue

            # ---- build file dict ----
            rel_folder = os.path.relpath(dirpath, root)
            if rel_folder == ".":
                rel_folder = ""

            size_mb = size_bytes / (1024 * 1024)

            file_dict: dict = {
                "full_path": full_path.replace("\\", "/"),
                "name":      filename,
                "folder":    rel_folder.replace("\\", "/"),
                "size":      f"{size_mb:,.1f}",
                "codec":     (
                    parsed["streams"]["video"]["codec"]
                    if parsed["streams"]["video"] else "unknown"
                ),
                "duration":  _format_duration(parsed["duration_secs"]),
                "is_hi10":   parsed["is_hi10"],
                "streams":   parsed["streams"],
                "status":    "pending",
            }

            folder_files.append(file_dict)
            total_files += 1
            total_bytes += size_bytes

        if folder_files:
            rel_dir = os.path.relpath(dirpath, root)
            if rel_dir == ".":
                rel_dir = ""
            yield {
                "type":   "folder",
                "folder": rel_dir.replace("\\", "/"),
                "files":  folder_files,
            }

    yield {
        "type":        "done",
        "total_files": total_files,
        "total_mb":    total_bytes / (1024 * 1024),
    }
