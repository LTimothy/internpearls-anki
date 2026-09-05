from internpearls.collection import note_rows, suspend_notes, unsuspend_notes
from internpearls.config import add_dupes_ignored, _cfg


def test_note_rows_by_scope_tag(anki):
    n = anki.col.add_note("g1", ["Front one", "Back one"], ["InternPearls"], deck="Deck")
    anki.col.add_note("g2", ["Other front", "Other back"], ["SomethingElse"], deck="Deck")
    rows = note_rows(anki.col, scope_tag="InternPearls")
    assert len(rows) == 1
    nid, text, deck, note_type = rows[0]
    assert nid == n.id
    assert text == "Front one Back one"
    assert deck == "Deck"
    assert note_type


def test_note_rows_by_deck(anki):
    anki.col.add_note("g1", ["Front one", "Back one"], ["InternPearls"], deck="DeckA")
    anki.col.add_note("g2", ["Front two", "Back two"], ["InternPearls"], deck="DeckB")
    rows = note_rows(anki.col, deck_name="DeckA")
    assert len(rows) == 1
    assert rows[0][2] == "DeckA"


def test_note_rows_no_filter_returns_everything(anki):
    anki.col.add_note("g1", ["Front one", "Back one"], ["InternPearls"], deck="DeckA")
    anki.col.add_note("g2", ["Front two", "Back two"], ["Other"], deck="DeckB")
    rows = note_rows(anki.col)
    assert len(rows) == 2


def test_note_rows_resolves_filtered_deck_to_home_deck(anki):
    n = anki.col.add_note("g1", ["Front one", "Back one"], ["InternPearls"],
                          deck="Ankisthesia")
    anki.col.file_in_filtered_deck(n.id, "Filtered Deck", "Ankisthesia")
    rows = note_rows(anki.col, scope_tag="InternPearls")
    assert rows[0][2] == "Ankisthesia"
    deck_rows = note_rows(anki.col, deck_name="Ankisthesia")
    assert len(deck_rows) == 1


def test_suspend_and_unsuspend_notes_go_through_scheduler(anki):
    n = anki.col.add_note("g1", ["Front one", "Back one"], ["InternPearls"], deck="Deck")
    cid = n.card_ids()[0]
    suspend_notes(anki.col, [n.id])
    assert anki.col.get_card(cid).queue == -1
    unsuspend_notes(anki.col, [n.id])
    assert anki.col.get_card(cid).queue == 0


def test_dupes_ignored_default_empty(anki):
    assert _cfg()["dupes_ignored"] == []


def test_add_dupes_ignored_persists(anki):
    add_dupes_ignored("1:2")
    assert _cfg()["dupes_ignored"] == ["1:2"]
    add_dupes_ignored("3:4")
    assert _cfg()["dupes_ignored"] == ["1:2", "3:4"]
