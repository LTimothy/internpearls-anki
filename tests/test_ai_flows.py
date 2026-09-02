"""End-to-end wizard flows against the mock Anki and the fake CLI."""
import os
import sys

from internpearls import ai_cli, ai_dialog

FAKE = os.path.join(os.path.dirname(__file__), "fake_cli.py")


def test_setup_page_shown_when_no_backend(anki, monkeypatch):
    monkeypatch.setattr(ai_cli, "find_cli", lambda kind, override="": None)
    dlg = ai_dialog._GenerateDialog()
    assert dlg.stack.currentWidget() is dlg.setup_page
    # every backend's row names it and states its own safety plainly
    for kind, meta in ai_cli.BACKENDS.items():
        text = dlg.setup_rows[kind].text()
        assert meta["label"] in text
        assert meta["safety"] in text


def test_input_page_shown_when_backend_found(anki, monkeypatch):
    monkeypatch.setattr(
        ai_cli, "find_cli",
        lambda kind, override="": "/usr/bin/x" if kind == "claude" else None)
    monkeypatch.setattr(
        ai_cli, "probe", lambda kind, path: {"ok": True, "detail": "v1"})
    dlg = ai_dialog._GenerateDialog()
    assert dlg.stack.currentWidget() is dlg.input_page
    assert not dlg.generate_btn.isEnabled()   # empty source
    dlg.source_box.setPlainText("some source")
    assert dlg.generate_btn.isEnabled()


def _ready_dialog(anki, monkeypatch, cli_mode="ok"):
    monkeypatch.setattr(
        ai_cli, "find_cli",
        lambda kind, override="": sys.executable if kind == "claude" else None)
    monkeypatch.setattr(
        ai_cli, "probe", lambda kind, path: {"ok": True, "detail": "v1"})
    monkeypatch.setattr(
        ai_cli, "build_argv",
        lambda kind, path, mode, scratch, imgs:
            ([sys.executable, FAKE, cli_mode], True))
    dlg = ai_dialog._GenerateDialog()
    dlg.source_box.setPlainText("LAST toxicity source text")
    return dlg


def test_generation_reaches_review_with_cards(anki, monkeypatch):
    dlg = _ready_dialog(anki, monkeypatch)
    dlg._start_generation()
    dlg._wait_for_worker(timeout=15)   # test helper: joins thread + drains timer
    assert dlg.stack.currentWidget() is dlg.review_page
    assert len(dlg.session.cards) == 1
    assert dlg.session.included == [True]


def test_generation_failure_returns_to_input(anki, monkeypatch):
    dlg = _ready_dialog(anki, monkeypatch, cli_mode="fail")
    dlg._start_generation()
    dlg._wait_for_worker(timeout=15)
    assert dlg.stack.currentWidget() is dlg.input_page
    assert dlg.source_box.toPlainText()   # inputs intact


def test_cancel_generation_preserves_inputs(anki, monkeypatch):
    dlg = _ready_dialog(anki, monkeypatch, cli_mode="slow")
    dlg._start_generation()
    dlg._cancel_generation()
    dlg._wait_for_worker(timeout=15)
    assert dlg.stack.currentWidget() is dlg.input_page
    assert dlg.source_box.toPlainText() == "LAST toxicity source text"
    assert dlg.instructions_box.toPlainText() == ""
    assert not dlg.session.cards   # nothing touched the collection
