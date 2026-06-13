"""
VideoConverter — Flask application entry point.
Routes only; no business logic here.
"""
import ctypes
import json
import os
import re
import shutil
import sqlite3
import string
import subprocess as _sp
import sys
import threading
import time
from datetime import datetime, timezone
from flask import Flask, Response, jsonify, render_template, request, stream_with_context

import config
import converter
import db
import scanner
app = Flask(__name__)

BASE_DIR    = os.path.dirname(__file__)
DB_PATH     = os.path.join(BASE_DIR, "conversions.db")
db.init_db(DB_PATH)

# Disable power-throttling for ffmpeg so E-cores don't cause FP overflows
try:
    ffmpeg_exe = _sp.check_output(["where", "ffmpeg"], text=True).split()[0].strip()
    _sp.run(
        ["powercfg", "/powerthrottling", "disable", "/path", ffmpeg_exe],
        capture_output=True, timeout=5,
    )
except Exception:
    pass  # non-fatal — proceeed without it

# ---------------------------------------------------------------------------
# Job state (mutated only while holding _job_lock)
# ---------------------------------------------------------------------------
_job_lock        = threading.Lock()
_stop_event      = threading.Event()   # hard stop: kills ffmpeg mid-encode
_soft_stop_event = threading.Event()   # soft stop: finishes current file, stops queue
_ffmpeg_pid: list[int] = [0]   # _ffmpeg_pid[0] = current child PID; 0 means idle

_job: dict = {
    "state":           "idle",   # idle | running | done
    "current_index":   0,
    "total":           0,
    "current_file":    "",
    "file_started_at": 0.0,
    "progress_pct":    0.0,
    "fps":             0.0,
    "eta_secs":        0,
    "saved_mb":        0.0,
    "encoder":         "",
    "files":           [],
    "log":             [],
    "paused":          False,
    "phase":           "",          # "" | "ocr_batch" | "converting"
    "ocr_batch":       {"total": 0, "done": 0, "current_file": "", "files": []},
    "steps":           [],          # per-file step checklist
    "ffmpeg_status":   "",          # single live status line (no history)
    "ffmpeg_status_at": 0.0,         # unix timestamp of last status update
}

_PROCESS_ALL_ACCESS = 0x1F0FFF
_session_started_at: float = 0.0   # set when /api/start is called
_ESTIMATE_VERSION = 2  # bump when estimate methodology/output semantics change


def _as_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        if isinstance(value, str):
            value = value.replace(",", "").strip()
            if not value:
                return default
        return float(value)
    except Exception:
        return default


def _status_savings_fields(data: dict) -> dict:
    files = data.get("files") or []
    realized_mb = sum(
        _as_float(f.get("saved"), 0.0)
        for f in files
        if (f.get("status") == "done")
    )
    projected_total_mb = _as_float(data.get("saved_mb"), realized_mb)
    in_progress_est_mb = max(0.0, projected_total_mb - realized_mb)

    current_saved_mb = 0.0
    current_est_mb = 0.0
    current_path = data.get("current_file") or ""
    current = next((f for f in files if (f.get("full_path") or "") == current_path), None)
    if current:
        current_est_mb = _as_float(current.get("est_mb"), 0.0)
        if current.get("status") == "done":
            current_saved_mb = _as_float(current.get("saved"), 0.0)
        elif data.get("state") == "running":
            current_saved_mb = max(0.0, projected_total_mb - realized_mb)
        else:
            current_saved_mb = _as_float(current.get("saved"), 0.0)

    return {
        "session_realized_mb": round(realized_mb, 1),
        "session_in_progress_est_mb": round(in_progress_est_mb, 1),
        "session_projected_total_mb": round(projected_total_mb, 1),
        "current_file_saved_mb": round(current_saved_mb, 1),
        "current_file_est_mb": round(current_est_mb, 1),
    }


def _suspend_ffmpeg() -> None:
    pid = _ffmpeg_pid[0]
    if pid:
        h = ctypes.windll.kernel32.OpenProcess(_PROCESS_ALL_ACCESS, False, pid)
        if h:
            ctypes.windll.ntdll.NtSuspendProcess(h)
            ctypes.windll.kernel32.CloseHandle(h)


def _resume_ffmpeg() -> None:
    pid = _ffmpeg_pid[0]
    if pid:
        h = ctypes.windll.kernel32.OpenProcess(_PROCESS_ALL_ACCESS, False, pid)
        if h:
            ctypes.windll.ntdll.NtResumeProcess(h)
            ctypes.windll.kernel32.CloseHandle(h)


def _kill_ffmpeg() -> None:
    """Immediately terminate the ffmpeg child process (Hard Stop)."""
    pid = _ffmpeg_pid[0]
    if pid:
        h = ctypes.windll.kernel32.OpenProcess(_PROCESS_ALL_ACCESS, False, pid)
        if h:
            ctypes.windll.kernel32.TerminateProcess(h, 1)
            ctypes.windll.kernel32.CloseHandle(h)


def _job_log(msg: str) -> None:
    with _job_lock:
        _job["log"].append(msg)
        if len(_job["log"]) > 500:
            _job["log"] = _job["log"][-400:]


def _write_crash_log(msg: str) -> None:
    """Append a crash/traceback to a persistent file so it survives restarts."""
    try:
        _crash_path = os.path.join(os.path.dirname(__file__), "logs", "worker_crashes.log")
        os.makedirs(os.path.dirname(_crash_path), exist_ok=True)
        with open(_crash_path, "a", encoding="utf-8") as _cf:
            from datetime import datetime as _dt
            _cf.write(f"\n{'='*60}\n{_dt.now().isoformat(timespec='seconds')}\n{msg}\n")
    except Exception:
        pass


def _write_ffmpeg_status_log(msg: str) -> None:
    """Append raw ffmpeg status lines to a persistent diagnostic file."""
    try:
        _status_path = os.path.join(os.path.dirname(__file__), "logs", "ffmpeg_status_raw.log")
        os.makedirs(os.path.dirname(_status_path), exist_ok=True)
        with open(_status_path, "a", encoding="utf-8") as _sf:
            from datetime import datetime as _dt
            with _job_lock:
                _current = _job.get("current_file", "")
            _sf.write(f"[{_dt.now().isoformat(timespec='seconds')}] {_current} | {msg}\n")
    except Exception:
        pass


def _step(step_id: str, state: str, detail: str = "", attempt: int = 1) -> None:
    """Update a single step's state in the current job under the lock."""
    with _job_lock:
        for s in _job["steps"]:
            if s["id"] == step_id:
                s["state"]   = state
                s["detail"]  = detail
                s["attempt"] = attempt
                return


def _set_phase(phase: str) -> None:
    with _job_lock:
        _job["phase"] = phase


def _build_steps(file_info: dict, anime_mode: bool) -> list:
    """Build the per-file step list based on codec and stream types."""
    streams  = file_info.get("streams") or {}
    vid      = streams.get("video") or {}
    v_codec  = (vid.get("codec") or "").lower() if isinstance(vid, dict) else ""
    subs     = streams.get("subs") or []
    has_pgs  = any(
        (s.get("codec") or "").upper() in ("PGS", "HDMV_PGS_SUBTITLE", "PGSSUB")
        for s in subs
    )
    steps: list = []
    if anime_mode:
        ocr_state  = "waiting" if has_pgs else "skipped"
        ocr_detail = "" if has_pgs else "No PGS tracks"
        steps.append({"id": "ocr",   "label": "OCR",   "state": ocr_state,  "detail": ocr_detail, "attempt": 1})
        if v_codec in ("av1", "av1_cuvid") and not bool(getattr(config, "REENCODE_AV1", True)):
            steps.append({"id": "remux", "label": "Remux", "state": "waiting", "detail": "AV1 stream-copy → MP4", "attempt": 1})
        elif v_codec in ("hevc", "hevc_cuvid", "hevc_qsv"):
            steps.append({"id": "remux", "label": "Remux", "state": "waiting", "detail": "HEVC stream-copy → MP4", "attempt": 1})
        else:
            steps.append({"id": "estimate", "label": "Estimate", "state": "waiting", "detail": "", "attempt": 1})
            steps.append({"id": "compress", "label": "Compress", "state": "waiting", "detail": "",           "attempt": 1})
            steps.append({"id": "audio",    "label": "Audio",    "state": "waiting", "detail": "",           "attempt": 1})
            steps.append({"id": "remux",    "label": "Remux",    "state": "waiting", "detail": "MKV → MP4", "attempt": 1})
        steps.append({"id": "verify", "label": "Verify", "state": "waiting", "detail": "", "attempt": 1})
    else:
        steps.append({"id": "estimate", "label": "Estimate", "state": "waiting", "detail": "", "attempt": 1})
        steps.append({"id": "compress", "label": "Compress", "state": "waiting", "detail": "", "attempt": 1})
        steps.append({"id": "verify",   "label": "Verify",   "state": "waiting", "detail": "", "attempt": 1})
    return steps


def _process_step_log(msg: str, attempt_counter: list) -> None:
    """Match a converter log line and advance step states accordingly."""
    m = msg.strip()
    if m == "Anime mode: compressing then remuxing to MP4.":
        _step("compress", "running", "auto")
    elif m.startswith("Hi10 H.264 detected") and "libx265 software encoder" in m:
        _step("compress", "running", "libx265 (software)")
    elif m == "Force SW mode: skipping hevc_qsv.":
        _step("compress", "running", "libx265 (software)")
    elif m == "QSV failed, trying software encoder...":
        _step("compress", "running", "libx265 (software fallback)")
    elif m.startswith("Compressing with "):
        enc = m.split("Compressing with ", 1)[1].rstrip(".")
        _step("compress", "running", enc)
    elif m == "Remuxing compressed output to MP4...":
        _step("compress", "done")
        attempt_counter[0] = 1
        _step("remux", "running", "attempt 1/6")
    elif m == "Remuxing to MP4...":
        if not attempt_counter[0]:
            attempt_counter[0] = 1
        _step("remux", "running", f"attempt {attempt_counter[0]}/6")
    elif m.startswith("DTS overflow detected"):
        attempt_counter[0] += 1
        _step("remux", "retry", f"attempt {attempt_counter[0]}/6 · DTS fix")
    elif m.startswith("DTS fix retry"):
        attempt_counter[0] += 1
        _step("remux", "retry", f"attempt {attempt_counter[0]}/6 · genpts fix")
    elif m.startswith("AAC mux failed"):
        attempt_counter[0] += 1
        _step("audio", "running", "pre-encoding individually")
        _step("remux", "retry", f"attempt {attempt_counter[0]}/6 · audio pre-enc")
    elif m.startswith("Subtitle DTS fix"):
        attempt_counter[0] += 1
        _step("remux", "retry", f"attempt {attempt_counter[0]}/6 · SRT pre-extract")
    elif m.startswith("Retrying with pre-extracted SRT"):
        _step("remux", "running", f"attempt {attempt_counter[0]}/6 · SRT subs")
    elif m.startswith("Retrying without subtitle"):
        attempt_counter[0] += 1
        _step("remux", "retry", f"attempt {attempt_counter[0]}/6 · no subs")
    elif "Pre-encoding audio track" in m:
        detail = m.split("Pre-encoding audio track", 1)[1].strip().rstrip(".")
        _step("audio", "running", detail)
    elif m.startswith("AV1 source") and "stream-copying" in m:
        if not attempt_counter[0]:
            attempt_counter[0] = 1
        _step("remux", "running", f"AV1 stream-copy · attempt {attempt_counter[0]}/6")
    elif m.startswith("Integrity check failed"):
        _step("verify", "failed", m.split("Integrity check failed:", 1)[-1].strip())
    elif m.startswith("Done. Saved"):
        _step("audio", "done")
        _step("remux", "done")
        _step("verify", "running")
    elif m == "Integrity check passed.":
        _step("verify", "done")
    elif m.startswith("Skipped – output was not smaller"):
        _step("compress", "failed", "no savings")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stream_preview_path(source_path: str) -> str:
    """Deterministic preview path used by stream-edit workflow."""
    root, ext = os.path.splitext(source_path)
    return f"{root}.__stream_preview__{ext}"


def _eng_stereo_preview_path(source_path: str) -> str:
    """Deterministic preview path used by English-stereo test workflow."""
    root, ext = os.path.splitext(source_path)
    return f"{root}.__eng_stereo_preview__{ext}"


def _original_backup_path(source_path: str) -> str:
    """Default backup path for in-place replacement workflows."""
    root, ext = os.path.splitext(source_path)
    return f"{root}.original-backup{ext}"


def _first_audio_stream_index_for_language(path: str, language: str = "eng") -> int | None:
    """Return the first ffprobe absolute stream index for the requested audio language."""
    probe = scanner._ffprobe(path)
    if not probe:
        return None
    want = (language or "").strip().lower()
    for stream in probe.get("streams", []):
        if (stream.get("codec_type") or "").lower() != "audio":
            continue
        tags = stream.get("tags") or {}
        lang = (tags.get("language") or "und").lower()
        if lang == want:
            try:
                return int(stream.get("index"))
            except Exception:
                continue
    return None


def _is_job_running() -> bool:
    with _job_lock:
        return _job.get("state") == "running"


def _normalise_lang_code(lang: str) -> str:
    """Normalise mixed language tags (eng/en/EN-us) into compact codes."""
    raw = (lang or "").strip().lower().replace("_", "-")
    if not raw or raw in {"und", "unknown", "none", "null"}:
        return "und"
    base = raw.split("-", 1)[0]
    if len(base) == 2:
        return base
    iso3_to_iso2 = {
        "eng": "en", "ara": "ar", "jpn": "ja", "spa": "es", "por": "pt",
        "fra": "fr", "fre": "fr", "deu": "de", "ger": "de", "ita": "it",
        "rus": "ru", "zho": "zh", "chi": "zh", "kor": "ko",
    }
    return iso3_to_iso2.get(base, base)


def _subtitle_payload_text(srt_text: str) -> str:
    """Strip cue numbers and timestamps, keeping only dialogue text."""
    kept: list[str] = []
    for line in (srt_text or "").splitlines():
        t = line.strip()
        if not t:
            continue
        if t.isdigit() or "-->" in t:
            continue
        kept.append(t)
    return " ".join(kept)


def _detect_subtitle_language(dialogue_text: str) -> dict:
    """Lightweight script/language heuristic for subtitle preview text."""
    text = (dialogue_text or "").strip()
    if not text:
        return {
            "code": "und",
            "label": "Unknown",
            "confidence": 0,
            "script_counts": {},
            "english_stopword_ratio": 0.0,
        }

    counts = {
        "latin": 0,
        "arabic": 0,
        "cyrillic": 0,
        "cjk": 0,
        "other": 0,
    }

    for ch in text:
        o = ord(ch)
        if (65 <= o <= 90) or (97 <= o <= 122):
            counts["latin"] += 1
        elif (
            (0x0600 <= o <= 0x06FF)
            or (0x0750 <= o <= 0x077F)
            or (0x08A0 <= o <= 0x08FF)
            or (0xFB50 <= o <= 0xFDFF)
            or (0xFE70 <= o <= 0xFEFF)
        ):
            counts["arabic"] += 1
        elif (0x0400 <= o <= 0x04FF) or (0x0500 <= o <= 0x052F):
            counts["cyrillic"] += 1
        elif (
            (0x3040 <= o <= 0x30FF)
            or (0x3400 <= o <= 0x4DBF)
            or (0x4E00 <= o <= 0x9FFF)
            or (0xF900 <= o <= 0xFAFF)
        ):
            counts["cjk"] += 1
        elif ch.isalpha():
            counts["other"] += 1

    total_letters = sum(counts.values())
    if total_letters <= 0:
        return {
            "code": "und",
            "label": "Unknown",
            "confidence": 0,
            "script_counts": counts,
            "english_stopword_ratio": 0.0,
        }

    dominant_script = max(counts, key=counts.get)
    dominant_ratio = counts[dominant_script] / max(total_letters, 1)

    tokens = [
        w
        for w in re.findall(r"[A-Za-z']+", text.lower())
        if len(w) >= 1
    ]
    stopwords = {
        "the", "and", "you", "your", "are", "for", "with", "that", "this", "was",
        "have", "not", "but", "from", "they", "she", "him", "her", "what", "when",
        "where", "why", "how", "who", "can", "could", "will", "would", "there", "here",
        "is", "it", "to", "of", "in", "on", "at", "we", "i", "me", "my", "our",
        "be", "do", "did", "does", "an", "a", "as", "if", "then", "than",
    }
    stopword_hits = sum(1 for t in tokens if t in stopwords)
    stopword_ratio = (stopword_hits / len(tokens)) if tokens else 0.0

    code = "und"
    label = "Unknown"
    if dominant_script == "latin":
        if len(tokens) >= 12 and stopword_ratio >= 0.05:
            code = "en"
            label = "English (heuristic)"
        else:
            code = "latin"
            label = "Latin script"
    elif dominant_script == "arabic":
        code = "ar"
        label = "Arabic script"
    elif dominant_script == "cyrillic":
        code = "cyrl"
        label = "Cyrillic script"
    elif dominant_script == "cjk":
        code = "cjk"
        label = "CJK script"
    else:
        code = "und"
        label = "Unknown"

    return {
        "code": code,
        "label": label,
        "confidence": int(round(dominant_ratio * 100)),
        "script_counts": counts,
        "english_stopword_ratio": round(stopword_ratio, 3),
    }


# Windows error codes that indicate unrecoverable hardware / device failure.
# When any of these are detected the entire queue is halted immediately.
_FATAL_WINERRORS = {
    19,    # ERROR_WRITE_PROTECT
    21,    # ERROR_NOT_READY
    23,    # ERROR_CRC  (data error / read failure)
    1117,  # ERROR_IO_DEVICE  ("fatal device error")
    1392,  # ERROR_FILE_CORRUPT
}


def _is_fatal_device_error(exc: BaseException) -> bool:
    """Return True if *exc* looks like a fatal hardware/device I/O error."""
    if isinstance(exc, OSError):
        if getattr(exc, "winerror", None) in _FATAL_WINERRORS:
            return True
        # errno-based check (cross-platform fallback)
        import errno as _errno
        if exc.errno in (_errno.EIO, _errno.ENODEV, _errno.ENXIO):
            return True
    # Also catch device errors buried in error text (e.g. from ffmpeg stderr)
    msg = str(exc).lower()
    if "fatal device error" in msg or "i/o device error" in msg or "device error" in msg:
        return True
    return False


def _queue_worker(
    files: list[dict],
    anime_mode: bool,
    quality: int,
    low_savings_threshold_pct: int = 5,
    estimate_only: bool = False,
    force_reestimate: bool = False,
) -> None:
    """Runs in a daemon thread; processes the file queue sequentially."""
    total_saved = 0.0

    # ------------------------------------------------------------------
    # OCR batch pre-pass (anime mode only)
    # Run ocr_subs.py ONCE with all pending files so PyTorch loads exactly
    # once and is reused across every file.  Per-file subprocess restarts
    # are the root cause of cold-start native crashes (0xC0000005/409).
    # ------------------------------------------------------------------
    ocr_failed_paths: set[str] = set()
    if anime_mode:
        import glob as _glob
        _all_paths = [fi["full_path"].replace("\\", "/") for fi in files]
        _db_statuses = db.get_latest_statuses_by_paths(_all_paths)
        _ocr_paths = [
            p for p in _all_paths
            if _db_statuses.get(p, {}).get("status") not in ("done", "skipped", "no_saving")
        ]
        if _ocr_paths:
            _path_to_idx = {fi["full_path"].replace("\\", "/"): i for i, fi in enumerate(files)}
            with _job_lock:
                for p in _ocr_paths:
                    _job["files"][_path_to_idx[p]]["status"] = "ocr"
            _ocr_script = os.path.join(os.path.dirname(__file__), "ocr_subs.py")
            _ocr_env = os.environ.copy()
            _ocr_env["PYTHONUTF8"] = "1"

            # Build a per-file drop manifest so ocr_subs.py knows which
            # PGS streams to skip (e.g. a track that previously crashed PyTorch).
            _drop_manifest = {
                p: db.get_dropped_streams(p)
                for p in _ocr_paths
            }
            # Write to a temp file; ocr_subs.py reads it via --skip-manifest
            import tempfile as _tmpfile
            _manifest_fd, _manifest_path = _tmpfile.mkstemp(suffix=".json", prefix="ocr_drops_")
            try:
                with os.fdopen(_manifest_fd, "w", encoding="utf-8") as _mf:
                    import json as _json_tmp
                    _json_tmp.dump(_drop_manifest, _mf)
            except Exception:
                _manifest_path = None

            # Retry loop: if the batch crashes, mark only the crashing file as
            # failed and restart with the remaining files.  This ensures one
            # bad file doesn't block all the others.
            _remaining = list(_ocr_paths)

            # Initialise OCR batch state for the UI.
            with _job_lock:
                _job["phase"] = "ocr_batch"
                _job["ocr_batch"] = {
                    "total":        len(_ocr_paths),
                    "done":         0,
                    "current_file": os.path.basename(_ocr_paths[0]) if _ocr_paths else "",
                    "files": [
                        {"name": os.path.basename(p), "state": "waiting"}
                        for p in _ocr_paths
                    ],
                }
                # Mark the first file as running immediately.
                if _job["ocr_batch"]["files"]:
                    _job["ocr_batch"]["files"][0]["state"] = "running"

            while _remaining:
                _ocr_bn_map: dict[str, str] = {os.path.basename(p): p for p in _remaining}
                _current_ocr_path = _remaining[0]
                crashed = False
                _ocr_proc = None
                _ocr_rsp_file = None
                try:
                    _manifest_args = ["--skip-manifest", _manifest_path] if _manifest_path else []
                    _base_cmd = [sys.executable, _ocr_script] + _manifest_args
                    _cmdline_len = sum(len(p) + 3 for p in _base_cmd + _remaining)
                    if _cmdline_len > 28000:
                        import tempfile as _tempfile
                        _rsp = _tempfile.NamedTemporaryFile(
                            mode="w", suffix=".txt", delete=False,
                            encoding="utf-8", prefix="ocr_rsp_",
                        )
                        _rsp.write("\n".join(_remaining))
                        _rsp.close()
                        _ocr_rsp_file = _rsp.name
                        _file_args = [f"@{_ocr_rsp_file}"]
                    else:
                        _file_args = _remaining
                    _ocr_proc = _sp.Popen(
                        _base_cmd + _file_args,
                        stdout=_sp.PIPE,
                        stderr=_sp.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        env=_ocr_env,
                    )
                    _prev_was_sep = False
                    _ocr_prev_path = _current_ocr_path
                    for _ocr_line in _ocr_proc.stdout:
                        stripped = _ocr_line.rstrip()
                        if _prev_was_sep and stripped.startswith("  ") and stripped.strip() in _ocr_bn_map:
                            _new_path = _ocr_bn_map[stripped.strip()]
                            if _new_path != _current_ocr_path:
                                # Mark previous as done, new one as running
                                _prev_bn = os.path.basename(_current_ocr_path)
                                _new_bn  = os.path.basename(_new_path)
                                with _job_lock:
                                    b = _job["ocr_batch"]
                                    for _fi in b["files"]:
                                        if _fi["name"] == _prev_bn:
                                            _fi["state"] = "done"
                                            b["done"] = b.get("done", 0) + 1
                                        if _fi["name"] == _new_bn:
                                            _fi["state"] = "running"
                                    b["current_file"] = _new_bn
                                _current_ocr_path = _new_path
                        _prev_was_sep = (stripped == "-" * 60)
                        if stripped:
                            _job_log(f"[ocr] {stripped}")
                        if _stop_event.is_set():
                            _ocr_proc.kill()
                            break
                    _ocr_proc.wait()
                    if _ocr_proc.returncode != 0:
                        crashed = True
                except Exception as _ocr_exc:
                    _job_log(f"[ocr] Batch OCR error: {_ocr_exc}")
                    crashed = True
                finally:
                    if _ocr_rsp_file and os.path.exists(_ocr_rsp_file):
                        try:
                            os.remove(_ocr_rsp_file)
                        except OSError:
                            pass

                if crashed:
                    # Check which files in this batch now have sidecars
                    _no_sidecar = []
                    for _fp in _remaining:
                        _stem   = os.path.splitext(os.path.basename(_fp))[0]
                        _parent = os.path.dirname(_fp)
                        if not _glob.glob(os.path.join(_parent, f"{_stem}.pgs*.srt")):
                            _no_sidecar.append(_fp)

                    # The crashing file is the one being processed when the crash
                    # occurred AND has no sidecar — mark it permanently failed.
                    if _current_ocr_path in _no_sidecar:
                        ocr_failed_paths.add(_current_ocr_path)
                        _exit = _ocr_proc.returncode if _ocr_proc is not None else "N/A"
                        _job_log(f"[ocr] Crash on {os.path.basename(_current_ocr_path)} (exit {_exit}) — marking as failed, retrying remaining files.")

                    # Restart with files that still need OCR (excluding the crasher)
                    _remaining = [p for p in _no_sidecar if p != _current_ocr_path]
                    if _remaining:
                        _job_log(f"[ocr] Restarting OCR batch for {len(_remaining)} remaining file(s).")
                    else:
                        break
                else:
                    # Clean exit — any file still missing a sidecar genuinely failed,
                    # UNLESS all its PGS streams were dropped (nothing to OCR → OK).
                    _PGS_CODECS = {"hdmv_pgs_subtitle", "pgssub"}
                    for _fp in _remaining:
                        _stem   = os.path.splitext(os.path.basename(_fp))[0]
                        _parent = os.path.dirname(_fp)
                        if _glob.glob(os.path.join(_parent, f"{_stem}.pgs*.srt")):
                            continue  # has sidecar — success
                        # No sidecar: check whether any non-dropped PGS streams exist
                        _fi     = next((fi for fi in files if fi["full_path"].replace("\\", "/") == _fp), None)
                        _fstreams = (_fi.get("streams") or {}).get("subs", []) if _fi else []
                        _drops  = set(_drop_manifest.get(_fp, []))
                        _has_active_pgs = any(
                            s.get("codec", "").upper() in ("PGS", "HDMV_PGS_SUBTITLE", "PGSSUB")
                            and s.get("index") not in _drops
                            for s in _fstreams
                        )
                        if _has_active_pgs:
                            ocr_failed_paths.add(_fp)
                    break

            if ocr_failed_paths:
                _job_log(f"[ocr] {len(ocr_failed_paths)} file(s) have no OCR output — will mark as failed.")

            # Stamp each file with its OCR outcome so the UI can show a sub-badge.
            # "done"    → OCR ran and produced a .pgs*.srt sidecar
            # "skipped" → no active PGS bitmap tracks; OCR was a no-op
            for _fp in _ocr_paths:
                if _fp in ocr_failed_paths:
                    continue  # will be marked failed overall; no sub-badge needed
                _stem    = os.path.splitext(os.path.basename(_fp))[0]
                _parent  = os.path.dirname(_fp)
                _has_srt = bool(_glob.glob(os.path.join(_parent, f"{_stem}.pgs*.srt")))
                _ocr_idx = _path_to_idx.get(_fp)
                if _ocr_idx is not None:
                    with _job_lock:
                        _job["files"][_ocr_idx]["ocr_status"] = "done" if _has_srt else "skipped"

            # Clean up the drop manifest temp file
            if _manifest_path and os.path.exists(_manifest_path):
                try:
                    os.remove(_manifest_path)
                except OSError:
                    pass

            # OCR batch is done — mark final states and clear the batch UI.
            with _job_lock:
                b = _job["ocr_batch"]
                for _fi in b["files"]:
                    _fp_full = next((p for p in _ocr_paths if os.path.basename(p) == _fi["name"]), None)
                    if _fp_full in ocr_failed_paths:
                        _fi["state"] = "failed"
                    elif _fi["state"] not in ("done", "failed"):
                        _fi["state"] = "done"
                b["done"]         = sum(1 for _fi in b["files"] if _fi["state"] == "done")
                b["current_file"] = ""
                _job["phase"] = ""

    for idx, file_info in enumerate(files):
        if _stop_event.is_set() or _soft_stop_event.is_set():
            break

        _sidecar_reset = False  # set True when re-queuing a done/low_savings file for sub-inject only
        full_path = file_info["full_path"].replace("\\", "/")

        # Always check the live DB status — never trust what the client sent
        db_rec = db.get_latest_statuses_by_paths([full_path]).get(full_path, {})
        db_status = db_rec.get("status")
        if db_status in ("done", "skipped", "no_saving", "low_savings"):
            # For statuses where re-encode is normally suppressed, check if
            # sidecar subtitle files have appeared alongside the file
            # (.pgsN.srt, .en.srt, .srt, etc.).  If so, re-process so the
            # converter's sub-inject path can mux them in (no re-encode —
            # just a fast copy + merge).  Applies to done/low_savings/no_saving;
            # 'skipped' is an explicit manual skip so we leave it alone.
            _should_skip = True
            if db_status in ("done", "low_savings", "no_saving"):
                from pathlib import Path as _Path
                _SIDECAR_EXTS = {".srt", ".ass", ".ssa"}
                _fp = _Path(full_path)
                _stem_lower = _fp.stem.lower()
                try:
                    _should_skip = not any(
                        p.suffix.lower() in _SIDECAR_EXTS and (
                            p.stem.lower() == _stem_lower
                            or p.stem.lower().startswith(_stem_lower + ".")
                        )
                        for p in _fp.parent.iterdir()
                    )
                except OSError:
                    pass
            if _should_skip:
                with _job_lock:
                    _job["files"][idx]["status"] = db_status
                    _job["current_index"] = idx
                continue
            # Sidecar(s) found — reset DB and flag that this is a sub-inject
            # operation (no re-encode).  The estimate step will be skipped.
            _rec_id_for_reset = db_rec.get("id")
            if _rec_id_for_reset:
                db.reset_done_to_pending(_rec_id_for_reset)
            db_status = "pending"
            _sidecar_reset = True
        # Independent sidecar check for files already at pending status (e.g.
        # reset to pending by the scanner before the worker got here).
        # An MP4 with sidecar SRT/ASS files will always hit the sub-inject
        # fast-path in compress_and_remux (no re-encode), so the savings
        # estimate is meaningless and must be skipped to avoid re-triggering
        # low_savings from a stale cached estimate.
        if not _sidecar_reset and full_path.lower().endswith(".mp4"):
            from pathlib import Path as _Path
            _SIDECAR_EXTS = {".srt", ".ass", ".ssa"}
            _sfp = _Path(full_path)
            _stem_l = _sfp.stem.lower()
            try:
                if any(
                    p.suffix.lower() in _SIDECAR_EXTS and (
                        p.stem.lower() == _stem_l
                        or p.stem.lower().startswith(_stem_l + ".")
                    )
                    for p in _sfp.parent.iterdir()
                ):
                    _sidecar_reset = True
            except OSError:
                pass
        force_sw = bool(db_rec.get("force_sw", False))
        force_convert = bool(db_rec.get("force_convert", False))

        try:
            mtime      = os.path.getmtime(full_path)
            size_bytes = os.path.getsize(full_path)
            size_mb    = size_bytes / (1024 * 1024)
        except OSError as exc:
            _job_log(f"Skipped {full_path}: {exc}")
            with _job_lock:
                _job["files"][idx]["status"] = "failed"
            continue

        # Ensure a DB record exists for this file
        codec = (file_info.get("streams") or {}).get("video", {}) or {}
        rec_id = db.upsert_pending(
            source_path       = full_path,
            source_mtime      = mtime,
            source_size_bytes = size_bytes,
            source_size_mb    = size_mb,
            source_codec      = codec.get("codec") if isinstance(codec, dict) else None,
            anime_mode        = anime_mode,
        )

        with _job_lock:
            _job["current_index"]   = idx
            _job["current_file"]    = full_path
            _job["file_started_at"] = time.time()
            _job["progress_pct"]  = 0.0
            _job["fps"]           = 0.0
            _job["eta_secs"]      = 0
            _job["encoder"]       = ""
            _job["ffmpeg_status"] = "starting ffmpeg..."
            _job["ffmpeg_status_at"] = time.time()
            # Status will be set to 'ocr' or 'converting' below once we know
            # whether an OCR pre-flight is needed.

        db.mark_running(rec_id, _utcnow())
        _job_log(f"[{idx+1}/{len(files)}] {os.path.basename(full_path)}")

        # Check OCR batch result (anime mode only)
        if full_path in ocr_failed_paths:
            _job_log("OCR pre-flight failed for this file — marking as failed.")
            db.mark_failed(rec_id, "OCR batch pre-flight failed", _utcnow())
            with _job_lock:
                _job["files"][idx]["status"] = "failed"
                _job["files"][idx]["error_tail"] = "OCR batch pre-flight failed"
            continue

        # Now set the badge to 'converting'.
        with _job_lock:
            _job["files"][idx]["status"] = "converting"

        # Build the step checklist for this file and set phase.
        _new_steps = _build_steps(file_info, anime_mode)
        # Backfill OCR step state from the batch result.
        for _s in _new_steps:
            if _s["id"] == "ocr":
                ocr_st = (file_info.get("ocr_status") or
                          ("done" if full_path not in ocr_failed_paths and
                           any(fi.get("ocr_status") for fi in [file_info]) else ""))
                # Read from the job files dict which was stamped during batch
                with _job_lock:
                    _stamped = _job["files"][idx].get("ocr_status", "")
                if _stamped == "done":
                    _s["state"]  = "done"
                    _s["detail"] = "SRT extracted"
                elif _stamped == "skipped" or _s["state"] == "skipped":
                    _s["state"]  = "skipped"
                    _s["detail"] = "No PGS tracks"
                elif _s["state"] == "waiting":
                    # Batch ran but no explicit stamp — mark done
                    _s["state"]  = "done"
                    _s["detail"] = "SRT extracted"
                break
        with _job_lock:
            _job["steps"] = _new_steps
            _job["phase"] = "converting"

        # Output goes into the same directory as the source so the converted
        # file replaces (or sits beside) the original in-place.  When the
        # extension is unchanged compress_simple uses os.replace() to swap
        # the file atomically; when it changes (e.g. MKV→MP4 in anime mode)
        # the old source is removed explicitly below.
        output_dir = os.path.dirname(full_path)

        _tmp_holder: list[str] = [""]  # set by compress_simple so _progress can stat it

        def _progress(pct: float, fps: float, eta: int) -> None:
            with _job_lock:
                _job["progress_pct"] = pct
                _job["fps"]          = fps
                _job["eta_secs"]     = eta
                # Live savings estimate: completed files + current file projection.
                # We scale the partial temp size up by the inverse of progress so
                # the estimate reflects the projected *full* output size, not just
                # the bytes written so far.
                tmp = _tmp_holder[0]
                if tmp and pct > 1.0:
                    try:
                        live_out_bytes   = os.path.getsize(tmp)
                        projected_full   = live_out_bytes / (pct / 100.0)
                        live_saved       = max(0.0, size_bytes - projected_full) / (1024 * 1024)
                        _job["saved_mb"] = total_saved + live_saved
                    except OSError:
                        pass

        # Per-file log capture so error details can be surfaced in the UI
        _file_log: list[str] = []
        _REMUX_ATTEMPT = [0]   # tracks current remux attempt number for this file

        def _capture_log(msg: str) -> None:
            if msg.startswith("__ffstatus__:"):
                raw = msg.split(":", 1)[1].strip()
                if raw:
                    with _job_lock:
                        _job["ffmpeg_status"] = raw
                        _job["ffmpeg_status_at"] = time.time()
                    _write_ffmpeg_status_log(raw)
                return
            _file_log.append(msg)
            _job_log(msg)
            _process_step_log(msg, _REMUX_ATTEMPT)

        # Hash the source before encoding so we can recognise it later even
        # if it's moved to a different drive (resetting mtime).
        src_hash = db.hash_file_head(full_path)
        if src_hash:
            db.update_source_hash(rec_id, src_hash)

        # --- Step 0: Estimate (run before encode when step is present) ---
        _has_estimate = any(s["id"] == "estimate" for s in _new_steps)
        if _has_estimate and not _stop_event.is_set():
            if _sidecar_reset:
                # Re-queued for sidecar merge only — no re-encode, so the
                # savings estimate is meaningless.  Skip it unconditionally.
                _step("estimate", "skipped", "sidecar merge — no re-encode")
            else:
                # Fast-skip heuristic: HEVC sources already at low bitrate
                # are extremely unlikely to compress further — skip the 10s
                # test encode and mark low_savings immediately.
                # Threshold is 1500 kbps normalised to 1080p @ 25fps so that
                # 4K/60fps sources are treated fairly.  A 4K 50fps file at
                # 4446 kbps normalises to ~556 kbps — correctly fast-skipped.
                _HEVC_FASTSKIP_KBPS = 1500
                _src_codec   = (db_rec.get("codec") or "").upper()
                _src_bitrate = db_rec.get("bitrate_kbps")
                if (_src_codec == "HEVC"
                        and _src_bitrate is not None):
                    # Normalise bitrate to 1920×1080 @ 25fps reference
                    _vid_info    = (file_info.get("streams") or {}).get("video") or {}
                    _res_str     = (_vid_info.get("resolution") or "")
                    _fps_str     = (_vid_info.get("fps") or "")
                    try:
                        _w, _h   = (int(x) for x in _res_str.split("x"))
                        _fps_val = float(_fps_str)
                        _pixels  = _w * _h
                    except (ValueError, AttributeError):
                        _pixels  = 1920 * 1080
                        _fps_val = 25.0
                    _ref_pixels  = 1920 * 1080
                    _ref_fps     = 25.0
                    _norm_bitrate = _src_bitrate * (_ref_pixels / _pixels) * (_ref_fps / max(_fps_val, 1.0))
                    if _norm_bitrate < _HEVC_FASTSKIP_KBPS and not force_convert:
                        _step("estimate", "done", f"HEVC {_src_bitrate}\u202fkbps (norm {round(_norm_bitrate)}\u202fkbps) \u2014 fast skip")
                        _job_log(f"HEVC source at {_src_bitrate} kbps (normalised {round(_norm_bitrate)} kbps < {_HEVC_FASTSKIP_KBPS} kbps) — skipping estimate, marking low_savings.")
                        db.save_estimate(
                            rec_id,
                            0,
                            0.0,
                            est_sample_cv_pct=0.0,
                            est_high_variance=False,
                            est_aggregation="fast_skip",
                            est_quality=quality,
                            est_version=_ESTIMATE_VERSION,
                        )
                        db.mark_low_savings(rec_id, 0, low_savings_threshold_pct, _utcnow())
                        with _job_lock:
                            _job["files"][idx]["status"] = "low_savings"
                        continue
                    if _norm_bitrate < _HEVC_FASTSKIP_KBPS and force_convert:
                        _job_log(
                            f"HEVC source at {_src_bitrate} kbps (normalised {round(_norm_bitrate)} kbps < {_HEVC_FASTSKIP_KBPS} kbps) — force convert enabled, bypassing fast-skip."
                        )
                # Use cached estimate from DB if available — avoids the 10s test-encode
                # on re-runs (e.g. queue re-started after threshold change).
                _cached_pct = db_rec.get("est_saving_pct")
                _cached_mb  = db_rec.get("est_saving_mb")
                _cached_quality = db_rec.get("est_quality")
                _cached_version = db_rec.get("est_version")
                _cache_context_ok = (
                    _cached_quality == quality
                    and _cached_version == _ESTIMATE_VERSION
                )
                if (
                    not force_reestimate
                    and _cached_pct is not None
                    and _cached_mb is not None
                    and _cache_context_ok
                ):
                    _est = {
                        "estimated_saving_pct": _cached_pct,
                        "estimated_saving_mb": _cached_mb,
                        "sample_cv_pct": db_rec.get("est_sample_cv_pct"),
                        "high_variance": bool(db_rec.get("est_high_variance", False)),
                        "aggregation": db_rec.get("est_aggregation"),
                        "error": None,
                    }
                    _step("estimate", "running", "cached")
                else:
                    if force_reestimate and _cached_pct is not None:
                        _job_log("Force re-estimate enabled — bypassing cached estimate.")
                    elif _cached_pct is not None and not _cache_context_ok:
                        _job_log(
                            f"Estimate cache invalid (quality/version mismatch; cached q={_cached_quality}, v={_cached_version}, current q={quality}, v={_ESTIMATE_VERSION}) — recomputing."
                        )
                    _step("estimate", "running", "sampling 10s clip\u2026")
                    _est = converter.estimate(full_path, quality=quality)
                if _est.get("error"):
                    # WMV, too short, etc. — skip estimate, proceed to encode normally
                    _step("estimate", "skipped", _est["error"])
                else:
                    _est_pct = _est["estimated_saving_pct"]
                    _est_mb  = _est["estimated_saving_mb"]
                    _est_high_variance = bool(_est.get("high_variance", False))
                    _est_cv = _est.get("sample_cv_pct")
                    _est_aggregation = _est.get("aggregation")
                    # Persist result so it is reused on any future run
                    db.save_estimate(
                        rec_id,
                        _est_pct,
                        _est_mb,
                        est_sample_cv_pct=_est_cv,
                        est_high_variance=_est_high_variance,
                        est_aggregation=_est_aggregation,
                        est_quality=quality,
                        est_version=_ESTIMATE_VERSION,
                    )
                    with _job_lock:
                        _job["files"][idx]["est_pct"] = _est_pct
                        _job["files"][idx]["est_mb"]  = _est_mb
                        _job["files"][idx]["est_cv"] = _est_cv
                        _job["files"][idx]["est_high_variance"] = _est_high_variance
                        _job["files"][idx]["est_aggregation"] = _est_aggregation
                    _threshold = low_savings_threshold_pct
                    if _est_pct < _threshold and not _est_high_variance and not force_convert:
                        _step("estimate", "done", f"~{_est_pct}% \u2014 below {_threshold}% threshold")
                        _job_log(f"Estimated savings {_est_pct}% < {_threshold}% threshold \u2014 skipping encode.")
                        db.mark_low_savings(rec_id, _est_pct, _threshold, _utcnow())
                        with _job_lock:
                            _job["files"][idx]["status"] = "low_savings"
                        continue
                    else:
                        if _est_pct < _threshold and force_convert:
                            _job_log(
                                f"Estimated savings {_est_pct}% < {_threshold}% threshold \u2014 force convert enabled, continuing."
                            )
                        _step("estimate", "done", f"~{_est_pct}% \u00b7 {_est_mb}\u202fMB")
                        if estimate_only:
                            # Estimate-only pass: don't compress, leave file pending
                            # so the normal convert queue picks it up later.
                            _step("compress", "skipped", "estimate-only mode")
                            db.reset_done_to_pending(rec_id)  # revert running → pending
                            with _job_lock:
                                _job["files"][idx]["status"] = "pending"
                            continue

        _conv_start = time.time()
        try:
            result = converter.convert_video(
                input_path      = full_path,
                output_dir      = output_dir,
                anime_mode      = anime_mode,
                quality         = quality,
                progress_cb     = _progress,
                stop_event      = _stop_event,
                log             = _capture_log,
                pid_holder      = _ffmpeg_pid,
                tmp_holder      = _tmp_holder,
                force_sw        = force_sw,
                dropped_streams = db.get_dropped_streams(full_path),
            )
        except Exception as exc:
            _job_log(f"UNHANDLED EXCEPTION during conversion: {exc}")
            import traceback
            error_tail = traceback.format_exc()
            _write_crash_log(f"UNHANDLED EXCEPTION converting {full_path}:\n{error_tail}")
            db.mark_failed(rec_id, error_tail, _utcnow())
            with _job_lock:
                _job["files"][idx]["status"]     = "failed"
                _job["files"][idx]["error_tail"] = error_tail
                _job["files"][idx]["conv_secs"]  = max(0, int(time.time() - _conv_start))
            if _is_fatal_device_error(exc):
                _job_log("FATAL DEVICE ERROR — halting queue to prevent further damage.")
                _stop_event.set()
            continue

        _conv_secs = max(0, int(time.time() - _conv_start))
        if result["ok"]:
            out_norm = os.path.normpath(result["output_path"]) if result["output_path"] else ""
            src_norm = os.path.normpath(full_path)

            # Belt-and-suspenders: verify tracks before committing to 'done'
            track_ok = True
            track_reason = ""
            if out_norm and out_norm != src_norm:
                # Skip subtitle check for MP4 outputs (anime mode): remux_to_mp4
                # intentionally excludes non-English subs and unprocessable bitmap
                # subs (e.g. dvd_subtitle), so the output MP4 legitimately has
                # fewer subtitle tracks than the source.  Audio is still checked.
                out_is_mp4 = result["output_path"].lower().endswith(".mp4")
                track_ok, track_reason = converter._verify_tracks_preserved(
                    full_path, result["output_path"],
                    check_subs=not out_is_mp4,
                    dropped_streams=db.get_dropped_streams(full_path),
                )

            if not track_ok:
                _job_log(
                    f"ERROR: track verification failed — removing bad output: {track_reason}"
                )
                try:
                    os.remove(result["output_path"])
                except OSError:
                    pass
                db.mark_failed(rec_id, f"track verification: {track_reason}", _utcnow())
                _clog = result.get("conv_logger")
                if _clog:
                    _clog.failure(f"track verification: {track_reason}")
                with _job_lock:
                    _job["files"][idx]["status"]     = "failed"
                    _job["files"][idx]["error_tail"] = f"track verification: {track_reason}"
                    _job["files"][idx]["conv_secs"]  = _conv_secs
                    _job["files"][idx]["log_dir"]    = str(_clog.run_dir) if _clog else ""
            else:
                total_saved += result["saved_mb"]
                out_hash = db.hash_file_head(result["output_path"]) if result.get("output_path") else None
                _dur = result.get("duration_secs") or 0
                _out_br = round(result["output_size_mb"] * 8192 / _dur) if _dur > 0 else None
                db.mark_done(
                    record_id           = rec_id,
                    output_path         = result["output_path"],
                    output_size_mb      = result["output_size_mb"],
                    saved_mb            = result["saved_mb"],
                    saved_pct           = result["saved_pct"],
                    completed_at        = _utcnow(),
                    encoder_used        = result["encoder_used"],
                    output_hash         = out_hash,
                    output_bitrate_kbps = _out_br,
                )
                # Remove source when output path differs (e.g. MKV→MP4 in anime mode)
                if out_norm and out_norm != src_norm:
                    try:
                        os.remove(full_path)
                        _job_log(f"Deleted source: {full_path}")
                    except OSError as exc:
                        _job_log(f"Could not delete source: {exc}")
                _clog = result.get("conv_logger")
                if _clog:
                    _clog.success(result["encoder_used"], result["saved_pct"])
                with _job_lock:
                    _job["files"][idx].update({
                        "status":     "done",
                        "output":     str(round(result["output_size_mb"], 1)),
                        "saved":      str(round(result["saved_mb"], 1)),
                        "pct":        str(result["saved_pct"]),
                        "output_path": result["output_path"],
                        "conv_secs":  _conv_secs,
                    })
        else:
            # Harvest error details from the per-file log capture
            ffmpeg_cmd = next(
                (line[len("Running: "):] for line in _file_log if line.startswith("Running: ")),
                "",
            )
            _clog = result.get("conv_logger")
            if result.get("error") == "stopped" or _stop_event.is_set():
                # Hard-stop is user intent, not a conversion failure.
                db.reset_done_to_pending(rec_id)
                if _clog:
                    _clog.failure("stopped")
                with _job_lock:
                    _job["files"][idx]["status"] = "pending"
                    _job["files"][idx]["ffmpeg_cmd"] = ""
                    _job["files"][idx]["error_tail"] = ""
                    _job["files"][idx]["conv_secs"] = _conv_secs
                _job_log("Stopped by user — returned file to pending.")
            elif result.get("error") == "no_savings":
                db.mark_no_saving(rec_id, _utcnow())
                if _clog:
                    _clog.failure("no_savings")
                with _job_lock:
                    _job["files"][idx]["status"]    = "no_saving"
                    _job["files"][idx]["conv_secs"] = _conv_secs
                _job_log("Skipped – output was not smaller than source.")
            else:
                error_tail = "\n".join(_file_log[-50:])
                db.mark_failed(rec_id, error_tail, _utcnow())
                if _clog:
                    _clog.failure(result.get("error", ""))
                with _job_lock:
                    _job["files"][idx]["status"]     = "failed"
                    _job["files"][idx]["ffmpeg_cmd"] = ffmpeg_cmd
                    _job["files"][idx]["error_tail"] = error_tail
                    _job["files"][idx]["conv_secs"]  = _conv_secs
                    _job["files"][idx]["log_dir"]    = str(_clog.run_dir) if _clog else ""
                _job_log(f"Failed: {result.get('error', 'unknown')}")
                # Check if any logged line indicates a fatal device error
                if any("device error" in line.lower() or "i/o device" in line.lower()
                       for line in _file_log[-20:]):
                    _job_log("FATAL DEVICE ERROR detected in ffmpeg output — halting queue.")
                    _stop_event.set()

        with _job_lock:
            _job["saved_mb"] = total_saved

    with _job_lock:
        # Use 'stopped' when the user requested cancellation so the UI can
        # distinguish an intentional stop from a completed queue.
        _stopped = _stop_event.is_set() or _soft_stop_event.is_set()
        _job["state"]        = "stopped" if _stopped else "done"
        _job["current_file"] = ""
        _job["progress_pct"] = _job["progress_pct"] if _stopped else 100.0
        _ffmpeg_pid[0]       = 0
        _job["phase"]     = ""
        _job["steps"]     = []
        _job["ffmpeg_status"] = ""
        _job["ffmpeg_status_at"] = 0.0
        _job["ocr_batch"] = {"total": 0, "done": 0, "current_file": "", "files": []}

_SETTINGS_PATH = os.path.join(os.path.dirname(__file__), "settings.json")

_SETTINGS_DEFAULTS = {
    "qsv_quality":               config.QSV_QUALITY,
    "sw_hevc_crf":               config.SW_HEVC_CRF,
    "local_temp_dir":            config.LOCAL_TEMP_DIR,
    "keep_failed_intermediates": config.KEEP_FAILED_INTERMEDIATES,
    "default_sort":              "bitrate",   # bitrate | size | name
    "anime_mode":                False,
    "low_savings_threshold_pct": 5,
}

def _load_settings() -> dict:
    if os.path.exists(_SETTINGS_PATH):
        try:
            with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Merge with defaults so new keys are always present
            return {**_SETTINGS_DEFAULTS, **data}
        except Exception:
            pass
    return dict(_SETTINGS_DEFAULTS)

def _save_settings(data: dict):
    merged = {**_SETTINGS_DEFAULTS, **data}
    with open(_SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SYSTEM_FOLDERS = {
    "system volume information", "$recycle.bin", "$windows.~bt", "$windows.~ws",
    "windows", "windows.old", "program files", "program files (x86)", "programdata",
    "recovery", "boot", "perflogs", "msocache",
}

# Folder names that were used by legacy pipelines and should be unwound.
_LEGACY_FOLDERS = {"converted", "hevc", "failed"}


def _resolve_cleanup_dest(src_path: str) -> str | None:
    """
    If src_path sits inside a folder named 'converted' or 'hevc', return
    the path it should be moved to (up to two levels up through legacy folders).
    Returns None if the file is not in a legacy folder.
    """
    parent = os.path.dirname(src_path)
    if os.path.basename(parent).lower() not in _LEGACY_FOLDERS:
        return None
    dest_dir = os.path.dirname(parent)          # move up once
    if os.path.basename(dest_dir).lower() in _LEGACY_FOLDERS:
        dest_dir = os.path.dirname(dest_dir)    # move up twice
    return os.path.join(dest_dir, os.path.basename(src_path))


def _cleanup_legacy_folders(root_path: str) -> dict:
    """
    Walk root_path recursively.  Any file found directly inside a folder named
    'converted', 'hevc', or 'failed' is moved up (at most two levels) so it
    sits beside its former container.  Files rescued from a 'failed' folder
    also have their DB records deleted so the next scan retries them cleanly.
    Empty legacy folders are removed after the walk.

    Returns {"moved": [...], "skipped": [...], "errors": [...]}
    """
    moved, skipped, errors, renamed = [], [], [], []
    legacy_dirs: list[str] = []

    for dirpath, dirnames, filenames in os.walk(root_path, topdown=False):
        if os.path.basename(dirpath).lower() not in _LEGACY_FOLDERS:
            continue
        legacy_dirs.append(dirpath)
        for filename in filenames:
            src = os.path.join(dirpath, filename)
            dest = _resolve_cleanup_dest(src)
            if dest is None:
                continue
            if os.path.exists(dest):
                skipped.append({
                    "path": src.replace("\\", "/"),
                    "reason": "destination already exists",
                })
                continue
            try:
                shutil.move(src, dest)
                moved.append({
                    "from": src.replace("\\", "/"),
                    "to":   dest.replace("\\", "/"),
                })
                # Keep DB paths aligned with filesystem after cleanup moves.
                db.move_path(src, dest)
                # Files rescued from a failed/ folder need their DB record cleared
                # so the next scan treats them as fresh candidates.
                if os.path.basename(os.path.dirname(src)).lower() == "failed":
                    db.delete_records_by_path(src)
                    db.delete_records_by_path(dest)
            except Exception as exc:
                errors.append({
                    "path":   src.replace("\\", "/"),
                    "reason": str(exc),
                })

    # Remove any legacy dirs that are now empty (deepest first — topdown=False order)
    for d in legacy_dirs:
        try:
            if os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)
        except Exception:
            pass

    # Rename partial-download files (.!qB suffix left by qBittorrent)
    for dirpath, _dns, filenames in os.walk(root_path):
        for filename in filenames:
            if not filename.lower().endswith(".!qb"):
                continue
            src = os.path.join(dirpath, filename)
            dest = os.path.join(dirpath, filename[:-4])
            if os.path.exists(dest):
                skipped.append({"path": src.replace("\\", "/"), "reason": "destination already exists"})
                continue
            try:
                os.rename(src, dest)
                renamed.append({"from": src.replace("\\", "/"), "to": dest.replace("\\", "/")})
            except Exception as exc:
                errors.append({"path": src.replace("\\", "/"), "reason": str(exc)})

    return {"moved": moved, "skipped": skipped, "errors": errors, "renamed": renamed}


def _cleanup_legacy_folders_stream(root_path: str):
    """Generator version of cleanup — yields SSE-ready progress dicts.

    Events:
      {"type": "scan_done", "total": N}
      {"type": "progress",  "done": N, "total": N, "name": "<filename>"}
      {"type": "done",      "moved": N, "skipped": N, "errors": N}
    """
    # Phase 1: fast walk to collect all candidates first (gives us a total)
    # Each entry is (src, dest, is_rename) where is_rename=True for .!qB strips.
    candidates: list[tuple[str, str, bool]] = []
    legacy_dirs: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(root_path, topdown=False):
        if os.path.basename(dirpath).lower() not in _LEGACY_FOLDERS:
            continue
        legacy_dirs.append(dirpath)
        for filename in filenames:
            src = os.path.join(dirpath, filename)
            dest = _resolve_cleanup_dest(src)
            if dest is not None:
                candidates.append((src, dest, False))

    # Collect .!qB partial-download renames
    for dirpath, _dirnames, filenames in os.walk(root_path):
        for filename in filenames:
            if filename.lower().endswith(".!qb"):
                src = os.path.join(dirpath, filename)
                dest = os.path.join(dirpath, filename[:-4])
                candidates.append((src, dest, True))

    total = len(candidates)
    yield {"type": "scan_done", "total": total}

    # Phase 2: move/rename files one by one, yielding progress before each
    moved_n = skipped_n = errors_n = renamed_n = 0
    for i, (src, dest, is_rename) in enumerate(candidates):
        yield {"type": "progress", "done": i, "total": total, "name": os.path.basename(src)}
        if os.path.exists(dest):
            skipped_n += 1
            continue
        try:
            shutil.move(src, dest)
            if is_rename:
                db.move_path(src, dest)
                renamed_n += 1
            else:
                db.move_path(src, dest)
                moved_n += 1
                if os.path.basename(os.path.dirname(src)).lower() == "failed":
                    db.delete_records_by_path(src)
                    db.delete_records_by_path(dest)
        except Exception:
            errors_n += 1

    # Remove now-empty legacy dirs
    for d in legacy_dirs:
        try:
            if os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)
        except Exception:
            pass

    yield {"type": "done", "moved": moved_n, "skipped": skipped_n, "errors": errors_n, "renamed": renamed_n}


def _volume_label(drive: str) -> str:
    """Return e.g. 'C:/ (Local Disk)' or just 'C:/' if no label."""
    buf = ctypes.create_unicode_buffer(256)
    try:
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            drive, buf, len(buf), None, None, None, None, 0
        )
        if ok and buf.value:
            return f"{drive} ({buf.value})"
    except Exception:
        pass
    return drive


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", app_version=config.APP_VERSION)


@app.route("/api/browse")
def api_browse():
    req_path = request.args.get("path", "").strip()

    def _safe_isdir(path: str) -> bool:
        try:
            return os.path.isdir(path)
        except OSError:
            return False

    def _safe_exists(path: str) -> bool:
        try:
            return os.path.exists(path)
        except OSError:
            return False

    if req_path:
        req_path = os.path.normpath(req_path)
        # Walk up the tree until we find an existing directory (handles deleted remembered paths)
        while req_path and not _safe_isdir(req_path):
            parent = os.path.dirname(req_path)
            if parent == req_path:
                req_path = ""  # drive root doesn't exist — fall back to drive listing
                break
            req_path = parent

    # Root — list available drives
    if not req_path:
        drives = []
        for letter in string.ascii_uppercase:
            drive = letter + ":/"
            if _safe_exists(drive):
                drives.append({
                    "name": _volume_label(drive),
                    "full_path": drive,
                    "has_children": True,
                })
        return jsonify({"path": "", "parent": None, "dirs": drives})

    parent = os.path.dirname(req_path)
    if parent == req_path:
        parent = None  # at drive root

    dirs = []
    try:
        for entry in sorted(os.scandir(req_path), key=lambda e: e.name.lower()):
            if not entry.is_dir(follow_symlinks=False):
                continue
            if entry.name.startswith("."):
                continue
            if entry.name.lower() in _SYSTEM_FOLDERS:
                continue
            try:
                attrs = entry.stat(follow_symlinks=False).st_file_attributes
                if attrs & 0x2 or attrs & 0x4:  # HIDDEN | SYSTEM
                    continue
            except Exception:
                pass
            try:
                has_children = any(
                    e.is_dir(follow_symlinks=False) and e.name.lower() not in _SYSTEM_FOLDERS
                    for e in os.scandir(entry.path)
                )
            except PermissionError:
                has_children = False
            dirs.append({
                "name": entry.name,
                "full_path": entry.path.replace("\\", "/"),
                "has_children": has_children,
            })
    except (PermissionError, OSError):
        pass

    return jsonify({
        "path": req_path.replace("\\", "/"),
        "parent": parent.replace("\\", "/") if parent else None,
        "dirs": dirs,
    })


@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    return jsonify(_load_settings())

@app.route("/api/settings", methods=["POST"])
def api_settings_post():
    data = request.get_json(force=True, silent=True) or {}
    # Validate numeric fields
    try:
        if "qsv_quality" in data:
            data["qsv_quality"] = max(1, min(51, int(data["qsv_quality"])))
        if "sw_hevc_crf" in data:
            data["sw_hevc_crf"] = max(0, min(51, int(data["sw_hevc_crf"])))
        if "low_savings_threshold_pct" in data:
            data["low_savings_threshold_pct"] = max(0, min(100, int(data["low_savings_threshold_pct"])))
        if "keep_failed_intermediates" in data:
            data["keep_failed_intermediates"] = bool(data["keep_failed_intermediates"])
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid numeric value"}), 400
    _save_settings({**_load_settings(), **data})
    return jsonify({"ok": True})


@app.route("/api/estimate")
def api_estimate():
    """Run a 10-second sample encode and return an estimated compression ratio."""
    path = request.args.get("path", "").strip()
    if not path:
        return jsonify({"error": "No path provided"}), 400
    path = os.path.normpath(path)
    settings = _load_settings()
    result = converter.estimate(path, quality=settings.get("qsv_quality"))
    return jsonify(result)


@app.route("/api/start", methods=["POST"])
def api_start():
    """Begin processing a file queue in a background thread."""
    with _job_lock:
        if _job["state"] == "running":
            return jsonify({"error": "A job is already running"}), 409

    data      = request.get_json(force=True, silent=True) or {}
    files     = data.get("files", [])
    anime     = bool(data.get("anime_mode", False))
    estimate_only = bool(data.get("estimate_only", False))
    force_reestimate = bool(data.get("force_reestimate", False))
    settings  = _load_settings()
    quality   = int(settings.get("qsv_quality", config.QSV_QUALITY))
    threshold = int(settings.get("low_savings_threshold_pct", 5))
    # Apply local_temp_dir so ffmpeg staging uses the user-configured path
    temp_dir  = settings.get("local_temp_dir", "").strip()
    if temp_dir:
        config.LOCAL_TEMP_DIR = temp_dir
    config.KEEP_FAILED_INTERMEDIATES = bool(settings.get("keep_failed_intermediates", config.KEEP_FAILED_INTERMEDIATES))

    if not files:
        return jsonify({"error": "No files provided"}), 400

    global _session_started_at
    _session_started_at = time.time()
    _stop_event.clear()
    _soft_stop_event.clear()
    with _job_lock:
        _job.update({
            "state":         "running",
            "current_index": 0,
            "total":         len(files),
            "current_file":  "",
            "progress_pct":  0.0,
            "fps":           0.0,
            "eta_secs":      0,
            "saved_mb":      0.0,
            "encoder":       "",
            "files":         [dict(f) for f in files],
            "log":           [],
            "paused":        False,
            "phase":         "",
            "ocr_batch":     {"total": 0, "done": 0, "current_file": "", "files": []},
            "steps":         [],
            "ffmpeg_status": "",
            "ffmpeg_status_at": 0.0,
        })

    def _worker_safe():
        try:
            _queue_worker(
                list(files),
                anime,
                quality,
                threshold,
                estimate_only,
                force_reestimate,
            )
        except Exception as exc:
            import traceback
            tb = traceback.format_exc()
            _job_log(f"Worker crashed unexpectedly: {exc}\n{tb}")
            _write_crash_log(f"Worker crashed unexpectedly: {exc}\n{tb}")
            with _job_lock:
                _job["state"]        = "done"
                _job["current_file"] = ""
                _ffmpeg_pid[0]       = 0

    t = threading.Thread(
        target=_worker_safe,
        daemon=True,
    )
    t.start()
    return jsonify({"ok": True})


@app.route("/api/status")
def api_status():
    """Return current job state."""
    with _job_lock:
        data = dict(_job)
    data["session_started_at"] = _session_started_at
    data.update(_status_savings_fields(data))
    return jsonify(data)


@app.route("/api/stop", methods=["POST"])
def api_stop():
    """Signal the queue worker to stop after the current file completes (soft stop)."""
    _soft_stop_event.set()
    _resume_ffmpeg()   # unblock if paused so the current file can finish
    with _job_lock:
        _job["paused"] = False
    return jsonify({"ok": True})


@app.route("/api/hardstop", methods=["POST"])
def api_hardstop():
    """Immediately kill the ffmpeg process and stop the queue."""
    _stop_event.set()
    _resume_ffmpeg()   # unblock if paused first, then kill
    _kill_ffmpeg()
    with _job_lock:
        _job["paused"] = False
    return jsonify({"ok": True})


@app.route("/api/pause", methods=["POST"])
def api_pause():
    """Suspend the ffmpeg child process."""
    _suspend_ffmpeg()
    with _job_lock:
        _job["paused"] = True
    return jsonify({"ok": True})


@app.route("/api/resume", methods=["POST"])
def api_resume():
    """Resume a previously suspended ffmpeg process."""
    _resume_ffmpeg()
    with _job_lock:
        _job["paused"] = False
    return jsonify({"ok": True})


@app.route("/api/scan")
def api_scan():
    """Stream a recursive directory scan as Server-Sent Events."""
    path = request.args.get("path", "").strip()
    if not path or not os.path.isdir(path):
        return jsonify({"error": "Invalid path"}), 400

    def _generate():
        try:
            for event in scanner.walk(path):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as exc:
            import traceback
            app.logger.error("Scanner error: %s\n%s", exc, traceback.format_exc())
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"

    return Response(
        stream_with_context(_generate()),
        mimetype="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@app.route("/api/cleanup", methods=["POST"])
def api_cleanup():
    """Move files out of legacy 'converted'/'hevc' subfolders.

    Body: { "path": "<root directory to clean up>" }
    Returns: { "moved": [...], "skipped": [...], "errors": [...] }
    """
    data = request.get_json(force=True, silent=True) or {}
    path = data.get("path", "").strip()
    if not path:
        return jsonify({"error": "No path provided"}), 400
    path = os.path.normpath(path)
    if not os.path.isdir(path):
        return jsonify({"error": "Not a directory"}), 400
    result = _cleanup_legacy_folders(path)
    return jsonify(result)


@app.route("/api/cleanup_stream")
def api_cleanup_stream():
    """Stream cleanup progress as Server-Sent Events.

    Query param: path=<root directory to clean up>
    Emits: scan_done → progress (×N) → done
    """
    path = request.args.get("path", "").strip()
    if not path:
        return jsonify({"error": "No path provided"}), 400
    path = os.path.normpath(path)
    if not os.path.isdir(path):
        return jsonify({"error": "Not a directory"}), 400

    def _generate():
        try:
            for event in _cleanup_legacy_folders_stream(path):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"

    return Response(
        stream_with_context(_generate()),
        mimetype="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@app.route("/api/open")
def api_open():
    """Open a file in its default app, or reveal it in Explorer."""
    path    = request.args.get("path", "").strip()
    action  = request.args.get("action", "play")   # play | folder

    if not path:
        return jsonify({"error": "No path provided"}), 400

    path = os.path.normpath(path)
    if not os.path.exists(path):
        return jsonify({"error": "Path not found"}), 404

    try:
        if action == "folder":
            if os.path.isfile(path):
                import subprocess
                subprocess.Popen(["explorer", "/select,", path])
            else:
                os.startfile(path)
        else:
            os.startfile(path)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/trash", methods=["POST"])
def api_trash():
    """Move a source file to the Recycle Bin and remove its DB record."""
    data = request.get_json(force=True, silent=True) or {}
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify({"error": "No path provided"}), 400

    path = os.path.normpath(path)
    if not os.path.isfile(path):
        return jsonify({"error": "File not found"}), 404

    try:
        import send2trash
        send2trash.send2trash(path)
    except Exception as e:
        return jsonify({"error": f"Recycle bin error: {e}"}), 500

    # Remove DB record so the file won't reappear on next scan
    db.delete_records_by_path(path.replace("\\", "/"))

    return jsonify({"ok": True})


@app.route("/api/update_status", methods=["POST"])
def api_update_status():
    """Manually set status and/or force flags of one or more files."""
    data  = request.get_json(force=True) or {}
    paths = data.get("paths", [])
    new_status = data.get("status", "")
    force_sw   = data.get("force_sw", None)  # None = don't change it
    force_convert = data.get("force_convert", None)  # None = don't change it

    ALLOWED = {"skipped", "pending", "failed"}
    if new_status and new_status not in ALLOWED:
        return jsonify({"error": f"status must be one of {ALLOWED}"}), 400
    if not paths:
        return jsonify({"error": "No paths provided"}), 400
    if not new_status and force_sw is None and force_convert is None:
        return jsonify({"error": "Provide at least one of: status, force_sw, force_convert"}), 400

    updated = 0
    with db._connect() as con:
        cur = con.cursor()
        for path in paths:
            norm = path.replace("\\", "/")
            if new_status:
                if new_status == "pending":
                    cur.execute(
                        """
                        UPDATE conversions
                           SET status='pending',
                               started_at=NULL,
                               completed_at=NULL,
                               error_tail=NULL,
                               output_path=NULL,
                               output_size_mb=NULL,
                               output_hash=NULL,
                               output_bitrate_kbps=NULL,
                               saved_mb=NULL,
                               saved_pct=NULL
                         WHERE source_path=?
                        """,
                        (norm,),
                    )
                else:
                    cur.execute(
                        "UPDATE conversions SET status=?, started_at=NULL, completed_at=NULL, error_tail=NULL "
                        "WHERE source_path=?",
                        (new_status, norm),
                    )
                updated += cur.rowcount
            if force_sw is not None:
                cur.execute(
                    "UPDATE conversions SET force_sw=? WHERE source_path=?",
                    (1 if force_sw else 0, norm),
                )
                updated += cur.rowcount
            if force_convert is not None:
                cur.execute(
                    "UPDATE conversions SET force_convert=? WHERE source_path=?",
                    (1 if force_convert else 0, norm),
                )
                updated += cur.rowcount
    return jsonify({"ok": True, "updated": updated})


@app.route("/api/drop_streams", methods=["POST"])
def api_drop_streams():
    """Set (or clear) the list of dropped ffprobe stream indices for a file.

    Body: { "path": "<source_path>", "dropped": [5, 7] }
    Pass an empty list to un-drop all streams.
    """
    data = request.get_json(force=True, silent=True) or {}
    path = data.get("path", "").strip()
    dropped = data.get("dropped", [])

    if not path:
        return jsonify({"error": "No path provided"}), 400
    if not isinstance(dropped, list):
        return jsonify({"error": "dropped must be a list of integers"}), 400

    norm = path.replace("\\", "/")
    db.set_dropped_streams(norm, [int(i) for i in dropped])
    return jsonify({"ok": True, "path": norm, "dropped": sorted(set(int(i) for i in dropped))})


@app.route("/api/drop_pgs_bulk", methods=["POST"])
def api_drop_pgs_bulk():
    """Bulk-set dropped_streams for multiple files at once.

    Body: { "updates": [{"path": "...", "dropped": [5, 7]}, ...] }
    Merges provided indices with any already-dropped streams for each file.
    Returns: { "ok": true, "updated": { path: [indices] } }
    """
    data = request.get_json(force=True, silent=True) or {}
    updates = data.get("updates", [])
    if not updates or not isinstance(updates, list):
        return jsonify({"error": "updates must be a non-empty list"}), 400

    result = {}
    for u in updates:
        path = (u.get("path") or "").strip()
        dropped = u.get("dropped", [])
        if not path or not isinstance(dropped, list):
            continue
        norm = path.replace("\\", "/")
        merged = sorted(set(int(i) for i in dropped))
        db.set_dropped_streams(norm, merged)
        result[norm] = merged

    return jsonify({"ok": True, "updated": result})


@app.route("/api/probe_streams")
def api_probe_streams():
    """Run ffprobe on a file and return stream info in the same shape as the scanner."""
    path = request.args.get("path", "").strip()
    if not path:
        return jsonify({"error": "No path provided"}), 400
    path = os.path.normpath(path)
    if not os.path.isfile(path):
        return jsonify({"error": "File not found"}), 404
    probe = scanner._ffprobe(path)
    if not probe:
        return jsonify({"error": "ffprobe failed"}), 500
    parsed = scanner._parse_probe(probe)
    return jsonify({"ok": True, "streams": parsed["streams"]})


@app.route("/api/subtitle_preview")
def api_subtitle_preview():
    """Extract a short subtitle text preview and detect likely language/script."""
    path = request.args.get("path", "").strip()
    stream_index_raw = request.args.get("stream_index", "").strip()
    metadata_language = request.args.get("metadata_language", "").strip()

    if not path:
        return jsonify({"error": "No path provided"}), 400
    path = os.path.normpath(path)
    if not os.path.isfile(path):
        return jsonify({"error": "File not found"}), 404

    try:
        stream_index = int(stream_index_raw)
    except Exception:
        return jsonify({"error": "Invalid stream_index"}), 400

    try:
        max_lines = int(request.args.get("max_lines", 120))
    except Exception:
        max_lines = 120
    max_lines = max(20, min(300, max_lines))

    probe = scanner._ffprobe(path)
    if not probe:
        return jsonify({"error": "ffprobe failed"}), 500

    stream = None
    for s in probe.get("streams", []):
        if int(s.get("index", -1)) == stream_index:
            stream = s
            break

    if not stream or (stream.get("codec_type") or "").lower() != "subtitle":
        return jsonify({"error": "Subtitle stream not found"}), 404

    codec_name = (stream.get("codec_name") or "").lower()
    if codec_name in {"hdmv_pgs_subtitle", "pgssub", "dvd_subtitle", "vobsub", "xsub"}:
        return jsonify({
            "error": "Image-based subtitle track cannot be previewed as text without OCR.",
            "is_text_subtitle": False,
        }), 422

    cmd = [
        "ffmpeg", "-v", "error",
        "-i", path,
        "-map", f"0:{stream_index}",
        "-f", "srt", "-",
    ]

    try:
        proc = _sp.run(cmd, capture_output=True, timeout=40)
    except Exception as exc:
        return jsonify({"error": f"ffmpeg launch failed: {exc}"}), 500

    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()
        tail = "\n".join(err.splitlines()[-25:]) if err else "subtitle extraction failed"
        return jsonify({"error": tail}), 500

    text = (proc.stdout or b"").decode("utf-8", "replace")
    lines = text.splitlines()
    if len(lines) > max_lines:
        preview_text = "\n".join(lines[:max_lines]) + "\n\n[Preview truncated]"
    else:
        preview_text = text

    dialogue_text = _subtitle_payload_text(text)
    detected = _detect_subtitle_language(dialogue_text)

    stream_lang = ((stream.get("tags") or {}).get("language") or "")
    metadata_norm = _normalise_lang_code(metadata_language or stream_lang)
    detected_norm = _normalise_lang_code(detected.get("code", "und"))
    mismatch = (
        metadata_norm != "und"
        and detected_norm not in {"und", "latin"}
        and metadata_norm != detected_norm
    )

    return jsonify({
        "ok": True,
        "path": path,
        "stream_index": stream_index,
        "codec": codec_name,
        "preview_text": preview_text,
        "preview_line_count": len(lines),
        "metadata_language": metadata_norm,
        "detected_language": detected,
        "language_mismatch": mismatch,
        "is_text_subtitle": True,
    })


@app.route("/api/stream_edit_status")
def api_stream_edit_status():
    """Return whether a stream-edit preview exists for the given source file."""
    path = request.args.get("path", "").strip()
    if not path:
        return jsonify({"error": "No path provided"}), 400
    path = os.path.normpath(path)
    if not os.path.isfile(path):
        return jsonify({"error": "File not found"}), 404

    preview = _stream_preview_path(path)
    exists = os.path.isfile(preview)
    size_mb = round(os.path.getsize(preview) / (1024 * 1024), 2) if exists else 0.0
    return jsonify({
        "ok": True,
        "path": path,
        "preview_path": preview if exists else "",
        "preview_exists": exists,
        "preview_size_mb": size_mb,
    })


@app.route("/api/stream_edit_preview", methods=["POST"])
def api_stream_edit_preview():
    """Create/overwrite a stream-copy preview that excludes selected stream indices."""
    if _is_job_running():
        return jsonify({"error": "Cannot edit streams while conversion is running"}), 409

    data = request.get_json(force=True, silent=True) or {}
    path = (data.get("path") or "").strip()
    dropped = data.get("dropped", None)

    if not path:
        return jsonify({"error": "No path provided"}), 400
    path = os.path.normpath(path)
    if not os.path.isfile(path):
        return jsonify({"error": "File not found"}), 404

    if dropped is None:
        dropped_indices = db.get_dropped_streams(path)
    elif isinstance(dropped, list):
        try:
            dropped_indices = sorted(set(int(i) for i in dropped))
        except Exception:
            return jsonify({"error": "dropped must be a list of integers"}), 400
    else:
        return jsonify({"error": "dropped must be a list of integers"}), 400

    preview = _stream_preview_path(path)
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-i", path,
        "-map", "0",
    ]
    for idx in dropped_indices:
        cmd += ["-map", f"-0:{idx}"]
    cmd += ["-c", "copy", preview]

    try:
        proc = _sp.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60 * 60,
        )
    except Exception as exc:
        return jsonify({"error": f"ffmpeg launch failed: {exc}"}), 500

    if proc.returncode != 0 or not os.path.isfile(preview):
        err = (proc.stderr or "").strip()
        tail = "\n".join(err.splitlines()[-25:]) if err else "stream-copy failed"
        return jsonify({"error": tail}), 500

    # Persist latest dropped-stream selection to DB for consistency.
    db.set_dropped_streams(path, dropped_indices)

    return jsonify({
        "ok": True,
        "path": path,
        "preview_path": preview,
        "preview_size_mb": round(os.path.getsize(preview) / (1024 * 1024), 2),
        "dropped": dropped_indices,
    })


@app.route("/api/stream_edit_commit", methods=["POST"])
def api_stream_edit_commit():
    """Replace source file with accepted stream-edit preview and remove the original."""
    if _is_job_running():
        return jsonify({"error": "Cannot edit streams while conversion is running"}), 409

    data = request.get_json(force=True, silent=True) or {}
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify({"error": "No path provided"}), 400

    path = os.path.normpath(path)
    if not os.path.isfile(path):
        return jsonify({"error": "File not found"}), 404

    preview = _stream_preview_path(path)
    if not os.path.isfile(preview):
        return jsonify({"error": "No preview copy exists for this file"}), 404

    try:
        os.replace(preview, path)
    except Exception as exc:
        return jsonify({"error": f"replace failed: {exc}"}), 500

    # Dropped streams are now baked into the source; clear the override list.
    db.set_dropped_streams(path, [])

    try:
        stat = os.stat(path)
        size_mb = round(stat.st_size / (1024 * 1024), 2)
        size_bytes = int(stat.st_size)
        mtime = stat.st_mtime
    except Exception:
        size_mb = 0.0
        size_bytes = 0
        mtime = time.time()

    # Sync DB metadata for the new file bytes. Preserve done status when the
    # previous record was already done so edited files don't re-queue as pending.
    status = "pending"
    try:
        probe = scanner._ffprobe(path)
        parsed = scanner._parse_probe(probe) if probe else {}
        codec = (parsed.get("codec") or "").upper() if parsed else ""
        bitrate = int(((parsed.get("streams") or {}).get("video") or {}).get("bitrate", 0) / 1000) if parsed else 0
        duration = float(parsed.get("duration_secs", 0.0)) if parsed else 0.0
        video_track_count = int(parsed.get("video_track_count", 0)) if parsed else 0
        audio_track_count = int(parsed.get("audio_track_count", 0)) if parsed else 0
        file_hash = db.hash_file_head(path)
        status = db.sync_after_stream_edit(
            source_path=path,
            source_mtime=mtime,
            source_size_bytes=size_bytes,
            source_size_mb=size_mb,
            source_codec=codec or None,
            source_bitrate_kbps=bitrate or None,
            source_duration_secs=duration or None,
            source_video_track_count=video_track_count,
            source_audio_track_count=audio_track_count,
            content_hash=file_hash,
            completed_at=_utcnow(),
        )
        streams = (parsed.get("streams") or None) if parsed else None
    except Exception:
        streams = None

    return jsonify({
        "ok": True,
        "path": path,
        "new_size_mb": size_mb,
        "status": status,
        "streams": streams,
    })


@app.route("/api/stream_edit_discard", methods=["POST"])
def api_stream_edit_discard():
    """Delete a previously created stream-edit preview copy for a file."""
    data = request.get_json(force=True, silent=True) or {}
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify({"error": "No path provided"}), 400

    path = os.path.normpath(path)
    preview = _stream_preview_path(path)
    if os.path.isfile(preview):
        try:
            os.remove(preview)
        except Exception as exc:
            return jsonify({"error": f"Could not remove preview: {exc}"}), 500

    return jsonify({"ok": True, "path": path})


@app.route("/api/eng_stereo_status")
def api_eng_stereo_status():
    """Return whether an English-stereo preview exists for the given source file."""
    path = request.args.get("path", "").strip()
    if not path:
        return jsonify({"error": "No path provided"}), 400
    path = os.path.normpath(path)
    if not os.path.isfile(path):
        return jsonify({"error": "File not found"}), 404

    preview = _eng_stereo_preview_path(path)
    exists = os.path.isfile(preview)
    size_mb = round(os.path.getsize(preview) / (1024 * 1024), 2) if exists else 0.0
    return jsonify({
        "ok": True,
        "path": path,
        "preview_path": preview if exists else "",
        "preview_exists": exists,
        "preview_size_mb": size_mb,
    })


@app.route("/api/eng_stereo_preview", methods=["POST"])
def api_eng_stereo_preview():
    """Create/overwrite an English-only AAC stereo preview copy for test playback."""
    if _is_job_running():
        return jsonify({"error": "Cannot create preview while conversion is running"}), 409

    data = request.get_json(force=True, silent=True) or {}
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify({"error": "No path provided"}), 400

    path = os.path.normpath(path)
    if not os.path.isfile(path):
        return jsonify({"error": "File not found"}), 404

    eng_index = _first_audio_stream_index_for_language(path, "eng")
    if eng_index is None:
        return jsonify({"error": "No English audio track found"}), 400

    preview = _eng_stereo_preview_path(path)
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-i", path,
        "-map", "0:v",
        "-map", f"0:{eng_index}",
        "-map", "0:s?",
        "-map", "0:t?",
        "-c:v", "copy",
        "-c:s", "copy",
        "-c:t", "copy",
        "-c:a", "aac",
        "-ac", "2",
        "-b:a", "192k",
        "-disposition:a:0", "default",
        "-metadata:s:a:0", "language=eng",
        preview,
    ]

    try:
        proc = _sp.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60 * 60,
        )
    except Exception as exc:
        return jsonify({"error": f"ffmpeg launch failed: {exc}"}), 500

    if proc.returncode != 0 or not os.path.isfile(preview):
        err = (proc.stderr or "").strip()
        tail = "\n".join(err.splitlines()[-25:]) if err else "english stereo build failed"
        return jsonify({"error": tail}), 500

    return jsonify({
        "ok": True,
        "path": path,
        "preview_path": preview,
        "preview_size_mb": round(os.path.getsize(preview) / (1024 * 1024), 2),
    })


@app.route("/api/eng_stereo_commit", methods=["POST"])
def api_eng_stereo_commit():
    """Replace source file with accepted English-stereo preview, preserving a backup."""
    if _is_job_running():
        return jsonify({"error": "Cannot replace while conversion is running"}), 409

    data = request.get_json(force=True, silent=True) or {}
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify({"error": "No path provided"}), 400

    path = os.path.normpath(path)
    if not os.path.isfile(path):
        return jsonify({"error": "File not found"}), 404

    preview = _eng_stereo_preview_path(path)
    if not os.path.isfile(preview):
        return jsonify({"error": "No English-stereo preview exists for this file"}), 404

    backup = _original_backup_path(path)
    if os.path.exists(backup):
        root, ext = os.path.splitext(path)
        backup = f"{root}.original-backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}{ext}"

    try:
        shutil.copy2(path, backup)
    except Exception as exc:
        return jsonify({"error": f"backup failed: {exc}"}), 500

    try:
        os.replace(preview, path)
    except Exception as exc:
        return jsonify({"error": f"replace failed: {exc}"}), 500

    try:
        stat = os.stat(path)
        size_mb = round(stat.st_size / (1024 * 1024), 2)
        size_bytes = int(stat.st_size)
        mtime = stat.st_mtime
    except Exception:
        size_mb = 0.0
        size_bytes = 0
        mtime = time.time()

    status = "pending"
    try:
        probe = scanner._ffprobe(path)
        parsed = scanner._parse_probe(probe) if probe else {}
        codec = (parsed.get("codec") or "").upper() if parsed else ""
        bitrate = int(((parsed.get("streams") or {}).get("video") or {}).get("bitrate", 0) / 1000) if parsed else 0
        duration = float(parsed.get("duration_secs", 0.0)) if parsed else 0.0
        video_track_count = int(parsed.get("video_track_count", 0)) if parsed else 0
        audio_track_count = int(parsed.get("audio_track_count", 0)) if parsed else 0
        file_hash = db.hash_file_head(path)
        status = db.sync_after_stream_edit(
            source_path=path,
            source_mtime=mtime,
            source_size_bytes=size_bytes,
            source_size_mb=size_mb,
            source_codec=codec or None,
            source_bitrate_kbps=bitrate or None,
            source_duration_secs=duration or None,
            source_video_track_count=video_track_count,
            source_audio_track_count=audio_track_count,
            content_hash=file_hash,
            completed_at=_utcnow(),
        )
        streams = (parsed.get("streams") or None) if parsed else None
    except Exception:
        streams = None

    return jsonify({
        "ok": True,
        "path": path,
        "backup_path": backup,
        "new_size_mb": size_mb,
        "status": status,
        "streams": streams,
    })


@app.route("/api/eng_stereo_discard", methods=["POST"])
def api_eng_stereo_discard():
    """Delete a previously created English-stereo preview copy for a file."""
    data = request.get_json(force=True, silent=True) or {}
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify({"error": "No path provided"}), 400

    path = os.path.normpath(path)
    preview = _eng_stereo_preview_path(path)
    if os.path.isfile(preview):
        try:
            os.remove(preview)
        except Exception as exc:
            return jsonify({"error": f"Could not remove preview: {exc}"}), 500

    return jsonify({"ok": True, "path": path})


@app.route("/api/diagnose")
def api_diagnose():
    """Run ffprobe and return a structured diagnostic report for a file."""
    path = request.args.get("path", "").strip()
    if not path:
        return jsonify({"error": "No path provided"}), 400
    path = os.path.normpath(path)
    if not os.path.exists(path):
        return jsonify({"error": "File not found"}), 404

    import subprocess as _sp
    from pathlib import Path as _Path

    report: dict = {"path": path, "sections": []}

    def _section(title: str, lines: list) -> None:
        report["sections"].append({"title": title, "lines": lines})

    # 1. Basic file info
    stat = os.stat(path)
    size_mb = stat.st_size / 1024 / 1024
    _section("File", [
        f"Path   : {path}",
        f"Size   : {size_mb:.1f} MB ({stat.st_size:,} bytes)",
        f"Ext    : {_Path(path).suffix.lower()}",
    ])

    # 2. ffprobe streams
    streams = []
    duration = 0.0
    br_kbps = 0
    try:
        probe = _sp.run(
            ["ffprobe", "-v", "quiet",
             "-probesize", "100M", "-analyzeduration", "100M",
             "-print_format", "json", "-show_streams", "-show_format", path],
            capture_output=True, text=True, timeout=60,
        )
        pdata    = json.loads(probe.stdout)
        streams  = pdata.get("streams", [])
        fmt      = pdata.get("format", {})
        duration = float(fmt.get("duration") or 0)
        br_kbps  = int(fmt.get("bit_rate") or 0) // 1000
        stream_lines = [
            f"Duration : {duration:.1f}s  |  Bitrate: {br_kbps} kbps  |  Format: {fmt.get('format_name', '')}"
        ]
        for s in streams:
            idx   = s.get("index", "?")
            ctype = s.get("codec_type", "?")
            cname = s.get("codec_name", "?")
            lang  = s.get("tags", {}).get("language", "")
            title = s.get("tags", {}).get("title", "")
            extra = ""
            if ctype == "video":
                extra = f"  {s.get('width','')}x{s.get('height','')}  {s.get('pix_fmt','')}  {s.get('r_frame_rate','')}"
            elif ctype == "audio":
                extra = f"  {s.get('sample_rate','')} Hz  {s.get('channel_layout','')}  {int(s.get('bit_rate') or 0) // 1000} kbps"
            elif ctype == "subtitle":
                extra = "  (default)" if s.get("disposition", {}).get("default") else ""
            meta = (f"  lang={lang}" if lang else "") + (f"  title={title!r}" if title else "")
            stream_lines.append(f"  #{idx} {ctype:9} {cname:25}{meta}{extra}")
        _section("Streams", stream_lines)
    except Exception as e:
        _section("Streams", [f"ERROR: {e}"])

    # 3. Sidecar subtitle files
    _SIDECAR_EXTS = {".srt", ".ass", ".ssa"}
    src = _Path(path)
    sidecars = [p for ext in _SIDECAR_EXTS for p in [src.parent / (src.stem + ext)] if p.exists()]
    if sidecars:
        _section("Sidecar subtitles", [f"  {p.name}  ({p.stat().st_size // 1024} KB)" for p in sidecars])
    else:
        _section("Sidecar subtitles", ["  (none)"])

    # 4. DB record
    norm = path.replace("\\", "/")
    with db._connect() as con:
        row = con.execute(
            "SELECT id, status, force_sw, error_tail, started_at, completed_at, output_path "
            "FROM conversions WHERE source_path=?",
            (norm,),
        ).fetchone()
    if row:
        db_lines = [
            f"  id={row['id']}  status={row['status']}  force_sw={bool(row['force_sw'])}",
            f"  started={row['started_at'] or '—'}  completed={row['completed_at'] or '—'}",
            f"  output={row['output_path'] or '—'}",
        ]
        if row["error_tail"]:
            db_lines.append("  error_tail:")
            for el in row["error_tail"].splitlines()[-8:]:
                db_lines.append(f"    {el}")
        _section("Database record", db_lines)
    else:
        _section("Database record", ["  (no record found)"])

    # 5. Log files (most recent run)
    log_base = os.path.join(os.path.dirname(__file__), "logs")
    stem_lower = src.stem.lower()
    log_dirs = []
    if os.path.isdir(log_base):
        log_dirs = sorted(
            [d for d in os.scandir(log_base) if d.is_dir() and stem_lower in d.name.lower()],
            key=lambda d: d.name,
        )
    if log_dirs:
        log_dir = log_dirs[-1]
        log_lines = [f"  Directory: {log_dir.name}", ""]
        for lf in sorted(os.scandir(log_dir.path), key=lambda f: f.name):
            if lf.is_file() and lf.name.endswith(".log"):
                kb = lf.stat().st_size // 1024
                log_lines.append(f"  ┌── {lf.name}  ({kb} KB)")
                try:
                    with open(lf.path, "r", encoding="utf-8", errors="replace") as fh:
                        content = fh.read()
                    for ln in content.splitlines():
                        log_lines.append("  │ " + ln)
                except Exception as e:
                    log_lines.append(f"  │ (could not read: {e})")
                log_lines.append("  └──")
                log_lines.append("")
        _section("Log files (most recent run)", log_lines)
    else:
        _section("Log files (most recent run)", ["  (none found)"])

    # 6. Conversion notes
    v_streams   = [s for s in streams if s.get("codec_type") == "video"]
    a_streams   = [s for s in streams if s.get("codec_type") == "audio"]
    s_streams   = [s for s in streams if s.get("codec_type") == "subtitle"]
    pgs_codecs  = {"hdmv_pgs_subtitle", "pgssub"}
    text_codecs = {"ass", "subrip", "srt", "webvtt", "mov_text"}
    has_pgs     = any(s.get("codec_name", "").lower() in pgs_codecs for s in s_streams)
    is_hi10     = any(s.get("pix_fmt", "").lower() in ("yuv420p10le", "yuv420p10be") for s in v_streams)
    is_mp4      = src.suffix.lower() == ".mp4"
    all_aac     = all(s.get("codec_name", "").lower() in ("aac", "aac_latm") for s in a_streams) if a_streams else False
    recs = []
    if is_hi10:
        recs.append("Hi10 video — QSV unsupported, will use libx265 software encoder")
    if has_pgs:
        recs.append("PGS bitmap subs — OCR required (Tesseract)")
    if any(s.get("codec_name", "").lower() in text_codecs for s in s_streams) and not is_mp4:
        recs.append("Text subs (ASS/SRT) — if typesetter track, attempt 4 (SRT timestamp spread) may be needed")
    if is_mp4 and all_aac and not has_pgs:
        if sidecars:
            recs.append("MP4 + AAC + no bitmap subs + sidecars -> fast-path: stream-copy + inject sidecars")
        else:
            recs.append("MP4 + AAC + no bitmap subs -> fast-path: compress_simple (no remux)")
    if sidecars:
        recs.append(f"Sidecar subs: {', '.join(p.name for p in sidecars)}")
    if duration > 0 and 0 < br_kbps < 2000:
        recs.append(f"Low bitrate ({br_kbps} kbps) — savings may be minimal or negative")
    _section("Conversion notes", recs if recs else ["  (nothing unusual)"])

    return jsonify(report)


@app.route("/api/analyse_folders")
def api_analyse_folders():
    """Analyse conversion opportunity grouped by folder."""
    import posixpath as _pp
    from datetime import datetime as _dt

    root = request.args.get("root", "").strip().rstrip("\\/")
    if not root:
        return jsonify({"error": "root parameter required"}), 400

    try:
        min_done    = max(0, int(request.args.get("min_done",    1)))
        min_pending = max(0, int(request.args.get("min_pending", 1)))
        top         = max(1, int(request.args.get("top",        50)))
    except ValueError:
        return jsonify({"error": "Invalid numeric parameter"}), 400
    sort_by = request.args.get("sort", "score")
    if sort_by not in ("score", "savings", "speed", "pending"):
        sort_by = "score"

    def _norm(p):
        return p.replace("\\", "/").lower()

    root_norm = _norm(root)
    prefix    = root_norm + "/"

    settings  = _load_settings()
    threshold = int(settings.get("low_savings_threshold_pct", 5))

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT c.id, c.source_path, c.status, c.saved_mb, c.saved_pct, c.source_size_mb,
               c.source_duration_secs, c.started_at, c.completed_at, c.est_saving_pct
        FROM conversions c
        INNER JOIN (
            SELECT source_path, MAX(id) AS max_id
            FROM conversions
            GROUP BY source_path
        ) latest ON c.id = latest.max_id
        """
    ).fetchall()
    con.close()

    _DONE    = "done"
    _PENDING = {"pending", "failed"}

    # Collect pending rows first so we can existence-check them in one pass
    # and purge stale records for files that no longer exist on disk.
    pending_under_root: list = []
    groups: dict = {}
    all_folders: set = set()

    for row in rows:
        path = row["source_path"]
        if not path:
            continue
        np = _norm(path)
        if not np.startswith(prefix):
            continue
        fld = _norm(os.path.dirname(path))
        all_folders.add(fld)
        if fld not in groups:
            groups[fld] = {"done": [], "pending": []}
        status = (row["status"] or "").lower()
        if status == _DONE:
            groups[fld]["done"].append(row)
        elif status in _PENDING:
            pending_under_root.append((fld, row))

    # Purge pending records for files that no longer exist on disk
    stale_ids: list[int] = []
    for fld, row in pending_under_root:
        native = row["source_path"].replace("/", os.sep)
        if not os.path.exists(native):
            stale_ids.append(row["id"])
            continue
        # Also exclude files already estimated below threshold
        est = row["est_saving_pct"]
        if est is None or est >= threshold:
            groups[fld]["pending"].append(row)

    if stale_ids:
        con2 = sqlite3.connect(DB_PATH)
        placeholders = ",".join("?" * len(stale_ids))
        con2.execute(f"DELETE FROM conversions WHERE id IN ({placeholders})", stale_ids)
        con2.commit()
        con2.close()

    results = []
    for fld, data in groups.items():
        done_rows    = data["done"]
        pending_rows = data["pending"]
        if len(done_rows) < min_done or len(pending_rows) < min_pending:
            continue
        done_with_pct = [r for r in done_rows if r["saved_pct"] is not None]
        if not done_with_pct:
            continue
        avg_savings_pct = sum(r["saved_pct"] for r in done_with_pct) / len(done_with_pct)
        total_saved_mb  = sum(r["saved_mb"] or 0.0 for r in done_rows)

        speed_vals = []
        for r in done_rows:
            dur, sa, ca = r["source_duration_secs"], r["started_at"], r["completed_at"]
            if dur and sa and ca:
                try:
                    wall = (_dt.fromisoformat(ca) - _dt.fromisoformat(sa)).total_seconds()
                    if wall > 0:
                        speed_vals.append(dur / wall)
                except (ValueError, TypeError):
                    pass
        avg_speed = sum(speed_vals) / len(speed_vals) if speed_vals else None

        pending_source_mb  = sum(r["source_size_mb"] or 0.0 for r in pending_rows)
        pending_no_size    = sum(1 for r in pending_rows if not r["source_size_mb"])
        est_add = sum(
            ((r["est_saving_pct"] if r["est_saving_pct"] is not None else avg_savings_pct) / 100.0)
            * (r["source_size_mb"] or 0.0)
            for r in pending_rows
        )

        speed_factor = 1.0
        if avg_speed is not None:
            speed_factor = max(0.5, min(3.0, avg_speed / 2.0))
        priority_score = est_add * speed_factor

        results.append({
            "folder":            fld,
            "done_count":        len(done_rows),
            "pending_count":     len(pending_rows),
            "pending_no_size":   pending_no_size,
            "avg_savings_pct":   round(avg_savings_pct, 1),
            "avg_speed":         round(avg_speed, 2) if avg_speed is not None else None,
            "total_saved_mb":    round(total_saved_mb, 1),
            "pending_source_mb": round(pending_source_mb, 1),
            "est_additional_mb": round(est_add, 1),
            "priority_score":    round(priority_score, 1),
        })

    sort_keys = {
        "score":   lambda r: r["priority_score"],
        "savings": lambda r: r["avg_savings_pct"],
        "speed":   lambda r: r["avg_speed"] or 0.0,
        "pending": lambda r: r["pending_count"],
    }
    results.sort(key=sort_keys[sort_by], reverse=True)
    shown = results[:top]

    total_pending = sum(r["pending_count"] for r in results)
    total_est_mb  = sum(r["est_additional_mb"] for r in results)

    return jsonify({
        "rows":              shown,
        "total_analysed":    len(all_folders),
        "opportunity_count": len(results),
        "total_pending":     total_pending,
        "total_est_mb":      round(total_est_mb, 1),
        "stale_removed":     len(stale_ids),
    })


def _fmt_duration_secs(secs: float | None) -> str:
    """Format a duration in seconds as H:MM:SS."""
    if not secs:
        return ""
    h = int(secs // 3600)
    m = int((secs % 3600) // 60)
    s = int(secs % 60)
    return f"{h}:{m:02}:{s:02}"


def _prep_file_dict(row, root_fwd: str = "") -> dict:
    """Convert a DB row to a file dict compatible with populateTable and _queue_worker."""
    p = row["source_path"]
    size_mb = row["source_size_mb"] or 0.0
    # Compute folder path relative to the prep root (mirrors scanner rel_folder)
    if root_fwd and p.startswith(root_fwd + "/"):
        rel = p[len(root_fwd) + 1:]
        folder = rel.rsplit("/", 1)[0] if "/" in rel else ""
    else:
        folder = os.path.dirname(p).replace("\\", "/")
    return {
        "full_path":    p,
        "name":         os.path.basename(p),
        "folder":       folder,
        "size":         f"{size_mb:.1f}",
        "bitrate_kbps": row["source_bitrate_kbps"],
        "codec":        row["source_codec"] or "",
        "duration":     _fmt_duration_secs(row["source_duration_secs"]),
        "is_hi10":      False,
        "streams":      None,
        "status":       "pending",
        "est_pct":      row["est_saving_pct"] if "est_saving_pct" in row.keys() else None,
        "est_mb":       row["est_saving_mb"] if "est_saving_mb" in row.keys() else None,
        "est_cv":       row["est_sample_cv_pct"] if "est_sample_cv_pct" in row.keys() else None,
        "est_high_variance": bool(row["est_high_variance"]) if "est_high_variance" in row.keys() and row["est_high_variance"] is not None else False,
        "est_aggregation": row["est_aggregation"] if "est_aggregation" in row.keys() else None,
    }


def _load_db_file_dict(row, root_fwd: str = "") -> dict:
    """Full file dict for all statuses — used by Load from DB."""
    import json as _json
    p       = row["source_path"]
    size_mb = row["source_size_mb"] or 0.0
    if root_fwd and p.startswith(root_fwd + "/"):
        rel    = p[len(root_fwd) + 1:]
        folder = rel.rsplit("/", 1)[0] if "/" in rel else ""
    else:
        folder = os.path.dirname(p).replace("\\", "/")
    status       = (row["status"] or "pending").lower()
    out_mb       = row["output_size_mb"]
    saved_mb_val = row["saved_mb"]
    saved_pct    = row["saved_pct"]
    dropped_raw  = row["dropped_streams"]
    dropped      = (_json.loads(dropped_raw) if dropped_raw else [])
    d = {
        "full_path":       p,
        "name":            os.path.basename(p),
        "folder":          folder,
        "size":            f"{size_mb:,.1f}",
        "bitrate_kbps":    row["source_bitrate_kbps"],
        "codec":           row["source_codec"] or "",
        "duration":        _fmt_duration_secs(row["source_duration_secs"]),
        "is_hi10":         False,
        "streams":         None,
        "status":          status,
        "force_sw":        bool(row["force_sw"]),
        "force_convert":   bool(row["force_convert"]),
        "dropped_streams": dropped,
        "est_pct":         row["est_saving_pct"],
        "est_mb":          row["est_saving_mb"],
        "est_cv":          row["est_sample_cv_pct"],
        "est_high_variance": bool(row["est_high_variance"]),
        "est_aggregation": row["est_aggregation"],
    }
    if status == "done":
        d["output"] = str(round(out_mb, 1))       if out_mb       is not None else None
        d["saved"]  = str(round(saved_mb_val, 1)) if saved_mb_val is not None else None
        d["pct"]    = str(saved_pct)              if saved_pct    is not None else None
    return d


@app.route("/api/load_from_db")
def api_load_from_db():
    """Return all latest records per source_path under root — fast alternative to a filesystem scan."""
    root = request.args.get("root", "").strip()
    if not root:
        return jsonify({"error": "No root provided"}), 400
    root = os.path.normpath(root)
    if not os.path.isdir(root):
        return jsonify({"error": "Not a directory"}), 400

    root_fwd = root.replace("\\", "/").rstrip("/")

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT c.source_path, c.status, c.source_size_mb, c.source_bitrate_kbps,
               c.source_codec, c.source_duration_secs,
               c.output_size_mb, c.saved_mb, c.saved_pct,
             c.est_saving_pct, c.est_saving_mb,
             c.est_sample_cv_pct, c.est_high_variance, c.est_aggregation,
             c.force_sw, c.force_convert, c.dropped_streams
        FROM conversions c
        INNER JOIN (
            SELECT source_path, MAX(id) AS max_id
            FROM conversions
            WHERE source_path LIKE ?
            GROUP BY source_path
        ) latest ON c.id = latest.max_id
        ORDER BY c.source_path
        """,
        (root_fwd + "/%",),
    ).fetchall()
    con.close()

    files = [_load_db_file_dict(r, root_fwd) for r in rows]
    return jsonify({"files": files, "total": len(files)})


@app.route("/api/prep_scan")
def api_prep_scan():
    """Return all pending files under root so the client can start an estimate-only pass."""
    root = request.args.get("root", "").strip()
    if not root:
        return jsonify({"error": "No root provided"}), 400
    root = os.path.normpath(root)
    if not os.path.isdir(root):
        return jsonify({"error": "Not a directory"}), 400

    root_fwd = root.replace("\\", "/").rstrip("/")

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT c.source_path, c.source_size_mb, c.source_bitrate_kbps,
             c.source_codec, c.source_duration_secs,
             c.est_saving_pct, c.est_saving_mb,
             c.est_sample_cv_pct, c.est_high_variance, c.est_aggregation
        FROM conversions c
        INNER JOIN (
            SELECT source_path, MAX(id) AS max_id
            FROM conversions
            WHERE status = 'pending' AND source_path LIKE ?
            GROUP BY source_path
        ) latest ON c.id = latest.max_id
        WHERE c.status = 'pending'
        ORDER BY c.source_path
        """,
        (root_fwd + "/%",),
    ).fetchall()
    con.close()

    files = [_prep_file_dict(r, root_fwd) for r in rows]
    return jsonify({"files": files, "total": len(files)})


@app.route("/api/build_prep_queue")
def api_build_prep_queue():
    """After an estimate-only pass, select one representative file per unsampled folder."""
    root = request.args.get("root", "").strip()
    if not root:
        return jsonify({"error": "No root provided"}), 400
    root = os.path.normpath(root)

    root_fwd = root.replace("\\", "/").rstrip("/")
    settings  = _load_settings()
    threshold = int(settings.get("low_savings_threshold_pct", 5))

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    # Pending files with estimates under root
    pending_rows = con.execute(
        """
        SELECT c.source_path, c.source_size_mb, c.source_bitrate_kbps,
               c.source_codec, c.source_duration_secs,
             c.est_saving_pct, c.est_saving_mb,
             c.est_sample_cv_pct, c.est_high_variance, c.est_aggregation
        FROM conversions c
        INNER JOIN (
            SELECT source_path, MAX(id) AS max_id
            FROM conversions
            WHERE status = 'pending' AND source_path LIKE ?
            GROUP BY source_path
        ) latest ON c.id = latest.max_id
        WHERE c.status = 'pending'
        ORDER BY c.source_path
        """,
        (root_fwd + "/%",),
    ).fetchall()

    # Folders that already have at least one done conversion
    done_rows = con.execute(
        "SELECT DISTINCT source_path FROM conversions"
        " WHERE status = 'done' AND source_path LIKE ?",
        (root_fwd + "/%",),
    ).fetchall()
    con.close()

    seeded_folders: set[str] = {
        os.path.dirname(r["source_path"]).replace("\\", "/")
        for r in done_rows
    }

    # Group pending files by their immediate folder
    by_folder: dict[str, list] = {}
    for row in pending_rows:
        folder = os.path.dirname(row["source_path"]).replace("\\", "/")
        by_folder.setdefault(folder, []).append(row)

    files: list[dict] = []
    folders_seeded         = 0
    folders_already_seeded = 0
    folders_no_candidates  = 0

    for folder, candidates in sorted(by_folder.items()):
        if folder in seeded_folders:
            folders_already_seeded += 1
            continue
        # Keep files that passed estimation, were not estimated yet, or were
        # flagged high-variance (conversion path bypasses low-savings auto-skip).
        good = [
            c for c in candidates
            if (
                c["est_saving_pct"] is None
                or c["est_saving_pct"] >= threshold
                or bool(c["est_high_variance"])
            )
        ]
        if not good:
            folders_no_candidates += 1
            continue
        # Pick the median-bitrate file as the most representative candidate
        sortable = sorted(
            good,
            key=lambda c: c["source_bitrate_kbps"] or c["source_size_mb"] or 0,
        )
        chosen = sortable[len(sortable) // 2]
        d = _prep_file_dict(chosen, root_fwd)
        d["est_pct"] = chosen["est_saving_pct"]
        d["est_mb"]  = chosen["est_saving_mb"]
        d["est_cv"]  = chosen["est_sample_cv_pct"]
        d["est_high_variance"] = bool(chosen["est_high_variance"])
        d["est_aggregation"] = chosen["est_aggregation"]
        files.append(d)
        folders_seeded += 1

    return jsonify({
        "files":                  files,
        "folders_seeded":         folders_seeded,
        "folders_already_seeded": folders_already_seeded,
        "folders_no_candidates":  folders_no_candidates,
    })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import logging as _logging

    # Route Werkzeug access logs to stdout so they're visible in the console
    # window, while stderr (real Python tracebacks) goes to flask_crash.log.
    _wz_log = _logging.getLogger("werkzeug")
    _wz_log.handlers.clear()
    _wz_log.addHandler(_logging.StreamHandler(sys.stdout))
    _wz_log.propagate = False

    os.makedirs(config.LOCAL_TEMP_DIR, exist_ok=True)
    recovered = db.reset_stale_running()
    if recovered:
        print(f"[startup] Reset {recovered} stale 'running' record(s) to 'pending'")
    app.run(port=config.FLASK_PORT, debug=config.FLASK_DEBUG)
