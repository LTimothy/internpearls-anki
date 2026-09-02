"""Real-PyQt6 render check for the AI Backends window: the row list, the
settings panel's own grid, and the height budget that replaced the scroll area
(see qt_tests/test_ai_dialog.py, which kept only the wizard's own pages)."""
import harness
from aqt.qt import QApplication, QPoint, QPushButton
from internpearls import ai_cli, ai_setup


def _dialog(monkeypatch, found=("claude",)):
    harness.bootstrap()
    harness.app()      # Qt aborts outright if a QWidget is built before the app
    monkeypatch.setattr(ai_cli, "find_cli",
                        lambda kind, override="": "/bin/echo" if kind in found else None)
    monkeypatch.setattr(ai_cli, "probe",
                        lambda kind, path: {"ok": True, "detail": "v1"})
    dlg = ai_setup._AIBackendsDialog(None)
    dlg.show()
    harness.app().processEvents()
    return dlg


def test_backend_row_shows_model_and_effort_for_claude(monkeypatch):
    """Model/Effort honesty pattern (item A): claude has a verified --model and
    --effort flag, so it gets live controls, pre-filled with the add-on's own
    default (sonnet/medium), not left blank. Model is a closed, non-editable
    list of claude's known aliases plus Custom, not free text: that's what
    keeps its field column the same height/inset as Effort's own combo."""
    dlg = _dialog(monkeypatch)
    assert dlg.panel.kind == "claude"
    model = dlg.panel.model
    assert model.combo.isVisible() is True
    assert model.readonly.isVisible() is False
    assert model.combo.isEditable() is False
    assert model.combo.currentText() == "sonnet"
    assert model.custom.isVisible() is False
    assert model.effort.isVisible() is True
    assert model.effort.currentData() == ""   # "Default (medium)", not an override


def test_backend_row_shows_read_only_model_for_agy_and_hides_effort(monkeypatch):
    """agy has no verified way to honor a model or effort choice in headless
    mode, so it must not offer a control that lies about being respected: no
    Effort row is laid out for it at all, rather than an inert one."""
    dlg = _dialog(monkeypatch, found=("agy",))
    assert dlg.panel.kind == "agy"
    model = dlg.panel.model
    assert model.combo.isVisible() is False
    assert model.readonly.isVisible() is True
    assert "Flash" in model.readonly.text()
    assert model.effort.isVisible() is False
    assert [label for label, _field in model.rows()] == ["Model"]


def test_changing_model_and_effort_persists_to_config(monkeypatch):
    from aqt import mw
    dlg = _dialog(monkeypatch)
    model = dlg.panel.model
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
    model1 = dlg1.panel.model
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
    # now settles on codex as the preferred backend, and its Model field must
    # not be pre-filled with the "opus" set under claude above.
    dlg3 = ai_setup._AIBackendsDialog(None)
    dlg3.show()
    harness.app().processEvents()
    assert dlg3.panel.kind == "codex"
    assert dlg3.panel.model.custom.text() == ""

    dlg2.source_box.setPlainText("Some source material")
    dlg2._start_generation()
    dlg2._wait_for_worker(timeout=15)
    assert captured["model"] == ""   # argv-bound model is clean too


def test_model_and_effort_fields_align_and_custom_reveals_edit(monkeypatch):
    """The macOS bug this fixes: Model's field widget and Effort's field
    widget must start at the same x once mapped into the dialog's own
    coordinate space. A QFormLayout centred both of them mid-window; the
    settings panel's grid gives every field one left edge after a fixed-width,
    left-aligned label column. Also covers the Custom path end to end: picking
    it reveals the custom line edit at that same x, and a typed name persists
    to config the same way a known alias does."""
    from aqt import mw
    dlg = _dialog(monkeypatch)
    model = dlg.panel.model

    model_x = model.combo.mapTo(dlg, QPoint(0, 0)).x()
    effort_x = model.effort.mapTo(dlg, QPoint(0, 0)).x()
    assert model_x == effort_x

    # And every field starts after the label column rather than centred in the
    # window: the executable path above them shares that same left edge, which
    # is the property a centring QFormLayout broke.
    assert dlg.panel.path.mapTo(dlg, QPoint(0, 0)).x() == effort_x
    assert effort_x < dlg.width() // 2

    model.combo.setCurrentIndex(model.combo.findText("Custom"))
    harness.app().processEvents()
    assert model.custom.isVisible() is True

    custom_x = model.custom.mapTo(dlg, QPoint(0, 0)).x()
    assert custom_x == effort_x

    model.custom.setText("claude-3-opus-20240229")
    model.custom.textEdited.emit("claude-3-opus-20240229")
    conf = mw.addonManager.getConfig("internpearls")
    assert conf["ai_model"]["claude"] == "claude-3-opus-20240229"


def test_every_row_is_one_compact_block_with_its_own_chip(monkeypatch):
    """One row per backend, each carrying its own state chip, its label, and
    its safety sentence: the three tall group boxes this replaced said the same
    things three times over and pushed Close off the bottom."""
    from internpearls import widgets
    dlg = _dialog(monkeypatch)
    assert list(dlg.rows) == list(ai_cli.BACKENDS)
    for kind, meta in ai_cli.BACKENDS.items():
        row = dlg.rows[kind]
        assert meta["label"] in row.text()
        assert meta["safety"] in row.text()
        pills = [w.text() for w in row.findChildren(type(row.title))]
        assert widgets.CHIPS["found" if kind == "claude" else "notfound"] in pills


def test_ai_backends_dialog_fits_a_laptop_screen_unscrolled(monkeypatch):
    """The three backend groups plus the Re-check row used to sit directly in
    the dialog's own layout with nothing capping their height: fully unfolded
    that ran taller than an ordinary laptop screen and took the Close button
    off the bottom with it, which the scroll area then papered over. The
    compact rows make the whole window fit unscrolled, in both the state a
    reader with nothing installed opens onto and the state where every
    assistant is present, so the budget is asserted against both."""
    for found in ((), ("claude", "codex", "agy")):
        dlg = _dialog(monkeypatch, found=found)
        height = dlg.sizeHint().height()
        assert height <= 640, (
            f"AI Backends is {height}px tall with found={found or 'nothing'}")

        geo = QApplication.primaryScreen().availableGeometry()
        assert dlg.height() <= geo.height() - 60, (
            f"dialog is {dlg.height()}px tall against a {geo.height()}px screen")

        close_btn = next(b for b in dlg.findChildren(QPushButton) if b.text() == "Close")
        assert close_btn.isVisible()
        top_left = close_btn.mapTo(dlg, QPoint(0, 0))
        assert 0 <= top_left.y() <= dlg.height()
        assert top_left.y() + close_btn.height() <= dlg.height() + 1


def test_ai_backends_scene_reads_its_detection_state(shot):
    """The harness scene, in both states it can be rendered in: with nothing
    installed every row wears NOT FOUND, and with `found=1` every row wears
    FOUND. Rendered through harness.render rather than built directly, so the
    scene the other suites sweep is the one asserted about here."""
    from internpearls import widgets
    for opts, want in (({}, "notfound"), ({"found": 1}, "found")):
        dlg = shot("ai-backends", **opts).dialog
        for kind, row in dlg.rows.items():
            pills = [w.text() for w in row.findChildren(type(row.title))]
            assert widgets.CHIPS[want] in pills, f"{kind} in {opts}"
