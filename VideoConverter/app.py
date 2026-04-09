"""
VideoConverter — Flask application entry point.
Routes only; no business logic here.
"""
import ctypes
import json
import os
import shutil
import string
import threading
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

# ---------------------------------------------------------------------------
# Job state (mutated only while holding _job_lock)
# ---------------------------------------------------------------------------
_job_lock   = threading.Lock()
_stop_event = threading.Event()
_ffmpeg_pid: list[int] = [0]   # _ffmpeg_pid[0] = current child PID; 0 means idle

_job: dict = {
    "state":         "idle",   # idle | running | done
    "current_index": 0,
    "total":         0,
    "current_file":  "",
    "progress_pct":  0.0,
    "fps":           0.0,
    "eta_secs":      0,
    "saved_mb":      0.0,
    "encoder":       "",
    "files":         [],
    "log":           [],
    "paused":        False,
}

_PROCESS_ALL_ACCESS = 0x1F0FFF


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


def _job_log(msg: str) -> None:
    with _job_lock:
        _job["log"].append(msg)
        if len(_job["log"]) > 500:
            _job["log"] = _job["log"][-400:]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _queue_worker(files: list[dict], anime_mode: bool, quality: int) -> None:
    """Runs in a daemon thread; processes the file queue sequentially."""
    total_saved = 0.0

    for idx, file_info in enumerate(files):
        if _stop_event.is_set():
            break

        full_path = file_info["full_path"]
        try:
            mtime = os.path.getmtime(full_path)
            size_mb = os.path.getsize(full_path) / (1024 * 1024)
        except OSError as exc:
            _job_log(f"Skipped {full_path}: {exc}")
            with _job_lock:
                _job["files"][idx]["status"] = "failed"
            continue

        # Ensure a DB record exists for this file
        codec = (file_info.get("streams") or {}).get("video", {}) or {}
        rec_id = db.upsert_pending(
            source_path   = full_path,
            source_mtime  = mtime,
            source_size_mb = size_mb,
            source_codec  = codec.get("codec") if isinstance(codec, dict) else None,
        )

        with _job_lock:
            _job["current_index"] = idx
            _job["current_file"]  = full_path
            _job["progress_pct"]  = 0.0
            _job["fps"]           = 0.0
            _job["eta_secs"]      = 0
            _job["encoder"]       = ""
            _job["files"][idx]["status"] = "converting"

        db.mark_running(rec_id, _utcnow())
        _job_log(f"[{idx+1}/{len(files)}] {os.path.basename(full_path)}")

        output_dir = os.path.join(os.path.dirname(full_path), "converted")

        def _progress(pct: float, fps: float, eta: int) -> None:
            with _job_lock:
                _job["progress_pct"] = pct
                _job["fps"]          = fps
                _job["eta_secs"]     = eta

        result = converter.convert_video(
            input_path  = full_path,
            output_dir  = output_dir,
            anime_mode  = anime_mode,
            quality     = quality,
            progress_cb = _progress,
            stop_event  = _stop_event,
            log         = _job_log,
            pid_holder  = _ffmpeg_pid,
        )

        if result["ok"]:
            total_saved += result["saved_mb"]
            db.mark_done(
                record_id      = rec_id,
                output_path    = result["output_path"],
                output_size_mb = result["output_size_mb"],
                saved_mb       = result["saved_mb"],
                saved_pct      = result["saved_pct"],
                completed_at   = _utcnow(),
                encoder_used   = result["encoder_used"],
            )
            # Delete source after successful verified conversion
            try:
                os.remove(full_path)
                _job_log(f"Deleted source: {full_path}")
            except OSError as exc:
                _job_log(f"Could not delete source: {exc}")
            with _job_lock:
                _job["files"][idx].update({
                    "status":     "done",
                    "output":     str(round(result["output_size_mb"], 1)),
                    "saved":      str(round(result["saved_mb"], 1)),
                    "pct":        str(result["saved_pct"]),
                    "output_path": result["output_path"],
                })
        else:
            db.mark_failed(rec_id, result.get("error", ""), _utcnow())
            with _job_lock:
                _job["files"][idx]["status"] = "failed"
            _job_log(f"Failed: {result.get('error', 'unknown')}")

        with _job_lock:
            _job["saved_mb"] = total_saved

    with _job_lock:
        _job["state"]        = "done"
        _job["current_file"] = ""
        _job["progress_pct"] = 100.0
        _ffmpeg_pid[0]       = 0

_SETTINGS_PATH = os.path.join(os.path.dirname(__file__), "settings.json")

_SETTINGS_DEFAULTS = {
    "qsv_quality":   config.QSV_QUALITY,
    "sw_hevc_crf":   config.SW_HEVC_CRF,
    "local_temp_dir": config.LOCAL_TEMP_DIR,
    "default_sort":  "bitrate",   # bitrate | size | name
    "anime_mode":    False,
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
_LEGACY_FOLDERS = {"converted", "hevc"}


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
    'converted' or 'hevc' is moved up (at most two levels) so it sits beside
    its former container.  Empty legacy folders are removed after the walk.

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

    req_path = os.path.normpath(req_path)
    if not os.path.isdir(req_path):
        return jsonify({"error": "Not a directory"}), 400

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

    if not files:
        return jsonify({"error": "No files provided"}), 400

    _stop_event.clear()
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
        })

    t = threading.Thread(
        target=_queue_worker,
        args=(list(files), anime, quality),
        daemon=True,
    )
    t.start()
    return jsonify({"ok": True})


@app.route("/api/status")
def api_status():
    """Return current job state."""
    with _job_lock:
        return jsonify(dict(_job))


@app.route("/api/stop", methods=["POST"])
def api_stop():
    """Signal the queue worker to stop after the current file (or kill mid-encode)."""
    _stop_event.set()
    _resume_ffmpeg()   # unblock if paused so it can see the stop_event
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
        for event in scanner.walk(path):
            yield f"data: {json.dumps(event)}\n\n"

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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    os.makedirs(config.LOCAL_TEMP_DIR, exist_ok=True)
    app.run(port=config.FLASK_PORT, debug=config.FLASK_DEBUG)
