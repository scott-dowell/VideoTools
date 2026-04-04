"""
VideoConverter — conversion engine.

Two modes:
  compress_simple()  — Normal mode. One FFmpeg call, copy audio/subs, keep
                       source container (MKV→MKV, MP4→MP4). Always attempts
                       QSV, falls back to libx265. Discards output if not
                       smaller than source.

  (anime pipeline)   — TODO: port from convert_videos.py. Full remux, AAC
                       transcode, OCR subtitle handling, track filtering.
"""
from __future__ import annotations

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
