"""Clicks, and what really happens to the widgets after one.

tests/test_dialogs.py covers some of this against the mock, which knows what a click
calls but not what it shows: the mock's widgets have no visibility, no geometry, and no
paint. These assert the visible result.
"""
import harness


def _carets(dialog, q):
    return [b for b in dialog.findChildren(q.QPushButton)
            if b.text() in (harness.CARET_CLOSED, harness.CARET_OPEN)]


def test_rows_start_collapsed(shot):
    _, q = harness.bootstrap()
    s = shot("review")
    carets = _carets(s.dialog, q)
    assert carets, "the review list has no carets at all"
    assert all(c.text() == harness.CARET_CLOSED for c in carets), (
        "a row is already open on arrival; the list is meant to open collapsed so it "
        "reads as a scannable list rather than a stack of note dumps")


def test_clicking_a_caret_reveals_that_rows_answer(shot):
    """The answer text must go from absent to present, not merely from one widget state
    to another: a row that "expands" without painting anything is the bug shape here."""
    _, q = harness.bootstrap()
    closed = shot("review")
    opened = shot("review", expand=(0,))

    def answer_visible(s):
        return any("basic note with a tag" in w.text() and w.isVisible()
                   for w in s.dialog.findChildren(q.QLabel))

    assert not answer_visible(closed), "row 0's answer is showing before it is expanded"
    assert answer_visible(opened), "clicking the caret did not reveal row 0's answer"
    assert _carets(opened.dialog, q)[0].text() == harness.CARET_OPEN, (
        "the caret did not turn to face down after opening")


def test_expanding_one_row_leaves_its_neighbours_closed(shot):
    _, q = harness.bootstrap()
    s = shot("review", expand=(0,))
    carets = _carets(s.dialog, q)
    assert carets[0].text() == harness.CARET_OPEN
    assert all(c.text() == harness.CARET_CLOSED for c in carets[1:]), (
        "opening one row opened others too")


def test_the_caret_does_not_eat_a_gutter(shot):
    """The v0.32.1 layout bug: the caret was an unconstrained QPushButton sitting at its
    platform minimum width, which left a wide empty column down the whole list. The
    exact width is a platform detail; that it is not enormous is not.
    """
    _, q = harness.bootstrap()
    s = shot("review")
    for caret in _carets(s.dialog, q):
        assert caret.width() <= 24, (
            f"a caret is {caret.width()}px wide. It is unconstrained again and is "
            "pushing every row's text right.")


def test_feedback_boxes_appear_only_when_the_setting_is_on(shot):
    """The Settings toggle: off means the review is a read-only preview with nothing to
    send afterward."""
    _, q = harness.bootstrap()
    off = shot("review", expand=(0,), feedback=False)
    on = shot("review", expand=(0,), feedback=True)

    def boxes(s):
        return [w for w in s.dialog.findChildren(q.QPlainTextEdit) if w.isVisible()]

    assert not boxes(off), "note boxes are showing with card feedback turned off"
    assert boxes(on), "note boxes are missing with card feedback turned on"
