"""End-to-end wizard flows against the mock Anki and the fake CLI."""
from internpearls import ai_cli, ai_dialog


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
