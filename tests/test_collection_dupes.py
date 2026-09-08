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


def test_note_rows_sql_path_matches_the_per_note_path(anki):
    """The one-query loader and the per-note loader read the same tuples, so a
    collection whose db lacks `all` (the fallback) and a real one agree."""
    from internpearls import collection
    col = anki.mw.col
    col.add_note("g1", ["Front one", "Back one"], ["InternPearls"], deck="Intern Custom")
    col.add_note("g2", ["Front two", "Back two"], ["Other"], deck="Ankisthesia")
    fast = collection.note_rows(col)
    assert len(fast) == 2 and fast[0][1] == "Front one Back one"
    nids = {r[0] for r in fast}
    slow = []
    for nid in sorted(nids):
        note = col.get_note(nid)
        slow.append((nid, " ".join(note.fields[:2]),
                     collection._home_deck_name(col, note), note.note_type()["name"]))
    assert fast == slow
    assert collection.note_rows(col, scope_tag="InternPearls") == [fast[0]]
    assert collection.note_rows(col, deck_name="Ankisthesia") == [fast[1]]


def test_deck_search_escapes_anki_search_syntax():
    """`_` and `*` are wildcards inside a `deck:` term and a deck name may legally
    contain a quote, so all three (and the backslash that escapes them) are escaped."""
    from internpearls.collection import deck_search
    assert deck_search("Step_2 Pearls") == r'deck:"Step\_2 Pearls"'
    assert deck_search("Cards *starred*") == r'deck:"Cards \*starred\*"'
    assert deck_search('The "good" deck') == r'deck:"The \"good\" deck"'
    assert deck_search("back\\slash") == r'deck:"back\\slash"'
    assert deck_search("Intern Pearls::Intern Custom") == 'deck:"Intern Pearls::Intern Custom"'


def test_note_rows_by_deck_does_not_wildcard_an_underscore(anki):
    """An unescaped `Step_2 Pearls` also selects `Step 2 Pearls`, which would put
    cards from a deck the learner never chose into a duplicate-scan pool."""
    anki.col.add_note("g1", ["Front one", "Back one"], ["InternPearls"],
                      deck="Step_2 Pearls")
    anki.col.add_note("g2", ["Front two", "Back two"], ["InternPearls"],
                      deck="Step 2 Pearls")
    rows = note_rows(anki.col, deck_name="Step_2 Pearls")
    assert [r[2] for r in rows] == ["Step_2 Pearls"]
