"""Issue #50: hard stop button should render with a visible border."""

from pathlib import Path
import re


def _read(rel_path: str) -> str:
    root = Path(__file__).resolve().parents[1]
    return (root / rel_path).read_text(encoding="utf-8")


def test_hard_stop_button_uses_border_style_contract():
    css = _read("static/style.css")

    match = re.search(r"\.btn-hstop\s*\{([^}]*)\}", css, flags=re.S)
    assert match, "Expected .btn-hstop CSS block to exist"
    block = match.group(1)

    compact = re.sub(r"\s+", "", block)
    assert "border:1pxsolid" in compact
    assert "border:none" not in compact


if __name__ == "__main__":
    test_hard_stop_button_uses_border_style_contract()
    print("Issue #50 hard-stop style tests passed")
