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
