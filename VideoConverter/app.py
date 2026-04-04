"""
VideoConverter — Flask application entry point.
Routes only; no business logic here.
"""
import ctypes
import os
import string

from flask import Flask, jsonify, render_template, request

import config

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SYSTEM_FOLDERS = {
    "system volume information", "$recycle.bin", "$windows.~bt", "$windows.~ws",
    "windows", "windows.old", "program files", "program files (x86)", "programdata",
    "recovery", "boot", "perflogs", "msocache",
}


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


@app.route("/api/status")
def api_status():
    """Placeholder — will return live job status once converter is wired up."""
    return jsonify({"status": "idle", "queue": []})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    os.makedirs(config.LOCAL_TEMP_DIR, exist_ok=True)
    app.run(port=config.FLASK_PORT, debug=config.FLASK_DEBUG)
