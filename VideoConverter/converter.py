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
        "-stats_period", "1",
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
        "-stats_period", "1",
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
            encoding='utf-8',
            errors='replace',
        )
        if pid_holder is not None:
            pid_holder[0] = proc.pid

        for line in proc.stdout:
            line_s = line.rstrip()
            if stop_event.is_set():
                proc.kill()
                proc.wait()
                log("Stopped by user.")
                return False

            if progress_cb and duration_secs > 0:
                m = _PROGRESS_RE.search(line_s)
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
                    continue   # progress line — don't clutter log

            # Log all non-progress, non-empty lines so errors are visible
            if line_s:
                log(line_s)

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

def _verify_tracks_preserved(src_path: str, out_path: str) -> tuple[bool, str]:
    """
    Probe source and output with ffprobe and verify that no audio or subtitle
    tracks were silently dropped.

    Rules:
      - Output must have at least as many audio streams as the source.
      - If the source has any subtitle streams, the output must have at least one.

    Returns (True, "") on pass, (False, reason) on fail.
    """
    def _probe_streams(path: str) -> list:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", path],
            capture_output=True, text=True, timeout=30,
        )
        return json.loads(r.stdout).get("streams", [])

    try:
        src_streams = _probe_streams(src_path)
        out_streams = _probe_streams(out_path)
    except Exception as exc:
        return False, f"ffprobe error during track verification: {exc}"

    src_audio = sum(1 for s in src_streams if s.get("codec_type") == "audio")
    out_audio = sum(1 for s in out_streams if s.get("codec_type") == "audio")
    src_subs  = sum(1 for s in src_streams if s.get("codec_type") == "subtitle")
    out_subs  = sum(1 for s in out_streams if s.get("codec_type") == "subtitle")

    if out_audio < src_audio:
        return False, (
            f"audio tracks dropped: source had {src_audio}, output has {out_audio}"
        )
    if src_subs > 0 and out_subs == 0:
        return False, (
            f"subtitles lost: source had {src_subs} subtitle track(s), output has none"
        )
    return True, ""


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
# Anime mode helpers
# ---------------------------------------------------------------------------

def _aac_encoder() -> str:
    """Return the correct AAC encoder for this platform.

    On Windows the built-in 'aac' encoder triggers an FP overflow on E-cores.
    'aac_mf' (Media Foundation) does not have this bug.
    """
    import sys
    return "aac_mf" if sys.platform == "win32" else "aac"


def is_hi10(input_path: str) -> bool:
    """Return True when the first video stream is 10-bit H.264 (Hi10P).

    QSV cannot decode 10-bit H.264, so Hi10 files must be remuxed without
    re-encoding the video stream.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-select_streams", "v:0",
                "-print_format", "json",
                "-show_streams",
                input_path,
            ],
            capture_output=True, text=True, timeout=15,
        )
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        if not streams:
            return False
        s = streams[0]
        codec = s.get("codec_name", "").lower()
        if codec != "h264":
            return False
        # bits_per_raw_sample may be int or string depending on ffprobe version
        bps = s.get("bits_per_raw_sample", 0)
        try:
            bps = int(bps)
        except (TypeError, ValueError):
            bps = 0
        # Also check pix_fmt — yuv420p10* indicates 10-bit
        pix_fmt = s.get("pix_fmt", "")
        return bps >= 10 or "420p10" in pix_fmt or "422p10" in pix_fmt or "444p10" in pix_fmt
    except Exception:
        return False


def _is_potentially_english(lang: str, title: str) -> bool:
    """Return True if a subtitle stream should be treated as English.

    Keeps a track when:
    - lang tag is 'eng', 'en', or 'und' (undefined — could be English)
    - OR the track title contains 'english', 'full', 'signs', or 'song'
    """
    lang  = (lang  or "").strip().lower()
    title = (title or "").strip().lower()
    if lang in ("eng", "en", "und", ""):
        return True
    english_keywords = ("english", "full", "signs", "song", "forced")
    return any(kw in title for kw in english_keywords)


def remux_to_mp4(
    input_path: str,
    output_dir: str,
    log: LogFn,
    stop_event: threading.Event,
    quality: int | None = None,
    progress_cb: ProgressCb | None = None,
    pid_holder: list[int] | None = None,
    hi10: bool | None = None,
) -> tuple[bool, str]:
    """
    Anime-mode remux into MP4.

    Decision tree:
      1. MP4 fast-path: already .mp4, audio is AAC, no bitmap subs
         → skip remux; go straight to compress_simple() (QSV/SW).
      2. Hi10 H.264 (or hi10=True): copy video stream, transcode audio to AAC.
         No QSV (it can't decode 10-bit H.264).
      3. Everything else: QSV/SW video compress → AAC audio → mov_text subs.

    Subtitle handling:
    - Text subs (ASS/SRT/subrip/srt/webvtt): map to mov_text, keep English tracks only.
    - Bitmap subs (PGS/VOBSUB): OCR → SRT → embed as mov_text.
    - Foreign-only subs: silently dropped.

    DTS overflow recovery:
    - If the first attempt fails with "DTS ... out of order", retry once with
      -max_interleave_delta 0.
    - If that also fails, retry without subtitle streams.

    Returns (True, encoder_used) or (False, "").
    """
    quality    = quality if quality is not None else config.QSV_QUALITY
    input_path = os.path.normpath(input_path)
    src_size   = os.path.getsize(input_path)
    duration   = _ffprobe_duration(input_path)

    # ------------------------------------------------------------------
    # Probe streams
    # ------------------------------------------------------------------
    try:
        probe = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_streams",
                input_path,
            ],
            capture_output=True, text=True, timeout=30,
        )
        probe_data = json.loads(probe.stdout)
        streams = probe_data.get("streams", [])
    except Exception as exc:
        log(f"ERROR: ffprobe failed: {exc}")
        return False, ""

    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    sub_streams   = [s for s in streams if s.get("codec_type") == "subtitle"]

    if not video_streams:
        log("ERROR: no video stream found")
        return False, ""

    v = video_streams[0]
    v_codec  = v.get("codec_name", "").lower()
    v_index  = v.get("index", 0)

    # ------------------------------------------------------------------
    # Determine if hi10
    # ------------------------------------------------------------------
    if hi10 is None:
        hi10 = is_hi10(input_path)

    # ------------------------------------------------------------------
    # MP4 fast-path: already .mp4, all audio is aac, no bitmap subs
    # ------------------------------------------------------------------
    suffix = Path(input_path).suffix.lower()
    if suffix == ".mp4":
        has_bitmap = any(
            s.get("codec_name", "").lower() in ("hdmv_pgs_subtitle", "dvd_subtitle")
            for s in sub_streams
        )
        all_aac = all(
            s.get("codec_name", "").lower() in ("aac", "aac_latm")
            for s in audio_streams
        ) if audio_streams else True
        if all_aac and not has_bitmap:
            log("MP4 fast-path: already MP4 + AAC, no bitmap subs — compressing directly.")
            return compress_simple(
                input_path, output_dir, log, stop_event,
                quality=quality, progress_cb=progress_cb, pid_holder=pid_holder,
            )

    # ------------------------------------------------------------------
    # Subtitle classification
    # ------------------------------------------------------------------
    TEXT_SUB_CODECS   = {"ass", "subrip", "srt", "webvtt", "mov_text"}
    BITMAP_SUB_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "pgssub", "vobsub"}

    english_text_subs   = []   # (stream_index, codec_name)
    bitmap_sub_indices  = []   # stream index list for OCR

    for s in sub_streams:
        codec = s.get("codec_name", "").lower()
        tags  = s.get("tags", {})
        lang  = tags.get("language", "")
        title = tags.get("title", "")
        eng   = _is_potentially_english(lang, title)
        if codec in TEXT_SUB_CODECS:
            if eng:
                english_text_subs.append(s["index"])
        elif codec in BITMAP_SUB_CODECS:
            if eng:
                bitmap_sub_indices.append(s["index"])

    # If no English subs kept at all but there's exactly one sub track, keep it
    # (sole-sub rule: likely English even if not tagged)
    if not english_text_subs and not bitmap_sub_indices and len(sub_streams) == 1:
        s = sub_streams[0]
        codec = s.get("codec_name", "").lower()
        if codec in TEXT_SUB_CODECS:
            english_text_subs.append(s["index"])
        elif codec in BITMAP_SUB_CODECS:
            bitmap_sub_indices.append(s["index"])

    # ------------------------------------------------------------------
    # OCR bitmap subs
    # ------------------------------------------------------------------
    import bitmap_subs as _bsubs

    srt_paths: list[str] = []
    if bitmap_sub_indices and not _bsubs.DEPS_OK:
        log(
            "ERROR: bitmap subtitle track(s) found but OCR dependencies are not installed. "
            "Run: pip install easyocr Pillow pysubs2"
        )
        return False, ""
    if bitmap_sub_indices:
        log(f"OCR bitmap subs ({len(bitmap_sub_indices)} track(s))...")
        try:
            srt_paths = _bsubs.ocr_bitmap_subs_to_srt(
                input_path=input_path,
                lang="en",
                out_dir=config.LOCAL_TEMP_DIR,
                all_streams=False,
                verbose=False,
                log_fn=log,
            )
        except Exception as exc:
            log(f"ERROR: bitmap sub OCR failed: {exc} — aborting to preserve subtitles")
            return False, ""

    # ------------------------------------------------------------------
    # Build output path
    # ------------------------------------------------------------------
    out_name   = Path(input_path).stem + ".mp4"
    final_path = os.path.join(output_dir, out_name)
    os.makedirs(config.LOCAL_TEMP_DIR, exist_ok=True)
    tmp_path   = os.path.join(config.LOCAL_TEMP_DIR, out_name)

    aac = _aac_encoder()

    def _build_cmd(include_subs: bool, extra_flags: list[str] | None = None) -> list[str]:
        """Build the ffmpeg remux command."""
        cmd = ["ffmpeg", "-y", "-i", input_path]

        # Append SRT files as additional inputs
        for srt in srt_paths:
            cmd += ["-i", srt]

        # Video
        if hi10:
            cmd += ["-map", f"0:{v_index}", "-c:v", "copy"]
            encoder_tag = "copy"
        else:
            # QSV encode
            cmd += [
                "-map", f"0:{v_index}",
                "-c:v", "hevc_qsv",
                "-global_quality", str(quality),
                "-tag:v", "hvc1",
            ]
            encoder_tag = "hevc_qsv"

        # Audio — ALL tracks → AAC transcode
        for i, a in enumerate(audio_streams):
            ai = a.get("index", 0)
            cmd += ["-map", f"0:{ai}", f"-c:a:{i}", aac]

        # Subtitles
        if include_subs:
            # Text subs from source
            for si in english_text_subs:
                cmd += ["-map", f"0:{si}"]
            # OCR'd SRT files (inputs 1..N)
            for j in range(len(srt_paths)):
                cmd += ["-map", f"{j+1}:s:0"]
            # Tag OCR'd tracks with English language (they were selected as English above)
            text_sub_offset = len(english_text_subs)
            for j in range(len(srt_paths)):
                cmd += [f"-metadata:s:s:{text_sub_offset + j}", "language=eng"]
            # Convert all subtitle streams to mov_text
            sub_count = len(english_text_subs) + len(srt_paths)
            if sub_count > 0:
                cmd += ["-c:s", "mov_text"]

        if extra_flags:
            cmd += extra_flags

        cmd += ["-movflags", "+faststart", tmp_path]
        return cmd, encoder_tag

    # ------------------------------------------------------------------
    # Run with DTS overflow recovery
    # ------------------------------------------------------------------
    try:
        encoder_used = ""

        for attempt, (inc_subs, extra) in enumerate([
            (True,  None),                          # attempt 0: normal
            (True,  ["-max_interleave_delta", "0"]), # attempt 1: DTS fix
            (False, None),                           # attempt 2: no subs
        ]):
            if stop_event.is_set():
                return False, ""

            cmd, enc = _build_cmd(inc_subs, extra)
            encoder_used = enc
            if attempt == 1:
                log("DTS overflow detected — retrying with -max_interleave_delta 0")
            elif attempt == 2:
                if english_text_subs or bitmap_sub_indices:
                    log(
                        "ERROR: DTS error could not be resolved with subtitles present. "
                        "Aborting to preserve subtitle tracks."
                    )
                    return False, ""
                log("DTS retry failed — retrying without subtitle streams")

            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

            # Capture stderr separately so we can inspect it for DTS errors
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

                output_lines: list[str] = []
                dts_error = False

                for line in proc.stdout:
                    output_lines.append(line)
                    if stop_event.is_set():
                        proc.kill()
                        proc.wait()
                        log("Stopped by user.")
                        return False, ""
                    # DTS overflow detection
                    if "out of order" in line.lower() or "dts" in line.lower() and "out of order" in line.lower():
                        dts_error = True
                    if progress_cb and duration > 0:
                        m = _PROGRESS_RE.search(line)
                        if m:
                            elapsed  = _parse_time(m.group("time"))
                            fps      = float(m.group("fps") or 0)
                            pct      = min(100.0, elapsed / duration * 100)
                            remaining = (duration - elapsed) / fps if fps > 0 else 0
                            eta_secs  = max(0, int(remaining))
                            try:
                                progress_cb(pct, fps, eta_secs)
                            except Exception:
                                pass

                proc.wait()
                if pid_holder is not None:
                    pid_holder[0] = 0

            except FileNotFoundError:
                log("ERROR: ffmpeg not found.")
                return False, ""

            if proc.returncode == 0 and os.path.exists(tmp_path):
                break  # success

            # Check if DTS error; if not, no point retrying sub-related attempts
            full_output = "".join(output_lines)
            if "out of order" not in full_output.lower():
                if attempt == 0:
                    # Non-DTS failure — bail immediately
                    log("Remux failed.")
                    return False, ""
                # else let the loop try next approach

        else:
            log("All remux attempts failed.")
            return False, ""

        if not os.path.exists(tmp_path):
            log("Remux failed — no output file.")
            return False, ""

        # --------------------------------------------------------------
        # Size check — skip if output not smaller (not for Hi10 copy)
        # --------------------------------------------------------------
        enc_size = os.path.getsize(tmp_path)
        if not hi10 and enc_size >= src_size:
            log(f"Output not smaller ({enc_size:,} >= {src_size:,} bytes). Skipping.")
            os.remove(tmp_path)
            return False, ""

        os.makedirs(output_dir, exist_ok=True)
        os.replace(tmp_path, final_path)
        saved = max(0, src_size - enc_size)
        log(f"Done. Saved {saved/1024/1024:.1f} MB → {final_path}")
        return True, encoder_used

    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        # Clean up any temp SRT files we created
        for srt in srt_paths:
            if os.path.exists(srt):
                try:
                    os.remove(srt)
                except OSError:
                    pass


def compress_and_remux(
    input_path: str,
    output_dir: str,
    log: LogFn,
    stop_event: threading.Event,
    quality: int | None = None,
    progress_cb: ProgressCb | None = None,
    pid_holder: list[int] | None = None,
) -> tuple[bool, str]:
    """
    Anime normal-H.264 path: compress with QSV/SW first, then remux the
    resulting MKV to MP4 (AAC audio, mov_text subs).

    Two-pass approach keeps things simple — compress_simple handles the
    codec decision, then remux_to_mp4 handles the container conversion.
    """
    import tempfile

    # Step 1: compress to a unique temp subdir so the path never collides
    # with compress_simple's own internal tmp_path (which also lives in
    # LOCAL_TEMP_DIR).  Using a separate subdir avoids the finally-block
    # self-delete bug when output_dir == config.LOCAL_TEMP_DIR.
    os.makedirs(config.LOCAL_TEMP_DIR, exist_ok=True)
    compress_dir = tempfile.mkdtemp(dir=config.LOCAL_TEMP_DIR, prefix="_cr_")

    ok, encoder_used = compress_simple(
        input_path=input_path,
        output_dir=compress_dir,
        log=log,
        stop_event=stop_event,
        quality=quality,
        progress_cb=progress_cb,
        pid_holder=pid_holder,
    )

    if not ok:
        # Clean up the temp dir
        try:
            import shutil
            shutil.rmtree(compress_dir, ignore_errors=True)
        except Exception:
            pass
        return False, encoder_used

    suffix   = Path(input_path).suffix.lower()
    out_ext  = suffix if suffix in (".mp4", ".mkv", ".m4v") else ".mkv"
    intermediate = os.path.join(compress_dir, Path(input_path).stem + out_ext)

    if not os.path.exists(intermediate):
        log("ERROR: intermediate file missing after compress_simple")
        return False, encoder_used

    # Step 2: remux the compressed MKV to MP4.
    # Video is already HEVC from step 1 — copy it, don't re-encode.
    # hi10=True tells remux_to_mp4 to use -c:v copy and skip the
    # "output not smaller" size check (savings were established in step 1).
    log("Remuxing compressed output to MP4...")
    ok2, _ = remux_to_mp4(
        input_path=intermediate,
        output_dir=output_dir,
        log=log,
        stop_event=stop_event,
        quality=quality,
        progress_cb=None,   # don't double-report progress
        pid_holder=pid_holder,
        hi10=True,
    )

    # Clean up intermediate and its temp dir
    try:
        import shutil
        shutil.rmtree(compress_dir, ignore_errors=True)
    except Exception:
        pass

    if not ok2:
        return False, encoder_used

    return True, encoder_used


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
        if is_hi10(input_path):
            log("Hi10 H.264 detected — using remux path (no re-encode).")
            ok, encoder_used = remux_to_mp4(
                input_path=input_path,
                output_dir=output_dir,
                log=log,
                stop_event=stop_event,
                quality=quality,
                progress_cb=progress_cb,
                pid_holder=pid_holder,
                hi10=True,
            )
        else:
            log("Anime mode: compressing then remuxing to MP4.")
            ok, encoder_used = compress_and_remux(
                input_path=input_path,
                output_dir=output_dir,
                log=log,
                stop_event=stop_event,
                quality=quality,
                progress_cb=progress_cb,
                pid_holder=pid_holder,
            )

        if not ok:
            return {
                "ok":             False,
                "output_path":    None,
                "output_size_mb": 0.0,
                "saved_mb":       0.0,
                "saved_pct":      0,
                "encoder_used":   encoder_used,
                "error":          "anime encode failed",
            }

        out_name     = Path(input_path).stem + ".mp4"
        output_path  = os.path.join(output_dir, out_name)
        ok_verify, reason = _verify_output(output_path, duration)
        if not ok_verify:
            log(f"Integrity check failed: {reason}")
            return {
                "ok":             False,
                "output_path":    output_path,
                "output_size_mb": 0.0,
                "saved_mb":       0.0,
                "saved_pct":      0,
                "encoder_used":   encoder_used,
                "error":          f"integrity: {reason}",
            }

        out_size  = os.path.getsize(output_path)
        out_mb    = out_size / (1024 * 1024)
        saved_mb  = max(0.0, src_mb - out_mb)
        saved_pct = int(saved_mb / src_mb * 100) if src_mb > 0 else 0
        return {
            "ok":             True,
            "output_path":    output_path,
            "output_size_mb": round(out_mb, 2),
            "saved_mb":       round(saved_mb, 2),
            "saved_pct":      saved_pct,
            "encoder_used":   encoder_used,
            "error":          None,
        }

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
