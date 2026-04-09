"""
tests/test_fixtures.py
======================
Verify that all 7 synthetic fixture files exist and have the expected
codec / pixel-format properties (via ffprobe).

Run after make_fixtures.ps1 has been executed:
    pytest VideoConverter/tests/test_fixtures.py -v
"""

import json
import os
import subprocess

import pytest

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")

EXPECTED_FILES = [
    "h264_short.mkv",
    "h264_long.mkv",
    "hevc_skip.mkv",
    "h264_tiny.mkv",
    "h264_multitrack.mkv",
    "h264_hi10.mkv",
    "h264_mp4_aac.mp4",
]


def _ffprobe(path: str) -> dict:
    result = subprocess.run(
        [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_streams", "-show_format",
            path,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def _video_stream(probe: dict) -> dict:
    for s in probe.get("streams", []):
        if s.get("codec_type") == "video":
            return s
    return {}


def _audio_streams(probe: dict) -> list[dict]:
    return [s for s in probe.get("streams", []) if s.get("codec_type") == "audio"]


def _sub_streams(probe: dict) -> list[dict]:
    return [s for s in probe.get("streams", []) if s.get("codec_type") == "subtitle"]


# ---------------------------------------------------------------------------
# Existence + size
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("filename", EXPECTED_FILES)
def test_fixture_exists(filename):
    path = os.path.join(FIXTURES_DIR, filename)
    assert os.path.exists(path), f"Fixture missing: {path}"
    assert os.path.getsize(path) > 0, f"Fixture is empty: {path}"


# ---------------------------------------------------------------------------
# Codec assertions
# ---------------------------------------------------------------------------

def test_h264_short_codec():
    probe = _ffprobe(os.path.join(FIXTURES_DIR, "h264_short.mkv"))
    v = _video_stream(probe)
    assert v.get("codec_name") == "h264"
    assert v.get("bits_per_raw_sample") in (None, "", "8", 8) or int(v.get("bits_per_raw_sample", 8)) <= 8


def test_hevc_skip_is_hevc():
    probe = _ffprobe(os.path.join(FIXTURES_DIR, "hevc_skip.mkv"))
    v = _video_stream(probe)
    assert v.get("codec_name") == "hevc"


def test_h264_hi10_is_10bit():
    probe = _ffprobe(os.path.join(FIXTURES_DIR, "h264_hi10.mkv"))
    v = _video_stream(probe)
    assert v.get("codec_name") == "h264"
    bits = int(v.get("bits_per_raw_sample") or v.get("bits_per_coded_sample") or 0)
    pix_fmt = v.get("pix_fmt", "")
    # Either ffprobe reports bits >= 10, or the pixel format contains '10'
    assert bits >= 10 or "10" in pix_fmt, (
        f"Expected 10-bit H264; bits_per_raw_sample={bits}, pix_fmt={pix_fmt}"
    )


def test_h264_mp4_is_mp4():
    path = os.path.join(FIXTURES_DIR, "h264_mp4_aac.mp4")
    probe = _ffprobe(path)
    fmt = probe.get("format", {}).get("format_name", "")
    assert "mp4" in fmt or "mov" in fmt


def test_h264_multitrack_two_audio_streams():
    probe = _ffprobe(os.path.join(FIXTURES_DIR, "h264_multitrack.mkv"))
    audio = _audio_streams(probe)
    assert len(audio) == 2, f"Expected 2 audio streams, got {len(audio)}"
    codecs = {s["codec_name"] for s in audio}
    assert "ac3" in codecs
    assert "aac" in codecs


def test_h264_multitrack_has_subtitle():
    probe = _ffprobe(os.path.join(FIXTURES_DIR, "h264_multitrack.mkv"))
    subs = _sub_streams(probe)
    assert len(subs) >= 1, "Expected at least one subtitle stream"


def test_h264_hi10_has_flac_audio():
    probe = _ffprobe(os.path.join(FIXTURES_DIR, "h264_hi10.mkv"))
    audio = _audio_streams(probe)
    assert any(s.get("codec_name") == "flac" for s in audio)


def test_h264_tiny_has_no_audio():
    probe = _ffprobe(os.path.join(FIXTURES_DIR, "h264_tiny.mkv"))
    audio = _audio_streams(probe)
    assert len(audio) == 0, "h264_tiny.mkv should have no audio streams"


# ---------------------------------------------------------------------------
# Duration sanity checks
# ---------------------------------------------------------------------------

def test_h264_short_duration():
    probe = _ffprobe(os.path.join(FIXTURES_DIR, "h264_short.mkv"))
    duration = float(probe["format"]["duration"])
    assert 28 <= duration <= 32, f"Expected ~30 s, got {duration:.1f} s"


def test_h264_long_duration():
    probe = _ffprobe(os.path.join(FIXTURES_DIR, "h264_long.mkv"))
    duration = float(probe["format"]["duration"])
    assert 295 <= duration <= 305, f"Expected ~300 s, got {duration:.1f} s"


def test_h264_tiny_duration():
    probe = _ffprobe(os.path.join(FIXTURES_DIR, "h264_tiny.mkv"))
    duration = float(probe["format"]["duration"])
    assert duration <= 10, f"Expected <= 10 s, got {duration:.1f} s"
