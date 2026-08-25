"""Real font metrics, which the mock suite has none of.

A label that fits in the mock always fits, because the mock has no font, no wrapping,
and no width. These are the questions only real Qt can answer: does the text fit, does
the dialog fit, do the rows line up.
"""
import pytest

import harness
from sampling import widget_rect

ALL_SCENES = sorted(harness.SCENES)


def _visible_labels(dialog, q):
    return [w for w in dialog.findChildren(q.QLabel)
            if w.isVisible() and w.text().strip()]


@pytest.mark.parametrize("theme", sorted(harness.THEMES))
@pytest.mark.parametrize("scene", ALL_SCENES)
def test_no_label_is_clipped(shot, scene, theme):
    """A label whose content needs more room than it has drops the overflow silently,
    the same way Qt drops a stylesheet rule it dislikes."""
    _, q = harness.bootstrap()
    s = shot(scene, theme=theme)
    clipped = []
    for label in _visible_labels(s.dialog, q):
        # Height only. A word-wrapped label's sizeHint width is its unwrapped width,
        # which legitimately exceeds the widget. Height is what tells the truth once
        # the wrap has happened.
        needed = (label.heightForWidth(label.width()) if label.wordWrap()
                  else label.sizeHint().height())
        if needed > label.height() + 1:
            clipped.append(f"{label.text()[:50]!r} needs {needed}px, "
                           f"has {label.height()}px")
    assert not clipped, f"{scene}/{theme}: clipped labels:\n  " + "\n  ".join(clipped)


@pytest.mark.parametrize("scene", ALL_SCENES)
def test_nothing_overflows_the_dialog_horizontally(shot, scene):
    """The caret bug's signature: a widget wider than the dialog that holds it."""
    _, q = harness.bootstrap()
    s = shot(scene)
    overflowing = []
    for widget in s.dialog.findChildren(q.QWidget):
        if not widget.isVisible():
            continue
        rect = q.QRect(widget.mapTo(s.dialog, q.QPoint(0, 0)), widget.size())
        if rect.right() > s.dialog.width():
            overflowing.append(
                f"{type(widget).__name__} right edge {rect.right()} > dialog "
                f"{s.dialog.width()}")
    assert not overflowing, (
        f"{scene}: widgets overflow the dialog:\n  " + "\n  ".join(overflowing))


def test_a_short_confirmation_starts_at_the_top_not_the_middle(shot):
    """Two ways a short _ask_scrollable confirmation ends up with blank space above its
    first line, both guarded here in one measurement. Scoped to the "ask-scrollable"
    scene, not "confirm": the Update my decks screen moved to a widget body (fixed
    summary label above a stretching card list) once this rework landed, which the
    scroll-viewport-centering defect below never applied to in the first place, so this
    regression test stays on the wrapper it actually guards.

    1. The label's text not being top-aligned within its own box (the original bug:
       setWidgetResizable(True) stretches the label's box to fill the scroll viewport,
       and Qt vertically centres a label's text within its own box by default).
    2. The scroll area itself floating away from the dialog's own top margin, which
       stayed possible even after fix 1: with nothing below the scroll area claiming
       the dialog's leftover height, Qt spread that surplus above, inside, and below
       the scroll area instead of collecting it below the content, so a short
       confirmation still opened with real blank space above the first line, just less
       of it (verified: fixing only #1 left this at 89px in a 620px-tall dialog).

    A fix to #1 alone cannot be told apart from a regression in #2 by measuring pixels
    only inside the label's own box, since that box's top moves with #2 too. So this
    reads pixels from the DIALOG's own top edge, not the label's: it walks down from
    row 0 and finds the first row, anywhere in the label's rect, that isn't background
    colour. That is the actual blank gap a person looking at the dialog sees.
    """
    _, q = harness.bootstrap()
    s = shot("ask-scrollable")
    body = max((w for w in s.dialog.findChildren(q.QLabel) if w.text()),
               key=lambda w: len(w.text()))
    rect = widget_rect(s.dialog, body)
    background = s.image.pixelColor(rect.left(), rect.top()).name()
    first_ink_row = next(
        (y for y in range(0, rect.bottom() + 1)
         if any(s.image.pixelColor(x, y).name() != background
               for x in range(rect.left(), rect.right() + 1))),
        rect.bottom() + 1)
    assert first_ink_row < 40, (
        f"the confirmation text starts painting at row {first_ink_row} of the dialog; "
        "it should hug the dialog's own top margin, not float partway down")


def test_manage_decks_empty_state_starts_at_the_top_not_the_middle(shot):
    """Same defect as `test_a_short_confirmation_starts_at_the_top_not_the_middle`,
    one call site over: the Manage decks empty state ("No decks available yet...")
    used to sit in its own QScrollArea with the default Expanding size policy, which
    claimed the dialog's leftover height and left the message floating mid-panel
    instead of hugging the row above it.

    Manage decks has real content above the empty message (title, source line,
    instructions), so this can't scan from the dialog's row 0 the way the confirmation
    test does; that would just find the title's ink. Instead it bounds the scan between
    two stable anchors: the bottom of the instructions paragraph (the last fixed row
    before the empty-state region, since the Select all/Select none bar isn't built at
    all with no rows to select) and the top of the "Preserved fields" section (the next
    fixed row after it). Only the empty-state message lives in that band, so the first
    non-background pixel in it is exactly the gap this message opens with.
    """
    _, q = harness.bootstrap()
    s = shot("manage-decks", empty=True)
    labels = [w for w in s.dialog.findChildren(q.QLabel) if w.text().strip()]
    body = next(w for w in labels if "No decks available" in w.text())
    preserved = next(w for w in labels if w.text() == "Preserved fields")
    instructions = next(w for w in labels
                        if w.text().startswith("Check the decks you want"))

    top_bound = widget_rect(s.dialog, instructions).bottom()
    bottom_bound = widget_rect(s.dialog, preserved).top()
    body_rect = widget_rect(s.dialog, body)

    background = s.image.pixelColor(body_rect.left(), top_bound).name()
    first_ink_row = next(
        (y for y in range(top_bound, bottom_bound)
         if any(s.image.pixelColor(x, y).name() != background
               for x in range(body_rect.left(), body_rect.right() + 1))),
        bottom_bound)
    gap_above = first_ink_row - top_bound
    assert gap_above < 40, (
        f"the empty-state message starts painting {gap_above}px below the row "
        "above it; it should hug that row, not float partway down toward "
        "Preserved fields")


def test_a_long_source_error_wraps_rather_than_widening_the_dialog(shot):
    """A local folder that has moved puts its whole path in the Source line, and that
    line used to be the one label in the panel that never wrapped: measured here, a
    five-segment path stretched the 480px dialog to 1374px, one line of error text wide.
    """
    _, q = harness.bootstrap()
    long_dir = "/" + "/".join(["a-folder-that-is-not-here"] * 5)
    s = shot("manage-decks", decks_dir=long_dir)
    label = next(l for l in _visible_labels(s.dialog, q)
                 if l.text().startswith("Source:"))
    assert label.wordWrap(), "the Source line does not wrap"
    assert label.fontMetrics().horizontalAdvance(label.text()) > s.dialog.width(), (
        "the fixture path is short enough to fit on one line, so it proves nothing")
    # 640 is what the harness asks for; anything wider means the dialog had to grow to
    # its own sizeHint to hold that one line.
    assert s.dialog.width() <= 640, (
        f"the error widened the dialog to {s.dialog.width()}px")


def test_a_wide_deck_label_is_elided_by_pixels_not_by_character_count():
    """The character cap cannot keep a row inside the panel on its own: 42 characters
    of a wide glyph (CJK, or just a run of capitals) paints at several times the width
    of 42 narrow ones, so a name the cap passes through untouched still runs past the
    space the cap stands for. Elided against real font metrics instead, which is the
    one thing the mock suite cannot measure.
    """
    _, q = harness.bootstrap()
    harness.app()
    from internpearls import dialogs
    name = "W" * dialogs._DECK_LABEL_MAX
    assert dialogs._elide(name) == name, (
        "the fixture must sit inside the character cap, or it proves nothing")

    panel = dialogs._DeckManagerDialog.__new__(dialogs._DeckManagerDialog)
    panel._checks = {}
    row = panel._deck_row({"name": f"Root::{name}", "cards": 1, "enabled": True,
                           "state": "current"}, name)
    box = row.findChildren(q.QCheckBox)[0]
    box.ensurePolished()
    metrics = box.fontMetrics()
    assert metrics.horizontalAdvance(name) > dialogs._DECK_LABEL_W, (
        "the fixture already fits the pixel budget, so nothing is being tested")
    assert metrics.horizontalAdvance(box.text()) <= dialogs._DECK_LABEL_W, (
        f"the row paints {box.text()!r} at "
        f"{metrics.horizontalAdvance(box.text())}px, past its {dialogs._DECK_LABEL_W}px "
        "budget")
    assert "…" in box.text() and box.toolTip() == f"Root::{name}"
    # The other half of the rule: an ordinary name, which is most of a list, is left
    # exactly as it is.
    assert dialogs._fit_label("Pharmacology") == "Pharmacology"


def test_the_source_options_stack_rather_than_share_a_row(shot):
    """What the deck-source screen was rebuilt out of: three sources crammed onto one
    QMessageBox button row beside Cancel, reading as four equal buttons. Each source is
    a full-width button on its own line now, so they share a left edge, run top to
    bottom in the offered order, and all sit above Cancel.
    """
    _, q = harness.bootstrap()
    s = shot("configure-source")
    rects = {b.text(): widget_rect(s.dialog, b)
             for b in s.dialog.findChildren(q.QPushButton)}
    options = [rects[label] for label in
               ("Try the example deck", "GitHub repo", "Local folder")]
    assert len({r.left() for r in options}) == 1, (
        f"the options start at different x: {[r.left() for r in options]}")
    tops = [r.top() for r in options]
    assert tops == sorted(tops) and len(set(tops)) == 3, (
        f"the options are not stacked in the offered order: {tops}")
    assert rects["Cancel"].top() > options[-1].bottom(), (
        "Cancel sits alongside the sources again; it belongs below them, out of the "
        "choice")


SETTINGS_SECTIONS = ("Deck sync", "Add-on updates", "Night mode", "Card review")


def test_each_settings_section_is_ruled_off_from_the_next(shot):
    """Settings is four unrelated decisions in one window, and used to read as one
    column of small grey prose with checkboxes in it: a bold heading was the only thing
    marking where one section ended and the next began, and a heading is easy to lose
    between two paragraphs set at the same weight.

    One hairline sits in each gap now. Measured by position rather than by count alone,
    since three rules bunched anywhere in the dialog would satisfy a count and separate
    nothing. That the rule's own colour actually paints is test_declared.py's job.
    """
    _, q = harness.bootstrap()
    s = shot("settings")
    headings = {l.text(): widget_rect(s.dialog, l)
                for l in _visible_labels(s.dialog, q) if l.text() in SETTINGS_SECTIONS}
    assert sorted(headings) == sorted(SETTINGS_SECTIONS), (
        f"expected every section heading, found {sorted(headings)}")
    rules = [widget_rect(s.dialog, f).top()
             for f in s.dialog.findChildren(q.QFrame)
             if f.isVisible() and f.frameShape() == q.QFrame.Shape.HLine]
    gaps = {}
    for above, below in zip(SETTINGS_SECTIONS, SETTINGS_SECTIONS[1:]):
        top, bottom = headings[above].bottom(), headings[below].top()
        gaps[f"{above} / {below}"] = [y for y in rules if top < y < bottom]
    wrong = {gap: found for gap, found in gaps.items() if len(found) != 1}
    assert not wrong, (
        f"these section gaps do not hold exactly one hairline: {wrong}. Four sections "
        "read as four groups only if each boundary is drawn.")


def test_review_rows_share_a_left_edge(shot):
    """Tagged and untagged rows must start at the same x.

    Before v0.32.1 the tag sat in its own widget beside the text, so a tagged row's
    text started about 150px right of an untagged one. The fixture has both: rows 0 and
    1 are tagged, row 2 is not.
    """
    _, q = harness.bootstrap()
    s = shot("confirm")
    lefts = {}
    for label in _visible_labels(s.dialog, q):
        for marker, row in (("one short line", 0), ("deliberately long", 1),
                            ("untagged row", 2)):
            if marker in label.text():
                lefts[row] = widget_rect(s.dialog, label).left()
    assert len(lefts) == 3, f"expected all three primary rows, found {sorted(lefts)}"
    assert len(set(lefts.values())) == 1, (
        f"rows start at different x: {lefts}. A tagged row's text must begin where an "
        "untagged row's does.")


def _label_left(dialog, q, marker):
    """The x of the one visible label whose text contains `marker`, in dialog space."""
    found = [l for l in _visible_labels(dialog, q) if marker in l.text()]
    assert len(found) == 1, f"expected exactly one label containing {marker!r}, got {len(found)}"
    return widget_rect(dialog, found[0]).left()


def _row_primary_left(dialog, q, trailing_marker):
    """The x of the primary label in the row whose trailing column holds
    `trailing_marker`.

    Needed for a per-deck summary row, whose primary text is the deck name and so
    reads identically to the section heading further down the list. The trailing
    counts are what tell the two apart.
    """
    trailing = [l for l in _visible_labels(dialog, q) if trailing_marker in l.text()]
    assert len(trailing) == 1, (
        f"expected one trailing label containing {trailing_marker!r}, got {len(trailing)}")
    row = trailing[0].parent()
    primary = [l for l in row.findChildren(q.QLabel) if l.isVisible() and l is not trailing[0]]
    assert primary, f"the row holding {trailing_marker!r} has no primary label"
    return widget_rect(dialog, primary[0]).left()


def test_every_confirm_card_row_starts_its_text_at_one_x(shot):
    """The whole point of the caret and chip columns: one grid for every row in a
    section that holds a chipped row, whatever kind each row is.

    A retired or moved row draws no caret, so each of them had its own reason to sit
    left of the card rows beside it, and the reader sees a ragged list rather than a
    tidy one. Compared against each other, never against a magnitude: the chip column
    is measured at the running platform's own font.
    """
    _, q = harness.bootstrap()
    s = shot("confirm")
    lefts = {marker: _label_left(s.dialog, q, marker) for marker in (
        "one short line",            # a card row: caret and chip
        "since-split card",          # a retired row: chip, no caret
        "deck was reorganized")}     # a moved row: chip, no caret
    assert len(set(lefts.values())) == 1, (
        f"rows start their text at different x: {lefts}. Every row in a chipped "
        "section reserves the same caret and chip columns, whether or not it fills "
        "them.")


def test_the_confirm_summary_sits_flush_with_its_own_heading(shot):
    """Alignment is decided per section, and the per-deck summary is its own section:
    nothing in it is ever chipped and nothing in it ever expands, so reserving the two
    card columns floated every deck name out over an empty gutter, right of the heading
    directly above it. Its rows decline the columns; the card sections below keep them
    (the test above), which is the same call made twice with different answers.
    """
    _, q = harness.bootstrap()
    s = shot("confirm")
    # The summary row's primary is the deck name, which reads the same as the section
    # heading further down the list, so it is found by its own trailing counts instead.
    summary_left = _row_primary_left(s.dialog, q, "3 kept")
    heading_left = _label_left(s.dialog, q, "updates:")
    assert summary_left == heading_left, (
        f"the summary heading starts at {heading_left} and its deck names at "
        f"{summary_left}; a section with nothing to line up against reserves no gutter")
    assert summary_left < _label_left(s.dialog, q, "one short line"), (
        "the card rows below have lost their own grid, rather than the summary having "
        "given up a gutter it could never use")


def test_a_rows_trailing_column_stops_short_of_the_list_frame(shot):
    """A summary row's counts and a moved row's destination are the rightmost thing on
    their row, and used to end exactly at the row's own right edge: the glyphs touched
    the enclosing list's border line with nothing between them.
    """
    _, q = harness.bootstrap()
    from internpearls import widgets
    s = shot("confirm")
    cramped = []
    for marker in ("3 kept",             # a summary row's counts
                   "Regional Basics"):   # a moved row's destination
        found = [l for l in _visible_labels(s.dialog, q) if marker in l.text()]
        assert len(found) == 1, f"expected one label containing {marker!r}"
        gap = (widget_rect(s.dialog, found[0].parent()).right()
               - widget_rect(s.dialog, found[0]).right())
        if gap < widgets.CARET_GAP:
            cramped.append(f"{marker!r}: {gap}px")
    assert not cramped, (
        "trailing text runs up against its row's right edge: " + ", ".join(cramped))


def test_result_rows_sit_flush_with_the_heading_and_the_footer(shot):
    """And the mirror of it: a screen with no chips and nothing to expand reserves
    neither column, so its outcome lines start where its own heading and backup line
    do. Reserving a chip column here indented every result line by the width of a pill
    nothing on the screen paints.
    """
    _, q = harness.bootstrap()
    s = shot("result")
    lefts = {marker: _label_left(s.dialog, q, marker) for marker in (
        "Update complete",     # the heading
        "29 kept",             # a result row
        "Archived",            # another result row
        "A backup of the deck")}   # the footer
    assert len(set(lefts.values())) == 1, (
        f"the result screen's parts start at different x: {lefts}. Its rows carry no "
        "chip and cannot expand, so they line up with the heading above them.")


def test_the_result_heading_reads_larger_than_the_flagged_heading(shot):
    """The run's own outcome is what the end screen reports; the flagged-card count is
    a heading inside it. They shipped the other way round, so a completed run announced
    its subordinate heading in the larger type and its result in the smaller.

    Compared against each other rather than against a size: type sizes are read from
    the running platform's own font.
    """
    _, q = harness.bootstrap()
    s = shot("result")
    labels = _visible_labels(s.dialog, q)
    result = next(l for l in labels if l.text().startswith("Update complete"))
    flagged = next(l for l in labels if l.text().endswith("flagged"))
    sizes = (q.QFontInfo(result.font()).pixelSize(),
             q.QFontInfo(flagged.font()).pixelSize())
    assert sizes[0] > sizes[1], (
        f"the result heading paints at {sizes[0]}px and the flagged heading at "
        f"{sizes[1]}px: the run's result is the headline of the screen reporting it")


def test_the_confirm_summary_is_rows_under_one_heading_not_a_bullet_list(shot):
    """The deck summary that opens the list reads in the same row vocabulary as
    everything below it: a heading, then one row per deck with its counts in the
    trailing column. It used to be an indented <ul> dropped into the label above the
    list, which is the last bulleted list this screen carried.
    """
    _, q = harness.bootstrap()
    s = shot("confirm")
    texts = [l.text() for l in _visible_labels(s.dialog, q)]
    assert not [t for t in texts if "<li>" in t or "<ul>" in t], \
        "the deck summary is built from rows now, not a bulleted list inside a label"
    assert any(t.startswith("1 deck has updates") for t in texts), \
        "the summary keeps its own heading"
    assert any("3 kept (1 changing)" in t and "2 new" in t for t in texts), \
        "a deck's counts read as the row's trailing column"


def test_a_decks_section_holds_all_four_row_kinds(shot):
    """Retired and moved cards used to hang below the list under headings of their
    own. Everything pending for one deck now reads under that deck's heading, in the
    order added, changed, archived, moved, so a reader meets a deck once rather than
    meeting the same deck name in four places.
    """
    _, q = harness.bootstrap()
    from internpearls import widgets
    s = shot("confirm")
    pills = [l.text() for l in _visible_labels(s.dialog, q)
             if l.text() in set(widgets.CHIPS.values())]
    assert pills == [widgets.CHIPS["new"], widgets.CHIPS["changed"],
                     widgets.CHIPS["retired"], widgets.CHIPS["moved"]], (
        f"the deck's section does not hold all four kinds in order: {pills}")
    headings = [l.text() for l in _visible_labels(s.dialog, q)
                if l.text() in ("Retired", "Moved")]
    assert not headings, (
        f"a kind still has a heading of its own: {headings}. Each row's chip already "
        "says what it is; the heading says which deck it belongs to.")


def test_sync_rows_start_their_text_at_one_x(shot):
    """Sync decks' confirmation is a list of deck rows now, so it answers the same
    question the update screen does: every row in a chipped section reserves the same
    caret and chip columns, whatever its own chip happens to be, so a NEW deck and an
    UPDATED one read down one edge rather than two.
    """
    _, q = harness.bootstrap()
    s = shot("sync-confirm")
    lefts = {marker: _row_primary_left(s.dialog, q, marker) for marker in (
        "6 cards",      # an UPDATED deck
        "4 cards",      # a NEW deck
        "128 cards")}   # a NEW deck whose name wraps
    assert len(set(lefts.values())) == 1, (
        f"deck rows start their text at different x: {lefts}")


def test_reconcile_rows_start_their_text_at_one_x(shot):
    """And the same for Reconcile my decks, whose three groups each carry a different
    kind of row: an archived card, a reworded pair, a relocated card. They are one list
    under three explanations, not three lists.
    """
    _, q = harness.bootstrap()
    s = shot("reconcile-confirm")
    lefts = {marker: _label_left(s.dialog, q, marker) for marker in (
        "since-split card",        # a retired row
        "An older wording",        # a reworded pair, both halves in one line
        "deck was reorganized")}   # a moved row
    assert len(set(lefts.values())) == 1, (
        f"rows start their text at different x: {lefts}")


@pytest.mark.parametrize("scene,markers", [
    ("duplicates-confirm", ("Found <b>2</b> duplicate", "keeping the one with")),
    ("empty-cards-confirm", ("Found <b>3</b> empty", "no content on any card")),
])
def test_a_single_kind_confirmation_starts_flush_with_its_own_heading(
        shot, scene, markers):
    """Clean up duplicates and Remove empty cards each list one kind of card, so there
    is no chip on any row and nothing for an unchipped row to line up against. Both
    decline the caret and chip columns, which means every row starts where the heading
    above it and the closing note below it start. Reserving the columns here indented
    the whole list by the width of a pill neither screen paints.
    """
    _, q = harness.bootstrap()
    s = shot(scene)
    lefts = {marker: _label_left(s.dialog, q, marker)
             for marker in markers + ("wrap lands under the text",)}
    # Within a pixel, not exactly equal: the rows sit inside the list's own frame and
    # the fixed text above and below it does not, so the border line itself is one
    # pixel of legitimate difference. A reserved chip column would be seventy-odd.
    assert max(lefts.values()) - min(lefts.values()) <= 1, (
        f"{scene}: the list is indented from its own fixed text: {lefts}")


@pytest.mark.parametrize("scene,markers", [
    ("sync-confirm", ("6 cards", "128 cards")),
    ("reconcile-confirm", ("Gadget Care", "Regional Basics")),
    ("empty-cards-confirm", ("c3, c4", "c5")),
])
def test_a_confirmation_rows_trailing_column_stops_short_of_the_frame(
        shot, scene, markers):
    """A deck's size, a retired card's deck and a moved card's destination are the
    rightmost thing on their rows, and must not run their glyphs up against the border
    line of the list they are drawn inside."""
    _, q = harness.bootstrap()
    from internpearls import widgets
    s = shot(scene)
    cramped = []
    for marker in markers:
        found = [l for l in _visible_labels(s.dialog, q) if marker in l.text()]
        assert len(found) == 1, f"expected one label containing {marker!r}"
        gap = (widget_rect(s.dialog, found[0].parent()).right()
               - widget_rect(s.dialog, found[0]).right())
        if gap < widgets.CARET_GAP:
            cramped.append(f"{marker!r}: {gap}px")
    assert not cramped, (
        "trailing text runs up against its row's right edge: " + ", ".join(cramped))


@pytest.mark.parametrize("scene", sorted(harness.SCENES))
def test_no_screen_carries_a_bullet_list(shot, scene):
    """No screen in the add-on lists anything as HTML bullets inside one label any
    more: a card, a deck, a setting or an outcome line is a row. A <ul> anywhere in any
    scene means one of them has fallen back to a list Qt draws with no alignment, no
    hairlines and no streaming.
    """
    _, q = harness.bootstrap()
    s = shot(scene)
    bulleted = [l.text()[:60] for l in _visible_labels(s.dialog, q)
                if "<li>" in l.text() or "<ul>" in l.text()]
    assert not bulleted, f"{scene} still renders a bulleted list: {bulleted}"


def test_the_look_change_checkbox_sits_with_the_text_that_explains_it(shot):
    """The checkbox used to be added under the whole body, which puts it below a list
    that streams as many rows as the update has pending: on a real update the sentence
    saying what ticking it costs is hundreds of rows above the box that acts on it, and
    a reader who never scrolls back never meets the two together. It sits between that
    fixed text and the list now, so both are on screen at once.
    """
    _, q = harness.bootstrap()
    s = shot("confirm", checkbox=True)
    boxes = [w for w in s.dialog.findChildren(q.QCheckBox) if w.isVisible()]
    assert len(boxes) == 1, "the confirmation carries exactly one checkbox"
    intro = [l for l in _visible_labels(s.dialog, q) if "catch-up" in l.text()]
    lists = [w for w in s.dialog.findChildren(q.QScrollArea) if w.isVisible()]
    assert intro and lists

    def top(w):
        return w.mapTo(s.dialog, q.QPoint(0, 0)).y()

    assert top(intro[0]) < top(boxes[0]) < min(top(w) for w in lists)


def test_the_look_change_checkbox_is_not_read_as_answering_the_format_change(shot):
    """A run can carry both a look change and a note-type format change, and the format
    one has its own dialog rather than this checkbox. The box lands under the last of
    the fixed text, so with the format paragraph written last it sat directly beneath a
    question it does not answer: the look sentence goes last instead."""
    _, q = harness.bootstrap()
    s = shot("confirm", checkbox=True)
    boxes = [w for w in s.dialog.findChildren(q.QCheckBox) if w.isVisible()]
    intro = [l for l in _visible_labels(s.dialog, q)
             if "changed format" in l.text() and "how some cards look" in l.text()]
    assert len(boxes) == 1 and len(intro) == 1, "the scene lost one of the two"
    text = intro[0].text()
    assert text.index("changed format") < text.index("how some cards look"), (
        "the look sentence is not the last thing above the checkbox")

    def top(w):
        return w.mapTo(s.dialog, q.QPoint(0, 0)).y()

    assert top(intro[0]) < top(boxes[0])
