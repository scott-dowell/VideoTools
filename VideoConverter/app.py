"""
VideoConverter — Flask application entry point.
Routes only; no business logic here.
"""
import ctypes
import json
import os
import shutil
import string
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
import subprocess as _sp
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
}

_PROCESS_ALL_ACCESS = 0x1F0FFF
_session_started_at: float = 0.0   # set when /api/start is called


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
        if v_codec in ("av1", "av1_cuvid"):
            steps.append({"id": "remux", "label": "Remux", "state": "waiting", "detail": "AV1 stream-copy \u2192 MP4", "attempt": 1})
        elif v_codec in ("hevc", "hevc_cuvid", "hevc_qsv"):
            steps.append({"id": "remux", "label": "Remux", "state": "waiting", "detail": "HEVC stream-copy \u2192 MP4", "attempt": 1})
        else:
            steps.append({"id": "estimate", "label": "Estimate", "state": "waiting", "detail": "", "attempt": 1})
            steps.append({"id": "compress", "label": "Compress", "state": "waiting", "detail": "",           "attempt": 1})
            steps.append({"id": "audio",    "label": "Audio",    "state": "waiting", "detail": "",           "attempt": 1})
            steps.append({"id": "remux",    "label": "Remux",    "state": "waiting", "detail": "MKV \u2192 MP4", "attempt": 1})
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
        _step("compress", "running", "hevc_qsv")
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
        _step("remux", "retry", f"attempt {attempt_counter[0]}/6 \u00b7 DTS fix")
    elif m.startswith("DTS fix retry"):
        attempt_counter[0] += 1
        _step("remux", "retry", f"attempt {attempt_counter[0]}/6 \u00b7 genpts fix")
    elif m.startswith("AAC mux failed"):
        attempt_counter[0] += 1
        _step("audio", "running", "pre-encoding individually")
        _step("remux", "retry", f"attempt {attempt_counter[0]}/6 \u00b7 audio pre-enc")
    elif m.startswith("Subtitle DTS fix"):
        attempt_counter[0] += 1
        _step("remux", "retry", f"attempt {attempt_counter[0]}/6 \u00b7 SRT pre-extract")
    elif m.startswith("Retrying with pre-extracted SRT"):
        _step("remux", "running", f"attempt {attempt_counter[0]}/6 \u00b7 SRT subs")
    elif m.startswith("Retrying without subtitle"):
        attempt_counter[0] += 1
        _step("remux", "retry", f"attempt {attempt_counter[0]}/6 \u00b7 no subs")
    elif "Pre-encoding audio track" in m:
        detail = m.split("Pre-encoding audio track", 1)[1].strip().rstrip(".")
        _step("audio", "running", detail)
    elif m.startswith("AV1 source"):
        if not attempt_counter[0]:
            attempt_counter[0] = 1
        _step("remux", "running", f"AV1 stream-copy \u00b7 attempt {attempt_counter[0]}/6")
    elif m.startswith("Integrity check failed"):
        _step("verify", "failed", m.split("Integrity check failed:", 1)[-1].strip())
    elif m.startswith("Done. Saved"):
        _step("audio", "done")
        _step("remux", "done")
        _step("verify", "running")
    elif m == "Integrity check passed.":
        _step("verify", "done")
    elif m.startswith("Skipped \u2013 output was not smaller"):
        _step("compress", "failed", "no savings")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def _queue_worker(files: list[dict], anime_mode: bool, quality: int, low_savings_threshold_pct: int = 5) -> None:
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

            while _remaining and not _stop_event.is_set():
                _ocr_bn_map: dict[str, str] = {os.path.basename(p): p for p in _remaining}
                _current_ocr_path = _remaining[0]
                crashed = False
                _ocr_proc = None
                _ocr_rsp_file = None
                try:
                    _manifest_args = ["--skip-manifest", _manifest_path] if _manifest_path else []
                    # Build base args; use a response file if the command line would
                    # exceed Windows' ~32 KB limit to avoid [WinError 206].
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

        full_path = file_info["full_path"].replace("\\", "/")

        # Always check the live DB status — never trust what the client sent
        db_rec = db.get_latest_statuses_by_paths([full_path]).get(full_path, {})
        db_status = db_rec.get("status")
        if db_status in ("done", "skipped", "no_saving", "low_savings"):
            with _job_lock:
                _job["files"][idx]["status"] = db_status
                _job["current_index"] = idx
            continue
        force_sw = bool(db_rec.get("force_sw", False))

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
            # Use cached estimate from DB if available — avoids the 10s test-encode
            # on re-runs (e.g. queue re-started after threshold change).
            _cached_pct = db_rec.get("est_saving_pct")
            _cached_mb  = db_rec.get("est_saving_mb")
            if _cached_pct is not None and _cached_mb is not None:
                _est = {"estimated_saving_pct": _cached_pct, "estimated_saving_mb": _cached_mb, "error": None}
                _step("estimate", "running", "cached")
            else:
                _step("estimate", "running", "sampling 10s clip\u2026")
                _est = converter.estimate(full_path, quality=quality)
            if _est.get("error"):
                # WMV, too short, etc. — skip estimate, proceed to encode normally
                _step("estimate", "skipped", _est["error"])
            else:
                _est_pct = _est["estimated_saving_pct"]
                _est_mb  = _est["estimated_saving_mb"]
                # Persist result so it is reused on any future run
                db.save_estimate(rec_id, _est_pct, _est_mb)
                with _job_lock:
                    _job["files"][idx]["est_pct"] = _est_pct
                    _job["files"][idx]["est_mb"]  = _est_mb
                _threshold = low_savings_threshold_pct
                if _est_pct < _threshold:
                    _step("estimate", "done", f"~{_est_pct}% \u2014 below {_threshold}% threshold")
                    _job_log(f"Estimated savings {_est_pct}% < {_threshold}% threshold \u2014 skipping encode.")
                    db.mark_low_savings(rec_id, _est_pct, _threshold, _utcnow())
                    with _job_lock:
                        _job["files"][idx]["status"] = "low_savings"
                    continue
                else:
                    _step("estimate", "done", f"~{_est_pct}% \u00b7 {_est_mb}\u202fMB")

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
            if result.get("error") == "no_savings":
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
        _job["ocr_batch"] = {"total": 0, "done": 0, "current_file": "", "files": []}

_SETTINGS_PATH = os.path.join(os.path.dirname(__file__), "settings.json")

_SETTINGS_DEFAULTS = {
    "qsv_quality":               config.QSV_QUALITY,
    "sw_hevc_crf":               config.SW_HEVC_CRF,
    "local_temp_dir":            config.LOCAL_TEMP_DIR,
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
    moved, skipped, errors = [], [], []
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

    return {"moved": moved, "skipped": skipped, "errors": errors}


def _cleanup_legacy_folders_stream(root_path: str):
    """Generator version of cleanup — yields SSE-ready progress dicts.

    Events:
      {"type": "scan_done", "total": N}
      {"type": "progress",  "done": N, "total": N, "name": "<filename>"}
      {"type": "done",      "moved": N, "skipped": N, "errors": N}
    """
    # Phase 1: fast walk to collect all candidates first (gives us a total)
    candidates: list[tuple[str, str]] = []
    legacy_dirs: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(root_path, topdown=False):
        if os.path.basename(dirpath).lower() not in _LEGACY_FOLDERS:
            continue
        legacy_dirs.append(dirpath)
        for filename in filenames:
            src = os.path.join(dirpath, filename)
            dest = _resolve_cleanup_dest(src)
            if dest is not None:
                candidates.append((src, dest))

    total = len(candidates)
    yield {"type": "scan_done", "total": total}

    # Phase 2: move files one by one, yielding progress before each
    moved_n = skipped_n = errors_n = 0
    for i, (src, dest) in enumerate(candidates):
        yield {"type": "progress", "done": i, "total": total, "name": os.path.basename(src)}
        if os.path.exists(dest):
            skipped_n += 1
            continue
        try:
            shutil.move(src, dest)
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

    yield {"type": "done", "moved": moved_n, "skipped": skipped_n, "errors": errors_n}


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
    return render_template("index.html")


@app.route("/api/browse")
def api_browse():
    req_path = request.args.get("path", "").strip()

    if req_path:
        req_path = os.path.normpath(req_path)
        # Walk up the tree until we find an existing directory (handles deleted remembered paths)
        while req_path and not os.path.isdir(req_path):
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
            if os.path.exists(drive):
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
    except PermissionError:
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
    settings  = _load_settings()
    quality   = int(settings.get("qsv_quality", config.QSV_QUALITY))
    threshold = int(settings.get("low_savings_threshold_pct", 5))
    # Apply local_temp_dir so ffmpeg staging uses the user-configured path
    temp_dir  = settings.get("local_temp_dir", "").strip()
    if temp_dir:
        config.LOCAL_TEMP_DIR = temp_dir

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
        })

    def _worker_safe():
        try:
            _queue_worker(list(files), anime, quality, threshold)
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


@app.route("/api/update_status", methods=["POST"])
def api_update_status():
    """Manually set the status and/or force_sw flag of one or more files."""
    data  = request.get_json(force=True) or {}
    paths = data.get("paths", [])
    new_status = data.get("status", "")
    force_sw   = data.get("force_sw", None)  # None = don't change it

    ALLOWED = {"skipped", "pending", "failed"}
    if new_status and new_status not in ALLOWED:
        return jsonify({"error": f"status must be one of {ALLOWED}"}), 400
    if not paths:
        return jsonify({"error": "No paths provided"}), 400
    if not new_status and force_sw is None:
        return jsonify({"error": "Provide at least one of: status, force_sw"}), 400

    updated = 0
    with db._connect() as con:
        cur = con.cursor()
        for path in paths:
            norm = path.replace("\\", "/")
            if new_status:
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
