"""The colour sets, checked as numbers rather than by eye.

qt_tests/test_contrast.py measures what actually paints, which is the ground truth, but it
can only see one dominant colour pair per widget: a small inline span like a row marker is
never the pair it measures. This file is what stands guard over the values themselves, and
it needs no Qt at all.
"""
import sys
import types

from internpearls import palette

AA = 4.5
WINDOW = {"light": "#efefef", "dark": "#2f2f31"}
BASE = {"light": "#ffffff", "dark": "#2f2f31"}
# Roles drawn as text straight onto the dialog, so they answer to the window colour.
# updated_fg is here as well as in PAIRS below: it is paired with updated_bg for the
# review row's marker chip, but dialogs.py also paints it bare on the window for the
# About dialog's "update available" pill, so it needs both checks. The two loops below
# are independent, so listing a role in both is not redundant.
ON_WINDOW = ("why", "accent", "caret", "dim", "muted", "warning", "updated_fg")
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


def test_a_row_rule_stays_quieter_than_the_panel_rule_around_it():
    """The two rule roles are a hierarchy, not two shades of the same thing: a panel
    rule bounds a region of a dialog, a row rule is the hairline between two rows inside
    one. A row rule that reads as strongly as the panel it sits in turns a list into a
    grid, so the ordering is the property worth pinning rather than either value.

    This is also the ceiling on the light row rule. It was raised because the light
    hairline measured the fainter of the two themes, not the darker one as assumed, but
    it can only come up as far as this leaves room for.
    """
    for theme, colors in _sets().items():
        row = contrast(colors["row_rule"], WINDOW[theme])
        panel = contrast(colors["panel_rule"], WINDOW[theme])
        assert row < panel, (
            f"{theme}: the row rule reads {row:.2f}:1 against the panel rule's "
            f"{panel:.2f}:1, so the hairline between rows is as loud as the frame")


def test_neither_theme_draws_a_much_fainter_row_rule_than_the_other():
    """Measured off the renders, the light row rule was 1.26:1 while dark was 1.51:1,
    the opposite of the assumption that the dark theme is always the flat one. Pinning
    them within reach of each other is what stops one theme's lists reading as separated
    rows and the other's as a single block.
    """
    light = contrast(palette.LIGHT["row_rule"], WINDOW["light"])
    dark = contrast(palette.DARK["row_rule"], WINDOW["dark"])
    assert max(light, dark) <= min(light, dark) * 1.25, (
        f"row rules separate {light:.2f}:1 on light and {dark:.2f}:1 on dark, far "
        "enough apart that one theme's rows read as a block")


def test_the_caret_reads_stronger_than_the_dim_text_beside_it():
    """The caret is the only thing on a card row that says the row opens, and opening
    rows is most of what the update screen is for. Drawn in `dim`, the role its own
    neighbouring text uses, it read as punctuation rather than as a control. It is a
    role of its own now, and the point of that role is that it is the louder of the two
    on both themes, by enough to notice rather than by a rounding error.
    """
    for theme, colors in _sets().items():
        caret = contrast(colors["caret"], WINDOW[theme])
        dim = contrast(colors["dim"], WINDOW[theme])
        assert caret >= dim * 1.3, (
            f"{theme}: the caret reads {caret:.2f}:1 against {dim:.2f}:1 for the text "
            "beside it, which is not a difference anybody sees")


def test_the_caret_stays_quieter_than_the_body_text_it_marks():
    """The other half of the same judgement: a caret louder than the card it belongs to
    would be a list of arrows with cards after them. Measured against the window text
    colour of the theme the render suite paints, which is what body text actually is.
    """
    body = {"light": "#000000", "dark": "#d7d7d7"}
    for theme, colors in _sets().items():
        caret = contrast(colors["caret"], WINDOW[theme])
        text = contrast(body[theme], WINDOW[theme])
        assert caret < text, (
            f"{theme}: the caret reads {caret:.2f}:1 against body text's {text:.2f}:1")


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


def test_the_fallback_carries_the_dark_set_where_there_is_no_theme_manager(monkeypatch):
    """The live demo runs this module under a mock Anki with no aqt.theme at all, so
    every dialog it renders took the light set no matter what the reader's browser was
    set to: light hexes painted onto the page's own dark panels. The fallback is the
    only place that can be told otherwise, since there is no theme manager to ask.
    """
    assert palette.is_dark() is False          # nothing has set the hook
    assert palette.colors() == palette.LIGHT
    monkeypatch.setattr(palette, "FALLBACK_DARK", True)
    assert palette.is_dark() is True
    assert palette.colors() == palette.DARK


def test_ankis_own_theme_outranks_the_fallback(monkeypatch):
    """The half worth pinning: inside real Anki the theme manager is always there and
    the hook is dead weight. A fallback that could override Anki's own night mode would
    be a demo convenience shipped as a bug to every user.
    """
    theme = types.ModuleType("aqt.theme")
    theme.theme_manager = types.SimpleNamespace(night_mode=False)
    monkeypatch.setitem(sys.modules, "aqt.theme", theme)
    monkeypatch.setattr(palette, "FALLBACK_DARK", True)
    assert palette.is_dark() is False
    theme.theme_manager.night_mode = True
    assert palette.is_dark() is True
