"""The Night Mode Dimming preview: a live example card, normal and dimmed side by
side, so the percent spinner's number is not abstract.

The per-pixel transform itself (night_mode_dim_factor) is unit-tested in
tests/test_logic.py, with no Qt involved. This only proves the dialog actually renders
it: the dimmed pane paints measurably darker than the normal one, and a different
percent paints a different amount of dark. Both assertions read real pixels off the
rendered widget, never a stored percent or factor.
"""
import harness
from internpearls import dialogs
from sampling import luminance, widget_rect


def _pane_rects(shot):
    """The normal and dimmed sample panes' own rects within the rendered dialog."""
    panes = shot.dialog.findChildren(dialogs._NightModeSamplePane)
    normal = next(p for p in panes if not p._dimmed)
    dimmed = next(p for p in panes if p._dimmed)
    return widget_rect(shot.dialog, normal), widget_rect(shot.dialog, dimmed)


def _avg_luminance(image, rect):
    total = 0.0
    count = 0
    for y in range(rect.top(), rect.bottom() + 1):
        for x in range(rect.left(), rect.right() + 1):
            total += luminance(image.pixelColor(x, y))
            count += 1
    assert count, "empty preview pane rect: the pane did not paint at its expected size"
    return total / count


def test_the_dimmed_pane_paints_measurably_darker_than_the_normal_pane(shot):
    harness.bootstrap()
    s = shot("night-mode-dimming", percent=50)
    normal_rect, dimmed_rect = _pane_rects(s)
    normal_avg = _avg_luminance(s.image, normal_rect)
    dimmed_avg = _avg_luminance(s.image, dimmed_rect)
    assert dimmed_avg < normal_avg, (
        f"the dimmed pane ({dimmed_avg:.3f} avg luminance) is not darker than the "
        f"normal pane ({normal_avg:.3f}) at 50% dim")


def test_changing_the_percent_changes_the_dimmed_pane(shot):
    harness.bootstrap()
    low = shot("night-mode-dimming", percent=10)
    high = shot("night-mode-dimming", percent=80)
    _, low_dimmed_rect = _pane_rects(low)
    _, high_dimmed_rect = _pane_rects(high)
    low_avg = _avg_luminance(low.image, low_dimmed_rect)
    high_avg = _avg_luminance(high.image, high_dimmed_rect)
    assert high_avg < low_avg, (
        f"80% dim ({high_avg:.3f} avg luminance) is not darker than 10% dim "
        f"({low_avg:.3f}): the preview is not tracking the percent")
