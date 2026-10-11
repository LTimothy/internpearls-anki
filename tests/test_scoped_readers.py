"""The scoped-note readers in collection.py, pinned on a collection with subdecks, a
note whose cards sit in two decks, a note with no cards, a subtag and an exclusion."""
import types

import mock_anki
from internpearls import collection

SCOPE = "InternPearls"
PARENT = "Intern Pearls::Pharm"


def _second_card(anki, note, deck):
    first = anki.col._cards[note._card_ids[0]]
    cid = anki.col._next_cid
    anki.col._next_cid += 1
    anki.col._cards[cid] = types.SimpleNamespace(**{
        **vars(first), "id": cid, "ord": 1, "did": anki.col.decks.id(deck), "reps": 4})
    note._card_ids.append(cid)


def _world(anki):
    cloze = mock_anki.make_model("Study Deck - Cloze", fields=["Text", "Extra"])
    anki.col.models._models.append(cloze)
    add = anki.col.add_note
    add("g1", ["Front one", "Back one"], [SCOPE], deck=PARENT + "::Sub A")
    add("g2", ["{{c1::Text}}", "Extra two"], [SCOPE + "::Child"], deck=PARENT + "::Sub B",
        model=cloze)
    split = add("g3", ["Split front", "Back three"], [SCOPE], deck=PARENT + "::Sub A")
    _second_card(anki, split, "Elsewhere")
    bare = add("g4", ["No cards", "Back four"], [SCOPE], deck=PARENT)
    anki.col._cards.pop(bare._card_ids.pop())
    add("g5", ["Out of scope", "Back five"], ["Other"], deck=PARENT)
    add("g6", ["Same front", "a"], [SCOPE], deck="Top")
    add("g7", ["Same front", "b"], [SCOPE, "Archived"], deck="Top")
    held = add("g8", ["Filtered", "Back eight"], [SCOPE], deck=PARENT + "::Sub B")
    anki.col.file_in_filtered_deck(held.id, "Study Session", PARENT + "::Sub B")
    anki.col._cards[held._card_ids[0]].reps = 2
    return {n.guid: n.id for n in anki.col._notes.values()}


def test_scoped_readers_return_what_they_always_have(anki):
    nid = _world(anki)
    in_scope = ["g1", "g2", "g3", "g4", "g6", "g7", "g8"]

    fronts = collection._existing_front_to_guid(SCOPE)
    assert dict(fronts) == {"Front one": "g1", "{{c1::Text}}": "g2", "Split front": "g3",
                            "No cards": "g4", "Filtered": "g8"}
    assert fronts.guids == set(in_scope)

    fields = collection._existing_guid_to_fields(SCOPE)
    assert list(fields) == in_scope
    assert fields["g1"]["Front"] == "Front one" and fields["g1"]["Notes"] == ""
    assert fields["g2"] == {"Text": "{{c1::Text}}", "Extra": "Extra two"}

    assert collection._existing_guid_to_nid(SCOPE) == {g: nid[g] for g in in_scope}

    assert collection._existing_guid_to_deck(SCOPE) == {
        "g1": PARENT + "::Sub A", "g2": PARENT + "::Sub B", "g3": PARENT + "::Sub A",
        "g6": "Top", "g7": "Top", "g8": PARENT + "::Sub B"}

    summary = collection._existing_notes_summary(SCOPE, exclude_tag="Archived")
    assert [s["guid"] for s in summary] == ["g1", "g2", "g3", "g6", "g8"]
    by_guid = {s["guid"]: s for s in summary}
    assert by_guid["g3"] == {"guid": "g3", "nid": nid["g3"], "model": "Study Deck - Basic",
                             "front": "Split front", "label": "Split front", "reps": 4,
                             "deck": PARENT + "::Sub A"}
    assert by_guid["g2"]["model"] == "Study Deck - Cloze"
    assert by_guid["g8"]["reps"] == 2 and by_guid["g8"]["deck"] == PARENT + "::Sub B"
    assert len(collection._existing_notes_summary(SCOPE)) == 6

    assert collection.installed_matching_collection(
        {PARENT: "v1", "Top": "v2", "Elsewhere": "v3", "Gone": "v4"}, SCOPE) == {
        PARENT: "v1", "Top": "v2"}


def test_scoped_readers_make_no_per_note_backend_calls(anki, monkeypatch):
    _world(anki)
    calls = []
    for name in ("get_note", "get_card"):
        real = getattr(anki.col, name)
        monkeypatch.setattr(anki.col, name,
                            lambda i, real=real, name=name: calls.append(name) or real(i))

    collection._existing_guid_to_deck(SCOPE)
    collection._existing_front_to_guid(SCOPE)
    collection._existing_guid_to_fields(SCOPE)
    collection._existing_guid_to_nid(SCOPE)
    collection._existing_notes_summary(SCOPE, exclude_tag="Archived")

    assert calls == []


def test_a_scope_tag_with_an_underscore_matches_only_itself_and_its_children(anki):
    add = anki.col.add_note
    m1 = add("m1", ["Mine", "a"], ["My_Deck"]).id
    m2 = add("m2", ["Child", "b"], ["My_Deck::Sub"]).id
    m3 = add("m3", ["Kept", "c"], ["My_Deck", "MyXDeck::Sub"]).id
    lookalike = add("x1", ["Lookalike", "d"], ["MyXDeck"]).id
    # The mock reads `_` as Anki does, or this test could not fail.
    assert lookalike in anki.col.find_notes('"tag:My_Deck"')

    assert {row[0] for row in collection._scoped_notes("My_Deck")} == {m1, m2, m3}
    assert {row[0] for row in collection._scoped_notes("My_Deck", "My_Deck::Sub")} == {
        m1, m3}
