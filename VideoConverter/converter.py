"""
VideoConverter — conversion engine.

Two modes:
  compress_simple()  — Normal mode. One FFmpeg call, copy audio/subs, keep
                       source container (MKV→MKV, MP4→MP4). Always attempts
                       QSV, falls back to libx265. Discards output if not
                       smaller than source.

    estimate()         — Quick savings estimator. Encodes multiple short clips
                                             spread across the file with QSV, then uses robust
                                             aggregation to reduce outlier bias.

  convert_video()    — Dispatcher: normal mode (compress_simple) or anime
                       mode (Phase 3b). This is the function called by the
                       queue worker thread in app.py.
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from statistics import mean, median, pstdev
from typing import Callable

import config
import conv_log as _conv_log

# ---------------------------------------------------------------------------
# Persistent OCR worker — Tesseract, handles all queue jobs via bitmap_subs.py --server
# ---------------------------------------------------------------------------

_ocr_worker: 'subprocess.Popen | None' = None
_ocr_worker_lock = threading.Lock()
# Rolling stderr buffer for the OCR worker — drained by a background thread so
# the pipe never fills up and blocks.  Read with _ocr_stderr_buf.getvalue().
_ocr_stderr_buf: 'queue.SimpleQueue[str] | None' = None  # lines are enqueued
_ocr_stderr_lines: list[str] = []   # bounded ring buffer, max 50 lines


def _ocr_stderr_drain(proc: 'subprocess.Popen') -> None:
    """Background thread: drain OCR worker stderr into _ocr_stderr_lines."""
    global _ocr_stderr_lines
    try:
        for line in proc.stderr:
            _ocr_stderr_lines = (_ocr_stderr_lines + [line.rstrip()])[-50:]
    except Exception:
        pass


def _get_ocr_worker() -> 'subprocess.Popen':
    """Return the persistent OCR worker process, starting it if needed."""
    global _ocr_worker, _ocr_stderr_lines
    with _ocr_worker_lock:
        if _ocr_worker is None or _ocr_worker.poll() is not None:
            _bsubs_script = os.path.join(os.path.dirname(__file__), "bitmap_subs.py")
            _ocr_stderr_lines = []
            _ocr_worker = subprocess.Popen(
                [sys.executable, _bsubs_script, "--server"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,  # drained by background thread
                text=True,
                bufsize=1,
            )
            t = threading.Thread(target=_ocr_stderr_drain, args=(_ocr_worker,),
                                 daemon=True, name="ocr-stderr-drain")
            t.start()
        return _ocr_worker


# ---------------------------------------------------------------------------
# Windows P-core helper
# ---------------------------------------------------------------------------

def _run_with_pcores_only(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """
    Run a subprocess restricted to P-cores only on Windows (avoids the native
    aac FP-overflow crash on Intel Gracemont E-cores).

    On non-Windows (or if affinity API is unavailable), falls back to a plain
    subprocess.run().

    Strategy: spawn the process, immediately call SetProcessAffinityMask to
    exclude the top 8 logical CPUs (which are E-cores on i7-12xxx/13xxx/14xxx),
    then wait for it to finish.
    """
    if os.name != "nt":
        return subprocess.run(cmd, **kwargs)

    import ctypes
    kernel32 = ctypes.windll.kernel32

    cpu_count = os.cpu_count() or 1
    # Assume the top 8 logical CPUs are E-cores on Intel hybrid chips.
    # If the machine has ≤ 8 CPUs, use all of them (no E-cores to exclude).
    e_core_count = 8
    if cpu_count > e_core_count:
        p_core_mask = (1 << (cpu_count - e_core_count)) - 1
    else:
        p_core_mask = (1 << cpu_count) - 1

    # Pop timeout from kwargs — Popen doesn't accept it; we'll use communicate()
    timeout = kwargs.pop("timeout", None)

    # subprocess.run keyword args that map to Popen
    proc = subprocess.Popen(cmd, **kwargs)
    # Set affinity immediately after spawn
    try:
        kernel32.SetProcessAffinityMask(proc._handle, p_core_mask)
    except Exception:
        pass  # Non-fatal — process will still run, just might hit E-cores

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        raise

    return subprocess.CompletedProcess(
        args=cmd,
        returncode=proc.returncode,
        stdout=stdout,
        stderr=stderr,
    )


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------
LogFn      = Callable[[str], None]
ProgressCb = Callable[[float, float, int], None]   # pct, fps, eta_secs


def _should_keep_failed_intermediates() -> bool:
    return bool(getattr(config, "KEEP_FAILED_INTERMEDIATES", False))


def _preserve_failed_artifacts(paths: list[str], input_path: str, log: LogFn) -> str:
    """Move failed temp artifacts into a timestamped review directory."""
    keep_root = os.path.join(config.LOCAL_TEMP_DIR, "_failed_intermediates")
    stem = Path(input_path).stem
    stamp = time.strftime("%Y%m%d_%H%M%S")
    base_dir = os.path.join(keep_root, f"{stem}_{stamp}")
    keep_dir = base_dir
    suffix = 1
    while os.path.exists(keep_dir):
        suffix += 1
        keep_dir = f"{base_dir}_{suffix}"

    os.makedirs(keep_dir, exist_ok=True)

    kept = 0
    for src in paths:
        if not src or not os.path.exists(src):
            continue
        name = os.path.basename(os.path.normpath(src))
        dst = os.path.join(keep_dir, name)

        try:
            if os.path.isdir(src):
                if os.path.exists(dst):
                    shutil.rmtree(dst, ignore_errors=True)
                shutil.move(src, dst)
            else:
                # Avoid collisions if two artifacts share the same basename.
                if os.path.exists(dst):
                    root, ext = os.path.splitext(name)
                    i = 2
                    while os.path.exists(dst):
                        dst = os.path.join(keep_dir, f"{root}_{i}{ext}")
                        i += 1
                shutil.move(src, dst)
            kept += 1
        except Exception as exc:
            log(f"WARNING: could not preserve artifact {src}: {exc}")

    if kept:
        log(f"Preserved failed intermediates in: {keep_dir}")
        return keep_dir

    # Remove the empty directory if nothing could be preserved.
    try:
        os.rmdir(keep_dir)
    except OSError:
        pass
    return ""


# ---------------------------------------------------------------------------
# FFmpeg command builders
# ---------------------------------------------------------------------------

# Codecs with a reliable QSV decoder on Intel Quick Sync hardware.
_QSV_DECODERS: dict[str, str] = {
    "h264":        "h264_qsv",
    "hevc":        "hevc_qsv",
    "mpeg2video":  "mpeg2_qsv",
    "vp9":         "vp9_qsv",
    "vc1":         "vc1_qsv",
    "mjpeg":       "mjpeg_qsv",
}

# Codec fourCCs found in non-standard containers (e.g. HEVC in ASF/WMV) where
# the demuxer does not map the tag to a codec_name.  Key: codec_tag_string from
# ffprobe; Value: ffmpeg SW decoder name to force before -i.
_TAG_CODEC_MAP: dict[str, str] = {
    "hvc1": "hevc", "hev1": "hevc",
    "HVC1": "hevc", "HEV1": "hevc",
    "avc1": "h264", "avc3": "h264",
    "AVC1": "h264", "AVC3": "h264",
}


def _ffprobe_vcodec(input_path: str) -> tuple[str, str]:
    """Return (codec_name, input_decoder).

    codec_name:    identified codec (empty string if unrecognisable).
    input_decoder: non-empty when the container demuxer cannot auto-detect the
                   codec (e.g. hvc1 tag in an ASF/WMV file) and ffmpeg needs an
                   explicit SW decoder forced before -i.
    """
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-select_streams", "v:0", input_path],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
        )
        streams = json.loads(result.stdout).get("streams", [])
        if not streams:
            return "", ""
        s = streams[0]
        codec_name = (s.get("codec_name") or "").lower()
        if codec_name and codec_name != "none":
            return codec_name, ""   # normal path — demuxer identified the codec
        # Demuxer reported null/none — check codec_tag_string for a known fourCC.
        tag = s.get("codec_tag_string") or ""
        mapped = _TAG_CODEC_MAP.get(tag, "")
        if mapped:
            return mapped, mapped   # codec known via tag; must force SW decoder
        return "", ""
    except Exception:
        return "", ""


def _ffprobe_source_fps(input_path: str) -> float:
    """Return source video FPS from ffprobe, or 0.0 if unknown."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-select_streams", "v:0", input_path],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
        )
        streams = json.loads(result.stdout or "{}").get("streams", [])
        if not streams:
            return 0.0
        s = streams[0]
        for key in ("avg_frame_rate", "r_frame_rate"):
            raw = (s.get(key) or "").strip()
            if not raw or raw in ("N/A", "0/0"):
                continue
            if "/" in raw:
                num_s, den_s = raw.split("/", 1)
                num = float(num_s)
                den = float(den_s)
                if den > 0:
                    fps = num / den
                    if fps > 0:
                        return fps
            else:
                fps = float(raw)
                if fps > 0:
                    return fps
    except Exception:
        pass
    return 0.0


def _qsv_cmd(input_path: str, output_path: str, quality: int,
             dropped_streams: list[int] | None = None,
             v_codec: str = "",
             input_decoder: str = "",
             transcode_audio: bool = False) -> list[str]:
    # When input_decoder is set, the container demuxer cannot identify the
    # codec (e.g. hvc1-in-ASF); force SW decode — QSV hw decode won't work.
    # Otherwise use QSV hw decode when a suitable decoder exists.
    if input_decoder:
        pre_input = ["-c:v", input_decoder]
    else:
        qsv_dec = _QSV_DECODERS.get(v_codec or "")
        pre_input = ["-hwaccel", "qsv", "-hwaccel_output_format", "qsv", "-c:v", qsv_dec] if qsv_dec else []
    cmd = [
        "ffmpeg", "-y",
        "-stats_period", "1",
        "-progress", "pipe:1",
        "-fflags", "+discardcorrupt",
        "-probesize", "100M",
        "-analyzeduration", "100M",
    ] + pre_input + [
        "-i", input_path,
        "-c:v", "hevc_qsv",
        "-global_quality", str(quality),
        "-c:a", "aac" if transcode_audio else "copy",
        "-c:s", "copy",
        "-map", "0:v:0",
        "-map", "0:a?",
        "-map", "0:s?",
    ]
    for idx in (dropped_streams or []):
        cmd += ["-map", f"-0:{idx}"]
    cmd.append(output_path)
    return cmd


def _sw_cmd(input_path: str, output_path: str, quality: int,
            dropped_streams: list[int] | None = None,
            input_decoder: str = "",
            transcode_audio: bool = False) -> list[str]:
    pre_input = ["-c:v", input_decoder] if input_decoder else []
    cmd = [
        "ffmpeg", "-y",
        "-stats_period", "1",
        "-progress", "pipe:1",
        "-fflags", "+discardcorrupt",
        "-probesize", "100M",
        "-analyzeduration", "100M",
    ] + pre_input + [
        "-i", input_path,
        "-c:v", "libx265",
        "-crf", str(quality),
        "-preset", "medium",
        "-c:a", "aac" if transcode_audio else "copy",
        "-c:s", "copy",
        "-map", "0:v:0",
        "-map", "0:a?",
        "-map", "0:s?",
    ]
    for idx in (dropped_streams or []):
        cmd += ["-map", f"-0:{idx}"]
    cmd.append(output_path)
    return cmd


# ---------------------------------------------------------------------------
# Progress parser
# ---------------------------------------------------------------------------

_PROGRESS_RE = re.compile(
    r"frame=\s*(?P<frame>\d+)\s+fps=\s*(?P<fps>[\d.]+).*?time=(?P<time>[\d:.]+).*?speed=\s*(?P<speed>[\d.]+)x"
)
_PROGRESS_FRAME_ONLY_RE = re.compile(
    r"frame=\s*(?P<frame>\d+)\s+fps=\s*(?P<fps>[\d.]+)"
)

_PROGRESS_KV_KEYS = {
    "frame", "fps", "stream_0_0_q", "bitrate", "total_size",
    "out_time_us", "out_time_ms", "out_time", "dup_frames",
    "drop_frames", "speed", "progress",
}


def _parse_time(ts: str) -> float:
    """Parse 'HH:MM:SS.ss' from ffmpeg stats line into seconds."""
    try:
        parts = ts.split(":")
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except Exception:
        return 0.0


def _fmt_ffmpeg_time(seconds: float) -> str:
    """Format seconds as HH:MM:SS.xx for live ffmpeg status display."""
    try:
        if seconds <= 0:
            return "00:00:00.00"
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = seconds % 60
        return f"{h:02d}:{m:02d}:{s:05.2f}"
    except Exception:
        return "00:00:00.00"


def _frame_progress_pct(frame_n: float, duration_secs: float, source_fps: float) -> float:
    """Estimate percent complete from encoded frame count."""
    try:
        if duration_secs <= 0 or source_fps <= 0:
            return 0.0
        total_frames = duration_secs * source_fps
        if total_frames <= 0:
            return 0.0
        return min(100.0, (frame_n / total_frames) * 100.0)
    except Exception:
        return 0.0


def _frame_eta_secs(frame_n: float, duration_secs: float, source_fps: float, fps_now: float) -> int:
    """Estimate remaining seconds from frame count and current encode FPS."""
    try:
        if duration_secs <= 0 or source_fps <= 0 or fps_now <= 0:
            return 0
        total_frames = duration_secs * source_fps
        return max(0, int((total_frames - frame_n) / fps_now))
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Core encode helper
# ---------------------------------------------------------------------------

def _run_ffmpeg(
    cmd: list[str],
    log: LogFn,
    stop_event: threading.Event,
    duration_secs: float = 0.0,
    source_fps: float = 0.0,
    progress_cb: ProgressCb | None = None,
    pid_holder: list[int] | None = None,   # pid_holder[0] = pid, caller clears
    output_path: str | None = None,        # when set, validate output on non-zero exit
) -> bool:
    """Run an FFmpeg command; return True on success.

    Args:
        cmd:           FFmpeg argv (including 'ffmpeg' itself).
        log:           Callable for log lines.
        stop_event:    Set this to abort the encode.
        duration_secs: Total source duration; enables progress calculation.
        source_fps:    Source stream FPS for frame-based fallback progress
                   when ffmpeg does not provide a usable timestamp.
        progress_cb:   Called with (pct, fps, eta_secs) on each stats line.
        pid_holder:    If provided, pid_holder[0] is set to the child PID so
                       the caller can NtSuspend/NtResume it.
        output_path:   When provided and exit code is non-zero, the output is
                       validated with ffprobe.  If it has a valid duration the
                       non-zero exit is treated as non-fatal (some codecs, e.g.
                       dvd_subtitle copy, cause ffmpeg to exit 1 despite
                       producing a fully valid output).
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

        # Read stdout in a daemon thread — on Windows, Intel QSV's runtime
        # can launch helper processes that inherit the pipe handle.  If we
        # block directly on `for line in proc.stdout:` the loop never sees
        # EOF even after ffmpeg exits because those grandchildren still hold
        # the write-end of the pipe.  Reading via a queue + timeout lets us
        # detect process exit and break out cleanly.
        _line_queue: queue.Queue[str | None] = queue.Queue()

        def _reader() -> None:
            try:
                # ffmpeg often emits live stats with carriage returns (\r)
                # instead of newlines. Split on both CR and LF so progress
                # callbacks continue to update even when no '\n' is emitted.
                if hasattr(proc.stdout, "read"):
                    buf = ""
                    while True:
                        ch = proc.stdout.read(1)
                        if ch == "":
                            if buf:
                                _line_queue.put(buf)
                            break
                        if ch in ("\r", "\n"):
                            if buf:
                                _line_queue.put(buf)
                                buf = ""
                        else:
                            buf += ch
                else:
                    # Test doubles may provide only an iterable stdout.
                    for line in proc.stdout:
                        _line_queue.put(line)
            finally:
                _line_queue.put(None)  # sentinel — always signals completion

        _reader_thread = threading.Thread(target=_reader, daemon=True)
        _reader_thread.start()

        # Track last time we received any output from ffmpeg.
        # If the process produces no output for this many seconds we treat it
        # as hung (e.g. QSV driver freeze at startup) and kill it.
        _HUNG_TIMEOUT_SECS = 120
        _last_output_time  = time.monotonic()
        _kv_frame = "0"
        _kv_bitrate = "N/A"
        _kv_fps = 0.0
        _kv_speed = 0.0

        def _emit_frame_progress() -> None:
            if not progress_cb or duration_secs <= 0 or source_fps <= 0:
                return
            try:
                frame_n = float(_kv_frame or 0)
            except Exception:
                frame_n = 0.0
            pct = _frame_progress_pct(frame_n, duration_secs, source_fps)
            eta_secs = _frame_eta_secs(frame_n, duration_secs, source_fps, _kv_fps)
            try:
                progress_cb(pct, _kv_fps, eta_secs)
            except Exception:
                pass

        def _emit_kv_status() -> None:
            fps_txt = f"{_kv_fps:.1f}" if _kv_fps > 0 else "N/A"
            spd_txt = f"{_kv_speed:.2f}x" if _kv_speed > 0 else "N/A"
            log(
                f"__ffstatus__:frame={_kv_frame} fps={fps_txt} "
                f"bitrate={_kv_bitrate} speed={spd_txt}"
            )

        while True:
            try:
                line = _line_queue.get(timeout=0.5)
                _last_output_time = time.monotonic()
            except queue.Empty:
                # No output yet — check whether we should abort
                if stop_event.is_set():
                    proc.kill()
                    proc.wait()
                    _reader_thread.join(timeout=5)
                    log("Stopped by user.")
                    return False
                # ffmpeg has exited but grandchild processes inherited the pipe
                # handle so the reader thread never sees EOF and never enqueues
                # the sentinel None.  If the process is already dead and no new
                # output has arrived for 2 seconds, we can safely break out.
                if proc.poll() is not None and (time.monotonic() - _last_output_time) > 2:
                    break
                # Kill if the process is alive but has been silent too long
                if proc.poll() is None and (time.monotonic() - _last_output_time) > _HUNG_TIMEOUT_SECS:
                    proc.kill()
                    proc.wait()
                    _reader_thread.join(timeout=5)
                    log(f"ERROR: ffmpeg produced no output for {_HUNG_TIMEOUT_SECS}s — killed (hung process).")
                    return False
                continue

            if line is None:
                # Reader thread reached EOF
                break

            line_s = line.rstrip()
            if stop_event.is_set():
                proc.kill()
                proc.wait()
                _reader_thread.join(timeout=5)
                log("Stopped by user.")
                return False

            if progress_cb and duration_secs > 0:
                # Prefer ffmpeg's machine-readable progress stream when present.
                # This path is more robust than parsing human stats lines.
                _is_single_kv_line = bool(re.match(r"^[a-z0-9_]+=[^\n\r]*$", line_s.strip())) and (
                    not bool(re.search(r"\s+[A-Za-z0-9_]+=", line_s))
                )
                if _is_single_kv_line:
                    k, v = line_s.split("=", 1)
                    k = k.strip()
                    v = v.strip()
                    if k in _PROGRESS_KV_KEYS:
                        if k == "frame":
                            _kv_frame = v or _kv_frame
                            _emit_kv_status()
                            _emit_frame_progress()
                        elif k == "bitrate":
                            _kv_bitrate = v or _kv_bitrate
                            _emit_kv_status()
                        elif k == "fps":
                            try:
                                _kv_fps = float(v)
                                _emit_kv_status()
                                _emit_frame_progress()
                            except Exception:
                                pass
                        elif k == "speed":
                            try:
                                _kv_speed = float(v.rstrip("x"))
                                _emit_kv_status()
                                _emit_frame_progress()
                            except Exception:
                                pass
                        elif k == "progress":
                            _emit_frame_progress()
                        continue

                m = _PROGRESS_RE.search(line_s)
                if m:
                    log(f"__ffstatus__:{line_s}")
                    frame_n  = float(m.group("frame") or 0)
                    fps      = float(m.group("fps") or 0)
                    pct      = _frame_progress_pct(frame_n, duration_secs, source_fps)
                    eta_secs = _frame_eta_secs(frame_n, duration_secs, source_fps, fps)
                    try:
                        progress_cb(pct, fps, eta_secs)
                    except Exception:
                        pass
                    continue   # progress line — don't clutter log

                # Fallback for lines like "time=N/A bitrate=N/A speed=N/A"
                # where ffmpeg still reports frame/fps and encoding is active.
                if source_fps > 0:
                    mf = _PROGRESS_FRAME_ONLY_RE.search(line_s)
                    if mf:
                        log(f"__ffstatus__:{line_s}")
                        try:
                            frame_n = float(mf.group("frame") or 0)
                            fps_now = float(mf.group("fps") or 0)
                            pct = _frame_progress_pct(frame_n, duration_secs, source_fps)
                            eta_secs = _frame_eta_secs(frame_n, duration_secs, source_fps, fps_now)
                            progress_cb(pct, fps_now, eta_secs)
                            continue
                        except Exception:
                            pass

            # Log all non-progress, non-empty lines so errors are visible
            if line_s:
                log(line_s)

        _reader_thread.join(timeout=5)
        proc.wait()
        if pid_holder is not None:
            pid_holder[0] = 0
        if proc.returncode == 0:
            return True
        # Windows exception codes (e.g. 0xC0000005 ACCESS_VIOLATION) indicate a
        # hard process crash.  The output file is unreliable — never treat as success.
        # These show up as large positive ints (>= 0x80000000) or negative ints on Windows.
        _is_windows_crash = (
            proc.returncode < 0
            or proc.returncode >= 0x80000000
        )
        if _is_windows_crash:
            log(
                f"ERROR: ffmpeg crashed with code {proc.returncode} "
                f"(0x{proc.returncode & 0xFFFFFFFF:08X}) — output is unreliable."
            )
            return False
        # Non-zero exit — if an output path was provided, check whether the
        # output is actually valid (some streams such as dvd_subtitle cause
        # ffmpeg to exit non-zero even after a complete, valid encode).
        if output_path and os.path.exists(output_path):
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default", output_path],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            )
            if probe.returncode == 0:
                try:
                    dur = float(
                        next(l for l in probe.stdout.splitlines() if "duration=" in l)
                        .split("=")[1]
                    )
                    if dur > 0:
                        # Require a decodable video stream with packets before
                        # accepting a non-zero ffmpeg exit as success. This
                        # prevents audio-only artifacts (video:0KiB) from being
                        # treated as valid outputs.
                        _has_video_packets = False
                        try:
                            vprobe = subprocess.run(
                                [
                                    "ffprobe", "-v", "error",
                                    "-select_streams", "v:0",
                                    "-count_packets",
                                    "-show_entries", "stream=nb_read_packets",
                                    "-of", "json",
                                    output_path,
                                ],
                                capture_output=True,
                                text=True,
                                encoding="utf-8",
                                errors="replace",
                                timeout=30,
                            )
                            if vprobe.returncode == 0:
                                vj = json.loads(vprobe.stdout or "{}")
                                streams = vj.get("streams") or []
                                if streams:
                                    packets = streams[0].get("nb_read_packets")
                                    _has_video_packets = str(packets).isdigit() and int(packets) > 0
                        except Exception:
                            _has_video_packets = False

                        if not _has_video_packets:
                            log(
                                f"NOTE: ffmpeg exited {proc.returncode} but output has no readable "
                                "video packets — treating as failure."
                            )
                            return False

                        # If we know the source duration, verify the output covers
                        # at least 95% of it.  A truncated output (e.g. from a
                        # corrupt source that caused mid-encode errors) must not be
                        # silently accepted.
                        if duration_secs > 0 and dur < duration_secs * 0.95:
                            log(
                                f"NOTE: ffmpeg exited {proc.returncode} and output "
                                f"duration ({dur:.1f}s) is significantly shorter than "
                                f"source ({duration_secs:.1f}s) — treating as failure."
                            )
                        else:
                            log(
                                f"NOTE: ffmpeg exited {proc.returncode} but output is "
                                "valid — treating as success (non-fatal codec warning)"
                            )
                            return True
                except (ValueError, StopIteration):
                    pass
        return False
    except FileNotFoundError:
        log("ERROR: ffmpeg not found. Is it on PATH?")
        return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _temp_dir_for(output_dir: str) -> str:
    """Return a temp staging directory on the same drive as *output_dir*.

    Keeping temp on the same drive as the output means:
    - os.replace() always succeeds (no cross-drive WinError 17)
    - C:\\ space is not consumed by conversions targeting other drives
    Falls back to config.LOCAL_TEMP_DIR for UNC/network paths.
    """
    drive = os.path.splitdrive(os.path.abspath(output_dir))[0]
    if drive:
        return os.path.join(drive + os.sep, "Temp", "vc_working")
    return config.LOCAL_TEMP_DIR


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
    conv_logger: "_conv_log.ConversionLogger | None" = None,
    tmp_holder: list[str] | None = None,
    force_sw: bool = False,
    dropped_streams: list[int] | None = None,
) -> tuple[bool, str]:
    """
    Normal-mode compression.

    - Keeps source container (MP4→MP4, MKV→MKV, anything else→MKV)
    - Copies all audio and subtitle tracks unchanged (minus any dropped)
    - Tries hevc_qsv first (unless force_sw=True), falls back to libx265
    - Discards output and returns (False, "") if result is not smaller than source

    Returns (True, encoder_used) if a smaller output file was produced,
            (False, "") otherwise.
    """
    quality    = quality    if quality    is not None else config.QSV_QUALITY
    input_path = os.path.normpath(input_path)
    src_size   = os.path.getsize(input_path)
    duration   = _ffprobe_duration(input_path)
    source_fps = _ffprobe_source_fps(input_path)

    suffix   = Path(input_path).suffix.lower()
    # .m4v uses the ipod muxer which doesn't support HEVC — treat as .mp4
    out_ext  = ".mp4" if suffix in (".mp4", ".m4v") else (".mkv" if suffix == ".mkv" else ".mkv")
    out_name = Path(input_path).stem + out_ext

    _local_temp = _temp_dir_for(output_dir)
    os.makedirs(_local_temp, exist_ok=True)
    tmp_path   = os.path.join(_local_temp, out_name)
    final_path = os.path.join(output_dir, out_name)
    if tmp_holder is not None:
        tmp_holder[0] = tmp_path

    # Probe source video codec once — used to enable QSV hardware decoding.
    v_codec, input_decoder = _ffprobe_vcodec(input_path)
    if not v_codec:
        # ffprobe could not identify the video codec — common with DRM-protected WMV
        # files or containers with a non-standard/unrecognised fourCC.  ffmpeg will
        # also fail (error: "no decoder found for: none"), so skip all three retry
        # attempts and fail immediately with a meaningful message.
        log("Video codec unrecognisable (ffprobe returned no codec name). "
            "File may be DRM-protected or use an unsupported codec. Cannot convert.")
        return False, ""
    if input_decoder:
        log(f"Codec identified via container tag (not demuxer): forcing SW -{input_decoder} decoder. "
            "Audio will be transcoded to AAC for container compatibility.")
    transcode_audio = bool(input_decoder)

    encoder_used = ""
    normalized_path = ""
    try:
        # Try QSV first (unless the caller requested SW-only for this file)
        if force_sw:
            log("Force SW mode: skipping hevc_qsv.")
            success = False  # fall straight through to SW block below
        else:
            encoder_used = "hevc_qsv"
            if input_decoder:
                dec_note = f" (sw decode via -{input_decoder}, hw encode)"
            elif v_codec in _QSV_DECODERS:
                dec_note = f" (hw decode via {_QSV_DECODERS[v_codec]})"
            else:
                dec_note = " (sw decode)"
            log(f"Compressing with hevc_qsv{dec_note}...")
            qsv_log = conv_logger.tee(log, "compress_qsv") if conv_logger else log
            success = _run_ffmpeg(
                _qsv_cmd(input_path, tmp_path, quality, dropped_streams, v_codec=v_codec,
                         input_decoder=input_decoder, transcode_audio=transcode_audio),
                qsv_log, stop_event,
                duration_secs=duration, source_fps=source_fps,
                progress_cb=progress_cb, pid_holder=pid_holder,
                output_path=tmp_path,
            )
        if not success:
            if stop_event.is_set():
                return False, ""
            log("QSV failed, trying software encoder...")
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            encoder_used = "libx265"
            sw_quality = quality if quality <= 51 else config.SW_HEVC_CRF
            sw_log = conv_logger.tee(log, "compress_sw") if conv_logger else log
            success = _run_ffmpeg(
                _sw_cmd(input_path, tmp_path, sw_quality, dropped_streams,
                        input_decoder=input_decoder, transcode_audio=transcode_audio),
                sw_log, stop_event,
                duration_secs=duration, source_fps=source_fps,
                progress_cb=progress_cb, pid_holder=pid_holder,
                output_path=tmp_path,
            )

        if not success and not stop_event.is_set():
            # Both QSV and SW failed — try SW again with -err_detect ignore_err
            # to push through corrupt reference frame errors (e.g. "Reference N >= M").
            log("SW failed — retrying with -err_detect ignore_err to handle corrupt source...")
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            encoder_used = "libx265"
            sw_quality = quality if quality <= 51 else config.SW_HEVC_CRF
            sw_log2 = conv_logger.tee(log, "compress_sw_corrupt") if conv_logger else log
            corrupt_cmd = [
                "ffmpeg", "-y",
                "-stats_period", "1",
                "-progress", "pipe:1",
                "-fflags", "+discardcorrupt",
                "-err_detect", "ignore_err",
                "-probesize", "100M",
                "-analyzeduration", "100M",
            ]
            if input_decoder:
                corrupt_cmd += ["-c:v", input_decoder]
            corrupt_cmd += [
                "-i", input_path,
                "-c:v", "libx265",
                "-crf", str(sw_quality),
                "-preset", "medium",
                "-c:a", "aac" if transcode_audio else "copy",
                "-c:s", "copy",
                "-map", "0:v:0",
                "-map", "0:a?",
                "-map", "0:s?",
            ]
            for _di in (dropped_streams or []):
                corrupt_cmd += ["-map", f"-0:{_di}"]
            corrupt_cmd.append(tmp_path)
            success = _run_ffmpeg(
                corrupt_cmd, sw_log2, stop_event,
                duration_secs=duration, source_fps=source_fps,
                progress_cb=progress_cb, pid_holder=pid_holder,
                output_path=tmp_path,
            )

        if (not success or not os.path.exists(tmp_path)) and not stop_event.is_set():
            # Last-resort recovery for badly timestamped sources (often MPEG-TS
            # content with broken PTS/DTS inside an .mp4 extension): stream-copy
            # remux to normalize timestamps, then retry software encode once.
            log("All direct encode attempts failed - trying timestamp-normalization remux, then one final software encode...")
            norm_name = Path(input_path).stem + ".__normalized__.mkv"
            normalized_path = os.path.join(_local_temp, norm_name)
            if os.path.exists(normalized_path):
                os.remove(normalized_path)

            norm_log = conv_logger.tee(log, "normalize_ts") if conv_logger else log
            norm_cmd = [
                "ffmpeg", "-y",
                "-stats_period", "1",
                "-progress", "pipe:1",
                "-fflags", "+genpts+discardcorrupt",
                "-err_detect", "ignore_err",
                "-probesize", "100M",
                "-analyzeduration", "100M",
                "-i", input_path,
                "-map", "0",
                "-c", "copy",
                "-avoid_negative_ts", "make_zero",
                "-muxpreload", "0",
                "-muxdelay", "0",
            ]
            for _di in (dropped_streams or []):
                norm_cmd += ["-map", f"-0:{_di}"]
            norm_cmd.append(normalized_path)

            norm_ok = _run_ffmpeg(
                norm_cmd, norm_log, stop_event,
                pid_holder=pid_holder,
                output_path=normalized_path,
            )

            if norm_ok and os.path.exists(normalized_path):
                log("Timestamp-normalized remux succeeded. Retrying software encode...")
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                encoder_used = "libx265"
                sw_quality = quality if quality <= 51 else config.SW_HEVC_CRF
                sw_log3 = conv_logger.tee(log, "compress_sw_normalized") if conv_logger else log
                success = _run_ffmpeg(
                    _sw_cmd(normalized_path, tmp_path, sw_quality, dropped_streams,
                            input_decoder=None, transcode_audio=transcode_audio),
                    sw_log3, stop_event,
                    duration_secs=duration, source_fps=source_fps,
                    progress_cb=progress_cb, pid_holder=pid_holder,
                    output_path=tmp_path,
                )
            else:
                log("Timestamp-normalization remux failed.")

        if not success or not os.path.exists(tmp_path):
            log("Encode failed.")
            if conv_logger:
                conv_logger.mark_fail_at("compress phase (QSV, SW, SW+ignore_err, and normalize+SW failed)")
            return False, ""

        enc_size = os.path.getsize(tmp_path)
        if enc_size >= src_size:
            log(f"Output not smaller ({enc_size:,} >= {src_size:,} bytes). Skipping.")
            if conv_logger:
                conv_logger.mark_fail_at(
                    f"output not smaller ({enc_size:,} >= {src_size:,} bytes)"
                )
            os.remove(tmp_path)
            return False, "no_savings"

        # Verify audio/subtitle tracks survived before atomically replacing source
        ok_tracks, track_reason = _verify_tracks_preserved(input_path, tmp_path, dropped_streams=dropped_streams)
        if not ok_tracks:
            log(f"ERROR: track verification failed — aborting to preserve source: {track_reason}")
            os.remove(tmp_path)
            return False, ""

        os.makedirs(output_dir, exist_ok=True)
        # Retry the move — on Windows, AV software can briefly lock a newly
        # written file, causing WinError 32.  A short back-off resolves it.
        # os.replace() fails across drives (WinError 17), so fall back to
        # shutil.move() which handles cross-drive by copy+delete.
        import shutil as _shutil
        for _attempt in range(6):
            try:
                os.replace(tmp_path, final_path)
                break
            except OSError as _e:
                if _e.winerror == 17:  # ERROR_NOT_SAME_DEVICE — cross-drive move
                    _shutil.move(tmp_path, final_path)
                    break
                if _e.winerror == 32 and _attempt < 5:  # ERROR_SHARING_VIOLATION
                    import time as _time
                    _time.sleep(0.5)
                    continue
                raise
        saved = src_size - enc_size
        log(f"Done. Saved {saved / 1024 / 1024:.1f} MB → {final_path}")
        return True, encoder_used

    finally:
        if normalized_path and os.path.exists(normalized_path):
            try:
                os.remove(normalized_path)
            except OSError:
                pass
        if tmp_holder is not None:
            tmp_holder[0] = ""
        if os.path.exists(tmp_path):
            for _attempt in range(6):
                try:
                    os.remove(tmp_path)
                    break
                except PermissionError:
                    if _attempt == 5:
                        break  # best-effort cleanup, don't mask the real error
                    import time as _time
                    _time.sleep(0.5)


# ---------------------------------------------------------------------------
# Savings estimator
# ---------------------------------------------------------------------------

def _ffprobe_duration(input_path: str) -> float:
    """Return duration in seconds via ffprobe, or 0.0 on failure.

    Prefers the video stream's actual duration over the container-level
    format duration.  Older MKV files (mkvmerge v7, 2015-era) set
    format.duration to the last chapter end time, which can be several
    minutes longer than the actual video track.  When the video stream
    has a DURATION statistics tag (Matroska) or a direct duration field
    (MP4/TS) that is meaningfully shorter than format.duration, that
    value is used so that _verify_output compares like-for-like.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_format",
                "-show_streams",
                "-select_streams", "v:0",
                input_path,
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
        )
        data = json.loads(result.stdout)
        fmt_dur = float(data["format"]["duration"])

        # Try to get the video stream duration, which is more reliable than
        # the container-level duration for old/malformed MKV files.
        streams = data.get("streams", [])
        if streams:
            v = streams[0]
            v_dur: float | None = None

            # MP4/TS: stream has an explicit duration field
            raw = v.get("duration")
            if raw is not None:
                try:
                    v_dur = float(raw)
                except (TypeError, ValueError):
                    pass

            # MKV: Matroska statistics tag "DURATION" → "HH:MM:SS.mmm..."
            if v_dur is None:
                dur_tag = v.get("tags", {}).get("DURATION", "")
                if dur_tag:
                    try:
                        h, m, s = dur_tag.split(":")
                        v_dur = int(h) * 3600 + int(m) * 60 + float(s)
                    except Exception:
                        pass

            if v_dur is not None and v_dur > 0:
                # If the format duration is more than 2% longer than the
                # video stream duration the container metadata is inflated
                # (e.g. chapter end time past the last frame).  Use the
                # video stream duration as the reference instead.
                if fmt_dur > 0 and (fmt_dur - v_dur) / fmt_dur > 0.02:
                    return v_dur

        return fmt_dur
    except Exception:
        return 0.0


def estimate(input_path: str, quality: int | None = None) -> dict:
    """
    Encode multiple representative clips from input_path with hevc_qsv and
    extrapolate the compression ratio to the full file.

    The sample points are spread across the file and aggregated with robust
    statistics so one unusual section is less likely to dominate the estimate.

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

    # WMV/ASF containers don't support reliable fast-seeking; skip estimation.
    if Path(input_path).suffix.lower() == ".wmv":
        return {"error": "Estimation not supported for WMV"}

    src_size = os.path.getsize(input_path)
    src_mb   = src_size / 1024 / 1024

    duration = _ffprobe_duration(input_path)
    if duration < 20:
        # Too short to sample reliably — skip
        return {"error": "File too short to estimate"}

    _SAMPLE_COUNT = 5
    _CLIP_SECS = 10.0

    clip_secs = min(_CLIP_SECS, duration / 3.0)
    if clip_secs <= 0:
        return {"error": "Duration calculation error"}

    os.makedirs(config.LOCAL_TEMP_DIR, exist_ok=True)

    v_codec, _ = _ffprobe_vcodec(input_path)
    qsv_dec = _QSV_DECODERS.get(v_codec or "")
    hw_args = ["-hwaccel", "qsv", "-hwaccel_output_format", "qsv", "-c:v", qsv_dec] if qsv_dec else []

    sample_centers = [duration * (i + 1) / (_SAMPLE_COUNT + 1) for i in range(_SAMPLE_COUNT)]
    sample_specs: list[tuple[int, float]] = []
    for idx, center in enumerate(sample_centers, start=1):
        seek = max(center - (clip_secs / 2.0), 0.0)
        sample_specs.append((idx, seek))

    try:
        sample_ratios: list[float] = []

        for idx, seek in sample_specs:
            src_tmp = os.path.join(config.LOCAL_TEMP_DIR, f"_est_src_{Path(input_path).stem}_{idx}.mkv")
            enc_tmp = os.path.join(config.LOCAL_TEMP_DIR, f"_est_enc_{Path(input_path).stem}_{idx}.mkv")

            # Clean up any leftover files from previous interrupted runs
            for p in (src_tmp, enc_tmp):
                if os.path.exists(p):
                    try: os.remove(p)
                    except OSError: pass

            # 1. Extract 10s source segment (copy mode)
            # Use -map 0 to match the full stream set so size comparison is fair.
            sp_result = subprocess.run([
                "ffmpeg", "-y",
                "-ss", str(seek),
                "-t",  str(clip_secs),
                "-i",  input_path,
                "-map", "0",
                "-c",   "copy",
                "-f",   "matroska",
                src_tmp
            ], capture_output=True, timeout=60)

            if sp_result.returncode != 0 or not os.path.exists(src_tmp):
                continue
            
            src_seg_bytes = os.path.getsize(src_tmp)
            if src_seg_bytes == 0:
                continue

            # 2. Encode extracted segment
            # We encode the segment file directly to ensure perfect alignment with what we measured.
            cmd = [
                "ffmpeg", "-y",
            ] + hw_args + [
                "-i",  src_tmp,
                "-c:v", "hevc_qsv",
                "-global_quality", str(quality),
                "-c:a", "copy",
                "-c:s", "copy",
                "-f", "matroska",
                enc_tmp,
            ]
            result = subprocess.run(
                cmd, capture_output=True, timeout=120,
            )
            if result.returncode != 0 or not os.path.exists(enc_tmp):
                continue

            enc_seg_size = os.path.getsize(enc_tmp)
            sample_ratios.append(enc_seg_size / src_seg_bytes)

        if not sample_ratios:
            return {"error": "ffmpeg encode failed"}

        ratio_mean = mean(sample_ratios)
        sample_cv_pct = 0.0
        if len(sample_ratios) > 1 and ratio_mean > 0:
            sample_cv_pct = (pstdev(sample_ratios) / ratio_mean) * 100.0

        sorted_ratios = sorted(sample_ratios)
        if len(sorted_ratios) >= 5:
            trim_n = max(1, int(len(sorted_ratios) * 0.2))
            core = sorted_ratios[trim_n: len(sorted_ratios) - trim_n]
            if core:
                ratio = mean(core)
                aggregation = "trimmed_mean_20"
            else:
                ratio = median(sorted_ratios)
                aggregation = "median"
        elif len(sorted_ratios) >= 3:
            ratio = median(sorted_ratios)
            aggregation = "median"
        else:
            ratio = ratio_mean
            aggregation = "mean"

        high_variance = sample_cv_pct >= 45.0

        estimated_output_mb = src_mb * ratio
        estimated_saving_mb = src_mb - estimated_output_mb
        if estimated_saving_mb < 0:
            estimated_saving_mb = 0.0
        estimated_saving_pct = int(estimated_saving_mb / src_mb * 100) if src_mb > 0 else 0

        return {
            "estimated_output_mb":  round(estimated_output_mb, 1),
            "estimated_saving_mb":  round(estimated_saving_mb, 1),
            "estimated_saving_pct": estimated_saving_pct,
            "sample_count": len(sample_ratios),
            "sample_cv_pct": round(sample_cv_pct, 1),
            "aggregation": aggregation,
            "high_variance": high_variance,
            "error": None,
        }

    except subprocess.TimeoutExpired:
        return {"error": "Timed out"}
    except Exception as e:
        return {"error": str(e)}
    finally:
        for idx in range(1, _SAMPLE_COUNT + 1):
            for prefix in ("_est_src_", "_est_enc_", "_est_"):
                tmp_path = os.path.join(
                    config.LOCAL_TEMP_DIR,
                    f"{prefix}{Path(input_path).stem}_{idx}.mkv",
                )
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass


# ---------------------------------------------------------------------------
# Output integrity check
# ---------------------------------------------------------------------------

def _verify_tracks_preserved(
    src_path: str,
    out_path: str,
    check_subs: bool = True,
    dropped_streams: list[int] | None = None,
) -> tuple[bool, str]:
    """
    Probe source and output with ffprobe and verify that no audio or subtitle
    tracks were silently dropped.

    Rules:
      - Output must have at least as many audio streams as the source, minus
        any intentionally dropped audio stream indices.
      - If check_subs is True and the source has any subtitle streams, the
        output must have at least one.

    check_subs should be False for MP4 (anime mode) outputs where subtitle
    tracks are legitimately transformed or excluded during remux (e.g. bitmap
    subs that cannot be OCR'd, or non-English text subs filtered by language).

    Returns (True, "") on pass, (False, reason) on fail.
    """
    def _probe_streams(path: str) -> list:
        for attempt in range(3):
            r = subprocess.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", path],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            )
            if r.stdout.strip():
                return json.loads(r.stdout).get("streams", [])
            if attempt < 2:
                time.sleep(0.5)
        raise ValueError(f"ffprobe returned empty output for: {path}")

    try:
        src_streams = _probe_streams(src_path)
        out_streams = _probe_streams(out_path)
    except Exception as exc:
        return False, f"ffprobe error during track verification: {exc}"

    _dropped = set(dropped_streams or [])
    src_audio_streams = [s for s in src_streams if s.get("codec_type") == "audio"]
    # Subtract intentionally-dropped audio tracks from the expected count
    dropped_audio = sum(1 for s in src_audio_streams if s.get("index") in _dropped)
    src_audio = len(src_audio_streams) - dropped_audio
    out_audio = sum(1 for s in out_streams if s.get("codec_type") == "audio")
    src_subs  = sum(1 for s in src_streams if s.get("codec_type") == "subtitle")
    out_subs  = sum(1 for s in out_streams if s.get("codec_type") == "subtitle")

    if out_audio < src_audio:
        return False, (
            f"audio tracks dropped: source had {src_audio}, output has {out_audio}"
        )
    if check_subs and src_subs > 0 and out_subs == 0:
        return False, (
            f"subtitles lost: source had {src_subs} subtitle track(s), output has none"
        )
    return True, ""


def _parse_duration_seconds(value: str | float | int | None) -> float:
    """Parse ffprobe duration values or HH:MM:SS.mmm strings into seconds."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        pass
    if ":" not in text:
        return 0.0
    try:
        hh, mm, ss = text.split(":")
        return int(hh) * 3600 + int(mm) * 60 + float(ss)
    except (ValueError, TypeError):
        return 0.0


def _effective_av_duration_from_probe(data: dict) -> float:
    """Return max video/audio stream duration, falling back to container duration."""
    streams = data.get("streams", []) or []
    av_durations: list[float] = []
    for s in streams:
        if s.get("codec_type") not in ("video", "audio"):
            continue
        d = _parse_duration_seconds(s.get("duration"))
        if d <= 0:
            tags = s.get("tags") or {}
            d = _parse_duration_seconds(tags.get("DURATION") or tags.get("DURATION-eng"))
        if d > 0:
            av_durations.append(d)

    if av_durations:
        return max(av_durations)

    return _parse_duration_seconds((data.get("format") or {}).get("duration"))


def _probe_duration_for_compare(path: str) -> float:
    """Probe a file and return its effective A/V duration for integrity checks."""
    for attempt in range(3):
        try:
            result = subprocess.run(
                [
                    "ffprobe", "-v", "quiet",
                    "-print_format", "json",
                    "-show_streams",
                    "-show_format",
                    path,
                ],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            )
            if not result.stdout.strip():
                raise ValueError(f"ffprobe returned empty output (rc={result.returncode})")
            data = json.loads(result.stdout)
            return _effective_av_duration_from_probe(data)
        except Exception:
            if attempt < 2:
                time.sleep(0.5)
    return 0.0


def _verify_output(
    output_path: str,
    src_duration: float,
    src_path: str | None = None,
    log: Callable[[str], None] | None = None,
) -> tuple[bool, str]:
    """
    Sanity-check the encoded output via ffprobe.

        Checks:
      1. File exists and size > 0
      2. At least one video stream present
            3. Effective A/V duration within 5% of source duration (subtitle-only tails ignored)

    Returns (True, "") on pass, (False, reason) on fail.
    """
    if not os.path.exists(output_path):
        return False, "file not found"
    if os.path.getsize(output_path) == 0:
        return False, "empty file"

    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            result = subprocess.run(
                [
                    "ffprobe", "-v", "quiet",
                    "-print_format", "json",
                    "-show_streams",
                    "-show_format",
                    output_path,
                ],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            )
            if not result.stdout.strip():
                raise ValueError(f"ffprobe returned empty output (rc={result.returncode})")
            data = json.loads(result.stdout)
            break
        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(0.5)
    else:
        return False, f"ffprobe error: {last_exc}"

    streams = data.get("streams", [])
    has_video = any(s.get("codec_type") == "video" for s in streams)
    if not has_video:
        return False, "no video stream"

    src_compare = src_duration
    if src_path:
        probed_src = _probe_duration_for_compare(src_path)
        if probed_src > 0:
            src_compare = probed_src

    out_duration = _effective_av_duration_from_probe(data)
    if src_compare > 0 and out_duration > 0:
        diff_pct = abs(out_duration - src_compare) / src_compare
        if log is not None:
            log(
                "Integrity duration check (A/V): "
                f"src={src_compare:.1f}s out={out_duration:.1f}s "
                f"diff={diff_pct*100:.2f}% threshold=5.00%"
            )
        if diff_pct > 0.05:
            return False, (
                f"duration mismatch: src={src_compare:.1f}s "
                f"out={out_duration:.1f}s ({diff_pct*100:.1f}% off)"
            )
    elif log is not None:
        log(
            "Integrity duration check (A/V): duration unavailable "
            f"src={src_compare:.1f}s out={out_duration:.1f}s"
        )

    return True, ""


# ---------------------------------------------------------------------------
# OCR one-shot helper
# ---------------------------------------------------------------------------

def _ocr_oneshot(input_path: str, out_dir: str, log: LogFn) -> 'list[str] | None':
    """
    Run OCR as an isolated one-shot subprocess.  Slower than the persistent
    worker (Tesseract subprocess per frame) but completely immune to
    any state corruption left by a previous native crash.

    Returns list of generated SRT paths, or None on any failure.
    """
    result_json = os.path.join(out_dir, f"_ocr_result_{os.getpid()}.json")
    bsubs_script = os.path.join(os.path.dirname(__file__), "bitmap_subs.py")
    cmd = [sys.executable, bsubs_script, input_path, out_dir, "en", result_json]
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,   # 15-min ceiling — generously covers 600+ frames
        )
        if r.returncode != 0:
            log(f"WARNING: OCR one-shot exited {r.returncode}")
            for _line in (r.stderr or "").splitlines()[-8:]:
                if _line.strip():
                    log(f"  [ocr] {_line.rstrip()}")
            return None
        if os.path.exists(result_json):
            with open(result_json, encoding="utf-8") as _f:
                return json.load(_f)
        return []
    except subprocess.TimeoutExpired:
        log("WARNING: OCR one-shot timed out after 15 minutes")
        return None
    except Exception as _exc:
        log(f"WARNING: OCR one-shot error: {_exc}")
        return None
    finally:
        try:
            os.remove(result_json)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Anime mode helpers
# ---------------------------------------------------------------------------

def _aac_encoder() -> str:
    """Return the correct AAC encoder for this platform.

    Native 'aac' is used on all platforms.  On Windows it must be run via
    _run_with_pcores_only() to avoid the FP-overflow crash on Intel E-cores.
    We no longer use 'aac_mf' (Windows Media Foundation) because it shares
    the same MFT subsystem as QSV and hangs silently on this hardware.
    """
    return "aac"


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
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
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


def _is_av1(input_path: str) -> bool:
    """Return True when the first video stream is AV1.

    AV1 files are not re-encoded; the video is stream-copied into the
    MP4 container while audio and subtitles go through the normal path.
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
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
        )
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        if not streams:
            return False
        return streams[0].get("codec_name", "").lower() in ("av1", "av1_cuvid")
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
    conv_logger: "_conv_log.ConversionLogger | None" = None,
    tmp_holder: list[str] | None = None,
    dropped_streams: list[int] | None = None,
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
    source_fps = _ffprobe_source_fps(input_path)

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
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        probe_data = json.loads(probe.stdout)
        streams = probe_data.get("streams", [])
    except Exception as exc:
        log(f"ERROR: ffprobe failed: {exc}")
        return False, ""

    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    sub_streams   = [s for s in streams if s.get("codec_type") == "subtitle"]

    # ------------------------------------------------------------------
    # Detect external subtitle files alongside the source.
    #
    # Two matching rules (case-insensitive, sorted alphabetically so
    # track order is deterministic):
    #   Exact  — stem == video_stem          e.g. Title.srt / Title.ass
    #   Prefix — stem == video_stem.<suffix> e.g. Title.en.srt
    #                                             Title.pgs1.srt (ocr_subs.py output)
    #                                             Title.signs.ass
    # The dot separator prevents false positives from unrelated files
    # whose names merely start with the same word.
    # ------------------------------------------------------------------
    _EXT_SUB_EXTS = {".srt", ".ass", ".ssa"}
    ext_sub_paths: list[str] = []
    _src_stem_lower = Path(input_path).stem.lower()
    for _p in sorted(Path(input_path).parent.iterdir()):
        if _p.suffix.lower() not in _EXT_SUB_EXTS:
            continue
        _ps = _p.stem.lower()
        if _ps == _src_stem_lower or _ps.startswith(_src_stem_lower + "."):
            ext_sub_paths.append(str(_p))
            log(f"Found external subtitle: {_p.name}")
    _ocr_ext_sub_paths = [
        p for p in ext_sub_paths
        if re.search(r"\.pgs\d+\.srt$", p, re.IGNORECASE)
    ]

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
            if ext_sub_paths:
                # Sub-inject path: copy all streams + mux in sidecar subs (no re-encode)
                log(f"MP4 sub-inject: copying streams and merging {len(ext_sub_paths)} sidecar subtitle(s)...")
                _si_tmp = os.path.join(
                    config.LOCAL_TEMP_DIR, f"_subinject_{os.getpid()}.mp4"
                )
                os.makedirs(config.LOCAL_TEMP_DIR, exist_ok=True)
                if tmp_holder is not None:
                    tmp_holder[0] = _si_tmp

                # Pre-process SRT files with pysubs2 to fix overlapping /
                # non-monotonic timestamps that cause mov_text muxing to fail.
                # pysubs2 is already an OCR dependency so it is always available.
                _processed_sub_paths: list[str] = []
                _sub_tmp_files: list[str] = []
                for _srt_p in ext_sub_paths:
                    if not _srt_p.lower().endswith(".srt"):
                        _processed_sub_paths.append(_srt_p)
                        continue
                    try:
                        import pysubs2 as _ps2
                        _subs = _ps2.load(_srt_p, encoding="utf-8-sig")
                        _subs.sort()
                        # Clamp each entry's end time to the next entry's start
                        # so no two events overlap (mov_text requirement).
                        for _ei in range(len(_subs) - 1):
                            if _subs[_ei].end > _subs[_ei + 1].start:
                                _subs[_ei].end = _subs[_ei + 1].start
                        _tmp_srt = os.path.join(
                            config.LOCAL_TEMP_DIR,
                            f"_subfix_{os.getpid()}_{len(_sub_tmp_files)}.srt",
                        )
                        _subs.save(_tmp_srt, format_="srt", encoding="utf-8")
                        _processed_sub_paths.append(_tmp_srt)
                        _sub_tmp_files.append(_tmp_srt)
                        log(f"Pre-processed {Path(_srt_p).name}: fixed overlapping timestamps")
                    except Exception as _pe:
                        log(f"WARNING: could not pre-process {Path(_srt_p).name}: {_pe} — using original")
                        _processed_sub_paths.append(_srt_p)

                _si_cmd = ["ffmpeg", "-y", "-i", input_path]
                for sp in _processed_sub_paths:
                    _si_cmd += ["-i", sp]
                _si_cmd += ["-map", "0"]
                _existing_sub_count = sum(
                    1 for s in sub_streams
                )
                for j in range(len(_processed_sub_paths)):
                    _si_cmd += ["-map", f"{j+1}:s:0"]
                _si_cmd += ["-c", "copy", "-c:s", "mov_text"]
                for j in range(len(_processed_sub_paths)):
                    _si_cmd += [f"-metadata:s:s:{_existing_sub_count + j}", "language=eng"]
                _si_cmd += ["-max_interleave_delta", "0", "-movflags", "+faststart", _si_tmp]
                log(f"Running: {' '.join(_si_cmd)}")
                try:
                    _si_proc = subprocess.run(
                        _si_cmd, capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=300,
                    )
                except FileNotFoundError:
                    log("ERROR: ffmpeg not found.")
                    for _tf in _sub_tmp_files:
                        try: os.remove(_tf)
                        except OSError: pass
                    return False, ""
                for _tf in _sub_tmp_files:
                    try: os.remove(_tf)
                    except OSError: pass
                if conv_logger:
                    conv_logger.write_phase("sub_inject", _si_proc.stdout + _si_proc.stderr)
                if _si_proc.returncode != 0 or not os.path.exists(_si_tmp):
                    log("Sub-inject failed:")
                    for _l in (_si_proc.stdout + _si_proc.stderr).splitlines()[-20:]:
                        if _l.strip():
                            log(_l)
                    if os.path.exists(_si_tmp):
                        os.remove(_si_tmp)
                    return False, ""
                os.makedirs(output_dir, exist_ok=True)
                _si_final = os.path.join(output_dir, Path(input_path).stem + ".mp4")
                import shutil as _shutil
                try:
                    os.replace(_si_tmp, _si_final)
                except OSError as _e:
                    if _e.winerror == 17:
                        _shutil.move(_si_tmp, _si_final)
                    else:
                        if os.path.exists(_si_tmp):
                            os.remove(_si_tmp)
                        raise
                # Verify subtitle streams are actually present in the output
                try:
                    _verify_proc = subprocess.run(
                        ["ffprobe", "-v", "quiet", "-print_format", "json",
                         "-show_streams", _si_final],
                        capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=30,
                    )
                    _verify_streams = json.loads(_verify_proc.stdout).get("streams", [])
                    _out_sub_count = sum(
                        1 for s in _verify_streams if s.get("codec_type") == "subtitle"
                    )
                    _expected = _existing_sub_count + len(ext_sub_paths)
                except Exception as _ve:
                    log(f"WARNING: could not verify subtitle streams: {_ve}")
                    _out_sub_count = -1
                    _expected = -1

                if _out_sub_count != -1 and _out_sub_count < _expected:
                    log(
                        f"WARNING: expected {_expected} subtitle stream(s) in output "
                        f"but found {_out_sub_count} — sidecar files NOT deleted."
                    )
                else:
                    log(f"Verified {_out_sub_count} subtitle stream(s) in output.")
                    for _sp in ext_sub_paths:
                        try:
                            os.remove(_sp)
                            log(f"Removed sidecar: {Path(_sp).name}")
                        except OSError as _re:
                            log(f"WARNING: could not remove sidecar {Path(_sp).name}: {_re}")
                log(f"Done. Subs merged → {_si_final}")
                return True, "copy"
            log("MP4 fast-path: already MP4 + AAC, no bitmap subs — compressing directly.")
            return compress_simple(
                input_path, output_dir, log, stop_event,
                quality=quality, progress_cb=progress_cb, pid_holder=pid_holder,
                tmp_holder=tmp_holder,
            )

    # ------------------------------------------------------------------
    # Subtitle classification
    # ------------------------------------------------------------------
    TEXT_SUB_CODECS    = {"ass", "subrip", "srt", "webvtt", "mov_text"}
    PGS_SUB_CODECS    = {"hdmv_pgs_subtitle", "pgssub"}   # can be OCR'd to SRT
    COPY_BITMAP_CODECS = {"dvd_subtitle", "vobsub"}        # copy as-is (bin_data in MP4)
    BITMAP_SUB_CODECS = PGS_SUB_CODECS | COPY_BITMAP_CODECS

    english_text_subs   = []   # stream indices for text subs (-> mov_text)
    pgs_sub_indices     = []   # stream indices for PGS subs (-> OCR -> SRT)
    copy_bitmap_indices = []   # stream indices for dvd_subtitle/vobsub (-> copy)

    _dropped = set(dropped_streams or [])

    for s in sub_streams:
        codec = s.get("codec_name", "").lower()
        tags  = s.get("tags", {})
        lang  = tags.get("language", "")
        title = tags.get("title", "")
        eng   = _is_potentially_english(lang, title)
        sidx  = s.get("index", -1)
        if sidx in _dropped:
            log(f"Skipping dropped subtitle stream #{sidx} ({codec}, {lang})")
            continue
        if codec in TEXT_SUB_CODECS:
            # Keep all text subtitle tracks regardless of language — they are
            # tiny and mislabeled language tags (common in anime rips) should
            # not cause dialogue subs to be silently dropped.
            english_text_subs.append(s["index"])
        elif codec in PGS_SUB_CODECS:
            if eng:
                pgs_sub_indices.append(s["index"])
        elif codec in COPY_BITMAP_CODECS:
            if eng:
                copy_bitmap_indices.append(s["index"])

    # If no English subs kept at all but there's exactly one sub track, keep it
    # (sole-sub rule: likely English even if not tagged) — unless it's dropped.
    if not english_text_subs and not pgs_sub_indices and not copy_bitmap_indices and len(sub_streams) == 1:
        s = sub_streams[0]
        codec = s.get("codec_name", "").lower()
        if s.get("index", -1) not in _dropped:
            if codec in TEXT_SUB_CODECS:
                english_text_subs.append(s["index"])
            elif codec in PGS_SUB_CODECS:
                pgs_sub_indices.append(s["index"])
        elif codec in COPY_BITMAP_CODECS:
            copy_bitmap_indices.append(s["index"])

    # ------------------------------------------------------------------
    # OCR bitmap subs
    # Skip if pre-OCR'd PGS sidecar files (.pgsN.srt) already exist —
    # produced by the standalone ocr_subs.py tool and already included
    # in ext_sub_paths above.
    # Run in a subprocess so any native crash in Tesseract cannot
    # take down the Flask server process.
    # ------------------------------------------------------------------
    _pgs_sidecars = [
        p for p in ext_sub_paths
        if re.search(r"\.pgs\d+\.srt$", p, re.IGNORECASE)
    ]
    if _pgs_sidecars and pgs_sub_indices:
        log(
            f"Found {len(_pgs_sidecars)} pre-OCR\'d PGS sidecar(s) "
            f"({', '.join(Path(p).name for p in _pgs_sidecars)}) "
            f"\u2014 skipping OCR step."
        )
        pgs_sub_indices = []
    srt_paths: list[str] = []
    if pgs_sub_indices:
        import bitmap_subs as _bsubs
        if not _bsubs.DEPS_OK:
            log(
                "ERROR: bitmap subtitle track(s) found but OCR dependencies are not installed. "
                "Run: pip install pytesseract Pillow pysubs2 + winget install UB-Mannheim.TesseractOCR"
            )
            return False, ""
        log(f"OCR bitmap subs ({len(pgs_sub_indices)} track(s))...")
        os.makedirs(config.LOCAL_TEMP_DIR, exist_ok=True)
        _ocr_job = json.dumps({"input_path": input_path, "out_dir": config.LOCAL_TEMP_DIR, "lang": "en"})
        _ocr_attempts = 2
        _ocr_success = False
        for _ocr_try in range(_ocr_attempts):
            try:
                _worker = _get_ocr_worker()
                _worker.stdin.write(_ocr_job + "\n")
                _worker.stdin.flush()
                _result_line = _worker.stdout.readline()
                if not _result_line:
                    log(f"WARNING: OCR worker closed unexpectedly (attempt {_ocr_try+1}/{_ocr_attempts}) — restarting worker")
                    for _el in _ocr_stderr_lines[-10:]:
                        if _el.strip():
                            log(f"  [ocr stderr] {_el}")
                    continue
                _result = json.loads(_result_line.strip())
                if not _result.get("ok"):
                    log(f"WARNING: OCR worker reported failure: {_result.get('error')} (attempt {_ocr_try+1}/{_ocr_attempts})")
                    continue
                srt_paths = _result["paths"]
                _ocr_success = True
                break
            except Exception as exc:
                log(f"WARNING: OCR worker failed: {exc} (attempt {_ocr_try+1}/{_ocr_attempts})")
                for _el in _ocr_stderr_lines[-10:]:
                    if _el.strip():
                        log(f"  [ocr stderr] {_el}")
                # Kill the worker so _get_ocr_worker() spawns a fresh one
                try:
                    _worker.kill()
                    _worker.wait()
                except Exception:
                    pass
        if not _ocr_success:
            # Persistent worker crashed (likely a native PyTorch fault).
            # Retry once with a completely fresh subprocess — no shared state.
            log("Persistent OCR worker failed — retrying with isolated one-shot subprocess...")
            _oneshot_result = _ocr_oneshot(input_path, config.LOCAL_TEMP_DIR, log)
            if _oneshot_result is not None:
                srt_paths = _oneshot_result
                _ocr_success = True
            else:
                log("WARNING: OCR failed on all attempts — continuing without PGS subtitles (text subs preserved)")
                srt_paths = []
    if copy_bitmap_indices:
        log(f"Copying {len(copy_bitmap_indices)} dvd_subtitle/vobsub track(s) directly into MP4...")

    # ------------------------------------------------------------------
    # Build output path
    # ------------------------------------------------------------------
    out_name   = Path(input_path).stem + ".mp4"
    final_path = os.path.join(output_dir, out_name)
    os.makedirs(config.LOCAL_TEMP_DIR, exist_ok=True)
    tmp_path   = os.path.join(config.LOCAL_TEMP_DIR, out_name)

    aac = _aac_encoder()
    _pre_audio: list[str | None] | None = None
    _extracted_text_srts: list[str] | None = None
    _remux_ok = False

    def _preencode_audio_to_aac() -> list[str | None] | None:
        """
        Pre-encode each non-AAC audio stream to an individual .m4a file using
        aac_mf, one track at a time.  Returns a list parallel to audio_streams
        where None means the stream is already AAC (copy from source), or a path
        to the pre-encoded temp file.  Returns None if any encoding fails.

        Encoding tracks one-by-one avoids the aac_mf multi-stream crash while
        keeping aac_mf (not native aac) to side-step the E-core FP overflow.

        Note: aac_mf may exit non-zero on DTS/PTS warnings even when the output
        is fully written and playable.  We therefore verify the output with
        ffprobe rather than trusting the exit code.
        """
        def _is_valid_audio(path: str) -> bool:
            """Return True if path is a readable audio file with duration > 0."""
            if not os.path.exists(path) or os.path.getsize(path) < 1024:
                return False
            probe = subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default", path,
                ],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            )
            return probe.returncode == 0 and "duration=" in probe.stdout

        files: list[str | None] = []
        for i, a in enumerate(audio_streams):
            if stop_event.is_set():
                for f in files:
                    if f and os.path.exists(f):
                        try:
                            os.remove(f)
                        except OSError:
                            pass
                return None
            src_ac = a.get("codec_name", "").lower()
            if src_ac in ("aac", "aac_latm"):
                files.append(None)   # already AAC — will copy from source
                continue
            ai = a.get("index", 0)
            tmp_audio = os.path.join(
                config.LOCAL_TEMP_DIR,
                f"_audio_{os.getpid()}_{i}.m4a",
            )
            # Always include DTS-fix flags: aac_mf can exit non-zero on
            # non-monotonic DTS without them and leave an invalid container.
            cmd = [
                "ffmpeg", "-y",
                "-fflags", "+genpts",
                "-avoid_negative_ts", "make_zero",
                "-i", input_path,
                "-map", f"0:{ai}",
                "-c:a", "aac",
                "-vn", "-sn",
                tmp_audio,
            ]
            log(f"Pre-encoding audio track {i+1}/{len(audio_streams)} to AAC...")
            # Use P-core pinning to avoid native aac FP-overflow on Intel E-cores.
            r = _run_with_pcores_only(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", timeout=600,
            )
            # Write raw stderr to its own step log regardless of outcome
            if conv_logger:
                conv_logger.write_step(
                    f"audio_track_{i+1}_aac", r.stderr or ""
                )
            # Verify with ffprobe — do not rely solely on exit code.
            if not _is_valid_audio(tmp_audio):
                log(f"AAC pre-encode failed for track {i+1} (rc={r.returncode})")
                err_lines = (r.stderr or "").strip().splitlines()
                for el in err_lines[-5:]:
                    if el.strip():
                        log(f"  [aac stderr] {el.rstrip()}")
                if os.path.exists(tmp_audio):
                    try:
                        os.remove(tmp_audio)
                    except OSError:
                        pass
                log(f"Audio pre-encode failed for track {i+1} — will stream-copy instead")
                if conv_logger:
                    conv_logger.mark_fail_at(
                        f"audio track {i+1} pre-encode (aac rc={r.returncode})"
                    )
                # Fall back to stream-copying this track rather than aborting
                # the whole attempt.  The DTS issue is in the subtitle tracks,
                # not the audio, so stream-copying a failed audio track is safe.
                files.append(None)
                continue
            files.append(tmp_audio)
        return files

    def _extract_text_subs_to_srt() -> list[str] | None:
        """
        Pre-extract ASS/text sub tracks to temp SRT files so the muxer sees
        simple timestamps instead of complex ASS events that cause DTS overflow.
        After extraction, sanitizes each SRT with pysubs2 to remove any cues
        with timestamps beyond the video duration (corrupt out-of-range entries
        that cause ffmpeg to enter an infinite DTS-correction loop).
        Returns a list of SRT paths (one per track in english_text_subs),
        or None if any extraction fails.
        """
        if not english_text_subs:
            return []
        extracted: list[str] = []
        for i, si in enumerate(english_text_subs):
            out_srt = os.path.join(
                config.LOCAL_TEMP_DIR, f"_textsub_{os.getpid()}_{i}.srt"
            )
            cmd = [
                "ffmpeg", "-y", "-i", input_path,
                "-map", f"0:{si}", "-c:s", "srt", out_srt,
            ]
            log(f"Pre-extracting text sub track {i+1}/{len(english_text_subs)} to SRT...")
            r = subprocess.run(
                cmd, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=120,
            )
            if r.returncode != 0 or not os.path.exists(out_srt) or os.path.getsize(out_srt) == 0:
                log(f"Text sub extraction failed for track {i+1} (rc={r.returncode}) — dropping track")
                # partial cleanup
                for f in extracted:
                    if os.path.exists(f):
                        try:
                            os.remove(f)
                        except OSError:
                            pass
                return None
            # Sanitize: spread any cues that share the same start time apart by
            # 1 ms each.  ASS "typesetter" tracks store animations as many
            # simultaneous events (one cue per character/layer), which all land on
            # the same DTS when converted to SRT.  ffmpeg then enters an infinite
            # DTS-monotonicity correction loop when muxing those cues into
            # mov_text.  Offsetting duplicates by 1 ms each is imperceptible but
            # makes every DTS strictly monotonic.
            try:
                import pysubs2 as _pysubs2
                _subs = _pysubs2.load(out_srt)
                _subs.events.sort(key=lambda _e: (_e.start, _e.end))
                _prev_start = -1
                _offset = 0
                for _e in _subs.events:
                    if _e.start == _prev_start:
                        _offset += 1
                        _e.start += _offset
                        if _e.end <= _e.start:
                            _e.end = _e.start + 1
                    else:
                        _prev_start = _e.start
                        _offset = 0
                _subs.save(out_srt)
                if _offset > 0:
                    log(f"  Spread same-timestamp cues (max offset {_offset} ms) in sub track {i+1}")
            except Exception as _se:
                log(f"  WARNING: SRT de-duplicate failed: {_se} — using as-is")
            extracted.append(out_srt)
        return extracted

    def _build_cmd(
        include_subs: bool,
        extra_flags: list[str] | None = None,
        pre_audio: list[str | None] | None = None,
        extracted_text_srts: list[str] | None = None,
    ) -> tuple[list[str], str]:
        """
        Build the ffmpeg remux command.
        When extracted_text_srts is provided, those pre-extracted SRT files are
        used instead of inline ASS→mov_text conversion from english_text_subs.
        """
        _ext_srts = extracted_text_srts or []
        cmd = [
            "ffmpeg", "-y",
            "-stats_period", "1", "-progress", "pipe:1",
            "-probesize", "100M", "-analyzeduration", "100M",
            "-i", input_path,
        ]

        # Append OCR SRT files as additional inputs (inputs 1..len(srt_paths))
        for srt in srt_paths:
            cmd += ["-i", srt]

        # Append external subtitle files (inputs after OCR SRTs)
        _ext_sub_base = 1 + len(srt_paths)
        for srt in ext_sub_paths:
            cmd += ["-i", srt]

        # Append pre-extracted text sub SRT files (after external subs)
        _pre_ext_srt_base = _ext_sub_base + len(ext_sub_paths)
        for srt in _ext_srts:
            cmd += ["-i", srt]

        # Append pre-encoded audio files as extra inputs
        pre_audio_start_idx = _pre_ext_srt_base + len(_ext_srts)
        if pre_audio:
            for af in pre_audio:
                if af is not None:
                    cmd += ["-i", af]

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

        # Audio — use pre-encoded file (copy) if available, else inline transcode
        pre_idx = pre_audio_start_idx
        for i, a in enumerate(audio_streams):
            ai = a.get("index", 0)
            src_ac = a.get("codec_name", "").lower()
            if pre_audio and pre_audio[i] is not None:
                cmd += ["-map", f"{pre_idx}:a:0", f"-c:a:{i}", "copy"]
                pre_idx += 1
            else:
                audio_codec = "copy" if src_ac in ("aac", "aac_latm") else aac
                cmd += ["-map", f"0:{ai}", f"-c:a:{i}", audio_codec]

        # Subtitles
        if include_subs:
            if _ext_srts:
                # Pre-extracted SRTs replace inline ASS mapping — skip english_text_subs
                for j in range(len(srt_paths)):
                    cmd += ["-map", f"{j+1}:s:0"]
                for j in range(len(ext_sub_paths)):
                    cmd += ["-map", f"{_ext_sub_base + j}:s:0"]
                for j in range(len(_ext_srts)):
                    cmd += ["-map", f"{_pre_ext_srt_base + j}:s:0"]
                # Tag OCR, external, and extracted tracks as English
                for j in range(len(srt_paths)):
                    cmd += [f"-metadata:s:s:{j}", "language=eng"]
                for j in range(len(ext_sub_paths)):
                    cmd += [f"-metadata:s:s:{len(srt_paths) + j}", "language=eng"]
                for j in range(len(_ext_srts)):
                    cmd += [f"-metadata:s:s:{len(srt_paths) + len(ext_sub_paths) + j}", "language=eng"]
                sub_count = len(srt_paths) + len(ext_sub_paths) + len(_ext_srts)
            else:
                # Text subs from source (-> mov_text)
                for si in english_text_subs:
                    cmd += ["-map", f"0:{si}"]
                # OCR'd SRT files (inputs 1..N) (-> mov_text)
                for j in range(len(srt_paths)):
                    cmd += ["-map", f"{j+1}:s:0"]
                # External subtitle files
                for j in range(len(ext_sub_paths)):
                    cmd += ["-map", f"{_ext_sub_base + j}:s:0"]
                # Tag OCR'd and external tracks with English language
                text_sub_offset = len(english_text_subs)
                for j in range(len(srt_paths)):
                    cmd += [f"-metadata:s:s:{text_sub_offset + j}", "language=eng"]
                for j in range(len(ext_sub_paths)):
                    cmd += [f"-metadata:s:s:{text_sub_offset + len(srt_paths) + j}", "language=eng"]
                sub_count = len(english_text_subs) + len(srt_paths) + len(ext_sub_paths)

            if sub_count > 0:
                cmd += ["-c:s", "mov_text"]
            # dvd_subtitle/vobsub tracks — copy directly (become bin_data in MP4)
            for si in copy_bitmap_indices:
                cmd += ["-map", f"0:{si}", f"-c:s:{sub_count}", "copy"]
                sub_count += 1

        if extra_flags:
            cmd += extra_flags

        cmd += ["-movflags", "+faststart", tmp_path]
        return cmd, encoder_tag

    # ------------------------------------------------------------------
    # Run with DTS overflow recovery
    # ------------------------------------------------------------------
    try:
        encoder_used = ""
        _skip_to_srt_extract = False  # set True when DTS loop kills attempt → jump to SRT path

        for attempt, (inc_subs, extra, use_preenc, use_extracted_subs) in enumerate([
            (True,  None,                                                               False, False),  # 0: normal
            (True,  ["-max_interleave_delta", "0"],                                   False, False),  # 1: DTS fix
            (True,  ["-fflags", "+genpts", "-avoid_negative_ts", "make_zero"],       False, False),  # 2: aggressive DTS fix
            (True,  None,                                                               True,  False),  # 3: pre-encode audio
            (True,  None,                                                               True,  True),   # 4: pre-extract text subs → SRT
            (False, None,                                                               True,  False),  # 5: no subs (last resort)
        ]):
            if stop_event.is_set():
                return False, ""

            # If a previous attempt was killed by the DTS warn loop and we have
            # text subs to extract, skip the intermediate DTS-flag attempts and
            # go straight to the SRT pre-extraction path.
            if _skip_to_srt_extract and not use_extracted_subs and inc_subs:
                continue

            # On the first pre-encode attempt, run the pre-encode step
            if use_preenc and _pre_audio is None:
                log("AAC mux failed — pre-encoding audio tracks individually with native aac...")
                _pre_audio = _preencode_audio_to_aac()
                if _pre_audio is None:
                    log("Audio pre-encode failed — giving up.")
                    return False, ""

            # On the first sub-extraction attempt, pre-extract text subs to SRT
            if use_extracted_subs and _extracted_text_srts is None:
                if not english_text_subs:
                    continue  # nothing to extract; skip to no-subs fallback
                log("Subtitle DTS fix — pre-extracting text subs to SRT for cleaner muxing...")
                _extracted_text_srts = _extract_text_subs_to_srt()
                if not _extracted_text_srts:
                    log("Text sub extraction failed — skipping to no-subs fallback.")
                    continue

            cmd, enc = _build_cmd(
                inc_subs, extra,
                pre_audio=_pre_audio if use_preenc else None,
                extracted_text_srts=_extracted_text_srts if use_extracted_subs else None,
            )
            encoder_used = enc
            if attempt == 0:
                log("Remuxing to MP4...")
            elif attempt == 1:
                log("DTS overflow detected — retrying with -max_interleave_delta 0")
            elif attempt == 2:
                log("DTS fix retry — trying -fflags +genpts -avoid_negative_ts make_zero")
            elif attempt == 4:
                log("Retrying with pre-extracted SRT subtitles (avoids ASS→mov_text DTS issues)")
            elif attempt == 5:
                if english_text_subs or pgs_sub_indices or copy_bitmap_indices:
                    log(
                        "WARNING: all subtitle remux attempts failed — retrying without "
                        "subtitle tracks. Output MP4 will have no subtitles."
                    )
                else:
                    log("Retrying without subtitle streams")

            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

            # Capture stderr separately so we can inspect it for DTS errors
            log(f"Running: {' '.join(cmd)}")
            # Wall-clock deadline: stream-copy remux should complete in well
            # under 2× the file's duration; encode passes take longer but
            # still shouldn't exceed 10×.  Attempt 4 does a re-encode of audio
            # but not video, so 4× is generous.  Using 4× with a 120 s floor
            # catches any DTS-loop hang without timing out normal encodes.
            _remux_deadline = time.monotonic() + max(120, duration * 4)
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

                output_lines: list[str] = []
                dts_error = False
                _timed_out = False
                _dts_loop_killed = False
                _dts_warn_count = 0
                _DTS_WARN_LIMIT = 50  # kill after 50 consecutive DTS warnings
                _kv_frame = "0"
                _kv_bitrate = "N/A"
                _kv_fps = 0.0
                _kv_speed = 0.0

                def _emit_kv_status() -> None:
                    fps_txt = f"{_kv_fps:.1f}" if _kv_fps > 0 else "N/A"
                    spd_txt = f"{_kv_speed:.2f}x" if _kv_speed > 0 else "N/A"
                    log(
                        f"__ffstatus__:frame={_kv_frame} fps={fps_txt} "
                        f"bitrate={_kv_bitrate} speed={spd_txt}"
                    )

                def _emit_frame_progress() -> None:
                    if not progress_cb or duration <= 0 or source_fps <= 0:
                        return
                    try:
                        frame_n = float(_kv_frame or 0)
                    except Exception:
                        frame_n = 0.0
                    pct = _frame_progress_pct(frame_n, duration, source_fps)
                    eta_secs = _frame_eta_secs(frame_n, duration, source_fps, _kv_fps)
                    try:
                        progress_cb(pct, _kv_fps, eta_secs)
                    except Exception:
                        pass

                for line in proc.stdout:
                    output_lines.append(line)
                    if stop_event.is_set():
                        proc.kill()
                        proc.wait()
                        log("Stopped by user.")
                        return False, ""
                    # Detect infinite DTS-correction loop early: if ffmpeg is
                    # stuck, it will print "Non-monotonic DTS" thousands of
                    # times per second.  Kill after _DTS_WARN_LIMIT consecutive
                    # occurrences (a progress line resets the counter).
                    ll = line.lower()
                    if "non-monotonic dts" in ll or "non monotonous dts" in ll:
                        _dts_warn_count += 1
                        if _dts_warn_count >= _DTS_WARN_LIMIT:
                            proc.kill()
                            proc.wait()
                            _timed_out = True
                            _dts_loop_killed = True
                            log(
                                f"WARNING: remux attempt {attempt+1} stuck in DTS "
                                f"warning loop (>{_DTS_WARN_LIMIT} warnings) — "
                                f"killing and retrying."
                            )
                            break
                    elif "frame=" in line:
                        _dts_warn_count = 0  # reset on progress line
                    if time.monotonic() > _remux_deadline:
                        proc.kill()
                        proc.wait()
                        _timed_out = True
                        log(
                            f"WARNING: remux attempt {attempt+1} exceeded wall-clock "
                            f"timeout ({int(duration * 10)}s) — likely stuck in DTS "
                            f"warning loop. Killing and retrying."
                        )
                        break
                    # DTS overflow detection
                    if "out of order" in line.lower() or "dts" in line.lower() and "out of order" in line.lower():
                        dts_error = True
                    if progress_cb and duration > 0:
                        _line_s = line.strip()
                        _is_single_kv_line = bool(re.match(r"^[a-z0-9_]+=[^\n\r]*$", _line_s)) and (
                            not bool(re.search(r"\s+[A-Za-z0-9_]+=", _line_s))
                        )
                        if _is_single_kv_line:
                            k, v = _line_s.split("=", 1)
                            if k in _PROGRESS_KV_KEYS:
                                if k == "frame":
                                    _kv_frame = v or _kv_frame
                                    _emit_kv_status()
                                    _emit_frame_progress()
                                elif k == "bitrate":
                                    _kv_bitrate = v or _kv_bitrate
                                    _emit_kv_status()
                                elif k == "fps":
                                    try:
                                        _kv_fps = float(v)
                                        _emit_kv_status()
                                        _emit_frame_progress()
                                    except Exception:
                                        pass
                                elif k == "speed":
                                    try:
                                        _kv_speed = float(v.rstrip("x"))
                                        _emit_kv_status()
                                        _emit_frame_progress()
                                    except Exception:
                                        pass
                                elif k == "progress":
                                    _emit_frame_progress()
                            continue

                        m = _PROGRESS_RE.search(_line_s)
                        if m:
                            log(f"__ffstatus__:{_line_s}")
                            frame_n  = float(m.group("frame") or 0)
                            fps      = float(m.group("fps") or 0)
                            pct      = _frame_progress_pct(frame_n, duration, source_fps)
                            eta_secs = _frame_eta_secs(frame_n, duration, source_fps, fps)
                            try:
                                progress_cb(pct, fps, eta_secs)
                            except Exception:
                                pass
                            continue

                        if source_fps > 0:
                            mf = _PROGRESS_FRAME_ONLY_RE.search(_line_s)
                            if mf:
                                log(f"__ffstatus__:{_line_s}")
                                try:
                                    frame_n = float(mf.group("frame") or 0)
                                    fps_now = float(mf.group("fps") or 0)
                                    pct = _frame_progress_pct(frame_n, duration, source_fps)
                                    eta_secs = _frame_eta_secs(frame_n, duration, source_fps, fps_now)
                                    progress_cb(pct, fps_now, eta_secs)
                                except Exception:
                                    pass

                if not _timed_out:
                    proc.wait()
                if pid_holder is not None:
                    pid_holder[0] = 0

            except FileNotFoundError:
                log("ERROR: ffmpeg not found.")
                return False, ""

            # Always save the full attempt output to its phase log
            if conv_logger:
                conv_logger.write_phase(
                    f"remux_attempt_{attempt+1}", "".join(output_lines)
                )

            if proc.returncode == 0 and os.path.exists(tmp_path) and not _timed_out:
                break  # success

            # Classify the failure: timestamp-related errors trigger retries;
            # unknown/silent failures (last line is a progress line) also retry;
            # aac_mf encoder crashes (Windows MFT) also retry via pre-encode path.
            # A wall-clock timeout is treated as a DTS error to force the next attempt.
            full_output = "".join(output_lines)
            lo = full_output.lower()
            _TS_PATTERNS = ("out of order", "non-monotonic", "non monotonous",
                            "invalid duration", "dts")
            is_ts_error = _timed_out or any(p in lo for p in _TS_PATTERNS)
            # Silent crash: ffmpeg exited non-zero but its last output line was a
            # progress line (frame=...) or stats line — meaning it died mid-encode
            # with no explicit error message.  Search backwards to the actual last
            # non-empty line (do NOT skip frame= lines this time).
            last_line = next((l.rstrip() for l in reversed(output_lines) if l.strip()), "")
            is_silent = (
                last_line == ""
                or "frame=" in last_line
                or last_line.startswith("[out#")
                or last_line.startswith("Lsize=")
            )
            # aac encoder crashes (access violation, MFT hang) also retry via pre-encode path.
            is_aac_mf_fail = any("[aac_mf @" in l or "[aac @" in l for l in output_lines)

            if not is_ts_error and not is_silent and not is_aac_mf_fail:
                if attempt == 0:
                    # True non-recoverable failure — log output for diagnosis and bail
                    for ol in output_lines:
                        ol = ol.rstrip()
                        if ol:
                            log(ol)
                    log("Remux failed.")
                    if conv_logger:
                        conv_logger.mark_fail_at("remux attempt 1 (non-recoverable error)")
                    return False, ""
            # If killed by the DTS warn loop and we have text subs, skip straight
            # to the SRT extraction attempt — intermediate DTS-flag variants won't help.
            if _dts_loop_killed and english_text_subs and not use_extracted_subs:
                log("DTS warn loop detected — skipping to SRT pre-extraction attempt")
                _skip_to_srt_extract = True
            # Timestamp error, silent crash, or aac_mf failure — continue to next attempt

        else:
            log("All remux attempts failed.")
            if conv_logger:
                conv_logger.mark_fail_at("remux (all attempts failed)")
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
            return False, "no_savings"

        os.makedirs(output_dir, exist_ok=True)
        import shutil as _shutil
        try:
            os.replace(tmp_path, final_path)
        except OSError as _e:
            if _e.winerror == 17:
                _shutil.move(tmp_path, final_path)
            else:
                raise
        saved = max(0, src_size - enc_size)
        log(f"Done. Saved {saved/1024/1024:.1f} MB → {final_path}")
        _remux_ok = True
        return True, encoder_used

    finally:
        if _remux_ok:
            for _sp in _ocr_ext_sub_paths:
                if os.path.exists(_sp):
                    try:
                        os.remove(_sp)
                        log(f"Removed OCR sidecar: {Path(_sp).name}")
                    except OSError as _re:
                        log(f"WARNING: could not remove OCR sidecar {Path(_sp).name}: {_re}")
        _preserved = False
        if not _remux_ok and _should_keep_failed_intermediates():
            _keep_paths = [tmp_path]
            _keep_paths.extend(srt_paths)
            if _pre_audio:
                _keep_paths.extend([af for af in _pre_audio if af])
            if _extracted_text_srts:
                _keep_paths.extend([s for s in _extracted_text_srts if s])
            _preserved = bool(_preserve_failed_artifacts(_keep_paths, input_path, log))

        if not _preserved:
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
            # Clean up any pre-encoded audio files
            if _pre_audio:
                for af in _pre_audio:
                    if af and os.path.exists(af):
                        try:
                            os.remove(af)
                        except OSError:
                            pass
            # Clean up any pre-extracted text sub SRT files
            if _extracted_text_srts:
                for srt in _extracted_text_srts:
                    if srt and os.path.exists(srt):
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
    conv_logger: "_conv_log.ConversionLogger | None" = None,
    tmp_holder: list[str] | None = None,
    artifact_holder: list[str] | None = None,
    force_sw: bool = False,
    dropped_streams: list[int] | None = None,
) -> tuple[bool, str]:
    """
    Anime normal-H.264 path: compress with QSV/SW first, then remux the
    resulting MKV to MP4 (AAC audio, mov_text subs).

    Two-pass approach keeps things simple — compress_simple handles the
    codec decision, then remux_to_mp4 handles the container conversion.
    """
    import tempfile

    # Fast-exit: if source is already an MP4 and has sidecar subtitle files,
    # skip compression entirely — just inject the subs via stream-copy.
    # Compressing again would be wasteful and the intermediate temp file
    # wouldn't have the sidecars next to it, so inject would never fire.
    if Path(input_path).suffix.lower() == ".mp4":
        _EXT_SUB_EXTS = {".srt", ".ass", ".ssa"}
        _src = Path(input_path)
        _stem_lower = _src.stem.lower()
        _has_sidecars = any(
            p.suffix.lower() in _EXT_SUB_EXTS and (
                p.stem.lower() == _stem_lower
                or p.stem.lower().startswith(_stem_lower + ".")
            )
            for p in _src.parent.iterdir()
        )
        if _has_sidecars:
            log("MP4 with sidecar subtitles — skipping compression, injecting subs directly.")
            return remux_to_mp4(
                input_path=input_path,
                output_dir=output_dir,
                log=log,
                stop_event=stop_event,
                quality=quality,
                progress_cb=progress_cb,
                pid_holder=pid_holder,
                conv_logger=conv_logger,
                tmp_holder=tmp_holder,
                dropped_streams=dropped_streams,
            )

    # AV1 handling:
    # - legacy behavior: stream-copy AV1 into MP4 (no video re-encode)
    # - default behavior: re-encode AV1 to HEVC at AV1_QSV_QUALITY for
    #   meaningful size savings
    if _is_av1(input_path):
        if not bool(getattr(config, "REENCODE_AV1", True)):
            log("AV1 source — stream-copying video into MP4 (no re-encode).")
            return remux_to_mp4(
                input_path=input_path,
                output_dir=output_dir,
                log=log,
                stop_event=stop_event,
                quality=quality,
                progress_cb=progress_cb,
                pid_holder=pid_holder,
                hi10=True,
                conv_logger=conv_logger,
                tmp_holder=tmp_holder,
                dropped_streams=dropped_streams,
            )

        av1_quality = int(getattr(config, "AV1_QSV_QUALITY", 27))
        av1_quality = max(1, min(51, av1_quality))
        if quality is not None and quality != av1_quality:
            log(f"AV1 policy override: using quality {av1_quality} (instead of {quality}).")
        else:
            log(f"AV1 source — re-encoding for savings at quality {av1_quality}.")
        quality = av1_quality

    # Step 1: compress to a unique temp subdir so the path never collides
    # with compress_simple's own internal tmp_path (which also lives in
    # LOCAL_TEMP_DIR).  Using a separate subdir avoids the finally-block
    # self-delete bug when output_dir == config.LOCAL_TEMP_DIR.
    _local_temp = _temp_dir_for(output_dir)
    os.makedirs(_local_temp, exist_ok=True)
    compress_dir = tempfile.mkdtemp(dir=_local_temp, prefix="_cr_")
    if artifact_holder is not None:
        artifact_holder[0] = compress_dir

    # Phase 1 reports 0→92%; phase 2 (stream-copy remux) reports 92→100%.
    # This prevents the progress bar from sitting frozen at ~100% while the
    # container conversion runs silently.
    _COMPRESS_SHARE = 0.92

    def _compress_progress(pct: float, fps: float, eta: int) -> None:
        if progress_cb is not None:
            progress_cb(pct * _COMPRESS_SHARE, fps, eta)

    def _remux_progress(pct: float, fps: float, eta: int) -> None:
        if progress_cb is not None:
            progress_cb(_COMPRESS_SHARE * 100 + pct * (1.0 - _COMPRESS_SHARE), fps, eta)

    ok, encoder_used = compress_simple(
        input_path=input_path,
        output_dir=compress_dir,
        log=log,
        stop_event=stop_event,
        quality=quality,
        progress_cb=_compress_progress if progress_cb is not None else None,
        pid_holder=pid_holder,
        conv_logger=conv_logger,
        tmp_holder=tmp_holder,
        force_sw=force_sw,
        dropped_streams=dropped_streams,
    )

    if not ok:
        if _should_keep_failed_intermediates():
            _preserve_failed_artifacts([compress_dir], input_path, log)
        else:
            # Clean up the temp dir
            try:
                shutil.rmtree(compress_dir, ignore_errors=True)
            except Exception:
                pass
        if artifact_holder is not None:
            artifact_holder[0] = ""
        return False, encoder_used

    suffix   = Path(input_path).suffix.lower()
    # .m4v uses the ipod muxer which doesn't support HEVC — treat as .mp4
    out_ext  = ".mp4" if suffix in (".mp4", ".m4v") else (".mkv" if suffix == ".mkv" else ".mkv")
    intermediate = os.path.join(compress_dir, Path(input_path).stem + out_ext)

    if not os.path.exists(intermediate):
        log("ERROR: intermediate file missing after compress_simple")
        if _should_keep_failed_intermediates():
            _preserve_failed_artifacts([compress_dir], input_path, log)
        else:
            shutil.rmtree(compress_dir, ignore_errors=True)
        if artifact_holder is not None:
            artifact_holder[0] = ""
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
        progress_cb=_remux_progress if progress_cb is not None else None,
        pid_holder=pid_holder,
        hi10=True,
        conv_logger=conv_logger,
        tmp_holder=tmp_holder,
        dropped_streams=dropped_streams,
    )

    # Keep the intermediate workspace alive until the caller finishes the
    # final integrity check. False positives happen after remux succeeds.
    if ok2:
        if artifact_holder is not None:
            artifact_holder[0] = compress_dir
        else:
            try:
                shutil.rmtree(compress_dir, ignore_errors=True)
            except Exception:
                pass
    elif _should_keep_failed_intermediates():
        _preserve_failed_artifacts([compress_dir], input_path, log)
        if artifact_holder is not None:
            artifact_holder[0] = ""
    else:
        try:
            shutil.rmtree(compress_dir, ignore_errors=True)
        except Exception:
            pass
        if artifact_holder is not None:
            artifact_holder[0] = ""

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
    tmp_holder: list[str] | None = None,
    force_sw: bool = False,
    dropped_streams: list[int] | None = None,
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

    clog = _conv_log.ConversionLogger(input_path)

    if anime_mode:
        _artifact_holder: list[str] = [""]
        _anime_force_sw = force_sw
        if is_hi10(input_path):
            log("Hi10 H.264 detected — QSV unsupported, will use libx265 software encoder.")
            _anime_force_sw = True
        log("Anime mode: compressing then remuxing to MP4.")
        ok, encoder_used = compress_and_remux(
            input_path=input_path,
            output_dir=output_dir,
            log=log,
            stop_event=stop_event,
            quality=quality,
            progress_cb=progress_cb,
            pid_holder=pid_holder,
            conv_logger=clog,
            tmp_holder=tmp_holder,
            artifact_holder=_artifact_holder,
            force_sw=_anime_force_sw,
            dropped_streams=dropped_streams,
        )

        if not ok:
            clog.failure()
            return {
                "ok":             False,
                "output_path":    None,
                "output_size_mb": 0.0,
                "saved_mb":       0.0,
                "saved_pct":      0,
                "encoder_used":   encoder_used,
                "error":          "no_savings" if encoder_used == "no_savings" else "anime encode failed",
                "conv_logger":    clog,
            }

        out_name     = Path(input_path).stem + ".mp4"
        output_path  = os.path.join(output_dir, out_name)
        ok_verify, reason = _verify_output(output_path, duration, src_path=input_path, log=log)
        if not ok_verify:
            log(f"Integrity check failed: {reason}")
            clog.mark_fail_at(f"integrity check: {reason}")
            if _should_keep_failed_intermediates():
                _keep_paths = [output_path]
                if _artifact_holder[0]:
                    _keep_paths.append(_artifact_holder[0])
                _preserve_failed_artifacts(_keep_paths, input_path, log)
            elif _artifact_holder[0] and os.path.exists(_artifact_holder[0]):
                shutil.rmtree(_artifact_holder[0], ignore_errors=True)
            clog.failure()
            return {
                "ok":             False,
                "output_path":    output_path,
                "output_size_mb": 0.0,
                "saved_mb":       0.0,
                "saved_pct":      0,
                "encoder_used":   encoder_used,
                "error":          f"integrity: {reason}",
                "conv_logger":    clog,
            }
        log("Integrity check passed.")
        if _artifact_holder[0] and os.path.exists(_artifact_holder[0]):
            shutil.rmtree(_artifact_holder[0], ignore_errors=True)

        out_size  = os.path.getsize(output_path)
        out_mb    = out_size / (1024 * 1024)
        saved_mb  = max(0.0, src_mb - out_mb)
        saved_pct = int(saved_mb / src_mb * 100) if src_mb > 0 else 0
        clog.success(encoder_used, saved_pct)
        return {
            "ok":             True,
            "output_path":    output_path,
            "output_size_mb": round(out_mb, 2),
            "saved_mb":       round(saved_mb, 2),
            "saved_pct":      saved_pct,
            "encoder_used":   encoder_used,
            "error":          None,
            "conv_logger":    clog,
            "duration_secs":  duration,
        }

    # Normal mode
    if not force_sw and is_hi10(input_path):
        log("Hi10 H.264 detected — QSV unsupported, forcing libx265 software encoder.")
        force_sw = True

    ok, encoder_used = compress_simple(
        input_path  = input_path,
        output_dir  = output_dir,
        log         = log,
        stop_event  = stop_event,
        quality     = quality,
        progress_cb = progress_cb,
        pid_holder  = pid_holder,
        conv_logger = clog,
        tmp_holder  = tmp_holder,
        force_sw    = force_sw,
        dropped_streams = dropped_streams,
    )

    if not ok:
        clog.failure()
        return {
            "ok":           False,
            "output_path":  None,
            "output_size_mb": 0.0,
            "saved_mb":     0.0,
            "saved_pct":    0,
            "encoder_used": encoder_used,
            "error":        "no_savings" if encoder_used == "no_savings" else "encode failed",
            "conv_logger":  clog,
        }

    # Locate output file
    suffix   = Path(input_path).suffix.lower()
    # .m4v uses the ipod muxer which doesn't support HEVC — treat as .mp4
    out_ext  = ".mp4" if suffix in (".mp4", ".m4v") else (".mkv" if suffix == ".mkv" else ".mkv")
    out_name = Path(input_path).stem + out_ext
    output_path = os.path.join(output_dir, out_name)

    # Integrity check
    ok_verify, reason = _verify_output(output_path, duration, src_path=input_path, log=log)
    if not ok_verify:
        log(f"Integrity check failed: {reason}")
        clog.mark_fail_at(f"integrity check: {reason}")
        clog.failure()
        return {
            "ok":           False,
            "output_path":  output_path,
            "output_size_mb": 0.0,
            "saved_mb":     0.0,
            "saved_pct":    0,
            "encoder_used": encoder_used,
            "error":        f"integrity: {reason}",
            "conv_logger":  clog,
        }
    log("Integrity check passed.")

    out_size   = os.path.getsize(output_path)
    out_mb     = out_size / (1024 * 1024)
    saved_mb   = src_mb - out_mb
    saved_pct  = int(saved_mb / src_mb * 100) if src_mb > 0 else 0
    clog.success(encoder_used, saved_pct)
    return {
        "ok":             True,
        "output_path":    output_path,
        "output_size_mb": round(out_mb, 2),
        "saved_mb":       round(saved_mb, 2),
        "saved_pct":      saved_pct,
        "encoder_used":   encoder_used,
        "error":          None,
        "conv_logger":    clog,
        "duration_secs":  duration,
    }
