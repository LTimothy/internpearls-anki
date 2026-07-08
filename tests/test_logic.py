"""Tests for internpearls/logic.py.

Pure Python, no Anki/aqt install required: run with `pytest` from the addon/ directory.
These build a minimal fake .apkg (a zip with just a "notes" table) since that's all the
logic under test ever reads or writes; the many other tables a real Anki collection
has are irrelevant to this code.
"""
import os
import sqlite3
import sys
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "internpearls"))
import logic  # noqa: E402


def _make_fake_apkg(path, notes):
    """notes: list of (id, guid, front) tuples. Writes a zip with collection.anki2."""
    db_path = path + ".tmp.db"
    if os.path.exists(db_path):
        os.remove(db_path)
    con = sqlite3.connect(db_path)
    con.execute("create table notes (id integer primary key, guid text, flds text)")
    for nid, guid, front in notes:
        flds = front + logic.FS + "back text"
        con.execute("insert into notes (id, guid, flds) values (?, ?, ?)",
                    (nid, guid, flds))
    con.commit()
    con.close()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(db_path, "collection.anki2")
    os.remove(db_path)


# --------------------------------------------------------------------------- bullets
def test_bullets_wraps_each_item_in_li():
    html = logic.bullets(["a", "b"])
    assert html == "<ul style='margin:4px 0 4px 0;'><li>a</li><li>b</li></ul>"


def test_bullets_empty_list():
    assert logic.bullets([]) == "<ul style='margin:4px 0 4px 0;'></ul>"


# ---------------------------------------------------------------- version comparison
def test_version_tuple_parses_dotted_integers():
    assert logic.version_tuple("0.10.2") == (0, 10, 2)


def test_version_tuple_ignores_non_digit_prefix():
    assert logic.version_tuple("v0.5") == (0, 5)


def test_version_at_least_treats_short_form_as_equal():
    # 0.5 and 0.5.0 must compare equal, not "0.5 is older because it's shorter"
    assert logic.version_at_least("0.5.0", "0.5") is True
    assert logic.version_at_least("0.5", "0.5.0") is True


def test_version_at_least_numeric_not_lexicographic():
    # a naive string/lexicographic compare would say "0.9.0" > "0.10.0"
    assert logic.version_at_least("0.9.0", "0.10.0") is False
    assert logic.version_at_least("0.10.0", "0.9.0") is True


def test_version_at_least_strictly_older_current():
    assert logic.version_at_least("0.10.1", "0.10.2") is False


# --------------------------------------------------------------------- apkg_notes
def test_apkg_notes_reads_id_front_guid(tmp_path):
    apkg = str(tmp_path / "deck.apkg")
    _make_fake_apkg(apkg, [(1, "guid-a", "Front A"), (2, "guid-b", "Front B")])
    rows = logic.apkg_notes(apkg)
    assert sorted(rows) == [(1, "Front A", "guid-a"), (2, "Front B", "guid-b")]


def test_apkg_notes_rejects_non_apkg_zip(tmp_path):
    bogus = str(tmp_path / "bogus.apkg")
    with zipfile.ZipFile(bogus, "w") as z:
        z.writestr("not_a_collection.txt", "nope")
    try:
        logic.apkg_notes(bogus)
        assert False, "expected RuntimeError for a zip with no collection.anki2"
    except RuntimeError:
        pass


# ----------------------------------------------------------------------- remap_cards
def test_remap_cards_end_to_end(tmp_path):
    apkg = str(tmp_path / "deck.apkg")
    _make_fake_apkg(apkg, [
        (1, "apkg-guid-matched", "Matches directly"),
        (2, "apkg-guid-aliased", "New wording"),
        (3, "apkg-guid-new", "Never seen before"),
    ])
    her = {
        "Matches directly": "her-guid-direct",   # front unchanged since her last sync
        "Old wording": "her-guid-aliased",       # her card still has the old front
    }
    aliases = {"New wording": "Old wording"}     # records that rename

    remap, in_place, as_new = logic.remap_cards(apkg, her, aliases)

    assert in_place == 2          # "Matches directly" and "New wording" (via alias)
    assert as_new == 1            # "Never seen before" has no match anywhere
    # GUIDs get rewritten to match her existing cards so Anki's importer updates in
    # place instead of creating duplicates:
    assert remap == {1: "her-guid-direct", 2: "her-guid-aliased"}


def test_remap_cards_no_matches_are_all_new(tmp_path):
    apkg = str(tmp_path / "deck.apkg")
    _make_fake_apkg(apkg, [(1, "g1", "Nobody has this")])
    remap, in_place, as_new = logic.remap_cards(apkg, her={}, aliases={})
    assert (remap, in_place, as_new) == ({}, 0, 1)


# ------------------------------------------------------------------ write_personalized
def test_write_personalized_rewrites_only_remapped_guids(tmp_path):
    src = str(tmp_path / "src.apkg")
    out = str(tmp_path / "out.apkg")
    _make_fake_apkg(src, [
        (1, "original-guid-1", "Front 1"),
        (2, "original-guid-2", "Front 2"),
    ])
    logic.write_personalized(src, {1: "rewritten-guid"}, out)
    rows = {rid: (front, guid) for rid, front, guid in logic.apkg_notes(out)}
    assert rows[1] == ("Front 1", "rewritten-guid")   # remapped
    assert rows[2] == ("Front 2", "original-guid-2")  # untouched
