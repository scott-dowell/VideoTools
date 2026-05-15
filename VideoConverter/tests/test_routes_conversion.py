"""
tests/test_routes_conversion.py
================================
Tests for the conversion Flask routes:
  POST /api/start
  GET  /api/status
  POST /api/stop
  POST /api/pause
  POST /api/resume

Run:  pytest VideoConverter/tests/test_routes_conversion.py -v
"""

import os
import shutil
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db
import app as flask_app
import converter

FIXTURES = Path(__file__).parent / "fixtures"

pytestmark = pytest.mark.skipif(
    not FIXTURES.exists() or not any(FIXTURES.iterdir()),
    reason="Run VideoConverter/tests/make_fixtures.ps1 first",
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    flask_app.DB_PATH = db_path
    yield db_path


@pytest.fixture(autouse=True)
def mock_estimate():
    """Prevent real ffmpeg test-encode during the estimate step in all worker tests."""
    with patch.object(converter, "estimate", return_value={
        "estimated_saving_pct": 20,
        "estimated_saving_mb":  100.0,
        "error": None,
    }):
        yield


@pytest.fixture
def client():
    flask_app.app.testing = True
    with flask_app.app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def reset_job():
    """Reset global job state before each test."""
    flask_app._stop_event.clear()
    flask_app._ffmpeg_pid[0] = 0
    with flask_app._job_lock:
        flask_app._job.update({
            "state":         "idle",
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
        })
    yield
    # Ensure any background thread stops after the test
    flask_app._stop_event.set()
    time.sleep(0.1)
    flask_app._stop_event.clear()


def _make_file_list(names):
    return [{"full_path": str(FIXTURES / n), "name": n, "status": "pending"} for n in names]


# ---------------------------------------------------------------------------
# /api/status — idle
# ---------------------------------------------------------------------------

def test_api_status_idle(client):
    resp = client.get("/api/status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["state"] == "idle"


# ---------------------------------------------------------------------------
# /api/start
# ---------------------------------------------------------------------------

def test_api_start_returns_ok(client):
    """POST /api/start with a valid file list returns 200 ok and state flips to running."""
    # Use a fast-returning mock so the thread doesn't actually encode
    with patch.object(converter, "convert_video", return_value={
        "ok": False, "output_path": None,
        "output_size_mb": 0, "saved_mb": 0, "saved_pct": 0,
        "encoder_used": "", "error": "mocked",
    }):
        resp = client.post("/api/start", json={
            "files": _make_file_list(["h264_short.mkv"]),
            "anime_mode": False,
        })

    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True

    # Give the thread a moment to start
    time.sleep(0.05)
    status = client.get("/api/status").get_json()
    assert status["state"] in ("running", "done")


def test_api_double_start_409(client):
    """Second POST /api/start while running returns 409."""
    event = threading.Event()

    def _blocking(*args, **kwargs):
        event.wait(timeout=3)
        return {
            "ok": False, "output_path": None,
            "output_size_mb": 0, "saved_mb": 0,
            "saved_pct": 0, "encoder_used": "", "error": "mocked",
        }

    with patch.object(converter, "convert_video", _blocking):
        client.post("/api/start", json={
            "files": _make_file_list(["h264_short.mkv"]),
            "anime_mode": False,
        })
        time.sleep(0.05)
        resp2 = client.post("/api/start", json={
            "files": _make_file_list(["h264_short.mkv"]),
            "anime_mode": False,
        })
    event.set()

    assert resp2.status_code == 409


def test_api_start_empty_files_400(client):
    resp = client.post("/api/start", json={"files": [], "anime_mode": False})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# /api/stop
# ---------------------------------------------------------------------------

def test_api_stop(client):
    """POST /api/stop signals stop_event and eventually state goes to done."""
    event = threading.Event()

    def _blocking(*args, **kwargs):
        event.wait(timeout=3)
        return {
            "ok": False, "output_path": None,
            "output_size_mb": 0, "saved_mb": 0,
            "saved_pct": 0, "encoder_used": "", "error": "stopped",
        }

    with patch.object(converter, "convert_video", _blocking):
        client.post("/api/start", json={
            "files": _make_file_list(["h264_short.mkv"]),
            "anime_mode": False,
        })
        time.sleep(0.05)
        stop_resp = client.post("/api/stop")
    event.set()

    assert stop_resp.status_code == 200
    assert stop_resp.get_json()["ok"] is True
    assert flask_app._stop_event.is_set()


# ---------------------------------------------------------------------------
# /api/pause + /api/resume
# ---------------------------------------------------------------------------

def test_api_pause_sets_paused(client):
    resp = client.post("/api/pause")
    assert resp.status_code == 200
    with flask_app._job_lock:
        assert flask_app._job["paused"] is True


def test_api_resume_clears_paused(client):
    with flask_app._job_lock:
        flask_app._job["paused"] = True
    resp = client.post("/api/resume")
    assert resp.status_code == 200
    with flask_app._job_lock:
        assert flask_app._job["paused"] is False


# ---------------------------------------------------------------------------
# DB integration — running / done / failed transitions
# ---------------------------------------------------------------------------

def test_db_done_on_success(client, tmp_path, fresh_db):
    """A successful conversion writes status='done' to the DB."""
    src = str(FIXTURES / "h264_short.mkv")
    out = str(tmp_path / "converted" / "h264_short.mkv")
    os.makedirs(str(tmp_path / "converted"), exist_ok=True)

    import shutil
    shutil.copy(src, str(tmp_path / "h264_short.mkv"))
    test_src = str(tmp_path / "h264_short.mkv")

    done_event = threading.Event()

    def _ok_convert(*args, **kwargs):
        # Create a fake (smaller) output file
        os.makedirs(str(tmp_path / "converted"), exist_ok=True)
        with open(out, "wb") as f:
            f.write(b"\x00" * 100)
        return {
            "ok": True,
            "output_path": out,
            "output_size_mb": 0.0001,
            "saved_mb": 1.0,
            "saved_pct": 90,
            "encoder_used": "hevc_qsv",
            "error": None,
        }

    with patch.object(converter, "convert_video", _ok_convert), \
         patch.object(converter, "_verify_tracks_preserved", return_value=(True, "")):
        client.post("/api/start", json={
            "files": [{"full_path": test_src, "name": "h264_short.mkv", "status": "pending"}],
            "anime_mode": False,
        })
        # Wait for the worker to finish (max 3s)
        for _ in range(30):
            time.sleep(0.1)
            status = client.get("/api/status").get_json()
            if status["state"] == "done":
                break

    mtime = os.path.getmtime(test_src) if os.path.exists(test_src) else None
    # Source may have been deleted by the worker (expected on success)
    import sqlite3
    conn = sqlite3.connect(fresh_db)
    rows = conn.execute(
        "SELECT status FROM conversions WHERE source_path = ?", (test_src,)
    ).fetchall()
    conn.close()
    assert rows, "No DB record found"
    assert rows[0][0] == "done"


def test_db_failed_on_error(client, tmp_path, fresh_db):
    """A failed conversion writes status='failed' to the DB."""
    src = str(FIXTURES / "h264_short.mkv")
    import shutil
    shutil.copy(src, str(tmp_path / "h264_short.mkv"))
    test_src = str(tmp_path / "h264_short.mkv")

    with patch.object(converter, "convert_video", return_value={
        "ok": False, "output_path": None,
        "output_size_mb": 0, "saved_mb": 0, "saved_pct": 0,
        "encoder_used": "", "error": "simulated failure",
    }):
        client.post("/api/start", json={
            "files": [{"full_path": test_src, "name": "h264_short.mkv", "status": "pending"}],
            "anime_mode": False,
        })
        for _ in range(30):
            time.sleep(0.1)
            if client.get("/api/status").get_json()["state"] == "done":
                break

    import sqlite3
    conn = sqlite3.connect(fresh_db)
    rows = conn.execute(
        "SELECT status FROM conversions WHERE source_path = ?", (test_src,)
    ).fetchall()
    conn.close()
    assert rows
    assert rows[0][0] == "failed"
    # Critical: source file must NOT be deleted after a failed conversion
    assert os.path.exists(test_src), "Source file was deleted despite a failed conversion"


def test_failed_conversion_source_not_deleted(client, tmp_path, fresh_db):
    """Source video is NEVER deleted when conversion returns ok=False.

    This is a safety-critical invariant: no video should be discarded
    unless the output has been successfully verified.
    """
    src = str(FIXTURES / "h264_short.mkv")
    import shutil
    shutil.copy(src, str(tmp_path / "h264_short.mkv"))
    test_src = str(tmp_path / "h264_short.mkv")

    original_size = os.path.getsize(test_src)

    with patch.object(converter, "convert_video", return_value={
        "ok": False, "output_path": None,
        "output_size_mb": 0, "saved_mb": 0, "saved_pct": 0,
        "encoder_used": "", "error": "simulated encoder failure",
    }):
        client.post("/api/start", json={
            "files": [{"full_path": test_src, "name": "h264_short.mkv", "status": "pending"}],
            "anime_mode": False,
        })
        for _ in range(30):
            time.sleep(0.1)
            if client.get("/api/status").get_json()["state"] == "done":
                break

    assert os.path.exists(test_src), "Source file was deleted despite conversion failure"
    assert os.path.getsize(test_src) == original_size, "Source file was modified"


def test_failed_conversion_source_not_deleted_multiple_files(client, tmp_path, fresh_db):
    """No source file from a multi-file queue is deleted when all conversions fail."""
    src = str(FIXTURES / "h264_short.mkv")
    import shutil
    sources = []
    for i in range(3):
        dest = str(tmp_path / f"video_{i}.mkv")
        shutil.copy(src, dest)
        sources.append(dest)

    with patch.object(converter, "convert_video", return_value={
        "ok": False, "output_path": None,
        "output_size_mb": 0, "saved_mb": 0, "saved_pct": 0,
        "encoder_used": "", "error": "simulated failure",
    }):
        client.post("/api/start", json={
            "files": [
                {"full_path": s, "name": os.path.basename(s), "status": "pending"}
                for s in sources
            ],
            "anime_mode": False,
        })
        for _ in range(60):
            time.sleep(0.1)
            if client.get("/api/status").get_json()["state"] == "done":
                break

    for s in sources:
        assert os.path.exists(s), f"Source file was deleted after failed conversion: {s}"


# ---------------------------------------------------------------------------
# In-place replacement (output_path == source_path)
# ---------------------------------------------------------------------------

def test_source_not_deleted_when_replaced_in_place(client, tmp_path, fresh_db):
    """When convert_video returns output_path == source_path (atomic in-place
    replacement) the worker must NOT attempt a second os.remove() call on the
    same path — that would delete the freshly-converted file.
    """
    import shutil
    src = str(FIXTURES / "h264_short.mkv")
    shutil.copy(src, str(tmp_path / "h264_short.mkv"))
    test_src = str(tmp_path / "h264_short.mkv")

    with patch.object(converter, "convert_video", return_value={
        "ok": True,
        "output_path": test_src,   # same path — simulates atomic os.replace()
        "output_size_mb": 0.5,
        "saved_mb": 1.0,
        "saved_pct": 67,
        "encoder_used": "hevc_qsv",
        "error": None,
    }):
        client.post("/api/start", json={
            "files": [{"full_path": test_src, "name": "h264_short.mkv", "status": "pending"}],
            "anime_mode": False,
        })
        for _ in range(30):
            time.sleep(0.1)
            if client.get("/api/status").get_json()["state"] == "done":
                break

    # File must still exist — the worker should not have deleted it
    assert os.path.exists(test_src), (
        "Worker deleted the file even though output_path == source_path"
    )


# ---------------------------------------------------------------------------
# Error tail / ffmpeg_cmd stored on failure
# ---------------------------------------------------------------------------

def test_error_tail_stored_in_job_on_failure(client, tmp_path, fresh_db):
    """On a failed conversion the job stores error_tail and ffmpeg_cmd in the
    per-file entry so the UI can display them in the error modal.
    """
    import shutil
    src = str(FIXTURES / "h264_short.mkv")
    shutil.copy(src, str(tmp_path / "h264_short.mkv"))
    test_src = str(tmp_path / "h264_short.mkv")

    def _failing_convert(*args, **kwargs):
        log = kwargs.get("log", lambda m: None)
        log("Running: ffmpeg -y -i input.mkv -c:v hevc_qsv output.mkv")
        log("[hevc_qsv @ 0x...] Error: encoder init failed")
        return {
            "ok": False, "output_path": None,
            "output_size_mb": 0, "saved_mb": 0, "saved_pct": 0,
            "encoder_used": "", "error": "encoder init failed",
        }

    with patch.object(converter, "convert_video", _failing_convert):
        client.post("/api/start", json={
            "files": [{"full_path": test_src, "name": "h264_short.mkv", "status": "pending"}],
            "anime_mode": False,
        })
        for _ in range(30):
            time.sleep(0.1)
            if client.get("/api/status").get_json()["state"] == "done":
                break

    status = client.get("/api/status").get_json()
    entry  = status["files"][0]
    assert entry["status"] == "failed"
    assert "error_tail" in entry,  "error_tail missing from failed file entry"
    assert len(entry["error_tail"]) > 0, "error_tail is empty"
    assert "ffmpeg_cmd" in entry, "ffmpeg_cmd missing from failed file entry"
    assert "ffmpeg" in entry["ffmpeg_cmd"], "ffmpeg_cmd does not contain the command"


# ---------------------------------------------------------------------------
# 'stopped' state after user-requested stop
# ---------------------------------------------------------------------------

def test_job_state_is_stopped_after_stop(client, tmp_path, fresh_db):
    """After the user requests a stop, job state becomes 'stopped' (not 'done')."""
    import shutil
    src = str(FIXTURES / "h264_short.mkv")
    shutil.copy(src, str(tmp_path / "h264_short.mkv"))
    test_src = str(tmp_path / "h264_short.mkv")

    encode_started = threading.Event()

    def _blocking_convert(*args, **kwargs):
        stop = kwargs.get("stop_event")
        encode_started.set()
        if stop:
            stop.wait(timeout=5)
        return {
            "ok": False, "output_path": None,
            "output_size_mb": 0, "saved_mb": 0, "saved_pct": 0,
            "encoder_used": "", "error": "stopped",
        }

    with patch.object(converter, "convert_video", _blocking_convert):
        client.post("/api/start", json={
            "files": [{"full_path": test_src, "name": "h264_short.mkv", "status": "pending"}],
            "anime_mode": False,
        })
        assert encode_started.wait(timeout=3), "conversion never started"
        client.post("/api/stop")
        for _ in range(50):
            time.sleep(0.1)
            state = client.get("/api/status").get_json()["state"]
            if state in ("stopped", "done"):
                break

    assert state == "stopped", f"Expected 'stopped' but got '{state}'"


def test_stop_mid_queue_remaining_files_stay_pending(client, tmp_path, fresh_db):
    """Files that have not been started when Stop fires must remain 'pending'."""
    import shutil
    src = str(FIXTURES / "h264_short.mkv")
    copies = []
    for i in range(3):
        dst = str(tmp_path / f"vid_{i}.mkv")
        shutil.copy(src, dst)
        copies.append(dst)

    first_started = threading.Event()

    def _blocking_convert(*args, **kwargs):
        stop = kwargs.get("stop_event")
        first_started.set()
        if stop:
            stop.wait(timeout=5)
        return {
            "ok": False, "output_path": None,
            "output_size_mb": 0, "saved_mb": 0, "saved_pct": 0,
            "encoder_used": "", "error": "stopped",
        }

    with patch.object(converter, "convert_video", _blocking_convert):
        client.post("/api/start", json={
            "files": [
                {"full_path": c, "name": os.path.basename(c), "status": "pending"}
                for c in copies
            ],
            "anime_mode": False,
        })
        assert first_started.wait(timeout=3), "conversion never started"
        client.post("/api/stop")
        for _ in range(60):
            time.sleep(0.1)
            state = client.get("/api/status").get_json()["state"]
            if state in ("stopped", "done"):
                break

    status = client.get("/api/status").get_json()
    assert status["state"] == "stopped"
    # First file was being converted when stopped
    assert status["files"][0]["status"] == "failed"
    # Remaining files were never started — must stay pending
    assert status["files"][1]["status"] == "pending", (
        f"files[1] should be pending, got {status['files'][1]['status']}"
    )
    assert status["files"][2]["status"] == "pending", (
        f"files[2] should be pending, got {status['files'][2]['status']}"
    )


# ---------------------------------------------------------------------------
# Track verification safety tests
# ---------------------------------------------------------------------------

def _wait_done(client, timeout=3.0):
    for _ in range(int(timeout / 0.1)):
        time.sleep(0.1)
        if client.get("/api/status").get_json()["state"] == "done":
            return True
    return False


def test_track_verify_fail_marks_failed_and_deletes_output(client, tmp_path, fresh_db):
    """
    When _verify_tracks_preserved returns False the worker must:
    - delete the bad output file
    - mark the DB record 'failed'
    - NOT delete the source file
    - report the file status as 'failed' in /api/status
    """
    src = str(FIXTURES / "h264_short.mkv")
    shutil.copy(src, str(tmp_path / "h264_short.mkv"))
    test_src = str(tmp_path / "h264_short.mkv")

    bad_output = str(tmp_path / "bad_output.mp4")
    Path(bad_output).write_bytes(b"\x00" * 100)  # exists but will fail verification

    def _ok_convert(*args, **kwargs):
        return {
            "ok": True,
            "output_path": bad_output,
            "output_size_mb": 0.0001,
            "saved_mb": 1.0,
            "saved_pct": 90,
            "encoder_used": "hevc_qsv",
            "error": None,
        }

    with patch.object(converter, "convert_video", _ok_convert), \
         patch.object(converter, "_verify_tracks_preserved", return_value=(False, "subtitles lost: source had 1 subtitle track(s), output has none")):

        client.post("/api/start", json={
            "files": [{"full_path": test_src, "name": "h264_short.mkv", "status": "pending"}],
            "anime_mode": True,
        })
        _wait_done(client)

    # Output file must be deleted
    assert not os.path.exists(bad_output), "Bad output file should have been deleted"
    # Source must be preserved
    assert os.path.exists(test_src), "Source file must NOT be deleted when track check fails"
    # DB must be failed
    import sqlite3
    conn = sqlite3.connect(fresh_db)
    rows = conn.execute(
        "SELECT status FROM conversions WHERE source_path = ?", (test_src,)
    ).fetchall()
    conn.close()
    assert rows and rows[0][0] == "failed", f"Expected DB status=failed, got: {rows}"
    # API status must show failed
    status = client.get("/api/status").get_json()
    assert status["files"][0]["status"] == "failed"


def test_track_verify_pass_deletes_source(client, tmp_path, fresh_db):
    """
    When _verify_tracks_preserved passes and output path differs,
    the source file IS deleted and DB is marked done.
    """
    src = str(FIXTURES / "h264_short.mkv")
    shutil.copy(src, str(tmp_path / "h264_short.mkv"))
    test_src = str(tmp_path / "h264_short.mkv")

    good_output = str(tmp_path / "h264_short.mp4")
    Path(good_output).write_bytes(b"\x00" * 100)

    def _ok_convert(*args, **kwargs):
        return {
            "ok": True,
            "output_path": good_output,
            "output_size_mb": 0.0001,
            "saved_mb": 1.0,
            "saved_pct": 90,
            "encoder_used": "hevc_qsv",
            "error": None,
        }

    with patch.object(converter, "convert_video", _ok_convert), \
         patch.object(converter, "_verify_tracks_preserved", return_value=(True, "")):

        client.post("/api/start", json={
            "files": [{"full_path": test_src, "name": "h264_short.mkv", "status": "pending"}],
            "anime_mode": True,
        })
        _wait_done(client)

    assert os.path.exists(good_output), "Output should exist after success"
    assert not os.path.exists(test_src), "Source should be deleted after successful track verify"
    import sqlite3
    conn = sqlite3.connect(fresh_db)
    rows = conn.execute(
        "SELECT status FROM conversions WHERE source_path = ?", (test_src,)
    ).fetchall()
    conn.close()
    assert rows and rows[0][0] == "done"
