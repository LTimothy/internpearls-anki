"""The shared filter bar and saved notes in the real saved-decisions dialog."""
import pytest

import harness
from test_declined_geometry import _open, _registry
from test_filter_bar import _search


@pytest.fixture
def declined_dialog(tmp_path, monkeypatch):
    harness.bootstrap()
    _registry(tmp_path, monkeypatch, {
        "a": {"state": "held", "front": "Alpha prompt", "deck": "IP::Medicine",
              "note": "Check <b>dose</b> & wording", "decided": "", "hash": ""},
        "b": {"state": "never", "front": "Beta prompt", "deck": "IP::Surgery",
              "decided": "", "hash": ""}})
    _, dlg = _open()
    yield dlg
    dlg.close()
    dlg.deleteLater()


def _bar(dlg):
    from internpearls.widgets import FilterBar
    bar = dlg.findChild(FilterBar)
    assert bar is not None and bar.isVisible()
    return bar


def test_declined_filter_bar_is_above_the_list_and_filters_real_rows(declined_dialog):
    from PyQt6.QtWidgets import QLabel, QPushButton
    dlg = declined_dialog
    assert dlg.windowTitle().endswith(": Later and declined cards")
    bar = _bar(dlg)
    assert bar.mapTo(dlg, bar.rect().bottomLeft()).y() <= dlg._list.mapTo(
        dlg, dlg._list.rect().topLeft()).y()
    _search(bar, "DOSE")
    assert bar._count.text() == "Showing 1 of 2 cards"
    buttons = [b for b in dlg.findChildren(QPushButton) if b.text() == "Offer again" and b.isVisible()]
    assert [b.accessibleName() for b in buttons] == ["Offer again: Alpha prompt"]
    assert "Never imported" not in [w.text() for w in dlg.findChildren(QLabel) if w.isVisible()]
    bar.options.buttons["never"].click()
    assert bar._count.text() == "Showing 0 of 2 cards"
    _search(bar, "surgery")
    assert bar._count.text() == "Showing 1 of 2 cards"
    harness.app().processEvents()
    next(b for b in dlg.findChildren(QPushButton) if b.text() == "Offer again" and b.isVisible()).click()
    assert bar._count.text() == "Showing 0 of 1 cards"
    assert bar.options.buttons["never"].isChecked() and bar.search.text() == "surgery"


def test_declined_saved_note_is_plain_text_below_the_front(declined_dialog):
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QLabel
    labels = {w.text(): w for w in declined_dialog.findChildren(QLabel) if w.isVisible()}
    note = labels.get("Your note: Check <b>dose</b> & wording")
    assert note is not None
    assert note.textFormat() == Qt.TextFormat.PlainText
    assert note.mapTo(declined_dialog, note.rect().topLeft()).y() >= labels["Alpha prompt"].mapTo(
        declined_dialog, labels["Alpha prompt"].rect().bottomLeft()).y()
    assert not any("<ul>" in text or "<li>" in text for text in labels)
    help_label = labels["Offer again forgets the decision, so the card comes back on your next Update my decks."]
    assert help_label.textFormat() == Qt.TextFormat.PlainText


def test_declined_filter_reuses_search_escape_and_tab_focus(declined_dialog):
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    dlg = declined_dialog
    bar = _bar(dlg)
    last = bar.options.buttons["frozen"]
    last.setFocus()
    dlg.focusNextChild()
    assert dlg.focusWidget() is bar.search
    _search(bar, "Alpha")
    QTest.keyClick(bar.search, Qt.Key.Key_Escape)
    bar._timer._fire()
    assert dlg.isVisible() and bar.search.text() == ""
    assert not bar._count.isVisible()


def test_offer_again_keeps_scroll_position_with_both_filters(tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QPushButton
    harness.bootstrap()
    _registry(tmp_path, monkeypatch, {
        f"g{i}": {"state": "held" if i < 200 else "never", "front": f"Prompt {i}",
                  "deck": "IP::Medicine", "decided": "", "hash": ""}
        for i in range(400)})
    app, dlg = _open()
    try:
        filters = _bar(dlg)
        filters.options.buttons["held"].click()
        _search(filters, "medicine")
        lst = dlg._list
        assert lst.shown() < lst.total(), "filtering must still stream long lists"
        scroll = lst.verticalScrollBar()
        for _ in range(2):
            scroll.setValue(scroll.maximum())
            app.processEvents()
        before = scroll.value()
        assert before > 0
        viewport_top = lst.viewport().mapToGlobal(lst.viewport().rect().topLeft()).y()
        button = next(b for b in dlg.findChildren(QPushButton)
                      if b.text() == "Offer again" and b.isVisible()
                      and b.mapToGlobal(b.rect().topLeft()).y() > viewport_top)
        button.click()
        app.processEvents()
        assert abs(scroll.value() - before) < 200
        assert filters._count.text() == "Showing 199 of 399 cards"
        assert filters.options.buttons["held"].isChecked()
        assert filters.search.text() == "medicine"
    finally:
        dlg.close()
        dlg.deleteLater()
