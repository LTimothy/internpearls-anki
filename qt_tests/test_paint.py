"""The specific rules v0.32.1 fixed, asserted as pixels rather than as spelling.

tests/test_review.py checks that the why rule's stylesheet resets `border` before
setting `border-left`, because that is what Qt needs to not silently drop it. That test
passes if someone writes a different rule that Qt also drops. This one looks for the
green.

Counts are never asserted, only presence: ubuntu and macOS disagree about font metrics,
so "some of this colour is inside this widget" ports and "102 pixels of it" does not.
"""
import harness
from sampling import colour_counts, widget_rect


def test_the_why_rule_actually_paints_green(shot):
    """The v0.32.1 bug: a lone `border-left` on a QLabel is dropped unless the `border`
    shorthand is reset first, so this rule shipped invisible. The indent it did apply
    made the missing rule look intentional.

    Only the label's leftmost 3 columns are sampled, because the same stylesheet sets
    `color` to the same green: a whole-image search would find the text and pass even
    with the border dropped, which is the bug this is for.
    """
    _, q = harness.bootstrap()
    from internpearls.review import _WHY_RULE
    s = shot("review", expand=(0,))
    why = next(w for w in s.dialog.findChildren(q.QLabel)
               if "common case" in w.text())
    rect = widget_rect(s.dialog, why)
    edge = q.QRect(rect.left(), rect.top(), 3, rect.height())
    assert colour_counts(s.image, edge).get(_WHY_RULE, 0) > 0, (
        f"the why rule's green {_WHY_RULE} is not painting down the label's left edge. "
        "Qt has dropped the border-left again: it needs `border: none` before it.")


def test_the_dosing_block_paints_its_own_background(shot):
    """Row 1 is the only fixture row with a Dosing field."""
    from internpearls.review import _DOSING_BG, _DOSING_FG
    s = shot("review", expand=(1,))
    painted = colour_counts(s.image)
    assert painted.get(_DOSING_BG, 0) > 0, f"the dosing background {_DOSING_BG} is absent"
    assert painted.get(_DOSING_FG, 0) > 0, (
        f"the dosing foreground {_DOSING_FG} is absent: the text is taking its colour "
        "from the palette again, which is what turned it white on near-white in Night "
        "Mode")


def test_the_dosing_block_stays_readable_on_a_dark_palette(shot):
    """The regression that mattered: a hardcoded background needs a hardcoded
    foreground, or the theme flips one and not the other."""
    from internpearls.review import _DOSING_BG, _DOSING_FG
    s = shot("review", theme="dark", expand=(1,))
    painted = colour_counts(s.image)
    assert painted.get(_DOSING_BG, 0) > 0 and painted.get(_DOSING_FG, 0) > 0, (
        "the dosing block loses its own colours on a dark palette")


def test_a_cloze_deletion_paints_in_the_decks_blue(shot):
    from internpearls.review import _CLOZE_COLOR
    s = shot("review")
    assert colour_counts(s.image).get(_CLOZE_COLOR, 0) > 0, (
        f"no cloze blue {_CLOZE_COLOR}: the <style> block is being dropped, so "
        "deletions are rendering as plain text")


def test_only_one_hairline_is_drawn_between_two_rows(shot):
    """The other v0.32.1 bug: the row set a selector-less stylesheet, which propagates
    into child widgets, so the row and its header each drew a rule and the separator
    came out doubled. Two runs of the rule colour at clearly different widths is the
    fingerprint (the row's own width, and the narrower header's); one width means one
    line.
    """
    from internpearls.review import _ROW_RULE
    s = shot("review")
    widths = set()
    for y in range(s.image.height()):
        run = 0
        for x in range(s.image.width()):
            if s.image.pixelColor(x, y).name() == _ROW_RULE:
                run += 1
            elif run:
                widths.add(run)
                run = 0
        if run:
            widths.add(run)
    significant = {w for w in widths if w > 100}
    assert len(significant) <= 1, (
        f"the rule colour {_ROW_RULE} paints runs of {sorted(significant)}px. More "
        "than one width means more than one line: a selector-less stylesheet is "
        "leaking into child widgets again.")
