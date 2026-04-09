"""
tests/test_converter_anime.py
==============================
Unit and integration tests for the Phase 3b anime mode pipeline.

Requires fixture videos — run make_fixtures.ps1 first.
Run:  pytest VideoConverter/tests/test_converter_anime.py -v
"""

import os
import sys
import threading
from pathlib import Path
from unittest.mock import patch, MagicMock
import subprocess

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import converter

FIXTURES = Path(__file__).parent / "fixtures"

pytestmark = pytest.mark.skipif(
    not FIXTURES.exists() or not any(FIXTURES.iterdir()),
    reason="Run VideoConverter/tests/make_fixtures.ps1 first",
)


def _logs():
    msgs = []
    return msgs, msgs.append


# ---------------------------------------------------------------------------
# _aac_encoder
# ---------------------------------------------------------------------------

def test_aac_encoder_windows():
    """`_aac_encoder()` returns 'aac_mf' on win32."""
    with patch("sys.platform", "win32"):
        enc = converter._aac_encoder()
    assert enc == "aac_mf"


def test_aac_encoder_linux():
    """`_aac_encoder()` returns 'aac' on non-win32."""
    with patch("sys.platform", "linux"):
        enc = converter._aac_encoder()
    assert enc == "aac"


# ---------------------------------------------------------------------------
# is_hi10
# ---------------------------------------------------------------------------

def test_is_hi10_true():
    """`is_hi10()` returns True for the 10-bit H.264 fixture."""
    assert converter.is_hi10(str(FIXTURES / "h264_hi10.mkv")) is True


def test_is_hi10_false_8bit():
    """`is_hi10()` returns False for a normal 8-bit H.264 fixture."""
    assert converter.is_hi10(str(FIXTURES / "h264_short.mkv")) is False


def test_is_hi10_false_missing():
    """`is_hi10()` returns False gracefully for a non-existent file."""
    assert converter.is_hi10("/does/not/exist.mkv") is False


# ---------------------------------------------------------------------------
# _is_potentially_english
# ---------------------------------------------------------------------------

def test_english_sub_protection_eng_tag():
    assert converter._is_potentially_english("eng", "") is True


def test_english_sub_protection_en_tag():
    assert converter._is_potentially_english("en", "") is True


def test_english_sub_protection_und_tag():
    assert converter._is_potentially_english("und", "") is True


def test_english_sub_protection_empty_tag():
    """Empty / missing language tag defaults to keep."""
    assert converter._is_potentially_english("", "") is True


def test_english_sub_protection_foreign():
    """Japanese-tagged sub without English keyword → drop."""
    assert converter._is_potentially_english("jpn", "Japanese") is False


def test_english_sub_protection_keyword_in_title():
    """'English' in title keeps the track even with foreign lang tag."""
    assert converter._is_potentially_english("jpn", "Full English") is True


def test_english_sub_protection_signs():
    """'signs' keyword → keep."""
    assert converter._is_potentially_english("jpn", "Signs & Songs") is True


# ---------------------------------------------------------------------------
# remux_to_mp4 — MP4 fast-path
# ---------------------------------------------------------------------------

def test_remux_mp4_fastpath(tmp_path):
    """h264_mp4_aac.mp4 already has AAC and no bitmap subs → compress_simple called."""
    out_dir = str(tmp_path / "out")
    msgs, log = _logs()
    stop = threading.Event()

    ok, enc = converter.remux_to_mp4(
        str(FIXTURES / "h264_mp4_aac.mp4"),
        out_dir, log, stop,
    )
    assert ok, f"Expected ok=True for MP4 fast-path; logs: {msgs}"
    assert any("fast-path" in m.lower() for m in msgs)
    # Output is .mp4 when fast-path delegates to compress_simple
    out = list(Path(out_dir).glob("*.mp4"))
    assert out, "Fast-path should produce .mp4 output"


# ---------------------------------------------------------------------------
# remux_to_mp4 — MKV with multi-track (h264_multitrack.mkv)
# ---------------------------------------------------------------------------

def test_remux_mkv_multitrack(tmp_path):
    """h264_multitrack.mkv should be remuxed to .mp4 with AAC audio."""
    out_dir = str(tmp_path / "out")
    msgs, log = _logs()
    stop = threading.Event()

    ok, enc = converter.remux_to_mp4(
        str(FIXTURES / "h264_multitrack.mkv"),
        out_dir, log, stop,
    )
    assert ok, f"Remux of multitrack MKV failed; logs: {msgs}"

    out_files = list(Path(out_dir).glob("*.mp4"))
    assert out_files, "Expected .mp4 output"
    out_path = str(out_files[0])

    # Probe output: confirm audio is AAC
    result = subprocess.run(
        [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
            out_path,
        ],
        capture_output=True, text=True, timeout=30,
    )
    import json
    data = json.loads(result.stdout)
    audio_codecs = [
        s.get("codec_name", "").lower()
        for s in data.get("streams", [])
        if s.get("codec_type") == "audio"
    ]
    assert audio_codecs, "Expected at least one audio stream"
    assert all(c in ("aac", "aac_latm") for c in audio_codecs), (
        f"Expected all audio AAC, got: {audio_codecs}"
    )


# ---------------------------------------------------------------------------
# remux_to_mp4 — Hi10 path (h264_hi10.mkv)
# ---------------------------------------------------------------------------

def test_remux_hi10(tmp_path):
    """h264_hi10.mkv should be remuxed with video stream copied (no re-encode)."""
    out_dir = str(tmp_path / "out")
    msgs, log = _logs()
    stop = threading.Event()

    ok, enc = converter.remux_to_mp4(
        str(FIXTURES / "h264_hi10.mkv"),
        out_dir, log, stop,
        hi10=True,
    )
    assert ok, f"Hi10 remux failed; logs: {msgs}"
    assert enc == "copy", f"Expected encoder 'copy', got '{enc}'"

    out_files = list(Path(out_dir).glob("*.mp4"))
    assert out_files, "Expected .mp4 output"
    out_path = str(out_files[0])

    # Video stream should still be H.264 (copied, not re-encoded)
    result = subprocess.run(
        [
            "ffprobe", "-v", "quiet",
            "-select_streams", "v:0",
            "-print_format", "json",
            "-show_streams",
            out_path,
        ],
        capture_output=True, text=True, timeout=30,
    )
    import json
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    assert streams, "Expected video stream in output"
    assert streams[0].get("codec_name", "").lower() == "h264", (
        "Hi10 video must remain H.264 (copied, not re-encoded)"
    )


# ---------------------------------------------------------------------------
# stop event during remux
# ---------------------------------------------------------------------------

def test_stop_during_remux(tmp_path):
    """Setting stop_event before remux starts causes it to return False."""
    out_dir = str(tmp_path / "out")
    msgs, log = _logs()
    stop = threading.Event()
    stop.set()

    ok, enc = converter.remux_to_mp4(
        str(FIXTURES / "h264_multitrack.mkv"),
        out_dir, log, stop,
    )
    assert ok is False, "Expected False when stop_event is already set"


# ---------------------------------------------------------------------------
# convert_video dispatcher — anime mode
# ---------------------------------------------------------------------------

def test_convert_video_anime_hi10(tmp_path):
    """convert_video with anime_mode=True and Hi10 source uses remux path."""
    out_dir = str(tmp_path / "out")
    msgs, log = _logs()
    stop = threading.Event()

    result = converter.convert_video(
        input_path=str(FIXTURES / "h264_hi10.mkv"),
        output_dir=out_dir,
        anime_mode=True,
        quality=None,
        progress_cb=None,
        stop_event=stop,
        log=log,
    )
    assert result["ok"] is True, f"Expected ok=True; logs: {msgs}; error: {result.get('error')}"
    assert result["output_path"] is not None
    assert result["output_path"].endswith(".mp4")
    assert result["encoder_used"] == "copy"


def test_convert_video_anime_normal(tmp_path):
    """convert_video with anime_mode=True and 8-bit source uses compress+remux."""
    out_dir = str(tmp_path / "out")
    msgs, log = _logs()
    stop = threading.Event()

    result = converter.convert_video(
        input_path=str(FIXTURES / "h264_multitrack.mkv"),
        output_dir=out_dir,
        anime_mode=True,
        quality=None,
        progress_cb=None,
        stop_event=stop,
        log=log,
    )
    assert result["ok"] is True, f"Expected ok=True; logs: {msgs}; error: {result.get('error')}"
    assert result["output_path"] is not None
    assert result["output_path"].endswith(".mp4")
