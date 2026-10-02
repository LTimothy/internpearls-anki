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
    for hook in list(sys.modules["aqt"].gui_hooks.profile_did_open):
        hook()
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
    _profile_hooks_registered(anki)
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
    hooks = sys.modules["aqt"].gui_hooks.profile_did_open
    assert hooks.count(background._on_profile_open) == 1


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

