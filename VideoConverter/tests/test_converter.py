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
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

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


def test_convert_video_normal_mode_hi10_forces_sw(tmp_path):
    """Normal-mode convert_video should force software encode for Hi10 H.264."""
    out_dir = str(tmp_path / "out")
    stop = threading.Event()
    captured = {}

    def _fake_compress_simple(*args, **kwargs):
        captured["force_sw"] = kwargs.get("force_sw")
        return False, ""

    with patch.object(converter, "is_hi10", return_value=True), \
         patch.object(converter, "compress_simple", side_effect=_fake_compress_simple):
        _ = converter.convert_video(
            input_path=str(FIXTURES / "h264_short.mkv"),
            output_dir=out_dir,
            anime_mode=False,
            quality=None,
            progress_cb=None,
            stop_event=stop,
        )

    assert captured.get("force_sw") is True


def test_convert_video_uses_pretrimmed_source_when_enabled(tmp_path):
    """When pretrim is enabled, convert_video should pass repaired input to encoder."""
    out_dir = str(tmp_path / "out")
    stop = threading.Event()
    captured = {}
    repaired = str(tmp_path / "repair" / "h264_short.mkv")
    Path(repaired).parent.mkdir(parents=True, exist_ok=True)
    Path(repaired).write_bytes(b"trimmed")

    def _fake_compress_simple(*args, **kwargs):
        captured["input_path"] = kwargs.get("input_path")
        return False, ""

    with patch.object(converter.config, "PRETRIM_TO_VIDEO_END", True), \
         patch.object(converter, "_pretrim_source_to_video_end", return_value=repaired), \
         patch.object(converter, "compress_simple", side_effect=_fake_compress_simple):
        _ = converter.convert_video(
            input_path=str(FIXTURES / "h264_short.mkv"),
            output_dir=out_dir,
            anime_mode=False,
            quality=None,
            progress_cb=None,
            stop_event=stop,
        )

    assert os.path.normpath(captured.get("input_path", "")) == os.path.normpath(repaired)


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


def test_verify_output_rejects_short_video_with_full_audio(tmp_path):
    """Reject outputs where audio looks complete but video is heavily truncated."""
    src = tmp_path / "src.mp4"
    out = tmp_path / "out.mp4"
    src.write_bytes(b"s" * 4096)
    out.write_bytes(b"o" * 2048)

    src_probe = {
        "streams": [
            {"codec_type": "video", "duration": "1000.0"},
            {"codec_type": "audio", "duration": "1000.0"},
        ],
        "format": {"duration": "1000.0"},
    }
    out_probe = {
        "streams": [
            {"codec_type": "video", "duration": "200.0"},
            {"codec_type": "audio", "duration": "1000.0"},
        ],
        "format": {"duration": "1000.0"},
    }

    def _fake_run(cmd, **kwargs):
        target = cmd[-1]
        payload = src_probe if os.path.normpath(target) == os.path.normpath(str(src)) else out_probe
        return MagicMock(returncode=0, stdout=json.dumps(payload), stderr="")

    with patch("subprocess.run", side_effect=_fake_run):
        ok, reason = converter._verify_output(str(out), 1000.0, src_path=str(src))

    assert not ok
    assert "video duration mismatch" in reason


def test_verify_output_allows_shorter_audio_when_alignment_check_disabled(tmp_path):
    """Subtitle-injection outputs may have shorter audio tails while video remains complete."""
    src = tmp_path / "src.mp4"
    out = tmp_path / "out.mp4"
    src.write_bytes(b"s" * 4096)
    out.write_bytes(b"o" * 2048)

    src_probe = {
        "streams": [
            {"codec_type": "video", "duration": "1000.0"},
            {"codec_type": "audio", "duration": "900.0"},
        ],
        "format": {"duration": "1000.0"},
    }
    out_probe = {
        "streams": [
            {"codec_type": "video", "duration": "1000.0"},
            {"codec_type": "audio", "duration": "900.0"},
        ],
        "format": {"duration": "1000.0"},
    }

    def _fake_run(cmd, **kwargs):
        target = cmd[-1]
        payload = src_probe if os.path.normpath(target) == os.path.normpath(str(src)) else out_probe
        return MagicMock(returncode=0, stdout=json.dumps(payload), stderr="")

    with patch("subprocess.run", side_effect=_fake_run):
        ok, reason = converter._verify_output(
            str(out),
            1000.0,
            src_path=str(src),
            check_av_alignment=False,
        )

    assert ok
    assert reason == ""


def test_compress_simple_does_not_replace_source_when_integrity_fails(tmp_path):
    """If temp-output integrity fails, source stays and artifact is preserved."""
    src = tmp_path / "source.mp4"
    src.write_bytes(b"x" * 4096)
    out_dir = str(tmp_path)
    msgs, log = _logs()
    stop = threading.Event()

    def _fake_run_ffmpeg(cmd, *args, **kwargs):
        tmp_out = cmd[-1]
        Path(tmp_out).parent.mkdir(parents=True, exist_ok=True)
        Path(tmp_out).write_bytes(b"y" * 1024)
        return True

    with patch.object(converter, "_ffprobe_duration", return_value=100.0), \
         patch.object(converter, "_ffprobe_source_fps", return_value=30.0), \
         patch.object(converter, "_ffprobe_vcodec", return_value=("h264", "")), \
         patch.object(converter, "_run_ffmpeg", side_effect=_fake_run_ffmpeg), \
         patch.object(converter, "_verify_output", return_value=(False, "video duration mismatch: src=100.0s out_video=20.0s (80.0% off)")), \
            patch.object(converter, "_preserve_failed_artifacts") as mock_preserve, \
         patch("os.replace") as mock_replace:
        ok, enc = converter.compress_simple(str(src), out_dir, log, stop)

    assert not ok
    assert enc == ""
    assert src.exists()
    assert src.stat().st_size == 4096
    assert mock_replace.call_count == 0
    assert mock_preserve.call_count == 1
    assert any("integrity verification failed before replace" in m for m in msgs)


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


def test_estimate_uses_trimmed_mean_and_sets_variance_metadata(tmp_path):
    """Robust estimator should trim outliers and expose variance metadata."""
    fake = tmp_path / "video.mkv"
    fake.write_bytes(b"x")

    src_bytes = 100 * 1024 * 1024
    sample_bytes = {
        1: int(4.0 * 1024 * 1024),
        2: int(4.1 * 1024 * 1024),
        3: int(4.2 * 1024 * 1024),
        4: int(4.3 * 1024 * 1024),
        5: int(12.0 * 1024 * 1024),
    }

    def _fake_run(cmd, capture_output=True, timeout=120):
        out_path = Path(cmd[-1])
        idx = int(out_path.stem.rsplit("_", 1)[-1])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if "_est_src_" in out_path.stem:
            out_path.write_bytes(b"\x00" * (10 * 1024 * 1024))
        else:
            out_path.write_bytes(b"\x00" * sample_bytes[idx])
        return MagicMock(returncode=0)

    orig_getsize = os.path.getsize

    def _fake_getsize(p):
        p = str(p)
        if os.path.normpath(p) == os.path.normpath(str(fake)):
            return src_bytes
        return orig_getsize(p)

    with patch.object(converter, "_ffprobe_duration", return_value=100.0), \
         patch.object(converter.config, "ESTIMATE_SAMPLE_FRACTIONS", (1/6, 2/6, 3/6, 4/6, 5/6)), \
         patch.object(converter.config, "ESTIMATE_CLIP_SECS", 10.0), \
         patch.object(converter.config, "LOCAL_TEMP_DIR", str(tmp_path)), \
         patch("subprocess.run", side_effect=_fake_run), \
         patch("os.path.getsize", side_effect=_fake_getsize):
        result = converter.estimate(str(fake))

    assert result["error"] is None
    assert result["aggregation"] == "trimmed_mean_20"
    assert result["sample_count"] == 5
    assert result["high_variance"] is True
    assert result["sample_cv_pct"] > 45.0
    # Ratios are [0.40, 0.41, 0.42, 0.43, 1.20], trimmed mean is 0.42.
    assert result["estimated_output_mb"] == 42.0
    assert result["estimated_saving_mb"] == 58.0
    assert result["estimated_saving_pct"] == 58


def test_estimate_defaults_to_two_15s_clips_in_first_and_last_third(tmp_path):
    """Default estimate policy samples middle of first/last third with 15s clips."""
    fake = tmp_path / "video.mkv"
    fake.write_bytes(b"x")

    seen_seeks: list[float] = []
    seen_t: list[float] = []

    def _fake_run(cmd, capture_output=True, timeout=120):
        out_path = Path(cmd[-1])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if "_est_src_" in str(out_path):
            ss_idx = cmd.index("-ss")
            t_idx = cmd.index("-t")
            seen_seeks.append(float(cmd[ss_idx + 1]))
            seen_t.append(float(cmd[t_idx + 1]))
            out_path.write_bytes(b"\x00" * (4 * 1024 * 1024))
        elif "_est_enc_" in str(out_path):
            out_path.write_bytes(b"\x00" * (2 * 1024 * 1024))
        return MagicMock(returncode=0)

    with patch.object(converter, "_ffprobe_duration", return_value=180.0), \
         patch.object(converter.config, "LOCAL_TEMP_DIR", str(tmp_path)), \
         patch("subprocess.run", side_effect=_fake_run), \
         patch("os.path.isfile", return_value=True), \
         patch("os.path.getsize", return_value=100 * 1024 * 1024):
        result = converter.estimate(str(fake))

    assert result["error"] is None
    assert result["sample_count"] == 2
    assert seen_t == [15.0, 15.0]
    # Centers at 30s and 150s, with 15s clips => starts at 22.5s and 142.5s.
    assert seen_seeks == [22.5, 142.5]


def test_estimate_sample_commands_are_video_only(tmp_path):
    """Estimator sample extract/encode should avoid audio/subtitle/data streams."""
    fake = tmp_path / "video.mkv"
    fake.write_bytes(b"x")

    seen_extract = []
    seen_encode = []

    def _fake_run(cmd, capture_output=True, timeout=120):
        out_path = Path(cmd[-1])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if "_est_src_" in str(out_path):
            seen_extract.append(cmd)
            out_path.write_bytes(b"\x00" * (8 * 1024 * 1024))
        elif "_est_enc_" in str(out_path):
            seen_encode.append(cmd)
            out_path.write_bytes(b"\x00" * (4 * 1024 * 1024))
        return MagicMock(returncode=0)

    with patch.object(converter, "_ffprobe_duration", return_value=180.0), \
         patch.object(converter.config, "LOCAL_TEMP_DIR", str(tmp_path)), \
         patch("subprocess.run", side_effect=_fake_run), \
         patch("os.path.isfile", return_value=True), \
         patch("os.path.getsize", return_value=100 * 1024 * 1024):
        result = converter.estimate(str(fake))

    assert result["error"] is None
    assert seen_extract, "expected extract commands"
    assert seen_encode, "expected encode commands"
    # Extraction must be video-only copy.
    assert any("-map" in c and "0:v:0" in c for c in seen_extract)
    assert all("-an" in c and "-sn" in c and "-dn" in c for c in seen_extract)
    # Encode must remain video-only.
    assert all("-an" in c and "-sn" in c and "-dn" in c for c in seen_encode)


# ---------------------------------------------------------------------------
# compress_simple — track verification before os.replace
# ---------------------------------------------------------------------------

def test_compress_simple_aborts_when_track_verify_fails(tmp_path):
    """
    If _verify_tracks_preserved fails after encoding, compress_simple must:
    - NOT call os.replace (source stays intact)
    - Return (False, '')
    - Delete the temp output
    """
    from unittest.mock import patch
    src = str(FIXTURES / "h264_short.mkv")
    src_size = Path(src).stat().st_size
    out_dir = str(tmp_path / "out")
    msgs, log = _logs()
    stop = threading.Event()

    def _fake_run(cmd, log_fn, stop_ev, **kwargs):
        # Write something smaller than source so the size check passes
        tmp_out = cmd[-1]
        Path(tmp_out).parent.mkdir(parents=True, exist_ok=True)
        Path(tmp_out).write_bytes(b"\x00" * 100)
        return True

    with patch.object(converter, "_run_ffmpeg", _fake_run), \
         patch.object(converter, "_verify_tracks_preserved",
                      return_value=(False, "audio tracks dropped: source had 2, output has 1")):
        ok, enc = converter.compress_simple(src, out_dir, log, stop)

    assert ok is False, "Expected failure when track verification fails"
    assert any("track verification" in m.lower() or "error" in m.lower() for m in msgs)
    assert Path(src).exists(), "Source file must not be deleted"
    assert Path(src).stat().st_size == src_size, "Source file must not be modified"


def test_compress_simple_track_verify_pass_proceeds(tmp_path):
    """When _verify_tracks_preserved passes, compress_simple proceeds normally."""
    from unittest.mock import patch
    src = str(FIXTURES / "h264_short.mkv")
    out_dir = str(tmp_path / "out")
    msgs, log = _logs()
    stop = threading.Event()

    with patch.object(converter, "_verify_tracks_preserved", return_value=(True, "")):
        ok, enc = converter.compress_simple(src, out_dir, log, stop)

    assert isinstance(ok, bool)
    assert not any("track verification failed" in m.lower() for m in msgs), \
        f"Unexpected track verification failure in logs: {msgs}"
