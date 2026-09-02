"""Issue #41: Prevent queue-card clipping caused by right-panel split layout."""

from pathlib import Path


def _read(rel_path: str) -> str:
    root = Path(__file__).resolve().parents[1]
    return (root / rel_path).read_text(encoding="utf-8")


def test_right_splitter_lives_inside_right_split_pane():
    html = _read("templates/index.html")
    start = html.find('<div id="rightSplitPane"')
    end = html.find('</div><!-- /rightSplitPane -->')
    assert start != -1 and end != -1 and end > start
    inside = html[start:end]
    assert 'id="currentJobCard"' in inside
    assert 'id="rightSplitter"' in inside
    assert '<i class="bi bi-terminal me-2"></i>Log' in inside


def test_current_job_not_hard_fixed_height_inline():
    html = _read("templates/index.html")
    # Fixed inline height creates hard vertical pressure and can clip siblings.
    assert 'id="currentJobCard" style="height:260px' not in html


def test_splitter_restore_clamps_height_on_init_and_resize():
    js = _read("static/app.js")
    assert "function getMaxJobHeight()" in js
    assert "function clampJobHeight(px)" in js
    assert "window.addEventListener('resize'" in js


def test_log_box_can_shrink_in_split_layout():
    css = _read("static/style.css")
    # No hard minimum that can force column overflow in short viewports.
    assert "min-height: 120px;" not in css


if __name__ == "__main__":
    test_right_splitter_lives_inside_right_split_pane()
    test_current_job_not_hard_fixed_height_inline()
    test_splitter_restore_clamps_height_on_init_and_resize()
    test_log_box_can_shrink_in_split_layout()
    print("Issue #41 layout tests passed")
