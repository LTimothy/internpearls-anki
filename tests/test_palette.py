"""The colour sets, checked as numbers rather than by eye.

qt_tests/test_contrast.py measures what actually paints, which is the ground truth, but it
can only see one dominant colour pair per widget: a small inline span like a row marker is
never the pair it measures. This file is what stands guard over the values themselves, and
it needs no Qt at all.
"""
from internpearls import palette

AA = 4.5
WINDOW = {"light": "#efefef", "dark": "#2f2f31"}
BASE = {"light": "#ffffff", "dark": "#2f2f31"}
# Roles drawn as text straight onto the dialog, so they answer to the window colour.
ON_WINDOW = ("why", "accent", "dim", "muted", "warning")
# Roles that carry their own background, so they answer to it instead.
PAIRS = (("dosing_fg", "dosing_bg"), ("new_fg", "new_bg"), ("updated_fg", "updated_bg"))


def _luminance(value):
    value = value.lstrip("#")
    parts = [int(value[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    parts = [p / 12.92 if p <= 0.03928 else ((p + 0.055) / 1.055) ** 2.4 for p in parts]
    return 0.2126 * parts[0] + 0.7152 * parts[1] + 0.0722 * parts[2]


def contrast(a, b):
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _sets():
    return {"light": palette.LIGHT, "dark": palette.DARK}


def test_every_text_role_clears_aa_against_its_own_window():
    failures = []
    for theme, colors in _sets().items():
        for role in ON_WINDOW:
            for surface in (WINDOW[theme], BASE[theme]):
                got = contrast(colors[role], surface)
                if got < AA:
                    failures.append(f"{theme}/{role} on {surface}: {got:.2f}:1")
    assert not failures, "below AA: " + "; ".join(failures)


def test_every_paired_role_clears_aa_inside_its_own_pair():
    failures = []
    for theme, colors in _sets().items():
        for fg, bg in PAIRS:
            got = contrast(colors[fg], colors[bg])
            if got < AA:
                failures.append(f"{theme}/{fg} on {bg}: {got:.2f}:1")
    assert not failures, "below AA: " + "; ".join(failures)


def test_marker_chips_stand_off_their_window_enough_to_read_as_chips():
    """Contrast inside the chip was never the problem: separation from the window was.
    At 1.02:1 a chip background is invisible and the marker reads as tinted text."""
    for theme, colors in _sets().items():
        for bg in ("new_bg", "updated_bg"):
            got = contrast(colors[bg], WINDOW[theme])
            assert got >= 1.4, f"{theme}/{bg} only {got:.2f}:1 off its window"


def test_both_sets_define_exactly_the_same_roles():
    assert set(palette.LIGHT) == set(palette.DARK)


def test_colors_follows_ankis_theme(monkeypatch):
    monkeypatch.setattr(palette, "is_dark", lambda: True)
    assert palette.colors() == palette.DARK
    monkeypatch.setattr(palette, "is_dark", lambda: False)
    assert palette.colors() == palette.LIGHT
