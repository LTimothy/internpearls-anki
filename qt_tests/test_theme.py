"""That the dark theme is actually dark.

The render tool shipped a --dark flag that did nothing for two versions:
styleHints().setColorScheme() is a no-op on Qt 6.9 under both the offscreen and cocoa
platforms. It looked like it worked because the machine it was built on runs macOS in
dark mode, so cocoa handed back a dark palette the flag had not asked for. On a
light-mode machine the same command rendered light and reported success.

Every dark assertion in this suite rests on this working, so it is checked directly
rather than trusted. Without this test, a broken dark theme means every dark test
silently re-runs the light one and the whole column reports green.
"""
import harness


def test_the_dark_palette_is_actually_dark():
    _, q = harness.bootstrap()
    harness.apply_theme("dark")
    window = harness.app().palette().color(q.QPalette.ColorRole.Window)
    assert window.name() == harness.THEMES["dark"]["Window"], (
        f"dark theme left the window at {window.name()}; the palette did not apply")


def test_light_and_dark_render_differently(shot):
    """The end-to-end version: not just that the palette object changed, but that the
    pixels did. This is the assertion the old --dark flag would have failed."""
    light = shot("review", theme="light")
    dark = shot("review", theme="dark")
    assert light.image != dark.image, (
        "the dark render is pixel-identical to the light one, so the theme is not "
        "reaching the widgets and every dark assertion here is testing light twice")
