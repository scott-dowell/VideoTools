"""Issue #40: Session savings card layout parity with neighbor cards."""

from pathlib import Path


def _read(rel_path: str) -> str:
    root = Path(__file__).resolve().parents[1]
    return (root / rel_path).read_text(encoding="utf-8")


def test_session_estimate_moved_to_right_meta_block():
    html = _read("templates/index.html")

    # New target id in right-side meta stack.
    assert 'id="statSessionMetaEst"' in html

    # Old left-stack line should be removed to keep stack height consistent.
    assert 'id="statSessionEst"' not in html

    # Ensure elapsed + elapsed-label + est line all live in the right meta block.
    assert 'id="statSessionElapsed"' in html
    assert 'id="statSessionElapsedLabel"' in html


def test_stat_cards_use_equal_height_layout_contract():
    css = _read("static/style.css")

    # Equal-height behavior in Bootstrap grid columns.
    assert ".stat-card" in css
    assert "height:100%" in css.replace(" ", "")


def test_session_card_updates_new_meta_estimate_target():
    js = _read("static/app.js")

    # JS should write estimate text into the right-meta estimate line.
    assert "statSessionMetaEst" in js
    assert "statSessionEst" not in js


if __name__ == "__main__":
    test_session_estimate_moved_to_right_meta_block()
    test_stat_cards_use_equal_height_layout_contract()
    test_session_card_updates_new_meta_estimate_target()
    print("Issue #40 layout tests passed")
