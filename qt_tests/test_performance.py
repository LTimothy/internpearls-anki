"""Guards the property the Update my decks screen's whole design rests on: opening it
must not depend on how many cards are pending.

widgets.StreamingList's own docstring has the measured cost this bounds: about 2ms per
card to build and show a row, offscreen. Building every row for a large first sync
(2903 cards, the largest deck this add-on ships today) up front would freeze the UI for
5.7 seconds with no feedback and no way out before the dialog even appears. Building
only the first batch, and the rest only as the reader scrolls near it, is what keeps
the screen's open time flat regardless of the backlog.
"""
import time

import harness

# Comfortably past the largest deck this add-on ships today (2903 cards), so this
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
            items, {}, flags, new_index, False,
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
