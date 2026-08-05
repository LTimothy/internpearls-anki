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
    """Qt stretches a QLabel to a resizable scroll area's viewport and vertically centres
    its text, so a short confirmation floated with a screenful of blank above it.

    The label's own widget box is not a useful signal here: setWidgetResizable(True)
    stretches that box to fill the whole scroll viewport regardless of alignment (its
    heightForWidth is ~114px, but its measured box is a full 340px tall), so the box's
    top is always near the scroll's top either way. What actually moves with the fix is
    where the glyphs get painted inside that box, so this reads pixels: it walks down
    from the box's own top edge and finds the first row that isn't background colour.
    """
    _, q = harness.bootstrap()
    s = shot("confirm")
    body = max((w for w in s.dialog.findChildren(q.QLabel) if w.text()),
               key=lambda w: len(w.text()))
    rect = widget_rect(s.dialog, body)
    background = s.image.pixelColor(rect.left(), rect.top()).name()
    first_ink_row = next(
        (y for y in range(rect.top(), rect.bottom() + 1)
         if any(s.image.pixelColor(x, y).name() != background
               for x in range(rect.left(), rect.right() + 1))),
        rect.bottom() + 1)
    offset = first_ink_row - rect.top()
    assert offset < 40, (
        f"the confirmation text starts {offset}px into its {rect.height()}px-tall box; "
        "it should hug the top instead of floating near the middle")


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
