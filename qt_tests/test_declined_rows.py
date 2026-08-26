"""A re-offered decline is still a row about a card, at the dialog's own 660px floor.

The update screen's card row carries a caret column, a chip column and, at its right,
a decision control. A re-offered decline used to add a second chip column (SKIPPED /
KEPT YOURS) and, for one changed since that decline, a third (UPDATED), each a
fixed-width column measured off the widest chip word: on the 660px minimum that left
the card's own front about 150px, on exactly the rows the "Worth another look" hint
asks the reader to look at. These measure the two things that fixed it, both as
relationships rather than magnitudes (see sampling.py): the front of a declined row is
as wide as the front of an ordinary row beside it, and the body a row expands lines up
with that row's own text rather than with a column in front of it.
"""
import harness
from sampling import widget_rect

# Row 0 in the declined fixture: a new card, skipped before and changed since.
DECLINED = "one short line"
# Row 2: an ordinary new card, same three-option control, no decline.
PLAIN = "untagged row"


def _visible_labels(dialog, q):
    return [w for w in dialog.findChildren(q.QLabel) if w.isVisible()]


def _primary(dialog, q, marker):
    """The one visible row label whose text carries `marker`."""
    found = [l for l in _visible_labels(dialog, q) if marker in l.text()]
    assert len(found) == 1, f"expected one label containing {marker!r}, got {len(found)}"
    return found[0]


def _chip_width(dialog, q):
    """A rendered pill's own width, which is this screen's chip column."""
    from internpearls import widgets
    labels = [l for l in _visible_labels(dialog, q)
              if l.text() in set(widgets.CHIPS.values())]
    assert labels, "no chip painted on this screen, so there is nothing to measure"
    return labels[0].width()


def test_a_re_offered_row_gives_its_card_the_same_width_as_any_other_row():
    """The defect, measured where it bit: at the confirmation's own minimum width, with
    the same control beside both rows, a declined row's front must be as wide as an
    undeclined one's. Compared against this screen's own chip column rather than a
    pixel count: a selected button carries bold text and so runs a pixel or two wider
    than an unselected one, while the defect was two whole columns.
    """
    _, q = harness.bootstrap()
    s = harness.render("confirm", declined=True, size=(660, 900))
    declined = widget_rect(s.dialog, _primary(s.dialog, q, DECLINED)).width()
    plain = widget_rect(s.dialog, _primary(s.dialog, q, PLAIN)).width()
    assert abs(declined - plain) < _chip_width(s.dialog, q), (
        f"a re-offered decline's front is {declined}px against an ordinary row's "
        f"{plain}px on the same screen: it is carrying a column the other row is not")


def test_a_re_offered_row_starts_its_text_where_every_other_row_does():
    _, q = harness.bootstrap()
    s = harness.render("confirm", declined=True, size=(660, 900))
    lefts = {marker: widget_rect(s.dialog, _primary(s.dialog, q, marker)).left()
             for marker in (DECLINED, PLAIN)}
    assert len(set(lefts.values())) == 1, f"rows start at different x: {lefts}"


def test_the_changed_since_hint_sits_in_the_rows_own_text_column():
    """It used to be added straight to the row's outer layout, so it painted at x=0,
    left of even the caret, while being a sentence about the card whose text starts
    two columns in."""
    _, q = harness.bootstrap()
    s = harness.render("confirm", declined=True, size=(660, 900))
    hint = _primary(s.dialog, q, "Worth another look")
    assert (widget_rect(s.dialog, hint).left()
            == widget_rect(s.dialog, _primary(s.dialog, q, DECLINED)).left()), (
        "the changed-since hint does not line up with its own row's text")


def test_an_expanded_declined_row_lines_its_body_up_with_its_own_text():
    """The row-indent constraint, on the row that broke it: the body indents by the
    columns the row actually draws, so a chip added in front of the label can never
    leave the body hanging left of the line it belongs to."""
    _, q = harness.bootstrap()
    s = harness.render("confirm", declined=True, expand=(0,), size=(660, 900))
    body_line = _primary(s.dialog, q, "A basic note with a tag.")
    assert (widget_rect(s.dialog, body_line).left()
            == widget_rect(s.dialog, _primary(s.dialog, q, DECLINED)).left()), (
        "an expanded body no longer starts under its own row's text")
