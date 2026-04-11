"""
tests/test_db.py
================
Unit tests for VideoConverter/db.py (SQLite persistence layer).

pytest VideoConverter/tests/test_db.py -v
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_db(tmp_path):
    """Initialise a fresh in-memory-equivalent DB in a temp file."""
    db_path = str(tmp_path / "test_conversions.db")
    db.init_db(db_path)
    yield db_path


# ---------------------------------------------------------------------------
# init_db
# ---------------------------------------------------------------------------

def test_db_init_creates_table(fresh_db):
    import sqlite3
    conn = sqlite3.connect(fresh_db)
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    conn.close()
    assert "conversions" in tables


def test_db_init_idempotent(tmp_path):
    """Calling init_db twice on the same path must not raise."""
    db_path = str(tmp_path / "idempotent.db")
    db.init_db(db_path)
    db.init_db(db_path)   # second call — should succeed without error


# ---------------------------------------------------------------------------
# upsert_pending
# ---------------------------------------------------------------------------

def test_upsert_pending_inserts_new_row(fresh_db):
    record_id = db.upsert_pending(
        "/data/show/ep01.mkv", 1_700_000_000.0,
        source_size_bytes=1_258_291_200,
        source_size_mb=1200.5,
        source_codec="h264",
    )
    assert isinstance(record_id, int)
    assert record_id > 0

    row = db.get_record("/data/show/ep01.mkv", 1_700_000_000.0)
    assert row is not None
    assert row["status"] == "pending"
    assert row["source_codec"] == "h264"
    assert abs(row["source_size_mb"] - 1200.5) < 0.01
    assert row["source_size_bytes"] == 1_258_291_200


def test_upsert_pending_idempotent(fresh_db):
    """Inserting the same (path, mtime) twice returns the same id."""
    id1 = db.upsert_pending("/data/ep.mkv", 1_000.0)
    id2 = db.upsert_pending("/data/ep.mkv", 1_000.0)
    assert id1 == id2


def test_upsert_pending_different_mtime_new_row(fresh_db):
    """Same path with a different mtime must create a distinct record."""
    id1 = db.upsert_pending("/data/ep.mkv", 1_000.0)
    id2 = db.upsert_pending("/data/ep.mkv", 2_000.0)   # file was replaced
    assert id1 != id2

    row_old = db.get_record("/data/ep.mkv", 1_000.0)
    row_new = db.get_record("/data/ep.mkv", 2_000.0)
    assert row_old is not None
    assert row_new is not None


def test_upsert_pending_stores_anime_mode(fresh_db):
    """anime_mode=True is persisted on insert."""
    import sqlite3
    db.upsert_pending("/data/ep.mkv", 1_000.0, anime_mode=True)
    with sqlite3.connect(fresh_db) as conn:
        row = conn.execute(
            "SELECT anime_mode FROM conversions WHERE source_path=?", ("/data/ep.mkv",)
        ).fetchone()
    assert row is not None
    assert row[0] == 1


def test_upsert_pending_updates_anime_mode_on_conflict(fresh_db):
    """Re-queueing the same (path, mtime) with a different anime_mode updates the column."""
    import sqlite3
    db.upsert_pending("/data/ep.mkv", 1_000.0, anime_mode=False)
    db.upsert_pending("/data/ep.mkv", 1_000.0, anime_mode=True)   # same key
    with sqlite3.connect(fresh_db) as conn:
        row = conn.execute(
            "SELECT anime_mode FROM conversions WHERE source_path=?", ("/data/ep.mkv",)
        ).fetchone()
    assert row is not None
    assert row[0] == 1   # updated, not left at 0


# ---------------------------------------------------------------------------
# get_record
# ---------------------------------------------------------------------------

def test_get_record_miss(fresh_db):
    assert db.get_record("/nonexistent/file.mkv", 9999.0) is None


def test_get_record_returns_correct_row(fresh_db):
    db.upsert_pending("/a.mkv", 1.0, source_codec="hevc")
    db.upsert_pending("/b.mkv", 1.0, source_codec="h264")
    row = db.get_record("/b.mkv", 1.0)
    assert row["source_codec"] == "h264"


# ---------------------------------------------------------------------------
# mark_running
# ---------------------------------------------------------------------------

def test_mark_running_flips_status(fresh_db):
    rec_id = db.upsert_pending("/data/ep.mkv", 1.0)
    db.mark_running(rec_id, "2026-04-10T12:00:00Z")

    row = db.get_record("/data/ep.mkv", 1.0)
    assert row["status"] == "running"
    assert row["started_at"] == "2026-04-10T12:00:00Z"


# ---------------------------------------------------------------------------
# mark_done
# ---------------------------------------------------------------------------

def test_mark_done_fills_output_fields(fresh_db):
    rec_id = db.upsert_pending("/data/ep.mkv", 1.0, source_size_mb=1000.0)
    db.mark_running(rec_id, "2026-04-10T12:00:00Z")
    db.mark_done(
        rec_id,
        output_path="/data/converted/ep.mkv",
        output_size_mb=400.0,
        saved_mb=600.0,
        saved_pct=60,
        completed_at="2026-04-10T12:10:00Z",
        encoder_used="hevc_qsv",
    )

    row = db.get_record("/data/ep.mkv", 1.0)
    assert row["status"] == "done"
    assert row["output_path"] == "/data/converted/ep.mkv"
    assert abs(row["output_size_mb"] - 400.0) < 0.01
    assert abs(row["saved_mb"] - 600.0) < 0.01
    assert row["saved_pct"] == 60
    assert row["encoder_used"] == "hevc_qsv"
    assert row["completed_at"] == "2026-04-10T12:10:00Z"


# ---------------------------------------------------------------------------
# mark_failed
# ---------------------------------------------------------------------------

def test_mark_failed_sets_error_tail(fresh_db):
    rec_id = db.upsert_pending("/data/ep.mkv", 1.0)
    db.mark_running(rec_id, "2026-04-10T12:00:00Z")
    db.mark_failed(rec_id, "Error: codec not supported\n...", "2026-04-10T12:01:00Z")

    row = db.get_record("/data/ep.mkv", 1.0)
    assert row["status"] == "failed"
    assert "codec not supported" in row["error_tail"]
    assert row["completed_at"] == "2026-04-10T12:01:00Z"


def test_mark_failed_null_error_tail(fresh_db):
    rec_id = db.upsert_pending("/data/ep.mkv", 1.0)
    db.mark_failed(rec_id, None, "2026-04-10T12:01:00Z")
    row = db.get_record("/data/ep.mkv", 1.0)
    assert row["status"] == "failed"
    assert row["error_tail"] is None


# ---------------------------------------------------------------------------
# Re-processing detection (mtime change)
# ---------------------------------------------------------------------------

def test_mtime_change_creates_new_pending_record(fresh_db):
    """
    Simulate a file being replaced in-place: same path, new mtime.
    The old record stays; a new pending record is created.
    """
    old_mtime = 1_700_000_000.0
    new_mtime = old_mtime + 3600          # 1 hour later

    id_old = db.upsert_pending("/data/ep.mkv", old_mtime)
    db.mark_done(id_old, "/data/converted/ep.mkv", 400.0, 600.0, 60,
                 "2026-04-10T12:10:00Z", "hevc_qsv")

    id_new = db.upsert_pending("/data/ep.mkv", new_mtime)
    assert id_new != id_old

    # Old record still done; new record is pending
    old_row = db.get_record("/data/ep.mkv", old_mtime)
    new_row = db.get_record("/data/ep.mkv", new_mtime)
    assert old_row["status"] == "done"
    assert new_row["status"] == "pending"


# ---------------------------------------------------------------------------
# get_record_by_fingerprint
# ---------------------------------------------------------------------------

def test_get_record_by_fingerprint_finds_done_record(fresh_db):
    """Fingerprint lookup finds a done record by (mtime, size_bytes)."""
    rec_id = db.upsert_pending(
        "/original/folder/ep01.mkv", 1_700_000_000.0,
        source_size_bytes=417_333_248,
    )
    db.mark_done(rec_id, "/original/folder/ep01.mp4", 138.9, 258.6, 65,
                 "2026-04-10T10:00:00Z", "hevc_qsv")

    # Simulate folder rename: same mtime + size, different path
    row = db.get_record_by_fingerprint(1_700_000_000.0, 417_333_248)
    assert row is not None
    assert row["status"] == "done"


def test_get_record_by_fingerprint_miss(fresh_db):
    """Returns None when no matching done record exists."""
    assert db.get_record_by_fingerprint(1_700_000_000.0, 417_333_248) is None


def test_get_record_by_fingerprint_ignores_non_done(fresh_db):
    """Pending/failed records are not returned by fingerprint lookup."""
    rec_id = db.upsert_pending(
        "/folder/ep01.mkv", 1_700_000_000.0,
        source_size_bytes=417_333_248,
    )
    row = db.get_record_by_fingerprint(1_700_000_000.0, 417_333_248)
    assert row is None
