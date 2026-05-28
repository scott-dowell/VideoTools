"""
tests/test_cleanup.py
======================
Tests for:
  _resolve_cleanup_dest()   — unit
  _cleanup_legacy_folders() — unit
  POST /api/cleanup         — Flask route

Run:  pytest VideoConverter/tests/test_cleanup.py -v
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as flask_app
from app import _resolve_cleanup_dest, _cleanup_legacy_folders


@pytest.fixture
def client():
    flask_app.app.testing = True
    with flask_app.app.test_client() as c:
        yield c


# ---------------------------------------------------------------------------
# _resolve_cleanup_dest — unit tests
# ---------------------------------------------------------------------------

def test_resolve_file_in_converted(tmp_path):
    """File inside 'converted/' should map to parent directory."""
    (tmp_path / "converted").mkdir()
    f = tmp_path / "converted" / "video.mkv"
    f.touch()
    dest = _resolve_cleanup_dest(str(f))
    assert dest == str(tmp_path / "video.mkv")


def test_resolve_file_in_hevc(tmp_path):
    """File inside 'hevc/' should map to parent directory."""
    (tmp_path / "hevc").mkdir()
    f = tmp_path / "hevc" / "video.mkv"
    f.touch()
    dest = _resolve_cleanup_dest(str(f))
    assert dest == str(tmp_path / "video.mkv")


def test_resolve_nested_converted_inside_hevc(tmp_path):
    """File nested two deep (hevc/converted/) should move up two levels."""
    (tmp_path / "hevc" / "converted").mkdir(parents=True)
    f = tmp_path / "hevc" / "converted" / "video.mkv"
    f.touch()
    dest = _resolve_cleanup_dest(str(f))
    assert dest == str(tmp_path / "video.mkv")


def test_resolve_nested_hevc_inside_converted(tmp_path):
    """File nested two deep (converted/hevc/) should also move up two levels."""
    (tmp_path / "converted" / "hevc").mkdir(parents=True)
    f = tmp_path / "converted" / "hevc" / "video.mkv"
    f.touch()
    dest = _resolve_cleanup_dest(str(f))
    assert dest == str(tmp_path / "video.mkv")


def test_resolve_non_legacy_parent_returns_none(tmp_path):
    """File in a normal folder → None (no move needed)."""
    (tmp_path / "movies").mkdir()
    f = tmp_path / "movies" / "video.mkv"
    f.touch()
    assert _resolve_cleanup_dest(str(f)) is None


def test_resolve_case_insensitive(tmp_path):
    """Folder name 'CONVERTED' (uppercase) is still treated as legacy."""
    (tmp_path / "CONVERTED").mkdir()
    f = tmp_path / "CONVERTED" / "video.mkv"
    f.touch()
    dest = _resolve_cleanup_dest(str(f))
    assert dest is not None
    assert os.path.basename(dest) == "video.mkv"


# ---------------------------------------------------------------------------
# _cleanup_legacy_folders — unit tests
# ---------------------------------------------------------------------------

def test_cleanup_moves_file_from_converted(tmp_path):
    """File inside converted/ is moved to parent and empty dir is removed."""
    conv = tmp_path / "converted"
    conv.mkdir()
    (conv / "video.mkv").write_bytes(b"\x00" * 8)

    result = _cleanup_legacy_folders(str(tmp_path))

    assert len(result["moved"]) == 1
    assert len(result["skipped"]) == 0
    assert len(result["errors"]) == 0
    assert (tmp_path / "video.mkv").exists()
    assert not conv.exists()


def test_cleanup_moves_file_from_hevc(tmp_path):
    """File inside hevc/ is moved to parent."""
    hevc = tmp_path / "hevc"
    hevc.mkdir()
    (hevc / "vid.mkv").write_bytes(b"\x00" * 8)

    result = _cleanup_legacy_folders(str(tmp_path))

    assert len(result["moved"]) == 1
    assert (tmp_path / "vid.mkv").exists()


def test_cleanup_skips_collision(tmp_path):
    """If destination already exists the file is skipped, not overwritten."""
    conv = tmp_path / "converted"
    conv.mkdir()
    (conv / "video.mkv").write_bytes(b"source")
    (tmp_path / "video.mkv").write_bytes(b"existing")

    result = _cleanup_legacy_folders(str(tmp_path))

    assert len(result["skipped"]) == 1
    assert len(result["moved"]) == 0
    assert (tmp_path / "video.mkv").read_bytes() == b"existing"
    assert (conv / "video.mkv").exists()


def test_cleanup_non_legacy_files_untouched(tmp_path):
    """Files in regular (non-legacy) folders are not touched."""
    (tmp_path / "movies").mkdir()
    f = tmp_path / "movies" / "untouched.mkv"
    f.write_bytes(b"\x00" * 8)

    result = _cleanup_legacy_folders(str(tmp_path))

    assert result["moved"] == []
    assert f.exists()


def test_cleanup_multiple_files(tmp_path):
    """Multiple files in converted/ are all moved."""
    conv = tmp_path / "converted"
    conv.mkdir()
    for i in range(3):
        (conv / f"v{i}.mkv").write_bytes(b"\x00" * 4)

    result = _cleanup_legacy_folders(str(tmp_path))

    assert len(result["moved"]) == 3
    assert not conv.exists()


def test_cleanup_nested_two_deep(tmp_path):
    """File inside hevc/converted/ moves up two levels when run from root."""
    (tmp_path / "hevc" / "converted").mkdir(parents=True)
    f = tmp_path / "hevc" / "converted" / "deep.mkv"
    f.write_bytes(b"\x00" * 4)

    result = _cleanup_legacy_folders(str(tmp_path))

    assert len(result["moved"]) == 1
    assert (tmp_path / "deep.mkv").exists()


def test_cleanup_returns_from_and_to_keys(tmp_path):
    """Each entry in 'moved' has 'from' and 'to' keys."""
    (tmp_path / "converted").mkdir()
    (tmp_path / "converted" / "a.mkv").write_bytes(b"\x00")

    result = _cleanup_legacy_folders(str(tmp_path))

    entry = result["moved"][0]
    assert "from" in entry
    assert "to" in entry


def test_cleanup_updates_db_paths_on_move(tmp_path, monkeypatch):
    """Cleanup should rewrite DB path references after a file move."""
    conv = tmp_path / "converted"
    conv.mkdir()
    src = conv / "dbmove.mkv"
    src.write_bytes(b"\x00")

    calls = []

    def _fake_move_path(old_path, new_path):
        calls.append((old_path.replace("\\", "/"), new_path.replace("\\", "/")))
        return {"source_updated": 0, "output_updated": 0}

    monkeypatch.setattr(flask_app.db, "move_path", _fake_move_path)

    result = _cleanup_legacy_folders(str(tmp_path))

    assert len(result["moved"]) == 1
    assert len(calls) == 1
    old_path, new_path = calls[0]
    assert old_path.endswith("/converted/dbmove.mkv")
    assert new_path.endswith("/dbmove.mkv")


def test_cleanup_empty_dir_returns_zero_moved(tmp_path):
    """Running cleanup on a dir with no legacy folders is a no-op."""
    result = _cleanup_legacy_folders(str(tmp_path))
    assert result == {"moved": [], "skipped": [], "errors": []}


def test_cleanup_legacy_dir_not_removed_when_not_empty(tmp_path):
    """If a collision prevents the move the legacy dir should NOT be removed."""
    conv = tmp_path / "converted"
    conv.mkdir()
    (conv / "video.mkv").write_bytes(b"source")
    (tmp_path / "video.mkv").write_bytes(b"existing")

    _cleanup_legacy_folders(str(tmp_path))

    # Because a file remains inside, the dir must still exist
    assert conv.exists()


# ---------------------------------------------------------------------------
# POST /api/cleanup — route tests
# ---------------------------------------------------------------------------

def test_api_cleanup_missing_path(client):
    r = client.post("/api/cleanup", json={})
    assert r.status_code == 400
    assert "error" in r.get_json()


def test_api_cleanup_empty_string_path(client):
    r = client.post("/api/cleanup", json={"path": ""})
    assert r.status_code == 400
    assert "error" in r.get_json()


def test_api_cleanup_not_a_directory(client, tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x")
    r = client.post("/api/cleanup", json={"path": str(f)})
    assert r.status_code == 400
    assert "error" in r.get_json()


def test_api_cleanup_nonexistent_path(client):
    r = client.post("/api/cleanup", json={"path": "C:/nonexistent_xyz_12345"})
    assert r.status_code == 400
    assert "error" in r.get_json()


def test_api_cleanup_valid_with_legacy_files(client, tmp_path):
    (tmp_path / "converted").mkdir()
    (tmp_path / "converted" / "vid.mkv").write_bytes(b"\x00" * 4)

    r = client.post("/api/cleanup", json={"path": str(tmp_path)})

    assert r.status_code == 200
    data = r.get_json()
    assert "moved" in data
    assert "skipped" in data
    assert "errors" in data
    assert len(data["moved"]) == 1


def test_api_cleanup_valid_empty_dir(client, tmp_path):
    r = client.post("/api/cleanup", json={"path": str(tmp_path)})
    assert r.status_code == 200
    data = r.get_json()
    assert data["moved"] == []
    assert data["errors"] == []


def test_api_cleanup_skipped_reported_in_response(client, tmp_path):
    (tmp_path / "converted").mkdir()
    (tmp_path / "converted" / "vid.mkv").write_bytes(b"src")
    (tmp_path / "vid.mkv").write_bytes(b"existing")

    r = client.post("/api/cleanup", json={"path": str(tmp_path)})

    assert r.status_code == 200
    data = r.get_json()
    assert len(data["skipped"]) == 1
    assert len(data["moved"]) == 0
