"""Issue #43: Normalize queue path keys so done rows survive later scan updates."""

from pathlib import Path


def test_queue_path_index_normalizes_windows_and_forward_slash_paths():
    app_js = Path(__file__).resolve().parents[1] / 'static' / 'app.js'
    text = app_js.read_text(encoding='utf-8')

    assert 'function _normalizePathKey(path)' in text
    assert "_fileIndexByPath[_normalizePathKey(f.full_path)] = i" in text
    assert "_fileIndexByPath[_normalizePathKey(msg.full_path)]" in text
    assert "statusByPath[_normalizePathKey(f.full_path)]" in text


if __name__ == '__main__':
    test_queue_path_index_normalizes_windows_and_forward_slash_paths()
    print('Issue #43 path-normalization test passed')
