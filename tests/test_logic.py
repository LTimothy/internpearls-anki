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

    remap, in_place, as_new, new_notes = logic.remap_cards(apkg, her, aliases)

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
    remap, in_place, as_new, new_notes = logic.remap_cards(apkg, her={}, aliases={})
    assert (remap, in_place, as_new) == ({}, 0, 1)
    assert new_notes == [(1, ["Nobody has this", "back text"], "g1")]


def test_remap_cards_guid_already_matches_needs_no_rewrite(tmp_path):
    # If the incoming note already carries the learner's GUID (e.g. re-syncing an
    # unchanged deck), it counts as in-place but must NOT be added to remap — rewriting
    # it to itself is pointless churn.
    apkg = str(tmp_path / "deck.apkg")
    _make_mock_apkg(apkg, [(1, "shared-guid", "Same front")])
    her = {"Same front": "shared-guid"}
    remap, in_place, as_new, new_notes = logic.remap_cards(apkg, her, aliases={})
    assert (remap, in_place, as_new, new_notes) == ({}, 1, 0, [])


def test_remap_cards_alias_target_also_missing_is_new(tmp_path):
    # An alias records a rename, but if the learner's collection has NEITHER the new
    # wording nor the old one the alias points to, the card is genuinely new to her.
    apkg = str(tmp_path / "deck.apkg")
    _make_mock_apkg(apkg, [(1, "g1", "New wording")])
    aliases = {"New wording": "Old wording"}   # but "Old wording" isn't in her map
    remap, in_place, as_new, new_notes = logic.remap_cards(apkg, her={}, aliases=aliases)
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
    remap, in_place, as_new, new_notes = logic.remap_cards(apkg, her, aliases={})
    assert (remap, in_place, as_new, new_notes) == ({}, 1, 0, [])


def test_remap_cards_guid_match_wins_over_front_match(tmp_path):
    # If the incoming GUID already belongs to her card A, a coincidental front-text
    # match against her card B must not override it: GUID is the deliberate identity.
    apkg = str(tmp_path / "deck.apkg")
    _make_mock_apkg(apkg, [(1, "guid-a", "Front of B")])
    her = {"Front of A": "guid-a", "Front of B": "guid-b"}
    remap, in_place, as_new, new_notes = logic.remap_cards(apkg, her, aliases={})
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
    _, _, as_new, new_notes = logic.remap_cards(
        apkg, her={"She has this": "g-known"}, aliases={})
    assert as_new == len(new_notes) == 2
    assert [rid for rid, _, _ in new_notes] == [2, 3]   # apkg order preserved


def test_remap_cards_new_notes_carries_every_field_for_image_cards(tmp_path):
    # An image note's first field is an <img> tag, not a prompt, so new_notes must carry
    # the whole field list; field zero alone would render as a broken image in the
    # confirmation's inline list rather than naming the card.
    apkg = str(tmp_path / "deck.apkg")
    _make_mock_apkg(apkg, [(1, "g1", ['<img src="femoral.jpg">', "Name this nerve",
                                      "Femoral nerve"])])
    _, _, _, new_notes = logic.remap_cards(apkg, her={}, aliases={})
    assert new_notes == [(1, ['<img src="femoral.jpg">', "Name this nerve",
                              "Femoral nerve"], "g1")]
    # and the display helper picks the prompt out of exactly that list:
    assert logic.note_display_label(new_notes[0][1]) == "Name this nerve"


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
    assert logic.field_preview_text('<img src="femoral.jpg">') == "[image: femoral.jpg]"


def test_field_preview_text_reports_text_and_image_together():
    assert logic.field_preview_text('Look here: <img src="a/b/nerve.png">') == (
        "Look here: [image: nerve.png]")


def test_field_preview_text_plain_field_is_unchanged():
    assert logic.field_preview_text("Just prose") == "Just prose"
    assert logic.field_preview_text("") == ""


# --------------------------------------------------------------- cloze_filled_html
def test_cloze_filled_html_fills_each_deletion_with_its_answer():
    assert logic.cloze_filled_html(
        "The {{c1::tibial}} nerve lies posterior to the {{c2::medial}} malleolus") == (
        'The <span class="cloze">tibial</span> nerve lies posterior to the '
        '<span class="cloze">medial</span> malleolus')


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


# ------------------------------------------------------- duplicate dialog body
def _dup_group(label, keep_deck, arch_deck, keep_reps=0, arch_reps=0):
    return {
        "model": "M", "front": "f",
        "keep": {"guid": "k", "label": label, "deck": keep_deck, "reps": keep_reps},
        "archive": [{"guid": "a", "label": label, "deck": arch_deck, "reps": arch_reps}],
    }


def test_duplicate_dialog_html_shows_the_label_not_a_raw_image_tag():
    groups = [_dup_group("Name this nerve block",
                         "Deck::3. The Blocks", "Deck::3. The Blocks")]
    html = logic.duplicate_dialog_html(groups)
    assert "Name this nerve block" in html
    assert "<img" not in html


def test_duplicate_dialog_html_reads_as_a_copy_count_when_decks_match():
    groups = [_dup_group("Card A", "Deck::Blocks", "Deck::Blocks", keep_reps=3)]
    html = logic.duplicate_dialog_html(groups)
    assert "2 copies in Blocks" in html
    assert "duplicate copy of" in html and "</b> card." in html


def test_duplicate_dialog_html_names_both_decks_when_they_differ():
    groups = [_dup_group("Card A", "Deck::New", "Deck::Old", keep_reps=5, arch_reps=1)]
    html = logic.duplicate_dialog_html(groups)
    assert "keeping New" in html and "archiving Old" in html


def test_duplicate_dialog_html_escapes_the_label():
    groups = [_dup_group("A <script> & B", "Deck::X", "Deck::X")]
    html = logic.duplicate_dialog_html(groups)
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_duplicate_dialog_html_pluralizes_the_heading():
    groups = [_dup_group("A", "D::X", "D::X"), _dup_group("B", "D::Y", "D::Y")]
    html = logic.duplicate_dialog_html(groups)
    assert "duplicate copies of" in html and "</b> cards." in html


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


def test_apkg_deck_names_converts_unit_separator_to_double_colon(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "zstandard", _FakeZstandardModule)
    p = _newer_apkg(tmp_path / "newer_sep.apkg",
                     ["Intern Pearls\x1fIntern Custom\x1fCA1 Handbook"])
    assert logic.apkg_deck_names(p) == [
        "Intern Pearls::Intern Custom::CA1 Handbook"]


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
