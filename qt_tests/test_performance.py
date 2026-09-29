"""Guards the property the Update my decks screen's whole design rests on: opening it
must not depend on how many cards are pending.

widgets.StreamingList's own docstring has the measured cost this bounds: about 2ms per
card to build and show a row, offscreen. Building every row for a large first sync
(nearly 3,000 cards, the largest deck this add-on ships today) up front would freeze the UI for
5.7 seconds with no feedback and no way out before the dialog even appears. Building
only the first batch, and the rest only as the reader scrolls near it, is what keeps
the screen's open time flat regardless of the backlog.
"""
import time

import harness

# Comfortably past the largest deck this add-on ships today (nearly 3,000 cards), so this
# guards the property at a scale nothing currently shipped reaches.
_PENDING = 3000

# 250ms is generous over the ~2ms/card measured cost of building only the visible batch
# (StreamingList's default batch=50, so roughly 100ms of actual row-building work): wide
# enough to absorb CI being slower than a dev machine, while still failing hard if a
# future change makes the screen build every row up front again (3000 rows' worth would
# take several seconds, not milliseconds).
_BUDGET_SECONDS = 0.25


def _many_details(n):
    return [{"guid": f"g{i}", "notetype": "Study Deck - Basic",
             "kind": "new" if i % 2 else "changed",
             "fields": [("Front", f"Synthetic pending card {i}"),
                        ("Back", "answer"), ("Why", ""), ("Image", ""),
                        ("Tag", ""), ("Dosing", ""), ("Notes", "")]}
            for i in range(n)]


def test_update_screen_opens_fast_with_thousands_of_cards_pending():
    """Measures the build and the show, not just constructing the Python objects: the
    per-card cost StreamingList exists to defer is in building each row's real Qt
    widgets, which only happens inside _extend(), called from __init__ and from a
    show() that actually lays the dialog out.
    """
    import aqt.qt as aqt_qt
    from internpearls import review
    from internpearls.ui import _ask_with_widget
    from internpearls.widgets import StreamingList

    harness.bootstrap()
    app = harness.app()
    # Qt's own one-time font-family population cost (tens to hundreds of ms, logged to
    # stderr as "Populating font family aliases") lands on whichever widget happens to
    # be the first one built in the process. That is a real cost, but not this test's:
    # it is unrelated to how many cards are pending, and would otherwise make this
    # test's pass/fail depend on whether it happens to run before or after some other
    # scene in the same process. A throwaway label absorbs it here instead.
    aqt_qt.QLabel("warm").deleteLater()

    items = [("header", "Example Deck")]
    for i, detail in enumerate(_many_details(_PENDING)):
        if i:
            items.append(("sep",))
        items.append(("card", "Example Deck", detail))
    flags, new_index = {}, {}

    shown = []

    def fake_exec(self):
        self.resize(700, 620)
        self.show()
        app.processEvents()
        shown.append(self)
        return 1

    original = aqt_qt.QDialog.exec
    aqt_qt.QDialog.exec = fake_exec
    start = time.perf_counter()
    try:
        body, _boxes, flush = review.build_update_body(
            items, {}, flags, new_index, {},
            f"<b>{_PENDING}</b> pending cards", lambda: "", "safety note")
        _ask_with_widget(body, yes_label="Update")
    finally:
        aqt_qt.QDialog.exec = original
    elapsed = time.perf_counter() - start
    flush()

    assert shown, "the dialog never opened"
    assert elapsed < _BUDGET_SECONDS, (
        f"opening the update screen with {_PENDING} pending cards took "
        f"{elapsed:.3f}s, over the {_BUDGET_SECONDS}s budget. If this regressed, "
        "something is likely building every row up front again instead of only the "
        "first batch (see widgets.StreamingList).")

    lst = body.findChild(StreamingList)
    assert lst is not None, "expected a StreamingList in the update screen's body"
    assert lst.total() == len(items)   # every header/sep/card marker counts as one item
    assert lst.shown() < lst.total(), (
        "every row was built up front; the screen must only build its first batch "
        "regardless of how many cards are pending")


def test_filtering_a_huge_list_stays_lazy_and_quick():
    """A filter rebuilds the stream from the matching items, so it must build a first
    batch of them and no more, inside the same budget as opening the screen."""
    import aqt.qt as aqt_qt
    from internpearls import review
    from internpearls.widgets import FilterBar, StreamingList

    harness.bootstrap()
    aqt_qt.QLabel("warm").deleteLater()
    items = [("header", "Example Deck")]
    for i, detail in enumerate(_many_details(_PENDING)):
        items += ([("sep",)] if i else []) + [("card", "Example Deck", detail)]
    body, _boxes, flush = review.build_update_body(
        items, {}, {}, {}, {}, "", lambda: "", "safety note")
    lst, bar = body.findChild(StreamingList), body.findChild(FilterBar)
    start = time.perf_counter()
    bar.options.buttons["changed"].click()
    elapsed = time.perf_counter() - start
    flush()
    changed = _PENDING // 2
    assert lst.total() == 1 + changed + (changed - 1)   # heading, rows, hairlines
    assert 0 < lst.shown() < lst.total()
    assert elapsed < _BUDGET_SECONDS, (
        f"filtering {_PENDING} cards took {elapsed:.3f}s, over {_BUDGET_SECONDS}s")


def _long_update_body():
    import aqt.qt as aqt_qt
    from internpearls import review
    from internpearls.widgets import FilterBar, StreamingList

    harness.bootstrap()
    app = harness.app()
    aqt_qt.QLabel("warm").deleteLater()
    items = [("header", "Example Deck")]
    for i, detail in enumerate(_many_details(_PENDING)):
        detail["fields"][0] = ("Front", f"Synthetic pending card {i}, a front long enough "
                                        "to wrap onto a second line in the list")
        items += ([("sep",)] if i else []) + [("card", "Example Deck", detail)]
    body, _boxes, flush = review.build_update_body(
        items, {}, {}, {}, {}, "", lambda: "", "safety note")
    dlg = aqt_qt.QDialog()
    aqt_qt.QVBoxLayout(dlg).addWidget(body)
    dlg.resize(700, 620)
    dlg.show()
    app.processEvents()
    return app, dlg, body.findChild(StreamingList), body.findChild(FilterBar), flush


def test_scrolling_deep_into_a_long_list_costs_no_more_than_the_first_batch():
    """Each batch revealed while scrolling must cost about the same wherever it lands.
    Revealing rows inside the visible list used to re-lay-out every row already shown,
    so each batch took longer than the last and a long catch-up stalled."""
    app, dlg, lst, _bar, flush = _long_update_body()
    bar = lst.verticalScrollBar()
    steps = []
    for _ in range(30):
        start = time.perf_counter()
        bar.setValue(bar.maximum())
        app.processEvents()
        steps.append(time.perf_counter() - start)
    flush()
    dlg.close()
    assert lst.shown() > 1000
    assert max(steps[-5:]) < _BUDGET_SECONDS, (
        f"late scroll steps took {[round(s, 3) for s in steps[-5:]]}s, over "
        f"{_BUDGET_SECONDS}s (first steps {[round(s, 3) for s in steps[:3]]}s)")


def test_filtering_after_the_prefetch_has_built_everything_stays_quick():
    """A filter tears down every row built so far, and the idle prefetch keeps building
    while the dialog is open, so this is the case a learner reading a long list hits."""
    app, dlg, lst, bar, flush = _long_update_body()
    lst._build_upto(lst.total())
    app.processEvents()
    start = time.perf_counter()
    bar.options.buttons["new"].click()
    app.processEvents()
    elapsed = time.perf_counter() - start
    flush()
    dlg.close()
    assert elapsed < 1.0, (
        f"filtering after a full prefetch took {elapsed:.3f}s")
