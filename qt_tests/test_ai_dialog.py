"""Real-PyQt6 render check for the "Generate cards with AI" wizard.

Drives the dialog through a real (mocked-CLI) generation so the review page
renders its actual card rows, not just an empty stack page: that's the one
page whose layout depends on session state built up by the earlier pages.
"""
import json

import harness
from internpearls import ai_cli, ai_dialog


def test_wizard_renders_all_pages(monkeypatch):
    harness.bootstrap()
    app = harness.app()

    monkeypatch.setattr(ai_cli, "find_cli",
                        lambda kind, override="": "/bin/echo"
                        if kind == "claude" else None)
    monkeypatch.setattr(ai_cli, "probe",
                        lambda kind, path: {"ok": True, "detail": "v1"})
    cards = [{"note_type": "Study Deck - Basic",
             "fields": {"Front": "q", "Back": "a"},
             "tags": [], "images": [], "rationale": "r"}]
    monkeypatch.setattr(
        ai_cli, "run_generation",
        lambda kind, path, prompt, mode, scratch, image_paths=(), on_event=None,
              cancel=None, timeout=None, model=None, effort=None: {"text": json.dumps(cards), "tokens": 15,
                                          "duration_s": 12.3})

    dlg = ai_dialog._GenerateDialog()
    dlg.show()
    app.processEvents()
    assert dlg.stack.currentWidget() is dlg.input_page   # backend found

    dlg.source_box.setPlainText("Regional block landmarks and needle depths")
    dlg._start_generation()
    app.processEvents()
    assert dlg.stack.currentWidget() is dlg.progress_page
    dlg._wait_for_worker(timeout=15)   # joins the worker thread, drains the timer

    assert dlg.stack.currentWidget() is dlg.review_page
    assert dlg.session.cards   # generation actually produced a card
    # Rows are hairlined BETWEEN each other (a QFrame separator, not around), so
    # the widget count is one row per card plus one separator per gap; the
    # trailing addStretch() is a spacer item, not a widget, so it contributes
    # nothing here.
    from aqt.qt import QFrame
    row_widgets = [dlg.cards_lay.itemAt(i).widget() for i in range(dlg.cards_lay.count())]
    row_widgets = [w for w in row_widgets if w is not None and not isinstance(w, QFrame)]
    assert len(row_widgets) == len(dlg.session.cards)   # review rows built

    for page in (dlg.setup_page, dlg.input_page, dlg.progress_page,
                dlg.review_page):
        dlg.stack.setCurrentWidget(page)
        app.processEvents()
        dlg.stack.currentWidget().repaint()

    assert dlg.windowTitle() == "Intern Pearls: Generate cards with AI"


def test_import_enables_real_undo_action_with_the_native_shortcut(monkeypatch):
    """Findings 1 and 2, against the real Qt objects this bug was actually
    found with.

    Finding 1: a successful import used to write a real, mergeable undo entry
    that nothing ever told Anki's main window about, so Edit > Undo stayed
    greyed out in the running app even though col.undo() genuinely worked
    headless. This drives the real "Generate cards with AI" flow through
    _do_import() and checks the one thing that actually matters: the mock
    main window's own undo QAction (mirroring mw.form.actionUndo, which real
    Anki's own update_undo_actions() enables from col.undo_status()) flips
    from disabled to enabled.

    Finding 2: the completion message hardcoded "Ctrl+Z" on every platform.
    Because this file runs against REAL PyQt6 (see harness.bootstrap), Qt's
    own QKeySequence(StandardKey.Undo).toString(NativeText) here is not a
    mock's guess at platform behavior: it is exactly what a real Anki
    Edit menu on this machine renders. Asserting the message names that,
    and never the literal "Ctrl+Z" unless this machine's own native
    rendering happens to equal it, is the strongest check available for
    "the shortcut shown is the one this platform's Edit menu actually
    uses."
    """
    mock, q = harness.bootstrap()
    app = harness.app()
    import mock_anki
    from aqt.qt import QKeySequence

    native = QKeySequence(QKeySequence.StandardKey.Undo).toString(
        QKeySequence.SequenceFormat.NativeText)
    assert ai_dialog._undo_shortcut() == native

    # A fresh collection/undo-action pair, independent of whatever an earlier
    # test in this process already did: mirrors what tests/conftest.py's
    # `anki` fixture does per test for the fake-Qt suite.
    mock.mw.col = mock_anki.MockCollection()
    mock.mw.reset_count = 0
    mock.mw.update_undo_actions()
    assert mock.mw.form.actionUndo.isEnabled() is False   # nothing to undo yet

    monkeypatch.setattr(ai_cli, "find_cli",
                        lambda kind, override="": "/bin/echo"
                        if kind == "claude" else None)
    monkeypatch.setattr(ai_cli, "probe",
                        lambda kind, path: {"ok": True, "detail": "v1"})
    cards = [{"note_type": "Study Deck - Basic",
             "fields": {"Front": "q", "Back": "a"},
             "tags": [], "images": [], "rationale": "r"}]
    monkeypatch.setattr(
        ai_cli, "run_generation",
        lambda kind, path, prompt, mode, scratch, image_paths=(), on_event=None,
              cancel=None, timeout=None, model=None, effort=None: {"text": json.dumps(cards), "tokens": 15,
                                          "duration_s": 12.3})

    dlg = ai_dialog._GenerateDialog()
    dlg.show()
    dlg.source_box.setPlainText("Regional block landmarks and needle depths")
    dlg._start_generation()
    app.processEvents()
    dlg._wait_for_worker(timeout=15)
    assert dlg.stack.currentWidget() is dlg.review_page

    n = dlg._do_import()
    assert n == 1
    assert mock.mw.reset_count > 0                       # the UI was notified at all
    assert mock.mw.form.actionUndo.isEnabled() is True    # ...and undo is reachable
    assert f"{native} reverts it" in mock.gui.tooltips[-1]


def test_review_row_renders_a_real_image_thumbnail(monkeypatch, tmp_path):
    """I2, against a real QImage/rich-text QLabel: a card with a web image
    resolves off the UI thread and the review row actually paints a
    thumbnail (not just a text placeholder) plus the source host, and the
    card starts unchecked."""
    harness.bootstrap()
    app = harness.app()
    from internpearls import ai_dialog as ad
    from aqt.qt import QImage, QColor

    monkeypatch.setattr(ai_cli, "find_cli",
                        lambda kind, override="": "/bin/echo"
                        if kind == "claude" else None)
    monkeypatch.setattr(ai_cli, "probe",
                        lambda kind, path: {"ok": True, "detail": "v1"})
    cards = [{"note_type": "Study Deck - Basic",
             "fields": {"Front": "q", "Back": "a"},
             "tags": [], "images": [{"source": "url:https://example.com/pic.png",
                                     "alt": "", "attribution": ""}],
             "rationale": "r"}]
    monkeypatch.setattr(
        ai_cli, "run_generation",
        lambda kind, path, prompt, mode, scratch, image_paths=(), on_event=None,
              cancel=None, timeout=None, model=None, effort=None: {"text": json.dumps(cards), "tokens": 15,
                                          "duration_s": 12.3})
    png = str(tmp_path / "pic.png")
    image = QImage(40, 30, QImage.Format.Format_RGB32)
    image.fill(QColor("#ff00ff"))
    image.save(png, "PNG")
    png_bytes = open(png, "rb").read()
    monkeypatch.setattr(ad, "fetch_card_image", lambda url: (png_bytes, "png"))

    dlg = ad._GenerateDialog()
    dlg.source_box.setPlainText("Regional block landmarks and needle depths")
    dlg._start_generation()
    dlg._wait_for_worker(timeout=15)
    app.processEvents()

    assert dlg.stack.currentWidget() is dlg.review_page
    assert dlg.session.included == [False]   # I2: starts excluded until seen
    row = dlg.cards_lay.itemAt(0).widget()
    from aqt.qt import QLabel
    text = " ".join(l.text() for l in row.findChildren(QLabel))
    assert "<img" in text
    assert "example.com" in text


def test_review_checkbox_click_updates_count_and_button_label(monkeypatch):
    """Bug: clicking a review card's checkbox visibly did nothing: the box
    stayed checked, the "N included" header never moved, and the Import
    button's label never moved. The underlying wiring (toggled -> write
    session.included) actually worked; nothing ever refreshed the header or
    button off that write, so a toggle that DID happen left no visible sign it
    had, which reads exactly like a broken control. Drives the real
    QCheckBox's own click() (a genuine Qt press+release, not a direct call
    into session state), so this fails again if the widget wiring itself ever
    breaks, not just the summary refresh.
    """
    harness.bootstrap()
    app = harness.app()

    monkeypatch.setattr(ai_cli, "find_cli",
                        lambda kind, override="": "/bin/echo"
                        if kind == "claude" else None)
    monkeypatch.setattr(ai_cli, "probe",
                        lambda kind, path: {"ok": True, "detail": "v1"})
    cards = [{"note_type": "Study Deck - Basic",
             "fields": {"Front": "q1", "Back": "a1"},
             "tags": [], "images": [], "rationale": "r"},
            {"note_type": "Study Deck - Basic",
             "fields": {"Front": "q2", "Back": "a2"},
             "tags": [], "images": [], "rationale": "r"}]
    monkeypatch.setattr(
        ai_cli, "run_generation",
        lambda kind, path, prompt, mode, scratch, image_paths=(), on_event=None,
              cancel=None, timeout=None, model=None, effort=None: {"text": json.dumps(cards), "tokens": 15,
                                          "duration_s": 12.3})

    dlg = ai_dialog._GenerateDialog()
    dlg.show()
    dlg.source_box.setPlainText("Regional block landmarks and needle depths")
    dlg._start_generation()
    dlg._wait_for_worker(timeout=15)
    app.processEvents()

    assert dlg.session.included == [True, True]
    assert "2 included" in dlg.review_header.text()
    assert dlg.import_btn.text() == "Import 2 cards"

    box0 = dlg.include_boxes[0]
    box0.click()
    app.processEvents()

    assert box0.isChecked() is False                     # the widget itself flips
    assert dlg.session.included == [False, True]          # and the model behind it
    assert "1 included" in dlg.review_header.text()       # ...visibly, in the header
    assert dlg.import_btn.text() == "Import 1 card"       # ...and the button

    box0.click()   # toggling back must be just as visible
    app.processEvents()
    assert box0.isChecked() is True
    assert dlg.session.included == [True, True]
    assert "2 included" in dlg.review_header.text()
    assert dlg.import_btn.text() == "Import 2 cards"


def test_import_button_click_shows_dialog_instead_of_raising(monkeypatch):
    """Bug: _do_import ran straight off Qt's clicked signal with no guard of
    its own. @_safe on generate_cards() only catches an exception that unwinds
    back through *its own* call stack, and a signal dispatch never does:
    real PyQt/PySide hand a slot's exception to the process's own excepthook
    instead, which is Anki's raw "encountered a problem" box, not this add-on's
    dialog. Drives the real button's click() (an actual Qt signal emission)
    rather than calling dlg._do_import() directly, so this exercises the exact
    dispatch path the bug lived in: a raise inside a directly-invoked method
    call would never have caught this class of bug.
    """
    harness.bootstrap()
    app = harness.app()
    monkeypatch.setattr(ai_cli, "find_cli",
                        lambda kind, override="": "/bin/echo"
                        if kind == "claude" else None)
    monkeypatch.setattr(ai_cli, "probe",
                        lambda kind, path: {"ok": True, "detail": "v1"})

    dlg = ai_dialog._GenerateDialog()
    dlg.show()
    # A card naming a note type this collection doesn't have: exactly what
    # add_generated_notes' own atomic check rejects (see collection.py).
    dlg.session.cards = [{"note_type": "Study Deck - Cloze",
                          "fields": {"Text": "x"}, "tags": [], "images": [],
                          "_media_files": []}]
    dlg.session.included = [True]
    dlg.session.image_data = {}

    warnings = []
    monkeypatch.setattr(ai_dialog, "_warn", lambda text, **kw: warnings.append(text))

    dlg.import_btn.click()   # a real Qt signal, not dlg._do_import() called directly
    app.processEvents()

    assert warnings, "the failure must reach the add-on's own dialog, not vanish"
    assert "Study Deck - Cloze" in warnings[0]
    assert dlg.isVisible()   # still open: an unhandled exception here would tear it down


def test_completion_timer_exception_shows_dialog_and_recovers_to_input(monkeypatch):
    """B/C, against a real QTimer: _poll_worker (and _poll_image_worker) fire off
    Qt's own timeout signal the same way a button's clicked signal fires, so an
    unguarded exception there reached Anki's raw crash box too: and it was worse
    than an unguarded button click, not just as bad. By its last firing, the poll
    has already latched _gen_done and stopped its own timer, so if the completion
    code itself then raised, nothing was left running for Cancel to cancel and
    nothing ever moved the dialog off the progress page: stuck forever, with a
    Cancel button wired to a no-op.

    Emits the real QTimer's own `timeout` signal (dlg._timer.timeout.emit()):
    an actual Qt signal dispatch, not a direct call into _finish_generation,
    so this exercises the exact path the bug lived in.
    """
    harness.bootstrap()
    app = harness.app()
    from internpearls import ai_logic

    monkeypatch.setattr(ai_cli, "find_cli",
                        lambda kind, override="": "/bin/echo"
                        if kind == "claude" else None)
    monkeypatch.setattr(ai_cli, "probe",
                        lambda kind, path: {"ok": True, "detail": "v1"})
    monkeypatch.setattr(
        ai_cli, "run_generation",
        lambda kind, path, prompt, mode, scratch, image_paths=(), on_event=None,
              cancel=None, timeout=None, model=None, effort=None: {"text": "[]", "tokens": 1, "duration_s": 0.1})

    def boom(*a, **k):
        raise RuntimeError("boom in completion path")
    monkeypatch.setattr(ai_logic, "parse_cards_json", boom)

    warnings = []
    monkeypatch.setattr(ai_dialog, "_warn", lambda text, **kw: warnings.append(text))

    dlg = ai_dialog._GenerateDialog()
    dlg.show()
    dlg.source_box.setPlainText("Regional block landmarks and needle depths")
    dlg._start_generation()
    app.processEvents()
    dlg._worker.join(timeout=15)
    assert not dlg._worker.is_alive()

    dlg._timer.timeout.emit()   # a real Qt signal, not _finish_generation() called directly
    app.processEvents()

    assert warnings, "the failure must reach the add-on's own dialog, not vanish"
    assert "boom in completion path" in warnings[0]
    assert dlg._gen_done is True
    assert dlg.stack.currentWidget() is dlg.input_page   # landed somewhere usable
    assert dlg.isVisible()   # still open: an unhandled exception would have torn it down

    # Cancel is still wired and callable, not a permanent no-op stuck against a
    # progress page that no longer shows.
    dlg._cancel_generation()


def test_mode_radios_render_the_backends_own_text(monkeypatch):
    """C1, confirmed by rendering: the mode hints must show the found backend's
    own truthful text, not one label shared by all three. The radios
    themselves carry only the short, stable name (item 8's wrap fix); the
    per-backend sentence is what varies by backend and lives in the hint
    underneath, see _refresh_backend_row."""
    harness.bootstrap()
    monkeypatch.setattr(ai_cli, "find_cli",
                        lambda kind, override="": "/bin/echo"
                        if kind == "claude" else None)
    monkeypatch.setattr(ai_cli, "probe",
                        lambda kind, path: {"ok": True, "detail": "v1"})
    dlg = ai_dialog._GenerateDialog()
    modes = ai_cli.BACKENDS["claude"]["modes"]
    assert dlg.thorough_radio.text() == "Thorough"
    assert dlg.quick_radio.text() == "Quick draft"
    assert dlg.thorough_hint.text() == modes["thorough"]
    assert dlg.quick_hint.text() == modes["quick"]


def test_backend_row_shows_editable_model_and_effort_for_claude(monkeypatch):
    """Model/Effort honesty pattern (item A): claude has a verified --model and
    --effort flag, so it gets live, editable controls, pre-filled with the
    add-on's own default (sonnet/medium), not left blank."""
    harness.bootstrap()
    monkeypatch.setattr(ai_cli, "find_cli",
                        lambda kind, override="": "/bin/echo"
                        if kind == "claude" else None)
    monkeypatch.setattr(ai_cli, "probe",
                        lambda kind, path: {"ok": True, "detail": "v1"})
    dlg = ai_dialog._GenerateDialog()
    dlg.show()
    harness.app().processEvents()
    assert dlg.model_combo.isVisible() is True
    assert dlg.model_readonly.isVisible() is False
    assert dlg.model_combo.isEditable() is True
    assert dlg.model_combo.currentText() == "sonnet"
    assert dlg.effort_combo.isVisible() is True
    assert dlg.effort_combo.currentData() == ""   # "Default (medium)", not an override


def test_backend_row_shows_read_only_model_for_agy_and_hides_effort(monkeypatch):
    """agy has no verified way to honor a model or effort choice in headless
    mode, so it must not offer a control that lies about being respected."""
    harness.bootstrap()
    monkeypatch.setattr(ai_cli, "find_cli",
                        lambda kind, override="": "/bin/echo"
                        if kind == "agy" else None)
    monkeypatch.setattr(ai_cli, "probe",
                        lambda kind, path: {"ok": True, "detail": "v1"})
    dlg = ai_dialog._GenerateDialog()
    dlg.show()
    harness.app().processEvents()
    assert dlg.session.backend == "agy"
    assert dlg.model_combo.isVisible() is False
    assert dlg.model_readonly.isVisible() is True
    assert "Flash" in dlg.model_readonly.text()
    assert dlg.effort_combo.isVisible() is False


def test_changing_model_and_effort_persists_to_config(monkeypatch):
    from aqt import mw
    harness.bootstrap()
    monkeypatch.setattr(ai_cli, "find_cli",
                        lambda kind, override="": "/bin/echo"
                        if kind == "claude" else None)
    monkeypatch.setattr(ai_cli, "probe",
                        lambda kind, path: {"ok": True, "detail": "v1"})
    dlg = ai_dialog._GenerateDialog()
    dlg.model_combo.setEditText("opus")
    idx = dlg.effort_combo.findData("high")
    dlg.effort_combo.setCurrentIndex(idx)
    conf = mw.addonManager.getConfig("internpearls")
    assert conf["ai_model"]["claude"] == "opus"
    assert conf["ai_effort"]["claude"] == "high"
    assert dlg.session.ai_model["claude"] == "opus"
    assert dlg.session.ai_effort["claude"] == "high"


def test_model_set_under_claude_does_not_leak_into_codex(monkeypatch):
    """Item 1: ai_model/ai_effort are stored per backend kind. Setting a model
    while claude is active must not pre-fill or get sent for codex when it's
    the backend detected on a later open: the leak this test reproduces
    would otherwise silently send `-m opus` (or now `--model opus`) to a
    backend the user never chose that model for."""
    from aqt import mw
    harness.bootstrap()

    # First open: only claude detected, set its model to "opus".
    monkeypatch.setattr(ai_cli, "find_cli",
                        lambda kind, override="": "/bin/echo"
                        if kind == "claude" else None)
    monkeypatch.setattr(ai_cli, "probe",
                        lambda kind, path: {"ok": True, "detail": "v1"})
    dlg1 = ai_dialog._GenerateDialog()
    dlg1.model_combo.setEditText("opus")
    conf = mw.addonManager.getConfig("internpearls")
    assert conf["ai_model"]["claude"] == "opus"
    assert conf["ai_model"].get("codex", "") == ""

    # Second open: only codex detected this time: the stale claude-scoped
    # value must not pre-fill its Model field.
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
    assert dlg2.model_combo.currentText() == ""   # not pre-filled with "opus"

    dlg2.source_box.setPlainText("Some source material")
    dlg2._start_generation()
    dlg2._wait_for_worker(timeout=15)
    assert captured["model"] == ""   # argv-bound model is clean too


def test_input_page_gates_note_types_against_real_checkboxes(monkeypatch):
    """A, against real QCheckBox widgets rather than the fake-Qt suite's own
    checkbox stand-in: a collection carrying neither managed type ("Study Deck -
    Basic"/"Study Deck - Cloze" only ever arrive with a deck sync) must render
    both of them genuinely unselectable, with a reason: not merely "checked()
    would read False if you asked it right", but isEnabled() actually False and
    isChecked() actually False on the real widget a user would click. Basic and
    Cloze are Anki's own stock types and always exist, so they render enabled and
    checked, exactly as tests/test_ai_flows.py's mock-collection version already
    covers structurally; this is the same claim proven against a real Qt
    QCheckBox's own isEnabled()/isChecked(), the layer a hand-driven pass
    actually looks at.
    """
    harness.bootstrap()
    import mock_anki
    from aqt import mw

    mw.col.models = mock_anki._Models([
        mock_anki.make_model("Basic", fields=["Front", "Back"]),
        mock_anki.make_model("Cloze", fields=["Text", "Back Extra"])])

    monkeypatch.setattr(ai_cli, "find_cli",
                        lambda kind, override="": "/bin/echo"
                        if kind == "claude" else None)
    monkeypatch.setattr(ai_cli, "probe",
                        lambda kind, path: {"ok": True, "detail": "v1"})

    dlg = ai_dialog._GenerateDialog()
    dlg.show()

    for missing in ("Study Deck - Basic", "Study Deck - Cloze"):
        box = dlg.type_boxes[missing]
        assert box.isEnabled() is False, f"{missing} isn't in the collection yet"
        assert box.isChecked() is False
        assert "sync your decks first" in box.text()

    for present in ("Basic", "Cloze"):
        box = dlg.type_boxes[present]
        assert box.isEnabled() is True
        assert box.isChecked() is True


def test_attach_warns_once_when_a_pdfs_images_cant_be_decoded(monkeypatch, tmp_path):
    """Anki's own bundled Python lacks Pillow, so a real user's PDF images
    silently extract nothing; ai_logic.extract_attachment now flags that as
    images_undecoded rather than looking identical to "no images". The
    wizard must tell the user, once per session, not stay quiet about it."""
    harness.bootstrap()
    from aqt.qt import QFileDialog
    from internpearls import ai_logic

    monkeypatch.setattr(ai_cli, "find_cli",
                        lambda kind, override="": "/bin/echo"
                        if kind == "claude" else None)
    monkeypatch.setattr(ai_cli, "probe",
                        lambda kind, path: {"ok": True, "detail": "v1"})

    pdf_a = str(tmp_path / "a.pdf")
    pdf_b = str(tmp_path / "b.pdf")
    open(pdf_a, "wb").close()
    open(pdf_b, "wb").close()
    calls = iter([([pdf_a], ""), ([pdf_b], "")])
    monkeypatch.setattr(QFileDialog, "getOpenFileNames",
                        lambda *a, **k: next(calls))
    monkeypatch.setattr(
        ai_logic, "extract_attachment",
        lambda p, dest: {"text": "some text", "images": [], "images_undecoded": True})
    warnings = []
    monkeypatch.setattr(ai_dialog, "_warn", lambda text, **kw: warnings.append(text))

    dlg = ai_dialog._GenerateDialog()
    dlg._attach()
    assert len(warnings) == 1
    assert "images" in warnings[0] and "Anki" in warnings[0]

    dlg._attach()   # a second attached PDF hitting the same limitation stays quiet
    assert len(warnings) == 1


def test_view_skills_extra_button_toggles_and_leaves_dialog_open(monkeypatch):
    """I5, against a real QDialogButtonBox: clicking the extra (ActionRole)
    button runs on_extra without the dialog answering accept/reject, and
    dismissing (simulated here as reject(), the same outcome Escape or the
    close box produce) never touches on_extra at all.
    """
    _, q = harness.bootstrap()
    a = harness.app()
    from internpearls.ui import _ask_scrollable

    calls = []

    def on_extra(dlg):
        calls.append(1)
        return "updated body"

    original = q.QDialog.exec

    def fake_exec(self):
        self.show()
        a.processEvents()
        btn = next(b for b in self.findChildren(q.QPushButton)
                  if b.text() == "Toggle")
        btn.click()
        a.processEvents()
        # A real ActionRole button never calls accept()/reject() on its own:
        # only the click handler ran, the dialog is still up and undecided.
        assert self.isVisible()
        self.reject()
        return self.result()

    q.QDialog.exec = fake_exec
    try:
        _ask_scrollable("body text", yes_label="Close", no_label=None,
                        extra_label="Toggle", on_extra=on_extra)
    finally:
        q.QDialog.exec = original
    assert calls == [1]   # the explicit click, and only the explicit click, ran it


def test_view_skills_toggle_button_relabels_after_each_click(monkeypatch):
    """Minor fix: the extra button used to keep reading "Disable deck skill"
    even after a click actually disabled it. It must now name the action
    that's still available, not the one already taken."""
    _, q = harness.bootstrap()
    app = harness.app()
    from internpearls import config

    config.save_deck_skill({"text": "do X", "version": "1",
                            "consented_on": "2026-01-01", "enabled": True})
    dlg = ai_dialog._GenerateDialog.__new__(ai_dialog._GenerateDialog)

    original = q.QDialog.exec
    seen = []

    def fake_exec(self):
        self.show()
        app.processEvents()
        btn = next(b for b in self.findChildren(q.QPushButton)
                  if "deck skill" in b.text().lower())
        seen.append(btn.text())
        btn.click()
        app.processEvents()
        seen.append(btn.text())
        btn.click()
        app.processEvents()
        seen.append(btn.text())
        self.reject()
        return self.result()

    q.QDialog.exec = fake_exec
    try:
        dlg._view_skills()
    finally:
        q.QDialog.exec = original
    assert seen == ["Disable deck skill", "Enable deck skill", "Disable deck skill"]
