"""Tests for internpearls/logic.py.

Pure Python, no Anki/aqt install required: run with `pytest` from the addon/ directory.
These build a minimal mock .apkg (a zip with just a "notes" table) since that's all the
logic under test ever reads or writes; the many other tables a real Anki collection
has are irrelevant to this code.
"""
import json
import os
import sqlite3
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "internpearls"))
import logic  # noqa: E402


def _make_mock_apkg(path, notes, models=None):
    """notes: list of (id, guid, front) tuples, where `front` may instead be a list of
    field values to control every field, and an optional 4th element pins the note's
    note type id (default 0). Writes a zip with collection.anki2. `models`, if given,
    is the col.models JSON value (a {model_id: model_dict} map, the legacy format
    genanki writes) so apkg_models/apkg_note_details have something to read."""
    db_path = path + ".tmp.db"
    if os.path.exists(db_path):
        os.remove(db_path)
    con = sqlite3.connect(db_path)
    con.execute("create table notes (id integer primary key, guid text, mid integer, "
                "flds text)")
    for note in notes:
        nid, guid, front = note[0], note[1], note[2]
        mid = note[3] if len(note) > 3 else 0
        flds = (logic.FS.join(front) if isinstance(front, (list, tuple))
                else front + logic.FS + "back text")
        con.execute("insert into notes (id, guid, mid, flds) values (?, ?, ?, ?)",
                    (nid, guid, mid, flds))
    if models is not None:
        import json
        con.execute("create table col (models text)")
        con.execute("insert into col (models) values (?)", (json.dumps(models),))
    con.commit()
    con.close()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(db_path, "collection.anki2")
    os.remove(db_path)


def test_plural_keeps_the_bare_noun_for_one():
    assert logic.plural(1, "card") == "1 card"
    assert logic.plural(1, "retired card") == "1 retired card"


def test_plural_adds_the_s_for_several():
    assert logic.plural(3, "card") == "3 cards"
    assert logic.plural(12, "deck") == "12 decks"


def test_plural_treats_zero_as_plural():
    """The case a naive "s if count > 1" gets wrong: English says "0 cards", and
    "Preserved fields restored on 0 card" is exactly the line that would read it."""
    assert logic.plural(0, "card") == "0 cards"


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


# ------------------------------------------------------------- manifest_needs_newer_addon
def test_manifest_schema_within_supported_is_fine():
    assert logic.manifest_needs_newer_addon({"schema": 2}, supported_schema=2) is False
    assert logic.manifest_needs_newer_addon({"schema": 1}, supported_schema=2) is False


def test_manifest_schema_newer_than_supported_is_blocked():
    assert logic.manifest_needs_newer_addon({"schema": 3}, supported_schema=2) is True


def test_manifest_missing_schema_defaults_to_1_never_blocked():
    # Manifests written before the `schema` field existed are always readable.
    assert logic.manifest_needs_newer_addon({}, supported_schema=2) is False


def test_manifest_needs_newer_addon_handles_falsy_manifest():
    assert logic.manifest_needs_newer_addon(None, supported_schema=2) is False
    assert logic.manifest_needs_newer_addon({}, supported_schema=0) is False


def test_manifest_schema_that_is_not_an_int_gates_instead_of_raising():
    # A quoted or otherwise malformed schema used to raise TypeError out of the ">"
    # comparison, aborting the sync with a stack trace instead of the purpose-built
    # "update the add-on" message this function exists to trigger.
    for bad in ("3", "1", 2.0, None, [], {"a": 1}, True):
        assert logic.manifest_needs_newer_addon(
            {"schema": bad}, supported_schema=2) is True


# ------------------------------------------------------------------ should_notify_update
def test_should_notify_when_newer_and_never_notified():
    assert logic.should_notify_update("0.14.1", "0.15.0", None) is True


def test_should_not_notify_when_up_to_date():
    assert logic.should_notify_update("0.15.0", "0.15.0", None) is False
    assert logic.should_notify_update("0.15.0", "0.14.1", None) is False   # latest older


def test_should_not_notify_twice_for_same_version():
    # Already told them about 0.15.0 -> stay quiet on the next startup.
    assert logic.should_notify_update("0.14.1", "0.15.0", "0.15.0") is False


def test_should_notify_again_for_a_newer_release():
    # We notified about 0.15.0 before; 0.16.0 is newer, so nag once more.
    assert logic.should_notify_update("0.14.1", "0.16.0", "0.15.0") is True


def test_should_notify_handles_v_prefix_and_blank_latest():
    assert logic.should_notify_update("0.14.1", "v0.15.0", None) is True
    assert logic.should_notify_update("0.14.1", "", None) is False
    assert logic.should_notify_update("0.14.1", None, None) is False


# --------------------------------------------------------------- clamp_interval_minutes
def test_clamp_interval_keeps_a_valid_value():
    assert logic.clamp_interval_minutes(30) == 30


def test_clamp_interval_raises_below_floor():
    assert logic.clamp_interval_minutes(0, floor_minutes=1) == 1
    assert logic.clamp_interval_minutes(-5, floor_minutes=1) == 1
    assert logic.clamp_interval_minutes(1, floor_minutes=15) == 15


def test_clamp_interval_falls_back_to_default_on_garbage():
    assert logic.clamp_interval_minutes(None, default_minutes=15) == 15
    assert logic.clamp_interval_minutes("", default_minutes=15) == 15
    assert logic.clamp_interval_minutes("not a number", default_minutes=15) == 15


def test_clamp_interval_accepts_numeric_strings():
    assert logic.clamp_interval_minutes("45") == 45


def test_clamp_interval_caps_an_absurd_value():
    """The floor's counterpart. The result becomes a QTimer interval in milliseconds,
    which is a C int, so a hand-edited config could overflow it and turn every launch
    into Anki's raw add-on error dialog."""
    assert logic.clamp_interval_minutes(99999999) == 7 * 24 * 60
    assert logic.clamp_interval_minutes(99999999, ceiling_minutes=60) == 60
    # and the capped value is still a sane millisecond count for a C int
    assert logic.clamp_interval_minutes(99999999) * 60 * 1000 < 2 ** 31


# ----------------------------------------------------------- decide_addon_update_action
def test_decide_update_action_none_when_current():
    assert logic.decide_addon_update_action(
        "0.16.0", "0.16.0", auto_update=False, notify=True) == "none"
    assert logic.decide_addon_update_action(
        "0.16.0", "0.15.0", auto_update=True, notify=True) == "none"


def test_decide_update_action_auto_update_when_enabled():
    assert logic.decide_addon_update_action(
        "0.14.1", "0.16.0", auto_update=True, notify=False) == "auto_update"


def test_decide_update_action_auto_update_beats_notify_when_both_on():
    assert logic.decide_addon_update_action(
        "0.14.1", "0.16.0", auto_update=True, notify=True) == "auto_update"


def test_decide_update_action_notify_when_auto_update_off():
    assert logic.decide_addon_update_action(
        "0.14.1", "0.16.0", auto_update=False, notify=True) == "notify"


def test_decide_update_action_none_when_both_toggles_off():
    assert logic.decide_addon_update_action(
        "0.14.1", "0.16.0", auto_update=False, notify=False) == "none"


def test_decide_update_action_notify_respects_once_per_release():
    # Already notified about 0.16.0 -> a plain notify stays quiet on the next check.
    assert logic.decide_addon_update_action(
        "0.14.1", "0.16.0", auto_update=False, notify=True,
        last_notified="0.16.0") == "none"


def test_decide_update_action_auto_update_ignores_last_notified():
    # Auto-update isn't a nag, so it isn't suppressed by a prior notify record.
    assert logic.decide_addon_update_action(
        "0.14.1", "0.16.0", auto_update=True, notify=True,
        last_notified="0.16.0") == "auto_update"


def test_decide_update_action_none_on_blank_latest():
    assert logic.decide_addon_update_action(
        "0.14.1", "", auto_update=True, notify=True) == "none"
    assert logic.decide_addon_update_action(
        "0.14.1", None, auto_update=True, notify=True) == "none"


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


def test_decks_to_update_skips_excluded_even_when_stale():
    # An opted-out deck is skipped no matter how out of date it is.
    manifest = _manifest(("A", "v2"), ("B", "v2"))
    installed = {"A": "v1", "B": "v1"}          # both would otherwise update
    todo = [d["name"] for d in logic.decks_to_update(manifest, installed, excluded=["A"])]
    assert todo == ["B"]


def test_decks_to_update_excluded_default_is_backward_compatible():
    manifest = _manifest(("A", "v1"))
    # No excluded arg == old behavior: a new deck is still pending.
    assert len(logic.decks_to_update(manifest, {})) == 1


def test_decks_to_update_skips_an_entry_missing_its_name_or_version():
    # Both used to KeyError, so one malformed row in the manifest stopped every other
    # deck from syncing. There is nothing to fetch without a name and nothing to compare
    # without a version, so such a row is skipped and the rest still go.
    manifest = {"decks": [{"version": "v1"},          # no name
                          {"name": "B"},              # no version
                          {"name": "", "version": "v9"},
                          {"name": "C", "version": "v3"}]}
    assert [d["name"] for d in logic.decks_to_update(manifest, {})] == ["C"]


def test_decks_to_update_skips_a_versionless_entry_even_when_installed():
    # installed.get("B") != None would otherwise mark it pending, and the sync would
    # then KeyError on d["version"] downstream instead.
    manifest = {"decks": [{"name": "B"}]}
    assert logic.decks_to_update(manifest, {"B": "v1"}) == []


def test_decks_to_update_skips_a_non_dict_entry():
    # A manifest row that isn't a dict at all (e.g. a bare string) must not crash on
    # .get(); it's skipped like any other malformed row.
    manifest = {"decks": ["not-a-deck", {"name": "C", "version": "v3"}]}
    assert [d["name"] for d in logic.decks_to_update(manifest, {})] == ["C"]


# ----------------------------------------------------------------------- deck_status
def test_deck_status_reports_new_update_current():
    manifest = {"decks": [
        {"name": "X::A", "version": "v1", "cards": 10},
        {"name": "X::B", "version": "v2", "cards": 20},
        {"name": "X::C", "version": "v3", "cards": 30},
    ]}
    installed = {"X::B": "v1", "X::C": "v3"}     # A unseen, B stale, C current
    rows = {r["name"]: r for r in logic.deck_status(manifest, installed)}
    assert rows["X::A"]["state"] == "new"
    assert rows["X::B"]["state"] == "update"
    assert rows["X::C"]["state"] == "current"
    assert rows["X::A"]["short"] == "A"          # last :: segment
    assert rows["X::A"]["cards"] == 10


def test_deck_status_marks_excluded_as_disabled():
    manifest = {"decks": [{"name": "X::A", "version": "v1", "cards": 5}]}
    rows = logic.deck_status(manifest, {}, excluded=["X::A"])
    assert rows[0]["enabled"] is False


def test_deck_status_enabled_by_default():
    manifest = {"decks": [{"name": "X::A", "version": "v1", "cards": 5}]}
    assert logic.deck_status(manifest, {})[0]["enabled"] is True


def test_deck_status_tolerates_empty_manifest():
    assert logic.deck_status(None, {}) == []


def test_deck_status_passes_through_missing_card_count():
    # A manifest deck without a "cards" field must not crash; cards is just None.
    manifest = {"decks": [{"name": "X::A", "version": "v1"}]}
    assert logic.deck_status(manifest, {})[0]["cards"] is None


def test_deck_status_excluded_deck_still_listed_but_disabled():
    # Excluding a deck hides it from syncing but it must still appear in the manager so
    # the user can re-enable it.
    manifest = {"decks": [{"name": "X::A", "version": "v1", "cards": 3}]}
    rows = logic.deck_status(manifest, {}, excluded=["X::A"])
    assert len(rows) == 1 and rows[0]["enabled"] is False


def test_deck_status_skips_an_entry_missing_its_name():
    # d["name"] used to KeyError here, crashing Manage decks on a row that
    # decks_to_update already tolerated.
    manifest = {"decks": [{"version": "v1"}, {"name": "C", "version": "v3"}]}
    assert [r["name"] for r in logic.deck_status(manifest, {})] == ["C"]


def test_deck_status_tolerates_an_entry_missing_its_version():
    manifest = {"decks": [{"name": "X::A"}]}
    rows = logic.deck_status(manifest, {})
    assert rows[0]["name"] == "X::A" and rows[0]["state"] == "new"


def test_deck_status_skips_a_non_dict_entry():
    manifest = {"decks": ["not-a-deck", {"name": "C", "version": "v3"}]}
    assert [r["name"] for r in logic.deck_status(manifest, {})] == ["C"]


# ----------------------------------------------------------------------- parse_fields
def test_parse_fields_trims_and_drops_empties():
    assert logic.parse_fields(" Notes , My Field ,, ") == ["Notes", "My Field"]


def test_parse_fields_dedupes_preserving_order():
    assert logic.parse_fields("a, b, a, b") == ["a", "b"]


def test_parse_fields_empty_returns_default():
    assert logic.parse_fields("") == ["Notes"]
    assert logic.parse_fields("   ,  ") == ["Notes"]
    assert logic.parse_fields(None) == ["Notes"]


def test_parse_fields_custom_default():
    assert logic.parse_fields("", default=("A", "B")) == ["A", "B"]


# --------------------------------------------------------------------- apkg_notes
def test_apkg_notes_reads_id_fields_guid(tmp_path):
    apkg = str(tmp_path / "deck.apkg")
    _make_mock_apkg(apkg, [(1, "guid-a", "Front A"), (2, "guid-b", "Front B")])
    rows = logic.apkg_notes(apkg)
    assert rows == [(1, ["Front A", "back text"], "guid-a"),
                    (2, ["Front B", "back text"], "guid-b")]


def test_apkg_notes_rejects_non_apkg_zip(tmp_path):
    bogus = str(tmp_path / "bogus.apkg")
    with zipfile.ZipFile(bogus, "w") as z:
        z.writestr("not_a_collection.txt", "nope")
    try:
        logic.apkg_notes(bogus)
        assert False, "expected RuntimeError for a zip with no collection.anki2"
    except RuntimeError:
        pass


# --------------------------------------------------------------------- apkg media
def _add_apkg_media(path, files):
    """files: {filename: bytes}. Appends the numbered blobs and the `media` index an
    .apkg carries alongside its collection, mirroring what genanki writes."""
    with zipfile.ZipFile(path, "a", zipfile.ZIP_DEFLATED) as z:
        index = {}
        for i, (name, blob) in enumerate(files.items()):
            z.writestr(str(i), blob)
            index[str(i)] = name
        z.writestr("media", json.dumps(index))


def test_apkg_media_index_maps_each_filename_to_its_member(tmp_path):
    apkg = str(tmp_path / "deck.apkg")
    _make_mock_apkg(apkg, [(1, "guid-a", "Front A")])
    _add_apkg_media(apkg, {"sample-a.jpg": b"\x89PNG-ish", "sample-b.jpg": b"jpeg-ish"})
    assert logic.apkg_media_index(apkg) == {"sample-a.jpg": "0", "sample-b.jpg": "1"}


def test_apkg_media_index_is_empty_when_the_deck_carries_no_pictures(tmp_path):
    # The normal case for a text-only deck: no media member at all. It must read as
    # "nothing to resolve" rather than raising, so a caller falls back to naming.
    apkg = str(tmp_path / "deck.apkg")
    _make_mock_apkg(apkg, [(1, "guid-a", "Front A")])
    assert logic.apkg_media_index(apkg) == {}


def test_apkg_media_index_tolerates_an_unreadable_media_member(tmp_path):
    apkg = str(tmp_path / "deck.apkg")
    _make_mock_apkg(apkg, [(1, "guid-a", "Front A")])
    with zipfile.ZipFile(apkg, "a") as z:
        z.writestr("media", "not json at all")
    assert logic.apkg_media_index(apkg) == {}


def test_extract_apkg_media_writes_only_the_names_asked_for(tmp_path):
    """A deck can carry a couple of hundred images; a review that opens one card must
    not pay for the rest."""
    apkg = str(tmp_path / "deck.apkg")
    _make_mock_apkg(apkg, [(1, "guid-a", "Front A")])
    _add_apkg_media(apkg, {"sample-a.jpg": b"one", "sample-b.jpg": b"two"})
    dest = str(tmp_path / "media")
    out = logic.extract_apkg_media(apkg, logic.apkg_media_index(apkg),
                                   ["sample-a.jpg"], dest)
    assert list(out) == ["sample-a.jpg"]
    assert open(out["sample-a.jpg"], "rb").read() == b"one"
    assert sorted(os.listdir(dest)) == ["sample-a.jpg"]


def test_extract_apkg_media_skips_a_name_the_index_does_not_have(tmp_path):
    # A field can reference an image the deck never shipped. One bad reference must not
    # blank the whole row.
    apkg = str(tmp_path / "deck.apkg")
    _make_mock_apkg(apkg, [(1, "guid-a", "Front A")])
    _add_apkg_media(apkg, {"sample-a.jpg": b"one"})
    out = logic.extract_apkg_media(apkg, logic.apkg_media_index(apkg),
                                   ["sample-a.jpg", "missing.jpg"], str(tmp_path / "m"))
    assert list(out) == ["sample-a.jpg"]


# ----------------------------------------------------------------------- remap_cards
def test_remap_cards_end_to_end(tmp_path):
    apkg = str(tmp_path / "deck.apkg")
    _make_mock_apkg(apkg, [
        (1, "apkg-guid-matched", "Matches directly"),
        (2, "apkg-guid-aliased", "New wording"),
        (3, "apkg-guid-new", "Never seen before"),
    ])
    her = {
        "Matches directly": "her-guid-direct",   # front unchanged since her last sync
        "Old wording": "her-guid-aliased",       # her card still has the old front
    }
    aliases = {"New wording": "Old wording"}     # records that rename

    remap, in_place, as_new, new_notes, _ = logic.remap_cards(apkg, her, aliases)

    assert in_place == 2          # "Matches directly" and "New wording" (via alias)
    assert as_new == 1            # "Never seen before" has no match anywhere
    # GUIDs get rewritten to match her existing cards so Anki's importer updates in
    # place instead of creating duplicates:
    assert remap == {1: "her-guid-direct", 2: "her-guid-aliased"}
    # and the one genuinely new card comes back in full, for the review dialog:
    assert new_notes == [(3, ["Never seen before", "back text"], "apkg-guid-new")]


def test_remap_cards_no_matches_are_all_new(tmp_path):
    apkg = str(tmp_path / "deck.apkg")
    _make_mock_apkg(apkg, [(1, "g1", "Nobody has this")])
    remap, in_place, as_new, new_notes, _ = logic.remap_cards(apkg, her={}, aliases={})
    assert (remap, in_place, as_new) == ({}, 0, 1)
    assert new_notes == [(1, ["Nobody has this", "back text"], "g1")]


def test_remap_cards_guid_already_matches_needs_no_rewrite(tmp_path):
    # If the incoming note already carries the learner's GUID (e.g. re-syncing an
    # unchanged deck), it counts as in-place but must NOT be added to remap — rewriting
    # it to itself is pointless churn.
    apkg = str(tmp_path / "deck.apkg")
    _make_mock_apkg(apkg, [(1, "shared-guid", "Same front")])
    her = {"Same front": "shared-guid"}
    remap, in_place, as_new, new_notes, _ = logic.remap_cards(apkg, her, aliases={})
    assert (remap, in_place, as_new, new_notes) == ({}, 1, 0, [])


def test_remap_cards_alias_target_also_missing_is_new(tmp_path):
    # An alias records a rename, but if the learner's collection has NEITHER the new
    # wording nor the old one the alias points to, the card is genuinely new to her.
    apkg = str(tmp_path / "deck.apkg")
    _make_mock_apkg(apkg, [(1, "g1", "New wording")])
    aliases = {"New wording": "Old wording"}   # but "Old wording" isn't in her map
    remap, in_place, as_new, new_notes, _ = logic.remap_cards(apkg, her={}, aliases=aliases)
    assert (remap, in_place, as_new) == ({}, 0, 1)
    assert [rid for rid, _, _ in new_notes] == [1]


def test_remap_cards_matches_by_guid_before_front(tmp_path):
    # Stable-id builds keep a card's GUID through a front rewording. A learner whose
    # card already carries the incoming GUID must match in place with NO remap entry,
    # even when the front text differs and no alias exists — this is exactly the
    # "reworded twice, alias only bridges one hop" case that used to strand history.
    apkg = str(tmp_path / "deck.apkg")
    _make_mock_apkg(apkg, [(1, "stable-guid", "Reworded front, take three")])
    her = {"Original front wording": "stable-guid"}
    remap, in_place, as_new, new_notes, _ = logic.remap_cards(apkg, her, aliases={})
    assert (remap, in_place, as_new, new_notes) == ({}, 1, 0, [])


def test_remap_cards_guid_match_wins_over_front_match(tmp_path):
    # If the incoming GUID already belongs to her card A, a coincidental front-text
    # match against her card B must not override it: GUID is the deliberate identity.
    apkg = str(tmp_path / "deck.apkg")
    _make_mock_apkg(apkg, [(1, "guid-a", "Front of B")])
    her = {"Front of A": "guid-a", "Front of B": "guid-b"}
    remap, in_place, as_new, new_notes, _ = logic.remap_cards(apkg, her, aliases={})
    assert (remap, in_place, as_new, new_notes) == ({}, 1, 0, [])


def test_remap_cards_new_notes_length_always_matches_as_new(tmp_path):
    # as_new is just a count of new_notes; if these two ever disagree the confirmation
    # would promise a number of cards the review dialog can't actually show.
    apkg = str(tmp_path / "deck.apkg")
    _make_mock_apkg(apkg, [
        (1, "g-known", "She has this"),
        (2, "g-new-a", "New A"),
        (3, "g-new-b", "New B"),
    ])
    _, _, as_new, new_notes, _ = logic.remap_cards(
        apkg, her={"She has this": "g-known"}, aliases={})
    assert as_new == len(new_notes) == 2
    assert [rid for rid, _, _ in new_notes] == [2, 3]   # apkg order preserved


def test_remap_cards_new_notes_carries_every_field_for_image_cards(tmp_path):
    # An image note's first field is an <img> tag, not a prompt, so new_notes must carry
    # the whole field list; field zero alone would render as a broken image in the
    # confirmation's inline list rather than naming the card.
    apkg = str(tmp_path / "deck.apkg")
    _make_mock_apkg(apkg, [(1, "g1", ['<img src="sample-a.jpg">', "Name this nerve",
                                      "Femoral nerve"])])
    _, _, _, new_notes, _ = logic.remap_cards(apkg, her={}, aliases={})
    assert new_notes == [(1, ['<img src="sample-a.jpg">', "Name this nerve",
                              "Femoral nerve"], "g1")]
    # and the display helper picks the prompt out of exactly that list:
    assert logic.note_display_label(new_notes[0][1]) == "Name this nerve"


def test_remap_cards_reports_every_pair_it_matched(tmp_path):
    """Change detection has to reuse this ladder rather than reimplement it: a second
    implementation would eventually disagree, and the visible symptom is a preview that
    lies about which cards are about to change."""
    apkg = str(tmp_path / "deck.apkg")
    _make_mock_apkg(apkg, [
        (1, "apkg-guid-same", "Matched by guid"),
        (2, "apkg-guid-other", "Matched by front"),
        (3, "apkg-guid-new", "Nobody has this"),
    ])
    her = {"Matched by front": "her-guid-front", "Matched by guid": "apkg-guid-same"}
    _, in_place, as_new, new_notes, matched = logic.remap_cards(apkg, her, aliases={})
    assert in_place == 2 and as_new == 1
    assert matched == [(1, "apkg-guid-same", "apkg-guid-same"),
                       (2, "apkg-guid-other", "her-guid-front")]
    assert len(new_notes) == 1


def test_remap_cards_matched_and_new_together_account_for_every_note(tmp_path):
    apkg = str(tmp_path / "deck.apkg")
    _make_mock_apkg(apkg, [(1, "a", "One"), (2, "b", "Two"), (3, "c", "Three")])
    _, _, _, new_notes, matched = logic.remap_cards(
        apkg, her={"One": "her-one"}, aliases={})
    assert len(matched) + len(new_notes) == 3


# ------------------------------------------------------------------ find_changed_notes
def _detail(rid, notetype, fields):
    return {"rid": rid, "guid": f"g{rid}", "notetype": notetype, "fields": fields}


def test_find_changed_notes_reports_the_previous_value_of_each_changed_field():
    details = [_detail(1, "Study Deck - Basic",
                       [("Front", "A prompt"), ("Back", "the new answer"),
                        ("Why", "unchanged why"), ("Notes", "")])]
    her = {"her-1": {"Front": "A prompt", "Back": "the old answer",
                     "Why": "unchanged why", "Notes": ""}}
    assert logic.find_changed_notes([(1, "g1", "her-1")], details, her) == {
        1: {"Back": "the old answer"}}


def test_find_changed_notes_ignores_a_note_that_is_identical():
    details = [_detail(1, "Study Deck - Basic",
                       [("Front", "A prompt"), ("Back", "same"), ("Notes", "")])]
    her = {"her-1": {"Front": "A prompt", "Back": "same", "Notes": ""}}
    assert logic.find_changed_notes([(1, "g1", "her-1")], details, her) == {}


def test_find_changed_notes_skips_protected_fields():
    """Notes is the learner's own annotation space and every spec ships it empty, so
    without this every card she has ever written a note on would read as changed."""
    details = [_detail(1, "Study Deck - Basic",
                       [("Front", "A prompt"), ("Notes", "")])]
    her = {"her-1": {"Front": "A prompt", "Notes": "her own annotation"}}
    assert logic.find_changed_notes([(1, "g1", "her-1")], details, her,
                                    protected=["Notes"]) == {}


def test_find_changed_notes_matches_a_protected_field_case_insensitively():
    """The preserved-fields box is free text and the real names are capitalised, so
    collection.py's _note_field resolves them case-insensitively when it snapshots and
    restores. This skip was exact-match, so a typed "notes" listed the field as about to
    change in the preview while the restore quietly put it back."""
    details = [_detail(1, "Study Deck - Basic",
                       [("Front", "A prompt"), ("Notes", ""), ("Dosing", "")])]
    her = {"her-1": {"Front": "A prompt", "Notes": "her own annotation",
                     "Dosing": "her dose note"}}
    assert logic.find_changed_notes([(1, "g1", "her-1")], details, her,
                                    protected=["notes", "DOSING"]) == {}


def test_find_changed_notes_compares_by_name_not_position():
    """Index 1 is Back on a basic note and Prompt on an image note. A positional
    comparison mislabels whole decks."""
    details = [_detail(1, "Study Deck - Image ID",
                       [("Image", '<img src="a.jpg">'), ("Prompt", "Which block?"),
                        ("Answer", "Femoral")])]
    her = {"her-1": {"Prompt": "Which block?", "Answer": "Femoral",
                     "Image": '<img src="a.jpg">'}}
    assert logic.find_changed_notes([(1, "g1", "her-1")], details, her) == {}


def test_find_changed_notes_ignores_a_field_her_note_type_does_not_have():
    # _ensure_notetypes adds a genuinely missing field before an import; until it has,
    # a field she cannot hold is not a content change to show her.
    details = [_detail(1, "Study Deck - Basic",
                       [("Front", "A prompt"), ("Dosing", "0.5 mg IV")])]
    her = {"her-1": {"Front": "A prompt"}}
    assert logic.find_changed_notes([(1, "g1", "her-1")], details, her) == {}


def test_find_changed_notes_ignores_whitespace_only_differences():
    details = [_detail(1, "Study Deck - Basic", [("Front", " A prompt ")])]
    her = {"her-1": {"Front": "A prompt"}}
    assert logic.find_changed_notes([(1, "g1", "her-1")], details, her) == {}


def test_find_changed_notes_skips_a_pair_with_nothing_to_compare_against():
    details = [_detail(1, "Study Deck - Basic", [("Front", "A prompt")])]
    assert logic.find_changed_notes([(1, "g1", "unknown-guid")], details, {}) == {}
    assert logic.find_changed_notes([(99, "g99", "her-1")], details,
                                    {"her-1": {"Front": "other"}}) == {}


# ----------------------------------------------------------------- apkg_note_details
# Field names are NOT uniform across our note types, which is the whole reason this
# function reads col.models instead of guessing positionally: index 1 is "Back" on a
# basic note but "Prompt" on an image note.
_MODELS = {
    "1": {"name": "Intern Pearls Basic",
          "flds": [{"name": "Front", "ord": 0}, {"name": "Back", "ord": 1},
                   {"name": "Why", "ord": 2}, {"name": "Notes", "ord": 3}]},
    "2": {"name": "Intern Pearls Image",
          "flds": [{"name": "Image", "ord": 0}, {"name": "Prompt", "ord": 1},
                   {"name": "Answer", "ord": 2}]},
}


def test_apkg_note_details_labels_fields_per_notetype(tmp_path):
    apkg = str(tmp_path / "deck.apkg")
    _make_mock_apkg(apkg, [
        (1, "g1", ["Front text", "Back text", "Why text", ""], 1),
        (2, "g2", ['<img src="x.jpg">', "Name this", "Answer text"], 2),
    ], models=_MODELS)
    basic, image = logic.apkg_note_details(apkg)
    assert basic["notetype"] == "Intern Pearls Basic"
    assert basic["fields"] == [("Front", "Front text"), ("Back", "Back text"),
                               ("Why", "Why text"), ("Notes", "")]
    assert image["notetype"] == "Intern Pearls Image"
    # index 1 is "Prompt" here, not "Back": the exact mislabeling a positional guess
    # would produce.
    assert image["fields"][1] == ("Prompt", "Name this")


def test_apkg_note_details_orders_fields_by_ord_not_json_order(tmp_path):
    # col.models is JSON, so field order in the dict is not authoritative; "ord" is.
    models = {"1": {"name": "Scrambled",
                    "flds": [{"name": "Second", "ord": 1}, {"name": "First", "ord": 0}]}}
    apkg = str(tmp_path / "deck.apkg")
    _make_mock_apkg(apkg, [(1, "g1", ["value a", "value b"], 1)], models=models)
    assert logic.apkg_note_details(apkg)[0]["fields"] == [
        ("First", "value a"), ("Second", "value b")]


def test_apkg_note_details_filters_to_requested_rids_preserving_order(tmp_path):
    apkg = str(tmp_path / "deck.apkg")
    _make_mock_apkg(apkg, [
        (1, "g1", ["A"], 1), (2, "g2", ["B"], 1), (3, "g3", ["C"], 1),
    ], models=_MODELS)
    details = logic.apkg_note_details(apkg, rids=[3, 1])
    assert [d["rid"] for d in details] == [1, 3]   # apkg order, not the caller's


def test_apkg_note_details_unknown_notetype_falls_back_to_generic_labels(tmp_path):
    # A deck built with a note type this .apkg doesn't describe still previews, with
    # generic labels: a mislabeled preview beats no preview, and never a crash.
    apkg = str(tmp_path / "deck.apkg")
    _make_mock_apkg(apkg, [(1, "g1", ["a", "b"], 99)], models=_MODELS)
    d = logic.apkg_note_details(apkg)[0]
    assert d["notetype"] == ""
    assert d["fields"] == [("Field 1", "a"), ("Field 2", "b")]


def test_apkg_note_details_without_any_models_table_still_works(tmp_path):
    apkg = str(tmp_path / "deck.apkg")
    _make_mock_apkg(apkg, [(1, "g1", ["a", "b"], 1)])   # no models= at all
    d = logic.apkg_note_details(apkg)[0]
    assert d["guid"] == "g1"
    assert d["fields"] == [("Field 1", "a"), ("Field 2", "b")]


def test_apkg_note_details_rejects_non_apkg_zip(tmp_path):
    bogus = str(tmp_path / "bogus.apkg")
    with zipfile.ZipFile(bogus, "w") as z:
        z.writestr("not_a_collection.txt", "nope")
    try:
        logic.apkg_note_details(bogus)
        assert False, "expected RuntimeError for a zip with no collection.anki2"
    except RuntimeError:
        pass


# ----------------------------------------------------------------- field_preview_text
def test_field_preview_text_names_images_instead_of_rendering_them(tmp_path):
    # The review dialog never extracts the .apkg's media, so a rendered <img> would be
    # a broken image. Naming the file says "this card has a picture" instead.
    assert logic.field_preview_text('<img src="sample-a.jpg">') == "[image: sample-a.jpg]"


def test_field_preview_text_reports_text_and_image_together():
    assert logic.field_preview_text('Look here: <img src="a/b/nerve.png">') == (
        "Look here: [image: nerve.png]")


def test_field_preview_text_plain_field_is_unchanged():
    assert logic.field_preview_text("Just prose") == "Just prose"
    assert logic.field_preview_text("") == ""


# ----------------------------------------------------------------- render_math_spans
def test_field_preview_html_renders_a_mathjax_block_as_plain_html():
    # A QLabel has no MathJax engine, so without this the dialog shows the raw
    # backslash markup. Real formula from the ABG deck (Winter's).
    out = logic.field_preview_html(
        "Winter's formula: "
        "\\[ \\text{Expected PaCO}_2 = 1.5 \\times \\text{HCO}_3^- + 8 \\;\\; (\\pm 2) \\]")
    assert out == ("Winter's formula: "
                   "Expected PaCO<sub>2</sub> = 1.5 × HCO<sub>3</sub><sup>-</sup> + 8 (± 2)")


def test_field_preview_html_renders_a_fraction_inline():
    out = logic.field_preview_html(
        "\\[ \\text{P/F ratio} = \\frac{\\text{PaO}_2}{\\text{FiO}_2} \\]")
    assert out == "P/F ratio = PaO<sub>2</sub>/FiO<sub>2</sub>"


def test_field_preview_html_parenthesizes_a_fraction_arm_holding_an_expression():
    out = logic.field_preview_html("\\(= \\frac{\\text{age}}{4} + 4\\)")
    assert out == "= age/4 + 4"


def test_field_preview_text_flattens_mathjax_without_stray_spaces():
    # The plain path gets bare characters, not <sub>/<sup>: plain_text turns every
    # stripped tag into a space, which would render "PaCO 2".
    out = logic.field_preview_text(
        "\\( \\text{Anion gap} = \\text{Na}^+ - (\\text{Cl}^- + \\text{HCO}_3^-) \\)")
    assert out == "Anion gap = Na+ - (Cl- + HCO3-)"


def test_render_math_spans_leaves_a_field_without_math_alone():
    plain = 'SpO₂ &lt;94% and a <span class="cloze">blank</span>'
    assert logic.render_math_spans(plain) == plain


# ----------------------------------------------------------------- field_preview_html
def test_field_preview_html_keeps_the_structure_a_card_was_written_with():
    # A comparison is written as a table because the grid carries the meaning. Reducing
    # it to plain text is what made a correct card read as "just jumbled text".
    out = logic.field_preview_html("<table><tr><th>A</th><td>1</td></tr></table>")
    assert out == "<table><tr><th>A</th><td>1</td></tr></table>"


def test_field_preview_html_keeps_colspan_but_drops_everything_else():
    out = logic.field_preview_html(
        '<td colspan="2" class="hl" onclick="x()">Both</td>')
    assert out == '<td colspan="2">Both</td>'


def test_field_preview_html_drops_an_unrenderable_tag_and_keeps_its_text():
    out = logic.field_preview_html('<a href="http://x">Barash</a> p551')
    assert "<a" not in out and "href" not in out
    assert "Barash" in out and "p551" in out


def test_field_preview_html_names_images_the_same_way_the_text_preview_does():
    # Same reason as field_preview_text: the dialog never extracts the .apkg's media.
    assert logic.field_preview_html(
        'Look: <img src="a/b/nerve.png">') == "Look: [image: nerve.png]"


def test_field_preview_html_drops_script_and_style_blocks_whole():
    out = logic.field_preview_html("<style>td{color:red}</style>Keep<script>x()</script>")
    assert "color:red" not in out and "x()" not in out
    assert "Keep" in out


def test_field_preview_html_leaves_entities_and_escaped_comparators_alone():
    # &#8593; is an arrow the card meant to show; &lt;94% is a literal '<' the spec
    # escaped. Neither is markup, and double-escaping either would corrupt the card.
    assert logic.field_preview_html("SpO2 &lt;94% &#8593;") == "SpO2 &lt;94% &#8593;"
    assert logic.field_preview_html("") == ""


def test_field_preview_html_substitutes_what_the_resolver_returns():
    out = logic.field_preview_html(
        'before <img src="sample-a.jpg"> after',
        image_html=lambda name: f'<img src="/tmp/{name}" width="440">')
    assert '<img src="/tmp/sample-a.jpg" width="440">' in out
    assert "[image:" not in out
    assert "before" in out and "after" in out


def test_field_preview_html_falls_back_to_naming_when_the_resolver_declines():
    """A file Qt cannot decode, or one the deck never shipped, has to degrade to the
    chip rather than to a broken image icon."""
    out = logic.field_preview_html('<img src="missing.jpg">', image_html=lambda n: None)
    assert out == "[image: missing.jpg]"


def test_field_preview_html_hands_the_resolver_a_bare_filename():
    seen = []
    logic.field_preview_html('<img src="subdir/sample-a.jpg">',
                             image_html=lambda name: seen.append(name) or None)
    assert seen == ["sample-a.jpg"]


def test_field_preview_html_with_no_resolver_is_unchanged():
    assert logic.field_preview_html('<img src="sample-a.jpg">') == "[image: sample-a.jpg]"


def test_field_preview_html_resolves_multiple_images_in_correct_order():
    """Multiple images in one field must each be resolved with their own filename.
    A placeholder collision or reversed restoration would fail this guard."""
    resolver_calls = []

    def track_and_resolve(name):
        resolver_calls.append(name)
        return f'<img src="/resolved/{name}" alt="{name}">'

    out = logic.field_preview_html(
        'First <img src="sample-a.jpg"> and second <img src="sample-b.jpg">',
        image_html=track_and_resolve)

    # Both filenames were passed to the resolver in document order
    assert resolver_calls == ["sample-a.jpg", "sample-b.jpg"]
    # Both resolved images appear in the output paired with their correct names
    assert '<img src="/resolved/sample-a.jpg" alt="sample-a.jpg">' in out
    assert '<img src="/resolved/sample-b.jpg" alt="sample-b.jpg">' in out
    # No placeholder text leaked into the output
    assert "__RESOLVED_IMG_" not in out
    # Surrounding text is preserved
    assert "First" in out and "and second" in out


def test_field_image_names_lists_every_picture_in_document_order():
    assert logic.field_image_names(
        '<img src="a.jpg">text<img src="sub/b.png">') == ["a.jpg", "b.png"]


def test_field_image_names_dedupes_and_handles_no_images():
    assert logic.field_image_names('<img src="a.jpg"><img src="a.jpg">') == ["a.jpg"]
    assert logic.field_image_names("plain text") == []
    assert logic.field_image_names("") == []


# --------------------------------------------------------------- cloze_filled_html
def test_cloze_filled_html_leaves_markup_alone_when_asked_not_to_escape():
    # The already-sanitized path: field_preview_html has run, so escaping again would
    # turn the field's own table into visible tags.
    assert logic.cloze_filled_html(
        "<td>{{c1::&#8593;}}</td>", escape=False) == (
        '<td><span class="cloze">&#8593;</span></td>')


def test_cloze_filled_html_fills_each_deletion_with_its_answer():
    assert logic.cloze_filled_html(
        "The {{c1::tibial}} nerve lies posterior to the {{c2::medial}} malleolus") == (
        'The <span class="cloze">tibial<sup class="cn">c1</sup></span> nerve lies '
        'posterior to the <span class="cloze">medial<sup class="cn">c2</sup></span> '
        'malleolus')


def test_cloze_filled_html_labels_each_group_when_a_field_holds_more_than_one():
    # Two groups is two cards sharing one field, and which blanks belong to which card
    # is invisible when every deletion renders the same. Blanks that share a number
    # share a label, which is the whole point: it reads as one card's worth.
    out = logic.cloze_filled_html("{{c1::a}}, {{c1::b}}, {{c2::c}}")
    assert out.count('<sup class="cn">c1</sup>') == 2
    assert out.count('<sup class="cn">c2</sup>') == 1


def test_cloze_filled_html_leaves_a_single_group_field_unlabelled():
    # Labelling every blank "c1" on a one-card note is noise for a distinction that
    # isn't there.
    assert "<sup" not in logic.cloze_filled_html("{{c1::one}} and {{c1::two}}")


def test_cloze_filled_html_group_labels_can_be_forced_either_way():
    assert "<sup" in logic.cloze_filled_html("{{c1::one}}", mark_groups=True)
    assert "<sup" not in logic.cloze_filled_html(
        "{{c1::one}} {{c2::two}}", mark_groups=False)


def test_cloze_filled_html_drops_the_hint_and_keeps_the_answer():
    assert logic.cloze_filled_html("Give {{c1::4 mg::dose}} of it") == (
        'Give <span class="cloze">4 mg</span> of it')


def test_cloze_filled_html_passes_through_a_field_with_no_deletions():
    assert logic.cloze_filled_html("Just prose") == "Just prose"
    assert logic.cloze_filled_html("") == ""


def test_cloze_filled_html_escapes_field_content_rather_than_rendering_it():
    # A card's own text is data. Escaping has to happen before the spans go in, or the
    # spans get escaped too and the markup shows up as visible text.
    assert logic.cloze_filled_html("SpO2 {{c1::<94%}} is low") == (
        'SpO2 <span class="cloze">&lt;94%</span> is low')


def test_cloze_filled_html_returns_empty_string_for_none():
    assert logic.cloze_filled_html(None) == ""


def test_cloze_filled_html_keeps_a_bare_colon_in_the_answer():
    # ":" is not the "::" hint separator, so an answer containing one must render whole.
    assert logic.cloze_filled_html("{{c1::ratio 1:2}}") == (
        '<span class="cloze">ratio 1:2</span>')


def test_cloze_filled_html_does_not_let_a_malformed_deletion_swallow_the_next_one():
    # An unclosed deletion must not let its non-greedy match backtrack past a following
    # "{{" and eat a well-formed deletion's answer. The malformed one degrades to raw
    # text instead of silently swallowing real content.
    assert logic.cloze_filled_html(
        "Unclosed {{c1::foo and then {{c2::bar}} end") == (
        'Unclosed {{c1::foo and then <span class="cloze">bar</span> end')


# -------------------------------------------------------------- build_feedback_digest
def test_build_feedback_digest_groups_by_deck_and_names_each_card(tmp_path):
    text = logic.build_feedback_digest([
        {"deck": "Intern Pearls::Intern Custom::Pharmacology", "front": "Vasopressor?",
         "guid": "abc123", "note": "dose is wrong"},
        {"deck": "Intern Pearls::Intern Custom::Pharmacology", "front": "Beta blocker?",
         "guid": "def456", "note": "too bulky"},
        {"deck": "Intern Pearls::Intern Custom::Regional", "front": "Which nerve?",
         "guid": "ghi789", "note": "contrast is backwards"},
    ], version="0.30.0", date="2026-07-15")
    assert "Intern Pearls card feedback" in text
    assert "2026-07-15" in text and "0.30.0" in text
    # Deck headings use the leaf name; the full path is noise in a text message.
    assert "Pharmacology" in text and "Intern Pearls::Intern Custom" not in text
    # The GUID is the point: it points at the exact spec note without hunting.
    assert "abc123" in text and "def456" in text and "ghi789" in text
    assert "dose is wrong" in text
    # Each deck appears once, as a heading, with its cards under it.
    assert text.count("Pharmacology") == 1


def test_build_feedback_digest_empty_is_empty_string():
    # Lets the caller treat "" as "nothing to send" without a second check.
    assert logic.build_feedback_digest([]) == ""


def test_build_feedback_digest_is_plain_text_not_html():
    text = logic.build_feedback_digest([
        {"deck": "D", "front": "SpO<sub>2</sub> &lt;94%", "guid": "g", "note": "x"}])
    # Fronts are stored as HTML; the digest gets pasted into a plain text thread, so
    # tags come out and entities are decoded. A stripped tag leaves a space behind
    # (plain_text's rule, shared with note_display_label) rather than joining words.
    assert "<sub>" not in text
    assert "&lt;" not in text
    assert "SpO 2 <94%" in text


# ------------------------------------------------------------- apkg_models / templates
_BASIC_MODEL = {
    "name": "Study Deck - Basic",
    "css": ".card { color: black; }",
    "tmpls": [{"name": "Card 1", "qfmt": "{{Front}}", "afmt": "{{Back}}",
               "ord": 0, "did": None}],
    "flds": [{"name": "Front"}, {"name": "Back"}],
    "id": 123, "mod": 456,
}


def test_apkg_models_reads_name_css_templates(tmp_path):
    apkg = str(tmp_path / "deck.apkg")
    _make_mock_apkg(apkg, [(1, "g1", "F")], models={"123": _BASIC_MODEL})
    out = logic.apkg_models(apkg)
    assert out == {"Study Deck - Basic": {
        "css": ".card { color: black; }",
        "tmpls": [("Card 1", "{{Front}}", "{{Back}}")],
    }}


def test_model_shape_ignores_incidental_keys():
    # ids, mod times, and field lists must not make two otherwise-identical models
    # "differ" — fields are _ensure_notetypes' job, not the template comparison's.
    a = dict(_BASIC_MODEL)
    b = dict(_BASIC_MODEL, id=999, mod=1, flds=[{"name": "Front"}])
    assert logic.model_shape(a) == logic.model_shape(b)


def test_changed_templates_flags_css_and_template_edits():
    base = logic.model_shape(_BASIC_MODEL)
    css_changed = dict(base, css=".card { color: red; }")
    tmpl_changed = dict(base, tmpls=[("Card 1", "{{Front}}<hr>", "{{Back}}")])
    assert logic.changed_templates({"X": css_changed}, {"X": base}) == ["X"]
    assert logic.changed_templates({"X": tmpl_changed}, {"X": base}) == ["X"]
    assert logic.changed_templates({"X": base}, {"X": base}) == []


def test_changed_templates_skips_notetypes_the_collection_lacks():
    # A note type only the .apkg has isn't a template CHANGE — the import creates it
    # as-is, so there's nothing to reconcile or warn about.
    shape = logic.model_shape(_BASIC_MODEL)
    assert logic.changed_templates({"Only in apkg": shape}, {}) == []


# ------------------------------------------------------------------ write_personalized
def test_write_personalized_rewrites_only_remapped_guids(tmp_path):
    src = str(tmp_path / "src.apkg")
    out = str(tmp_path / "out.apkg")
    _make_mock_apkg(src, [
        (1, "original-guid-1", "Front 1"),
        (2, "original-guid-2", "Front 2"),
    ])
    logic.write_personalized(src, {1: "rewritten-guid"}, out)
    rows = {rid: (fields[0], guid) for rid, fields, guid in logic.apkg_notes(out)}
    assert rows[1] == ("Front 1", "rewritten-guid")   # remapped
    assert rows[2] == ("Front 2", "original-guid-2")  # untouched


def test_write_personalized_preserves_media_and_manifest(tmp_path):
    # A real .apkg carries a "media" manifest and numbered media blobs alongside
    # collection.anki2. write_personalized repackages the whole zip, so those must
    # survive untouched — otherwise images silently vanish from synced cards.
    src = str(tmp_path / "src.apkg")
    out = str(tmp_path / "out.apkg")
    _make_mock_apkg(src, [(1, "g1", "Front 1")])
    with zipfile.ZipFile(src, "a") as z:      # add media the way a genanki package would
        z.writestr("media", '{"0": "sample-a.jpg"}')
        z.writestr("0", b"\xff\xd8\xff-mock-jpeg-bytes")
    logic.write_personalized(src, {1: "new-guid"}, out)
    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())
        assert {"collection.anki2", "media", "0"} <= names
        assert z.read("media") == b'{"0": "sample-a.jpg"}'
        assert z.read("0") == b"\xff\xd8\xff-mock-jpeg-bytes"
    # and the GUID rewrite still took effect
    assert logic.apkg_notes(out)[0][2] == "new-guid"


def test_apkg_notes_splits_every_field_and_tolerates_no_mid_column(tmp_path):
    # flds packs every field joined by the separator. apkg_notes returns all of them so
    # callers can label an image card properly (its first field is an <img>, not a
    # prompt) rather than being stuck with field zero. The table here deliberately has
    # no `mid` column, which apkg_notes must not depend on; only apkg_note_details does.
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
    assert logic.apkg_notes(apkg) == [
        (1, ["The Front", "the back", "why text", "", "Tag", "dose", "notes"], "g1")]


# ------------------------------------------------ find_retired_in_collection
_LEDGER = {
    "Deck A": {
        "old1": {"identity": "bulky card one", "reason": "split",
                 "superseded_by": ["new1a", "new1b"]},
        "old2": {"identity": "reworded card two", "reason": "reworded",
                 "superseded_by": ["new2"]},
    },
    "Deck B": {
        "old3": {"identity": "removed card three", "reason": "deleted",
                 "superseded_by": []},
    },
}


def test_find_retired_returns_only_cards_she_has():
    # She has old1 and old3 (retired) plus new1a (a replacement) and her own card.
    her = {"old1", "old3", "new1a", "mine"}
    found = logic.find_retired_in_collection(_LEDGER, her)
    guids = {r["guid"] for r in found}
    assert guids == {"old1", "old3"}   # only retired cards she still holds


def test_find_retired_counts_present_replacements():
    her = {"old1", "new1a"}            # one of old1's two replacements is present
    (r,) = logic.find_retired_in_collection(_LEDGER, her)
    assert r["guid"] == "old1"
    assert r["superseded_by"] == ["new1a", "new1b"]
    assert r["replacements_present"] == 1


def test_find_retired_carries_deck_reason_identity():
    (r,) = logic.find_retired_in_collection(_LEDGER, {"old2"})
    assert (r["deck"], r["reason"], r["identity"]) == (
        "Deck A", "reworded", "reworded card two")


def test_find_retired_empty_when_nothing_matches():
    assert logic.find_retired_in_collection(_LEDGER, {"mine", "new1a"}) == []
    assert logic.find_retired_in_collection({}, {"old1"}) == []
    assert logic.find_retired_in_collection(None, {"old1"}) == []


def test_find_retired_sorted_by_deck_then_identity():
    her = {"old1", "old2", "old3"}
    found = logic.find_retired_in_collection(_LEDGER, her)
    assert [r["identity"] for r in found] == [
        "bulky card one", "reworded card two", "removed card three"]


def test_find_retired_falls_back_to_front_when_her_guid_drifted():
    # Her copy predates the ledger's GUID (an id_seed change), so a GUID-only
    # match misses it entirely and the card lingers forever.
    her = {"hers1"}
    front_map = {"bulky card one": "hers1"}
    (r,) = logic.find_retired_in_collection(_LEDGER, her, front_map)
    assert r["guid"] == "hers1"        # HER guid, so the caller can find the note
    assert r["identity"] == "bulky card one"


def test_find_retired_prefers_guid_match_over_front():
    # Both signals available: the GUID is the stronger one and wins.
    her = {"old1", "hers1"}
    front_map = {"bulky card one": "hers1"}
    (r,) = logic.find_retired_in_collection(_LEDGER, her, front_map)
    assert r["guid"] == "old1"


def test_find_retired_front_fallback_prefers_recorded_front_over_identity():
    ledger = {"Deck A": {"old1": {"identity": "the front it was frozen to",
                                  "front": "the front she actually sees",
                                  "reason": "split", "superseded_by": []}}}
    front_map = {"the front she actually sees": "hers1"}
    (r,) = logic.find_retired_in_collection(ledger, {"hers1"}, front_map)
    assert r["guid"] == "hers1"


def test_find_retired_front_fallback_matches_nothing_when_front_is_unknown():
    # An image note's identity is "image||answer", never a first field, so it
    # simply doesn't match. That's the pre-fallback behaviour, not a false positive.
    ledger = {"Deck A": {"old1": {"identity": "pic.jpg||Femoral block",
                                  "reason": "split", "superseded_by": []}}}
    assert logic.find_retired_in_collection(
        ledger, {"hers1"}, {"Femoral block": "hers1"}) == []


def test_find_retired_without_front_map_keeps_guid_only_behaviour():
    assert logic.find_retired_in_collection(_LEDGER, {"hers1"}) == []


# ---------------------------------------------------------- stranded rewords
_SUPERSEDED = {"old wording of a card": "new wording of a card",
               "another old wording": "another new wording"}


def test_stranded_pair_found_when_she_holds_both_wordings():
    her = {"old wording of a card": "g_old", "new wording of a card": "g_new"}
    (p,) = logic.find_stranded_pairs(_SUPERSEDED, her)
    assert p == {"guid": "g_old", "front": "old wording of a card",
                 "successor_guid": "g_new", "successor_front": "new wording of a card"}


def test_stranded_skipped_when_she_holds_only_the_old_wording():
    # Not a stranding: the import's own front matching merges this one, and acting
    # here as well would fight that ladder.
    assert logic.find_stranded_pairs(_SUPERSEDED, {"old wording of a card": "g_old"}) == []


def test_stranded_skipped_when_she_holds_only_the_new_wording():
    # The normal case for anyone whose GUID matched: the reword landed in place.
    assert logic.find_stranded_pairs(_SUPERSEDED, {"new wording of a card": "g_new"}) == []


def test_stranded_skipped_when_both_wordings_are_the_same_note():
    her = {"old wording of a card": "g", "new wording of a card": "g"}
    assert logic.find_stranded_pairs(_SUPERSEDED, her) == []


def test_stranded_empty_inputs():
    assert logic.find_stranded_pairs({}, {"x": "g"}) == []
    assert logic.find_stranded_pairs(None, {"x": "g"}) == []
    assert logic.find_stranded_pairs(_SUPERSEDED, None) == []


def test_stranded_sorted_by_front():
    her = {f: f"g{i}" for i, f in enumerate(
        ["old wording of a card", "new wording of a card",
         "another old wording", "another new wording"])}
    assert [p["front"] for p in logic.find_stranded_pairs(_SUPERSEDED, her)] == [
        "another old wording", "old wording of a card"]


# --------------------------------------------------------------- deck moves
_MOVES = {
    "g1": {"from": "Pharm::Local Anesthetics", "to": "Regional::Local Anesthetics"},
    "g2": {"from": "Pharm::Vaporizers", "to": "Random Facts::Vaporizers"},
}


def test_deck_move_applies_when_card_still_at_recorded_from():
    her_deck = {"g1": "Pharm::Local Anesthetics"}
    (m,) = logic.find_deck_moves_needed(_MOVES, her_deck)
    assert m == {"guid": "g1", "from": "Pharm::Local Anesthetics",
                 "to": "Regional::Local Anesthetics"}


def test_deck_move_skipped_once_she_already_reconciled():
    # Her card is already at `to` — a previous reconcile already moved it.
    her_deck = {"g1": "Regional::Local Anesthetics"}
    assert logic.find_deck_moves_needed(_MOVES, her_deck) == []


def test_deck_move_skipped_when_she_filed_it_elsewhere_herself():
    # Not at `from` and not at `to` — her own organization, never overridden.
    her_deck = {"g1": "My Own Custom Deck"}
    assert logic.find_deck_moves_needed(_MOVES, her_deck) == []


def test_deck_move_skipped_when_note_missing_from_her_collection():
    assert logic.find_deck_moves_needed(_MOVES, {}) == []


def test_deck_moves_sorted_by_to_then_from():
    her_deck = {"g1": "Pharm::Local Anesthetics", "g2": "Pharm::Vaporizers"}
    found = logic.find_deck_moves_needed(_MOVES, her_deck)
    assert [m["guid"] for m in found] == ["g2", "g1"]   # "Random..." < "Regional..."


# ------------------------------------------------------- protected-field carryover
def test_carry_over_fills_blank_target_field():
    saved = {"Notes": "her mnemonic"}
    assert logic.fields_to_carry_over(saved, {"Notes": ""}) == {"Notes": "her mnemonic"}


def test_carry_over_never_overwrites_existing_target_text():
    saved = {"Notes": "old mnemonic"}
    current = {"Notes": "something she already wrote on the new card"}
    assert logic.fields_to_carry_over(saved, current) == {}


def test_carry_over_handles_whitespace_only_target_as_blank():
    saved = {"Notes": "her mnemonic"}
    assert logic.fields_to_carry_over(saved, {"Notes": "   "}) == {"Notes": "her mnemonic"}


def test_carry_over_only_touches_fields_with_saved_content():
    saved = {"Notes": "text", "Dosing": ""}
    assert logic.fields_to_carry_over(saved, {"Notes": "", "Dosing": ""}) == {
        "Notes": "text"}


# ------------------------------------------------------- night mode image css
def test_night_mode_image_css_enabled_returns_dimming_rule():
    css = logic.night_mode_image_css(True)
    assert ".nightMode img" in css
    assert "brightness(0.7)" in css
    assert "contrast(0.92)" in css
    assert css.startswith("<style>") and css.endswith("</style>")


def test_night_mode_image_css_disabled_returns_empty_string():
    assert logic.night_mode_image_css(False) == ""


# ------------------------------------------------------- duplicate grouping
def test_find_duplicate_groups_ignores_notes_with_no_duplicate():
    her_notes = [{"guid": "g1", "nid": 1, "model": "Basic", "front": "unique front",
                  "reps": 0, "deck": "Foo"}]
    assert logic.find_duplicate_groups(her_notes, []) == []


def test_find_duplicate_groups_prefers_the_copy_with_more_reviews():
    her_notes = [
        {"guid": "old", "nid": 1, "model": "Basic", "front": "dup front",
         "reps": 3, "deck": "Old::Path"},
        {"guid": "new", "nid": 2, "model": "Basic", "front": "dup front",
         "reps": 0, "deck": "New::Path"},
    ]
    groups = logic.find_duplicate_groups(her_notes, [])
    assert len(groups) == 1
    assert groups[0]["keep"]["guid"] == "old"
    assert [a["guid"] for a in groups[0]["archive"]] == ["new"]


def test_find_duplicate_groups_breaks_a_review_tie_by_canonical_deck():
    her_notes = [
        {"guid": "old", "nid": 1, "model": "Basic", "front": "dup front", "reps": 0,
         "deck": "Intern Pearls::Intern Custom::Upper Extremity Nerve Blocks"},
        {"guid": "new", "nid": 2, "model": "Basic", "front": "dup front", "reps": 0,
         "deck": "Intern Pearls::Intern Custom::Regional::Upper Extremity Nerve Blocks"},
    ]
    canonical = ["Intern Pearls::Intern Custom::Regional::Upper Extremity Nerve Blocks"]
    groups = logic.find_duplicate_groups(her_notes, canonical)
    assert groups[0]["keep"]["guid"] == "new"


def test_find_duplicate_groups_treats_a_canonical_subdeck_as_canonical_too():
    her_notes = [
        {"guid": "old", "nid": 1, "model": "Basic", "front": "dup front", "reps": 0,
         "deck": "Intern Pearls::Intern Custom::Upper Extremity Nerve Blocks"},
        {"guid": "new", "nid": 2, "model": "Basic", "front": "dup front", "reps": 0,
         "deck": "Intern Pearls::Intern Custom::Regional::Upper Extremity Nerve Blocks::3. The Blocks"},
    ]
    canonical = ["Intern Pearls::Intern Custom::Regional::Upper Extremity Nerve Blocks"]
    groups = logic.find_duplicate_groups(her_notes, canonical)
    assert groups[0]["keep"]["guid"] == "new"


def test_find_duplicate_groups_breaks_a_full_tie_by_lower_note_id():
    her_notes = [
        {"guid": "b", "nid": 2, "model": "Basic", "front": "dup front",
         "reps": 0, "deck": "Same"},
        {"guid": "a", "nid": 1, "model": "Basic", "front": "dup front",
         "reps": 0, "deck": "Same"},
    ]
    groups = logic.find_duplicate_groups(her_notes, [])
    assert groups[0]["keep"]["guid"] == "a"


def test_find_duplicate_groups_does_not_cross_note_types():
    her_notes = [
        {"guid": "b1", "nid": 1, "model": "Basic", "front": "same text",
         "reps": 0, "deck": "Foo"},
        {"guid": "c1", "nid": 2, "model": "Cloze", "front": "same text",
         "reps": 0, "deck": "Foo"},
    ]
    assert logic.find_duplicate_groups(her_notes, []) == []


# ------------------------------------------------------- note display labels
def test_note_display_label_uses_the_first_text_field():
    assert logic.note_display_label(["What is MAP?", "back", "why"]) == "What is MAP?"


def test_note_display_label_strips_html_and_decodes_entities():
    assert logic.note_display_label(["<b>ACE&amp;ARB</b> effect"]) == "ACE&ARB effect"


def test_note_display_label_falls_through_an_image_field_to_the_prompt():
    fields = ["<img src='block.jpg'>", "Name this nerve block", "Answer"]
    assert logic.note_display_label(fields) == "Name this nerve block"


def test_note_display_label_uses_image_filename_when_no_text_anywhere():
    fields = ["<img src='decks/media/block-3.jpg'>", "", ""]
    assert logic.note_display_label(fields) == "block-3.jpg"


def test_note_display_label_truncates_a_long_field():
    label = logic.note_display_label(["x" * 200], max_len=20)
    assert len(label) == 20 and label.endswith("…")


def test_note_display_label_handles_a_note_with_nothing_to_show():
    assert logic.note_display_label(["", "   ", None]) == "(card)"


# ------------------------------------------------------- duplicate dialog rows
def _dup_group(label, keep_deck, arch_deck, keep_reps=0, arch_reps=0):
    return {
        "model": "M", "front": "f",
        "keep": {"guid": "k", "label": label, "deck": keep_deck, "reps": keep_reps},
        "archive": [{"guid": "a", "label": label, "deck": arch_deck, "reps": arch_reps}],
    }


def test_duplicate_dialog_rows_show_the_label_not_a_raw_image_tag():
    groups = [_dup_group("Name this nerve block",
                         "Deck::3. The Blocks", "Deck::3. The Blocks")]
    _heading, rows = logic.duplicate_dialog_rows(groups)
    assert rows[0]["label"] == "Name this nerve block"
    assert "<img" not in rows[0]["label"]


def test_duplicate_dialog_rows_read_as_a_copy_count_when_decks_match():
    groups = [_dup_group("Card A", "Deck::Blocks", "Deck::Blocks", keep_reps=3)]
    heading, rows = logic.duplicate_dialog_rows(groups)
    assert "2 copies in Blocks" in rows[0]["detail"]
    assert "duplicate copy of" in heading and "</b> card." in heading


def test_duplicate_dialog_rows_name_both_decks_when_they_differ():
    groups = [_dup_group("Card A", "Deck::New", "Deck::Old", keep_reps=5, arch_reps=1)]
    _heading, rows = logic.duplicate_dialog_rows(groups)
    assert "keeping New" in rows[0]["detail"] and "archiving Old" in rows[0]["detail"]


def test_duplicate_dialog_rows_escape_the_label():
    groups = [_dup_group("A <script> & B", "Deck::X", "Deck::X")]
    _heading, rows = logic.duplicate_dialog_rows(groups)
    assert "<script>" not in rows[0]["label"] and "&lt;script&gt;" in rows[0]["label"]


def test_duplicate_dialog_rows_pluralize_the_heading():
    groups = [_dup_group("A", "D::X", "D::X"), _dup_group("B", "D::Y", "D::Y")]
    heading, _rows = logic.duplicate_dialog_rows(groups)
    assert "duplicate copies of" in heading and "</b> cards." in heading


def test_duplicate_dialog_rows_carry_no_colour_of_their_own():
    """The detail is data, not markup: the caller wraps it in the live theme\'s muted
    colour, which is the only way a pure module can stay out of the palette."""
    groups = [_dup_group("Card A", "Deck::Blocks", "Deck::Blocks", keep_reps=3)]
    _heading, rows = logic.duplicate_dialog_rows(groups)
    assert "color" not in rows[0]["detail"] and "<span" not in rows[0]["detail"]


def test_find_duplicate_groups_sorted_by_model_then_front():
    her_notes = [
        {"guid": "z1", "nid": 1, "model": "Basic", "front": "zzz", "reps": 0, "deck": "Foo"},
        {"guid": "z2", "nid": 2, "model": "Basic", "front": "zzz", "reps": 0, "deck": "Foo"},
        {"guid": "a1", "nid": 3, "model": "Basic", "front": "aaa", "reps": 0, "deck": "Foo"},
        {"guid": "a2", "nid": 4, "model": "Basic", "front": "aaa", "reps": 0, "deck": "Foo"},
    ]
    groups = logic.find_duplicate_groups(her_notes, [])
    assert [g["front"] for g in groups] == ["aaa", "zzz"]


# ------------------------------------------------- manifest scope suggestions
def test_manifest_scope_suggestion_returns_both_when_they_differ():
    manifest = {"scope_tag": "CardioDeck", "export_deck": "Cardio"}
    assert logic.manifest_scope_suggestion(
        manifest, "InternPearls", "Intern Pearls::Intern Custom"
    ) == ("CardioDeck", "Cardio")


def test_manifest_scope_suggestion_skips_values_already_configured():
    manifest = {"scope_tag": "CardioDeck", "export_deck": "Cardio"}
    assert logic.manifest_scope_suggestion(
        manifest, "CardioDeck", "Cardio") == (None, None)


def test_manifest_scope_suggestion_offers_just_the_one_that_differs():
    manifest = {"scope_tag": "CardioDeck", "export_deck": "Cardio"}
    assert logic.manifest_scope_suggestion(
        manifest, "CardioDeck", "Old Deck") == (None, "Cardio")


def test_manifest_scope_suggestion_ignores_missing_or_junk_values():
    assert logic.manifest_scope_suggestion({}, "A", "B") == (None, None)
    junk = {"scope_tag": "", "export_deck": 7}
    assert logic.manifest_scope_suggestion(junk, "A", "B") == (None, None)


# ------------------------------------------------------------------- apkg_deck_names
def _legacy_apkg(path, deck_names, with_col=True):
    """An old-format .apkg: deck names live in col.decks as a JSON blob.

    Always includes an empty notes table, so the file also imports cleanly through
    a real (or mock) importer, not just through apkg_deck_names. with_col=False
    omits the col table (and its deck names) entirely, for building a file that
    imports fine but whose deck names can't be read.
    """
    import json as _json
    import sqlite3 as _sql
    import zipfile as _zip
    db = str(path) + ".anki2"
    con = _sql.connect(db)
    con.execute("create table notes (id integer primary key, guid text, flds text, "
                "tags text)")
    if with_col:
        con.execute("create table col (decks text)")
        decks = {str(i + 1): {"name": n} for i, n in enumerate(deck_names)}
        con.execute("insert into col (decks) values (?)", (_json.dumps(decks),))
    con.commit()
    con.close()
    with _zip.ZipFile(path, "w") as z:
        z.write(db, "collection.anki2")
    return path


def test_apkg_deck_names_reads_the_legacy_format(tmp_path):
    p = _legacy_apkg(tmp_path / "legacy.apkg",
                     ["Intern Pearls::Intern Custom::CA1 Handbook", "Default"])
    assert sorted(logic.apkg_deck_names(p)) == [
        "Default", "Intern Pearls::Intern Custom::CA1 Handbook"]


def test_apkg_deck_names_raises_on_a_file_it_cannot_read(tmp_path):
    import zipfile as _zip
    p = tmp_path / "junk.apkg"
    with _zip.ZipFile(p, "w") as z:
        z.writestr("nothing.txt", "not a collection")
    with pytest.raises(Exception):
        logic.apkg_deck_names(p)


class _FakeZstandardModule:
    """Stands in for the real zstandard package, which isn't installed here. Its
    copy_stream just copies bytes through, so the fixture's "compressed" member can be
    a plain SQLite file; the test is proving which member gets picked, not that zstd
    decompression itself works."""

    class ZstdDecompressor:
        def copy_stream(self, src, dst):
            dst.write(src.read())


def _newer_apkg(path, deck_names):
    """A newer-format .apkg: deck names live in collection.anki21b's decks table
    (path segments separated by \\x1f), shipped alongside the same near-empty legacy
    collection.anki2 stub _legacy_apkg produces, which must lose to the newer format.
    """
    import sqlite3 as _sql
    import zipfile as _zip

    newer_db = str(path) + ".anki21b.db"
    con = _sql.connect(newer_db)
    con.execute("create table decks (id integer primary key, name text)")
    con.executemany("insert into decks (id, name) values (?, ?)",
                     [(i + 1, n) for i, n in enumerate(deck_names)])
    con.commit()
    con.close()

    stub_apkg = _legacy_apkg(str(path) + ".stub", [])
    with _zip.ZipFile(stub_apkg) as sz:
        stub_db_bytes = sz.read("collection.anki2")
    stub_db = str(path) + ".stub.anki2"
    with open(stub_db, "wb") as f:
        f.write(stub_db_bytes)

    with _zip.ZipFile(path, "w") as z:
        z.write(newer_db, "collection.anki21b")
        z.write(stub_db, "collection.anki2")
    return path


def test_apkg_deck_names_prefers_the_newer_format_over_the_legacy_stub(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "zstandard", _FakeZstandardModule)
    p = _newer_apkg(tmp_path / "newer.apkg", ["Default", "Intern Pearls"])
    assert sorted(logic.apkg_deck_names(p)) == ["Default", "Intern Pearls"]


def test_a_newer_apkg_without_a_zstd_decoder_asks_for_a_legacy_reexport(tmp_path, monkeypatch):
    """Anki's own runtime ships no zstandard module, so inside real Anki the decode
    is impossible: the user must get the re-export message, not a bare ImportError."""
    monkeypatch.setitem(sys.modules, "zstandard", None)
    p = _newer_apkg(tmp_path / "newer_nozstd.apkg", ["Default"])
    with pytest.raises(RuntimeError, match="Support older Anki versions"):
        logic.apkg_deck_names(p)


def test_apkg_deck_names_converts_unit_separator_to_double_colon(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "zstandard", _FakeZstandardModule)
    p = _newer_apkg(tmp_path / "newer_sep.apkg",
                     ["Intern Pearls\x1fIntern Custom\x1fCA1 Handbook"])
    assert logic.apkg_deck_names(p) == [
        "Intern Pearls::Intern Custom::CA1 Handbook"]


# ------------------------------------------------- reading a modern-format .apkg
def _modern_apkg(path, notes, notetypes, deck_names=("Default",)):
    """A modern-format .apkg, the shape Anki exports today.

    Checked against a real 25.7.5 export before this fixture was written. The whole
    collection lives in a zstd-compressed collection.anki21b whose schema is not the
    legacy one: note types are split across a `notetypes` table (the name) and a `fields`
    table (one text row per field, ordered by `ord`), and col.models is left an empty
    string. CSS and template HTML are protobuf blobs in those tables' `config` columns,
    which is why apkg_models cannot read one.

    Beside it sits the near-empty collection.anki2 stub Anki ships for old clients,
    carrying nothing but the "Please update to the latest Anki version" placeholder note
    reproduced verbatim here. A reader that still picks the stub sees that one note and
    no note types at all, which is exactly the failure these tests pin down.

    `notes` is [(id, guid, [field values], notetype id)]; `notetypes` is
    {notetype id: (name, [field names])}.
    """
    import sqlite3 as _sql
    import zipfile as _zip

    db = str(path) + ".anki21b.db"
    con = _sql.connect(db)
    con.execute("create table col (id integer primary key, models text not null, "
                "decks text not null)")
    con.execute("insert into col (id, models, decks) values (1, '', '')")
    con.execute("create table notes (id integer primary key, guid text not null, "
                "mid integer not null, flds text not null)")
    con.execute("create table notetypes (id integer not null primary key, "
                "name text not null, config blob not null)")
    con.execute("create table fields (ntid integer not null, ord integer not null, "
                "name text not null, config blob not null, primary key (ntid, ord)) "
                "without rowid")
    con.execute("create table decks (id integer primary key not null, "
                "name text not null)")
    for nid, guid, fields, mid in notes:
        con.execute("insert into notes (id, guid, mid, flds) values (?, ?, ?, ?)",
                    (nid, guid, mid, logic.FS.join(fields)))
    for mid, (name, field_names) in notetypes.items():
        con.execute("insert into notetypes (id, name, config) values (?, ?, ?)",
                    (mid, name, b"\x1a\x08protobuf"))
        for ordinal, field_name in enumerate(field_names):
            con.execute("insert into fields (ntid, ord, name, config) "
                        "values (?, ?, ?, ?)", (mid, ordinal, field_name, b""))
    for i, deck_name in enumerate(deck_names):
        con.execute("insert into decks (id, name) values (?, ?)", (i + 1, deck_name))
    con.commit()
    con.close()

    stub = str(path) + ".stub.anki2"
    scon = _sql.connect(stub)
    scon.execute("create table notes (id integer primary key, guid text, mid integer, "
                 "flds text)")
    scon.execute("insert into notes values (1785086149129, 'z,P#@w=ml[', "
                 "1785086149128, ?)",
                 ("Please update to the latest Anki version, then import the "
                  ".colpkg/.apkg file again." + logic.FS,))
    scon.execute("create table col (models text, decks text)")
    scon.execute("insert into col (models, decks) values ('{}', '{}')")
    scon.commit()
    scon.close()

    with _zip.ZipFile(path, "w") as z:
        z.write(db, "collection.anki21b")
        z.write(stub, "collection.anki2")
    return str(path)


_MODERN_NOTES = [
    (11, "guid-a", ["Front A", "Back A", "her own annotation"], 77),
    (12, "guid-b", ['<img src="x.jpg">', "Name this", ""], 88),
]
_MODERN_NOTETYPES = {77: ("Study Deck - Basic", ["Front", "Back", "Notes"]),
                     88: ("Study Deck - Image ID", ["Image", "Prompt", "Notes"])}


@pytest.fixture
def modern_apkg(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "zstandard", _FakeZstandardModule)
    return _modern_apkg(tmp_path / "modern.apkg", _MODERN_NOTES, _MODERN_NOTETYPES)


def test_apkg_notes_reads_the_real_notes_of_a_modern_package(modern_apkg):
    """The bug: apkg_notes only ever opened collection.anki2, so a package exported by
    current Anki yielded the stub's single placeholder note. Every match then failed,
    the importer overwrote every field including the protected ones, and the restore had
    no matched note to run on."""
    rows = logic.apkg_notes(modern_apkg)
    assert [(rid, fields[0], guid) for rid, fields, guid in rows] == [
        (11, "Front A", "guid-a"), (12, '<img src="x.jpg">', "guid-b")]
    assert rows[0][1] == ["Front A", "Back A", "her own annotation"]
    assert not any("Please update" in f for _r, fields, _g in rows for f in fields)


def test_remap_cards_matches_a_modern_package_against_her_collection(modern_apkg):
    """The consequence at the level the import actually depends on: with the stub being
    read, in_place was 0 and nothing was remapped, so a card she already had came in as
    a duplicate and her history stayed on the copy she stopped seeing."""
    remap, in_place, as_new, new_notes, matched = logic.remap_cards(
        modern_apkg, {"Front A": "her-guid-a"}, {})
    assert remap == {11: "her-guid-a"}
    assert (in_place, as_new) == (1, 1)
    assert [g for _rid, _f, g in new_notes] == ["guid-b"]
    assert matched == [(11, "guid-a", "her-guid-a")]


def test_apkg_note_details_labels_a_modern_package_from_its_notetype_tables(modern_apkg):
    """col.models is empty in this format, so the labels have to come from the
    `notetypes`/`fields` tables. Both are plain text columns, so no protobuf is needed
    and the preview is as precisely labeled as a legacy package's."""
    basic, image = logic.apkg_note_details(modern_apkg)
    assert basic["notetype"] == "Study Deck - Basic"
    assert basic["fields"] == [("Front", "Front A"), ("Back", "Back A"),
                               ("Notes", "her own annotation")]
    assert image["notetype"] == "Study Deck - Image ID"
    assert image["fields"][1] == ("Prompt", "Name this")


def test_apkg_note_types_names_a_modern_packages_notetypes(modern_apkg):
    assert logic.apkg_note_types(modern_apkg) == {
        "guid-a": "Study Deck - Basic", "guid-b": "Study Deck - Image ID"}


def test_apkg_models_refuses_a_modern_package_instead_of_reporting_no_templates(
        modern_apkg):
    """CSS and template HTML live in protobuf blobs there, so they genuinely cannot be
    read. Returning {} would read as "no template differs" and silently drop a card
    design change, so this raises with the one instruction that fixes it."""
    with pytest.raises(RuntimeError, match="Support older Anki versions"):
        logic.apkg_models(modern_apkg)


def test_write_personalized_refuses_a_modern_package(tmp_path):
    """Anki reads the anki21b, so rewriting guids into the stub beside it would apply
    none of them while remap_cards went on reporting matched counts: every front-matched
    card imports as a duplicate. Re-compressing the anki21b needs zstd, which nothing
    here has. The guard fires on the zip member alone, before any decode, so it does not
    even need zstandard importable."""
    src = _modern_apkg(tmp_path / "src.apkg", _MODERN_NOTES, _MODERN_NOTETYPES)
    with pytest.raises(RuntimeError, match="Support older Anki versions"):
        logic.write_personalized(src, {11: "her-guid-a"}, str(tmp_path / "out.apkg"))


def test_modern_readers_still_reject_a_zip_with_no_collection(tmp_path):
    bogus = str(tmp_path / "bogus.apkg")
    with zipfile.ZipFile(bogus, "w") as z:
        z.writestr("nothing.txt", "not a collection")
    for read in (logic.apkg_notes, logic.apkg_note_details, logic.apkg_note_types,
                 logic.apkg_models, logic.apkg_deck_names):
        with pytest.raises(RuntimeError):
            read(bogus)


# ---------------------------------------------------------------- manifest_decks_for
MANIFEST = ["A::Regional::Upper Extremity Nerve Blocks", "A::Regional", "A::CA1 Handbook"]


def test_manifest_decks_for_matches_an_exact_name():
    assert logic.manifest_decks_for(["A::CA1 Handbook"], MANIFEST) == ["A::CA1 Handbook"]


def test_manifest_decks_for_matches_a_subdeck_to_its_parent_spec():
    # A spec's deck_name is the parent path; cards live in deck_name::<subdeck>.
    assert logic.manifest_decks_for(
        ["A::CA1 Handbook::01. Foundational Concepts"], MANIFEST) == ["A::CA1 Handbook"]


def test_manifest_decks_for_longest_prefix_wins():
    # Both "A::Regional" and the nerve blocks deck prefix this; only the closest owns it.
    assert logic.manifest_decks_for(
        ["A::Regional::Upper Extremity Nerve Blocks::3. The Blocks"], MANIFEST) == [
            "A::Regional::Upper Extremity Nerve Blocks"]


def test_manifest_decks_for_ignores_unrelated_decks():
    assert logic.manifest_decks_for(["Default", "Someone Else::Deck"], MANIFEST) == []


def test_manifest_decks_for_requires_a_segment_boundary_not_just_a_string_prefix():
    # "A::Reg" is a string prefix of "A::Regional::X" but not a "::"-segment prefix.
    assert logic.manifest_decks_for(["A::Regional::X"], ["A::Reg"]) == []


# ------------------------------------------------- empty-card selection
def test_select_empty_cards_keeps_only_scoped_notes():
    from internpearls.logic import select_empty_cards
    report = [{"nid": 1, "card_ids": [11, 12], "will_delete_note": False},
              {"nid": 2, "card_ids": [21], "will_delete_note": False}]
    removable, skipped = select_empty_cards(report, {1})
    assert [r["nid"] for r in removable] == [1]
    assert skipped == []


def test_select_empty_cards_refuses_a_note_that_would_be_deleted():
    from internpearls.logic import select_empty_cards
    report = [{"nid": 1, "card_ids": [11], "will_delete_note": True}]
    removable, skipped = select_empty_cards(report, {1})
    assert removable == []
    assert [s["nid"] for s in skipped] == [1]


def test_empty_cards_dialog_rows_name_the_missing_deletions():
    from internpearls.logic import empty_cards_dialog_rows
    heading, rows, _tail = empty_cards_dialog_rows(
        [{"nid": 1, "card_ids": [11, 12], "label": "a regrouped card", "ords": [3, 4]}])
    assert rows[0]["label"] == "a regrouped card" and rows[0]["gone"] == "c3, c4"
    assert "<b>2</b> empty cards" in heading and "<b>1</b> note" in heading


def test_empty_cards_dialog_rows_escape_the_label():
    from internpearls.logic import empty_cards_dialog_rows
    _heading, rows, _tail = empty_cards_dialog_rows(
        [{"nid": 1, "card_ids": [11], "label": "SpO<sub>2</sub> & <b>x</b>",
          "ords": [2]}])
    assert "&lt;sub&gt;" in rows[0]["label"] and "&amp;" in rows[0]["label"]


def test_empty_cards_dialog_rows_carry_no_colour_of_their_own():
    """The deletion numbers are the row's trailing column, which widgets.simple_row
    draws in the live theme's muted colour: nothing here may name a colour at all."""
    from internpearls.logic import empty_cards_dialog_rows
    _heading, rows, _tail = empty_cards_dialog_rows(
        [{"nid": 1, "card_ids": [11, 12], "label": "a regrouped card", "ords": [3, 4]}])
    assert "color" not in rows[0]["gone"] and "<span" not in rows[0]["gone"]


def test_empty_cards_dialog_rows_report_the_notes_left_alone():
    """A note whose every card is empty is reported, never touched: removing its cards
    would delete the note. That sentence closes the confirmation."""
    from internpearls.logic import empty_cards_dialog_rows
    _heading, _rows, tail = empty_cards_dialog_rows(
        [{"nid": 1, "card_ids": [11], "label": "a regrouped card", "ords": [3]}],
        skipped=2)
    assert "<b>2 notes</b>" in tail and "delete the note itself" in tail
    assert not tail.startswith("<br>"), "the tail is its own paragraph now"


# ------------------------------------------------------------------ decline support
def test_note_fields_hash_is_stable_and_field_sensitive():
    a = logic.note_fields_hash(["front", "back"])
    assert a == logic.note_fields_hash(["front", "back"])
    assert a != logic.note_fields_hash(["front", "different back"])
    assert len(a) == 16
    int(a, 16)   # hex


def test_write_personalized_drops_declined_notes(tmp_path):
    src, out = str(tmp_path / "src.apkg"), str(tmp_path / "out.apkg")
    _make_mock_apkg(src, [(1, "guid-a", "front a"), (2, "guid-b", "front b")])
    logic.write_personalized(src, {}, out, drop={2})
    kept = logic.apkg_notes(out)
    assert [g for _, _, g in kept] == ["guid-a"]


def test_write_personalized_drop_wins_over_remap(tmp_path):
    src, out = str(tmp_path / "src.apkg"), str(tmp_path / "out.apkg")
    _make_mock_apkg(src, [(1, "guid-a", "front a")])
    logic.write_personalized(src, {1: "her-guid"}, out, drop={1})
    assert logic.apkg_notes(out) == []


def test_write_personalized_drop_removes_cards_rows_too(tmp_path):
    src, out = str(tmp_path / "src.apkg"), str(tmp_path / "out.apkg")
    _make_mock_apkg(src, [(1, "guid-a", "front a"), (2, "guid-b", "front b")])
    # Graft a cards table onto the mock package, two cards per note (cloze siblings).
    work = str(tmp_path / "work")
    with zipfile.ZipFile(src) as z:
        z.extractall(work)
    con = sqlite3.connect(os.path.join(work, "collection.anki2"))
    con.execute("create table cards (id integer primary key, nid integer)")
    con.executemany("insert into cards (id, nid) values (?, ?)",
                    [(10, 1), (11, 1), (20, 2), (21, 2)])
    con.commit()
    con.close()
    with zipfile.ZipFile(src, "w", zipfile.ZIP_DEFLATED) as z:
        for f in os.listdir(work):
            z.write(os.path.join(work, f), f)
    logic.write_personalized(src, {}, out, drop={1})
    with zipfile.ZipFile(out) as z:
        z.extract("collection.anki2", str(tmp_path / "check"))
    con = sqlite3.connect(str(tmp_path / "check" / "collection.anki2"))
    assert [r[0] for r in con.execute("select nid from cards order by id")] == [2, 2]
    con.close()


def test_declined_drop_filters_every_match_path_and_corrects_the_counts(tmp_path):
    """One note per branch: untouched (guid-a), declined after a front-text remap
    (guid-b), declined while importing as new (guid-c), and declined on a direct GUID
    match (her-guid-d). The drop wins over the remap, and each dropped note leaves
    whichever count it was sitting in."""
    src = str(tmp_path / "src.apkg")
    _make_mock_apkg(src, [(1, "guid-a", "front a"), (2, "guid-b", "front b"),
                          (3, "guid-c", "front c"), (4, "her-guid-d", "front d")])
    her = {"front b": "her-guid-b", "front d": "her-guid-d"}
    remap, in_place, as_new, _new, _matched = logic.remap_cards(src, her, {})
    assert (in_place, as_new) == (2, 2)

    drop, touched, in_place, as_new = logic.declined_drop(
        src, remap, her, {"her-guid-b", "guid-c", "her-guid-d"}, in_place, as_new)

    assert drop == {2, 3, 4}
    assert touched == {"guid-a"}
    assert (in_place, as_new) == (0, 1)
    assert 2 not in remap


def test_declined_guids_is_every_key_whatever_its_entry_says():
    """The one definition the import, the preview counts and the conversion plan all
    read, so they cannot disagree about which cards a decline suppresses. Membership is
    what declined_drop tests, so a garbage entry counts and a state is never consulted:
    filtering on state alone is how a standing skip/keep was counted as pending and then
    dropped from the import."""
    reg = {"g-skip": {"state": "skip"}, "g-keep": {"state": "keep"},
           "g-never": {"state": "never"}, "g-garbage": "not a dict"}
    assert logic.declined_guids(reg) == {"g-skip", "g-keep", "g-never", "g-garbage"}
    assert logic.declined_guids({}) == set()
    assert logic.declined_guids(None) == set()


def test_prune_declined_drops_retired_and_vanished_entries():
    reg = {
        "g-retired":  {"state": "never", "deck": "IP::A", "front": "x"},
        "g-vanished": {"state": "skip",  "deck": "IP::A", "front": "y"},
        "g-alive":    {"state": "skip",  "deck": "IP::A", "front": "z"},
        "g-unseen":   {"state": "keep",  "deck": "IP::B", "front": "w"},
    }
    changed = logic.prune_declined(
        reg, retired_guids={"g-retired"},
        seen={"IP::A": {"g-alive"}})   # IP::B was not downloaded this run
    assert changed is True
    assert set(reg) == {"g-alive", "g-unseen"}


def test_prune_declined_reports_no_change():
    reg = {"g": {"state": "skip", "deck": "IP::A", "front": "z"}}
    assert logic.prune_declined(reg, set(), {"IP::A": {"g"}}) is False


def test_prune_declined_survives_a_non_dict_entry():
    """A hand-edited declined.json can hold a garbage value for a guid. It must not
    crash the prune, and it is left alone (for the Declined dialog's Other/garbage
    handling) unless its guid is actually retired, in which case it prunes like any
    other entry."""
    reg = {"g-garbage": "not a dict", "g-retired-garbage": "also not a dict",
           "g-alive": {"state": "skip", "deck": "IP::A", "front": "z"}}
    changed = logic.prune_declined(
        reg, retired_guids={"g-retired-garbage"}, seen={"IP::A": {"g-alive"}})
    assert changed is True
    assert set(reg) == {"g-garbage", "g-alive"}


# ----------------------------------------------------------------- change_notes_for
def test_change_notes_for_matches_hash():
    fields = ["Front", "Back"]
    h = logic.note_fields_hash(fields)
    notes = {"g1": [{"kind": "feedback", "note": "older", "hash": h},
                    {"kind": "feedback", "note": "newer", "hash": h}]}
    got = logic.change_notes_for(notes, "g1", fields)
    assert [e["note"] for e in got] == ["newer", "older"]


def test_change_notes_for_drops_stale_hash():
    notes = {"g1": [{"kind": "feedback", "note": "old", "hash": "0" * 16}]}
    assert logic.change_notes_for(notes, "g1", ["Front", "Back"]) == []


def test_change_notes_for_tolerates_junk():
    fields = ["F"]
    h = logic.note_fields_hash(fields)
    notes = {"g1": ["not a dict", {"hash": h}, {"note": "", "hash": h},
                    {"note": "ok", "hash": h}]}
    assert [e["note"] for e in logic.change_notes_for(notes, "g1", fields)] == ["ok"]
    assert logic.change_notes_for(None, "g1", fields) == []
    assert logic.change_notes_for({"g1": "junk"}, "g1", fields) == []
    assert logic.change_notes_for(notes, "missing", fields) == []


def test_change_notes_for_drops_non_string_notes():
    fields = ["F"]
    h = logic.note_fields_hash(fields)
    notes = {"g1": [{"note": 12345, "hash": h}, {"note": None, "hash": h},
                    {"note": ["x"], "hash": h}, {"note": {"k": "v"}, "hash": h},
                    {"note": "ok", "hash": h}]}
    assert [e["note"] for e in logic.change_notes_for(notes, "g1", fields)] == ["ok"]


# ---------------------------------------------------------------- source_label_for
def test_source_label_for_returns_the_decks_own_string():
    assert logic.source_label_for({"g1": "[T10Q2]"}, "g1") == "[T10Q2]"
    assert logic.source_label_for({"g1": " [T10Q2] [T4Q11] "}, "g1") == "[T10Q2] [T4Q11]"


def test_source_label_for_is_absent_rather_than_empty_when_there_is_none():
    assert logic.source_label_for({"g1": "[T10Q2]"}, "g2") == ""
    assert logic.source_label_for({}, "g1") == ""
    assert logic.source_label_for(None, "g1") == ""


def test_source_label_for_tolerates_junk():
    """One bad entry in a fetched manifest costs that card its label and nothing else.
    None of these may raise."""
    assert logic.source_label_for({"g1": 12345}, "g1") == ""
    assert logic.source_label_for({"g1": None}, "g1") == ""
    assert logic.source_label_for({"g1": ["[T1Q1]"]}, "g1") == ""
    assert logic.source_label_for({"g1": "   "}, "g1") == ""
    assert logic.source_label_for("not a dict", "g1") == ""


def test_source_label_for_clips_a_runaway_label():
    got = logic.source_label_for({"g1": "[T1Q1] " * 200}, "g1")
    assert len(got) == logic.SOURCE_LABEL_MAX


# ---------------------------------------------------------------- merged_word_diff
def test_merged_word_diff_marks_a_replaced_value_and_keeps_shared_words_once():
    got = logic.merged_word_diff("Give 1 mg/kg over 10 minutes.",
                                 "Give 1.5 mg/kg over 2 minutes.")
    assert got == [("equal", "Give"), ("removed", "1"), ("added", "1.5"),
                   ("equal", "mg/kg over"), ("removed", "10"), ("added", "2"),
                   ("equal", "minutes.")]


def test_merged_word_diff_marks_a_dropped_leading_clause():
    got = logic.merged_word_diff("Old clause first, then the shared tail.",
                                 "then the shared tail.")
    assert got[0] == ("removed", "Old clause first,")
    assert ("equal", "then the shared tail.") in got
    assert not [seg for seg in got if seg[0] == "added"]


def test_merged_word_diff_handles_an_empty_side():
    assert logic.merged_word_diff("", "all new words") == [("added", "all new words")]
    assert logic.merged_word_diff("all gone words", "") == [("removed", "all gone words")]
    assert logic.merged_word_diff("", "") == []


# ------------------------------------------------------------ cloze_answer_changes
def test_cloze_answer_changes_names_a_dropped_blank():
    old = "A {{c1::pencil-point}} tip is best at {{c2::25 to 27 gauge}}."
    new = "A pencil-point tip is best at {{c2::25 to 27 gauge}}."
    assert logic.cloze_answer_changes(old, new) == (["pencil-point"], [])


def test_cloze_answer_changes_names_a_new_blank():
    old = "A pencil-point tip is best at {{c1::25 to 27 gauge}}."
    new = "A {{c2::pencil-point}} tip is best at {{c1::25 to 27 gauge}}."
    assert logic.cloze_answer_changes(old, new) == ([], ["pencil-point"])


def test_cloze_answer_changes_refuses_when_the_words_changed_too():
    """A reworded sentence has no blanks-only story to tell; None sends the caller to
    the verbatim fallback, which is the only honest rendering there."""
    old = "An older sentence blanking {{c1::a value}}."
    new = "A newer sentence blanking {{c1::a value}}."
    assert logic.cloze_answer_changes(old, new) is None


def test_cloze_answer_changes_treats_a_pure_regroup_as_empty_lists():
    old = "{{c1::alpha}} and {{c1::beta}} together."
    new = "{{c1::alpha}} and {{c2::beta}} together."
    assert logic.cloze_answer_changes(old, new) == ([], [])


def test_cloze_answer_changes_compares_repeated_answers_as_a_multiset():
    old = "{{c1::salt}} before and {{c2::salt}} after."
    new = "{{c1::salt}} before and salt after."
    assert logic.cloze_answer_changes(old, new) == (["salt"], [])


def test_cloze_answer_changes_ignores_the_hint_half_of_a_deletion():
    old = "Best at {{c1::25 gauge::a size}}."
    new = "Best at {{c1::25 gauge}}."
    assert logic.cloze_answer_changes(old, new) == ([], [])
