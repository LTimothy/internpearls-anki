"""Tests for internpearls/logic.py.

Pure Python, no Anki/aqt install required: run with `pytest` from the addon/ directory.
These build a minimal mock .apkg (a zip with just a "notes" table) since that's all the
logic under test ever reads or writes; the many other tables a real Anki collection
has are irrelevant to this code.
"""
import os
import sqlite3
import sys
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "internpearls"))
import logic  # noqa: E402


def _make_mock_apkg(path, notes, models=None):
    """notes: list of (id, guid, front) tuples. Writes a zip with collection.anki2.
    `models`, if given, is the col.models JSON value (a {model_id: model_dict} map,
    the legacy format genanki writes) so apkg_models has something to read."""
    db_path = path + ".tmp.db"
    if os.path.exists(db_path):
        os.remove(db_path)
    con = sqlite3.connect(db_path)
    con.execute("create table notes (id integer primary key, guid text, flds text)")
    for nid, guid, front in notes:
        flds = front + logic.FS + "back text"
        con.execute("insert into notes (id, guid, flds) values (?, ?, ?)",
                    (nid, guid, flds))
    if models is not None:
        import json
        con.execute("create table col (models text)")
        con.execute("insert into col (models) values (?)", (json.dumps(models),))
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


def test_bullets_uncapped_shows_everything_by_default():
    html = logic.bullets([str(i) for i in range(50)])
    assert html.count("<li>") == 50
    assert "more" not in html


def test_bullets_cap_truncates_and_summarizes():
    html = logic.bullets([str(i) for i in range(50)], cap=10)
    assert html.count("<li>") == 11   # 10 shown + 1 summary line
    assert "<li>0</li>" in html and "<li>9</li>" in html
    assert "<li>10</li>" not in html
    assert "...and 40 more" in html


def test_bullets_cap_no_op_when_under_the_limit():
    html = logic.bullets(["a", "b"], cap=10)
    assert html == "<ul style='margin:4px 0 4px 0;'><li>a</li><li>b</li></ul>"


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
def test_apkg_notes_reads_id_front_guid(tmp_path):
    apkg = str(tmp_path / "deck.apkg")
    _make_mock_apkg(apkg, [(1, "guid-a", "Front A"), (2, "guid-b", "Front B")])
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

    remap, in_place, as_new = logic.remap_cards(apkg, her, aliases)

    assert in_place == 2          # "Matches directly" and "New wording" (via alias)
    assert as_new == 1            # "Never seen before" has no match anywhere
    # GUIDs get rewritten to match her existing cards so Anki's importer updates in
    # place instead of creating duplicates:
    assert remap == {1: "her-guid-direct", 2: "her-guid-aliased"}


def test_remap_cards_no_matches_are_all_new(tmp_path):
    apkg = str(tmp_path / "deck.apkg")
    _make_mock_apkg(apkg, [(1, "g1", "Nobody has this")])
    remap, in_place, as_new = logic.remap_cards(apkg, her={}, aliases={})
    assert (remap, in_place, as_new) == ({}, 0, 1)


def test_remap_cards_guid_already_matches_needs_no_rewrite(tmp_path):
    # If the incoming note already carries the learner's GUID (e.g. re-syncing an
    # unchanged deck), it counts as in-place but must NOT be added to remap — rewriting
    # it to itself is pointless churn.
    apkg = str(tmp_path / "deck.apkg")
    _make_mock_apkg(apkg, [(1, "shared-guid", "Same front")])
    her = {"Same front": "shared-guid"}
    remap, in_place, as_new = logic.remap_cards(apkg, her, aliases={})
    assert (remap, in_place, as_new) == ({}, 1, 0)


def test_remap_cards_alias_target_also_missing_is_new(tmp_path):
    # An alias records a rename, but if the learner's collection has NEITHER the new
    # wording nor the old one the alias points to, the card is genuinely new to her.
    apkg = str(tmp_path / "deck.apkg")
    _make_mock_apkg(apkg, [(1, "g1", "New wording")])
    aliases = {"New wording": "Old wording"}   # but "Old wording" isn't in her map
    remap, in_place, as_new = logic.remap_cards(apkg, her={}, aliases=aliases)
    assert (remap, in_place, as_new) == ({}, 0, 1)


def test_remap_cards_matches_by_guid_before_front(tmp_path):
    # Stable-id builds keep a card's GUID through a front rewording. A learner whose
    # card already carries the incoming GUID must match in place with NO remap entry,
    # even when the front text differs and no alias exists — this is exactly the
    # "reworded twice, alias only bridges one hop" case that used to strand history.
    apkg = str(tmp_path / "deck.apkg")
    _make_mock_apkg(apkg, [(1, "stable-guid", "Reworded front, take three")])
    her = {"Original front wording": "stable-guid"}
    remap, in_place, as_new = logic.remap_cards(apkg, her, aliases={})
    assert (remap, in_place, as_new) == ({}, 1, 0)


def test_remap_cards_guid_match_wins_over_front_match(tmp_path):
    # If the incoming GUID already belongs to her card A, a coincidental front-text
    # match against her card B must not override it: GUID is the deliberate identity.
    apkg = str(tmp_path / "deck.apkg")
    _make_mock_apkg(apkg, [(1, "guid-a", "Front of B")])
    her = {"Front of A": "guid-a", "Front of B": "guid-b"}
    remap, in_place, as_new = logic.remap_cards(apkg, her, aliases={})
    assert (remap, in_place, as_new) == ({}, 1, 0)


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
    rows = {rid: (front, guid) for rid, front, guid in logic.apkg_notes(out)}
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
        z.writestr("media", '{"0": "femoral.jpg"}')
        z.writestr("0", b"\xff\xd8\xff-mock-jpeg-bytes")
    logic.write_personalized(src, {1: "new-guid"}, out)
    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())
        assert {"collection.anki2", "media", "0"} <= names
        assert z.read("media") == b'{"0": "femoral.jpg"}'
        assert z.read("0") == b"\xff\xd8\xff-mock-jpeg-bytes"
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
