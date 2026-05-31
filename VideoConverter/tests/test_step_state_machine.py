"""Unit tests for _process_step_log() step state machine."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import app as _app

_BASE_H264 = [
    {"id": "ocr",      "label": "OCR",      "state": "done",    "detail": "", "attempt": 1},
    {"id": "audio",    "label": "Audio",    "state": "waiting", "detail": "", "attempt": 1},
    {"id": "compress", "label": "Compress", "state": "waiting", "detail": "", "attempt": 1},
    {"id": "remux",    "label": "Remux",    "state": "waiting", "detail": "", "attempt": 1},
    {"id": "verify",   "label": "Verify",   "state": "waiting", "detail": "", "attempt": 1},
]

_BASE_AV1 = [
    {"id": "ocr",    "label": "OCR",    "state": "skipped", "detail": "No PGS tracks", "attempt": 1},
    {"id": "audio",  "label": "Audio",  "state": "waiting", "detail": "",              "attempt": 1},
    {"id": "remux",  "label": "Remux",  "state": "waiting", "detail": "AV1 \u2192 MP4", "attempt": 1},
    {"id": "verify", "label": "Verify", "state": "waiting", "detail": "",              "attempt": 1},
]


def _reset(base):
    with _app._job_lock:
        _app._job["steps"] = [dict(s) for s in base]


def _get(step_id):
    with _app._job_lock:
        for s in _app._job["steps"]:
            if s["id"] == step_id:
                return dict(s)
    return None


def _fire(line, cnt):
    _app._process_step_log(line, cnt)


# --- H.264 anime path ---

def test_compress_start():
    _reset(_BASE_H264)
    cnt = [0]
    _fire("Anime mode: compressing then remuxing to MP4.", cnt)
    s = _get("compress")
    assert s["state"] == "running"
    assert s["detail"] == "auto"


def test_compress_switches_to_software_for_hi10():
    _reset(_BASE_H264)
    cnt = [0]
    _fire("Anime mode: compressing then remuxing to MP4.", cnt)
    _fire("Hi10 H.264 detected — QSV unsupported, will use libx265 software encoder.", cnt)
    s = _get("compress")
    assert s["state"] == "running"
    assert s["detail"] == "libx265 (software)"


def test_compress_switches_to_software_on_qsv_fallback():
    _reset(_BASE_H264)
    cnt = [0]
    _fire("Anime mode: compressing then remuxing to MP4.", cnt)
    _fire("QSV failed, trying software encoder...", cnt)
    s = _get("compress")
    assert s["state"] == "running"
    assert s["detail"] == "libx265 (software fallback)"


def test_remux_after_compress():
    _reset(_BASE_H264)
    cnt = [0]
    _fire("Anime mode: compressing then remuxing to MP4.", cnt)
    _fire("Remuxing compressed output to MP4...", cnt)
    assert _get("compress")["state"] == "done"
    r = _get("remux")
    assert r["state"] == "running"
    assert cnt[0] == 1
    assert "1/6" in r["detail"]


def test_direct_remux_sets_attempt():
    _reset(_BASE_H264)
    cnt = [0]
    _fire("Remuxing to MP4...", cnt)
    r = _get("remux")
    assert r["state"] == "running"
    assert cnt[0] == 1


def test_dts_retry():
    _reset(_BASE_H264)
    cnt = [1]
    _fire("DTS overflow detected \u2014 retrying with -max_interleave_delta 0", cnt)
    r = _get("remux")
    assert r["state"] == "retry"
    assert cnt[0] == 2
    assert "2/6" in r["detail"]
    assert "DTS" in r["detail"]


def test_genpts_retry():
    _reset(_BASE_H264)
    cnt = [2]
    _fire("DTS fix retry \u2014 trying -fflags +genpts -avoid_negative_ts make_zero", cnt)
    r = _get("remux")
    assert r["state"] == "retry"
    assert cnt[0] == 3
    assert "genpts" in r["detail"]


def test_aac_mux_failed():
    _reset(_BASE_H264)
    cnt = [2]
    _fire("AAC mux failed \u2014 pre-encoding audio tracks individually with native aac...", cnt)
    assert _get("audio")["state"] == "running"
    r = _get("remux")
    assert r["state"] == "retry"
    assert cnt[0] == 3


def test_sub_dts_fix():
    _reset(_BASE_H264)
    cnt = [3]
    _fire("Subtitle DTS fix \u2014 pre-extracting text subs to SRT for cleaner muxing...", cnt)
    r = _get("remux")
    assert r["state"] == "retry"
    assert cnt[0] == 4
    assert "SRT" in r["detail"]


def test_no_subs_retry():
    _reset(_BASE_H264)
    cnt = [4]
    _fire("Retrying without subtitle streams", cnt)
    assert cnt[0] == 5
    r = _get("remux")
    assert r["state"] == "retry"


def test_done():
    _reset(_BASE_H264)
    cnt = [1]
    _fire("Done. Saved 59.0 MB \u2192 /some/path.mp4", cnt)
    assert _get("remux")["state"] == "done"
    assert _get("audio")["state"] == "done"
    assert _get("verify")["state"] == "running"
    _fire("Integrity check passed.", cnt)
    assert _get("verify")["state"] == "done"


def test_integrity_failed():
    _reset(_BASE_H264)
    cnt = [1]
    _fire("Integrity check failed: audio stream missing", cnt)
    v = _get("verify")
    assert v["state"] == "failed"
    assert "audio stream" in v["detail"]


# --- AV1 path ---

def test_av1_remux():
    _reset(_BASE_AV1)
    cnt = [0]
    _fire("AV1 source \u2014 stream-copying video into MP4 (no re-encode).", cnt)
    r = _get("remux")
    assert r["state"] == "running"
    assert "AV1" in r["detail"]
    assert cnt[0] == 1


def test_audio_track_progress():
    _reset(_BASE_H264)
    cnt = [0]
    _fire("Pre-encoding audio track 1/3 to AAC...", cnt)
    a = _get("audio")
    assert a["state"] == "running"
    assert "1/3" in a["detail"]
