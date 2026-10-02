"""Profile lifecycle, backups and restores, self-update failure handling."""
import sys

import mock_anki
from test_sync_flows import DECK, NEW_CSS, SCOPE, TAGS, _StubAction, _fields, _write_source
from mock_anki import make_model


def _open_profile(anki, path):
    """Switch to a fresh collection at `path` and fire Anki's profile-open hook."""
    col = mock_anki.MockCollection()
    col.path = path
    anki.mw.col = col
    sys.modules["aqt"].gui_hooks.profile_did_open()
    return col


def _profile_hooks_registered(anki):
    from internpearls import background
    anki.mw.col = None
    background._schedule_background_checks()


# ------------------------------------------------------------- profile switch
def test_a_deck_deferred_in_one_profile_still_syncs_in_another(anki, tmp_path):
    from internpearls import background
    _profile_hooks_registered(anki)
    first = _open_profile(anki, str(tmp_path / "a" / "collection.anki2"))
    folder = _write_source(tmp_path, {
        DECK: ("v2", [("g1", _fields("Front one"), TAGS)], make_model(css=NEW_CSS))})
    anki.mw._config = {"decks_dir": folder, "auto_sync_decks": True}

    background._auto_sync_check()
    assert first.find_notes(f'"tag:{SCOPE}"') == []    # held back for the template

    second = _open_profile(anki, str(tmp_path / "b" / "collection.anki2"))
    second.models.all()[0]["css"] = NEW_CSS               # nothing to hold back here
    background._auto_sync_check()

    assert second.note_by_guid("g1")["Front"] == "Front one"


def test_each_profile_hears_about_its_own_deferred_deck_once(anki, tmp_path):
    from internpearls import background
    _profile_hooks_registered(anki)
    _open_profile(anki, str(tmp_path / "a" / "collection.anki2"))
    folder = _write_source(tmp_path, {
        DECK: ("v2", [("g1", _fields("Front one"), TAGS)], make_model(css=NEW_CSS))})
    anki.mw._config = {"decks_dir": folder, "auto_sync_decks": True}
    background._auto_sync_check()
    background._auto_sync_check()
    assert sum("card-template" in t for t in anki.gui.tooltips) == 1

    _open_profile(anki, str(tmp_path / "b" / "collection.anki2"))
    background._auto_sync_check()
    background._auto_sync_check()

    assert sum("card-template" in t for t in anki.gui.tooltips) == 2


def test_profile_open_resets_the_reconcile_label_and_its_nudge(anki, tmp_path):
    from internpearls import background, sync
    stub = _StubAction()
    sync.register_reconcile_action(stub)
    _profile_hooks_registered(anki)
    folder = _write_source(tmp_path, {}, retired={
        DECK: {"old1": {"identity": "bulky crisis card", "reason": "split",
                        "superseded_by": []}}})
    anki.mw._config = {"decks_dir": folder, "auto_sync_decks": True}
    first = _open_profile(anki, str(tmp_path / "a" / "collection.anki2"))
    first.add_note("old1", _fields("bulky crisis card"), TAGS.split())
    background._auto_sync_check()
    assert stub.text == "Reconcile my decks (1 pending)"

    second = _open_profile(anki, str(tmp_path / "b" / "collection.anki2"))
    assert stub.text == "Reconcile my decks"

    second.add_note("old1", _fields("bulky crisis card"), TAGS.split())
    background._auto_sync_check()
    assert sum("ready to tidy up" in t for t in anki.gui.tooltips) == 2


# ------------------------------------------------------- first check timing
def test_auto_sync_first_check_is_armed_by_profile_open(anki, tmp_path):
    """Anki runs main_window_did_init before any profile is open, so a first check
    scheduled from there finds no collection and the next one is a full interval
    away. Opening the profile arms it instead."""
    import aqt.qt as aqt_qt
    from internpearls import background
    anki.mw._config = {"auto_sync_decks": True, "auto_sync_interval_minutes": 60}
    background._stop_auto_sync_timer()
    _profile_hooks_registered(anki)
    assert background._auto_sync_timer is not None, "the poll was never started"
    assert background._auto_sync_timer.started == 60 * 60 * 1000
    assert not [fn for _ms, fn in aqt_qt.QTimer.single_shots
                if fn is background._auto_sync_check]

    _open_profile(anki, str(tmp_path / "a" / "collection.anki2"))

    armed = [ms for ms, fn in aqt_qt.QTimer.single_shots
             if fn is background._auto_sync_check]
    assert armed and armed[0] <= 10000


def test_profile_open_does_not_arm_auto_sync_when_it_is_off(anki, tmp_path):
    import aqt.qt as aqt_qt
    from internpearls import background
    anki.mw._config = {"auto_sync_decks": False}
    _profile_hooks_registered(anki)
    _open_profile(anki, str(tmp_path / "a" / "collection.anki2"))
    assert not [fn for _ms, fn in aqt_qt.QTimer.single_shots
                if fn is background._auto_sync_check]


def test_profile_open_hook_is_registered_once(anki):
    from internpearls import background
    _profile_hooks_registered(anki)
    _profile_hooks_registered(anki)
    assert sys.modules["aqt"].gui_hooks.profile_did_open.count() == 1


# ------------------------------------------------------- full collection backup
def test_backup_full_collection_reports_up_to_date_when_nothing_changed(anki):
    """With force=True Anki still returns False when the collection has not changed
    since its last backup. That is not a failure."""
    from internpearls import collection
    anki.col.add_note("g1", _fields("Front one"), TAGS.split())
    collection.backup_collection_now()
    assert anki.gui.warnings == [] and "Backed up your whole collection" in anki.gui.infos[-1]

    collection.backup_collection_now()

    assert anki.gui.warnings == []
    assert "already" in anki.gui.infos[-1] and "up to date" in anki.gui.infos[-1]


def test_backup_full_collection_still_warns_when_the_backup_fails(anki, monkeypatch):
    from internpearls import collection

    def boom(**_kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(anki.col, "create_backup", boom)
    collection.backup_collection_now()
    assert anki.gui.warnings and "Couldn't create a collection backup" in anki.gui.warnings[-1]


# ------------------------------------------------------------- self-update
def _offer_update(monkeypatch, tmp_path, package):
    from internpearls import updates
    monkeypatch.setattr(updates, "_fetch_addon_version_info",
                        lambda timeout=None, token=None: {"version": "99.0.0"})
    monkeypatch.setattr(updates, "_download_addon_package",
                        lambda timeout=None, token=None, expected=None: package)


def test_self_update_reports_an_install_anki_refused(anki, monkeypatch, tmp_path):
    """addonManager.install returns an InstallError rather than raising, and the old
    version stays installed; saying "Updated" there was wrong."""
    from internpearls import updates
    bad = tmp_path / "broken.ankiaddon"
    bad.write_bytes(b"not a zip")
    _offer_update(monkeypatch, tmp_path, str(bad))
    anki.gui.interactive = False
    anki.gui.answers.append(True)    # Install now

    updates.check_updates()

    assert not any("Updated" in i for i in anki.gui.infos), anki.gui.infos
    assert anki.gui.warnings and "failed" in anki.gui.warnings[-1]
    assert anki.mw.addonManager.installed == []


def test_background_self_update_reports_an_install_anki_refused(anki, monkeypatch,
                                                                  tmp_path):
    from internpearls import background
    bad = tmp_path / "broken.ankiaddon"
    bad.write_bytes(b"not a zip")
    _offer_update(monkeypatch, tmp_path, str(bad))
    anki.mw._config = {"auto_update_addon": True, "notify_addon_updates": True}

    background._check_addon_updates_background()

    assert not any("updated itself" in t for t in anki.gui.tooltips), anki.gui.tooltips
    assert any("couldn't install v99.0.0" in t for t in anki.gui.tooltips)


def test_self_update_installs_a_good_package(anki, monkeypatch, tmp_path):
    import json
    import zipfile
    from internpearls import updates
    good = tmp_path / "good.ankiaddon"
    with zipfile.ZipFile(good, "w") as z:
        z.writestr("manifest.json", json.dumps({"package": "internpearls",
                                                "name": "Intern Pearls Deck Tools"}))
    _offer_update(monkeypatch, tmp_path, str(good))
    anki.gui.answers.append(True)

    updates.check_updates()

    assert anki.mw.addonManager.installed == [str(good)]
    assert any("Updated" in i for i in anki.gui.infos)


# ------------------------------------------------------------------- restore
def _exported_deck_names(anki):
    return [anki.col.decks.name(limit.deck_id) for _p, _o, limit in anki.col.exports]


def _newest_backup(collection):
    import os
    folder = collection._deck_backup_folder()
    files = [os.path.join(folder, f) for f in os.listdir(folder) if f.endswith(".apkg")]
    return max(files, key=os.path.getmtime)


def test_restore_backs_up_every_deck_it_will_change(anki, tmp_path):
    """A restore rewrites each matched note wherever the learner keeps it, so the
    backup taken first has to cover those decks, not export_deck alone."""
    from internpearls import collection
    anki.col.add_note("g1", _fields("Front one"), TAGS.split(), deck=DECK)
    anki.col.add_note("g2", _fields("Front two"), TAGS.split(), deck="Elsewhere::Mine")
    src = collection._backup_deck("Elsewhere", "Elsewhere")
    anki.col.exports.clear()
    anki.gui.file_picks.append(src)
    anki.gui.answers.append(True)          # Import

    collection.import_deck()

    assert not anki.gui.warnings
    assert "Elsewhere" in _exported_deck_names(anki)


def test_restore_puts_back_the_baseline_the_backup_was_taken_with(anki, tmp_path):
    """Restoring an older backup puts older source text back into a preserved field.
    The next update has to see that text as the source's, not as the learner's own
    edit, or the correction never arrives; the learner's own note must still stay."""
    from internpearls import collection
    from test_sync_flows import _sync
    folder = _write_source(tmp_path, {
        DECK: ("v1", [("g1", _fields("Front one", dosing="1 mg"), TAGS)], None)})
    anki.mw._config = {"decks_dir": folder, "protected_fields": ["Notes", "Dosing"]}
    _sync(anki)
    anki.col.note_by_guid("g1")["Notes"] = "my own note"

    _write_source(tmp_path, {
        DECK: ("v2", [("g1", _fields("Front one", dosing="2 mg"), TAGS)], None)})
    _sync(anki)                            # backs up v1 first, then applies v2
    assert anki.col.note_by_guid("g1")["Dosing"] == "2 mg"

    anki.gui.file_picks.append(_newest_backup(collection))
    anki.gui.answers.append(True)          # Import
    collection.import_deck()
    assert anki.col.note_by_guid("g1")["Dosing"] == "1 mg"

    _sync(anki)                            # v2 is offered again

    note = anki.col.note_by_guid("g1")
    assert note["Dosing"] == "2 mg"
    assert note["Notes"] == "my own note"


def test_restore_without_a_saved_baseline_keeps_annotations(anki, tmp_path):
    """A file the add-on did not back up itself has no baseline beside it. Dropping
    the restored notes' baselines then falls back to keeping every non-blank value,
    so an annotation in the file is never overwritten by the next update."""
    import shutil
    from internpearls import collection
    from test_sync_flows import _sync
    folder = _write_source(tmp_path, {
        DECK: ("v1", [("g1", _fields("Front one"), TAGS)], None)})
    anki.mw._config = {"decks_dir": folder}
    _sync(anki)
    anki.col.note_by_guid("g1")["Notes"] = "my own note"
    exported = collection._backup_deck(DECK, "manual")
    plain = str(tmp_path / "shared.apkg")
    shutil.copy(exported, plain)
    anki.col.note_by_guid("g1")["Notes"] = ""

    anki.gui.file_picks.append(plain)
    anki.gui.answers.append(True)
    collection.import_deck()
    _write_source(tmp_path, {
        DECK: ("v2", [("g1", _fields("Front one", back="new back"), TAGS)], None)})
    _sync(anki)

    note = anki.col.note_by_guid("g1")
    assert note["Back"] == "new back" and note["Notes"] == "my own note"


def test_auto_sync_skips_a_round_when_export_deck_is_missing(anki, tmp_path):
    """The interactive path asks before importing over cards it could not back up.
    Unattended there is no one to ask, so the round is skipped."""
    from internpearls import background
    anki.col.add_note("g1", _fields("Front one"), TAGS.split(), deck="Somewhere else")
    folder = _write_source(tmp_path, {
        DECK: ("v2", [("g9", _fields("A new card"), TAGS)], None)})
    anki.mw._config = {"decks_dir": folder, "auto_sync_decks": True}

    background._auto_sync_check()

    assert anki.col.imports == []
    assert any("couldn't create a backup" in t for t in anki.gui.tooltips)


def test_auto_sync_first_sync_with_nothing_to_back_up_still_runs(anki, tmp_path):
    from internpearls import background
    folder = _write_source(tmp_path, {
        DECK: ("v1", [("g1", _fields("Front one"), TAGS)], None)})
    anki.mw._config = {"decks_dir": folder, "auto_sync_decks": True}
    background._auto_sync_check()
    assert anki.col.note_by_guid("g1")["Front"] == "Front one"


# ------------------------------------------------------------ import options
def test_a_sync_imports_without_merging_note_types_or_taking_the_files_scheduling(
        anki, tmp_path):
    from test_sync_flows import _sync
    anki.mw._config = {"decks_dir": _write_source(tmp_path, {
        DECK: ("v1", [("g1", _fields("Front one"), TAGS)], None)})}
    scm = anki.col.scm

    _sync(anki)

    assert anki.col.import_options, "nothing was imported"
    for opts in anki.col.import_options:
        assert opts["merge_notetypes"] is False
        assert opts["with_scheduling"] is False
        assert opts["update_notes"] == 1          # ALWAYS
    assert anki.col.scm == scm


def test_a_restore_schedules_only_the_cards_it_adds_back(anki, tmp_path):
    """Anki applies a file's scheduling only to cards the import creates: a card still
    in the collection keeps its own, and one that was deleted comes back with the
    file's."""
    from internpearls import collection
    kept = anki.col.add_note("g1", _fields("Front one"), TAGS.split(), deck=DECK)
    gone = anki.col.add_note("g2", _fields("Front two"), TAGS.split(), deck=DECK)
    for note in (kept, gone):
        card = anki.col.get_card(note.card_ids()[0])
        card.ivl, card.due, card.reps, card.queue, card.type = 12, 90, 4, 2, 2
    src = collection._backup_deck(DECK, "manual")
    kept_card = anki.col.get_card(kept.card_ids()[0])
    kept_card.ivl, kept_card.due, kept_card.reps = 1, 3, 9
    anki.col.remove_cards_and_orphaned_notes(gone.card_ids())

    anki.gui.file_picks.append(src)
    anki.gui.answers.append(True)          # Import
    collection.import_deck()

    assert anki.col.import_options[-1]["with_scheduling"] is True
    assert (kept_card.ivl, kept_card.due, kept_card.reps) == (1, 3, 9)
    back = anki.col.get_card(anki.col.note_by_guid("g2").card_ids()[0])
    assert (back.ivl, back.due, back.reps) == (12, 90, 4)


def test_the_mock_importer_honours_its_options(anki, tmp_path):
    """The mock stands in for Anki's importer, so it must react to the options the
    add-on passes rather than ignore them."""
    from anki.collection import ImportAnkiPackageOptions, ImportAnkiPackageRequest
    from mock_anki import make_apkg
    note = anki.col.add_note("g1", _fields("Old front"), TAGS.split(), deck=DECK)
    src = str(tmp_path / "p.apkg")
    make_apkg(src, [("g1", _fields("New front"), TAGS)], deck=DECK)

    opts = ImportAnkiPackageOptions()
    opts.update_notes = 2                 # NEVER
    anki.col.import_anki_package(ImportAnkiPackageRequest(package_path=src, options=opts))
    assert note["Front"] == "Old front"

    opts = ImportAnkiPackageOptions()
    opts.update_notes = 1
    opts.merge_notetypes = True
    scm = anki.col.scm
    anki.col.import_anki_package(ImportAnkiPackageRequest(package_path=src, options=opts))
    assert note["Front"] == "New front" and anki.col.scm == scm + 1

    import pytest
    with pytest.raises(AttributeError):
        opts.no_such_option = True


def test_an_unanswered_question_fails_the_test_even_inside_a_safe_flow(anki, tmp_path):
    """A flow wrapped in _safe turns an ordinary exception into a warning, so the
    mock's unanswered question has to escape it."""
    import pytest
    from internpearls import collection
    src = collection._backup_deck(DECK, "manual") or str(tmp_path / "missing.apkg")
    anki.gui.file_picks.append(src)
    with pytest.raises(mock_anki.UnansweredQuestion):
        collection.import_deck()


def test_a_refused_install_names_the_reason_in_plain_words(anki, monkeypatch, tmp_path):
    from internpearls import updates
    monkeypatch.setattr(anki.mw.addonManager, "install",
                        lambda path: mock_anki.types.SimpleNamespace(errmsg="zip"))
    try:
        updates._install_package(str(tmp_path / "x.ankiaddon"))
    except RuntimeError as e:
        assert "not a valid add-on package" in str(e) and "zip" not in str(e)
    else:
        raise AssertionError("a refused install did not raise")
    monkeypatch.setattr(anki.mw.addonManager, "install",
                        lambda path: mock_anki.types.SimpleNamespace(errmsg="manifest"))
    try:
        updates._install_package(str(tmp_path / "x.ankiaddon"))
    except RuntimeError as e:
        assert "manifest" not in str(e)


def test_restore_resets_baselines_only_for_notes_the_import_wrote(anki, tmp_path,
                                                                    monkeypatch):
    """Anki reports which notes an import added or changed; a note it matched without
    changing still holds what the next update compares against, so its baseline stays."""
    import types
    from internpearls import collection
    one = anki.col.add_note("g1", _fields("Front one"), TAGS.split(), deck=DECK)
    two = anki.col.add_note("g2", _fields("Front two"), TAGS.split(), deck=DECK)
    collection._save_json(collection.SHIPPED, {"g1": {"Notes": "old"},
                                               "g2": {"Notes": "old"}})
    src = collection._backup_deck(DECK, "manual")
    collection._save_json(collection.SHIPPED, {"g1": {"Notes": "new"},
                                               "g2": {"Notes": "new"}})
    real = collection._import_apkg

    def import_with_log(path, with_scheduling=False):
        real(path, with_scheduling)
        row = types.SimpleNamespace(id=types.SimpleNamespace(nid=one.id))
        other = types.SimpleNamespace(id=types.SimpleNamespace(nid=two.id))
        return types.SimpleNamespace(log=types.SimpleNamespace(
            new=[], updated=[row], duplicate=[other]))

    monkeypatch.setattr(collection, "_import_apkg", import_with_log)
    anki.gui.file_picks.append(src)
    anki.gui.answers.append(True)          # Import
    collection.import_deck()

    assert collection._load_json(collection.SHIPPED, {}) == {
        "g1": {"Notes": "old"}, "g2": {"Notes": "new"}}


def test_restore_backs_up_a_deck_holding_an_unscoped_copy_of_a_restored_note(anki):
    """The importer matches GUIDs across the whole collection, not just the scope tag."""
    from internpearls import collection
    anki.col.add_note("g1", _fields("Front one"), TAGS.split(), deck=DECK)
    src = collection._backup_deck(DECK, "manual")
    note = anki.col.note_by_guid("g1")
    note.tags = ["untagged"]
    anki.col.set_deck(note.card_ids(), anki.col.decks.id("Loose"))
    anki.col.exports.clear()
    anki.gui.file_picks.append(src)
    anki.gui.answers.append(True)          # Import

    collection.import_deck()

    assert "Loose" in _exported_deck_names(anki)


def test_pruning_a_backup_removes_its_saved_baseline(anki, monkeypatch):
    import datetime
    import os
    from internpearls import collection
    anki.col.add_note("g1", _fields("Front one"), TAGS.split(), deck=DECK)
    ticks = iter(range(100))

    class Clock(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.datetime(2026, 9, 9) + datetime.timedelta(seconds=next(ticks))

    monkeypatch.setattr(collection.datetime, "datetime", Clock)
    first = collection._backup_deck(DECK, DECK)
    assert os.path.exists(collection._baseline_path(first))
    for _ in range(collection.DECK_BACKUPS_KEEP):
        collection._backup_deck(DECK, DECK)

    assert not os.path.exists(first)
    assert not os.path.exists(collection._baseline_path(first))
    kept = os.listdir(os.path.dirname(collection._baseline_path(first)))
    assert len(kept) == collection.DECK_BACKUPS_KEEP


def test_saved_baselines_are_kept_per_collection(anki, tmp_path):
    from internpearls import collection
    anki.col.path = str(tmp_path / "a" / "collection.anki2")
    first = collection._baseline_path(collection._backup_deck(DECK, DECK) or "x.apkg")
    anki.col.path = str(tmp_path / "b" / "collection.anki2")
    second = collection._baseline_path("x.apkg")
    assert first.rsplit("/", 1)[0] != second.rsplit("/", 1)[0]


def test_an_update_undone_in_one_profile_still_auto_syncs_in_another(anki, tmp_path):
    from internpearls import background, config, sync
    _profile_hooks_registered(anki)
    folder = _write_source(tmp_path, {
        DECK: ("v1", [("g1", _fields("Front one"), TAGS)], None)})
    anki.mw._config = {"decks_dir": folder, "auto_sync_decks": True}
    _open_profile(anki, str(tmp_path / "a" / "collection.anki2"))
    sync.undone_updates.add((config.collection_key(), DECK, "v1"))
    background._auto_sync_check()
    assert not anki.col.imports                 # A leaves it for a manual run

    second = _open_profile(anki, str(tmp_path / "b" / "collection.anki2"))
    background._auto_sync_check()

    assert second.note_by_guid("g1")["Front"] == "Front one"
