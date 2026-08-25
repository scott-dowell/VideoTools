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


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    """Point db helpers at a temp sqlite file for this test module."""
    orig = flask_app.DB_PATH
    test_db = str(tmp_path / "routes_other.db")
    flask_app.db.init_db(test_db)
    yield
    flask_app.db.init_db(orig)


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


def test_index_folder_modal_is_selection_only_and_actions_are_in_controls(client):
    """Folder browser modal should only select a folder; actions belong to main controls."""
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    # Modal keeps only folder selection/confirmation controls.
    assert 'id="confirmFolderBtn"' in html
    assert 'id="modalLoadBtn"' not in html
    assert 'id="modalPrepBtn"' not in html
    assert 'id="modalAnalyseBtn"' not in html
    assert 'id="modalCleanupBtn"' not in html

    # Folder actions live in the main controls area.
    assert 'id="rescanBtn"' in html
    assert 'id="loadBtn"' in html
    assert 'id="cleanupBtn"' in html
    assert 'id="analyseBtn"' in html
    assert 'id="prepBtn"' in html


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


# ---------------------------------------------------------------------------
# /api/batch_edit_plan
# ---------------------------------------------------------------------------

def test_batch_edit_plan_create_missing_path_returns_400(client):
    r = client.post("/api/batch_edit_plan/create", json={})
    assert r.status_code == 400
    assert "error" in r.get_json()


def test_batch_edit_plan_create_and_get(client, tmp_path):
    folder = tmp_path / "season"
    folder.mkdir()
    rep = folder / "ep01.mkv"
    peer_ok = folder / "ep02.mkv"
    peer_bad = folder / "special.mkv"
    rep.write_bytes(b"\x00")
    peer_ok.write_bytes(b"\x00")
    peer_bad.write_bytes(b"\x00")

    rep_sig = {
        "path": str(rep),
        "ext": ".mkv",
        "video": {"codec": "h264", "profile": "high", "resolution": "1920x1080"},
        "audio": [{"index": 10, "track": 0, "codec": "aac", "language": "en", "channels": 6, "title_norm": "main"}],
        "subs": [{"index": 20, "track": 0, "codec": "pgs", "language": "ja", "title_norm": "full"}],
    }
    ok_sig = {
        "path": str(peer_ok),
        "ext": ".mkv",
        "video": {"codec": "h264", "profile": "high", "resolution": "1920x1080"},
        "audio": [{"index": 11, "track": 0, "codec": "aac", "language": "en", "channels": 6, "title_norm": "main"}],
        "subs": [{"index": 21, "track": 0, "codec": "pgs", "language": "ja", "title_norm": "different title"}],
    }
    bad_sig = {
        "path": str(peer_bad),
        "ext": ".mkv",
        "video": {"codec": "h264", "profile": "high", "resolution": "1920x1080"},
        "audio": [{"index": 12, "track": 0, "codec": "aac", "language": "en", "channels": 2, "title_norm": "main"}],
        "subs": [{"index": 22, "track": 0, "codec": "pgs", "language": "ja", "title_norm": "full"}],
    }
    sig_map = {
        str(rep): rep_sig,
        str(peer_ok): ok_sig,
        str(peer_bad): bad_sig,
    }

    with patch("app._collect_folder_video_paths", return_value=[str(rep), str(peer_ok), str(peer_bad)]), \
         patch("app._build_stream_signature", side_effect=lambda p: sig_map.get(str(p))), \
         patch("app.db.get_dropped_streams", return_value=[10]):
        r = client.post("/api/batch_edit_plan/create", json={
            "path": str(rep),
            "scope_mode": "matching_folder",
            "include_eng_stereo": True,
        })

    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert data["summary"]["total_files"] == 3
    assert data["summary"]["compatible"] == 2
    assert data["summary"]["excluded"] == 1

    plan_id = data["plan_id"]
    r2 = client.get(f"/api/batch_edit_plan/{plan_id}")
    assert r2.status_code == 200
    data2 = r2.get_json()
    assert data2["ok"] is True
    assert data2["summary"]["compatible"] == 2
    assert data2["summary"]["excluded"] == 1
    assert data2["plan"]["plan_json"]["include_eng_stereo"] is True
    assert len(data2["plan"]["plan_json"]["drop_selectors"]) == 1

    reasons = {f["source_path"]: (f["match_state"], f.get("match_reason") or "") for f in data2["files"]}
    assert reasons[str(peer_bad).replace("\\", "/")][0] == "excluded"
    assert "audio channels mismatch" in reasons[str(peer_bad).replace("\\", "/")][1]


def test_batch_edit_plan_build_previews_resolves_drop_selectors_per_file(client, tmp_path):
    folder = tmp_path / "season"
    folder.mkdir()
    rep = folder / "ep01.mkv"
    peer = folder / "ep02.mkv"
    rep.write_bytes(b"\x00")
    peer.write_bytes(b"\x00")

    rep_sig = {
        "path": str(rep),
        "ext": ".mkv",
        "video": {"codec": "h264", "profile": "high", "resolution": "1920x1080"},
        "audio": [{"index": 10, "track": 0, "codec": "aac", "language": "en", "channels": 6, "title_norm": "main"}],
        "subs": [{"index": 20, "track": 0, "codec": "pgs", "language": "ja", "title_norm": "full"}],
    }
    peer_sig = {
        "path": str(peer),
        "ext": ".mkv",
        "video": {"codec": "h264", "profile": "high", "resolution": "1920x1080"},
        "audio": [{"index": 11, "track": 0, "codec": "aac", "language": "en", "channels": 6, "title_norm": "main"}],
        "subs": [{"index": 21, "track": 0, "codec": "pgs", "language": "ja", "title_norm": "full"}],
    }
    sig_map = {
        str(rep): rep_sig,
        str(peer): peer_sig,
    }

    with patch("app._collect_folder_video_paths", return_value=[str(rep), str(peer)]), \
         patch("app._build_stream_signature", side_effect=lambda p: sig_map.get(str(p))), \
         patch("app.db.get_dropped_streams", return_value=[10]):
        create_resp = client.post("/api/batch_edit_plan/create", json={
            "path": str(rep),
            "scope_mode": "matching_folder",
            "include_eng_stereo": False,
        })

    assert create_resp.status_code == 200
    plan_id = create_resp.get_json()["plan_id"]

    class _ImmediateThread:
        def __init__(self, target=None, args=(), kwargs=None, **_):
            self._target = target
            self._args = args
            self._kwargs = kwargs or {}

        def start(self):
            if self._target:
                self._target(*self._args, **self._kwargs)

    seen = []

    def _fake_stream_preview(path, dropped_indices, persist_selection=True):
        seen.append((path, list(dropped_indices), persist_selection))
        return True, ""

    with patch("app.threading.Thread", side_effect=lambda *a, **k: _ImmediateThread(*a, **k)), \
         patch("app._build_stream_signature", side_effect=lambda p: sig_map.get(str(p))), \
         patch("app._create_stream_edit_preview", side_effect=_fake_stream_preview):
        build_resp = client.post(f"/api/batch_edit_plan/{plan_id}/build_previews", json={})

    assert build_resp.status_code == 200
    assert build_resp.get_json()["ok"] is True
    assert len(seen) == 2

    seen_map = {Path(p).name: dropped for p, dropped, _persist in seen}
    assert seen_map["ep01.mkv"] == [10]
    assert seen_map["ep02.mkv"] == [11]

    status_resp = client.get(f"/api/batch_edit_plan/{plan_id}/build_previews/status")
    assert status_resp.status_code == 200
    status_data = status_resp.get_json()
    assert status_data["summary"]["preview_ready"] == 2
    assert status_data["summary"]["preview_failed"] == 0
    assert status_data["worker"]["state"] == "done"


def test_batch_edit_plan_build_previews_records_failures(client, tmp_path):
    folder = tmp_path / "season"
    folder.mkdir()
    rep = folder / "ep01.mkv"
    peer = folder / "ep02.mkv"
    rep.write_bytes(b"\x00")
    peer.write_bytes(b"\x00")

    rep_sig = {
        "path": str(rep),
        "ext": ".mkv",
        "video": {"codec": "h264", "profile": "high", "resolution": "1920x1080"},
        "audio": [{"index": 10, "track": 0, "codec": "aac", "language": "en", "channels": 6, "title_norm": "main"}],
        "subs": [],
    }
    peer_sig = {
        "path": str(peer),
        "ext": ".mkv",
        "video": {"codec": "h264", "profile": "high", "resolution": "1920x1080"},
        "audio": [{"index": 11, "track": 0, "codec": "aac", "language": "en", "channels": 6, "title_norm": "main"}],
        "subs": [],
    }
    sig_map = {
        str(rep): rep_sig,
        str(peer): peer_sig,
    }

    with patch("app._collect_folder_video_paths", return_value=[str(rep), str(peer)]), \
         patch("app._build_stream_signature", side_effect=lambda p: sig_map.get(str(p))), \
         patch("app.db.get_dropped_streams", return_value=[10]):
        create_resp = client.post("/api/batch_edit_plan/create", json={
            "path": str(rep),
            "scope_mode": "matching_folder",
            "include_eng_stereo": True,
        })

    assert create_resp.status_code == 200
    plan_id = create_resp.get_json()["plan_id"]

    class _ImmediateThread:
        def __init__(self, target=None, args=(), kwargs=None, **_):
            self._target = target
            self._args = args
            self._kwargs = kwargs or {}

        def start(self):
            if self._target:
                self._target(*self._args, **self._kwargs)

    def _fake_eng_preview(path):
        if Path(path).name == "ep02.mkv":
            return False, "No English audio track found"
        return True, ""

    with patch("app.threading.Thread", side_effect=lambda *a, **k: _ImmediateThread(*a, **k)), \
         patch("app._build_stream_signature", side_effect=lambda p: sig_map.get(str(p))), \
         patch("app._create_stream_edit_preview", return_value=(True, "")), \
         patch("app._create_eng_stereo_preview", side_effect=_fake_eng_preview):
        build_resp = client.post(f"/api/batch_edit_plan/{plan_id}/build_previews", json={})

    assert build_resp.status_code == 200

    get_resp = client.get(f"/api/batch_edit_plan/{plan_id}")
    assert get_resp.status_code == 200
    payload = get_resp.get_json()
    assert payload["summary"]["preview_ready"] == 1
    assert payload["summary"]["preview_failed"] == 1

    rows = {Path(f["source_path"]).name: f for f in payload["files"]}
    assert rows["ep01.mkv"]["preview_state"] == "ready"
    assert rows["ep02.mkv"]["preview_state"] == "failed"
    assert "eng stereo preview" in (rows["ep02.mkv"].get("preview_error") or "")


def test_batch_edit_plan_build_previews_missing_plan_returns_404(client):
    r = client.post("/api/batch_edit_plan/999999/build_previews", json={})
    assert r.status_code == 404
    assert "error" in r.get_json()


def test_batch_edit_plan_discard_previews_marks_discarded(client, tmp_path):
    folder = tmp_path / "season"
    folder.mkdir()
    rep = folder / "ep01.mkv"
    peer = folder / "ep02.mkv"
    rep.write_bytes(b"\x00")
    peer.write_bytes(b"\x00")

    rep_sig = {
        "path": str(rep),
        "ext": ".mkv",
        "video": {"codec": "h264", "profile": "high", "resolution": "1920x1080"},
        "audio": [{"index": 10, "track": 0, "codec": "aac", "language": "en", "channels": 6, "title_norm": "main"}],
        "subs": [],
    }
    peer_sig = {
        "path": str(peer),
        "ext": ".mkv",
        "video": {"codec": "h264", "profile": "high", "resolution": "1920x1080"},
        "audio": [{"index": 11, "track": 0, "codec": "aac", "language": "en", "channels": 6, "title_norm": "main"}],
        "subs": [],
    }
    sig_map = {str(rep): rep_sig, str(peer): peer_sig}

    with patch("app._collect_folder_video_paths", return_value=[str(rep), str(peer)]), \
         patch("app._build_stream_signature", side_effect=lambda p: sig_map.get(str(p))), \
         patch("app.db.get_dropped_streams", return_value=[10]):
        created = client.post("/api/batch_edit_plan/create", json={
            "path": str(rep),
            "scope_mode": "matching_folder",
            "include_eng_stereo": False,
        })

    plan_id = created.get_json()["plan_id"]
    flask_app.db.update_batch_edit_plan_file_state(plan_id, str(rep), preview_state="ready", updated_at="2026-07-26T00:00:00Z")
    flask_app.db.update_batch_edit_plan_file_state(plan_id, str(peer), preview_state="ready", updated_at="2026-07-26T00:00:00Z")

    with patch("app._discard_stream_edit_preview", return_value=(True, "")), \
         patch("app._discard_eng_stereo_preview", return_value=(True, "")):
        r = client.post(f"/api/batch_edit_plan/{plan_id}/discard_previews", json={"all_ready": True})

    assert r.status_code == 200
    data = r.get_json()
    assert data["summary"]["selected"] == 2
    assert data["summary"]["discarded"] == 2
    assert data["summary"]["failed"] == 0

    got = client.get(f"/api/batch_edit_plan/{plan_id}").get_json()
    assert got["summary"]["preview_ready"] == 0
    rows = {Path(f["source_path"]).name: f for f in got["files"]}
    assert rows["ep01.mkv"]["preview_state"] == "discarded"
    assert rows["ep02.mkv"]["preview_state"] == "discarded"


def test_batch_edit_plan_accept_preview_replaces_one_ready_file(client, tmp_path):
    folder = tmp_path / "season"
    folder.mkdir()
    rep = folder / "ep01.mkv"
    peer = folder / "ep02.mkv"
    rep.write_bytes(b"\x00")
    peer.write_bytes(b"\x00")

    rep_sig = {
        "path": str(rep),
        "ext": ".mkv",
        "video": {"codec": "h264", "profile": "high", "resolution": "1920x1080"},
        "audio": [{"index": 10, "track": 0, "codec": "aac", "language": "en", "channels": 6, "title_norm": "main"}],
        "subs": [],
    }
    peer_sig = {
        "path": str(peer),
        "ext": ".mkv",
        "video": {"codec": "h264", "profile": "high", "resolution": "1920x1080"},
        "audio": [{"index": 11, "track": 0, "codec": "aac", "language": "en", "channels": 6, "title_norm": "main"}],
        "subs": [],
    }
    sig_map = {str(rep): rep_sig, str(peer): peer_sig}

    with patch("app._collect_folder_video_paths", return_value=[str(rep), str(peer)]), \
         patch("app._build_stream_signature", side_effect=lambda p: sig_map.get(str(p))), \
         patch("app.db.get_dropped_streams", return_value=[10]):
        created = client.post("/api/batch_edit_plan/create", json={
            "path": str(rep),
            "scope_mode": "matching_folder",
            "include_eng_stereo": False,
        })

    plan_id = created.get_json()["plan_id"]
    flask_app.db.update_batch_edit_plan_file_state(plan_id, str(rep), preview_state="ready", updated_at="2026-07-26T00:00:00Z")

    with patch("app._accept_plan_preview_for_source", return_value=(True, {"workflow": "stream_edit", "backup_path": ""})):
        r = client.post(f"/api/batch_edit_plan/{plan_id}/accept_preview", json={"source_path": str(rep)})

    assert r.status_code == 200
    out = r.get_json()
    assert out["ok"] is True
    assert out["workflow"] == "stream_edit"

    got = client.get(f"/api/batch_edit_plan/{plan_id}").get_json()
    rows = {Path(f["source_path"]).name: f for f in got["files"]}
    assert rows["ep01.mkv"]["replace_state"] == "replaced"
    assert rows["ep01.mkv"]["preview_state"] == "none"


def test_batch_edit_plan_accept_all_ready_mixed_results(client, tmp_path):
    folder = tmp_path / "season"
    folder.mkdir()
    rep = folder / "ep01.mkv"
    peer = folder / "ep02.mkv"
    rep.write_bytes(b"\x00")
    peer.write_bytes(b"\x00")

    rep_sig = {
        "path": str(rep),
        "ext": ".mkv",
        "video": {"codec": "h264", "profile": "high", "resolution": "1920x1080"},
        "audio": [{"index": 10, "track": 0, "codec": "aac", "language": "en", "channels": 6, "title_norm": "main"}],
        "subs": [],
    }
    peer_sig = {
        "path": str(peer),
        "ext": ".mkv",
        "video": {"codec": "h264", "profile": "high", "resolution": "1920x1080"},
        "audio": [{"index": 11, "track": 0, "codec": "aac", "language": "en", "channels": 6, "title_norm": "main"}],
        "subs": [],
    }
    sig_map = {str(rep): rep_sig, str(peer): peer_sig}

    with patch("app._collect_folder_video_paths", return_value=[str(rep), str(peer)]), \
         patch("app._build_stream_signature", side_effect=lambda p: sig_map.get(str(p))), \
         patch("app.db.get_dropped_streams", return_value=[10]):
        created = client.post("/api/batch_edit_plan/create", json={
            "path": str(rep),
            "scope_mode": "matching_folder",
            "include_eng_stereo": False,
        })

    plan_id = created.get_json()["plan_id"]
    flask_app.db.update_batch_edit_plan_file_state(plan_id, str(rep), preview_state="ready", updated_at="2026-07-26T00:00:00Z")
    flask_app.db.update_batch_edit_plan_file_state(plan_id, str(peer), preview_state="ready", updated_at="2026-07-26T00:00:00Z")

    def _accept_side_effect(path):
        if Path(path).name == "ep02.mkv":
            return False, {"error": "replace failed", "status": 500}
        return True, {"workflow": "stream_edit", "backup_path": ""}

    with patch("app._accept_plan_preview_for_source", side_effect=_accept_side_effect):
        r = client.post(f"/api/batch_edit_plan/{plan_id}/accept_all_ready", json={})

    assert r.status_code == 200
    data = r.get_json()
    assert data["summary"]["selected"] == 2
    assert data["summary"]["replaced"] == 1
    assert data["summary"]["failed"] == 1
    assert len(data["failures"]) == 1

    got = client.get(f"/api/batch_edit_plan/{plan_id}").get_json()
    rows = {Path(f["source_path"]).name: f for f in got["files"]}
    assert rows["ep01.mkv"]["replace_state"] == "replaced"
    assert rows["ep02.mkv"]["replace_state"] == "failed"
    assert "replace failed" in (rows["ep02.mkv"].get("preview_error") or "")
