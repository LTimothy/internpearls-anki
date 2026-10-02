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
