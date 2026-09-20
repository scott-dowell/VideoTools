"""Verify pretrim_to_video_end setting is wired in the UI settings dialog."""

from pathlib import Path


def test_settings_ui_has_pretrim_checkbox():
    """Verify the settings modal HTML includes the pretrim_to_video_end checkbox."""
    html_path = Path(__file__).resolve().parents[1] / 'templates' / 'index.html'
    html_content = html_path.read_text(encoding='utf-8')
    
    assert 'id="settingsPretrimToVideoEnd"' in html_content, "Missing checkbox input element"
    assert 'Trim container to video stream end before convert (pretrim)' in html_content, "Missing label text"
    assert 'stream-copied to the first video stream endpoint before conversion' in html_content, "Missing help text"


def test_app_js_loads_and_saves_pretrim():
    """Verify the JavaScript load/save functions handle pretrim_to_video_end."""
    js_path = Path(__file__).resolve().parents[1] / 'static' / 'app.js'
    js_content = js_path.read_text(encoding='utf-8')
    
    # Check openSettings() loads the setting
    assert "document.getElementById('settingsPretrimToVideoEnd').checked = !!s.pretrim_to_video_end" in js_content
    
    # Check saveSettings() includes it in the payload
    assert "pretrim_to_video_end:      !!document.getElementById('settingsPretrimToVideoEnd').checked" in js_content


def test_backend_handles_pretrim_setting():
    """Verify app.py applies the pretrim_to_video_end setting from JSON."""
    app_path = Path(__file__).resolve().parents[1] / 'app.py'
    app_content = app_path.read_text(encoding='utf-8')
    
    # Check it's in settings defaults (with actual spacing)
    assert 'pretrim_to_video_end' in app_content and 'config.PRETRIM_TO_VIDEO_END' in app_content
    
    # Check it's validated in POST handler
    assert 'if "pretrim_to_video_end" in data:' in app_content
    assert 'data["pretrim_to_video_end"] = bool(data["pretrim_to_video_end"])' in app_content
    
    # Check it's applied to config before conversion
    assert 'config.PRETRIM_TO_VIDEO_END = bool(settings.get("pretrim_to_video_end"' in app_content


if __name__ == '__main__':
    test_settings_ui_has_pretrim_checkbox()
    print('✓ Settings UI has pretrim checkbox')
    
    test_app_js_loads_and_saves_pretrim()
    print('✓ JavaScript loads and saves pretrim setting')
    
    test_backend_handles_pretrim_setting()
    print('✓ Backend handles pretrim setting')
    
    print('\n✓ All pretrim_to_video_end UI wiring tests passed')
