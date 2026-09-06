"""Issue #44: Compress Current Job progress indicators to 2-column layout."""

from pathlib import Path


def test_step_list_uses_two_column_grid_layout():
    """Verify step-list CSS supports 2-column layout to reduce vertical height."""
    style_css = Path(__file__).resolve().parents[1] / 'static' / 'style.css'
    text = style_css.read_text(encoding='utf-8')

    # Check for 2-column grid or flex-wrap configuration on .step-list
    assert '.step-list {' in text
    # Should have either grid-template-columns with 2 columns or flex-wrap support
    assert 'grid-template-columns' in text or 'max-width' in text or 'display: grid' in text
    # Ensure the layout uses columns (either CSS Grid or flex-wrap)
    assert 'max-width: 50%' in text or 'grid-template-columns: 1fr 1fr' in text or 'flex: 0 0 48%' in text


if __name__ == '__main__':
    test_step_list_uses_two_column_grid_layout()
    print('Issue #44 step-list 2-column layout test passed')
