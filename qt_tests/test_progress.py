"""The progress window's own properties, on the real QProgressDialog.

test_smoke.py asserts every rendered scene carries the "Intern Pearls" title, but the
progress window is not one of the harness's scenes: it never calls exec(), it shows
itself the moment its value is set, so render() cannot capture it. It is a dialog the
add-on opens all the same, and it was the one that carried Anki's generic title.

Also covers the two things ui.cancellable_progress's pump promises: that it reports
Cancel, and that pumping is safe because the window is modal.
"""
import contextlib

import harness


@contextlib.contextmanager
def _progress(title="Syncing decks", total=2):
    """cancellable_progress with a real widget standing in for mw.

    Real Qt rejects a non-widget parent outright, and the mock's mw is a plain object
    (the harness only patches QDialog's own __init__, which QProgressDialog does not
    inherit at the Python level). Restored afterwards so no later scene parents itself
    to this throwaway window.
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
