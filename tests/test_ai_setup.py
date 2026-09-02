"""The AI Backends window: one compact row per assistant, plus the settings
panel for the preferred one (see ai_dialog.py's setup page, now just an entry
point into this)."""
import os
import sys

FAKE = os.path.join(os.path.dirname(__file__), "fake_cli.py")


def _none_found(monkeypatch):
    from internpearls import ai_cli
    monkeypatch.setattr(ai_cli, "detect_backends", lambda cfg: {
        "backends": {k: {"path": None, "ok": False, "detail": "not found", "enabled": True}
                     for k in ai_cli.BACKENDS}, "chosen": None})


def _all_found(monkeypatch):
    from internpearls import ai_cli
    monkeypatch.setattr(ai_cli, "detect_backends", lambda cfg: {
        "backends": {k: {"path": "/bin/x", "ok": True, "detail": "1", "enabled": True}
                     for k in ai_cli.BACKENDS}, "chosen": "claude"})


def test_backends_metadata_carries_an_install_url(anki):
    """The row's install-guide link has somewhere to send a reader who does not
    have this CLI yet, for every backend, not just the two anyone remembered."""
    from internpearls import ai_cli
    for kind, meta in ai_cli.BACKENDS.items():
        assert meta["install_url"].startswith("https://"), kind


def test_open_ai_backends_builds_one_row_per_backend(anki, monkeypatch):
    from internpearls import ai_cli, ai_setup
    _none_found(monkeypatch)
    dlg = ai_setup._AIBackendsDialog(anki.mw)
    assert set(dlg.rows) == set(ai_cli.BACKENDS)
    for kind, meta in ai_cli.BACKENDS.items():
        text = dlg.rows[kind].text()
        assert meta["label"] in text
        assert meta["safety"] in text
        assert meta["subscription"] in text
        assert meta["exe"] in text


def test_the_preferred_row_is_marked_and_the_others_offer_use(anki, monkeypatch):
    """Exactly one row carries the preferred marker (and so no Use link); every
    other row offers to become the preferred one by name."""
    from internpearls import ai_cli, ai_setup
    _all_found(monkeypatch)
    anki.mw._config = {"ai_backend": "codex"}
    dlg = ai_setup._AIBackendsDialog(anki.mw)
    assert dlg.preferred == "codex"
    assert dlg.rows["codex"].use_link is None
    for kind in ("claude", "agy"):
        assert dlg.rows[kind].use_link.text() == f"Use {ai_cli.BACKENDS[kind]['label']}"


def test_use_link_writes_the_preferred_backend(anki, monkeypatch):
    from internpearls import ai_setup
    _all_found(monkeypatch)
    anki.mw._config = {"ai_backend": "claude"}
    dlg = ai_setup._AIBackendsDialog(anki.mw)
    dlg.rows["codex"].use_link.clicked.emit()
    assert anki.mw._config["ai_backend"] == "codex"
    assert dlg.preferred == "codex"
    assert dlg.rows["codex"].use_link is None   # re-detected: now the marked row


def test_ignore_link_disables_the_backend_and_flips_its_own_wording(anki, monkeypatch):
    """ignore writes ai_backend_enabled[kind] = False and re-detects, so the row
    comes back set aside with the way back out written on it."""
    from internpearls import ai_cli, ai_setup

    def detect(cfg):
        enabled = cfg["ai_backend_enabled"]
        return {"backends": {k: {"path": "/bin/x" if enabled[k] else None,
                                 "ok": enabled[k], "detail": "1",
                                 "enabled": enabled[k]} for k in ai_cli.BACKENDS},
                "chosen": "claude" if enabled["claude"] else None}
    monkeypatch.setattr(ai_cli, "detect_backends", detect)
    anki.mw._config = {"ai_backend": "claude"}
    dlg = ai_setup._AIBackendsDialog(anki.mw)
    assert dlg.rows["agy"].ignore_link.text() == "ignore"

    dlg.rows["agy"].ignore_link.clicked.emit()
    assert anki.mw._config["ai_backend_enabled"]["agy"] is False
    assert dlg.rows["agy"].ignore_link.text() == "use again"
    assert dlg.rows["agy"].use_link is None      # an ignored backend can't be chosen

    dlg.rows["agy"].ignore_link.clicked.emit()
    assert anki.mw._config["ai_backend_enabled"]["agy"] is True
    assert dlg.rows["agy"].ignore_link.text() == "ignore"


def test_install_guide_link_opens_that_backends_documentation(anki, monkeypatch):
    from aqt.qt import QDesktopServices
    from internpearls import ai_cli, ai_setup
    _none_found(monkeypatch)
    dlg = ai_setup._AIBackendsDialog(anki.mw)
    QDesktopServices.opened.clear()
    for kind in ai_cli.BACKENDS:
        dlg.rows[kind].guide_link.clicked.emit()
    assert QDesktopServices.opened == [ai_cli.BACKENDS[k]["install_url"]
                                       for k in ai_cli.BACKENDS]


def test_settings_panel_follows_the_preferred_backend(anki, monkeypatch):
    from internpearls import ai_setup
    _all_found(monkeypatch)
    anki.mw._config = {"ai_backend": "claude"}
    dlg = ai_setup._AIBackendsDialog(anki.mw)
    assert dlg.panel.kind == "claude"
    assert isinstance(dlg.panel.model, ai_setup.ModelEffortControls)
    dlg.rows["agy"].use_link.clicked.emit()
    assert dlg.panel.kind == "agy"
    assert dlg.panel.model.kind == "agy"


def test_path_commit_writes_the_preferred_backends_path(anki, monkeypatch):
    from internpearls import ai_setup
    _all_found(monkeypatch)
    anki.mw._config = {"ai_backend": "claude"}
    dlg = ai_setup._AIBackendsDialog(anki.mw)
    dlg.panel.path.setText("/opt/claude")
    dlg.panel._commit_path()
    assert anki.mw._config["ai_cli_path"]["claude"] == "/opt/claude"


# === migration: ai_cli_path was a flat string through v0.55.1 ==============

def test_cli_path_flat_string_migrates_to_preferred_backend(anki):
    from internpearls.config import _cfg
    anki.mw._config = {"ai_backend": "claude", "ai_cli_path": "/opt/claude"}
    c = _cfg()
    assert c["ai_cli_path"] == {"claude": "/opt/claude", "codex": "", "agy": ""}
    anki.mw._config = {"ai_cli_path": "/opt/claude"}     # no preference: dropped
    assert _cfg()["ai_cli_path"] == {"claude": "", "codex": "", "agy": ""}


def test_cli_path_flat_string_survives_first_per_backend_save(anki, monkeypatch):
    """The migrated path for one backend must not be dropped by the first
    _write_map save for another backend: _write_map used to seed from {}
    whenever the raw config value wasn't already a dict, discarding whatever
    _cfg() had just migrated the legacy string into."""
    from internpearls import ai_setup
    _none_found(monkeypatch)
    anki.mw._config = {"ai_backend": "claude", "ai_cli_path": "/opt/claude"}
    ai_setup._AIBackendsDialog(anki.mw)
    # Saved against _write_map itself rather than through the panel's own field:
    # the panel is always about the preferred backend, and switching preference
    # first would re-migrate the legacy string onto the new one (config._cli_path_map
    # reads it as "the path for whatever backend is preferred"), which is a
    # different question from the seeding bug this test exists for.
    ai_setup._write_map("ai_cli_path", "codex", "/opt/codex")
    assert anki.mw._config["ai_cli_path"] == {
        "claude": "/opt/claude", "codex": "/opt/codex", "agy": ""}


def test_backend_enabled_defaults_true(anki):
    from internpearls.config import _cfg
    anki.mw._config = {}
    assert _cfg()["ai_backend_enabled"] == {"claude": True, "codex": True, "agy": True}
    anki.mw._config = {"ai_backend_enabled": {"agy": False}}
    assert _cfg()["ai_backend_enabled"]["agy"] is False


# === Test connection, Re-check, and detection status =======================

def _drain_conn_test(dlg, timeout=15):
    """Mirrors ai_dialog's own test helper: joins the background thread a
    Test connection click started, then fires its poll timer (the mock has
    no live event loop) to run the completion callback."""
    t, timer = dlg._refs[-1]
    t.join(timeout=timeout)
    timer.fire()


def test_connection_button_disabled_until_a_cli_is_found(anki, monkeypatch):
    from internpearls import ai_cli, ai_setup
    monkeypatch.setattr(ai_cli, "find_cli", lambda kind, override="": None)
    dlg = ai_setup._AIBackendsDialog(anki.mw)
    assert not dlg.panel.test_btn.isEnabled()


def test_connection_reports_working(anki, monkeypatch):
    from internpearls import ai_cli, ai_setup
    monkeypatch.setattr(
        ai_cli, "find_cli",
        lambda kind, override="": sys.executable if kind == "claude" else None)
    monkeypatch.setattr(ai_cli, "probe", lambda kind, path: {"ok": True, "detail": "v1"})
    monkeypatch.setattr(
        ai_cli, "build_argv",
        lambda kind, path, mode, scratch, imgs, model="", effort="":
            ([sys.executable, FAKE, "badjson"], True))
    dlg = ai_setup._AIBackendsDialog(anki.mw)
    assert dlg.panel.kind == "claude"
    assert dlg.panel.test_btn.isEnabled()
    dlg._test("claude")
    assert not dlg.panel.test_btn.isEnabled()   # disabled while the test runs
    _drain_conn_test(dlg)
    assert "working" in dlg.panel.test_status.text().lower()
    assert dlg.panel.test_btn.isEnabled()       # re-enabled once it's done


def test_recheck_mid_test_connection_does_not_reenable_or_double_run(anki, monkeypatch):
    # Minor fix carried over from the wizard's own setup page: Re-check used
    # to unconditionally reset the row's button/status, so pressing it while a
    # Test connection run was in flight re-enabled that button and cleared its
    # "Testing connection" text; a second click could then start a concurrent
    # test racing the first to write the same label.
    from internpearls import ai_cli, ai_setup
    monkeypatch.setattr(
        ai_cli, "find_cli",
        lambda kind, override="": sys.executable if kind == "claude" else None)
    monkeypatch.setattr(ai_cli, "probe", lambda kind, path: {"ok": True, "detail": "v1"})
    monkeypatch.setattr(
        ai_cli, "build_argv",
        lambda kind, path, mode, scratch, imgs, model="", effort="":
            ([sys.executable, FAKE, "badjson"], True))
    dlg = ai_setup._AIBackendsDialog(anki.mw)
    panel = dlg.panel
    dlg._test("claude")
    assert not panel.test_btn.isEnabled()
    status_mid_test = panel.test_status.text()
    n_refs = len(dlg._refs)

    dlg.recheck()   # simulates a Re-check click mid-test
    assert dlg.panel is panel                       # panel not rebuilt under it
    assert not panel.test_btn.isEnabled()           # still disabled
    assert panel.test_status.text() == status_mid_test  # not wiped

    dlg._test("claude")   # a second click while still running
    assert len(dlg._refs) == n_refs   # no second test was started

    _drain_conn_test(dlg)
    assert "working" in panel.test_status.text().lower()
    assert panel.test_btn.isEnabled()


def test_connection_not_signed_in_shows_readable_message(anki, monkeypatch):
    from internpearls import ai_cli, ai_setup
    monkeypatch.setattr(
        ai_cli, "find_cli",
        lambda kind, override="": sys.executable if kind == "claude" else None)
    monkeypatch.setattr(ai_cli, "probe", lambda kind, path: {"ok": True, "detail": "v1"})
    monkeypatch.setattr(
        ai_cli, "build_argv",
        lambda kind, path, mode, scratch, imgs, model="", effort="":
            ([sys.executable, FAKE, "not_signed_in"], True))
    dlg = ai_setup._AIBackendsDialog(anki.mw)
    dlg._test("claude")
    _drain_conn_test(dlg)
    text = dlg.panel.test_status.text().lower()
    assert "not working" in text and "sign in" in text
    assert "traceback" not in text and "run `claude login`" not in text  # not raw stderr


def test_row_chip_reads_as_one_of_the_readmes_three_states(anki, monkeypatch):
    # I7: the row must say which of the README's three states detection landed
    # in, and say it as its own chip rather than rendering --version's raw
    # output as if it were one. Plus the fourth state detection never produces:
    # a backend the reader set aside.
    from internpearls import ai_setup
    states = [
        ({"path": "/bin/x", "ok": True, "detail": "9.9.9", "enabled": True}, "found"),
        ({"path": "/bin/x", "ok": False, "detail": "boom", "enabled": True},
         "notresponding"),
        ({"path": None, "ok": False, "detail": "not found", "enabled": True},
         "notfound"),
        ({"path": None, "ok": False, "detail": "disabled", "enabled": False}, "ignored"),
    ]
    for info, want in states:
        assert ai_setup._state_chip(info) == want


def test_overall_status_names_the_backend_that_will_be_used(anki, monkeypatch):
    from internpearls import ai_setup
    _all_found(monkeypatch)
    dlg = ai_setup._AIBackendsDialog(anki.mw)
    assert dlg.overall.text() == "Ready: Claude Code will be used."
    _none_found(monkeypatch)
    dlg.recheck()
    assert dlg.overall.text() == "No usable assistant detected yet."
