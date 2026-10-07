"""Search and decision filters for saved card decisions."""
import pytest

from test_dialogs import _all_text, _snapshot_declined_dialog, _walk_widgets


@pytest.fixture
def declined_dialog(anki):
    from internpearls import config, dialogs
    config.save_declined({
        "later": {"state": "held", "front": "Alpha prompt", "deck": "IP::Medicine",
                  "note": "Check <b>DOSE</b>", "decided": "", "hash": ""},
        "legacy": {"state": "skip", "front": "Beta prompt", "deck": "IP::Surgery",
                   "decided": "", "hash": ""},
        "never": {"state": "never", "front": "Gamma prompt", "deck": "IP::Surgery",
                  "decided": "", "hash": ""},
        "keep": {"state": "keep", "front": "Delta prompt", "deck": "IP::Medicine",
                 "decided": "", "hash": ""},
        "frozen": {"state": "frozen", "front": "Epsilon prompt", "deck": "IP::Other",
                   "decided": "", "hash": ""},
        "unknown": {"state": "future", "front": "Zeta prompt", "deck": "IP::Other"},
        "garbage": "not a dict"})
    return dialogs._DeclinedDialog(None)


def _bar(dlg):
    from internpearls.widgets import FilterBar
    bars = [w for w in _walk_widgets(dlg) if isinstance(w, FilterBar)]
    assert len(bars) == 1, "the dialog needs the shared filter bar"
    return bars[0]


def _guids(dlg):
    return [item[1] for item in dlg._list._items if item[0] == "row"]


def _search(bar, query):
    bar.search.setText(query)
    bar.search.returnPressed.emit()


def test_declined_heading_and_offer_again_help(anki):
    texts = _all_text(_snapshot_declined_dialog(anki)).splitlines()
    assert "Later and declined cards" in texts
    assert ("Offer again forgets the decision, so the card comes back on your next "
            "Update my decks.") in texts


@pytest.mark.parametrize("query, expected", [
    ("ALPHA", ["later"]),
    ("ip::MEDICINE", ["later", "keep"]),
    ("dose", ["later"]),
    ("not found", []),
    ("   ", ["later", "legacy", "never", "frozen", "keep", "unknown", "garbage"])])
def test_declined_search_matches_front_deck_and_saved_note(declined_dialog, query, expected):
    bar = _bar(declined_dialog)
    _search(bar, query)
    assert _guids(declined_dialog) == expected
    assert bar._count.text() == (f"Showing {len(expected)} of 7 cards" if query.strip() else "")
    if not expected:
        assert not any(i[0] == "heading" for i in declined_dialog._list._items)


@pytest.mark.parametrize("mode, expected, heading", [
    ("held", ["later", "legacy"], "Later"),
    ("never", ["never"], "Never imported"),
    ("keep", ["keep"], "Kept yours"),
    ("frozen", ["frozen"], "Kept yours, no more updates"),
    ("other", ["unknown", "garbage"], "Other")])
def test_declined_decision_filter_keeps_only_its_group(declined_dialog, mode, expected, heading):
    bar = _bar(declined_dialog)
    bar.options.buttons[mode].click()
    assert _guids(declined_dialog) == expected
    assert [i[1] for i in declined_dialog._list._items if i[0] == "heading"] == [heading]
    assert bar._count.text() == f"Showing {len(expected)} of 7 cards"
    bar.options.buttons["all"].click()
    assert len(_guids(declined_dialog)) == 7
    assert not bar._count.isVisible()


def test_declined_filter_options_name_the_decisions(declined_dialog):
    bar = _bar(declined_dialog)
    assert [b.text() for b in bar.options.buttons.values()] == [
        "All", "Later", "Never", "Kept yours", "Kept yours with no more updates", "Other"]
    assert all(b.accessibleName() == f"Show {b.text().lower()} cards"
               for b in bar.options.buttons.values())


def test_other_filter_is_absent_without_other_entries(anki):
    from internpearls import config, dialogs
    config.save_declined({"a": {"state": "held", "front": "Alpha"}})
    assert "other" not in _bar(dialogs._DeclinedDialog(None)).options.buttons


def test_later_saved_note_is_under_the_front_as_plain_text(declined_dialog):
    from aqt.qt import QLabel, Qt
    row = declined_dialog._row("later", {"state": "held", "front": "Alpha prompt",
                                           "note": "Check <b>DOSE</b>"})
    labels = [w for w in _walk_widgets(row) if isinstance(w, QLabel)]
    assert [w.text() for w in labels][:2] == ["Alpha prompt", "Your note: Check <b>DOSE</b>"]
    assert labels[1]._format == Qt.TextFormat.PlainText


def test_offer_again_preserves_both_filters_and_updates_the_count(declined_dialog):
    from internpearls import config
    bar = _bar(declined_dialog)
    bar.options.buttons["held"].click()
    _search(bar, "prompt")
    declined_dialog._offer_again("later")
    assert "later" not in config.load_declined()
    assert _guids(declined_dialog) == ["legacy"]
    assert bar._count.text() == "Showing 1 of 6 cards"
    assert bar.options.buttons["held"].isChecked()
    assert bar.search.text() == "prompt"
    declined_dialog._offer_again("legacy")
    assert _guids(declined_dialog) == []
    assert bar._count.text() == "Showing 0 of 5 cards"
    assert not any(i[0] == "heading" for i in declined_dialog._list._items)
