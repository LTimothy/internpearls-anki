"""Quitting Anki while an AI dialog is open deletes the dialog's C++ object before the
add-on's own cleanup runs. That cleanup, and the error wrapper around it, must neither
raise nor try to show a dialog on a main window that is going away."""
import harness


def _delete(widget):
    from PyQt6 import sip
    sip.delete(widget)


def test_retiring_an_already_deleted_wizard_does_not_raise():
    harness.bootstrap()
    harness.app()
    harness._ai_backend_available("claude")
    from internpearls import ai_dialog
    dlg = harness.settled(ai_dialog._GenerateDialog())
    _delete(dlg)
    dlg._retire_for_delete()


def test_a_wizard_deleted_by_quit_ends_generate_cards_quietly(monkeypatch, capsys):
    harness.bootstrap()
    harness.app()
    harness._ai_backend_available("claude")
    from internpearls import ai_dialog, ui
    warned = []
    monkeypatch.setattr(ui, "_warn", lambda *a, **k: warned.append(a))
    monkeypatch.setattr(ai_dialog._GenerateDialog, "exec", _delete)
    ai_dialog.generate_cards()
    assert warned == []
    assert "Traceback" not in capsys.readouterr().out


def test_a_backends_dialog_deleted_by_quit_ends_quietly(monkeypatch, capsys):
    harness.bootstrap()
    harness.app()
    harness._ai_backend_available("claude")
    from internpearls import ai_setup, ui
    warned = []
    monkeypatch.setattr(ui, "_warn", lambda *a, **k: warned.append(a))
    monkeypatch.setattr(ai_setup._AIBackendsDialog, "exec", _delete)
    ai_setup.open_ai_backends()
    assert warned == []
    assert "Traceback" not in capsys.readouterr().out


def test_the_error_wrapper_shows_nothing_when_the_main_window_is_gone(
        monkeypatch, capsys):
    harness.bootstrap()
    from internpearls import ui
    warned = []
    monkeypatch.setattr(ui, "_warn", lambda *a, **k: warned.append(a))

    class Gone:
        def isVisible(self):
            raise RuntimeError("wrapped C/C++ object of type MainWindow has been deleted")

    @ui._safe
    def action():
        raise ValueError("boom")

    monkeypatch.setattr(ui, "mw", Gone())
    assert action() is None
    assert warned == []
    err = capsys.readouterr()
    assert "boom" in err.err


def test_the_error_wrapper_still_warns_while_the_window_is_alive(monkeypatch):
    harness.bootstrap()
    from internpearls import ui
    warned = []
    monkeypatch.setattr(ui, "_warn", lambda *a, **k: warned.append(a))

    @ui._safe
    def action():
        raise ValueError("boom")

    action()
    assert len(warned) == 1 and "boom" in warned[0][0]


def test_a_duplicate_scan_deleted_by_quit_ends_quietly(monkeypatch, capsys):
    import mock_anki
    mock, _ = harness.bootstrap()
    harness.app()
    harness._ai_backend_available("claude")
    mock.mw.col = mock_anki.MockCollection()
    mock.mw._config = {}
    from internpearls import dupes_dialog, ui
    warned = []
    monkeypatch.setattr(ui, "_warn", lambda *a, **k: warned.append(a))
    monkeypatch.setattr(dupes_dialog._DuplicateScanDialog, "exec", _delete)
    dupes_dialog.open_duplicate_scan()
    assert warned == []
    assert "Traceback" not in capsys.readouterr().out


def test_a_timer_handle_whose_qtimer_was_deleted_stops_and_starts_quietly():
    from internpearls.platform import NativePlatform
    _mock, q = harness.bootstrap()
    native = NativePlatform()
    owner = q.QObject()
    handle = native.create_timer(native.owner_id(owner), lambda: None, 50)
    handle.start()
    assert handle.is_active()
    _delete(handle._timer)
    handle.stop()
    assert not handle.is_active()
    handle.start()
    handle.stop()
    assert not handle.is_active()


def test_a_wizard_on_the_review_page_deleted_by_quit_ends_quietly(monkeypatch, capsys):
    harness.bootstrap()
    harness.app()
    harness._ai_backend_available("claude")
    from internpearls import ai_dialog, ui
    warned = []
    monkeypatch.setattr(ui, "_warn", lambda *a, **k: warned.append(a))

    def exec_then_quit(dlg):
        s = dlg.session
        s.cards = [{"note_type": "Study Deck - Basic",
                    "fields": {"Front": "Which receptor does fenoldopam stimulate?",
                               "Back": "D1", "Why": "", "Dosing": "", "Notes": ""},
                    "tags": [], "images": [], "rationale": ""}]
        s.image_data = {}
        dlg._pending_prev_included = None
        dlg.show()
        dlg._apply_review_state()
        harness.app().processEvents()
        from internpearls.platform import platform, platform_owner_id
        dlg._timer = platform().create_timer(platform_owner_id(dlg), lambda: None, 100)
        dlg._timer.start()
        _delete(dlg)

    monkeypatch.setattr(ai_dialog._GenerateDialog, "exec", exec_then_quit)
    ai_dialog.generate_cards()
    out = capsys.readouterr()
    assert warned == []
    assert "Traceback" not in out.out
    assert "closing" not in out.err


def test_retiring_a_wizard_survives_every_timer_being_deleted():
    harness.bootstrap()
    harness.app()
    harness._ai_backend_available("claude")
    from internpearls import ai_dialog
    from internpearls.platform import platform, platform_owner_id
    dlg = harness.settled(ai_dialog._GenerateDialog())
    for name in ("_attach_timer", "_timer", "_img_timer"):
        timer = platform().create_timer(platform_owner_id(dlg), lambda: None, 100)
        timer.start()
        setattr(dlg, name, timer)
    _delete(dlg)
    dlg._retire_for_delete()
