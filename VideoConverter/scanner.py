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
      "video": {"codec", "profile", "resolution", "fps", "hdr"},
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
from pathlib import Path
from typing import Generator

import db

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".m2ts"}

_SKIP_CODECS: set[str] = set()  # AV1 is handled by stream-copy in the converter; nothing is skipped

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
            encoding="utf-8",
            errors="replace",
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
                "video_track_count": int,
                "audio_track_count": int,
            "subtitle_track_count": int,
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
                "index":    stream.get("index", 0),
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
                "index":    stream.get("index", 0),
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
            "video_track_count": 0,
            "audio_track_count": len(audio_streams),
            "subtitle_track_count": len(sub_streams),
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

    hdr_transfers = {"smpte2084", "arib-std-b67", "smpte428"}
    hdr = video_stream.get("color_transfer", "").lower() in hdr_transfers

    video_info = {
        "codec":      _normalise_codec(codec_name),
        "profile":    profile,
        "resolution": resolution,
        "fps":        _parse_fps(video_stream.get("r_frame_rate", "0/1")),
        "hdr":        hdr,
    }

    return {
        "codec":         codec_name.lower(),   # raw, used for _SKIP_CODECS check
        "is_hi10":       is_hi10,
        "streams":       {"video": video_info, "audio": audio_streams, "subs": sub_streams},
        "duration_secs": duration_secs,
        "video_track_count": 1,
        "audio_track_count": len(audio_streams),
        "subtitle_track_count": len(sub_streams),
    }


def _db_lookup(path: str, mtime: float, size_bytes: int) -> str | None:
    """
    Return the DB status for this file, or None if no record / DB unavailable.

    Lookup order:
      1. Exact (source_path, source_mtime) match — normal case.
      2. output_path match — file changed extension (e.g. MKV→MP4 in anime mode).
      3. Fingerprint (source_mtime, source_size_bytes) match — file was moved/renamed.

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
        # Fallback: folder/file was renamed — mtime and exact size are unchanged.
        fp_record = db.get_record_by_fingerprint(mtime, size_bytes)
        if fp_record:
            return fp_record["status"]
        # Fallback: cross-drive copy resets mtime — compute 2 MB hash as last resort.
        file_hash = db.hash_file_head(path)
        if file_hash:
            hash_record = db.get_record_by_hash(file_hash)
            if hash_record:
                return hash_record["status"]
        return None
    except Exception:
        return None


def _has_ocr_sidecar(path: str) -> bool:
    """Return True when prior OCR output (*.pgs*.srt) exists for this file."""
    src = Path(path)
    stem_lower = src.stem.lower()
    try:
        for p in src.parent.glob(f"{src.stem}.pgs*.srt"):
            if p.is_file() and p.stem.lower().startswith(stem_lower + ".pgs"):
                return True
    except Exception:
        return False
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def walk(root: str) -> Generator[dict, None, None]:
    """
    Recursively walk *root*, yielding SSE-compatible dict events.

    Phase 1 — fast: stream the directory tree using os.walk() directly (no
    upfront materialisation).  For each folder, collect candidate video files
    via os.stat() only, then do a single batch DB query per folder to skip
    already-done or currently-running files.  Yields folder events immediately
    — no ffprobe, no per-file DB round-trips, no disk reads.

    Phase 2 — probe: after all folder events are streamed, run ffprobe on each
    queued file and emit probe / remove events.

    Event types:
      {"type": "folder",  "folder": str, "files": [minimal_file_dict, ...]}
      {"type": "probe",   "full_path": str, "codec": str, "duration": str,
                          "is_hi10": bool, "streams": {...}}
      {"type": "remove",  "full_path": str, "reason": str}
      {"type": "done",    "total_files": N, "total_mb": X}
      {"type": "warning", "path": str, "message": str}
      {"type": "error",   "message": str}
    """
    if not os.path.isdir(root):
        yield {"type": "error", "message": f"Not a directory: {root}"}
        return

    total_bytes = 0
    to_hash_check: list[dict] = []  # files with no DB record — need 2 MB hash read
    to_probe: list[dict] = []       # files needing ffprobe (no cached bitrate)
    to_probe_done: list[dict] = []  # done files with no output bitrate — probe to fill gap
    to_update_size: list[tuple] = []  # (record_id, size_bytes, size_mb) — fill missing sizes

    # ----------------------------------------------------------------
    # Phase 1 — fast folder scan (stat + one batch DB query per folder)
    # ----------------------------------------------------------------
    try:
        walk_gen = os.walk(root)
    except PermissionError as exc:
        yield {"type": "error", "message": str(exc)}
        return

    for dirpath, dirnames, filenames in walk_gen:
        dirnames.sort(key=str.lower)

        candidates = [
            f for f in filenames
            if os.path.splitext(f)[1].lower() in VIDEO_EXTENSIONS
        ]
        if not candidates:
            continue

        # Stat all candidates first (fast — no DB, no disk reads beyond metadata)
        stat_results: list[tuple[str, str, float, int]] = []  # (full_path, filename, mtime, size)
        for filename in sorted(candidates, key=str.lower):
            full_path = os.path.join(dirpath, filename)
            try:
                st         = os.stat(full_path)
                mtime      = st.st_mtime
                size_bytes = st.st_size
            except OSError as exc:
                yield {"type": "warning", "path": full_path, "message": str(exc)}
                continue
            if size_bytes == 0:
                yield {"type": "warning", "path": full_path,
                       "message": "Empty file — skipping"}
                continue
            stat_results.append((full_path.replace("\\", "/"), filename, mtime, size_bytes))

        if not stat_results:
            continue

        # One batch DB lookup for all paths in this folder
        folder_paths = [fp for fp, _, _, _ in stat_results]
        known = db.get_latest_statuses_by_paths(folder_paths)

        folder_files: list[dict] = []
        rel_folder = os.path.relpath(dirpath, root)
        if rel_folder == ".":
            rel_folder = ""

        for full_path, filename, mtime, size_bytes in stat_results:
            db_info   = known.get(full_path) or {}
            db_status = db_info.get("status")

            # Cheap per-file fallbacks (no disk I/O):
            #   1. output_path match — file IS the converted output sitting in-place
            #   2. fingerprint match — file was moved/renamed (mtime + size preserved)
            # Run when there is no record OR the record is only pending — a pending
            # record with a mismatched mtime often means the file is the converted
            # output whose mtime changed after the in-place replace.
            if not db_info or db_status in ("pending", "queued"):
                fallback_rec = (
                    db.get_record_by_output(full_path)
                    or db.get_record_by_fingerprint(mtime, size_bytes)
                )
                if fallback_rec:
                    db_status = fallback_rec["status"]
                    db_info = {
                        "id":            fallback_rec["id"],
                        "status":        db_status,
                        "bitrate_kbps":  fallback_rec.get("source_bitrate_kbps"),
                        "codec":         fallback_rec.get("source_codec"),
                        "duration_secs": fallback_rec.get("source_duration_secs"),
                        "video_track_count": fallback_rec.get("source_video_track_count"),
                        "audio_track_count": fallback_rec.get("source_audio_track_count"),
                        "output_size_mb": fallback_rec.get("output_size_mb"),
                        "saved_mb":      fallback_rec.get("saved_mb"),
                        "saved_pct":     fallback_rec.get("saved_pct"),
                    }
                    db.update_source_path(fallback_rec["id"], full_path)
                elif db_status in ("pending", "queued"):
                    file_hash = db.hash_file_head(full_path)
                    if file_hash:
                        hash_rec = db.get_record_by_hash(file_hash)
                        if hash_rec:
                            db_status = hash_rec["status"]
                            db_info = {
                                "id":            hash_rec["id"],
                                "status":        db_status,
                                "bitrate_kbps":  hash_rec.get("output_bitrate_kbps") or hash_rec.get("source_bitrate_kbps"),
                                "codec":         hash_rec.get("source_codec"),
                                "duration_secs": hash_rec.get("source_duration_secs"),
                                "video_track_count": hash_rec.get("source_video_track_count"),
                                "audio_track_count": hash_rec.get("source_audio_track_count"),
                                "subtitle_track_count": hash_rec.get("source_subtitle_track_count"),
                                "output_size_mb": hash_rec.get("output_size_mb"),
                                "saved_mb":      hash_rec.get("saved_mb"),
                                "saved_pct":     hash_rec.get("saved_pct"),
                                "est_saving_pct": hash_rec.get("est_saving_pct"),
                                "est_saving_mb": hash_rec.get("est_saving_mb"),
                                "est_sample_cv_pct": hash_rec.get("est_sample_cv_pct"),
                                "est_high_variance": hash_rec.get("est_high_variance"),
                                "est_aggregation": hash_rec.get("est_aggregation"),
                            }
                            db.update_source_path(hash_rec["id"], full_path)
                            db.delete_pending_records_by_path(full_path, keep_id=hash_rec["id"])

            if db_status in ("done", "low_savings", "no_saving"):
                # If sidecar subtitle files exist alongside the file, reset to
                # pending so the converter's sub-inject path can mux them in
                # (no re-encode — fast copy + merge).  Match both exact-stem
                # (Title.srt) and prefix-style (Title.pgs1.srt, Title.en.srt)
                # using the same dot-separator rule as converter.py.
                # Note: 'skipped' is an explicit manual skip — not overridden.
                _SIDECAR_EXTS = {".srt", ".ass", ".ssa"}
                _src_path = Path(full_path)
                _stem_lower = _src_path.stem.lower()
                _has_sidecars = any(
                    p.suffix.lower() in _SIDECAR_EXTS and (
                        p.stem.lower() == _stem_lower
                        or p.stem.lower().startswith(_stem_lower + ".")
                    )
                    for p in _src_path.parent.iterdir()
                )
                if _has_sidecars:
                    record_id = db_info.get("id")
                    if record_id:
                        db.reset_done_to_pending(record_id)
                    db_status = "pending"
                    # fall through to normal pending handling below
                elif db_status == "done":
                    # Include done files in the grid (read-only row, no re-encoding).
                    # If we don't have the output bitrate, queue for a one-time probe so
                    # the bitrate column is populated (and filterable) going forward.
                    size_mb = size_bytes / (1024 * 1024)
                    fp      = full_path.replace("\\", "/")
                    cached_bitrate = db_info.get("bitrate_kbps")
                    cached_codec   = db_info.get("codec") or ""
                    cached_dur     = db_info.get("duration_secs")
                    out_mb_val  = db_info.get("output_size_mb")
                    saved_mb_val = db_info.get("saved_mb")
                    saved_pct_val = db_info.get("saved_pct")
                    est_pct_val = db_info.get("est_saving_pct")
                    est_mb_val  = db_info.get("est_saving_mb")
                    est_cv_val  = db_info.get("est_sample_cv_pct")
                    est_hv_val  = bool(db_info.get("est_high_variance", False))
                    est_agg_val = db_info.get("est_aggregation")
                    folder_files.append({
                        "full_path":    fp,
                        "name":         filename,
                        "folder":       rel_folder.replace("\\", "/"),
                        "size":         f"{size_mb:,.1f}",
                        "codec":        cached_codec,
                        "duration":     _format_duration(cached_dur) if cached_dur else "",
                        "bitrate_kbps": cached_bitrate,
                        "video_track_count": db_info.get("video_track_count"),
                        "audio_track_count": db_info.get("audio_track_count"),
                        "subtitle_track_count": db_info.get("subtitle_track_count"),
                        "is_hi10":      False,
                        "streams":      None,
                        "status":       "done",
                        "output":       str(round(out_mb_val, 1)) if out_mb_val else None,
                        "saved":        str(round(saved_mb_val, 1)) if saved_mb_val else None,
                        "pct":          str(saved_pct_val) if saved_pct_val is not None else None,
                        "est_pct":      est_pct_val,
                        "est_mb":       est_mb_val,
                        "est_cv":       est_cv_val,
                        "est_high_variance": est_hv_val,
                        "est_aggregation": est_agg_val,
                        "ocr_status":   "done" if _has_ocr_sidecar(full_path) else "",
                    })
                    missing_track_meta = (
                        db_info.get("video_track_count") is None
                        or db_info.get("audio_track_count") is None
                        or db_info.get("subtitle_track_count") is None
                    )
                    if cached_bitrate is None or missing_track_meta:
                        record_id = db_info.get("id")
                        if record_id:
                            to_probe_done.append({"full_path": fp, "record_id": record_id})
                    continue
            if db_status == "running":
                yield {"type": "warning", "path": full_path,
                       "message": "Conversion already running in another session"}
                continue

            size_mb = size_bytes / (1024 * 1024)
            fp      = full_path.replace("\\", "/")

            # Use cached probe data if available — skip Phase 2 for this file.
            cached_bitrate = db_info.get("bitrate_kbps")  # None means never probed
            cached_codec   = db_info.get("codec") or ""
            cached_dur     = db_info.get("duration_secs")

            file_dict: dict = {
                "full_path":       fp,
                "name":            filename,
                "folder":          rel_folder.replace("\\", "/"),
                "size":            f"{size_mb:,.1f}",
                "codec":           cached_codec,
                "duration":        _format_duration(cached_dur) if cached_dur else "",
                "bitrate_kbps":    cached_bitrate,
                "video_track_count": db_info.get("video_track_count"),
                "audio_track_count": db_info.get("audio_track_count"),
                "subtitle_track_count": db_info.get("subtitle_track_count"),
                "is_hi10":         False,
                "streams":         None,
                "status":          db_status if (db_status and db_status not in ("pending", "queued")) else "pending",
                "force_sw":        bool(db_info.get("force_sw", False)),
                "dropped_streams": db_info.get("dropped_streams", []),
                "est_pct":         db_info.get("est_saving_pct"),
                "est_mb":          db_info.get("est_saving_mb"),
                "est_cv":          db_info.get("est_sample_cv_pct"),
                "est_high_variance": bool(db_info.get("est_high_variance", False)),
                "est_aggregation": db_info.get("est_aggregation"),
                "ocr_status":      "done" if _has_ocr_sidecar(full_path) else "",
            }

            folder_files.append(file_dict)
            total_bytes += size_bytes
            if not db_info:
                # No DB record — must hash-check in Phase 2 before deciding to probe
                to_hash_check.append({"full_path": fp, "mtime": mtime, "size_bytes": size_bytes})
            elif (
                cached_bitrate is None
                or db_info.get("video_track_count") is None
                or db_info.get("audio_track_count") is None
                or db_info.get("subtitle_track_count") is None
            ):
                # Known file with missing probe-derived metadata — queue for Phase 3 ffprobe
                to_probe.append({"full_path": fp, "mtime": mtime, "size_bytes": size_bytes})
            else:
                # Already probed — queue a size back-fill if DB record lacks it
                rec_id = db_info.get("id")
                if rec_id:
                    to_update_size.append((rec_id, size_bytes, size_bytes / (1024 * 1024)))

        if folder_files:
            yield {
                "type":   "folder",
                "folder": rel_folder.replace("\\", "/"),
                "files":  folder_files,
            }

    # ----------------------------------------------------------------
    # Phase 2 — hash check for files with no DB record
    # Reads the first 2 MB of each file to match against known source/output
    # hashes.  Only runs for files that couldn't be matched cheaply in Phase 1.
    # Survivors are forwarded to Phase 3 for ffprobe.
    # ----------------------------------------------------------------
    yield {
        "type":        "scan_done",
        "total_files": len(to_hash_check) + len(to_probe) + len(to_probe_done),
        "hash_files":  len(to_hash_check),
        "total_mb":    total_bytes / (1024 * 1024),
    }

    hash_total = len(to_hash_check)
    hash_done = 0
    for entry in to_hash_check:
        fp    = entry["full_path"]
        mtime = entry["mtime"]
        size_bytes = entry["size_bytes"]
        file_hash = db.hash_file_head(fp)
        if file_hash:
            hash_rec = db.get_record_by_hash(file_hash)
            if hash_rec and hash_rec["status"] == "done":
                db.update_source_path(hash_rec["id"], fp)
                yield {"type": "remove", "full_path": fp, "reason": "already converted (hash match)"}
            else:
                # No hash match — forward to Phase 3 for ffprobe
                to_probe.append({"full_path": fp, "mtime": mtime, "size_bytes": size_bytes})
        else:
            # No hash available — forward to Phase 3 for ffprobe
            to_probe.append({"full_path": fp, "mtime": mtime, "size_bytes": size_bytes})

        hash_done += 1
        yield {
            "type":  "hash_progress",
            "done":  hash_done,
            "total": hash_total,
        }

    # ----------------------------------------------------------------
    # Phase 3 — ffprobe each file, emit probe / remove events
    # ----------------------------------------------------------------
    kept = 0
    for entry in to_probe:
        fp         = entry["full_path"]
        mtime      = entry["mtime"]
        size_bytes = entry.get("size_bytes") or 0
        probe_data = _ffprobe(fp)
        if probe_data is None:
            yield {"type": "warning", "path": fp, "message": "ffprobe failed — skipping"}
            yield {"type": "remove",  "full_path": fp, "reason": "ffprobe failed"}
            continue

        parsed = _parse_probe(probe_data)

        if parsed["codec"] in _SKIP_CODECS:
            yield {"type": "remove", "full_path": fp, "reason": "AV1 — already optimal"}
            continue

        video_info    = parsed["streams"]["video"]
        display_codec = video_info["codec"] if video_info else "unknown"
        dur_secs      = parsed["duration_secs"]
        v_tracks      = parsed.get("video_track_count", 0)
        a_tracks      = parsed.get("audio_track_count", 0)
        s_tracks      = parsed.get("subtitle_track_count", 0)
        if not size_bytes:
            try:
                size_bytes = os.path.getsize(fp)
            except OSError:
                size_bytes = 0
        size_mb      = size_bytes / (1024 * 1024)
        bitrate_kbps  = round(size_bytes * 8 / dur_secs / 1000) if dur_secs > 0 else 0

        # Persist probe result (including file size) so future scans skip ffprobe.
        db.save_probe_result(fp, mtime, display_codec, bitrate_kbps, dur_secs,
                 video_track_count=v_tracks, audio_track_count=a_tracks,
                     subtitle_track_count=s_tracks,
                             source_size_bytes=size_bytes, source_size_mb=size_mb)

        yield {
            "type":        "probe",
            "full_path":   fp,
            "codec":       display_codec,
            "duration":    _format_duration(dur_secs),
            "is_hi10":     parsed["is_hi10"],
            "streams":     parsed["streams"],
            "bitrate_kbps": bitrate_kbps,
            "video_track_count": v_tracks,
            "audio_track_count": a_tracks,
            "subtitle_track_count": s_tracks,
        }
        kept += 1

    # ----------------------------------------------------------------
    # Phase 4 — probe done files with no output bitrate
    # These are old records that pre-date output_bitrate_kbps storage.
    # Must run BEFORE the 'done' event — the JS closes the SSE on 'done'.
    # Probe the output file on disk, persist the result so future scans
    # skip this phase, and emit probe events so the UI updates immediately.
    # ----------------------------------------------------------------
    for entry in to_probe_done:
        fp        = entry["full_path"]
        record_id = entry["record_id"]
        probe_data = _ffprobe(fp)
        if probe_data is None:
            continue
        parsed = _parse_probe(probe_data)
        video_info    = parsed["streams"]["video"]
        display_codec = video_info["codec"] if video_info else "HEVC"
        dur_secs      = parsed["duration_secs"]
        v_tracks      = parsed.get("video_track_count", 0)
        a_tracks      = parsed.get("audio_track_count", 0)
        s_tracks      = parsed.get("subtitle_track_count", 0)
        try:
            size_bytes = os.path.getsize(fp)
        except OSError:
            size_bytes = 0
        bitrate_kbps  = round(size_bytes * 8 / dur_secs / 1000) if (dur_secs > 0 and size_bytes > 0) else 0
        if bitrate_kbps:
            db.update_output_bitrate(record_id, bitrate_kbps)
        source_size_mb = (size_bytes / (1024 * 1024)) if size_bytes > 0 else None
        db.update_probe_result(
            record_id,
            display_codec,
            bitrate_kbps or None,
            dur_secs,
            video_track_count=v_tracks,
            audio_track_count=a_tracks,
            subtitle_track_count=s_tracks,
            source_size_bytes=size_bytes or None,
            source_size_mb=source_size_mb,
        )
        yield {
            "type":        "probe",
            "full_path":   fp,
            "codec":       display_codec,
            "duration":    _format_duration(dur_secs) if dur_secs else "",
            "is_hi10":     parsed["is_hi10"],
            "streams":     parsed["streams"],
            "bitrate_kbps": bitrate_kbps,
            "video_track_count": v_tracks,
            "audio_track_count": a_tracks,
            "subtitle_track_count": s_tracks,
        }

    yield {
        "type":        "done",
        "total_files": kept,
        "total_mb":    total_bytes / (1024 * 1024),
    }

    # Back-fill size data for existing records that lacked it (single batch write).
    db.batch_update_sizes(to_update_size)
