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
# updated_fg is here as well as in PAIRS below: it is paired with updated_bg for the
# review row's marker chip, but dialogs.py also paints it bare on the window for the
# About dialog's "update available" pill, so it needs both checks. The two loops below
# are independent, so listing a role in both is not redundant.
ON_WINDOW = ("why", "accent", "dim", "muted", "warning", "updated_fg")
# Roles that carry their own background, so they answer to it instead.
PAIRS = (("dosing_fg", "dosing_bg"), ("new_fg", "new_bg"), ("updated_fg", "updated_bg"),
        ("retired_fg", "retired_bg"), ("moved_fg", "moved_bg"))
# Roles that are not text at all (a rule/divider colour), so the AA checks above don't
# apply to them. They still answer to the window they're drawn on, at the separation
# threshold in the rules test below rather than at AA. Named explicitly rather than left
# implicit, so the completeness test below stays an honest check of every role in
# palette.py rather than a silent pass on anything absent from ON_WINDOW and PAIRS.
NON_TEXT_ROLES = ("row_rule", "cell_rule", "panel_rule")
# The separation a rule needs from its window to be a line rather than a suggestion of
# one. Below this the list it divides reads as one undifferentiated block.
RULE_SEPARATION = 1.25


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
        for bg in ("new_bg", "updated_bg", "retired_bg", "moved_bg"):
            got = contrast(colors[bg], WINDOW[theme])
            assert got >= 1.4, f"{theme}/{bg} only {got:.2f}:1 off its window"


def test_every_rule_stands_off_its_own_window():
    """A divider is measured against the window the same way a chip background is: its
    job is separation, and a rule that doesn't clear the window is a rule nobody sees."""
    for theme, colors in _sets().items():
        for role in NON_TEXT_ROLES:
            got = contrast(colors[role], WINDOW[theme])
            assert got >= RULE_SEPARATION, (
                f"{theme}/{role} only {got:.2f}:1 off its window")


def test_the_panel_rule_separates_at_least_as_well_on_dark_as_on_light():
    """The failure this role exists for: the deck rows were outlined in one translucent
    grey for both themes, which resolves to roughly the same distance from a light
    window as from a dark one on paper while reading far flatter on the dark one, where
    the eye has less range to spend. Holding dark at or above light is what keeps
    Night Mode's deck list a list of cards rather than a block of text.
    """
    light = contrast(palette.LIGHT["panel_rule"], WINDOW["light"])
    dark = contrast(palette.DARK["panel_rule"], WINDOW["dark"])
    assert dark >= light, (
        f"the panel rule separates {dark:.2f}:1 on dark against {light:.2f}:1 on "
        "light: the dark theme is the flatter of the two again")


def test_both_sets_define_exactly_the_same_roles():
    assert set(palette.LIGHT) == set(palette.DARK)


def test_every_role_is_covered_by_a_contrast_check():
    """ON_WINDOW and PAIRS above are hand-maintained tuples, not derived from
    palette.py, so a role added there and forgotten here would be checked by nothing:
    the exact escaped-because-nobody-looked failure this whole pass kept finding, and
    the contrast ledger being empty by policy (KNOWN_LOW_CONTRAST) makes this file the
    only guard left. NON_TEXT_ROLES is the deliberate, named exception, so a role
    missing from all three fails loudly here instead of quietly passing everywhere
    else.
    """
    covered = set(ON_WINDOW) | set(NON_TEXT_ROLES)
    for fg, bg in PAIRS:
        covered.add(fg)
        covered.add(bg)
    missing = set(palette.LIGHT) - covered
    assert not missing, (
        "role(s) defined in palette.py but exercised by no check in this file: "
        + ", ".join(sorted(missing)))


def test_colors_follows_ankis_theme(monkeypatch):
    monkeypatch.setattr(palette, "is_dark", lambda: True)
    assert palette.colors() == palette.DARK
    monkeypatch.setattr(palette, "is_dark", lambda: False)
    assert palette.colors() == palette.LIGHT
