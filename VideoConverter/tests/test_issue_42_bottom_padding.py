"""Issue #42: Add subtle bottom padding to main content wrapper."""

from pathlib import Path


def _read(rel_path: str) -> str:
    root = Path(__file__).resolve().parents[1]
    return (root / rel_path).read_text(encoding="utf-8")


def test_main_container_has_bottom_padding_class():
    html = _read("templates/index.html")
    expected = '<div class="container-fluid px-4 pb-2 d-flex flex-column flex-grow-1" style="min-height:0">'
    assert expected in html


if __name__ == "__main__":
    test_main_container_has_bottom_padding_class()
    print("Issue #42 bottom-padding test passed")
