"""Regression checks for the fixed Bitrate + Duration queue sort mode."""

from pathlib import Path


def test_sort_dropdown_exposes_bitrate_duration_option():
    """Queue toolbar and Settings modal should both expose the new sort option."""
    html_path = Path(__file__).resolve().parents[1] / "templates" / "index.html"
    html = html_path.read_text(encoding="utf-8")

    assert '<option value="bitrate_duration">⇅ Bitrate + Duration</option>' in html
    assert 'value="bitrate_duration">Bitrate + Duration — shortest first within each bitrate bucket</option>' in html


def test_app_js_uses_fixed_bitrate_duration_order_and_locks_direction_button():
    """Bitrate + Duration should ignore sort direction and disable the toggle button."""
    js_path = Path(__file__).resolve().parents[1] / "static" / "app.js"
    js = js_path.read_text(encoding="utf-8")

    assert "const brDelta = _fileBitrate(b) - _fileBitrate(a);" in js
    assert "const durDelta = _durationSecs(a) - _durationSecs(b);" in js
    assert "if (_sortBy === 'bitrate_duration') {" in js
    assert "btn.disabled = true;" in js
    assert "Bitrate + Duration uses a fixed order: highest bitrate, shortest duration" in js
