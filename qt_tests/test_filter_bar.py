"""The filter and search bar over a long Update my decks list, against real widgets."""
import os
import time

import harness

_FILLER = 24
_TOTAL = len(harness.synthetic_details()) + _FILLER


def _bar(dialog):
    from internpearls import widgets
    return dialog.findChild(widgets.FilterBar)


def _list(dialog):
    from internpearls import widgets
    return dialog.findChild(widgets.StreamingList)


def _pump(seconds=0.4):
    """Run the event loop long enough for the search box's typing delay to elapse."""
    app = harness.app()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def _search(bar, text):
    bar.search.setText(text)
    _pump()


def _pick(bar, mode):
    bar.options.buttons[mode].click()
    harness.app().processEvents()


def _texts(dialog):
    _, q = harness.bootstrap()
    return [l.text() for l in dialog.findChildren(q.QLabel) if l.text().strip()]


def _buttons(dialog, text):
    _, q = harness.bootstrap()
    return [b for b in dialog.findChildren(q.QPushButton) if b.text() == text]


def test_the_bar_appears_only_when_the_list_is_long_enough():
    assert _bar(harness.render("confirm", size=(880, 900)).dialog) is None
    long_shot = harness.render("confirm-long", size=(880, 900))
    bar = _bar(long_shot.dialog)
    assert bar is not None and bar.isVisible()
    assert bar.search.placeholderText() == "Search cards"
    assert not bar._count.isVisible(), "no count line while nothing is filtered"


def test_the_bar_sits_above_the_list_and_outside_its_scroll_area():
    dlg = harness.render("confirm-long", size=(880, 900)).dialog
    bar, lst = _bar(dlg), _list(dlg)
    assert bar.parentWidget() is lst.parentWidget()
    assert harness.left_x(dlg, bar) <= harness.left_x(dlg, lst) + 1
    assert bar.mapTo(dlg, bar.rect().bottomLeft()).y() <= lst.mapTo(
        dlg, lst.rect().topLeft()).y()


def test_a_filter_and_a_search_round_trip_keeps_every_decision():
    dlg = harness.render("confirm-long", size=(880, 900)).dialog
    bar = _bar(dlg)
    _buttons(dlg, "Skip")[0].click()
    assert len([b for b in _buttons(dlg, "Skip") if b.isChecked()]) == 1

    _pick(bar, "held")
    assert bar._count.text() == f"Showing 3 of {_TOTAL} cards"
    assert _list(dlg).total() == 1 + 3 + 2   # heading, rows, hairlines
    _search(bar, "number 13")
    assert bar._count.text() == f"Showing 1 of {_TOTAL} cards"
    _search(bar, "")
    _pick(bar, "all")
    assert not bar._count.isVisible()
    assert len([b for b in _buttons(dlg, "Skip") if b.isChecked()]) == 1, (
        "the rebuilt row must still show the decision made before the filter")


def test_not_reviewed_drops_the_rows_the_learner_has_decided():
    dlg = harness.render("confirm-long", size=(880, 900)).dialog
    bar = _bar(dlg)
    _buttons(dlg, "Skip")[0].click()
    _pick(bar, "unreviewed")
    assert bar._count.text() == f"Showing {_TOTAL - 1} of {_TOTAL} cards"


def test_a_search_matches_case_insensitively():
    dlg = harness.render("confirm-long", size=(880, 900)).dialog
    bar = _bar(dlg)
    _search(bar, "FILLER CARD NUMBER 5?")
    assert bar._count.text() == f"Showing 1 of {_TOTAL} cards"
    assert any("Filler card number 5?" in t for t in _texts(dlg))


def test_a_section_with_nothing_left_loses_its_heading():
    dlg = harness.render("confirm-long", size=(880, 900)).dialog
    bar = _bar(dlg)
    assert any(t == "Long Deck" for t in _texts(dlg))
    _search(bar, "which widget is this")
    assert not any(t == "Long Deck" for t in _texts(dlg))
    assert any(t == "Example Deck" for t in _texts(dlg))


def test_a_folded_group_under_a_search_counts_and_builds_only_matches():
    dlg = harness.render("confirm-long", group_size=8, group_kind="changed",
                         size=(880, 900)).dialog
    bar = _bar(dlg)
    assert _buttons(dlg, "Show 8 cards")
    _search(bar, "member card number 3")
    assert bar._count.text() == f"Showing 1 of {8 + _FILLER} cards"
    toggle = _buttons(dlg, "Show 1 card")
    assert len(toggle) == 1 and not _buttons(dlg, "Show 8 cards")
    toggle[0].click()
    harness.app().processEvents()
    shown = _texts(dlg)
    assert any("Group member card number 3?" in t for t in shown)
    assert not any("Group member card number 2?" in t for t in shown)
    _search(bar, "")
    assert _buttons(dlg, "Show 8 cards")


def test_a_group_decision_still_lands_after_the_list_was_rebuilt():
    dlg = harness.render("confirm-long", group_size=8, group_kind="changed",
                         size=(880, 900)).dialog
    bar = _bar(dlg)
    _pick(bar, "held")
    _pick(bar, "all")
    _buttons(dlg, "Keep all yours")[0].click()
    _pick(bar, "changed")
    assert _buttons(dlg, "Keep all yours")[0].isChecked()


def test_escape_clears_the_search_and_passes_through_when_it_is_empty():
    from PyQt6.QtCore import QEvent, Qt
    from PyQt6.QtGui import QKeyEvent
    dlg = harness.render("confirm-long", size=(880, 900)).dialog
    bar = _bar(dlg)
    bar.search.setText("abc")
    press = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
    bar.search.keyPressEvent(press)
    assert bar.search.text() == "" and press.isAccepted()
    again = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
    bar.search.keyPressEvent(again)
    assert not again.isAccepted(), "an empty field must leave Escape to the dialog"


def test_the_search_field_is_the_next_tab_stop_after_the_options():
    dlg = harness.render("confirm-long", size=(880, 900)).dialog
    bar = _bar(dlg)
    last = bar.options.buttons["unreviewed"]
    last.setFocus()
    assert dlg.focusWidget() is last
    dlg.focusNextChild()
    assert dlg.focusWidget() is bar.search


def test_the_bar_fits_the_dialogs_narrowest_width():
    dlg = harness.render("confirm-long", size=(660, 900)).dialog
    bar = _bar(dlg)
    assert bar.search.width() >= 120, f"the search field is {bar.search.width()}px wide"
    assert (bar.mapTo(dlg, bar.rect().bottomRight()).x() <= dlg.width())


def test_filter_bar_render_saved_as_png(tmp_path):
    out_dir = os.environ.get("IP_SHOT_DIR") or str(tmp_path)
    os.makedirs(out_dir, exist_ok=True)
    saved = []
    for theme in ("light", "dark"):
        shot = harness.render("confirm-long", theme=theme, size=(880, 900))
        bar = _bar(shot.dialog)
        _pick(bar, "changed")
        _search(bar, "card")
        png = os.path.join(out_dir, f"update-filter-bar-{theme}.png")
        shot.dialog.grab().toImage().save(png, "PNG")
        saved.append(png)
    for png in saved:
        assert os.path.getsize(png) > 0


def test_enter_in_the_search_field_applies_the_search_and_leaves_the_dialog_open():
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    dlg = harness.render("confirm-long", size=(880, 900)).dialog
    bar = _bar(dlg)
    bar.search.setText("number 13")
    bar.search.setFocus()

    for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
        QTest.keyClick(bar.search, key)
        harness.app().processEvents()
        assert dlg.isVisible() and dlg.result() == 0
    assert bar._count.text() == f"Showing 1 of {_TOTAL} cards"
