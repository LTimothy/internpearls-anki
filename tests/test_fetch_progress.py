"""Interactive source checks keep their results and cancellation on the GUI thread."""
import threading
from types import SimpleNamespace

import pytest


SITES = ["gated", "reconcile", "duplicates", "configure", "manage", "updates"]


def _site(anki, monkeypatch, site, fetch, proceeded):
    from internpearls import dialogs, sync, updates
    anki.mw._config = {"github_decks_repo": "example/decks"}
    if site == "updates":
        monkeypatch.setattr(updates, "_fetch_addon_version_info", fetch)
        monkeypatch.setattr(updates, "_refresh_update_action_label",
                            lambda *a: proceeded.append("updates"))
        return updates.check_updates
    mod = dialogs if site in ("configure", "manage") else sync
    monkeypatch.setattr(mod, "_fetch_manifest", fetch)
    if site == "gated":
        return lambda: sync._fetch_manifest_gated(sync._cfg())
    if site == "reconcile":
        monkeypatch.setattr(sync, "_reconcile_pending", lambda *a: (
            proceeded.append("reconcile") or ({}, [], 0, [], "Retired", "tag", [])))
        return sync.reconcile_decks
    if site == "duplicates":
        monkeypatch.setattr(sync, "_existing_notes_summary",
                            lambda *a, **kw: proceeded.append("duplicates") or [])
        return sync.clean_up_duplicates
    if site == "configure":
        monkeypatch.setattr(dialogs, "_SourceChoiceDialog", lambda *a: SimpleNamespace(
            choice="example", exec=lambda: None, deleteLater=lambda: None))
        monkeypatch.setattr(dialogs, "_offer_manifest_scope",
                            lambda *a: proceeded.append("configure"))
        return dialogs.configure_source

    def manager(*args, **kwargs):
        proceeded.append(args[3])
        return SimpleNamespace(
            exec=lambda: 0, change_source_requested=False, update_requested=False,
            excluded_decks=lambda old: old, protected_fields=lambda: [],
            _pf_edit=SimpleNamespace(text=lambda: ""), deleteLater=lambda: None)

    monkeypatch.setattr(dialogs, "_DeckManagerDialog", manager)
    return dialogs.manage_decks


@pytest.mark.parametrize("site", SITES)
@pytest.mark.parametrize("error", [False, True], ids=["result", "error"])
def test_source_checks_fetch_off_thread_and_keep_their_outcome(anki, monkeypatch, site, error):
    gui_thread = threading.get_ident()
    threads, proceeded = [], []

    def fetch(*args, **kwargs):
        threads.append(threading.get_ident())
        if error:
            raise RuntimeError("source <b>failed</b>")
        if site == "updates":
            return {"version": "0"}
        return {"decks": []}, lambda *a: None, "GitHub"

    run = _site(anki, monkeypatch, site, fetch, proceeded)
    result = run()
    assert threads and threads[0] != gui_thread
    if error:
        if site == "manage":
            assert proceeded == ["error: source <b>failed</b>"]
            assert not anki.gui.warnings
        else:
            assert not proceeded
            assert len(anki.gui.warnings) == 1
            assert "source &lt;b&gt;failed&lt;/b&gt;" in anki.gui.warnings[0]
    else:
        assert not anki.gui.warnings
        if site == "gated":
            assert result[0] == {"decks": []}
        else:
            assert proceeded == ["GitHub" if site == "manage" else site]


@pytest.mark.parametrize("site", SITES)
def test_cancelled_source_checks_return_quietly_without_proceeding(anki, monkeypatch, site):
    from aqt.qt import QProgressDialog
    from internpearls.platform import platform, wait_for_mock_work

    release = threading.Event()
    handles, proceeded = [], []
    native = platform()
    start_work = native.start_work

    def capture(*args, **kwargs):
        handle = start_work(*args, **kwargs)
        handles.append(handle)
        return handle

    monkeypatch.setattr(native, "start_work", capture)
    label = "Checking for add-on updates" if site == "updates" else "Checking the deck source"
    QProgressDialog.cancel_after = {label: 0}

    def fetch(*args, **kwargs):
        release.wait(1)
        if site == "updates":
            return {"version": "0"}
        return {"decks": []}, lambda *a: None, "GitHub"

    run = _site(anki, monkeypatch, site, fetch, proceeded)
    try:
        assert run() is None
        assert not proceeded
        assert not anki.gui.warnings
        assert not anki.gui.infos
    finally:
        release.set()
        for handle in handles:
            wait_for_mock_work(handle)
    assert not proceeded
    assert not anki.gui.warnings


@pytest.mark.parametrize("error", [False, True])
def test_run_with_progress_returns_or_raises_on_the_main_thread(anki, error):
    from internpearls import ui
    main_thread = threading.get_ident()
    seen = []
    failure = RuntimeError("fetch failed")

    def fetch():
        seen.append(threading.get_ident())
        if error:
            raise failure
        return None

    if error:
        with pytest.raises(RuntimeError) as raised:
            ui.run_with_progress("Checking the deck source", fetch)
        assert raised.value is failure
    else:
        assert ui.run_with_progress("Checking the deck source", fetch) is None
    assert seen[0] != main_thread


def test_abandoned_progress_discards_a_late_result(anki, monkeypatch):
    from internpearls import ui
    from internpearls.platform import platform, wait_for_mock_work
    release = threading.Event()
    native = platform()
    handles = []
    start_work = native.start_work

    def capture(*args, **kwargs):
        handle = start_work(*args, **kwargs)
        handles.append(handle)
        return handle

    monkeypatch.setattr(native, "start_work", capture)
    monkeypatch.setattr(ui, "_window_alive", lambda: False)
    try:
        with pytest.raises(ui.ProgressCancelled):
            ui.run_with_progress("Checking the deck source", lambda: release.wait(1))
    finally:
        release.set()
        for handle in handles:
            wait_for_mock_work(handle)
    assert handles and handles[0].cancel_event.is_set()


def test_progress_settles_replayed_work_without_stalling(anki, monkeypatch):
    from internpearls import ui
    from internpearls.platform import work_checkpoint
    from mock_anki import Runner

    runner = Runner(anki)
    results = []

    def fetch():
        work_checkpoint("fixture-fetch:start")
        return "ready"

    def stalled(_seconds):
        pytest.fail("the mock event pump did not reach the pending work")

    monkeypatch.setattr(ui.time, "sleep", stalled)
    response = runner.start_protocol(
        lambda: results.append(ui.run_with_progress("Checking the deck source", fetch)),
        epoch=1)
    assert response["status"] == "need"
    assert response["payload"]["kind"] == "work"
    response = runner.feed_protocol({
        "protocol": 1, "epoch": 1, "sequence": 1,
        "render_revision": response["render_revision"],
        "actions": [{"type": "advance", "elapsed_ms": 0, "checkpoint_credits": 1}],
    })
    assert response["status"] == "done"
    assert results == ["ready"]
