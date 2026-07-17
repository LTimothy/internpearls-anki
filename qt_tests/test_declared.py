"""If a widget declares a colour, that colour had better paint somewhere in its rect.

Qt drops a stylesheet declaration it dislikes without raising: no exception, no warning,
just absence. This walks every visible widget and asserts each hex colour in its own
stylesheet appears somewhere inside its rect. It is the only generic check in the suite
(all widgets, all scenes), and it catches the fully-dropped case: a colour that paints
nowhere at all, from a mistyped property or a rule Qt discards where that colour is not
reused elsewhere.

What it does NOT catch, proved by mutation on 2026-07-17, so do not mistake it for a
catch-all:
  - a colour dropped from one property but still painted by another that reuses it. The
    v0.32.1 why rule declared the same green for its border-left AND its text, so
    dropping the border still left the green text, and this check passed. The dedicated
    left-edge sample in test_paint.py is what catches that.
  - a doubled rule. The v0.32.1 twin hairline was extra presence, not absence.
  - a missing declaration. The v0.32.2 dosing bug let the foreground fall to the
    palette; there was no declared colour to check. test_contrast.py catches that.
So it catches none of the three v0.32.1/v0.32.2 bugs; those are covered, and
mutation-proved, by test_paint.py and test_contrast.py. This test earns its place on
the fully-dropped case they do not generalize to.

It is also the one that can cry wolf, so it is deliberately narrow: only colours a
visible widget declares in its own stylesheet, only inside that widget's own rect, and
with an explicit ignore list for the states a render never enters.
"""
import re

import pytest

import harness
from sampling import colour_counts, widget_rect

ALL_SCENES = sorted(harness.SCENES)

_HEX = re.compile(r"#[0-9a-fA-F]{6}")

# Stylesheet states a plain render never enters. A hover colour is absent because
# nothing is hovering, which is the point rather than a bug.
IGNORE_STATES = ("hover", "pressed", "disabled", "checked", "focus")


def _declared_colours(widget):
    """Hex colours in a widget's own stylesheet, minus any declared for a state this
    render never enters."""
    sheet = widget.styleSheet() or ""
    if any(state in sheet for state in IGNORE_STATES):
        return set()
    return {m.group(0).lower() for m in _HEX.finditer(sheet)}


@pytest.mark.parametrize("theme", sorted(harness.THEMES))
@pytest.mark.parametrize("scene", ALL_SCENES)
def test_every_declared_colour_actually_paints(shot, scene, theme):
    _, q = harness.bootstrap()
    s = shot(scene, theme=theme)
    missing = []
    for widget in s.dialog.findChildren(q.QWidget):
        if not widget.isVisible():
            continue
        declared = _declared_colours(widget)
        if not declared:
            continue
        rect = widget_rect(s.dialog, widget)
        if rect.width() < 1 or rect.height() < 1:
            continue
        painted = colour_counts(s.image, rect)
        for colour in declared:
            if painted.get(colour, 0) == 0:
                missing.append(
                    f"{type(widget).__name__} declares {colour} and paints none of it: "
                    f"{(widget.styleSheet() or '')[:70]!r}")
    assert not missing, (
        f"{scene}/{theme}: Qt dropped these declarations silently.\n  "
        + "\n  ".join(missing)
        + "\n\nA colour that reads correctly and never paints is the failure this "
          "suite exists for. Check the rule against README's \"Colors\" section: a "
          "lone border-left needs a `border` reset before it.")
