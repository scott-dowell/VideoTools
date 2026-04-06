"""
VideoConverter — conversion engine.

Two modes:
  compress_simple()  — Normal mode. One FFmpeg call, copy audio/subs, keep
                       source container (MKV→MKV, MP4→MP4). Always attempts
                       QSV, falls back to libx265. Discards output if not
                       smaller than source.

  estimate()         — Quick savings estimator. Encodes 10 s from the middle
                       of the file with QSV and extrapolates compression ratio.

  (anime pipeline)   — TODO: port from convert_videos.py. Full remux, AAC
                       transcode, OCR subtitle handling, track filtering.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

import config

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------
LogFn = Callable[[str], None]


# ---------------------------------------------------------------------------
# FFmpeg command builders
# ---------------------------------------------------------------------------

def _qsv_cmd(input_path: str, output_path: str) -> list[str]:
    return [
        "ffmpeg", "-y",
        "-i", input_path,
        "-c:v", "hevc_qsv",
        "-global_quality", str(config.QSV_QUALITY),
        "-c:a", "copy",
        "-c:s", "copy",
        "-map", "0:v:0",
        "-map", "0:a?",
        "-map", "0:s?",
        output_path,
    ]


def _sw_cmd(input_path: str, output_path: str) -> list[str]:
    return [
        "ffmpeg", "-y",
        "-i", input_path,
        "-c:v", "libx265",
        "-crf", str(config.SW_HEVC_CRF),
        "-preset", "medium",
        "-c:a", "copy",
        "-c:s", "copy",
        "-map", "0:v:0",
        "-map", "0:a?",
        "-map", "0:s?",
        output_path,
    ]


# ---------------------------------------------------------------------------
# Core encode helper
# ---------------------------------------------------------------------------

def _run_ffmpeg(cmd: list[str], log: LogFn, stop_event) -> bool:
    """Run an FFmpeg command, return True on success."""
    log(f"Running: {' '.join(cmd)}")
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for line in proc.stdout:
            if stop_event.is_set():
                proc.kill()
                log("Stopped by user.")
                return False
        proc.wait()
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
    stop_event,
) -> bool:
    """
    Normal-mode compression.

    - Keeps source container (MP4→MP4, MKV→MKV, anything else→MKV)
    - Copies all audio and subtitle tracks unchanged
    - Tries hevc_qsv first, falls back to libx265
    - Discards output and returns False if result is not smaller than source

    Returns True if a smaller output file was produced.
    """
    input_path = os.path.normpath(input_path)
    src_size = os.path.getsize(input_path)

    suffix = Path(input_path).suffix.lower()
    out_ext = suffix if suffix in (".mp4", ".mkv", ".m4v") else ".mkv"
    out_name = Path(input_path).stem + out_ext

    os.makedirs(config.LOCAL_TEMP_DIR, exist_ok=True)
    tmp_path = os.path.join(config.LOCAL_TEMP_DIR, out_name)
    final_path = os.path.join(output_dir, out_name)

    try:
        # Try QSV first
        success = _run_ffmpeg(_qsv_cmd(input_path, tmp_path), log, stop_event)
        if not success:
            log("QSV failed, trying software encoder...")
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            success = _run_ffmpeg(_sw_cmd(input_path, tmp_path), log, stop_event)

        if not success or not os.path.exists(tmp_path):
            log("Encode failed.")
            return False

        enc_size = os.path.getsize(tmp_path)
        if enc_size >= src_size:
            log(f"Output not smaller ({enc_size:,} >= {src_size:,} bytes). Skipping.")
            os.remove(tmp_path)
            return False

        os.makedirs(output_dir, exist_ok=True)
        os.replace(tmp_path, final_path)
        saved = src_size - enc_size
        log(f"Done. Saved {saved / 1024 / 1024:.1f} MB → {final_path}")
        return True

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
