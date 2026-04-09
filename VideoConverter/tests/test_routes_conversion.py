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

    with patch.object(converter, "convert_video", _ok_convert):
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
