"""The progress window's own properties, on the real QProgressDialog.

test_smoke.py asserts every rendered scene carries the "Intern Pearls" title, but the
progress window is not one of the harness's scenes: it never calls exec(), it shows
itself the moment its value is set, so render() cannot capture it. It is a dialog the
add-on opens all the same, and it was the one that carried Anki's generic title.

Also covers the two things ui.cancellable_progress's pump promises: that it reports
Cancel, and that pumping is safe because the window is modal.
"""
import contextlib
import sys

import harness
import pytest


def _native_mouse_event():
    import ctypes

    from PyQt6 import QtGui

    try:
        library = ctypes.CDLL(QtGui.__file__)
        post = getattr(library, "_ZN22QWindowSystemInterface16handleMouseEvent"
                       "INS_20AsynchronousDeliveryEEEbP7QWindowRK7QPointFS6_"
                       "6QFlagsIN2Qt11MouseButtonEES9_N6QEvent4TypeES7_"
                       "INS8_16KeyboardModifierEENS8_16MouseEventSourceE")
    except (AttributeError, OSError):
        pytest.skip("native mouse input is unavailable in this Qt build")
    post.argtypes = [ctypes.c_void_p] * 3 + [ctypes.c_int] * 5
    post.restype = ctypes.c_bool
    return post


def _post_mouse_click(widget, pos):
    """Queue window-system input, which Qt's input-exclusion flag can defer.

    QTest mouse clicks and posted QMouseEvents bypass that queue.
    """
    from PyQt6 import sip
    from aqt.qt import QEvent, QPointF, Qt

    post = _native_mouse_event()
    window = widget.window().windowHandle()
    local = QPointF(widget.mapTo(widget.window(), pos))
    global_pos = QPointF(widget.mapToGlobal(pos))
    for event, buttons in ((QEvent.Type.MouseButtonPress, Qt.MouseButton.LeftButton),
                           (QEvent.Type.MouseButtonRelease, Qt.MouseButton.NoButton)):
        post(sip.unwrapinstance(window), sip.unwrapinstance(local),
             sip.unwrapinstance(global_pos), buttons.value,
             Qt.MouseButton.LeftButton.value, event.value, 0, 0)


@contextlib.contextmanager
def _progress(title="Syncing decks", total=2):
    """cancellable_progress with a real widget standing in for mw.

    A real widget parent lets this check exercise window modality. Restored
    afterwards so no later scene parents itself to this throwaway window.
    """
    harness.bootstrap()
    app = harness.app()
    from aqt.qt import QProgressDialog, QWidget
    from internpearls import ui
    parent = QWidget()
    original = ui.mw
    ui.mw = parent
    try:
        with ui.cancellable_progress(title, total) as step:
            app.processEvents()
            # The visible one: a progress window from an earlier test in this file can
            # still be in the list, closed but not yet destroyed, and asking questions of
            # that one answers about a run that already finished.
            dlg = next(w for w in app.topLevelWidgets()
                       if isinstance(w, QProgressDialog) and w.isVisible())
            yield step, dlg
    finally:
        ui.mw = original
        parent.deleteLater()


def test_the_progress_window_carries_the_addon_title():
    from internpearls.config import APP_NAME
    with _progress() as (_step, dlg):
        assert dlg.windowTitle() == APP_NAME, (
            f"the progress window opened titled {dlg.windowTitle()!r}; every dialog in "
            "the add-on carries the add-on's own name")


def test_the_progress_window_is_modal_so_pumping_is_safe():
    """The pump runs queued events mid-download, so whatever else is on screen must not
    be clickable while it does."""
    from aqt.qt import Qt
    with _progress() as (_step, dlg):
        assert dlg.windowModality() == Qt.WindowModality.WindowModal


def test_the_progress_window_label_shows_markup_literally():
    from aqt.qt import QLabel, Qt
    with _progress() as (step, dlg):
        text = "Deck <b>bold</b> &amp; <i>x</i>"
        step(1, text)
        label = dlg.findChild(QLabel)
        assert label.textFormat() == Qt.TextFormat.PlainText
        assert label.text() == text


def test_the_pump_reports_cancel_without_advancing_the_bar():
    """What net's on_chunk sees: True while the run is live, False once Cancel is
    clicked, and no step consumed either way."""
    with _progress() as (step, dlg):
        before = dlg.value()
        assert step.pump() is True
        assert step.pump(65536) is True, "the pump must accept a bytes-so-far argument"
        assert dlg.value() == before, "pumping must not advance the progress bar"
        dlg.cancel()
        assert step.pump() is False
        assert step(1, "Syncing a deck (1 of 2)") is False, (
            "a cancelled run must still stop at the next step boundary, which is the "
            "only place an import may be skipped")


@pytest.mark.skipif(sys.platform != "darwin", reason="native mouse input requires macOS")
def test_slow_fetch_keeps_events_running_and_cancel_returns_promptly(monkeypatch):
    import threading
    import time

    import pytest
    from aqt.qt import QLabel, QProgressDialog, QPushButton, QTimer, Qt, QWidget
    from internpearls import ui
    from internpearls.config import APP_NAME

    _native_mouse_event()
    app = harness.app()
    parent = QWidget()
    monkeypatch.setattr(ui, "mw", parent)
    release = threading.Event()
    ticks, windows, threads = [], [], []
    main_thread = threading.get_ident()
    started = time.monotonic()
    timer = QTimer(parent)

    def tick():
        ticks.append(time.monotonic())
        for dlg in app.topLevelWidgets():
            if isinstance(dlg, QProgressDialog) and dlg.isVisible():
                windows.append((dlg.windowTitle(), dlg.windowModality(),
                                dlg.minimum(), dlg.maximum(),
                                dlg.findChild(QLabel).textFormat(),
                                dlg.findChild(QLabel).text(), time.monotonic()))
                cancel = dlg.findChild(QPushButton)
                _post_mouse_click(cancel, cancel.rect().center())
        if time.monotonic() - started > 1.5:
            release.set()

    def fetch():
        threads.append(threading.get_ident())
        release.wait(2)
        return "late result"

    timer.timeout.connect(tick)
    timer.start(10)
    try:
        with pytest.raises(ui.ProgressCancelled):
            ui.run_with_progress("Checking <b>the deck source</b>", fetch)
        returned = time.monotonic()
        assert not release.is_set(), "Cancel must return before the fetch finishes"
        assert len(ticks) >= 5
        assert windows
        title, modality, low, high, text_format, text, shown = windows[0]
        assert title == APP_NAME
        assert modality == Qt.WindowModality.WindowModal
        assert (low, high) == (0, 0)
        assert text_format == Qt.TextFormat.PlainText
        assert text == "Checking <b>the deck source</b>"
        assert 0.25 <= shown - started < 1
        assert returned - shown < 0.2
        assert threads[0] != main_thread
    finally:
        release.set()
        timer.stop()
        parent.deleteLater()
        app.processEvents()


def test_fast_fetch_does_not_show_a_progress_window(monkeypatch):
    from aqt.qt import QProgressDialog, QWidget
    from internpearls import ui

    app = harness.app()
    parent = QWidget()
    monkeypatch.setattr(ui, "mw", parent)
    shown = []
    original = QProgressDialog.showEvent

    def show_event(dlg, event):
        shown.append(dlg)
        original(dlg, event)

    monkeypatch.setattr(QProgressDialog, "showEvent", show_event)
    try:
        assert ui.run_with_progress("Checking the deck source", lambda: "ready") == "ready"
        assert not shown
    finally:
        parent.deleteLater()
        app.processEvents()


@pytest.mark.skipif(sys.platform != "darwin", reason="native mouse input requires macOS")
def test_mouse_click_skips_when_the_native_symbol_is_unavailable(monkeypatch):
    import ctypes

    monkeypatch.setattr(ctypes, "CDLL", lambda _path: object())
    with pytest.raises(pytest.skip.Exception, match="native mouse input"):
        _post_mouse_click(None, None)


def test_fetch_excludes_input_until_visible_and_cancel_returns(monkeypatch):
    import threading

    from aqt.qt import QEventLoop, QProgressDialog, QPushButton, QTimer, QWidget
    from internpearls import ui

    app = harness.app()
    parent = QWidget()
    monkeypatch.setattr(ui, "mw", parent)
    release = threading.Event()
    calls, cancelled = [], []
    process_events = ui.QApplication.processEvents
    timer = QTimer(parent)

    def record(flags=QEventLoop.ProcessEventsFlag.AllEvents):
        visible = any(dlg.isVisible() for dlg in parent.findChildren(QProgressDialog))
        calls.append((visible, flags))
        process_events(flags)

    def cancel():
        for dlg in parent.findChildren(QProgressDialog):
            if dlg.isVisible() and any(visible for visible, _flags in calls):
                cancelled.append(True)
                dlg.findChild(QPushButton).click()
                timer.stop()

    monkeypatch.setattr(ui.QApplication, "processEvents", record)
    timer.timeout.connect(cancel)
    timer.start(10)
    try:
        with pytest.raises(ui.ProgressCancelled):
            ui.run_with_progress("Checking the deck source", lambda: release.wait(2))
        assert cancelled == [True]
        assert not release.is_set(), "Cancel must return before the fetch finishes"
        hidden = [flags for visible, flags in calls if not visible]
        shown = [flags for visible, flags in calls if visible]
        assert hidden and shown, "the pump must run before and after the window appears"
        exclude = QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents
        assert all(flags & exclude for flags in hidden)
        assert all(not flags & exclude for flags in shown)
    finally:
        release.set()
        timer.stop()
        parent.deleteLater()
        process_events()


@pytest.mark.skipif(sys.platform != "darwin", reason="native mouse input requires macOS")
@pytest.mark.parametrize("duration", [150, 650], ids=["hidden", "shown"])
def test_fetch_defers_menu_input_and_modality_blocks_the_queued_click(monkeypatch, duration):
    import threading

    from aqt.qt import QMainWindow, QProgressDialog, QTimer
    from internpearls import ui

    _native_mouse_event()
    app = harness.app()
    parent = QMainWindow()
    menu = parent.menuBar()
    menu.setNativeMenuBar(False)
    action = menu.addAction("Run another flow")
    calls, clicks, visible = [], [], []
    action.triggered.connect(lambda: calls.append("triggered"))
    parent.show()
    app.processEvents()
    monkeypatch.setattr(ui, "mw", parent)
    release = threading.Event()
    click_timer = QTimer(parent)
    click_timer.setSingleShot(True)
    finish_timer = QTimer(parent)
    finish_timer.setSingleShot(True)
    observe_timer = QTimer(parent)

    def click():
        clicks.append(any(dlg.isVisible() for dlg in parent.findChildren(QProgressDialog)))
        _post_mouse_click(menu, menu.actionGeometry(action).center())

    def observe():
        if any(dlg.isVisible() for dlg in parent.findChildren(QProgressDialog)):
            visible.append(True)

    click_timer.timeout.connect(click)
    finish_timer.timeout.connect(release.set)
    observe_timer.timeout.connect(observe)
    click_timer.start(30)
    finish_timer.start(duration)
    observe_timer.start(10)
    try:
        assert ui.run_with_progress("Checking the deck source", lambda: release.wait(2))
        assert clicks == [False], "the click must be queued before the window appears"
        assert not calls, "menu input ran before the fetch returned"
        assert bool(visible) == (duration == 650)
        app.processEvents()
        assert calls == ([] if visible else ["triggered"])
        calls.clear()
        click()
        app.processEvents()
        assert calls == ["triggered"], "the menu must work after the check"
    finally:
        release.set()
        click_timer.stop()
        finish_timer.stop()
        observe_timer.stop()
        parent.close()
        parent.deleteLater()
        app.processEvents()


def test_destroying_the_parent_abandons_a_blocked_fetch(monkeypatch):
    import threading

    import pytest
    from PyQt6 import sip
    from aqt.qt import QTimer, QWidget
    from internpearls import ui

    app = harness.app()
    parent = QWidget()
    monkeypatch.setattr(ui, "mw", parent)
    release = threading.Event()
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(lambda: sip.delete(parent))
    timer.start(10)
    try:
        with pytest.raises(ui.ProgressCancelled):
            ui.run_with_progress("Checking the deck source", lambda: release.wait(1))
        assert not release.is_set()
    finally:
        release.set()
        timer.stop()
        if not sip.isdeleted(parent):
            parent.deleteLater()
        app.processEvents()
