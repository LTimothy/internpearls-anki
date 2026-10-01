"""The hold-for-later button and the LATER chip, on the real Update my decks screen."""
import harness


def _hold_button(dialog, q):
    found = [b for b in dialog.findChildren(q.QPushButton)
             if b.text().startswith("Update, and leave")]
    assert len(found) == 1, [b.text() for b in dialog.findChildren(q.QPushButton)]
    return found[0]


def test_the_hold_button_counts_every_row_nobody_has_opened():
    _, q = harness.bootstrap()
    s = harness.render("confirm", hold=True, size=(880, 900))
    button = _hold_button(s.dialog, q)
    assert button.text() == "Update, and leave 3 unopened for later"
    assert button.isVisible()
    assert "next time you run Update my decks, set to Import" in button.toolTip()


def test_opening_a_row_takes_it_out_of_the_hold_count():
    _, q = harness.bootstrap()
    s = harness.render("confirm", hold=True, expand=(0,), size=(880, 900))
    assert _hold_button(s.dialog, q).text() == "Update, and leave 2 unopened for later"


def test_deciding_on_a_row_takes_it_out_of_the_hold_count():
    _, q = harness.bootstrap()
    s = harness.render("confirm", hold=True, click_labels=("Later",), size=(880, 900))
    assert _hold_button(s.dialog, q).text() == "Update, and leave 2 unopened for later"


def test_a_held_row_paints_the_later_chip():
    from internpearls import widgets
    _, q = harness.bootstrap()
    s = harness.render("confirm", held=True, size=(880, 900))
    chips = [l.text() for l in s.dialog.findChildren(q.QLabel)
             if l.isVisible() and l.text() in set(widgets.CHIPS.values())]
    assert "LATER" in chips and "HELD" not in chips
