"""The chip is a fixed-width column, and its pill is a real rounded widget.

Both properties are why widgets.chip_cell exists, and neither is answerable from
structure. A chip used to be an inline `<span>` prepended into the row's own paragraph,
which cost the list two things at once: a row's primary text started at a different x
for every chip word (measured: 49px with NEW, 64 with MOVED, 71 with RETIRED, 76 with
UPDATED, and hard left with none), and Qt's rich-text engine silently dropped the span's
`border-radius` and `padding`, so the pill was a bare rectangle however it was spelled.

Offsets are compared against each other, never against a magnitude: the labels measure
differently on macOS and on ubuntu CI, so the column's width is not portable but its
sameness is.
"""
import harness
from internpearls import palette
from sampling import widget_rect

# The kinds, plus the unchipped row that has to line up with them. Read off CHIPS
# rather than spelled out, so a fifth kind is covered the day it is added.
def _kinds():
    from internpearls import widgets
    return list(widgets.CHIPS) + [None]


def _row_grid():
    """One widgets.simple_row of every kind plus one with no chip, laid out for real.

    simple_row rather than review._card_row: it is the one row builder where all five
    cases can stand side by side (a card row only ever carries new, changed, or
    nothing), and where the chip column is the row's own first column rather than one
    of several. review._card_row's own alignment is covered next door, by
    test_layout.py's test_review_rows_share_a_left_edge, against the real dialog.

    Returns (container, image, [(kind, row), ...]).
    """
    _, q = harness.bootstrap()
    app = harness.app()
    harness.apply_theme("light")   # so the pill's colours are palette.LIGHT's, not a
                                   # set left behind by whichever scene rendered last
    from internpearls import widgets

    container = q.QWidget()
    lay = q.QVBoxLayout(container)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)
    rows = []
    for kind in _kinds():
        row = widgets.simple_row(kind, f"The primary text of a {kind} row.")
        lay.addWidget(row)
        rows.append((kind, row))
    container.resize(600, 40 * len(rows))
    container.show()
    app.processEvents()
    return container, container.grab().toImage(), rows


def _primary(row):
    """The row's primary label: the column right after the chip cell."""
    return row.layout().itemAt(1).widget()


def test_every_row_starts_its_primary_text_at_the_same_offset():
    """The regression this whole column exists to stop, and the one that will go
    quietly: nothing else in the suite notices a chip that has gone back to being part
    of the row's own paragraph, because the words all still read correctly.
    """
    _, q = harness.bootstrap()
    _container, _image, rows = _row_grid()
    offsets = {kind: _primary(row).mapTo(row, q.QPoint(0, 0)).x()
               for kind, row in rows}
    assert len(set(offsets.values())) == 1, (
        f"rows start their primary text at different x: {offsets}. Every row reserves "
        "the same chip column, whichever chip it carries and whether it carries one "
        "at all.")


def _visible_pills(dialog, q):
    from internpearls import widgets
    labels = set(widgets.CHIPS.values())
    return [l for l in dialog.findChildren(q.QLabel)
            if l.isVisible() and l.text() in labels]


def test_a_screen_that_cannot_decline_measures_a_narrower_column():
    """KEPT YOURS is half again as wide as NEW, and measuring every chip in the add-on
    into every screen's column widened each pill, gutter and indent on Sync decks,
    Reconcile and Manage decks for a state none of those three can ever reach."""
    _, _q = harness.bootstrap()
    harness.app()
    from internpearls import widgets
    deck_screen = widgets.chip_column_width(("new", "changed"))
    everything = widgets.chip_column_width()
    assert 0 < deck_screen < everything, (
        f"a deck screen's chip column measures {deck_screen} against the whole "
        f"add-on's {everything}: it is still being measured against a chip it cannot "
        "show")


def test_manage_decks_paints_a_narrower_pill_than_the_decline_screen():
    """The same thing one level up, on the two screens themselves: the measurement is
    only worth having if each screen's rows are actually built with its own set."""
    _, q = harness.bootstrap()
    decks = harness.render("manage-decks")
    declines = harness.render("confirm", declined=True, size=(660, 900))
    deck_pills = _visible_pills(decks.dialog, q)
    decline_pills = _visible_pills(declines.dialog, q)
    assert deck_pills and decline_pills
    assert max(p.width() for p in deck_pills) < max(p.width() for p in decline_pills), (
        "Manage decks' pills are as wide as the screen that can show KEPT YOURS")


def _pill(row):
    """The pill inside a row's chip cell, or None on an unchipped row."""
    cell_layout = row.layout().itemAt(0).widget().layout()
    return cell_layout.itemAt(0).widget() if cell_layout is not None else None


def test_every_pill_is_the_same_width():
    """Four pills at their own natural widths inside a fixed gutter leave four
    different right edges down the list, which still reads as ragged even though every
    row's text now lines up. The pills are measured, not the cells that hold them: a
    cell is fixed-width by construction, so measuring those would pass whatever the
    pill inside it does."""
    _container, _image, rows = _row_grid()
    widths = {kind: _pill(row).width() for kind, row in rows if _pill(row) is not None}
    assert len(set(widths.values())) == 1, (
        f"the pills are different widths: {widths}")


def test_a_pill_paints_with_rounded_corners():
    """What Qt's rich text dropped. Sampled rather than asserted on the stylesheet: a
    `border-radius` declaration that never reaches a painter spells exactly the same as
    one that does, which is how the inline version shipped square for as long as it did.

    The pill's own top-left corner pixel sits outside the rounded shape, so it stays
    whatever is behind the row; the pixel directly below it, halfway down the left edge,
    is inside. A square pill makes those two the same colour.
    """
    _, q = harness.bootstrap()
    container, image, rows = _row_grid()
    background = palette.LIGHT["new_bg"]
    row = next(row for kind, row in rows if kind == "new")
    rect = widget_rect(container, _pill(row))

    edge = image.pixelColor(rect.left(), rect.top() + rect.height() // 2).name()
    corner = image.pixelColor(rect.left(), rect.top()).name()
    assert edge == background, (
        f"the pill's left edge at mid-height is {edge}, not its own background "
        f"{background}: this test is no longer sampling the pill")
    assert corner != edge, (
        f"the pill's top-left corner paints {corner}, the same as its left edge: the "
        "corner is square, so border-radius is being dropped again. That is the "
        "signature of the chip having moved back into rich text.")
