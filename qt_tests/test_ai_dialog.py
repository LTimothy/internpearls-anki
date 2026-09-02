"""Real-PyQt6 render check for the "Generate cards with AI" wizard.

Drives the dialog through a real (mocked-CLI) generation so the review page
renders its actual card rows, not just an empty stack page -- that's the one
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
              cancel=None, timeout=None: {"text": json.dumps(cards), "tokens": 15,
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
    assert dlg.cards_lay.count() == len(dlg.session.cards)   # review rows built

    for page in (dlg.setup_page, dlg.input_page, dlg.progress_page,
                dlg.review_page):
        dlg.stack.setCurrentWidget(page)
        app.processEvents()
        dlg.stack.currentWidget().repaint()

    assert dlg.windowTitle() == "Intern Pearls: Generate cards with AI"


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
              cancel=None, timeout=None: {"text": json.dumps(cards), "tokens": 15,
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
    label = row.layout().itemAt(1).widget()
    label.repaint()
    text = label.text()
    assert "<img" in text
    assert "example.com" in text


def test_mode_radios_render_the_backends_own_text(monkeypatch):
    """C1, confirmed by rendering: the mode radios must show the found
    backend's own truthful text, not one label shared by all three."""
    harness.bootstrap()
    monkeypatch.setattr(ai_cli, "find_cli",
                        lambda kind, override="": "/bin/echo"
                        if kind == "claude" else None)
    monkeypatch.setattr(ai_cli, "probe",
                        lambda kind, path: {"ok": True, "detail": "v1"})
    dlg = ai_dialog._GenerateDialog()
    modes = ai_cli.BACKENDS["claude"]["modes"]
    assert dlg.thorough_radio.text() == modes["thorough"]
    assert dlg.quick_radio.text() == modes["quick"]


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
        # A real ActionRole button never calls accept()/reject() on its own --
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
