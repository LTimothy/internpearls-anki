"""Every visible label's text has to be readable against what is actually behind it.

This is the general form of the v0.32.1 Night Mode bug, where the dosing block set its
own light background but left its text colour to the palette, so the text went white on
near-white. tests/test_review.py guards that case with a string lint
(test_no_widget_sets_a_background_without_setting_a_foreground), which asserts we spelled
the rule the way we learned to spell it. This asserts the outcome the rule is for, and
does not care how it breaks.

The lint stays. It runs with no PyQt6 and catches a bad rule before anything renders.
This is the ground truth behind it, not a replacement for it.
"""
import pytest

import harness
from sampling import text_contrast

ALL_SCENES = sorted(harness.SCENES)
AA = 4.5

# Render every scene with its rows opened. The review list starts collapsed
# (body.setVisible(False)), so its dosing and why blocks paint nothing until expanded,
# and collapsed content is one click away rather than gone, so it must meet AA too.
# render() clamps expand to the carets a scene actually has, so this is a no-op for the
# scenes without any.
_EXPAND_ALL = tuple(range(8))


def _text_widgets(dialog, q):
    """The widgets whose text this suite measures for contrast: every QLabel, plus flat
    QPushButtons. Flat buttons carry the add-on's own foreground colours (the caret is
    the dim colour, the link buttons are the accent) over the window, so they are
    measurable. Native buttons are excluded: their offscreen chrome is not the add-on's
    colour and reads as a false failure."""
    labels = list(dialog.findChildren(q.QLabel))
    buttons = [b for b in dialog.findChildren(q.QPushButton) if b.isFlat()]
    return labels + buttons

# Colours that fail AA today, every ratio measured 2026-07-16. These are real
# legibility bugs, not false positives, and every one is deliberately out of scope
# here: they are hardcoded foregrounds over a palette background, so fixing them is an
# add-on-wide colour change with its own design rather than a line in a test file.
# The pattern is worth seeing whole: the one hardcoded colour that is NOT here is
# _DOSING_FG, and it is the only one paired with a hardcoded background. README's
# "Colors" rule works. It is just not applied anywhere else yet.
#
# This is a debt ledger, not an exemption list. It may only ever shrink. Adding to it
# means shipping a legibility bug, which is a decision for the colour-system design,
# not a way to make this file green.
KNOWN_LOW_CONTRAST = {
    "#808080": "ui.py muted_label/hint_label: 3.43:1 light, 3.38:1 dark",
    "#8a9aa2": "review.py _DIM, the tag lead-in and caret: 2.53:1 light",
    "#2563eb": "ui.py ACCENT / review.py _CLOZE_COLOR: 4.49:1 light, 2.58:1 dark",
    "#2e6b3e": "review.py _WHY_RULE: 2.09:1 dark",
}


@pytest.mark.parametrize("theme", sorted(harness.THEMES))
@pytest.mark.parametrize("scene", ALL_SCENES)
def test_every_visible_label_clears_wcag_aa(shot, scene, theme):
    _, q = harness.bootstrap()
    s = shot(scene, theme=theme, expand=_EXPAND_ALL)
    failures = []
    for widget in _text_widgets(s.dialog, q):
        measured = text_contrast(s, widget)
        if measured is None:
            continue
        ratio, foreground, background = measured
        if ratio >= AA or foreground.name() in KNOWN_LOW_CONTRAST:
            continue
        failures.append(
            f"{ratio:.2f}:1  fg={foreground.name()} on bg={background.name()}  "
            f"{widget.text()[:60]!r}")
    assert not failures, (
        f"{scene}/{theme}: text below {AA}:1 against its actual background.\n  "
        + "\n  ".join(failures)
        + "\n\nIf this is a new colour, it needs to clear AA on both themes. If it is "
          "one of the known ones, it belongs in KNOWN_LOW_CONTRAST with its measured "
          "ratio, and that list may only shrink.")


def test_the_allowlist_still_describes_real_failures():
    """Stops the ledger from outliving the debt.

    An allowlist nobody prunes turns into a permanent exemption. If a colour in it
    starts passing everywhere (because it got fixed, or stopped being used), this fails
    and the entry has to come out, which is the only way the list ever shrinks.
    """
    _, q = harness.bootstrap()
    still_failing = set()
    for scene in ALL_SCENES:
        for theme in harness.THEMES:
            s = harness.render(scene, theme=theme, expand=_EXPAND_ALL)
            for widget in _text_widgets(s.dialog, q):
                measured = text_contrast(s, widget)
                if measured and measured[0] < AA:
                    still_failing.add(measured[1].name())
    stale = set(KNOWN_LOW_CONTRAST) - still_failing
    assert not stale, (
        f"KNOWN_LOW_CONTRAST lists {sorted(stale)}, which no longer fails anywhere. "
        "Remove the entry: the ledger is meant to shrink.")
