"""Real-PyQt6 render check for the AI Backends window: the Model/Effort
controls moved here from the wizard (see qt_tests/test_ai_dialog.py, which
kept only the wizard's own pages)."""
import harness
from aqt.qt import QApplication, QPoint, QPushButton
from internpearls import ai_cli, ai_setup


def test_backend_row_shows_model_and_effort_for_claude(monkeypatch):
    """Model/Effort honesty pattern (item A): claude has a verified --model and
    --effort flag, so it gets live controls, pre-filled with the add-on's own
    default (sonnet/medium), not left blank. Model is a closed, non-editable
    list of claude's known aliases plus Custom, not free text: that's what
    keeps its field column the same height/inset as Effort's own combo."""
    harness.bootstrap()
    monkeypatch.setattr(ai_cli, "find_cli",
                        lambda kind, override="": "/bin/echo"
                        if kind == "claude" else None)
    monkeypatch.setattr(ai_cli, "probe",
                        lambda kind, path: {"ok": True, "detail": "v1"})
    dlg = ai_setup._AIBackendsDialog(None)
    dlg.show()
    harness.app().processEvents()
    model = dlg.groups["claude"].model
    assert model.combo.isVisible() is True
    assert model.readonly.isVisible() is False
    assert model.combo.isEditable() is False
    assert model.combo.currentText() == "sonnet"
    assert model.custom.isVisible() is False
    assert model.effort.isVisible() is True
    assert model.effort.currentData() == ""   # "Default (medium)", not an override


def test_backend_row_shows_read_only_model_for_agy_and_hides_effort(monkeypatch):
    """agy has no verified way to honor a model or effort choice in headless
    mode, so it must not offer a control that lies about being respected."""
    harness.bootstrap()
    monkeypatch.setattr(ai_cli, "find_cli",
                        lambda kind, override="": "/bin/echo"
                        if kind == "agy" else None)
    monkeypatch.setattr(ai_cli, "probe",
                        lambda kind, path: {"ok": True, "detail": "v1"})
    dlg = ai_setup._AIBackendsDialog(None)
    dlg.show()
    harness.app().processEvents()
    model = dlg.groups["agy"].model
    assert model.combo.isVisible() is False
    assert model.readonly.isVisible() is True
    assert "Flash" in model.readonly.text()
    assert model.effort.isVisible() is False


def test_changing_model_and_effort_persists_to_config(monkeypatch):
    from aqt import mw
    harness.bootstrap()
    monkeypatch.setattr(ai_cli, "find_cli",
                        lambda kind, override="": "/bin/echo"
                        if kind == "claude" else None)
    monkeypatch.setattr(ai_cli, "probe",
                        lambda kind, path: {"ok": True, "detail": "v1"})
    dlg = ai_setup._AIBackendsDialog(None)
    model = dlg.groups["claude"].model
    model.combo.setCurrentIndex(model.combo.findText("opus"))
    idx = model.effort.findData("high")
    model.effort.setCurrentIndex(idx)
    conf = mw.addonManager.getConfig("internpearls")
    assert conf["ai_model"]["claude"] == "opus"
    assert conf["ai_effort"]["claude"] == "high"


def test_model_set_under_claude_does_not_leak_into_codex(monkeypatch):
    """Item 1: ai_model/ai_effort are stored per backend kind. Setting a model
    for claude in the AI Backends window must not pre-fill or get sent for
    codex when it's the backend the wizard detects on a later open: the leak
    this test reproduces would otherwise silently send `-m opus` (or now
    `--model opus`) to a backend the user never chose that model for."""
    from aqt import mw
    from internpearls import ai_dialog
    harness.bootstrap()

    # First: only claude detected, set its model to "opus" via the AI
    # Backends window.
    monkeypatch.setattr(ai_cli, "find_cli",
                        lambda kind, override="": "/bin/echo"
                        if kind == "claude" else None)
    monkeypatch.setattr(ai_cli, "probe",
                        lambda kind, path: {"ok": True, "detail": "v1"})
    dlg1 = ai_setup._AIBackendsDialog(None)
    model1 = dlg1.groups["claude"].model
    model1.combo.setCurrentIndex(model1.combo.findText("opus"))
    conf = mw.addonManager.getConfig("internpearls")
    assert conf["ai_model"]["claude"] == "opus"
    assert conf["ai_model"].get("codex", "") == ""

    # Second: only codex detected this time, and the wizard runs generation
    # with it: the stale claude-scoped value must not leak into codex's argv.
    monkeypatch.setattr(ai_cli, "find_cli",
                        lambda kind, override="": "/bin/echo"
                        if kind == "codex" else None)
    monkeypatch.setattr(ai_cli, "supports_flag", lambda path, flag, **kw: True)
    captured = {}

    def fake_run_generation(kind, path, prompt, mode, scratch, image_paths=(),
                            on_event=None, cancel=None, timeout=None,
                            model="", effort=""):
        captured["model"] = model
        return {"text": "[]", "tokens": 0, "duration_s": 0.1}
    monkeypatch.setattr(ai_cli, "run_generation", fake_run_generation)

    dlg2 = ai_dialog._GenerateDialog()
    dlg2.show()
    harness.app().processEvents()
    assert dlg2.session.backend == "codex"

    # Check the UI itself, not just config: a fresh AI Backends window opened
    # now must not show codex's free-text model field pre-filled with the
    # "opus" set under claude above.
    dlg3 = ai_setup._AIBackendsDialog(None)
    dlg3.show()
    harness.app().processEvents()
    assert dlg3.groups["codex"].model.custom.text() == ""

    dlg2.source_box.setPlainText("Some source material")
    dlg2._start_generation()
    dlg2._wait_for_worker(timeout=15)
    assert captured["model"] == ""   # argv-bound model is clean too


def test_model_and_effort_fields_align_and_custom_reveals_edit(monkeypatch):
    """The macOS bug this fixes: Model's field widget and Effort's field
    widget must start at the same x once mapped into the dialog's own
    coordinate space, since an editable combo (the old Model) renders with a
    different inset than a non-editable one (Effort, always). Also covers the
    Custom path end to end: picking it reveals the custom line edit at that
    same x, and a typed name persists to config the same way a known alias
    does."""
    from aqt import mw
    harness.bootstrap()
    monkeypatch.setattr(ai_cli, "find_cli",
                        lambda kind, override="": "/bin/echo"
                        if kind == "claude" else None)
    monkeypatch.setattr(ai_cli, "probe",
                        lambda kind, path: {"ok": True, "detail": "v1"})
    dlg = ai_setup._AIBackendsDialog(None)
    dlg.show()
    harness.app().processEvents()
    model = dlg.groups["claude"].model

    model_x = model.combo.mapTo(dlg, QPoint(0, 0)).x()
    effort_x = model.effort.mapTo(dlg, QPoint(0, 0)).x()
    assert model_x == effort_x

    model.combo.setCurrentIndex(model.combo.findText("Custom"))
    harness.app().processEvents()
    assert model.custom.isVisible() is True

    custom_x = model.custom.mapTo(dlg, QPoint(0, 0)).x()
    assert custom_x == effort_x

    model.custom.setText("claude-3-opus-20240229")
    model.custom.textEdited.emit("claude-3-opus-20240229")
    conf = mw.addonManager.getConfig("internpearls")
    assert conf["ai_model"]["claude"] == "claude-3-opus-20240229"


def test_ai_backends_dialog_fits_the_screen_and_close_is_reachable(monkeypatch):
    """The three backend groups plus the Re-check row used to sit directly in
    the dialog's own layout, with nothing to cap their combined height: fully
    unfolded (every backend "not found", the widest each group's status/hint
    text gets) that ran taller than an ordinary laptop screen and took the
    Close button off the bottom with it. They now live inside a QScrollArea,
    with Close kept outside it, and the dialog opens at a size clamped to the
    available screen (see _AIBackendsDialog.__init__)."""
    harness.bootstrap()
    monkeypatch.setattr(ai_cli, "find_cli", lambda kind, override="": None)
    dlg = ai_setup._AIBackendsDialog(None)
    dlg.show()
    harness.app().processEvents()

    geo = QApplication.primaryScreen().availableGeometry()
    assert dlg.height() <= geo.height() - 60, (
        f"dialog is {dlg.height()}px tall against a {geo.height()}px screen")

    close_btn = next(b for b in dlg.findChildren(QPushButton) if b.text() == "Close")
    assert close_btn.isVisible()
    top_left = close_btn.mapTo(dlg, QPoint(0, 0))
    assert 0 <= top_left.y() <= dlg.height()
    assert top_left.y() + close_btn.height() <= dlg.height() + 1
