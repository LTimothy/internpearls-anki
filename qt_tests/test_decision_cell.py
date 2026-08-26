"""widgets.decision_cell renders as a real segmented control.

Doesn't fit the shot()/scene system the whole-dialog tests use (there is no dialog
scene that carries a bare decision_cell), so this builds one directly, the same way
test_chip_column.py's _row_grid builds a bare row.
"""
import harness

NEW_CARD_OPTIONS = [("import", "Import"), ("skip", "Skip"), ("never", "Never")]


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


def test_every_button_renders_wider_than_a_single_character_would():
    """A non-zero sizeHint alone would also pass for a button whose label never
    reached the font metrics (padding/border still contribute something). Comparing
    against an identically-styled one-character button is what proves the rest of the
    option's own label actually painted.

    Compared against one character rather than none: some platform styles apply a
    floor width to a button carrying literally no text, which comes out wider than a
    short option like "Skip" legitimately paints and would sink the comparison for a
    reason that has nothing to do with whether the label painted.
    """
    _, q = harness.bootstrap()
    for theme in ("light", "dark"):
        cell = _build(theme)
        for value, button in cell.buttons.items():
            hint = button.sizeHint()
            assert hint.width() > 0 and hint.height() > 0, (
                f"{theme}/{value} rendered with a zero sizeHint")
            twin = q.QPushButton(button.text()[0])
            twin.setStyleSheet(button.styleSheet())
            one_char_width = twin.sizeHint().width()
            assert hint.width() > one_char_width, (
                f"{theme}/{value} is no wider ({hint.width()}) than the same button "
                f"carrying only its first character ({one_char_width}): the rest of "
                "its label doesn't seem to be painting")


def test_exactly_one_button_is_checked():
    for theme in ("light", "dark"):
        cell = _build(theme)
        checked = [v for v, b in cell.buttons.items() if b.isChecked()]
        assert checked == ["import"], (
            f"{theme}: expected only 'import' checked, got {checked}")
