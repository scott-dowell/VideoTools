"""Unit tests for _build_steps() step list construction."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import config
from app import _build_steps

H264_PGS = {
    "streams": {
        "video": {"codec": "h264"},
        "audio": [{"codec": "aac"}],
        "subs":  [{"codec": "PGS", "index": 2}],
    }
}
H264_ASS = {
    "streams": {
        "video": {"codec": "h264"},
        "audio": [{"codec": "aac"}],
        "subs":  [{"codec": "ass", "index": 1}],
    }
}
AV1_FILE = {
    "streams": {
        "video": {"codec": "av1"},
        "audio": [{"codec": "opus"}],
        "subs":  [{"codec": "ass", "index": 1}],
    }
}
HEVC_FILE = {
    "streams": {
        "video": {"codec": "hevc"},
        "audio": [{"codec": "aac"}],
        "subs":  [],
    }
}
NON_ANIME = {
    "streams": {
        "video": {"codec": "h264"},
        "audio": [{"codec": "ac3"}],
        "subs":  [],
    }
}


def test_h264_anime_with_pgs():
    steps = _build_steps(H264_PGS, anime_mode=True)
    ids = [s["id"] for s in steps]
    assert ids == ["ocr", "estimate", "compress", "audio", "remux", "verify"]
    ocr = next(s for s in steps if s["id"] == "ocr")
    assert ocr["state"] == "waiting"


def test_h264_anime_no_pgs():
    steps = _build_steps(H264_ASS, anime_mode=True)
    ids = [s["id"] for s in steps]
    assert ids == ["ocr", "estimate", "compress", "audio", "remux", "verify"]
    ocr = next(s for s in steps if s["id"] == "ocr")
    assert ocr["state"] == "skipped"
    assert ocr["detail"] == "No PGS tracks"


def test_av1_anime():
    steps = _build_steps(AV1_FILE, anime_mode=True)
    ids = [s["id"] for s in steps]
    assert "remux" in ids
    remux = next(s for s in steps if s["id"] == "remux")
    if bool(getattr(config, "REENCODE_AV1", True)):
        assert ids == ["ocr", "estimate", "compress", "audio", "remux", "verify"]
        assert remux["detail"] == "MKV → MP4"
    else:
        assert "compress" not in ids
        assert "AV1" in remux["detail"]


def test_hevc_anime_no_pgs():
    steps = _build_steps(HEVC_FILE, anime_mode=True)
    ids = [s["id"] for s in steps]
    assert "compress" not in ids
    assert "remux" in ids
    remux = next(s for s in steps if s["id"] == "remux")
    assert "HEVC" in remux["detail"]
    ocr = next(s for s in steps if s["id"] == "ocr")
    assert ocr["state"] == "skipped"


def test_non_anime():
    steps = _build_steps(NON_ANIME, anime_mode=False)
    ids = [s["id"] for s in steps]
    assert ids == ["estimate", "compress", "verify"]
    assert all(s["state"] == "waiting" for s in steps)


def test_all_steps_have_required_keys():
    for fi, am in [(H264_PGS, True), (AV1_FILE, True), (NON_ANIME, False)]:
        for s in _build_steps(fi, am):
            assert "id" in s
            assert "label" in s
            assert "state" in s
            assert "detail" in s
            assert "attempt" in s
