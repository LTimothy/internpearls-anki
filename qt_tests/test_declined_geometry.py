"""The Declined cards dialog opens at a size that shows a short list in full and gives a
long one a scrolling list of the old height, without anyone resizing it first."""
import json

import harness


def _registry(tmp_path, monkeypatch, entries):
    from internpearls import config
    path = tmp_path / "declined.json"
    path.write_text(json.dumps(entries), encoding="utf8")
    monkeypatch.setattr(config, "DECLINED", str(path))


def _entry(state, i):
    return {"state": state, "front": f"Declined card {i}", "deck": "Example Deck",
            "decided": "2026-08-01", "hash": ""}


def _open():
    from internpearls import dialogs
    app = harness.app()
    dlg = dialogs._DeclinedDialog(None)
    dlg.show()
    for _ in range(3):
        app.processEvents()
    return app, dlg


def _inside_viewport(dlg, widget):
    from PyQt6.QtCore import QPoint
    lst = dlg._list
    top = widget.mapTo(lst.viewport(), QPoint(0, 0)).y()
    return top >= 0 and top + widget.height() <= lst.viewport().height()


def test_two_entries_in_two_groups_show_in_full(tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QLabel, QPushButton
    harness.bootstrap()
    _registry(tmp_path, monkeypatch, {"a": _entry("held", 1), "b": _entry("never", 2)})
    app, dlg = _open()
    labels = {w.text(): w for w in dlg.findChildren(QLabel) if w.isVisible()}
    assert "Later" in labels and "Never imported" in labels
    for text in ("Later", "Never imported"):
        assert _inside_viewport(dlg, labels[text]), f"{text} is below the fold"
    buttons = [b for b in dlg.findChildren(QPushButton) if b.text() == "Offer again"]
    assert len(buttons) == 2 and all(_inside_viewport(dlg, b) for b in buttons)
    assert dlg._list.height() > 90
    dlg.close()
    dlg.deleteLater()


def test_a_short_list_does_not_leave_a_tall_empty_list(tmp_path, monkeypatch):
    harness.bootstrap()
    _registry(tmp_path, monkeypatch, {"a": _entry("held", 1)})
    _app, dlg = _open()
    assert dlg._list.height() < 340
    dlg.close()
    dlg.deleteLater()


def test_two_thousand_entries_get_the_full_height_and_scroll(tmp_path, monkeypatch):
    from internpearls import dialogs
    harness.bootstrap()
    states = ("held", "never", "frozen", "keep")
    _registry(tmp_path, monkeypatch, {f"g{i}": _entry(states[i % 4], i)
                                      for i in range(2000)})
    _app, dlg = _open()
    assert dlg._list.height() >= dialogs._DECLINED_LIST_H
    assert dlg._list.verticalScrollBar().maximum() > 0
    dlg.close()
    dlg.deleteLater()


def test_offer_again_shrinks_the_minimum_back_for_a_short_list(tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QPushButton
    harness.bootstrap()
    _registry(tmp_path, monkeypatch, {"a": _entry("held", 1), "b": _entry("never", 2)})
    app, dlg = _open()
    two = dlg._list.minimumHeight()
    next(b for b in dlg.findChildren(QPushButton)
         if b.text() == "Offer again" and b.isVisible()).click()
    app.processEvents()
    assert 0 < dlg._list.minimumHeight() < two
    dlg.close()
    dlg.deleteLater()


def test_a_front_that_wraps_at_the_dialog_width_is_not_cut(tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QLabel, QPushButton
    harness.bootstrap()
    long_front = ("A deliberately long prompt, written to run well past the dialog's "
                  "width so that the label wraps onto a second and a third line " * 2)
    entries = {"a": _entry("held", 1), "b": _entry("never", 2)}
    entries["b"]["front"] = long_front
    _registry(tmp_path, monkeypatch, entries)
    app, dlg = _open()
    for _ in range(3):
        app.processEvents()
    wrapped = next(w for w in dlg.findChildren(QLabel) if w.text() == long_front)
    assert wrapped.height() > 2 * wrapped.fontMetrics().height(), "the front did not wrap"
    assert dlg._list.verticalScrollBar().maximum() == 0, "the list scrolls for two rows"
    for button in (b for b in dlg.findChildren(QPushButton) if b.text() == "Offer again"):
        assert _inside_viewport(dlg, button)
    assert _inside_viewport(dlg, wrapped)
    dlg.close()
    dlg.deleteLater()


def test_widening_the_dialog_shrinks_the_list_back(tmp_path, monkeypatch):
    harness.bootstrap()
    long_front = ("A deliberately long prompt, written to run well past the dialog's "
                  "width so that the label wraps onto a second and a third line " * 2)
    entries = {"a": _entry("held", 1), "b": _entry("never", 2)}
    entries["b"]["front"] = long_front
    _registry(tmp_path, monkeypatch, entries)
    app, dlg = _open()
    narrow = dlg._list.minimumHeight()
    dlg.resize(1600, dlg.height())
    for _ in range(3):
        app.processEvents()
    assert dlg._list.minimumHeight() < narrow
    dlg.close()
    dlg.deleteLater()
