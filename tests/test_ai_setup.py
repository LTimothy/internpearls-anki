"""The AI Backends window: enable/prefer/path/model/effort/test settings for
the three assistants, moved out of the wizard (see ai_dialog.py's setup
page, now just an entry point into this)."""
import os
import sys

FAKE = os.path.join(os.path.dirname(__file__), "fake_cli.py")


def test_open_ai_backends_builds_one_group_per_backend(anki, monkeypatch):
    from internpearls import ai_setup, ai_cli
    monkeypatch.setattr(ai_cli, "detect_backends", lambda cfg: {
        "backends": {k: {"path": None, "ok": False, "detail": "not found", "enabled": True}
                     for k in ai_cli.BACKENDS}, "chosen": None})
    dlg = ai_setup._AIBackendsDialog(anki.mw)
    assert set(dlg.groups) == set(ai_cli.BACKENDS)
    assert isinstance(dlg.groups["claude"].model, ai_setup.ModelEffortControls)
    for kind, meta in ai_cli.BACKENDS.items():
        group = dlg.groups[kind]
        assert meta["safety"] in group.status.text()
        assert meta["label"] in group.title()
        assert isinstance(group.model, ai_setup.ModelEffortControls)


def test_toggling_enabled_writes_config(anki, monkeypatch):
    from internpearls import ai_setup, ai_cli
    monkeypatch.setattr(ai_cli, "detect_backends", lambda cfg: {
        "backends": {k: {"path": "/bin/x", "ok": True, "detail": "1", "enabled": True}
                     for k in ai_cli.BACKENDS}, "chosen": "claude"})
    anki.mw._config = {}
    dlg = ai_setup._AIBackendsDialog(anki.mw)
    dlg.groups["agy"].enabled.setChecked(False)
    assert anki.mw._config["ai_backend_enabled"]["agy"] is False
    dlg.groups["codex"].preferred.setChecked(True)
    assert anki.mw._config["ai_backend"] == "codex"
    dlg.groups["claude"].path.setText("/opt/claude")
    dlg.groups["claude"]._commit_path()
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
    from internpearls import ai_setup, ai_cli
    monkeypatch.setattr(ai_cli, "detect_backends", lambda cfg: {
        "backends": {k: {"path": None, "ok": False, "detail": "not found", "enabled": True}
                     for k in ai_cli.BACKENDS}, "chosen": None})
    anki.mw._config = {"ai_backend": "claude", "ai_cli_path": "/opt/claude"}
    dlg = ai_setup._AIBackendsDialog(anki.mw)
    dlg.groups["codex"].path.setText("/opt/codex")
    dlg.groups["codex"]._commit_path()
    assert anki.mw._config["ai_cli_path"] == {
        "claude": "/opt/claude", "codex": "/opt/codex", "agy": ""}


def test_backend_enabled_defaults_true(anki):
    from internpearls.config import _cfg
    anki.mw._config = {}
    assert _cfg()["ai_backend_enabled"] == {"claude": True, "codex": True, "agy": True}
    anki.mw._config = {"ai_backend_enabled": {"agy": False}}
    assert _cfg()["ai_backend_enabled"]["agy"] is False


# === Test connection, Re-check, and detection status, per backend row =====

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
    assert not dlg.groups["claude"].test_btn.isEnabled()


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
    g = dlg.groups["claude"]
    assert g.test_btn.isEnabled()
    dlg._test("claude")
    assert not g.test_btn.isEnabled()   # disabled while the test runs
    _drain_conn_test(dlg)
    assert "working" in g.test_status.text().lower()
    assert g.test_btn.isEnabled()       # re-enabled once it's done


def test_recheck_mid_test_connection_does_not_reenable_or_double_run(anki, monkeypatch):
    # Minor fix carried over from the wizard's own setup page: Re-check used
    # to unconditionally reset every row's button/status, so pressing it
    # while a Test connection run was in flight re-enabled that button and
    # cleared its "Testing connection" text; a second click could then start
    # a concurrent test racing the first to write the same label.
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
    g = dlg.groups["claude"]
    dlg._test("claude")
    assert not g.test_btn.isEnabled()
    status_mid_test = g.test_status.text()
    n_refs = len(dlg._refs)

    dlg.recheck()   # simulates a Re-check click mid-test
    assert not g.test_btn.isEnabled()             # still disabled
    assert g.test_status.text() == status_mid_test  # not wiped

    dlg._test("claude")   # a second click while still running
    assert len(dlg._refs) == n_refs   # no second test was started

    _drain_conn_test(dlg)
    assert "working" in g.test_status.text().lower()
    assert g.test_btn.isEnabled()


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
    text = dlg.groups["claude"].test_status.text().lower()
    assert "not working" in text and "sign in" in text
    assert "traceback" not in text and "run `claude login`" not in text  # not raw stderr


def test_detect_status_reads_as_one_of_the_readmes_three_states(anki, monkeypatch):
    # I7: the row text must say something semantically matching the README's
    # three states, not render --version's raw output as if it were one.
    from internpearls import ai_cli, ai_setup
    monkeypatch.setattr(
        ai_cli, "find_cli",
        lambda kind, override="": sys.executable if kind == "claude" else None)
    monkeypatch.setattr(ai_cli, "probe", lambda kind, path: {"ok": True, "detail": "9.9.9"})
    dlg = ai_setup._AIBackendsDialog(anki.mw)
    assert "installed and working" in dlg.groups["claude"].status.text()
