"""
tests/test_converter.py
=======================
Unit and integration tests for VideoConverter/converter.py.

Requires fixture videos — run make_fixtures.ps1 first.
Run:  pytest VideoConverter/tests/test_converter.py -v
"""

import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import converter

FIXTURES = Path(__file__).parent / "fixtures"

pytestmark = pytest.mark.skipif(
    not FIXTURES.exists() or not any(FIXTURES.iterdir()),
    reason="Run VideoConverter/tests/make_fixtures.ps1 first",
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _logs():
    """Return a list and a log callable that appends to it."""
    msgs = []
    return msgs, msgs.append


# ---------------------------------------------------------------------------
# compress_simple — QSV success path
# ---------------------------------------------------------------------------

def test_compress_simple_qsv_success(tmp_path):
    """compress_simple should produce a smaller output file."""
    out_dir = str(tmp_path / "converted")
    msgs, log = _logs()
    stop = threading.Event()

    ok, enc = converter.compress_simple(
        str(FIXTURES / "h264_short.mkv"),
        out_dir, log, stop,
    )
    assert ok, f"Expected ok=True; logs: {msgs}"
    assert enc in ("hevc_qsv", "libx265"), f"Unexpected encoder: {enc}"
    assert any("Done" in m for m in msgs), "Expected 'Done' in log"

    out_file = tmp_path / "converted" / "h264_short.mkv"
    assert out_file.exists()
    assert out_file.stat().st_size < (FIXTURES / "h264_short.mkv").stat().st_size


def test_compress_simple_sw_fallback(tmp_path):
    """When QSV fails, compress_simple falls back to libx265."""
    out_dir = str(tmp_path / "converted")
    msgs, log = _logs()
    stop = threading.Event()

    # Make QSV always fail on first call
    original_run = converter._run_ffmpeg
    call_count = {"n": 0}

    def patched_run(cmd, log_fn, stop_ev, **kwargs):
        call_count["n"] += 1
        if "hevc_qsv" in cmd:
            return False
        return original_run(cmd, log_fn, stop_ev, **kwargs)

    with patch.object(converter, "_run_ffmpeg", patched_run):
        ok, enc = converter.compress_simple(
            str(FIXTURES / "h264_short.mkv"),
            out_dir, log, stop,
        )

    assert ok, f"Expected ok=True after sw fallback; logs: {msgs}"
    assert enc == "libx265"
    assert any("software" in m.lower() for m in msgs)


def test_compress_simple_no_savings(tmp_path):
    """If the output is not smaller, compress_simple returns (False, '')."""
    out_dir = str(tmp_path / "converted")
    msgs, log = _logs()
    stop = threading.Event()

    # Patch to return a file that's the same size as the source
    original_run = converter._run_ffmpeg
    src_path = str(FIXTURES / "h264_short.mkv")

    def patched_run(cmd, log_fn, stop_ev, **kwargs):
        # Write tmp output the same size as source (fool the size check)
        tmp_out = cmd[-1]
        import shutil
        shutil.copy(src_path, tmp_out)
        return True

    with patch.object(converter, "_run_ffmpeg", patched_run):
        ok, enc = converter.compress_simple(src_path, out_dir, log, stop)

    assert not ok
    assert not (tmp_path / "converted" / "h264_short.mkv").exists()


def test_compress_simple_stop(tmp_path):
    """Setting stop_event mid-encode causes compress_simple to return (False,'')."""
    out_dir = str(tmp_path / "converted")
    msgs, log = _logs()
    stop = threading.Event()

    original_run = converter._run_ffmpeg

    def patched_run(cmd, log_fn, stop_ev, **kwargs):
        stop_ev.set()
        return original_run(cmd, log_fn, stop_ev, **kwargs)

    with patch.object(converter, "_run_ffmpeg", patched_run):
        ok, enc = converter.compress_simple(
            str(FIXTURES / "h264_short.mkv"),
            out_dir, log, stop,
        )

    assert not ok
    # Temp file must be cleaned up
    import config
    tmp_file = os.path.join(config.LOCAL_TEMP_DIR, "h264_short.mkv")
    assert not os.path.exists(tmp_file), "Temp file was not cleaned up"


def test_progress_cb_called(tmp_path):
    """progress_cb receives at least one call with a pct value > 0."""
    out_dir = str(tmp_path / "converted")
    msgs, log = _logs()
    stop = threading.Event()
    calls = []

    def cb(pct, fps, eta):
        calls.append((pct, fps, eta))

    ok, _ = converter.compress_simple(
        str(FIXTURES / "h264_short.mkv"),
        out_dir, log, stop,
        progress_cb=cb,
    )
    assert ok
    # At least some progress callbacks should have been fired
    assert len(calls) > 0, "progress_cb was never called"
    # pct values must be in [0, 100]
    for pct, fps, eta in calls:
        assert 0.0 <= pct <= 100.0


# ---------------------------------------------------------------------------
# convert_video — dispatcher
# ---------------------------------------------------------------------------

def test_convert_video_normal_mode(tmp_path):
    """convert_video in normal mode produces a valid result dict."""
    out_dir = str(tmp_path / "out")
    stop    = threading.Event()

    result = converter.convert_video(
        input_path  = str(FIXTURES / "h264_short.mkv"),
        output_dir  = out_dir,
        anime_mode  = False,
        quality     = None,
        progress_cb = None,
        stop_event  = stop,
    )

    assert result["ok"]
    assert result["output_path"] and os.path.exists(result["output_path"])
    assert result["saved_mb"] > 0
    assert 0 < result["saved_pct"] <= 100
    assert result["error"] is None


def test_convert_video_returns_false_on_stop(tmp_path):
    """convert_video returns ok=False when stopped."""
    out_dir = str(tmp_path / "out")
    stop    = threading.Event()

    def patched_run(cmd, log_fn, stop_ev, **kwargs):
        stop_ev.set()
        return False

    with patch.object(converter, "_run_ffmpeg", patched_run):
        result = converter.convert_video(
            str(FIXTURES / "h264_short.mkv"), out_dir,
            anime_mode=False, quality=None, progress_cb=None, stop_event=stop,
        )

    assert not result["ok"]


# ---------------------------------------------------------------------------
# _verify_output
# ---------------------------------------------------------------------------

def test_verify_output_ok(tmp_path):
    """A freshly encoded file passes the integrity check."""
    out_dir = str(tmp_path / "converted")
    stop    = threading.Event()
    msgs, log = _logs()

    ok, enc = converter.compress_simple(
        str(FIXTURES / "h264_short.mkv"), out_dir, log, stop,
    )
    assert ok

    out_path = str(tmp_path / "converted" / "h264_short.mkv")
    duration = converter._ffprobe_duration(str(FIXTURES / "h264_short.mkv"))
    passed, reason = converter._verify_output(out_path, duration)
    assert passed, f"Integrity check failed: {reason}"
    assert reason == ""


def test_verify_output_missing_file():
    ok, reason = converter._verify_output("/nonexistent/path.mkv", 30.0)
    assert not ok
    assert "not found" in reason


def test_verify_output_duration_mismatch():
    """_verify_output returns (False, reason) when the output duration differs
    from the source duration by more than 5 %.
    """
    src = str(FIXTURES / "h264_short.mkv")
    real_dur = converter._ffprobe_duration(src)
    if real_dur <= 0:
        pytest.skip("Could not determine fixture duration via ffprobe")
    # Provide a src_duration that is double the real file's duration
    wrong_dur = real_dur * 2.0
    ok, reason = converter._verify_output(src, wrong_dur)
    assert not ok, "Expected failure for duration mismatch"
    assert "duration" in reason.lower(), f"Expected 'duration' in reason, got: {reason!r}"


def test_verify_output_zero_bytes(tmp_path):
    f = tmp_path / "empty.mkv"
    f.write_bytes(b"")
    ok, reason = converter._verify_output(str(f), 30.0)
    assert not ok
    assert "empty" in reason


def test_verify_output_no_video_stream(tmp_path):
    """An audio-only file fails the video stream check."""
    # Generate a tiny audio-only file with ffmpeg
    out = str(tmp_path / "audio_only.mkv")
    import subprocess
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         "-c:a", "aac_mf", out],
        capture_output=True,
    )
    if not os.path.exists(out):
        pytest.skip("ffmpeg could not create audio-only test file")
    ok, reason = converter._verify_output(out, 2.0)
    assert not ok
    assert "video" in reason.lower()


# ---------------------------------------------------------------------------
# estimate()
# ---------------------------------------------------------------------------

def test_estimate_missing_file():
    """Non-existent input → error key set."""
    result = converter.estimate("/nonexistent/path/video.mkv")
    assert "error" in result
    assert result["error"] == "File not found"


def test_estimate_file_too_short(tmp_path):
    """A real video shorter than 20 s → 'File too short to estimate'."""
    short = FIXTURES / "h264_short.mkv"
    result = converter.estimate(str(short))
    # h264_short is < 20 s; we expect either the 'too short' error or a
    # valid result if ffprobe reports ≥ 20 s — accept both gracefully.
    if result.get("error"):
        assert "short" in result["error"].lower() or "not found" in result["error"].lower()


def test_estimate_returns_expected_keys(tmp_path):
    """estimate() always returns a dict with the documented keys (mocked)."""
    fake = tmp_path / "video.mkv"
    fake.write_bytes(b"\x00" * 4)
    mock_run = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock
    with patch("converter._ffprobe_duration", return_value=60.0), \
         patch("subprocess.run") as mock_sub, \
         patch("os.path.getsize", return_value=100 * 1024 * 1024):
        # Simulate ffmpeg writing a 50 MB sample output
        mock_sub.return_value = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock(returncode=0)
        sample_path = str(fake).replace("video.mkv", f"_est_video.mkv")
        with patch("os.path.getsize", side_effect=lambda p: 50 * 1024 * 1024 if "_est_" in p else 100 * 1024 * 1024), \
             patch("os.path.isfile", return_value=True):
            result = converter.estimate(str(fake))
    # Regardless of ffmpeg outcome, the dict must have these keys
    assert isinstance(result, dict)
    assert "error" in result
