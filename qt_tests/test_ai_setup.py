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
    assert model.combo.isEditable() is False
    assert model.combo.currentText() == "sonnet"
    assert model.custom.isVisible() is False
    assert model.effort.isVisible() is True
    assert model.effort.currentData() == ""   # "Default (medium)", not an override


def test_backend_row_shows_free_text_model_and_effort_for_agy(monkeypatch):
    """agy documents --model and --effort in its own help and lists ids under
    `agy models`, so Model is free text (no short alias list to close over)
    and Effort is a real combo whose blank entry leaves the add-on's own
    default (gemini-3.8-flash-low, low) in play rather than an override."""
    dlg = _dialog(monkeypatch, found=("agy",))
    assert dlg.panel.kind == "agy"
    model = dlg.panel.model
    assert model.combo.isVisible() is False
    assert model.custom.isVisible() is True
    assert "gemini" in model.custom.placeholderText()
    assert model.effort.isVisible() is True
    assert model.effort.currentData() == ""
    assert model.effort.itemText(0) == "Default (low)"
    assert [label for label, _field in model.rows()] == ["Model", "Effort"]
    assert model.values() == ("", "")


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
                            model="", effort="", log_path=None,
                            redact_texts=()):
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


def test_free_tier_pill_reflects_metadata_not_a_subscription_substring(monkeypatch):
    """The pill used to key off "free tier" in meta["subscription"], which
    also matched Codex's "free tier: about 50 coding messages a day" wording
    and gave it the agy-specific "throttled" pill. Explicit
    BACKENDS[kind]["free_tier"] now decides the pill per backend: agy is
    throttled, codex is capped (a different pill), claude has none."""
    dlg = _dialog(monkeypatch, found=("claude", "codex", "agy"))
    pills = {kind: [w.text() for w in dlg.rows[kind].findChildren(
        type(dlg.rows[kind].title))] for kind in ("claude", "codex", "agy")}
    assert "free tier, throttled" not in pills["claude"]
    assert "free tier available" not in pills["claude"]
    assert "free tier, throttled" not in pills["codex"]
    assert "free tier available" in pills["codex"]
    assert "free tier, throttled" in pills["agy"]
    assert "free tier available" not in pills["agy"]


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
        # 720, not 640: the v0.60.0 safety sentences are longer (they spell out
        # the sandboxed toolset each backend actually gets), and each row now
        # also carries its own Model/effort default line.
        assert height <= 720, (
            f"AI Backends is {height}px tall with found={found or 'nothing'}")

        geo = QApplication.primaryScreen().availableGeometry()
        assert dlg.height() <= geo.height() - 60, (
            f"dialog is {dlg.height()}px tall against a {geo.height()}px screen")

        close_btn = next(b for b in dlg.findChildren(QPushButton) if b.text() == "Close")
        assert close_btn.isVisible()
        top_left = close_btn.mapTo(dlg, QPoint(0, 0))
        assert 0 <= top_left.y() <= dlg.height()
        assert top_left.y() + close_btn.height() <= dlg.height() + 1


def test_ai_backends_dialog_minimum_width_stays_reasonable(monkeypatch):
    """The agy label line used to carry the shutdown note unwrapped
    ("(agy, formerly Gemini CLI, which stopped serving personal Google AI Pro
    and Ultra accounts on 2026-06-18)"), which pushed minimumSizeHint().width()
    to about 1200px. The note now lives on the wrapped muted detail line
    instead, so the label stays a short alias and the dialog's minimum width
    stays sane with all three backends found."""
    dlg = _dialog(monkeypatch, found=("claude", "codex", "agy"))
    width = dlg.minimumSizeHint().width()
    assert width <= 800, f"AI Backends minimum width is {width}px"


def test_backend_rows_never_clip_their_wrapped_detail_line():
    """The muted third line of every row ("Works with a ... . <safety>.") wraps to
    two lines at the width this window actually opens at, and used to be cut off
    mid-glyph at the row's bottom edge on a real screen.

    A word-wrapped QLabel reports a one-line minimum height, and a QVBoxLayout
    builds the window's minimum height out of those minimums, so the window's own
    minimum was shorter than its text: at that height every row was squeezed to one
    line. Measured at the smallest height the window will take, which is the state
    that clipped, across the widths a real screen puts it at (below its minimum
    width Qt clamps, which is itself worth pinning: that is the width the reader
    reported this at).

    Rendered through harness.render rather than the module-level _dialog helper so
    what is asserted here is the scene the render tool shows.
    """
    dlg = harness.render("ai-backends", found=1).dialog
    for width in (560, 640, 700, 773, 900):
        dlg.resize(width, dlg.minimumSizeHint().height())
        harness.app().processEvents()
        for kind, row in dlg.rows.items():
            label = row.detail
            needed = label.heightForWidth(label.width())
            assert label.height() >= needed, (
                f"{kind}'s detail line is {label.height()}px tall at "
                f"{label.width()}px wide, where its text needs {needed}px "
                f"(window {dlg.width()}x{dlg.height()})")
            bottom = label.mapTo(row, label.rect().bottomLeft()).y()
            assert bottom < row.height(), (
                f"{kind}'s detail line ends {bottom}px down a {row.height()}px "
                f"row, so its last line is clipped (window {dlg.width()}x"
                f"{dlg.height()})")


def test_settings_panel_controls_share_one_left_edge():
    """The owner's screenshot: Executable path, Model, Effort, and Test
    connection should all start at the same x inside the settings panel.
    Measured with mapTo(panel, QPoint(0, 0)).x() on each control's own
    top-level widget in the panel's grid (the path edit itself, Model's
    field container, Effort's combo, and the Test connection button's own
    container), across every backend (Effort is absent for codex, which has
    no verified effort flag)."""
    dlg = harness.render("ai-backends", found=1).dialog
    for kind in ai_cli.BACKENDS:
        dlg.use_backend(kind)
        harness.app().processEvents()
        panel = dlg.panel
        xs = {"path": panel.path.mapTo(panel, QPoint(0, 0)).x(),
             "model": panel.model.model_field.mapTo(panel, QPoint(0, 0)).x(),
             "test": panel.test_btn.mapTo(panel, QPoint(0, 0)).x()}
        if panel.model.has_effort:
            xs["effort"] = panel.model.effort.mapTo(panel, QPoint(0, 0)).x()
        assert len(set(xs.values())) == 1, (
            f"{kind}'s settings controls don't share a left edge: {xs}")


def test_settings_panel_controls_share_one_visual_left_edge():
    """The truer version of the geometry check above: what the running style
    actually draws, not just where QGridLayout put each control. Under
    macOS's native style a QComboBox's bezel starts a few pixels right of its
    own geometry (space reserved for a focus ring it only draws when
    focused), which the plain geometry check above can't see at all - it
    passed unmodified on this very panel while a reader's real macOS
    screenshot showed Model and Effort visibly indented past Executable path
    and Test connection. Under Fusion, which is what running pytest here
    always renders with, harness.visual_left degenerates to left_x exactly
    (see its own docstring), so this assertion is the plain geometry check
    again in that case; it is written against the truer property so it also
    means something the day this suite ever runs against a native style.
    """
    dlg = harness.render("ai-backends", found=1).dialog
    for kind in ai_cli.BACKENDS:
        dlg.use_backend(kind)
        harness.app().processEvents()
        panel = dlg.panel
        model_leading = (panel.model.combo if panel.model.combo.isVisible()
                         else panel.model.custom)
        xs = {"path": harness.visual_left(dlg, panel.path),
             "model": harness.visual_left(dlg, model_leading),
             "test": harness.visual_left(dlg, panel.test_btn)}
        if panel.model.has_effort:
            xs["effort"] = harness.visual_left(dlg, panel.model.effort)
        spread = max(xs.values()) - min(xs.values())
        assert spread <= 1, (
            f"{kind}'s settings controls don't share a drawn left edge: {xs}")


def test_ai_backends_rows_dont_clip_on_first_open(monkeypatch):
    """The reader's own report: dark mode, window about 726px wide, three
    backends found, every row's muted third line cut off mid-glyph on FIRST
    open, and the clipping does not persist past a Re-check (which rebuilds
    the rows). The existing multi-width sweep above missed this because it
    resizes and processes events at several widths in sequence, which gives
    every layout pass in between a chance to settle before anything is
    asserted.

    Reproduced here the way the reader actually saw it: BOTH dimensions are
    forced small, before the window is ever shown, the way the real open_size
    clamp can (a screen short enough that `open_h` lands under the layout's
    true minimum, ai_setup.py ~519-526). Exactly one show, one processEvents
    call, then check: no second resize, no second event-loop turn to give a
    later layout pass a chance to paper over a bad first one. Checks the
    window's own height against its layout's real minimum, not only each
    row's label, since a fix that only grew individual rows without ever
    pinning the window itself could still ship the clipping this guards
    against (see _WrappedHint's docstring, ai_setup.py, for why reading
    top.minimumSizeHint() synchronously inside a child's resizeEvent was not
    the fix it looked like)."""
    harness.bootstrap()
    harness.app()
    harness.apply_theme("dark")
    monkeypatch.setattr(ai_cli, "find_cli", lambda kind, override="": "/bin/echo")
    monkeypatch.setattr(ai_cli, "probe",
                        lambda kind, path: {"ok": True, "detail": "v1"})
    dlg = ai_setup._AIBackendsDialog(None)
    dlg.resize(726, 500)
    dlg.show()
    harness.app().processEvents()
    try:
        true_min = dlg.layout().minimumSize()
        assert dlg.height() >= true_min.height(), (
            f"window opened at {dlg.width()}x{dlg.height()}, below its "
            f"layout's true minimum of {true_min.width()}x{true_min.height()}")
        for kind, row in dlg.rows.items():
            label = row.detail
            needed = label.heightForWidth(label.width())
            assert label.height() >= needed, (
                f"{kind}'s detail line is {label.height()}px tall at "
                f"{label.width()}px wide on first open, where its text needs "
                f"{needed}px (window {dlg.width()}x{dlg.height()})")
            bottom = label.mapTo(row, label.rect().bottomLeft()).y()
            assert bottom < row.height(), (
                f"{kind}'s detail line ends {bottom}px down a {row.height()}px "
                f"row on first open, so its last line is clipped (window "
                f"{dlg.width()}x{dlg.height()})")
    finally:
        dlg.close()


def test_ai_backends_window_settles_even_with_a_stale_minimum_hint_read(monkeypatch):
    """22589b4's mechanism (see _WrappedHint's docstring, ai_setup.py) read
    top.minimumSizeHint() synchronously inside a child's resizeEvent and
    resized the window from it. The reviewer found that read can be stale on
    a real screen: the intermediate box layouts between a row and the window
    have not always caught up to a change this same layout pass already made
    elsewhere, so a Python-level minimumSizeHint() read is not something a fix
    should have to trust.

    Simulated here without needing a real screen: patch the window's own
    minimumSizeHint() to always answer a fixed, too-small size while it is
    built, resized, and shown, standing in for a lying (or merely stale) read
    at every one of those points, then restore the real one and give the
    window one more event-loop turn. Pins the property, not a cause: the window
    ends at or above its layout's real minimum even though every Python-level
    minimumSizeHint() read lied. Instrumented runs showed Qt's default top-level
    constraint already enforces this offscreen, so the SetMinimumSize constraint
    and the show-time settle are belt and braces for the real screen, where the
    first paint could otherwise commit before the wrapped labels reported their
    height; this test would stay green with either of them removed."""
    harness.bootstrap()
    harness.app()
    harness.apply_theme("dark")
    monkeypatch.setattr(ai_cli, "find_cli", lambda kind, override="": "/bin/echo")
    monkeypatch.setattr(ai_cli, "probe",
                        lambda kind, path: {"ok": True, "detail": "v1"})
    from aqt.qt import QSize
    stale = QSize(700, 480)
    with monkeypatch.context() as stale_patch:
        stale_patch.setattr(ai_setup._AIBackendsDialog, "minimumSizeHint",
                            lambda self: stale)
        dlg = ai_setup._AIBackendsDialog(None)
        dlg.resize(726, 500)
        dlg.show()
        harness.app().processEvents()
    # the real minimumSizeHint again, the way a later layout pass sees it
    harness.app().processEvents()
    try:
        true_min = dlg.layout().minimumSize()
        assert dlg.height() >= true_min.height(), (
            f"window is {dlg.width()}x{dlg.height()} after a stale first "
            f"minimumSizeHint() read, below its layout's true minimum of "
            f"{true_min.width()}x{true_min.height()}")
        for kind, row in dlg.rows.items():
            label = row.detail
            needed = label.heightForWidth(label.width())
            bottom = label.mapTo(row, label.rect().bottomLeft()).y()
            assert label.height() >= needed and bottom < row.height(), (
                f"{kind}'s detail line clipped after a stale minimumSizeHint() "
                f"read (window {dlg.width()}x{dlg.height()})")
    finally:
        dlg.close()


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


def test_ai_backends_rows_show_the_v0_60_default_and_setting_wording(
        monkeypatch, tmp_path):
    """Every row's Model line shows the new 'Model: <id>, effort: <level>
    (add-on default)'/(your setting) wording, and a rendered PNG of the
    scene is saved for a human to look at."""
    import os
    from aqt import mw

    dlg = _dialog(monkeypatch, found=("claude", "codex", "agy"))
    monkeypatch.setattr(ai_cli, "configured_default",
                        lambda path=None: ("gpt-5.1-codex", "high"))
    monkeypatch.setattr(ai_cli, "supports_flag", lambda path, flag, **kw: True)
    mw.addonManager.writeConfig("internpearls", {
        **mw.addonManager.getConfig("internpearls"),
        "ai_model": {"claude": "opus", "codex": "", "agy": ""},
        "ai_effort": {"claude": "high", "codex": "", "agy": ""}})
    dlg.recheck()
    QApplication.instance().processEvents()

    claude_text = dlg.rows["claude"].text()
    codex_text = dlg.rows["codex"].text()
    agy_text = dlg.rows["agy"].text()
    assert "Model: opus, effort: high (your setting)" in claude_text
    assert "Model: gpt-5.1-codex, effort: high (Codex's own setting)" in codex_text
    assert ("Model: gemini-3.8-flash-low, effort: low (add-on default)"
           in agy_text)

    out_dir = ("/private/tmp/claude-501/-Users-tim-Claude-Code/"
              "602a1035-6350-457d-8113-d6af3530df6f/scratchpad")
    os.makedirs(out_dir, exist_ok=True)
    png = os.path.join(out_dir, "ai-backends-v0.60.0-wording.png")
    dlg.grab().toImage().save(png, "PNG")
    assert os.path.exists(png)
