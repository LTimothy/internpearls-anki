"""The hold-for-later button and the HELD chip, on the real Update my decks screen."""
import harness


def _hold_button(dialog, q):
    found = [b for b in dialog.findChildren(q.QPushButton)
             if b.text().startswith("Update reviewed, hold")]
    assert len(found) == 1, [b.text() for b in dialog.findChildren(q.QPushButton)]
    return found[0]


def test_the_hold_button_counts_every_row_nobody_has_opened():
    _, q = harness.bootstrap()
    s = harness.render("confirm", hold=True, size=(880, 900))
    button = _hold_button(s.dialog, q)
    assert button.text() == "Update reviewed, hold 3 for later"
    assert button.isVisible() and button.toolTip()


def test_opening_a_row_takes_it_out_of_the_hold_count():
    _, q = harness.bootstrap()
    s = harness.render("confirm", hold=True, expand=(0,), size=(880, 900))
    assert _hold_button(s.dialog, q).text() == "Update reviewed, hold 2 for later"


def test_deciding_on_a_row_takes_it_out_of_the_hold_count():
    _, q = harness.bootstrap()
    s = harness.render("confirm", hold=True, click_labels=("Skip",), size=(880, 900))
    assert _hold_button(s.dialog, q).text() == "Update reviewed, hold 2 for later"


def test_a_held_row_paints_the_held_chip():
    from internpearls import widgets
    _, q = harness.bootstrap()
    s = harness.render("confirm", held=True, size=(880, 900))
    chips = [l.text() for l in s.dialog.findChildren(q.QLabel)
             if l.isVisible() and l.text() in set(widgets.CHIPS.values())]
    assert "HELD" in chips
