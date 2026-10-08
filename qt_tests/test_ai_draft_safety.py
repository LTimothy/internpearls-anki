"""Draft saves stop when the wizard closes or its Qt owner is deleted."""
import gc
import json
import threading
import time
import weakref

import harness
import pytest
from PyQt6 import sip
from PyQt6.QtTest import QTest

from internpearls import ai_cli, ai_dialog, ai_draft, ui
from internpearls.platform import NativePlatform


def _pending_draft(monkeypatch):
    harness.app()
    monkeypatch.setattr(ai_cli, "find_cli", lambda *a, **kw: None)
    dlg = harness.settled(ai_dialog._GenerateDialog())
    dlg.session.cards = [{"note_type": "Study Deck - Basic",
                          "fields": {"Front": "Question", "Back": "Answer"},
                          "tags": [], "images": [], "rationale": ""}]
    dlg._pending_prev_included = None
    dlg._apply_review_state()
    dlg.show()
    harness.app().processEvents()
    dlg.feedback_box.setPlainText("Keep this feedback")
    assert dlg._draft_pending and dlg._draft_timer.isActive()
    return dlg


@pytest.mark.parametrize("finish", ["close", "reject", "accept", "done", "attachment"])
def test_closing_with_a_pending_draft_retires_the_debounce(monkeypatch, finish):
    dlg = _pending_draft(monkeypatch)
    monkeypatch.setattr(ai_dialog, "_ask", lambda *a, **kw: False)
    if finish == "attachment":
        monkeypatch.setattr(dlg, "_attachment_in_progress", lambda: True)
        monkeypatch.setattr(dlg, "_cancel_running_attachment", lambda: None)
        dlg.reject()
    elif finish == "done":
        dlg.done(0)
    else:
        getattr(dlg, finish)()
    try:
        assert not dlg._draft_timer.isActive()
        dlg.feedback_box.setPlainText("Late change")
        assert not dlg._draft_timer.isActive()
        gc.collect()
        QTest.qWait(450)
        harness.app().processEvents()
        with open(dlg._draft_path, encoding="utf8") as fh:
            assert json.load(fh)["feedback"] == "Keep this feedback"
    finally:
        dlg._retire_for_delete()
        sip.delete(dlg)


def test_deleting_with_a_pending_draft_drops_late_callbacks(monkeypatch, capsys):
    dlg = _pending_draft(monkeypatch)
    timer = dlg._draft_timer
    warned = []
    monkeypatch.setattr(ai_dialog, "_warn", lambda *a, **kw: warned.append(a))
    sip.delete(dlg)
    dlg._schedule_draft()
    dlg._retire_for_delete()
    gc.collect()
    QTest.qWait(450)
    harness.app().processEvents()
    assert not timer.isActive()
    assert warned == []
    assert "Traceback" not in capsys.readouterr().out


def test_destroyed_timer_releases_its_callback():
    from aqt.qt import QWidget

    class Callback:
        def __call__(self):
            pytest.fail("a deleted owner's callback was delivered")

    harness.app()
    native = NativePlatform()
    owner = QWidget()
    callback = Callback()
    callback_ref = weakref.ref(callback)
    timer = native.create_timer(native.owner_id(owner), callback, 10, single_shot=True)
    timer.start()
    del callback
    sip.delete(owner)
    gc.collect()
    QTest.qWait(30)
    assert callback_ref() is None
    assert not timer.is_active()


def test_closing_during_new_draft_images_keeps_a_readable_draft(monkeypatch):
    import copy
    dlg = _pending_draft(monkeypatch)
    original = dlg.session.cards[0]
    dlg.session.cards = [copy.deepcopy(original) for _ in range(3)]
    dlg._pending_prev_included = None
    dlg._apply_review_state()
    dlg.decision_cells[2].buttons["skip"].click()
    dlg.session.note_types = ["Study Deck - Basic"]
    card = copy.deepcopy(original)
    card["fields"]["Front"] = "New question"
    card["images"] = [{"source": "url:https://example.com/image.png", "attribution": ""}]
    dlg._worker_error = None
    dlg._worker_result = {"text": json.dumps([card]), "tokens": 1, "duration_s": 1}
    started, release = threading.Event(), threading.Event()
    def resolve(*args):
        started.set()
        release.wait(5)
        return {"state": "error", "kind": "url", "error": "Unavailable"}
    monkeypatch.setattr(ai_dialog, "_resolve_with_thumb", resolve)
    monkeypatch.setattr(ai_dialog, "_ask", lambda *a, **kw: True)
    try:
        dlg._finish_generation()
        deadline = time.monotonic() + 2
        while not started.is_set() and time.monotonic() < deadline:
            QTest.qWait(10)
        assert started.is_set()
        assert dlg._generation_in_progress()
        dlg.reject()
        saved, damaged = ai_draft.load(dlg._draft_path, ai_dialog.FIELD_MAP)
        assert not damaged and saved is not None
        assert saved["cards"][0]["fields"]["Front"] == "New question"
        assert saved["decided"] == set()
        assert not dlg._draft_timer.isActive()
        release.set()
        dlg._img_worker.join(timeout=2)
        QTest.qWait(450)
        assert not dlg._draft_timer.isActive()
    finally:
        release.set()
        dlg._retire_for_delete()
        sip.delete(dlg)


def test_successful_import_with_a_leftover_draft_retires_callbacks(monkeypatch):
    dlg = _pending_draft(monkeypatch)
    finished, warnings = [], []
    dlg.finished.connect(finished.append)
    monkeypatch.setattr(ai_dialog, "_warn", lambda text, **kw: warnings.append((text, kw)))
    def fail_delete(path):
        raise PermissionError("Blocked <file>")
    monkeypatch.setattr(ai_draft, "discard", fail_delete)
    try:
        assert dlg._do_import() == 1
        assert finished == [1]
        assert not dlg.isVisible()
        assert dlg.session.imported == {0}
        assert warnings[0][1] == {"textFormat": "plain"}
        assert not dlg._draft_timer.isActive()
        with open(dlg._draft_path, "rb") as fh:
            saved_bytes = fh.read()
        dlg.feedback_box.setPlainText("Late feedback")
        assert not dlg._draft_timer.isActive()
    finally:
        dlg._retire_for_delete()
        sip.delete(dlg)
    QTest.qWait(450)
    with open(dlg._draft_path, "rb") as fh:
        assert fh.read() == saved_bytes
    monkeypatch.setattr(ai_dialog, "_ask", lambda *a, **kw: False)
    resumed = harness.settled(ai_dialog._GenerateDialog())
    try:
        assert resumed.session.imported == {0}
        assert resumed.decision_cells[0] is None
    finally:
        resumed._retire_for_delete()
        sip.delete(resumed)


@pytest.mark.parametrize("finish", ["close", "reject", "accept", "done",
                                    "input", "generation", "attachment"])
@pytest.mark.parametrize("close_anyway", [False, True])
def test_failed_draft_save_allows_a_safe_close_choice(monkeypatch, finish,
                                                    close_anyway):
    from aqt.qt import QMessageBox, QTimer

    dlg = _pending_draft(monkeypatch)
    assert dlg._flush_draft()
    with open(dlg._draft_path, "rb") as fh:
        saved = fh.read()
    dlg.feedback_box.setPlainText("Unsaved feedback")
    warnings, questions, finished, cancelled = [], [], [], []
    dlg.finished.connect(finished.append)
    monkeypatch.setattr(ai_dialog, "_warn", lambda *a, **kw: warnings.append(a))

    def fail_save(*a, **kw):
        raise OSError("Disk <full> & unwritable")

    original_save = ai_draft.save
    monkeypatch.setattr(ai_draft, "save", fail_save)
    original_exec = QMessageBox.exec

    def answer(box):
        questions.append(box.text())
        buttons = {button.text(): button for button in box.buttons()}
        assert set(buttons) == {"Close without saving", "Keep window open"}
        safe = buttons["Keep window open"]
        assert box.defaultButton() is safe
        assert box.escapeButton() is safe
        chosen = buttons["Close without saving"] if close_anyway else safe
        QTimer.singleShot(0, chosen.click)
        return original_exec(box)

    monkeypatch.setattr(QMessageBox, "exec", answer)

    def ask(text, **kw):
        if "couldn't be saved" in text:
            return ui._ask(text, **kw)
        return kw.get("yes_label") == "Cancel and close"

    monkeypatch.setattr(ai_dialog, "_ask", ask)
    if finish == "input":
        dlg.stack.setCurrentWidget(dlg.input_page)
    elif finish == "generation":
        dlg.stack.setCurrentWidget(dlg.progress_page)
        monkeypatch.setattr(dlg, "_generation_in_progress", lambda: True)
        monkeypatch.setattr(dlg, "_cancel_running_generation",
                            lambda: cancelled.append("generation"))
    elif finish == "attachment":
        monkeypatch.setattr(dlg, "_attachment_in_progress", lambda: True)
        monkeypatch.setattr(dlg, "_cancel_running_attachment",
                            lambda: cancelled.append("attachment"))
    try:
        if finish == "done":
            dlg.done(7)
        elif finish in ("input", "generation", "attachment"):
            dlg.reject()
        else:
            getattr(dlg, finish)()
        assert questions == ["Your draft couldn't be saved "
                             "(Disk &lt;full&gt; &amp; unwritable). Close anyway? "
                             "Changes since the last successful save will be lost."]
        assert warnings == []
        assert dlg.isVisible() is not close_anyway
        expected = 7 if finish == "done" else 1 if finish == "accept" else 0
        assert finished == ([expected] if close_anyway else [])
        assert cancelled == ([finish] if close_anyway and finish in (
            "generation", "attachment") else [])
        with open(dlg._draft_path, "rb") as fh:
            assert fh.read() == saved
        if close_anyway:
            dlg.feedback_box.setPlainText("Late change")
            assert not dlg._draft_timer.isActive()
            QTest.qWait(450)
            with open(dlg._draft_path, "rb") as fh:
                assert fh.read() == saved
        else:
            monkeypatch.setattr(ai_draft, "save", original_save)
            assert dlg._flush_draft()
            with open(dlg._draft_path, encoding="utf8") as fh:
                assert json.load(fh)["feedback"] == "Unsaved feedback"
    finally:
        dlg._retire_for_delete()
        sip.delete(dlg)
