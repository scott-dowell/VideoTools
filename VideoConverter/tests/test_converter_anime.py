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
    """`_aac_encoder()` returns 'aac' on win32."""
    with patch("sys.platform", "win32"):
        enc = converter._aac_encoder()
    assert enc == "aac"


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


def test_remux_mkv_multitrack_with_progress_cb(tmp_path):
    """remux_to_mp4 should not crash when progress_cb is provided."""
    out_dir = str(tmp_path / "out")
    msgs, log = _logs()
    stop = threading.Event()
    calls = []

    ok, enc = converter.remux_to_mp4(
        str(FIXTURES / "h264_multitrack.mkv"),
        out_dir, log, stop,
        progress_cb=lambda pct, fps, eta: calls.append((pct, fps, eta)),
    )

    assert ok, f"Remux with progress callback failed; logs: {msgs}"
    assert enc in ("copy", "hevc_qsv", "libx265")


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
    """convert_video with anime_mode=True and Hi10 source compresses via libx265/QSV + remux."""
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
    assert result["encoder_used"] in ("hevc_qsv", "libx265")


# ---------------------------------------------------------------------------
# Bitmap subtitle (PGS) OCR tests — requires h264_bitmap_sub.mkv fixture
# ---------------------------------------------------------------------------

BITMAP_FIXTURE = FIXTURES / "h264_bitmap_sub.mkv"

bitmap_required = pytest.mark.skipif(
    not BITMAP_FIXTURE.exists(),
    reason="h264_bitmap_sub.mkv fixture missing — see make_fixtures.ps1 for instructions",
)


@bitmap_required
def test_bitmap_sub_fixture_streams():
    """Fixture must have HEVC video, AAC audio, ASS sub, and PGS bitmap sub."""
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", str(BITMAP_FIXTURE)],
        capture_output=True, text=True, timeout=15,
    )
    import json
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    codecs = {s.get("codec_name", "").lower() for s in streams}
    assert "hevc" in codecs, "Expected HEVC video in fixture"
    assert "aac" in codecs, "Expected AAC audio in fixture"
    assert "ass" in codecs, "Expected ASS text sub in fixture"
    assert "hdmv_pgs_subtitle" in codecs, "Expected PGS bitmap sub in fixture"


@bitmap_required
def test_bitmap_sub_ocr_deps_ok():
    """bitmap_subs.DEPS_OK must be True — pytesseract/Pillow/pysubs2 required."""
    import bitmap_subs
    assert bitmap_subs.DEPS_OK, (
        "OCR deps not installed. Run: pip install pytesseract Pillow pysubs2 + winget install UB-Mannheim.TesseractOCR"
    )


@bitmap_required
def test_remux_bitmap_sub_produces_two_sub_tracks(tmp_path):
    """
    remux_to_mp4 on a file with ASS + PGS subs must produce an MP4 with
    both subtitle tracks as mov_text — the PGS should be OCR'd to text.
    """
    import json
    out_dir = str(tmp_path / "out")
    msgs, log = _logs()
    stop = threading.Event()

    ok, enc = converter.remux_to_mp4(
        str(BITMAP_FIXTURE),
        out_dir, log, stop,
        hi10=True,  # video is already HEVC — copy it
    )
    assert ok, f"remux_to_mp4 failed; logs: {msgs}"

    out_files = list(Path(out_dir).glob("*.mp4"))
    assert out_files, "Expected .mp4 output"
    out_path = str(out_files[0])

    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", out_path],
        capture_output=True, text=True, timeout=15,
    )
    data = json.loads(result.stdout)
    streams = data.get("streams", [])

    sub_streams = [s for s in streams if s.get("codec_type") == "subtitle"]
    assert len(sub_streams) >= 2, (
        f"Expected at least 2 subtitle tracks (ASS + OCR'd PGS), got {len(sub_streams)}; "
        f"logs: {msgs}"
    )
    sub_codecs = {s.get("codec_name", "").lower() for s in sub_streams}
    assert sub_codecs == {"mov_text"}, (
        f"All subtitle tracks should be mov_text in MP4, got: {sub_codecs}"
    )
    # OCR'd tracks must have language=eng, not und
    sub_langs = [s.get("tags", {}).get("language", "") for s in sub_streams]
    assert all(lang == "eng" for lang in sub_langs), (
        f"All subtitle tracks should have language=eng, got: {sub_langs}"
    )
    # Confirm log shows OCR was invoked
    assert any("ocr" in m.lower() for m in msgs), "Expected 'OCR' in log output"


@bitmap_required
def test_remux_bitmap_sub_no_deps_fails(tmp_path):
    """
    When OCR deps are unavailable and the file has bitmap subs,
    remux_to_mp4 must return (False, '') rather than silently drop them.
    """
    from unittest.mock import patch
    import bitmap_subs
    out_dir = str(tmp_path / "out")
    msgs, log = _logs()
    stop = threading.Event()

    with patch.object(bitmap_subs, "DEPS_OK", False):
        ok, enc = converter.remux_to_mp4(
            str(BITMAP_FIXTURE),
            out_dir, log, stop,
            hi10=True,
        )

    assert ok is False, "Expected failure when OCR deps missing and bitmap subs present"
    assert any("error" in m.lower() for m in msgs), "Expected ERROR message in log"
    # Must not produce any output file
    assert not list(Path(out_dir).glob("*.mp4")), "Must not produce output when aborting"


# ===========================================================================
# _verify_tracks_preserved tests
# ===========================================================================

def _make_probe_result(audio_count: int, sub_count: int) -> str:
    """Return a minimal ffprobe JSON string for mocking."""
    import json as _json
    streams = (
        [{"codec_type": "video", "codec_name": "hevc"}]
        + [{"codec_type": "audio", "codec_name": "aac"}] * audio_count
        + [{"codec_type": "subtitle", "codec_name": "mov_text"}] * sub_count
    )
    return _json.dumps({"streams": streams})


def test_verify_tracks_ok(tmp_path):
    """Matching audio + sub counts → passes."""
    from unittest.mock import patch, MagicMock
    src = str(tmp_path / "src.mkv")
    out = str(tmp_path / "out.mp4")
    # Create dummy files so exists check passes (not needed here — ffprobe is mocked)
    Path(src).write_bytes(b"x")
    Path(out).write_bytes(b"x")

    same_result = _make_probe_result(audio_count=2, sub_count=1)
    mock_run = MagicMock()
    mock_run.return_value.stdout = same_result
    with patch("converter.subprocess.run", mock_run):
        ok, reason = converter._verify_tracks_preserved(src, out)
    assert ok is True, f"Expected pass but got: {reason}"


def test_verify_tracks_audio_dropped(tmp_path):
    """Fewer audio tracks in output → fails."""
    from unittest.mock import patch, MagicMock
    src = str(tmp_path / "src.mkv")
    out = str(tmp_path / "out.mp4")

    call_count = [0]
    def side_effect(*args, **kwargs):
        m = MagicMock()
        # First call = src (2 audio), second = out (1 audio)
        m.stdout = _make_probe_result(2, 1) if call_count[0] == 0 else _make_probe_result(1, 1)
        call_count[0] += 1
        return m

    with patch("converter.subprocess.run", side_effect=side_effect):
        ok, reason = converter._verify_tracks_preserved(src, out)
    assert ok is False
    assert "audio" in reason.lower()


def test_verify_tracks_subs_lost(tmp_path):
    """Source had subs, output has none → fails."""
    from unittest.mock import patch, MagicMock
    src = str(tmp_path / "src.mkv")
    out = str(tmp_path / "out.mp4")

    call_count = [0]
    def side_effect(*args, **kwargs):
        m = MagicMock()
        m.stdout = _make_probe_result(2, 1) if call_count[0] == 0 else _make_probe_result(2, 0)
        call_count[0] += 1
        return m

    with patch("converter.subprocess.run", side_effect=side_effect):
        ok, reason = converter._verify_tracks_preserved(src, out)
    assert ok is False
    assert "subtitle" in reason.lower() or "sub" in reason.lower()


def test_verify_tracks_no_subs_in_source_ok(tmp_path):
    """Source with no subs and output with no subs is fine (hardsubbed source)."""
    from unittest.mock import patch, MagicMock
    src = str(tmp_path / "src.mkv")
    out = str(tmp_path / "out.mp4")

    same_result = _make_probe_result(audio_count=1, sub_count=0)
    mock_run = MagicMock()
    mock_run.return_value.stdout = same_result
    with patch("converter.subprocess.run", mock_run):
        ok, reason = converter._verify_tracks_preserved(src, out)
    assert ok is True, f"Expected pass for no-sub source but got: {reason}"


# ===========================================================================
# DTS fallback safety tests
# ===========================================================================

def test_remux_dts_fallback_aborts_when_subs_present(tmp_path):
    """
    When the DTS no-subs fallback (attempt 2) would be reached and subtitles
    are present, remux_to_mp4 must abort rather than silently drop subs.
    """
    import json as _json

    # Build a minimal ffprobe response that includes an ASS subtitle stream
    probe_data = {
        "streams": [
            {"index": 0, "codec_type": "video",    "codec_name": "hevc"},
            {"index": 1, "codec_type": "audio",    "codec_name": "aac",
             "tags": {"language": "jpn"}},
            {"index": 2, "codec_type": "subtitle", "codec_name": "ass",
             "tags": {"language": "eng"}},
        ]
    }

    msgs = []
    log = msgs.append
    stop = threading.Event()

    # Simulate: attempt 0 and 1 fail with a DTS error, attempt 2 would be 'no subs'.
    # We mock _run_ffmpeg to always return False, and mock subprocess.Popen so that
    # the DTS error text appears in stdout to satisfy the retry condition.
    proc_mock = MagicMock()
    proc_mock.stdout = iter(["error: out of order dts\n"])
    proc_mock.returncode = 1
    proc_mock.wait = MagicMock()
    proc_mock.pid = 1234

    with patch("converter.subprocess.run",
               return_value=MagicMock(stdout=_json.dumps(probe_data), returncode=0)), \
         patch("converter.subprocess.Popen", return_value=proc_mock), \
         patch("converter._ffprobe_duration", return_value=60.0):

        ok, enc = converter.remux_to_mp4(
            str(FIXTURES / "h264_bitmap_sub.mkv"),  # any existing file — probing is mocked
            str(tmp_path / "out"),
            log, stop,
            hi10=True,
        )

    assert ok is False, "Expected abort when DTS fallback would drop subs"
    assert any("dts" in m.lower() or "abort" in m.lower() or "subtitle" in m.lower()
               for m in msgs), f"Expected abort message in log. Got: {msgs}"


def test_remux_dts_attempt2_tried_before_abort(tmp_path):
    """
    The DTS recovery loop must try attempt 2 (-fflags +genpts -avoid_negative_ts)
    before reaching the abort/no-subs stage.  Verify by having only attempts
    0 and 1 fail with DTS error; attempt 2 succeeds.
    """
    import json as _json

    probe_data = {
        "streams": [
            {"index": 0, "codec_type": "video",    "codec_name": "hevc"},
            {"index": 1, "codec_type": "audio",    "codec_name": "aac",
             "tags": {"language": "jpn"}},
            {"index": 2, "codec_type": "subtitle", "codec_name": "ass",
             "tags": {"language": "eng"}},
        ]
    }

    msgs = []
    log = msgs.append
    stop = threading.Event()

    class _DtsThenOkProc:
        _call = 0
        pid = 999
        def __init__(self):
            type(self)._call += 1
            if type(self)._call <= 2:
                self.stdout = iter(["error: out of order dts\n"])
                self.returncode = 1
            else:
                self.stdout = iter([])
                self.returncode = 0
        def wait(self): pass
        def kill(self): pass

    _DtsThenOkProc._call = 0

    with patch("converter.subprocess.run",
               return_value=MagicMock(stdout=_json.dumps(probe_data), returncode=0)), \
         patch("converter.subprocess.Popen", side_effect=lambda *a, **k: _DtsThenOkProc()), \
         patch("converter._ffprobe_duration", return_value=60.0), \
         patch("converter._verify_tracks_preserved", return_value=(True, "")), \
         patch("os.path.getsize", return_value=1000), \
         patch("os.path.exists", return_value=True), \
         patch("os.replace"), \
         patch("os.makedirs"):

        ok, enc = converter.remux_to_mp4(
            str(FIXTURES / "h264_bitmap_sub.mkv"),
            str(tmp_path / "out"),
            log, stop,
            hi10=True,
        )

    assert ok is True, f"Expected success on attempt 2; logs: {msgs}"
    assert any("dts fix retry" in m.lower() for m in msgs), \
        f"Expected attempt-2 'DTS fix retry' log message; got: {msgs}"
