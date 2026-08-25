"""What widgets.StreamingList does against real geometry: fill the viewport it is
actually given, and refill it when the reader makes the dialog bigger.

The mock suite can only drive the arithmetic (mock_anki reports every height as 0), and
the bug this guards was invisible there: enlarge the dialog before scrolling and the
scrollbar's range collapses to zero, so valueChanged never fires again and every row past
the first batch is never built. Rows here are fixed-height labels so "how many rows fit"
is arithmetic rather than a font measurement.
"""
import harness

_ROW_H = 30
_ITEMS = 400


def _open(height, batch=5):
    """A StreamingList of fixed-height rows, alone in a shown dialog of `height`."""
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
    lay.setContentsMargins(0, 0, 0, 0)
    lst = StreamingList(build, list(range(_ITEMS)), batch=batch)
    lay.addWidget(lst)
    dlg.resize(360, height)
    dlg.show()
    app.processEvents()
    return app, dlg, lst


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
