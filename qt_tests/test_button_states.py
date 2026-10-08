"""Shared links and row decisions paint keyboard focus and disabled states."""
import os
from pathlib import Path

import pytest
from PyQt6.QtTest import QTest

import harness

OPTIONS = [("import", "Import"), ("held", "Later"), ("never", "Never")]


@pytest.fixture(params=["light", "dark"])
def controls(request):
    _, q = harness.bootstrap()
    app = harness.app()
    harness.apply_theme(request.param)
    from internpearls import ui, widgets

    changes = []
    dialog = q.QDialog()
    row = q.QWidget()
    layout = q.QHBoxLayout(row)
    before = q.QLineEdit()
    link = ui.link_button("Open guide", lambda: changes.append("link"))
    cell = widgets.decision_cell(OPTIONS, "import", changes.append)
    after = q.QLineEdit()
    for widget in (before, link, cell, after):
        layout.addWidget(widget)
    q.QVBoxLayout(dialog).addWidget(row)
    dialog.show()
    dialog.activateWindow()
    before.setFocus()
    app.processEvents()
    yield request.param, q, app, dialog, row, before, link, cell, after, changes
    dialog.close()
    dialog.deleteLater()
    app.processEvents()


def _border(image, inset=0):
    dpr = image.devicePixelRatio()
    band = round(5 * dpr)
    start = round(inset * dpr)
    w, h = image.width(), image.height()
    return [image.pixel(x, y) for y in range(start, h - start)
            for x in range(start, w - start)
            if x < band or x >= w - band or y < band or y >= h - band]


def _geometry(row, buttons):
    return row.size(), row.sizeHint(), [(b.geometry(), b.sizeHint()) for b in buttons]


def _settle(app):
    app.processEvents()
    app.processEvents()


@pytest.mark.parametrize("align_left", [False, True])
def test_link_focus_paints_an_accent_ring(controls, align_left):
    _, q, app, _, row, before, link, cell, _, _ = controls
    from internpearls import palette, ui
    replacement = ui.link_button("Open guide", align_left=align_left)
    row.layout().replaceWidget(link, replacement)
    link.hide()
    app.processEvents()
    before.setFocus()
    rest = replacement.grab().toImage()
    geometry = _geometry(row, [replacement] + list(cell.buttons.values()))
    replacement.setFocus(q.Qt.FocusReason.TabFocusReason)
    app.processEvents()
    focused = replacement.grab().toImage()
    assert replacement.hasFocus()
    assert _border(rest) != _border(focused)
    assert q.QColor(palette.colors()["accent"]).rgba() in _border(focused)
    assert _geometry(row, [replacement] + list(cell.buttons.values())) == geometry


def test_disabled_link_paints_muted_text(controls):
    _, q, app, _, row, _, link, cell, _, _ = controls
    from internpearls import palette
    rest = link.grab().toImage()
    geometry = _geometry(row, [link] + list(cell.buttons.values()))
    link.setEnabled(False)
    app.processEvents()
    disabled = link.grab().toImage()
    assert disabled != rest
    assert any(disabled.pixelColor(x, y).name() == palette.colors()["muted"]
               for y in range(disabled.height()) for x in range(disabled.width()))
    assert _geometry(row, [link] + list(cell.buttons.values())) == geometry


def test_link_accepts_tab_without_becoming_an_enter_default(controls):
    _, q, app, dialog, _, before, link, cell, _, changes = controls
    assert not link.autoDefault()
    assert link.focusPolicy() == q.Qt.FocusPolicy.StrongFocus
    before.setFocus()
    QTest.keyClick(before, q.Qt.Key.Key_Tab)
    app.processEvents()
    assert link.hasFocus()
    QTest.keyClick(link, q.Qt.Key.Key_Return)
    assert "link" not in changes
    QTest.keyClick(link, q.Qt.Key.Key_Tab)
    assert cell.buttons["import"].hasFocus()


@pytest.mark.parametrize("value", [v for v, _ in OPTIONS])
@pytest.mark.parametrize("checked", [False, True])
def test_decision_focus_paints_inside_shared_borders(controls, value, checked):
    _, q, app, _, row, before, link, cell, _, _ = controls
    from internpearls import palette
    cell.set_state(value if checked else next(v for v, _ in OPTIONS if v != value))
    before.setFocus()
    _settle(app)
    button = cell.buttons[value]
    rest = button.grab().toImage()
    geometry = _geometry(row, [link] + list(cell.buttons.values()))
    button.setFocus(q.Qt.FocusReason.TabFocusReason)
    app.processEvents()
    focused = button.grab().toImage()
    assert button.hasFocus()
    assert _border(rest, inset=1) != _border(focused, inset=1)
    assert q.QColor(palette.colors()["accent"]).rgba() in _border(focused, inset=1)
    # The group's outer rules and shared seams keep their original paint.
    for x in (0, focused.width() - 1):
        for y in range(round(6 * focused.devicePixelRatio()),
                       focused.height() - round(6 * focused.devicePixelRatio())):
            assert rest.pixel(x, y) == focused.pixel(x, y)
    assert _geometry(row, [link] + list(cell.buttons.values())) == geometry


@pytest.mark.parametrize("value", [v for v, _ in OPTIONS])
@pytest.mark.parametrize("checked", [False, True])
def test_disabled_decision_has_muted_text_and_no_selected_fill(controls, value, checked):
    _, q, app, dialog, row, before, link, cell, _, changes = controls
    from internpearls import palette
    cell.set_state(value if checked else next(v for v, _ in OPTIONS if v != value))
    before.setFocus()
    _settle(app)
    button = cell.buttons[value]
    rest = button.grab().toImage()
    geometry = _geometry(row, [link] + list(cell.buttons.values()))
    button.setEnabled(False)
    app.processEvents()
    disabled = button.grab().toImage()
    assert disabled != rest
    assert any(disabled.pixelColor(x, y).name() == palette.colors()["muted"]
               for y in range(disabled.height()) for x in range(disabled.width()))
    scene = dialog.grab().toImage()
    point = button.mapTo(dialog, q.QPoint(7, button.height() // 2))
    dpr = scene.devicePixelRatio()
    assert scene.pixelColor(round(point.x() * dpr), round(point.y() * dpr)) == (
        row.palette().color(q.QPalette.ColorRole.Window))
    button.click()
    QTest.keyClick(button, q.Qt.Key.Key_Space)
    assert changes == []
    assert button.isChecked() == checked
    assert _geometry(row, [link] + list(cell.buttons.values())) == geometry


def test_tab_reaches_each_decision_and_space_selects_it(controls):
    _, q, app, _, _, _, link, cell, after, changes = controls
    link.setFocus()
    for value, _ in OPTIONS:
        QTest.keyClick(app.focusWidget(), q.Qt.Key.Key_Tab)
        button = cell.buttons[value]
        assert button.hasFocus()
        QTest.keyClick(button, q.Qt.Key.Key_Space)
        assert [v for v, b in cell.buttons.items() if b.isChecked()] == [value]
    assert changes == [v for v, _ in OPTIONS]
    QTest.keyClick(app.focusWidget(), q.Qt.Key.Key_Tab)
    assert after.hasFocus()


@pytest.mark.parametrize("kind", ["link", "decision"])
def test_shared_buttons_accept_demo_activation(controls, monkeypatch, kind):
    _, _, _, _, _, _, link, cell, _, changes = controls
    import mock_anki
    button = link if kind == "link" else cell.buttons["held"]
    button.wid = "shared-button"
    monkeypatch.setitem(mock_anki._widgets, button.wid, button)
    mock_anki.apply_actions({"actions": [{"type": "activate", "id": button.wid}]})
    assert changes == ["link" if kind == "link" else "held"]
    if kind == "decision":
        assert cell.buttons["held"].isChecked()


def test_link_sizes_match_their_existing_row_styles(controls):
    _, q, _, dialog, _, _, link, _, _, _ = controls
    from internpearls import dupes_dialog, palette
    c = palette.colors()
    legacy = q.QPushButton("Open guide", dialog)
    legacy.setFlat(True)
    legacy.setAutoDefault(False)
    legacy.setStyleSheet(f"color: {c['accent']}; font-size: 12px;")
    assert link.sizeHint() == legacy.sizeHint()
    compact = dupes_dialog._link("Open guide")
    compact.setParent(dialog)
    legacy.setStyleSheet(
        f"QPushButton {{ color: {c['accent']}; font-size: 12px;"
        " border: 1px solid transparent; border-radius: 3px; }")
    assert compact.sizeHint() == legacy.sizeHint()


def test_decision_sizes_match_the_existing_segmented_row(controls):
    _, q, _, dialog, _, _, _, cell, _, _ = controls
    from internpearls import palette
    c = palette.colors()
    roles = {"import": "accept", "held": "updated", "never": "decline"}
    for state, _ in OPTIONS:
        cell.set_state(state)
        for index, (value, label) in enumerate(OPTIONS):
            legacy = q.QPushButton(label, dialog)
            left, right = (6 if index == 0 else 0), (6 if index == 2 else 0)
            selected = (f"font-weight: 600; color: {c[roles[value] + '_fg']};"
                        f" background: {c[roles[value] + '_bg']};" if value == state
                        else f"color: {c['dim']}; background: transparent;")
            legacy.setStyleSheet(
                f"QPushButton {{ border: 1px solid {c['cell_rule']};"
                f"{' border-left: none;' if index else ''}"
                f" border-top-left-radius: {left}px; border-bottom-left-radius: {left}px;"
                f" border-top-right-radius: {right}px; border-bottom-right-radius: {right}px;"
                f" min-height: 22px; max-height: 22px; padding: 2px 9px;"
                f" font-size: 11px; {selected} }}")
            assert cell.buttons[value].sizeHint() == legacy.sizeHint()


def test_receipt_link_keeps_shared_states_and_its_compact_size(controls):
    _, q, app, dialog, _, before, _, _, _, _ = controls
    from internpearls import palette, review
    row = review._rewrite_row({}, "Why", "Old explanation", "New explanation", "Card")
    dialog.layout().addWidget(row)
    _settle(app)
    link = next(b for b in row.findChildren(q.QPushButton) if b.text() == "Show yours")
    legacy = q.QPushButton("Show yours")
    legacy.setFlat(True)
    legacy.setStyleSheet(f"color: {palette.colors()['accent']}; font-size: 12px;"
                        " border: none; padding: 0;")
    assert link.sizeHint() == legacy.sizeHint()
    before.setFocus()
    _settle(app)
    rest = link.grab().toImage()
    geometry = _geometry(row, [link])
    link.setFocus(q.Qt.FocusReason.TabFocusReason)
    app.processEvents()
    focused = link.grab().toImage()
    assert _border(rest) != _border(focused)
    link.setEnabled(False)
    app.processEvents()
    disabled = link.grab().toImage()
    assert disabled != rest
    assert any(disabled.pixelColor(x, y).name() == palette.colors()["muted"]
               for y in range(disabled.height()) for x in range(disabled.width()))
    assert _geometry(row, [link]) == geometry


def test_save_button_state_screenshots(controls, tmp_path):
    theme, q, app, dialog, _, before, link, cell, _, _ = controls
    out_dir = Path(os.environ.get("IP_SHOT_DIR") or tmp_path)
    out_dir.mkdir(parents=True, exist_ok=True)

    def save(widget, name):
        image = dialog.grab().toImage()
        origin = widget.mapTo(dialog, q.QPoint(0, 0))
        dpr = image.devicePixelRatio()
        rect = q.QRect(round(origin.x() * dpr), round(origin.y() * dpr),
                       round(widget.width() * dpr), round(widget.height() * dpr))
        assert image.copy(rect).save(str(out_dir / name))

    for state in ("rest", "focused", "disabled"):
        before.setFocus()
        if state == "disabled":
            link.setEnabled(False)
            cell.setEnabled(False)
        elif state == "focused":
            link.setFocus()
        app.processEvents()
        save(link, f"link-{theme}-{state}.png")
        if state == "focused":
            cell.buttons["held"].setFocus()
            app.processEvents()
        save(cell, f"decision-{theme}-{state}.png")
