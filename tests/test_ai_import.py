"""Generated-note import against the mock collection.

Media contract under test (see collection.add_generated_notes' own docstring): a card
dict may carry "_media_files", the filenames the review dialog already resolved bytes
for; `media` supplies those bytes keyed by the same filenames.
"""
import os

import pytest

from internpearls import ai_logic, collection
from mock_anki import make_model

SCOPE = "InternPearls"
DECK = "Intern Pearls::Intern Custom::Generated"


def _card(front="Q1", note_type="Study Deck - Basic", media_files=(), tags=("LAST",)):
    fields = {"Front": front, "Back": "A", "Why": "W", "Image": "",
              "Tag": "", "Dosing": "", "Notes": ""}
    if note_type != "Study Deck - Basic":
        fields = {"Front": front, "Back": "A"}
    return {"note_type": note_type, "fields": fields, "tags": list(tags),
            "images": [], "rationale": "", "_media_files": list(media_files)}


def test_add_generated_notes_writes_iplocal_guids_and_media(anki):
    n = collection.add_generated_notes(
        [_card("Q1"), _card("Q2", media_files=["generated-1.svg"])],
        media={"generated-1.svg": b"<svg xmlns='x'/>"},
        deck_name=DECK, scope_tag=SCOPE)
    assert n == 2
    added = [note for note in anki.col._notes.values()
             if note.guid.startswith(ai_logic.GUID_PREFIX)]
    assert len(added) == 2
    assert all(ai_logic.is_generated_guid(note.guid) for note in added)
    assert all(f"{SCOPE}::Generated" in note.tags for note in added)
    assert all(note.deck == DECK for note in added)
    with_img = next(n for n in added if n["Front"] == "Q2")
    assert with_img["Image"] == '<img src="generated-1.svg">'
    without_img = next(n for n in added if n["Front"] == "Q1")
    assert without_img["Image"] == ""


def test_add_generated_notes_carries_card_tags_plus_scope_tag(anki):
    collection.add_generated_notes(
        [_card("Q1", tags=["Pharm", "LAST"])], media={}, deck_name=DECK, scope_tag=SCOPE)
    note = next(iter(anki.col._notes.values()))
    assert note.tags == ["Pharm", "LAST", f"{SCOPE}::Generated"]


def test_add_generated_notes_empty_list_is_noop(anki):
    assert collection.add_generated_notes([], media={}, deck_name=DECK, scope_tag=SCOPE) == 0
    assert anki.col._notes == {}
    assert DECK not in anki.col.decks.names


def test_add_generated_notes_never_touches_existing_notes(anki):
    her = anki.col.add_note("her-guid", ["Existing front", "b", "", "", "", "", ""],
                            ["InternPearls::Pharm"], deck="Intern Pearls::Intern Custom")
    before_fields, before_tags, before_deck = list(her.fields), list(her.tags), her.deck
    collection.add_generated_notes([_card("New card")], media={},
                                   deck_name=DECK, scope_tag=SCOPE)
    assert her.fields == before_fields
    assert her.tags == before_tags
    assert her.deck == before_deck


def test_add_generated_notes_unknown_note_type_raises_nothing_written(anki):
    good, bad = _card("Q1"), _card("Q2", note_type="Nonexistent Type")
    with pytest.raises(RuntimeError):
        collection.add_generated_notes([good, bad], media={}, deck_name=DECK, scope_tag=SCOPE)
    assert anki.col._notes == {}          # nothing partially written
    assert DECK not in anki.col.decks.names


def test_add_generated_notes_core_type_missing_from_collection_raises(anki):
    """"Basic" is an allowed core type in principle, but this mock collection's models
    list only has "Study Deck - Basic" -- it was never actually synced in."""
    with pytest.raises(RuntimeError):
        collection.add_generated_notes(
            [_card("Q1", note_type="Basic")], media={}, deck_name=DECK, scope_tag=SCOPE)
    assert anki.col._notes == {}


def test_add_generated_notes_is_one_undo_step(anki):
    anki.col.add_note("her-guid", ["Existing front", "b", "", "", "", "", ""],
                      ["InternPearls::Pharm"], deck="Intern Pearls::Intern Custom")
    n = collection.add_generated_notes(
        [_card("Q1"), _card("Q2"), _card("Q3")], media={}, deck_name=DECK, scope_tag=SCOPE)
    assert n == 3
    assert len(anki.col._notes) == 4   # her existing note + 3 generated
    anki.col.undo()
    assert len(anki.col._notes) == 1   # a single undo removed all 3 together
    remaining = next(iter(anki.col._notes.values()))
    assert remaining.guid == "her-guid"


def test_image_appended_to_primary_field_when_no_image_field(anki):
    """A core "Basic" note (Front/Back, no Image field) still needs somewhere to put
    a resolved image -- falls back to the note's first field."""
    anki.col.models._models.append(make_model(name="Basic", fields=["Front", "Back"]))
    card = _card("Q1", note_type="Basic", media_files=["generated-1.svg"])
    collection.add_generated_notes(
        [card], media={"generated-1.svg": b"<svg xmlns='x'/>"},
        deck_name=DECK, scope_tag=SCOPE)
    note = next(iter(anki.col._notes.values()))
    assert note["Front"] == 'Q1<img src="generated-1.svg">'


def test_two_separate_imports_get_independent_undo_steps(anki):
    """A second, later import must not fold into the first import's undo entry --
    undoing the second should leave the first's cards in place."""
    collection.add_generated_notes([_card("Q1")], media={}, deck_name=DECK, scope_tag=SCOPE)
    collection.add_generated_notes([_card("Q2")], media={}, deck_name=DECK, scope_tag=SCOPE)
    assert len(anki.col._notes) == 2
    anki.col.undo()
    assert len(anki.col._notes) == 1
    assert next(iter(anki.col._notes.values()))["Front"] == "Q1"


def test_add_generated_notes_media_write_data_fallback_to_add_file(anki, monkeypatch):
    """Some Anki versions lack col.media.write_data(); the fallback writes a temp file
    and calls add_file() instead. Simulate that by stubbing out write_data entirely."""
    class NoWriteData:
        def __init__(self):
            self.added_paths = []

        def add_file(self, path):
            self.added_paths.append(path)
            return os.path.basename(path)

    stub = NoWriteData()
    monkeypatch.setattr(anki.col, "media", stub)
    n = collection.add_generated_notes(
        [_card("Q1", media_files=["generated-1.svg"])],
        media={"generated-1.svg": b"<svg xmlns='x'/>"},
        deck_name=DECK, scope_tag=SCOPE)
    assert n == 1
    assert len(stub.added_paths) == 1
    note = next(iter(anki.col._notes.values()))
    assert note["Image"] == '<img src="generated-1.svg">'
