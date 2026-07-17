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
