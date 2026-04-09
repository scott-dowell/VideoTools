"""
VideoConverter — conversion engine.

Two modes:
  compress_simple()  — Normal mode. One FFmpeg call, copy audio/subs, keep
                       source container (MKV→MKV, MP4→MP4). Always attempts
                       QSV, falls back to libx265. Discards output if not
                       smaller than source.

  estimate()         — Quick savings estimator. Encodes 10 s from the middle
                       of the file with QSV and extrapolates compression ratio.

  convert_video()    — Dispatcher: normal mode (compress_simple) or anime
                       mode (Phase 3b). This is the function called by the
                       queue worker thread in app.py.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Callable

import config

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------
LogFn      = Callable[[str], None]
ProgressCb = Callable[[float, float, int], None]   # pct, fps, eta_secs


# ---------------------------------------------------------------------------
# FFmpeg command builders
# ---------------------------------------------------------------------------

def _qsv_cmd(input_path: str, output_path: str, quality: int) -> list[str]:
    return [
        "ffmpeg", "-y",
        "-i", input_path,
        "-c:v", "hevc_qsv",
        "-global_quality", str(quality),
        "-c:a", "copy",
        "-c:s", "copy",
        "-map", "0:v:0",
        "-map", "0:a?",
        "-map", "0:s?",
        output_path,
    ]


def _sw_cmd(input_path: str, output_path: str, quality: int) -> list[str]:
    return [
        "ffmpeg", "-y",
        "-i", input_path,
        "-c:v", "libx265",
        "-crf", str(quality),
        "-preset", "medium",
        "-c:a", "copy",
        "-c:s", "copy",
        "-map", "0:v:0",
        "-map", "0:a?",
        "-map", "0:s?",
        output_path,
    ]


# ---------------------------------------------------------------------------
# Progress parser
# ---------------------------------------------------------------------------

_PROGRESS_RE = re.compile(
    r"frame=\s*(?P<frame>\d+)\s+fps=\s*(?P<fps>[\d.]+).*?time=(?P<time>[\d:.]+)"
)


def _parse_time(ts: str) -> float:
    """Parse 'HH:MM:SS.ss' from ffmpeg stats line into seconds."""
    try:
        parts = ts.split(":")
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Core encode helper
# ---------------------------------------------------------------------------

def _run_ffmpeg(
    cmd: list[str],
    log: LogFn,
    stop_event: threading.Event,
    duration_secs: float = 0.0,
    progress_cb: ProgressCb | None = None,
    pid_holder: list[int] | None = None,   # pid_holder[0] = pid, caller clears
) -> bool:
    """Run an FFmpeg command; return True on success.

    Args:
        cmd:           FFmpeg argv (including 'ffmpeg' itself).
        log:           Callable for log lines.
        stop_event:    Set this to abort the encode.
        duration_secs: Total source duration; enables progress calculation.
        progress_cb:   Called with (pct, fps, eta_secs) on each stats line.
        pid_holder:    If provided, pid_holder[0] is set to the child PID so
                       the caller can NtSuspend/NtResume it.
    """
    log(f"Running: {' '.join(cmd)}")
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if pid_holder is not None:
            pid_holder[0] = proc.pid

        for line in proc.stdout:
            if stop_event.is_set():
                proc.kill()
                proc.wait()
                log("Stopped by user.")
                return False

            if progress_cb and duration_secs > 0:
                m = _PROGRESS_RE.search(line)
                if m:
                    elapsed = _parse_time(m.group("time"))
                    fps     = float(m.group("fps") or 0)
                    pct     = min(100.0, elapsed / duration_secs * 100)
                    remaining = (duration_secs - elapsed) / fps if fps > 0 else 0
                    eta_secs  = max(0, int(remaining))
                    try:
                        progress_cb(pct, fps, eta_secs)
                    except Exception:
                        pass

        proc.wait()
        if pid_holder is not None:
            pid_holder[0] = 0
        return proc.returncode == 0
    except FileNotFoundError:
        log("ERROR: ffmpeg not found. Is it on PATH?")
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compress_simple(
    input_path: str,
    output_dir: str,
    log: LogFn,
    stop_event: threading.Event,
    quality: int | None = None,
    progress_cb: ProgressCb | None = None,
    pid_holder: list[int] | None = None,
) -> tuple[bool, str]:
    """
    Normal-mode compression.

    - Keeps source container (MP4→MP4, MKV→MKV, anything else→MKV)
    - Copies all audio and subtitle tracks unchanged
    - Tries hevc_qsv first, falls back to libx265
    - Discards output and returns (False, "") if result is not smaller than source

    Returns (True, encoder_used) if a smaller output file was produced,
            (False, "") otherwise.
    """
    quality    = quality    if quality    is not None else config.QSV_QUALITY
    input_path = os.path.normpath(input_path)
    src_size   = os.path.getsize(input_path)
    duration   = _ffprobe_duration(input_path)

    suffix   = Path(input_path).suffix.lower()
    out_ext  = suffix if suffix in (".mp4", ".mkv", ".m4v") else ".mkv"
    out_name = Path(input_path).stem + out_ext

    os.makedirs(config.LOCAL_TEMP_DIR, exist_ok=True)
    tmp_path   = os.path.join(config.LOCAL_TEMP_DIR, out_name)
    final_path = os.path.join(output_dir, out_name)

    encoder_used = ""
    try:
        # Try QSV first
        encoder_used = "hevc_qsv"
        success = _run_ffmpeg(
            _qsv_cmd(input_path, tmp_path, quality), log, stop_event,
            duration_secs=duration, progress_cb=progress_cb, pid_holder=pid_holder,
        )
        if not success:
            if stop_event.is_set():
                return False, ""
            log("QSV failed, trying software encoder...")
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            encoder_used = "libx265"
            sw_quality = quality if quality <= 51 else config.SW_HEVC_CRF
            success = _run_ffmpeg(
                _sw_cmd(input_path, tmp_path, sw_quality), log, stop_event,
                duration_secs=duration, progress_cb=progress_cb, pid_holder=pid_holder,
            )

        if not success or not os.path.exists(tmp_path):
            log("Encode failed.")
            return False, ""

        enc_size = os.path.getsize(tmp_path)
        if enc_size >= src_size:
            log(f"Output not smaller ({enc_size:,} >= {src_size:,} bytes). Skipping.")
            os.remove(tmp_path)
            return False, ""

        os.makedirs(output_dir, exist_ok=True)
        os.replace(tmp_path, final_path)
        saved = src_size - enc_size
        log(f"Done. Saved {saved / 1024 / 1024:.1f} MB → {final_path}")
        return True, encoder_used

    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


# ---------------------------------------------------------------------------
# Savings estimator
# ---------------------------------------------------------------------------

def _ffprobe_duration(input_path: str) -> float:
    """Return duration in seconds via ffprobe, or 0.0 on failure."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_entries", "format=duration",
                input_path,
            ],
            capture_output=True, text=True, timeout=15,
        )
        data = json.loads(result.stdout)
        return float(data["format"]["duration"])
    except Exception:
        return 0.0


def estimate(input_path: str, quality: int | None = None) -> dict:
    """
    Encode a 10-second clip from the middle of input_path with hevc_qsv and
    extrapolate the compression ratio to the full file.

    Returns:
        {
          "estimated_output_mb": float,
          "estimated_saving_mb": float,
          "estimated_saving_pct": int,
          "error": str | None,
        }
    """
    quality = quality or config.QSV_QUALITY
    input_path = os.path.normpath(input_path)

    if not os.path.isfile(input_path):
        return {"error": "File not found"}

    src_size = os.path.getsize(input_path)
    src_mb   = src_size / 1024 / 1024

    duration = _ffprobe_duration(input_path)
    if duration < 20:
        # Too short to sample reliably — skip
        return {"error": "File too short to estimate"}

    seek = max(duration / 2 - 5, 0)
    clip_secs = min(10.0, duration - seek)

    os.makedirs(config.LOCAL_TEMP_DIR, exist_ok=True)
    tmp_path = os.path.join(
        config.LOCAL_TEMP_DIR,
        f"_est_{Path(input_path).stem}.mkv",
    )

    try:
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(seek),
            "-t",  str(clip_secs),
            "-i",  input_path,
            "-c:v", "hevc_qsv",
            "-global_quality", str(quality),
            "-an",           # skip audio — faster
            "-f", "matroska",
            tmp_path,
        ]
        result = subprocess.run(
            cmd, capture_output=True, timeout=120,
        )
        if result.returncode != 0 or not os.path.exists(tmp_path):
            return {"error": "ffmpeg encode failed"}

        enc_size = os.path.getsize(tmp_path)

        # Bytes of source data that correspond to the sampled clip
        src_clip_bytes = src_size * (clip_secs / duration)
        if src_clip_bytes == 0:
            return {"error": "Duration calculation error"}

        ratio = enc_size / src_clip_bytes
        estimated_output_mb = src_mb * ratio
        estimated_saving_mb = src_mb - estimated_output_mb
        if estimated_saving_mb < 0:
            estimated_saving_mb = 0.0
        estimated_saving_pct = int(estimated_saving_mb / src_mb * 100) if src_mb > 0 else 0

        return {
            "estimated_output_mb":  round(estimated_output_mb, 1),
            "estimated_saving_mb":  round(estimated_saving_mb, 1),
            "estimated_saving_pct": estimated_saving_pct,
            "error": None,
        }

    except subprocess.TimeoutExpired:
        return {"error": "Timed out"}
    except Exception as e:
        return {"error": str(e)}
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Output integrity check
# ---------------------------------------------------------------------------

def _verify_output(output_path: str, src_duration: float) -> tuple[bool, str]:
    """
    Sanity-check the encoded output via ffprobe.

    Checks:
      1. File exists and size > 0
      2. At least one video stream present
      3. Duration within 5% of src_duration (when src_duration > 0)

    Returns (True, "") on pass, (False, reason) on fail.
    """
    if not os.path.exists(output_path):
        return False, "file not found"
    if os.path.getsize(output_path) == 0:
        return False, "empty file"

    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_streams",
                "-show_format",
                output_path,
            ],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(result.stdout)
    except Exception as exc:
        return False, f"ffprobe error: {exc}"

    streams = data.get("streams", [])
    has_video = any(s.get("codec_type") == "video" for s in streams)
    if not has_video:
        return False, "no video stream"

    if src_duration > 0:
        try:
            out_duration = float(data["format"]["duration"])
        except (KeyError, ValueError, TypeError):
            out_duration = 0.0
        if out_duration > 0:
            diff_pct = abs(out_duration - src_duration) / src_duration
            if diff_pct > 0.05:
                return False, (
                    f"duration mismatch: src={src_duration:.1f}s "
                    f"out={out_duration:.1f}s ({diff_pct*100:.1f}% off)"
                )

    return True, ""


# ---------------------------------------------------------------------------
# Top-level dispatcher
# ---------------------------------------------------------------------------

def convert_video(
    input_path: str,
    output_dir: str,
    anime_mode: bool,
    quality: int | None,
    progress_cb: ProgressCb | None,
    stop_event: threading.Event,
    log: LogFn | None = None,
    pid_holder: list[int] | None = None,
) -> dict:
    """
    Convert a single video file.  Returns a result dict:
    {
      "ok":           bool,
      "output_path":  str | None,
      "output_size_mb": float,
      "saved_mb":     float,
      "saved_pct":    int,
      "encoder_used": str,
      "error":        str | None,
    }
    """
    if log is None:
        log = lambda msg: None

    input_path = os.path.normpath(input_path)
    src_size   = os.path.getsize(input_path)
    src_mb     = src_size / (1024 * 1024)
    duration   = _ffprobe_duration(input_path)

    if anime_mode:
        # Anime path implemented in Phase 3b; stub for now
        log("Anime mode not yet implemented — falling back to normal.")

    # Normal mode
    ok, encoder_used = compress_simple(
        input_path  = input_path,
        output_dir  = output_dir,
        log         = log,
        stop_event  = stop_event,
        quality     = quality,
        progress_cb = progress_cb,
        pid_holder  = pid_holder,
    )

    if not ok:
        return {
            "ok":           False,
            "output_path":  None,
            "output_size_mb": 0.0,
            "saved_mb":     0.0,
            "saved_pct":    0,
            "encoder_used": encoder_used,
            "error":        "encode failed or no savings",
        }

    # Locate output file
    suffix   = Path(input_path).suffix.lower()
    out_ext  = suffix if suffix in (".mp4", ".mkv", ".m4v") else ".mkv"
    out_name = Path(input_path).stem + out_ext
    output_path = os.path.join(output_dir, out_name)

    # Integrity check
    ok_verify, reason = _verify_output(output_path, duration)
    if not ok_verify:
        log(f"Integrity check failed: {reason}")
        return {
            "ok":           False,
            "output_path":  output_path,
            "output_size_mb": 0.0,
            "saved_mb":     0.0,
            "saved_pct":    0,
            "encoder_used": encoder_used,
            "error":        f"integrity: {reason}",
        }

    out_size   = os.path.getsize(output_path)
    out_mb     = out_size / (1024 * 1024)
    saved_mb   = src_mb - out_mb
    saved_pct  = int(saved_mb / src_mb * 100) if src_mb > 0 else 0

    return {
        "ok":             True,
        "output_path":    output_path,
        "output_size_mb": round(out_mb, 2),
        "saved_mb":       round(saved_mb, 2),
        "saved_pct":      saved_pct,
        "encoder_used":   encoder_used,
        "error":          None,
    }
