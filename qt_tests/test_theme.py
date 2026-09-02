"""That the dark theme is actually dark.

The render tool shipped a --dark flag that did nothing for two versions:
styleHints().setColorScheme() is a no-op on Qt 6.9 under both the offscreen and cocoa
platforms. It looked like it worked because the machine it was built on runs macOS in
dark mode, so cocoa handed back a dark palette the flag had not asked for. On a
light-mode machine the same command rendered light and reported success.

Every dark assertion in this suite rests on this working, so it is checked directly
rather than trusted. Without this test, a broken dark theme means every dark test
silently re-runs the light one and the whole column reports green.

test_light_and_dark_render_differently used to check exactly one hardcoded scene
("confirm") rather than harness.SCENES itself, so nothing here ever asserted the AI
wizard (or any of the other fifteen scenes) actually looked different under the two
themes -- a scene could paint pixel-identical light and dark renders forever and this
suite would stay green. Enumerated the same way test_contrast.py and test_layout.py
already do, so a new scene is covered the moment it's registered.
"""
import pytest

import harness

ALL_SCENES = sorted(harness.SCENES)

# Scenes that legitimately can't clear "the two themes render different pixels", with
# the reason each is here. Not a dumping ground: an entry here is a deliberate,
# investigated exception, not a way to quiet a failure. See harness.SCENES for what
# each scene actually renders.
KNOWN_THEME_INVARIANT = {}


@pytest.mark.parametrize("scene", ALL_SCENES)
def test_light_and_dark_render_differently(shot, scene):
    """The end-to-end version: not just that the palette object changed, but that the
    pixels did. This is the assertion the old --dark flag would have failed."""
    if scene in KNOWN_THEME_INVARIANT:
        pytest.skip(KNOWN_THEME_INVARIANT[scene])
    light = shot(scene, theme="light")
    dark = shot(scene, theme="dark")
    assert light.image != dark.image, (
        f"{scene}: the dark render is pixel-identical to the light one, so the theme "
        "is not reaching the widgets and every dark assertion here is testing light "
        "twice")


def test_the_dark_palette_is_actually_dark():
    _, q = harness.bootstrap()
    harness.apply_theme("dark")
    window = harness.app().palette().color(q.QPalette.ColorRole.Window)
    assert window.name() == harness.THEMES["dark"]["Window"], (
        f"dark theme left the window at {window.name()}; the palette did not apply")
