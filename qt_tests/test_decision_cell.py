"""widgets.decision_cell renders as a real segmented control.

Doesn't fit the shot()/scene system the whole-dialog tests use (there is no dialog
scene that carries a bare decision_cell), so this builds one directly, the same way
test_chip_column.py's _row_grid builds a bare row.
"""
import harness

NEW_CARD_OPTIONS = [("import", "Import"), ("skip", "Skip for now"), ("never", "Never")]


def _build(theme):
    _, q = harness.bootstrap()
    app = harness.app()
    harness.apply_theme(theme)
    from internpearls import widgets

    cell = widgets.decision_cell(NEW_CARD_OPTIONS, "import", lambda v: None)
    cell.resize(300, 30)
    cell.show()
    app.processEvents()
    return cell


def test_every_button_renders_wider_than_an_empty_label_would():
    """A non-zero sizeHint alone would also pass for a button whose label never
    reached the font metrics (padding/border still contribute something). Comparing
    against an identically-styled empty-text button is what proves the option's own
    text painted."""
    _, q = harness.bootstrap()
    for theme in ("light", "dark"):
        cell = _build(theme)
        for value, button in cell.buttons.items():
            hint = button.sizeHint()
            assert hint.width() > 0 and hint.height() > 0, (
                f"{theme}/{value} rendered with a zero sizeHint")
            # Same border/padding/font stylesheet, empty text: isolates the label's
            # own contribution to the width from the chrome every option shares.
            twin = q.QPushButton("")
            twin.setStyleSheet(button.styleSheet())
            blank_width = twin.sizeHint().width()
            assert hint.width() > blank_width, (
                f"{theme}/{value} is no wider ({hint.width()}) than the same button "
                f"with no text ({blank_width}): its label doesn't seem to be painting")


def test_exactly_one_button_is_checked():
    for theme in ("light", "dark"):
        cell = _build(theme)
        checked = [v for v, b in cell.buttons.items() if b.isChecked()]
        assert checked == ["import"], (
            f"{theme}: expected only 'import' checked, got {checked}")
