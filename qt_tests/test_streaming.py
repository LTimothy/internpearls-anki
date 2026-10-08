"""What widgets.StreamingList does against real geometry: fill the viewport it is
actually given, and refill it when the reader makes the dialog bigger.

The mock suite can only drive the arithmetic (mock_anki reports every height as 0), and
the bug this guards was invisible there: enlarge the dialog before scrolling and the
scrollbar's range collapses to zero, so valueChanged never fires again and every row past
the first batch is never built. Rows here are fixed-height labels so "how many rows fit"
is arithmetic rather than a font measurement.
"""
import harness
import pytest

_ROW_H = 30
_ITEMS = 400


def _open(height, batch=5, tall=()):
    """A StreamingList of fixed-height rows, alone in a shown dialog of `height`."""
    harness.bootstrap()
    app = harness.app()
    from aqt.qt import QDialog, QLabel, QVBoxLayout
    from internpearls.widgets import StreamingList

    def build(item):
        row = QLabel(f"Row {item}")
        row.setFixedHeight(600 if item in tall else _ROW_H)
        return row

    dlg = QDialog()
    lay = QVBoxLayout(dlg)
    lay.setContentsMargins(0, 0, 0, 0)
    lst = StreamingList(build, list(range(_ITEMS)), batch=batch)
    lay.addWidget(lst)
    dlg.resize(360, height)
    dlg.show()
    _settle(app)
    return app, dlg, lst


def _settle(app):
    from PyQt6.QtTest import QTest
    for _ in range(3):
        app.processEvents()
    QTest.qWait(1)


def _bottom(lst):
    bar = lst.verticalScrollBar()
    bar.blockSignals(True)
    bar.setValue(bar.maximum())
    bar.blockSignals(False)
    return bar


@pytest.mark.parametrize('operation', ['remove', 'replace'])
def test_shortening_a_tall_row_at_the_bottom_reveals_more_rows(operation):
    app, dlg, lst = _open(300, batch=100, tall=(0, 1))
    try:
        _settle(app)
        bar = _bottom(lst)
        maximum = bar.maximum()
        before = lst.shown()
        if operation == 'remove':
            lst.remove_item(0)
        else:
            lst.replace_item(0, 400)
        _settle(app)
        assert lst.shown() > before
        assert bar.value() <= maximum
        assert lst.shown() < lst.total()
    finally:
        dlg.close()


@pytest.mark.parametrize('action', ['down', 'page_down', 'end', 'ctrl_end',
                                    'space', 'page_step', 'resize', 'resize_width',
                                    'resize_shrink'])
def test_downward_navigation_after_a_tall_row_removal_reveals_more_rows(action):
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QAbstractSlider
    app, dlg, lst = _open(300, batch=100, tall=(0, 1))
    try:
        _settle(app)
        _bottom(lst)
        lst.remove_item(0)
        _settle(app)
        dlg.activateWindow()
        lst.setFocus()
        _settle(app)
        bar = _bottom(lst)
        before = lst.shown()
        assert bar.value() == bar.maximum()
        assert before < lst.total()
        if action == 'page_step':
            bar.triggerAction(QAbstractSlider.SliderAction.SliderPageStepAdd)
        elif action == 'resize':
            dlg.resize(360, 350)
        elif action == 'resize_width':
            dlg.resize(400, 300)
        elif action == 'resize_shrink':
            dlg.resize(360, 250)
        else:
            key = {'down': Qt.Key.Key_Down, 'page_down': Qt.Key.Key_PageDown,
                   'end': Qt.Key.Key_End, 'ctrl_end': Qt.Key.Key_End,
                   'space': Qt.Key.Key_Space}[action]
            modifier = (Qt.KeyboardModifier.ControlModifier if action == 'ctrl_end'
                        else Qt.KeyboardModifier.NoModifier)
            QTest.keyClick(lst, key, modifier)
        _settle(app)
        assert lst.shown() > before
        assert lst.shown() < lst.total()
    finally:
        dlg.close()


def test_removals_refill_a_viewport_with_rows_still_unshown():
    app, dlg, lst = _open(140, batch=5)
    try:
        _settle(app)
        before = lst.shown()
        for _ in range(before - 2):
            lst.remove_item(0)
        _settle(app)
        assert lst.shown() > 2
        assert lst._rows_container.sizeHint().height() > lst.viewport().height()
        assert lst.shown() < lst.total()
    finally:
        dlg.close()


@pytest.mark.parametrize('height', [_ROW_H, 650])
def test_a_changed_scroll_range_reveals_more_rows_near_the_bottom(height):
    app, dlg, lst = _open(300, batch=100, tall=(0, 1))
    try:
        _settle(app)
        _bottom(lst)
        before = lst.shown()
        lst.rows()[0].setFixedHeight(height)
        _settle(app)
        assert lst.shown() > before
    finally:
        dlg.close()


@pytest.mark.parametrize('action', ['single_step', 'page_step'])
def test_a_downward_action_that_moves_the_scrollbar_reveals_one_batch(action):
    from PyQt6.QtWidgets import QAbstractSlider
    app, dlg, lst = _open(300, batch=100, tall=(0, 1))
    try:
        bar = lst.verticalScrollBar()
        bar.blockSignals(True)
        bar.setValue(bar.maximum() - 50)
        bar.blockSignals(False)
        lst._scroll_value = bar.value()
        before = lst.shown()
        actions = QAbstractSlider.SliderAction
        bar.triggerAction(actions.SliderSingleStepAdd if action == 'single_step'
                          else actions.SliderPageStepAdd)
        _settle(app)
        assert lst.shown() == before + 100
    finally:
        dlg.close()


@pytest.mark.parametrize('action', ['value', 'up', 'page_up', 'page_step', 'shift_space'])
def test_scrolling_up_near_the_bottom_does_not_reveal_rows(action):
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QAbstractSlider
    app, dlg, lst = _open(300, batch=100, tall=(0, 1))
    try:
        _settle(app)
        dlg.activateWindow()
        lst.setFocus()
        _settle(app)
        bar = _bottom(lst)
        lst._scroll_value = bar.value()
        before = lst.shown()
        if action == 'value':
            bar.setValue(bar.value() - 1)
        elif action == 'page_step':
            bar.triggerAction(QAbstractSlider.SliderAction.SliderPageStepSub)
        else:
            key = {'up': Qt.Key.Key_Up, 'page_up': Qt.Key.Key_PageUp,
                   'shift_space': Qt.Key.Key_Space}[action]
            modifier = (Qt.KeyboardModifier.ShiftModifier if action == 'shift_space'
                        else Qt.KeyboardModifier.NoModifier)
            QTest.keyClick(lst, key, modifier)
        _settle(app)
        assert lst.shown() == before
    finally:
        dlg.close()


def test_a_list_fills_the_viewport_it_opens_with():
    """Opening must leave no gap under the last built row: whatever the viewport can
    show is built, since a list shorter than its viewport has no scrollbar to move."""
    app, dlg, lst = _open(400)
    assert lst.shown() * _ROW_H >= lst.viewport().height(), (
        f"{lst.shown()} rows do not fill a {lst.viewport().height()}px viewport")
    assert lst.shown() < lst.total(), "the list built more than it needed to"
    dlg.close()


def test_growing_the_dialog_builds_the_rows_the_bigger_viewport_shows():
    """The regression itself. No scrolling happens here at all: the dialog is enlarged,
    which is exactly the case the scrollbar signal cannot see."""
    app, dlg, lst = _open(220)
    before = lst.shown()
    dlg.resize(360, 900)
    app.processEvents()
    after = lst.shown()
    assert after > before, (
        f"enlarging the dialog stranded every unbuilt row ({before} rows before and "
        f"after, viewport now {lst.viewport().height()}px)")
    assert after * _ROW_H >= lst.viewport().height(), (
        f"{after} rows still do not fill a {lst.viewport().height()}px viewport")
    dlg.close()


def test_growing_a_dialog_over_a_long_list_still_leaves_rows_unbuilt():
    """The property test_performance.py pins, under the resize path too: filling a
    viewport is not building the backlog."""
    app, dlg, lst = _open(220)
    dlg.resize(360, 900)
    app.processEvents()
    assert lst.shown() < lst.total(), (
        "a resize built every row; the list must still only build what it shows")
    dlg.close()


def test_a_list_shorter_than_its_viewport_builds_every_row_and_stops():
    """A viewport with room to spare exhausts the list rather than overrunning it."""
    harness.bootstrap()
    app = harness.app()
    from aqt.qt import QDialog, QLabel, QVBoxLayout
    from internpearls.widgets import StreamingList

    def build(item):
        row = QLabel(f"Row {item}")
        row.setFixedHeight(_ROW_H)
        return row

    dlg = QDialog()
    lay = QVBoxLayout(dlg)
    lst = StreamingList(build, list(range(6)), batch=5)
    lay.addWidget(lst)
    dlg.resize(360, 900)
    dlg.show()
    app.processEvents()
    assert lst.shown() == 6 and lst.shown() == lst.total()
    dlg.close()


def test_idle_prefetch_builds_the_backlog_hidden_and_reveals_it_on_scroll():
    """Once the viewport is filled, rows ahead build a few at a time on the idle
    timer, but those rows stay hidden so the content height (and
    the scrollbar) does not move while the reader is still. Scrolling reveals the
    prebuilt rows instead of building them."""
    import time
    app, dlg, lst = _open(220)
    shown = lst.shown()
    height = lst.widget().sizeHint().height()
    deadline = time.monotonic() + 3
    while lst.built() < shown + 6 and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert lst.built() >= shown + 6, f"idle prefetch stalled at {lst.built()}"
    assert lst.shown() == shown, "prefetch must not reveal rows on its own"
    assert lst.widget().sizeHint().height() == height, "hidden rows moved the content height"
    built_before = lst.built()
    bar = lst.verticalScrollBar()
    bar.setValue(bar.maximum())
    app.processEvents()
    assert lst.shown() > shown
    assert lst.built() >= built_before
    dlg.close()


def test_a_row_that_grows_after_it_is_shown_is_not_clipped():
    """Rows sit in pages that report a plain height, so a row opening its body must
    still make its page, and the list, grow to fit it."""
    harness.bootstrap()
    app = harness.app()
    from aqt.qt import QDialog, QLabel, QVBoxLayout, QWidget
    from internpearls.widgets import StreamingList

    bodies = []

    def build(item):
        row = QWidget()
        lay = QVBoxLayout(row)
        heading = QLabel(f"Row {item}, with a heading long enough to wrap in a narrow "
                         "dialog like this one")
        heading.setWordWrap(True)
        lay.addWidget(heading)
        body = QLabel("An opened body that wraps onto more than one line of text "
                      "once the row is expanded by the reader")
        body.setWordWrap(True)
        body.setVisible(False)
        lay.addWidget(body)
        bodies.append(body)
        return row

    dlg = QDialog()
    lst = StreamingList(build, list(range(12)), batch=5)
    QVBoxLayout(dlg).addWidget(lst)
    dlg.resize(300, 500)
    dlg.show()
    app.processEvents()
    page = lst._pages[0]
    before = page.height()
    bodies[1].setVisible(True)
    app.processEvents()
    row = lst.rows()[1]
    assert page.height() > before, "the page did not grow when a row opened"
    assert row.geometry().bottom() < page.height(), "the opened row is clipped by its page"
    dlg.close()


def test_pages_follow_the_list_width_so_wrapped_rows_are_not_clipped():
    """A page's height depends on how its wrapped rows break, so it must be measured
    again whenever the list gets narrower or wider, including its first real layout."""
    harness.bootstrap()
    app = harness.app()
    from aqt.qt import QDialog, QLabel, QVBoxLayout
    from internpearls.widgets import StreamingList

    def build(item):
        row = QLabel(f"Row {item}: a long label that takes more lines as the dialog "
                     "narrows, and fewer as it widens again " * 2)
        row.setWordWrap(True)
        return row

    dlg = QDialog()
    lst = StreamingList(build, list(range(30)), batch=10)
    QVBoxLayout(dlg).addWidget(lst)

    def fits():
        page = lst._pages[0]
        return page.height() == page.layout().totalHeightForWidth(page.width())

    def settle():
        # A scrollbar appearing or going narrows the list, which takes a second pass.
        _settle(app)

    dlg.resize(700, 500)
    dlg.show()
    settle()
    assert fits(), "the first layout left a page at a height measured for another width"
    for width in (300, 900):
        dlg.resize(width, 500)
        settle()
        assert fits(), f"at {width}px a page kept the height of the previous width"
    dlg.close()


def test_wheeling_down_after_a_removal_at_the_bottom_reveals_the_next_rows():
    from PyQt6.QtCore import QPoint, QPointF, Qt
    from PyQt6.QtGui import QWheelEvent
    app, dlg, lst = _open(90, batch=5)
    for _ in range(3):
        app.processEvents()
    bar = lst.verticalScrollBar()
    bar.blockSignals(True)
    bar.setValue(bar.maximum())
    bar.blockSignals(False)
    lst.remove_item(0)
    for _ in range(3):
        app.processEvents()
    _bottom(lst)
    before = lst.shown()
    assert bar.value() == bar.maximum()
    pos = lst.viewport().rect().center()
    event = QWheelEvent(QPointF(pos), QPointF(lst.viewport().mapToGlobal(pos)),
                        QPoint(), QPoint(0, -120), Qt.MouseButton.NoButton,
                        Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.ScrollUpdate, False)
    app.sendEvent(lst.viewport(), event)
    app.processEvents()
    assert lst.shown() > before
    assert [row.text() for row in lst.rows()[:lst.shown()]] == [
        f'Row {i}' for i in range(1, lst.shown() + 1)]
    dlg.close()
