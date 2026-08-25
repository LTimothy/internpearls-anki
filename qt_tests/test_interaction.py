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
    s = shot("confirm")
    carets = _carets(s.dialog, q)
    assert carets, "the review list has no carets at all"
    assert all(c.text() == harness.CARET_CLOSED for c in carets), (
        "a row is already open on arrival; the list is meant to open collapsed so it "
        "reads as a scannable list rather than a stack of note dumps")


def test_clicking_a_caret_reveals_that_rows_answer(shot):
    """The answer text must go from absent to present, not merely from one widget state
    to another: a row that "expands" without painting anything is the bug shape here."""
    _, q = harness.bootstrap()
    closed = shot("confirm")
    opened = shot("confirm", expand=(0,))

    def answer_visible(s):
        return any("basic note with a tag" in w.text() and w.isVisible()
                   for w in s.dialog.findChildren(q.QLabel))

    assert not answer_visible(closed), "row 0's answer is showing before it is expanded"
    assert answer_visible(opened), "clicking the caret did not reveal row 0's answer"
    assert _carets(opened.dialog, q)[0].text() == harness.CARET_OPEN, (
        "the caret did not turn to face down after opening")


def test_expanding_one_row_leaves_its_neighbours_closed(shot):
    _, q = harness.bootstrap()
    s = shot("confirm", expand=(0,))
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
    s = shot("confirm")
    for caret in _carets(s.dialog, q):
        assert caret.width() <= 24, (
            f"a caret is {caret.width()}px wide. It is unconstrained again and is "
            "pushing every row's text right.")


def test_feedback_boxes_appear_only_when_the_setting_is_on(shot):
    """The Settings toggle: off means the review is a read-only preview with nothing to
    send afterward."""
    _, q = harness.bootstrap()
    off = shot("confirm", expand=(0,), feedback=False)
    on = shot("confirm", expand=(0,), feedback=True)

    def boxes(s):
        return [w for w in s.dialog.findChildren(q.QPlainTextEdit) if w.isVisible()]

    assert not boxes(off), "note boxes are showing with card feedback turned off"
    assert boxes(on), "note boxes are missing with card feedback turned on"


def test_the_update_screens_cleanup_runs_while_its_widgets_are_still_alive():
    """build_update_body hands back a `flush` that stops the debounce timer, writes the
    final flag state, and releases the extracted pictures. Everything it touches (that
    timer, and the feedback boxes pending_entries reads) is parented into the body, so
    Qt destroys all of it with the dialog the body was handed to.

    Running flush after that wrapper returns therefore reaches freed C++ objects and
    raises "wrapped C/C++ object of type QTimer has been deleted", which surfaced as the
    add-on's generic error box on every single Update my decks run. The mock suite
    cannot see this: its widgets are Python objects with no C++ lifetime behind them, so
    a destroyed tree still answers happily there.

    The wrapper runs it instead, before it drops the dialog.
    """
    _, q = harness.bootstrap()
    harness.app()
    from internpearls import review
    from internpearls.ui import _ask_with_widget

    body, _boxes, flush = review.build_update_body(
        [("header", "Example Deck")], {}, {}, {}, False, "", lambda: "", "safety")

    ran = []
    original = q.QDialog.exec
    q.QDialog.exec = lambda self: 1
    try:
        _ask_with_widget(body, yes_label="Update",
                         on_close=lambda: (flush(), ran.append(True)))
    finally:
        q.QDialog.exec = original

    assert ran, "the wrapper never ran the caller's cleanup"


def _capture_ask(q, **kw):
    """Run ui._ask's named-button path against real Qt, without blocking on exec().

    Returns (answer, box). The patched exec clicks nothing, which is what a dismissed
    dialog leaves behind: clickedButton() is None, so the answer must not read as yes.
    """
    from internpearls import ui
    boxes = []

    def fake_exec(self):
        boxes.append(self)
        return 0

    original = q.QMessageBox.exec
    q.QMessageBox.exec = fake_exec
    try:
        answer = ui._ask("This update also changes how some cards look.", **kw)
    finally:
        q.QMessageBox.exec = original
    return answer, boxes[0]


def test_a_named_question_cannot_be_agreed_to_by_pressing_return():
    """Anki's askUserDialog gives every button AcceptRole and sets no default, so Escape
    and the close box did nothing at all and Return activated whichever button was added
    first, which on these questions is the one that consents to a schema change. The
    declining button carries RejectRole and is both the default and the escape button
    now, so Return, Escape and the close box all mean the safe answer."""
    _, q = harness.bootstrap()
    answer, box = _capture_ask(q, yes_label="Apply the new look",
                               no_label="Keep my current look")

    labels = {b.text().replace("&", ""): b for b in box.buttons()}
    assert set(labels) == {"Apply the new look", "Keep my current look"}
    safe = labels["Keep my current look"]
    assert box.buttonRole(safe) == q.QMessageBox.ButtonRole.RejectRole
    assert box.buttonRole(labels["Apply the new look"]) == (
        q.QMessageBox.ButtonRole.AcceptRole)
    assert box.defaultButton() is safe, "Return would consent to a schema change"
    assert box.escapeButton() is safe, "Escape and the close box answer nothing"
    assert answer is False, "a dialog dismissed without a click read as a yes"
