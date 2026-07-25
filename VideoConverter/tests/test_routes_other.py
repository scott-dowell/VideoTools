"""
tests/test_routes_other.py
===========================
Tests for the miscellaneous Flask routes:
  GET  /api/browse
  GET  /api/settings
  POST /api/settings
  GET  /api/estimate
  GET  /api/open

Run:  pytest VideoConverter/tests/test_routes_other.py -v
"""

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as flask_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    flask_app.app.testing = True
    with flask_app.app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path):
    """Redirect settings.json to a temp path so tests never touch the real file."""
    orig = flask_app._SETTINGS_PATH
    flask_app._SETTINGS_PATH = str(tmp_path / "settings.json")
    yield
    flask_app._SETTINGS_PATH = orig


# ---------------------------------------------------------------------------
# /api/browse — root (drives)
# ---------------------------------------------------------------------------

def test_browse_root_returns_drives(client):
    """Empty path → drive list with parent=None and path=''."""
    r = client.get("/api/browse?path=")
    assert r.status_code == 200
    data = r.get_json()
    assert data["parent"] is None
    assert data["path"] == ""
    assert isinstance(data["dirs"], list)
    # C:/ must exist on any Windows CI box
    assert any("C" in d["full_path"].upper() for d in data["dirs"])


def test_browse_root_drive_entries_have_required_fields(client):
    """Every drive entry must expose name, full_path, has_children."""
    data = client.get("/api/browse?path=").get_json()
    for entry in data["dirs"]:
        assert "name" in entry
        assert "full_path" in entry
        assert "has_children" in entry


# ---------------------------------------------------------------------------
# /api/browse — valid subdirectory
# ---------------------------------------------------------------------------

def test_browse_valid_dir_returns_200(client, tmp_path):
    r = client.get(f"/api/browse?path={tmp_path}")
    assert r.status_code == 200


def test_browse_valid_dir_has_path_and_parent(client, tmp_path):
    data = client.get(f"/api/browse?path={tmp_path}").get_json()
    assert "path" in data
    assert "parent" in data
    assert "dirs" in data


def test_browse_lists_subdirectory(client, tmp_path):
    """A child directory should appear in the listing."""
    (tmp_path / "mysubdir").mkdir()
    data = client.get(f"/api/browse?path={tmp_path}").get_json()
    names = [d["name"] for d in data["dirs"]]
    assert "mysubdir" in names


def test_browse_each_entry_has_required_fields(client, tmp_path):
    (tmp_path / "adir").mkdir()
    data = client.get(f"/api/browse?path={tmp_path}").get_json()
    entry = next(d for d in data["dirs"] if d["name"] == "adir")
    assert "name" in entry
    assert "full_path" in entry
    assert "has_children" in entry


def test_browse_excludes_dot_folders(client, tmp_path):
    """Folders starting with '.' must be excluded."""
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "visible").mkdir()
    names = [d["name"] for d in client.get(f"/api/browse?path={tmp_path}").get_json()["dirs"]]
    assert "visible" in names
    assert ".hidden" not in names


def test_browse_files_not_included(client, tmp_path):
    """Regular files must not appear in the dirs list."""
    (tmp_path / "file.txt").write_text("hello")
    (tmp_path / "subdir").mkdir()
    names = [d["name"] for d in client.get(f"/api/browse?path={tmp_path}").get_json()["dirs"]]
    assert "file.txt" not in names


# ---------------------------------------------------------------------------
# /api/browse — invalid path
# ---------------------------------------------------------------------------

def test_browse_nonexistent_path_walks_up(client):
    # A non-existent deep path should walk up to the nearest existing ancestor
    r = client.get("/api/browse?path=C:/nonexistent_xyz_12345/also_gone/deep")
    assert r.status_code == 200
    data = r.get_json()
    # Should have landed on C:/ or drives listing (no error key)
    assert "error" not in data


def test_browse_disconnected_remembered_path_returns_json(client):
    """If os.path.isdir raises OSError for a remembered path, endpoint must still return JSON."""
    real_isdir = os.path.isdir

    def fake_isdir(path):
        p = str(path).replace("\\", "/").upper()
        if p.startswith("Z:/"):
            raise OSError("device not ready")
        return real_isdir(path)

    with patch("app.os.path.isdir", side_effect=fake_isdir):
        r = client.get("/api/browse?path=Z:/Last/Open/Folder")

    assert r.status_code == 200
    assert r.is_json
    data = r.get_json()
    assert data["path"] == ""
    assert data["parent"] is None
    assert isinstance(data["dirs"], list)


def test_browse_root_ignores_drive_exists_oserror(client):
    """Drive probing errors must be ignored so root browsing still succeeds."""
    real_exists = os.path.exists

    def fake_exists(path):
        if str(path).upper() == "Z:/":
            raise OSError("device not ready")
        return real_exists(path)

    with patch("app.os.path.exists", side_effect=fake_exists):
        r = client.get("/api/browse?path=")

    assert r.status_code == 200
    assert r.is_json
    data = r.get_json()
    assert data["path"] == ""
    assert data["parent"] is None
    assert isinstance(data["dirs"], list)


# ---------------------------------------------------------------------------
# /api/settings — GET
# ---------------------------------------------------------------------------

def test_settings_get_returns_200(client):
    r = client.get("/api/settings")
    assert r.status_code == 200


def test_settings_get_contains_default_keys(client):
    data = client.get("/api/settings").get_json()
    assert "qsv_quality" in data
    assert "sw_hevc_crf" in data
    assert "local_temp_dir" in data


def test_settings_get_default_qsv_quality_is_valid(client):
    """Default qsv_quality should be an int in the accepted range."""
    data = client.get("/api/settings").get_json()
    assert isinstance(data["qsv_quality"], int)
    assert 1 <= data["qsv_quality"] <= 51


# ---------------------------------------------------------------------------
# /api/settings — POST (valid saves)
# ---------------------------------------------------------------------------

def test_settings_post_valid_returns_ok(client):
    r = client.post("/api/settings", json={"qsv_quality": 25, "sw_hevc_crf": 28})
    assert r.status_code == 200
    assert r.get_json() == {"ok": True}


def test_settings_get_reflects_saved_value(client):
    client.post("/api/settings", json={"qsv_quality": 30})
    assert client.get("/api/settings").get_json()["qsv_quality"] == 30


def test_settings_post_persists_sw_hevc_crf(client):
    client.post("/api/settings", json={"sw_hevc_crf": 22})
    assert client.get("/api/settings").get_json()["sw_hevc_crf"] == 22


# ---------------------------------------------------------------------------
# /api/settings — POST (clamping)
# ---------------------------------------------------------------------------

def test_settings_qsv_quality_clamp_low(client):
    """qsv_quality=0 must be clamped up to 1."""
    client.post("/api/settings", json={"qsv_quality": 0})
    assert client.get("/api/settings").get_json()["qsv_quality"] == 1


def test_settings_qsv_quality_clamp_high(client):
    """qsv_quality=100 must be clamped down to 51."""
    client.post("/api/settings", json={"qsv_quality": 100})
    assert client.get("/api/settings").get_json()["qsv_quality"] == 51


def test_settings_sw_hevc_crf_clamp_low(client):
    """sw_hevc_crf=-1 must be clamped up to 0."""
    client.post("/api/settings", json={"sw_hevc_crf": -1})
    assert client.get("/api/settings").get_json()["sw_hevc_crf"] == 0


def test_settings_sw_hevc_crf_clamp_high(client):
    """sw_hevc_crf=100 must be clamped down to 51."""
    client.post("/api/settings", json={"sw_hevc_crf": 100})
    assert client.get("/api/settings").get_json()["sw_hevc_crf"] == 51


def test_settings_qsv_quality_boundary_1(client):
    """qsv_quality=1 is the lower bound and must not be clamped."""
    client.post("/api/settings", json={"qsv_quality": 1})
    assert client.get("/api/settings").get_json()["qsv_quality"] == 1


def test_settings_qsv_quality_boundary_51(client):
    """qsv_quality=51 is the upper bound and must not be clamped."""
    client.post("/api/settings", json={"qsv_quality": 51})
    assert client.get("/api/settings").get_json()["qsv_quality"] == 51


# ---------------------------------------------------------------------------
# /api/settings — POST (invalid)
# ---------------------------------------------------------------------------

def test_settings_post_invalid_qsv_type(client):
    r = client.post("/api/settings", json={"qsv_quality": "abc"})
    assert r.status_code == 400
    assert "error" in r.get_json()


def test_settings_post_invalid_crf_type(client):
    r = client.post("/api/settings", json={"sw_hevc_crf": "bad"})
    assert r.status_code == 400
    assert "error" in r.get_json()


def test_settings_post_invalid_none_value(client):
    r = client.post("/api/settings", json={"qsv_quality": None})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# /api/estimate
# ---------------------------------------------------------------------------

def test_estimate_missing_path_returns_400(client):
    r = client.get("/api/estimate")
    assert r.status_code == 400
    assert "error" in r.get_json()


def test_estimate_empty_path_returns_400(client):
    r = client.get("/api/estimate?path=")
    assert r.status_code == 400


def test_estimate_valid_path_calls_converter(client, tmp_path):
    """Valid path → converter.estimate is called and its result is returned."""
    fake_file = tmp_path / "video.mkv"
    fake_file.write_bytes(b"\x00" * 100)
    mock_result = {
        "estimated_output_mb": 500.0,
        "estimated_saving_mb": 200.0,
        "estimated_saving_pct": 28,
        "error": None,
    }
    with patch("converter.estimate", return_value=mock_result) as mock_est:
        r = client.get(f"/api/estimate?path={fake_file}")

    assert r.status_code == 200
    data = r.get_json()
    assert data["estimated_saving_pct"] == 28
    mock_est.assert_called_once()


def test_estimate_passes_quality_from_settings(client, tmp_path):
    """Quality is read from settings and forwarded to converter.estimate."""
    client.post("/api/settings", json={"qsv_quality": 20})
    fake_file = tmp_path / "video.mkv"
    fake_file.write_bytes(b"\x00" * 100)
    with patch("converter.estimate", return_value={"error": None}) as mock_est:
        client.get(f"/api/estimate?path={fake_file}")
    _, kwargs = mock_est.call_args
    assert kwargs.get("quality") == 20


# ---------------------------------------------------------------------------
# /api/open
# ---------------------------------------------------------------------------

def test_open_missing_path_returns_400(client):
    r = client.get("/api/open")
    assert r.status_code == 400
    assert "error" in r.get_json()


def test_open_empty_path_returns_400(client):
    r = client.get("/api/open?path=")
    assert r.status_code == 400


def test_open_nonexistent_path_returns_404(client):
    r = client.get("/api/open?path=C:/does_not_exist_xyz_12345.mp4")
    assert r.status_code == 404
    assert "error" in r.get_json()


def test_open_play_action_calls_startfile(client, tmp_path):
    """Valid file + default play action → os.startfile is called."""
    f = tmp_path / "video.mkv"
    f.write_bytes(b"\x00" * 4)
    with patch("os.startfile") as mock_sf:
        r = client.get(f"/api/open?path={f}&action=play")
    assert r.status_code == 200
    assert r.get_json() == {"ok": True}
    mock_sf.assert_called_once()


def test_open_default_action_is_play(client, tmp_path):
    """Omitting action= defaults to play (os.startfile)."""
    f = tmp_path / "video.mkv"
    f.write_bytes(b"\x00" * 4)
    with patch("os.startfile") as mock_sf:
        r = client.get(f"/api/open?path={f}")
    assert r.status_code == 200
    mock_sf.assert_called_once()


def test_open_folder_action_for_file_calls_explorer(client, tmp_path):
    """Valid file + action=folder → subprocess.Popen called with explorer."""
    f = tmp_path / "video.mkv"
    f.write_bytes(b"\x00" * 4)
    with patch("subprocess.Popen") as mock_popen:
        r = client.get(f"/api/open?path={f}&action=folder")
    assert r.status_code == 200
    assert r.get_json() == {"ok": True}
    mock_popen.assert_called_once()
    args = mock_popen.call_args[0][0]
    assert "explorer" in args[0].lower()


def test_open_folder_action_for_directory(client, tmp_path):
    """action=folder on a directory itself → os.startfile is called."""
    with patch("os.startfile") as mock_sf:
        r = client.get(f"/api/open?path={tmp_path}&action=folder")
    assert r.status_code == 200
    mock_sf.assert_called_once()


# ---------------------------------------------------------------------------
# /api/eng_stereo_preview
# ---------------------------------------------------------------------------

def test_eng_stereo_preview_preserves_other_audio_tracks(client, tmp_path):
    """English stereo preview should keep non-target audio tracks and replace the first English track."""
    video = tmp_path / "video.mkv"
    video.write_bytes(b"\x00" * 16)

    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        Path(cmd[-1]).write_bytes(b"preview")
        return SimpleNamespace(returncode=0, stderr="")

    with patch("app._probe_audio_streams", return_value=[
        {"index": 1, "language": "eng"},
        {"index": 2, "language": "jpn"},
        {"index": 4, "language": "spa"},
    ]), patch("app._sp.run", side_effect=fake_run):
        r = client.post("/api/eng_stereo_preview", json={"path": str(video)})

    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert data["preview_path"].endswith(".__eng_stereo_preview__.mkv")

    cmd = seen["cmd"]
    assert cmd[:6] == ["ffmpeg", "-y", "-v", "error", "-i", str(video)]
    assert ["-map", "0:v"] == cmd[6:8]
    assert "0:1" in cmd
    assert "0:2" in cmd
    assert "0:4" in cmd
    assert cmd.count("-map") >= 5
    assert ["-c:a", "copy"] in [cmd[i:i+2] for i in range(len(cmd) - 1)]
    assert ["-c:a:0", "aac"] in [cmd[i:i+2] for i in range(len(cmd) - 1)]
    assert ["-ac:a:0", "2"] in [cmd[i:i+2] for i in range(len(cmd) - 1)]
    assert ["-metadata:s:a:0", "title=English Stereo Test"] in [cmd[i:i+2] for i in range(len(cmd) - 1)]
