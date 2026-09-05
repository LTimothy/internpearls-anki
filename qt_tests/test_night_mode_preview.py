"""The Night mode dimming preview: a live example card, normal and dimmed side by
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


def _pane_tops(shot):
    normal, dimmed = _pane_rects(shot)
    return normal.top(), dimmed.top()


def test_the_preview_holds_still_when_the_scope_changes():
    """The scope hint under the radios is one line for Bright images only and three
    for Everything on cards and deck screens, so sizing it to whichever is showing
    pushed the Dim by row and the whole preview down the moment the reader picked
    the second radio.

    Measured at the width the dialog actually opens narrow to (its own 420px
    minimum, where the longer hint really does need three lines) and at the height
    its content asks for, since a window with room to spare hands the slack to the
    hint and hides the jump. Asserted both ways round, because either alone can pass
    for the wrong reason: the two scopes rendered as their own dialogs must place the
    panes identically, and so must switching the scope inside one open dialog, which
    is the jump the reader actually sees. Rendered directly rather than through the
    cached shot fixture, since this clicks a radio and the dialog it clicks must be
    its own.
    """
    harness.bootstrap()
    rendered = {}
    for scope in ("images", "content"):
        s = harness.render("night-mode-dimming", percent=50, scope=scope, size=(420, 300))
        s.dialog.resize(420, s.dialog.sizeHint().height())
        harness.app().processEvents()
        rendered[scope] = s
    assert _pane_tops(rendered["images"]) == _pane_tops(rendered["content"]), (
        f"the preview sits at {_pane_tops(rendered['images'])} under Bright images "
        f"only and {_pane_tops(rendered['content'])} under Everything on cards and "
        "deck screens")

    live = rendered["images"]
    before = _pane_tops(live)
    live.dialog._scope_content.setChecked(True)
    harness.app().processEvents()
    after = _pane_tops(live)
    assert before == after, (
        f"the preview moved from {before} to {after} when the scope changed")


def test_content_scope_hint_names_deck_screens(shot):
    harness.bootstrap()
    s = shot("night-mode-dimming", scope="content")
    assert "deck screens" in s.dialog._scope_hint.text()
