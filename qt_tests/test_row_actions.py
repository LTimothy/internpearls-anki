import time

import harness
import pytest

from test_dupes_scale import _open, _populate_many, _show
from test_declined_geometry import _entry, _registry, _open as _open_declined


def _settle(app):
    for _ in range(3):
        app.processEvents()


def _measure(app, lst, action, name):
    calls = []
    build = lst._build_row

    def counted(item):
        calls.append(item)
        return build(item)

    lst._build_row = counted
    position = lst.verticalScrollBar().value()
    start = time.perf_counter()
    action()
    _settle(app)
    elapsed = time.perf_counter() - start
    print(f'{name}: {elapsed:.6f}s, {len(calls)} item builds, scroll {position} -> {lst.verticalScrollBar().value()}')
    return calls, position


@pytest.mark.parametrize('action', ['suspend', 'ignore'])
def test_deep_duplicate_action_builds_at_most_one_row_and_keeps_scroll(monkeypatch, action):
    from PyQt6.QtWidgets import QPushButton
    mock, _ = harness.bootstrap()
    app = harness.app()
    harness._ai_backend_available('claude')
    _populate_many(mock, pairs=200)
    dlg = _open(mock)
    try:
        _show(dlg)
        lst = dlg._list
        while lst.shown() < 350:
            lst._extend()
        _settle(app)
        bar = lst.verticalScrollBar()
        bar.setValue(bar.maximum() * 2 // 3)
        _settle(app)
        # Act on a row at the reader's current position.
        from PyQt6.QtCore import QPoint
        index = next(i for i in range(0, lst.shown(), 2)
                     if lst.rows()[i].mapTo(lst.viewport(), QPoint(0, 0)).y() >= 0)
        pair = lst._items[index][1]
        row = lst.rows()[index]
        text = 'Suspend ours' if action == 'suspend' else 'Ignore pair'
        button = next(b for b in row.findChildren(QPushButton) if b.text() == text)
        dlg.activateWindow()
        button.setFocus()
        _settle(app)
        assert button.hasFocus()
        next_control = next(b for b in lst.rows()[index + 2].findChildren(QPushButton)
                            if b.text() == '▸')
        calls, position = _measure(app, lst, button.click, f'dupes 200 {action}')
        assert len(calls) <= 1
        assert bar.value() == position
        if action == 'suspend':
            updated = next(b for b in lst.rows()[index].findChildren(QPushButton)
                           if b.text() == 'Unsuspend ours')
            assert updated.hasFocus()
            assert 'currently suspended' in updated.accessibleName()
        else:
            assert next_control.hasFocus()
            assert '199 candidates' in dlg.summary_label.text()
            assert len(dlg._list._items) == 397
    finally:
        dlg.close()
        dlg.deleteLater()
        _settle(app)


def test_deep_offer_again_builds_at_most_one_row_and_keeps_scroll(tmp_path, monkeypatch):
    from PyQt6.QtCore import QPoint
    from PyQt6.QtWidgets import QPushButton
    harness.bootstrap()
    _registry(tmp_path, monkeypatch, {f'g{i}': _entry('held', i) for i in range(1000)})
    app, dlg = _open_declined()
    try:
        lst = dlg._list
        while lst.shown() < 850:
            lst._extend()
        _settle(app)
        bar = lst.verticalScrollBar()
        bar.setValue(bar.maximum() * 2 // 3)
        _settle(app)
        index = next(i for i in range(1, lst.shown())
                     if lst.rows()[i].mapTo(lst.viewport(), QPoint(0, 0)).y() >= 0)
        button = lst.rows()[index].findChildren(QPushButton)[0]
        next_control = lst.rows()[index + 1].findChildren(QPushButton)[0]
        dlg.activateWindow()
        button.setFocus()
        _settle(app)
        assert button.hasFocus()
        calls, position = _measure(app, lst, button.click, 'declined 1000 offer again')
        assert len(calls) <= 1
        assert bar.value() == position
        assert next_control.hasFocus()
        assert next_control.accessibleName().startswith('Offer again: Declined card ')
        assert lst.total() == 1000
    finally:
        dlg.close()
        dlg.deleteLater()
        _settle(app)


@pytest.mark.parametrize('index', [3, 7])
def test_removal_at_the_built_boundary_focuses_the_next_control(index):
    from PyQt6.QtWidgets import QDialog, QPushButton, QVBoxLayout
    from internpearls.widgets import StreamingList
    app = harness.app()
    dlg = QDialog()
    calls = []

    def build(item):
        calls.append(item)
        row = QPushButton(str(item))
        row.setFixedHeight(30)
        return row

    lst = StreamingList(build, list(range(12)), batch=4)
    QVBoxLayout(dlg).addWidget(lst)
    dlg.resize(300, 130)
    dlg.show()
    _settle(app)
    if index == 7:
        lst._build_upto(8)
        lst._extend()
    old = lst.rows()[index]
    dlg.activateWindow()
    bar = lst.verticalScrollBar()
    bar.blockSignals(True)
    old.setFocus()
    _settle(app)
    bar.blockSignals(False)
    assert old.hasFocus()
    assert lst.built() == index + 1
    calls.clear()
    lst.remove_item(index)
    _settle(app)
    assert app.focusWidget().text() == str(index + 1)
    assert calls == [index + 1]
    dlg.close()
    dlg.deleteLater()


def test_removing_the_last_entry_focuses_the_previous_control(tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QPushButton
    harness.bootstrap()
    _registry(tmp_path, monkeypatch, {'a': _entry('held', 1), 'b': _entry('never', 2)})
    app, dlg = _open_declined()
    controls = [w for w in dlg.findChildren(QPushButton) if w.text() == 'Offer again']
    dlg.activateWindow()
    controls[-1].setFocus()
    _settle(app)
    controls[-1].click()
    _settle(app)
    assert controls[0].hasFocus()
    assert [i[1] for i in dlg._list._items if i[0] == 'heading'] == ['Later']
    dlg.close()
    dlg.deleteLater()


def test_ignore_at_the_scroll_boundary_does_not_build_another_batch():
    mock, _ = harness.bootstrap()
    app = harness.app()
    harness._ai_backend_available('claude')
    _populate_many(mock, pairs=200)
    dlg = _open(mock)
    try:
        _show(dlg)
        lst = dlg._list
        while lst.shown() < 350:
            lst._extend()
        _settle(app)
        bar = lst.verticalScrollBar()
        # Block the scroll handler to put the reader at the edge of the built rows.
        bar.blockSignals(True)
        bar.setValue(bar.maximum())
        bar.blockSignals(False)
        _settle(app)
        calls, position = _measure(app, lst, lambda: dlg._ignore(dlg._pairs[150]),
                                   'dupes boundary ignore')
        assert len(calls) <= 1
        assert bar.value() == min(position, bar.maximum())
    finally:
        dlg.close()
        dlg.deleteLater()
        _settle(app)
