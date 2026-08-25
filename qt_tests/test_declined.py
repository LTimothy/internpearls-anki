"""Real-Qt render checks for the Declined cards dialog: the three group headings
actually paint, and every row's Offer again button has real on-screen size, not just
a sizeHint the mock suite cannot measure.
"""
import pytest

import harness
from sampling import widget_rect

HEADINGS = ("Never imported", "Skipped for now", "Kept your version")


def _visible_labels(dialog, q):
    return [w for w in dialog.findChildren(q.QLabel) if w.isVisible()]


@pytest.mark.parametrize("theme", sorted(harness.THEMES))
def test_all_three_group_headings_render(shot, theme):
    _, q = harness.bootstrap()
    s = shot("declined", theme=theme)
    texts = {w.text() for w in _visible_labels(s.dialog, q)}
    missing = [h for h in HEADINGS if h not in texts]
    assert not missing, f"{theme}: missing group headings {missing}"


@pytest.mark.parametrize("theme", sorted(harness.THEMES))
def test_every_offer_again_button_has_real_size(shot, theme):
    _, q = harness.bootstrap()
    s = shot("declined", theme=theme)
    buttons = [b for b in s.dialog.findChildren(q.QPushButton)
              if b.text() == "Offer again" and b.isVisible()]
    assert len(buttons) == 3, (
        f"{theme}: expected three Offer again buttons (one per group), found "
        f"{len(buttons)}")
    zero = []
    for b in buttons:
        rect = widget_rect(s.dialog, b)
        if rect.width() <= 0 or rect.height() <= 0:
            zero.append(b.accessibleName())
    assert not zero, f"{theme}: Offer again rendered with zero size for {zero}"
