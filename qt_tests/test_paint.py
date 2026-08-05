"""The specific rules v0.32.1 fixed, asserted as pixels rather than as spelling.

tests/test_review.py checks that the why rule's stylesheet resets `border` before
setting `border-left`, because that is what Qt needs to not silently drop it. That test
passes if someone writes a different rule that Qt also drops. This one looks for the
green.

Counts are never asserted, only presence: ubuntu and macOS disagree about font metrics,
so "some of this colour is inside this widget" ports and "102 pixels of it" does not.

Every colour read here comes from `palette.LIGHT` or `palette.DARK` directly, picked by
the literal theme name the test itself passed to `shot(...)`, never from
`palette.colors()`. `shot` is a session-scoped cache: it only actually renders (and so
only flips the theme harness.apply_theme() stubs into aqt.theme.theme_manager) on the
first call for a given (scene, theme, ...) key, and every later call in the file, from
any test, can return that same cached Shot with no render at all. Reading
`palette.colors()` after such a call sees whichever theme the *last* real render in the
session happened to leave the stub on, not the theme this particular Shot was actually
painted with. The literal set sidesteps that: it names the theme this test asked for,
not whatever the stub currently says.
"""
import harness
from internpearls import palette
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
    why_rule = palette.LIGHT["why"]
    s = shot("review", expand=(0,))
    why = next(w for w in s.dialog.findChildren(q.QLabel)
               if "common case" in w.text())
    rect = widget_rect(s.dialog, why)
    edge = q.QRect(rect.left(), rect.top(), 3, rect.height())
    assert colour_counts(s.image, edge).get(why_rule, 0) > 0, (
        f"the why rule's green {why_rule} is not painting down the label's left edge. "
        "Qt has dropped the border-left again: it needs `border: none` before it.")


def test_the_dosing_block_paints_its_own_background(shot):
    """Row 1 is the only fixture row with a Dosing field."""
    dosing_bg, dosing_fg = palette.LIGHT["dosing_bg"], palette.LIGHT["dosing_fg"]
    s = shot("review", expand=(1,))
    painted = colour_counts(s.image)
    assert painted.get(dosing_bg, 0) > 0, f"the dosing background {dosing_bg} is absent"
    assert painted.get(dosing_fg, 0) > 0, (
        f"the dosing foreground {dosing_fg} is absent: the text is taking its colour "
        "from the palette again, which is what turned it white on near-white in Night "
        "Mode")


def test_the_dosing_block_stays_readable_on_a_dark_palette(shot):
    """The regression that mattered: a hardcoded background needs a hardcoded
    foreground, or the theme flips one and not the other."""
    dosing_bg, dosing_fg = palette.DARK["dosing_bg"], palette.DARK["dosing_fg"]
    s = shot("review", theme="dark", expand=(1,))
    painted = colour_counts(s.image)
    assert painted.get(dosing_bg, 0) > 0 and painted.get(dosing_fg, 0) > 0, (
        "the dosing block loses its own colours on a dark palette")


def test_a_cloze_deletion_paints_in_the_decks_blue(shot):
    cloze_colour = palette.LIGHT["accent"]
    s = shot("review")
    assert colour_counts(s.image).get(cloze_colour, 0) > 0, (
        f"no cloze blue {cloze_colour}: the <style> block is being dropped, so "
        "deletions are rendering as plain text")


def test_only_one_hairline_is_drawn_between_two_rows(shot):
    """The other v0.32.1 bug: the row set a selector-less stylesheet, which propagates
    into child widgets, so the row and its header each drew a rule and the separator
    came out doubled. Two runs of the rule colour at clearly different widths is the
    fingerprint (the row's own width, and the narrower header's); one width means one
    line.
    """
    row_rule = palette.LIGHT["row_rule"]
    s = shot("review")
    widths = set()
    for y in range(s.image.height()):
        run = 0
        for x in range(s.image.width()):
            if s.image.pixelColor(x, y).name() == row_rule:
                run += 1
            elif run:
                widths.add(run)
                run = 0
        if run:
            widths.add(run)
    # 100 is only a noise floor to drop 1 to 2px antialiasing runs, well below any real
    # separator, which spans most of the dialog width. The real check is len <= 1 below.
    significant = {w for w in widths if w > 100}
    assert len(significant) <= 1, (
        f"the rule colour {row_rule} paints runs of {sorted(significant)}px. More "
        "than one width means more than one line: a selector-less stylesheet is "
        "leaking into child widgets again.")


def test_a_row_markers_background_actually_paints(shot):
    """tests/test_review.py checks that each marker pair clears WCAG AA on its own; it
    cannot see whether Qt's rich text actually honours the span's background at all. If
    the background never painted, the marker's foreground would land straight on the
    row's own window colour instead of on its pill, which is a different failure than a
    badly chosen pair and invisible to a string-level check. The fixture's first two
    rows carry "new" and "changed" kinds (see synthetic_details), so both pills are on
    screen with no row expanded.
    """
    from internpearls.widgets import CHIPS
    pairs = {"new": (palette.LIGHT["new_bg"], palette.LIGHT["new_fg"]),
             "changed": (palette.LIGHT["updated_bg"], palette.LIGHT["updated_fg"])}
    s = shot("review")
    painted = colour_counts(s.image)
    for kind in pairs:   # review only ever renders these two chip kinds (see sync.py)
        label = CHIPS[kind]
        background, _foreground = pairs[kind]
        assert painted.get(background, 0) > 0, (
            f"the {kind} marker's background {background} ({label}) is not painting: "
            "Qt is not honouring the span's background-color")


def test_the_about_link_paints_in_the_accent_colour_light(shot):
    """internpearls/ui.py's _ask_scrollable sets setOpenExternalLinks(True) on the body
    label, so About's repository line is a real <a href> anchor. Qt paints an anchor in
    its own built-in link colour, never in anything set on the widget -- verified
    directly: giving the label's palette a Link role has no effect at all on what its
    rich text paints, only a <style> block inside the document itself does. That is
    exactly why the anchor survived the whole colour pass untouched: every other role
    reached its widget through a stylesheet or an inline span colour, and this is the
    one spot neither mechanism was in play. Scoped to the body label's own rect, since a
    whole-image search would also pass if the accent colour happened to paint somewhere
    else in the dialog.
    """
    accent = palette.LIGHT["accent"]
    _, q = harness.bootstrap()
    s = shot("about")
    body = max((w for w in s.dialog.findChildren(q.QLabel) if w.text()),
               key=lambda w: len(w.text()))
    rect = widget_rect(s.dialog, body)
    assert colour_counts(s.image, rect).get(accent, 0) > 0, (
        f"the About link is not painting in the accent colour {accent}: Qt is still "
        "using its own built-in link colour instead")


def test_the_about_link_paints_in_the_accent_colour_dark(shot):
    """Same check, on the theme where Qt's own link blue is the one the audit called
    out: dark blue on a near-black window."""
    accent = palette.DARK["accent"]
    _, q = harness.bootstrap()
    s = shot("about", theme="dark")
    body = max((w for w in s.dialog.findChildren(q.QLabel) if w.text()),
               key=lambda w: len(w.text()))
    rect = widget_rect(s.dialog, body)
    assert colour_counts(s.image, rect).get(accent, 0) > 0, (
        f"the About link is not painting in the accent colour {accent}: Qt is still "
        "using its own built-in link colour instead")


def test_a_normal_confirmation_does_not_open_external_links(shot):
    """internpearls/ui.py's _ask_scrollable is the shared confirmation wrapper, and
    several callers (sync.py) interpolate collection content into its body that is not
    escaped: a card front, a retired-card identity, a raw note field. Before
    open_external_links defaulted to off, any anchor hiding in that content would launch
    the system browser on click. The "confirm" scene is an ordinary caller (no
    open_external_links argument), so its body must come back closed.
    """
    _, q = harness.bootstrap()
    s = shot("confirm")
    body = max((w for w in s.dialog.findChildren(q.QLabel) if w.text()),
               key=lambda w: len(w.text()))
    assert body.openExternalLinks() is False, (
        "a plain confirmation dialog is opening external links; unescaped collection "
        "content routed through _ask_scrollable could launch the system browser")


def test_about_opens_external_links(shot):
    """About is the one caller that wants its body's anchor (the repository link)
    actually clickable, and its body is fixed add-on text, never collection content, so
    it passes open_external_links=True explicitly."""
    _, q = harness.bootstrap()
    s = shot("about")
    body = max((w for w in s.dialog.findChildren(q.QLabel) if w.text()),
               key=lambda w: len(w.text()))
    assert body.openExternalLinks() is True, (
        "About is not opening external links: its repository anchor would render as "
        "inert text")


def test_an_expanded_image_row_paints_the_picture_itself(shot):
    """The mock suite can only assert that an <img> tag was written. Whether Qt's rich
    text actually loads a local file and paints it is a pixel question, and it is the
    whole point of the feature: a tag that renders as a broken-image icon passes every
    structural test there is.

    The fixture image is a solid magenta block, a colour nothing in the dialog's own
    palette uses, so finding it means the file was decoded and painted rather than
    approximated by a placeholder.
    """
    harness.bootstrap()
    s = shot("review", expand=(4,), image=True)
    assert colour_counts(s.image).get("#ff00ff", 0) > 100, (
        "the extracted picture is not painting: Qt did not load the file the <img> src "
        "points at")
