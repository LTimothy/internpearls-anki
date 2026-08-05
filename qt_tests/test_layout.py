"""Real font metrics, which the mock suite has none of.

A label that fits in the mock always fits, because the mock has no font, no wrapping,
and no width. These are the questions only real Qt can answer: does the text fit, does
the dialog fit, do the rows line up.
"""
import pytest

import harness
from sampling import widget_rect

ALL_SCENES = sorted(harness.SCENES)


def _visible_labels(dialog, q):
    return [w for w in dialog.findChildren(q.QLabel)
            if w.isVisible() and w.text().strip()]


@pytest.mark.parametrize("theme", sorted(harness.THEMES))
@pytest.mark.parametrize("scene", ALL_SCENES)
def test_no_label_is_clipped(shot, scene, theme):
    """A label whose content needs more room than it has drops the overflow silently,
    the same way Qt drops a stylesheet rule it dislikes."""
    _, q = harness.bootstrap()
    s = shot(scene, theme=theme)
    clipped = []
    for label in _visible_labels(s.dialog, q):
        # Height only. A word-wrapped label's sizeHint width is its unwrapped width,
        # which legitimately exceeds the widget. Height is what tells the truth once
        # the wrap has happened.
        needed = (label.heightForWidth(label.width()) if label.wordWrap()
                  else label.sizeHint().height())
        if needed > label.height() + 1:
            clipped.append(f"{label.text()[:50]!r} needs {needed}px, "
                           f"has {label.height()}px")
    assert not clipped, f"{scene}/{theme}: clipped labels:\n  " + "\n  ".join(clipped)


@pytest.mark.parametrize("scene", ALL_SCENES)
def test_nothing_overflows_the_dialog_horizontally(shot, scene):
    """The caret bug's signature: a widget wider than the dialog that holds it."""
    _, q = harness.bootstrap()
    s = shot(scene)
    overflowing = []
    for widget in s.dialog.findChildren(q.QWidget):
        if not widget.isVisible():
            continue
        rect = q.QRect(widget.mapTo(s.dialog, q.QPoint(0, 0)), widget.size())
        if rect.right() > s.dialog.width():
            overflowing.append(
                f"{type(widget).__name__} right edge {rect.right()} > dialog "
                f"{s.dialog.width()}")
    assert not overflowing, (
        f"{scene}: widgets overflow the dialog:\n  " + "\n  ".join(overflowing))


def test_a_short_confirmation_starts_at_the_top_not_the_middle(shot):
    """Two ways a short confirmation ends up with blank space above its first line, both
    guarded here in one measurement:

    1. The label's text not being top-aligned within its own box (the original bug:
       setWidgetResizable(True) stretches the label's box to fill the scroll viewport,
       and Qt vertically centres a label's text within its own box by default).
    2. The scroll area itself floating away from the dialog's own top margin, which
       stayed possible even after fix 1: with nothing below the scroll area claiming
       the dialog's leftover height, Qt spread that surplus above, inside, and below
       the scroll area instead of collecting it below the content, so a short
       confirmation still opened with real blank space above the first line, just less
       of it (verified: fixing only #1 left this at 89px in a 620px-tall dialog).

    A fix to #1 alone cannot be told apart from a regression in #2 by measuring pixels
    only inside the label's own box, since that box's top moves with #2 too. So this
    reads pixels from the DIALOG's own top edge, not the label's: it walks down from
    row 0 and finds the first row, anywhere in the label's rect, that isn't background
    colour. That is the actual blank gap a person looking at the dialog sees.
    """
    _, q = harness.bootstrap()
    s = shot("confirm")
    body = max((w for w in s.dialog.findChildren(q.QLabel) if w.text()),
               key=lambda w: len(w.text()))
    rect = widget_rect(s.dialog, body)
    background = s.image.pixelColor(rect.left(), rect.top()).name()
    first_ink_row = next(
        (y for y in range(0, rect.bottom() + 1)
         if any(s.image.pixelColor(x, y).name() != background
               for x in range(rect.left(), rect.right() + 1))),
        rect.bottom() + 1)
    assert first_ink_row < 40, (
        f"the confirmation text starts painting at row {first_ink_row} of the dialog; "
        "it should hug the dialog's own top margin, not float partway down")


def test_review_rows_share_a_left_edge(shot):
    """Tagged and untagged rows must start at the same x.

    Before v0.32.1 the tag sat in its own widget beside the text, so a tagged row's
    text started about 150px right of an untagged one. The fixture has both: rows 0 and
    1 are tagged, row 2 is not.
    """
    _, q = harness.bootstrap()
    s = shot("review")
    lefts = {}
    for label in _visible_labels(s.dialog, q):
        for marker, row in (("one short line", 0), ("deliberately long", 1),
                            ("untagged row", 2)):
            if marker in label.text():
                lefts[row] = widget_rect(s.dialog, label).left()
    assert len(lefts) == 3, f"expected all three primary rows, found {sorted(lefts)}"
    assert len(set(lefts.values())) == 1, (
        f"rows start at different x: {lefts}. A tagged row's text must begin where an "
        "untagged row's does.")
