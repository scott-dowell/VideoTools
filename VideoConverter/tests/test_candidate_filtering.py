"""
tests/test_candidate_filtering.py
==================================
Unit tests for VideoConverter/candidate_filtering.py.

pytest VideoConverter/tests/test_candidate_filtering.py -v
"""

import os
import sys

import pytest

# Allow importing from VideoConverter/ without installing the package
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import candidate_filtering as cf


# ---------------------------------------------------------------------------
# passes_size_filter
# ---------------------------------------------------------------------------

class TestPassesSizeFilter:
    def test_within_range(self):
        ok, reason = cf.passes_size_filter(500.0, min_size=100, max_size=2000)
        assert ok is True
        assert reason is None

    def test_below_minimum(self):
        ok, reason = cf.passes_size_filter(50.0, min_size=100, max_size=2000)
        assert ok is False
        assert "below minimum" in reason

    def test_above_maximum(self):
        ok, reason = cf.passes_size_filter(3000.0, min_size=100, max_size=2000)
        assert ok is False
        assert "above maximum" in reason

    def test_max_zero_means_no_upper_limit(self):
        ok, reason = cf.passes_size_filter(99999.0, min_size=100, max_size=0)
        assert ok is True

    def test_exactly_at_minimum(self):
        ok, _ = cf.passes_size_filter(100.0, min_size=100, max_size=0)
        assert ok is True

    def test_exactly_at_maximum(self):
        ok, _ = cf.passes_size_filter(2000.0, min_size=100, max_size=2000)
        assert ok is True


# ---------------------------------------------------------------------------
# passes_duration_filter
# ---------------------------------------------------------------------------

class TestPassesDurationFilter:
    def test_long_enough(self):
        ok, reason = cf.passes_duration_filter(120.0, min_duration=60)
        assert ok is True
        assert reason is None

    def test_too_short(self):
        ok, reason = cf.passes_duration_filter(30.0, min_duration=60)
        assert ok is False
        assert "duration too short" in reason

    def test_none_duration(self):
        ok, reason = cf.passes_duration_filter(None, min_duration=60)
        assert ok is False

    def test_exactly_at_minimum(self):
        ok, _ = cf.passes_duration_filter(60.0, min_duration=60)
        assert ok is True


# ---------------------------------------------------------------------------
# should_skip_folder
# ---------------------------------------------------------------------------

class TestShouldSkipFolder:
    def test_match(self):
        assert cf.should_skip_folder(r"D:\Anime\NoEncode", skip_folders=["NoEncode"])

    def test_no_match(self):
        assert not cf.should_skip_folder(r"D:\Anime\Shows", skip_folders=["NoEncode"])

    def test_empty_skip_list(self):
        assert not cf.should_skip_folder(r"D:\Anime\Shows", skip_folders=[])

    def test_substring_match(self):
        # e.g. "NoEncode" appears inside a longer path segment
        assert cf.should_skip_folder(r"D:\Anime\NoEncode\Season1", skip_folders=["NoEncode"])


# ---------------------------------------------------------------------------
# calculate_conversion_rate
# ---------------------------------------------------------------------------

class TestCalculateConversionRate:
    def test_normal(self):
        rate = cf.calculate_conversion_rate(size_mb=1000, duration=500)
        assert abs(rate - 2048.0) < 0.01   # 1000 * 1024 / 500

    def test_zero_duration(self):
        assert cf.calculate_conversion_rate(size_mb=1000, duration=0) == 0.0

    def test_negative_duration(self):
        assert cf.calculate_conversion_rate(size_mb=1000, duration=-1) == 0.0


# ---------------------------------------------------------------------------
# format_duration_hms
# ---------------------------------------------------------------------------

class TestFormatDurationHms:
    def test_zero(self):
        assert cf.format_duration_hms(0) == "00:00:00"

    def test_one_hour(self):
        assert cf.format_duration_hms(3600) == "01:00:00"

    def test_mixed(self):
        assert cf.format_duration_hms(3661) == "01:01:01"

    def test_under_a_minute(self):
        assert cf.format_duration_hms(45) == "00:00:45"


# ---------------------------------------------------------------------------
# handle_existing_conversion
# ---------------------------------------------------------------------------

class TestHandleExistingConversion:
    def test_no_output_file(self, tmp_path):
        src = tmp_path / "source.mkv"
        src.write_bytes(b"x" * 1024)
        out = tmp_path / "converted" / "source.mkv"  # does not exist
        action, stats = cf.handle_existing_conversion(str(src), str(out), {})
        assert action == "none"
        assert stats is None

    def test_output_smaller_than_source(self, tmp_path):
        src = tmp_path / "source.mkv"
        src.write_bytes(b"x" * 2000)
        out = tmp_path / "source_conv.mkv"
        out.write_bytes(b"x" * 1000)
        action, stats = cf.handle_existing_conversion(str(src), str(out), {})
        assert action == "remove_original"
        assert stats["saved_mb"] > 0

    def test_output_larger_than_source(self, tmp_path):
        src = tmp_path / "source.mkv"
        src.write_bytes(b"x" * 1000)
        out = tmp_path / "source_conv.mkv"
        out.write_bytes(b"x" * 2000)
        action, stats = cf.handle_existing_conversion(str(src), str(out), {})
        assert action == "replace_with_original"
        assert stats["saved_mb"] < 0
