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


def test_version_at_least_strips_v_prefix_on_latest():
    # version.json / git tags may carry a "v"; the comparator must ignore it.
    assert logic.version_at_least("0.12.0", "v0.12.0") is True
    assert logic.version_at_least("0.12.0", "v0.13.0") is False


def test_version_tuple_empty_string_is_empty_tuple():
    # A malformed/blank version string must not raise; it parses to ().
    assert logic.version_tuple("") == ()


# --------------------------------------------------------------------- decks_to_update
def _manifest(*pairs):
    return {"decks": [{"name": n, "version": v} for n, v in pairs]}


def test_decks_to_update_flags_new_and_changed_skips_unchanged():
    manifest = _manifest(("A", "v1"), ("B", "v2"), ("C", "v3"))
    installed = {"A": "v1", "B": "v_old"}   # A unchanged, B changed, C never seen
    todo = [d["name"] for d in logic.decks_to_update(manifest, installed)]
    assert todo == ["B", "C"]


def test_decks_to_update_empty_when_all_current():
    manifest = _manifest(("A", "v1"), ("B", "v2"))
    assert logic.decks_to_update(manifest, {"A": "v1", "B": "v2"}) == []


def test_decks_to_update_all_new_on_empty_installed():
    manifest = _manifest(("A", "v1"), ("B", "v2"))
    assert len(logic.decks_to_update(manifest, {})) == 2


def test_decks_to_update_tolerates_missing_manifest():
    # A None/empty manifest (e.g. an unconfigured source) must not raise.
    assert logic.decks_to_update(None, {}) == []
    assert logic.decks_to_update({}, {"A": "v1"}) == []


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


def test_remap_cards_guid_already_matches_needs_no_rewrite(tmp_path):
    # If the incoming note already carries the learner's GUID (e.g. re-syncing an
    # unchanged deck), it counts as in-place but must NOT be added to remap — rewriting
    # it to itself is pointless churn.
    apkg = str(tmp_path / "deck.apkg")
    _make_fake_apkg(apkg, [(1, "shared-guid", "Same front")])
    her = {"Same front": "shared-guid"}
    remap, in_place, as_new = logic.remap_cards(apkg, her, aliases={})
    assert (remap, in_place, as_new) == ({}, 1, 0)


def test_remap_cards_alias_target_also_missing_is_new(tmp_path):
    # An alias records a rename, but if the learner's collection has NEITHER the new
    # wording nor the old one the alias points to, the card is genuinely new to her.
    apkg = str(tmp_path / "deck.apkg")
    _make_fake_apkg(apkg, [(1, "g1", "New wording")])
    aliases = {"New wording": "Old wording"}   # but "Old wording" isn't in her map
    remap, in_place, as_new = logic.remap_cards(apkg, her={}, aliases=aliases)
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


def test_write_personalized_preserves_media_and_manifest(tmp_path):
    # A real .apkg carries a "media" manifest and numbered media blobs alongside
    # collection.anki2. write_personalized repackages the whole zip, so those must
    # survive untouched — otherwise images silently vanish from synced cards.
    src = str(tmp_path / "src.apkg")
    out = str(tmp_path / "out.apkg")
    _make_fake_apkg(src, [(1, "g1", "Front 1")])
    with zipfile.ZipFile(src, "a") as z:      # add media the way a genanki package would
        z.writestr("media", '{"0": "femoral.jpg"}')
        z.writestr("0", b"\xff\xd8\xff-fake-jpeg-bytes")
    logic.write_personalized(src, {1: "new-guid"}, out)
    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())
        assert {"collection.anki2", "media", "0"} <= names
        assert z.read("media") == b'{"0": "femoral.jpg"}'
        assert z.read("0") == b"\xff\xd8\xff-fake-jpeg-bytes"
    # and the GUID rewrite still took effect
    assert logic.apkg_notes(out)[0][2] == "new-guid"


def test_apkg_notes_extracts_first_field_from_many(tmp_path):
    # flds packs every field joined by the separator; the "front" this code keys on is
    # always the first one, regardless of how many fields the note type has.
    apkg = str(tmp_path / "deck.apkg")
    db = apkg + ".tmp.db"
    con = sqlite3.connect(db)
    con.execute("create table notes (id integer primary key, guid text, flds text)")
    flds = logic.FS.join(["The Front", "the back", "why text", "", "Tag", "dose", "notes"])
    con.execute("insert into notes values (1, 'g1', ?)", (flds,))
    con.commit()
    con.close()
    with zipfile.ZipFile(apkg, "w") as z:
        z.write(db, "collection.anki2")
    os.remove(db)
    assert logic.apkg_notes(apkg) == [(1, "The Front", "g1")]
