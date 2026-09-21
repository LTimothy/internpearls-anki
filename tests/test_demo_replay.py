import hashlib
import json
import os
import subprocess
import sys

import mock_anki
import pytest

from demo.replay import ReplayError, ReplayPlatform, WorkSuspended
from internpearls.platform import WorkRequest, new_work_request, use_platform


def _request(epoch=4, owner_id=1, operation=1, attempt=1,
             kind="background-fetch", action="background.fetch"):
    return WorkRequest(kind, owner_id, operation, attempt, {"action": action},
                       epoch=epoch)


def _protocol_request(response, sequence, actions=(), **changes):
    request = {
        "protocol": 1,
        "epoch": response["epoch"],
        "sequence": sequence,
        "render_revision": response["render_revision"],
        "actions": list(actions),
    }
    request.update(changes)
    return request


def _state_digest(anki, media_root):
    notes = []
    for nid, note in sorted(anki.col._notes.items()):
        notes.append((nid, note.guid, tuple(note.fields), tuple(note.tags), note.deck))
    media = []
    for root, _, files in os.walk(media_root):
        for name in sorted(files):
            path = os.path.join(root, name)
            media.append((os.path.relpath(path, media_root),
                          hashlib.sha256(open(path, "rb").read()).hexdigest()))
    media.extend(
        ("collection:" + name, hashlib.sha256(data).hexdigest())
        for name, data in sorted(anki.col.media._files.items()))
    return hashlib.sha256(repr((notes, media)).encode()).hexdigest()


def _ready_generate_dialog(monkeypatch):
    from internpearls import ai_cli, ai_dialog

    monkeypatch.setattr(
        ai_cli, "find_cli",
        lambda kind, override="": "/usr/bin/x" if kind == "claude" else None)
    monkeypatch.setattr(
        ai_cli, "probe", lambda kind, path: {"ok": True, "detail": "v1"})
    return ai_dialog._GenerateDialog()


def test_protocol_duplicate_sequence_is_idempotent_and_order_is_strict(anki):
    runner = mock_anki.Runner(anki)

    def flow():
        anki.gui.next_interaction({"kind": "first"})
        anki.gui.next_interaction({"kind": "second"})

    first = runner.start_protocol(flow, epoch=4)
    second = runner.feed_protocol(_protocol_request(first, 1))
    duplicate = runner.feed_protocol(_protocol_request(first, 1))
    out_of_order = runner.feed_protocol(_protocol_request(second, 3))
    stale_revision = runner.feed_protocol(_protocol_request(
        second, 2, render_revision=second["render_revision"] - 1))

    assert first["status"] == "need"
    assert second["status"] == "need"
    assert duplicate == second
    assert out_of_order["status"] == "stale"
    assert out_of_order["payload"] == {"code": "stale-sequence"}
    assert stale_revision["status"] == "stale"
    assert stale_revision["payload"] == {"code": "stale-render-revision"}


def test_protocol_rejects_wrong_epoch_and_unvalidated_journal_records(anki):
    runner = mock_anki.Runner(anki)
    first = runner.start_protocol(
        lambda: anki.gui.next_interaction({"kind": "only"}), epoch=7)

    wrong_epoch = runner.feed_protocol(_protocol_request(first, 1, epoch=8))
    invalid = runner.feed_protocol(_protocol_request(first, 1, actions=[
        {"type": "advance", "elapsed_ms": 1, "checkpoint_credits": 1,
         "extra": True},
    ]))

    assert wrong_epoch["payload"] == {"code": "stale-epoch"}
    assert invalid["status"] == "contract-error"
    assert invalid["payload"] == {"code": "invalid-action"}
    assert runner.journal == ()


def test_protocol_rejects_unknown_widget_without_poisoning_next_request(anki):
    from aqt.qt import QDialog, QPushButton, QVBoxLayout

    runner = mock_anki.Runner(anki)

    def flow():
        dialog = QDialog()
        layout = QVBoxLayout(dialog)
        button = QPushButton("Continue")
        button.clicked.connect(dialog.accept)
        layout.addWidget(button)
        dialog.exec()
        anki.mw._config["completed"] = True

    first = runner.start_protocol(flow, epoch=8)
    invalid = runner.feed_protocol(_protocol_request(first, 1, [
        {"type": "activate", "id": "missing"},
    ]))
    button_id = next(
        node["id"] for node in first["payload"]["contract_tree"]["nodes"]
        if node["kind"] == "button")
    valid = runner.feed_protocol(_protocol_request(first, 1, [
        {"type": "activate", "id": button_id},
    ]))

    assert invalid["payload"] == {"code": "unknown-widget-id"}
    assert runner.journal == ()
    assert valid["status"] == "done"
    assert anki.mw._config["completed"] is True


def test_rejected_batch_rolls_back_to_last_acknowledged_frontier(
        anki, monkeypatch):
    from internpearls import ai_cli, ai_setup

    def detect(cfg):
        configured = cfg["ai_cli_path"]
        backends = {
            kind: {"path": configured[kind] or None,
                   "ok": bool(configured[kind]), "detail": "fixture",
                   "enabled": True}
            for kind in ai_cli.BACKENDS
        }
        return {"backends": backends, "chosen": "claude"}

    monkeypatch.setattr(ai_cli, "detect_backends", detect)
    runner = mock_anki.Runner(anki)

    def flow():
        dialog = ai_setup._AIBackendsDialog(anki.mw)
        dialog.exec()

    first = runner.start_protocol(flow, epoch=35)
    line_id = next(
        node["id"] for node in first["payload"]["contract_tree"]["nodes"]
        if node["kind"] == "line"
        and node["accessible_name"] == "Executable path")
    test_id = next(
        node["id"] for node in first["payload"]["contract_tree"]["nodes"]
        if node["kind"] == "button" and node["text"] == "Test connection")
    accepted = runner.feed_protocol(_protocol_request(first, 1, [
        {"type": "edit-text", "id": line_id, "value": "/tool",
         "selection_start": 5, "selection_end": 5, "composing": False},
        {"type": "finish-edit", "id": line_id},
    ]))
    rejected = runner.feed_protocol(_protocol_request(accepted, 2, [
        {"type": "edit-text", "id": line_id, "value": "",
         "selection_start": 0, "selection_end": 0, "composing": False},
        {"type": "finish-edit", "id": line_id},
        {"type": "activate", "id": test_id},
    ]))

    assert rejected["status"] == "contract-error"
    assert rejected["payload"] == {"code": "disabled-target"}
    assert anki.mw._config["ai_cli_path"]["claude"] == "/tool"
    assert mock_anki._widgets[line_id].text() == "/tool"
    assert mock_anki._widgets[test_id].isEnabled()

    done = runner.feed_protocol(_protocol_request(
        rejected, 2, [{"type": "close",
                       "id": accepted["payload"]["contract_tree"]["root_id"]}]))

    assert done["status"] == "done"
    assert anki.mw._config["ai_cli_path"]["claude"] == "/tool"


def test_protocol_rejects_invalid_nested_modifiers_with_fixed_error(anki):
    runner = mock_anki.Runner(anki)
    first = runner.start_protocol(
        lambda: anki.gui.next_interaction({"kind": "wait"}), epoch=9)

    response = runner.feed_protocol(_protocol_request(first, 1, [
        {"type": "key", "id": "w1", "key": "Escape", "modifiers": [{}]},
    ]))

    assert response["status"] == "contract-error"
    assert response["payload"] == {"code": "invalid-action"}
    assert runner.journal == ()


def test_protocol_error_envelope_keeps_integer_fields_for_string_sequence(anki):
    runner = mock_anki.Runner(anki)
    first = runner.start_protocol(
        lambda: anki.gui.next_interaction({"kind": "wait"}), epoch=10)

    response = runner.feed_protocol(_protocol_request(
        first, "not-an-integer"))

    assert response["payload"] == {"code": "invalid-envelope"}
    assert isinstance(response["epoch"], int)
    assert isinstance(response["sequence"], int)
    assert isinstance(response["render_revision"], int)


def test_completed_protocol_flow_is_terminal_and_does_not_run_again(anki):
    from aqt.qt import QDialog, QPushButton, QVBoxLayout

    runner = mock_anki.Runner(anki)

    def flow():
        dialog = QDialog()
        layout = QVBoxLayout(dialog)
        button = QPushButton("Apply")
        button.clicked.connect(dialog.accept)
        layout.addWidget(button)
        dialog.exec()
        anki.mw._config["mutations"] = anki.mw._config.get("mutations", 0) + 1

    first = runner.start_protocol(flow, epoch=11)
    button_id = next(
        node["id"] for node in first["payload"]["contract_tree"]["nodes"]
        if node["kind"] == "button")
    done = runner.feed_protocol(_protocol_request(first, 1, [
        {"type": "activate", "id": button_id},
    ]))
    later = runner.feed_protocol(_protocol_request(done, 2, []))

    assert done["status"] == "done"
    assert later["status"] == "stale"
    assert anki.mw._config["mutations"] == 1


def test_suspended_work_cannot_finish_or_erase_its_journal(anki):
    from internpearls.platform import platform

    runner = mock_anki.Runner(anki)

    def flow():
        handle = platform().start_work(
            _request(epoch=12),
            lambda context: context.checkpoint("background-fetch:start"),
            lambda _result: None,
            pytest.fail)
        handle.start()

    response = runner.start_protocol(flow, epoch=12)

    assert response["status"] != "done"
    assert response["pending"]


def test_typed_actions_answer_production_confirmation_and_prompt(anki):
    from internpearls import ui

    runner = mock_anki.Runner(anki)

    def flow():
        if not ui._ask("Apply the change?", yes_label="Apply", no_label="Keep"):
            return
        value = ui._prompt("Label", default="")
        if value is not None:
            anki.mw._config["answer"] = value

    confirmation = runner.start_protocol(flow, epoch=13)
    confirm_tree = confirmation["payload"]["contract_tree"]
    apply_id = next(
        node["id"] for node in confirm_tree["nodes"]
        if node["kind"] == "button" and node["text"] == "Apply")
    prompt = runner.feed_protocol(_protocol_request(confirmation, 1, [
        {"type": "activate", "id": apply_id},
    ]))
    prompt_tree = prompt["payload"]["contract_tree"]
    line_id = next(
        node["id"] for node in prompt_tree["nodes"]
        if node["kind"] == "line")
    ok_id = next(
        node["id"] for node in prompt_tree["nodes"]
        if node["kind"] == "button" and node["text"] == "OK")
    edited = runner.feed_protocol(_protocol_request(prompt, 2, [
        {"type": "edit-text", "id": line_id, "value": "typed",
         "selection_start": 5, "selection_end": 5, "composing": False},
    ]))
    edited_line = next(
        node for node in edited["payload"]["contract_tree"]["nodes"]
        if node["id"] == line_id)
    done = runner.feed_protocol(_protocol_request(edited, 3, [
        {"type": "activate", "id": ok_id},
    ]))

    assert edited_line["value"] == "typed"
    assert done["status"] == "done"
    assert anki.mw._config["answer"] == "typed"


def test_typed_close_declines_production_confirmation(anki):
    from internpearls import ui

    runner = mock_anki.Runner(anki)

    def flow():
        anki.mw._config["answer"] = ui._ask(
            "Apply the change?", yes_label="Apply", no_label="Keep")

    confirmation = runner.start_protocol(flow, epoch=36)
    done = runner.feed_protocol(_protocol_request(confirmation, 1, [
        {"type": "close",
         "id": confirmation["payload"]["contract_tree"]["root_id"]},
    ]))

    assert done["status"] == "done"
    assert anki.mw._config["answer"] is False


def test_ordered_batch_edits_source_before_generate_activation(
        anki, monkeypatch):
    from internpearls import ai_cli, ai_dialog

    monkeypatch.setattr(
        ai_cli, "find_cli",
        lambda kind, override="": "/usr/bin/x" if kind == "claude" else None)
    monkeypatch.setattr(
        ai_cli, "probe", lambda kind, path: {"ok": True, "detail": "v1"})
    monkeypatch.setattr(
        ai_cli, "run_generation",
        lambda *args, **kwargs: {"text": "[]", "tokens": 0, "duration_s": 0})

    runner = mock_anki.Runner(anki)
    first = runner.start_protocol(ai_dialog.generate_cards, epoch=37)
    nodes = first["payload"]["contract_tree"]["nodes"]
    source_id = next(
        node["id"] for node in nodes
        if node["kind"] == "textarea"
        and node["placeholder"].startswith("Paste lecture"))
    generate = next(
        node for node in nodes
        if node["kind"] == "button" and node["text"] == "Generate"
        and node["effective_visible"])
    assert not generate["effective_enabled"]

    working = runner.feed_protocol(_protocol_request(first, 1, [
        {"type": "edit-text", "id": source_id, "value": "study source",
         "selection_start": 12, "selection_end": 12, "composing": False},
        {"type": "activate", "id": generate["id"]},
    ]))

    assert working["status"] == "need"
    assert [item["kind"] for item in working["pending"]] == ["assistant"]


def test_disabled_generate_validation_bypasses_production_safe_wrapper(
        anki, monkeypatch):
    from internpearls import ai_cli, ai_dialog

    monkeypatch.setattr(
        ai_cli, "find_cli",
        lambda kind, override="": "/usr/bin/x" if kind == "claude" else None)
    monkeypatch.setattr(
        ai_cli, "probe", lambda kind, path: {"ok": True, "detail": "v1"})
    monkeypatch.setattr(
        ai_cli, "run_generation",
        lambda *args, **kwargs: {"text": "[]", "tokens": 0, "duration_s": 0})

    runner = mock_anki.Runner(anki)
    first = runner.start_protocol(ai_dialog.generate_cards, epoch=42)
    nodes = first["payload"]["contract_tree"]["nodes"]
    source_id = next(
        node["id"] for node in nodes
        if node["kind"] == "textarea"
        and node["placeholder"].startswith("Paste lecture"))
    generate = next(
        node for node in nodes
        if node["kind"] == "button" and node["text"] == "Generate"
        and node["effective_visible"])
    assert not generate["effective_enabled"]

    rejected = runner.feed_protocol(_protocol_request(first, 1, [
        {"type": "activate", "id": generate["id"]},
    ]))

    assert rejected["status"] == "contract-error"
    assert rejected["payload"] == {"code": "disabled-target"}
    assert anki.gui.warnings == []
    assert runner.journal == ()

    working = runner.feed_protocol(_protocol_request(rejected, 1, [
        {"type": "edit-text", "id": source_id, "value": "study source",
         "selection_start": 12, "selection_end": 12, "composing": False},
        {"type": "activate", "id": generate["id"]},
    ]))

    assert working["status"] == "need"
    assert [item["kind"] for item in working["pending"]] == ["assistant"]


def test_nested_file_frontier_rejects_unconsumed_action_suffix(
        anki, monkeypatch):
    from internpearls import ai_cli, ai_dialog

    monkeypatch.setattr(
        ai_cli, "find_cli",
        lambda kind, override="": "/usr/bin/x" if kind == "claude" else None)
    monkeypatch.setattr(
        ai_cli, "probe", lambda kind, path: {"ok": True, "detail": "v1"})
    monkeypatch.setattr(
        ai_cli, "run_generation",
        lambda *args, **kwargs: {"text": "[]", "tokens": 0, "duration_s": 0})

    runner = mock_anki.Runner(anki)
    first = runner.start_protocol(ai_dialog.generate_cards, epoch=43)
    nodes = first["payload"]["contract_tree"]["nodes"]
    attach_id = next(
        node["id"] for node in nodes
        if node["kind"] == "button" and node["text"] == "Attach images or PDFs")
    generate_id = next(
        node["id"] for node in nodes
        if node["kind"] == "button" and node["text"] == "Generate"
        and node["effective_visible"])
    source_id = next(
        node["id"] for node in nodes
        if node["kind"] == "textarea"
        and node["placeholder"].startswith("Paste lecture"))

    rejected = runner.feed_protocol(_protocol_request(first, 1, [
        {"type": "activate", "id": attach_id},
        {"type": "activate", "id": generate_id},
    ]))

    assert rejected["status"] == "contract-error"
    assert rejected["payload"] == {"code": "invalid-action"}
    assert runner.journal == ()

    picker = runner.feed_protocol(_protocol_request(rejected, 1, [
        {"type": "activate", "id": attach_id},
    ]))
    cancelled = runner.feed_protocol(_protocol_request(picker, 2, [
        {"type": "select-files", "id": picker["payload"]["id"],
         "accept": picker["payload"]["accept"], "files": []},
    ]))

    assert cancelled["status"] == "need"
    assert all(action.get("id") != generate_id for action in runner.journal)

    working = runner.feed_protocol(_protocol_request(cancelled, 3, [
        {"type": "edit-text", "id": source_id, "value": "study source",
         "selection_start": 12, "selection_end": 12, "composing": False},
        {"type": "activate", "id": generate_id},
    ]))

    assert working["status"] == "need"
    assert [item["kind"] for item in working["pending"]] == ["assistant"]


def test_production_generate_replay_keeps_work_and_cancel_leaves_progress(
        anki, monkeypatch):
    from internpearls import ai_cli, ai_dialog

    monkeypatch.setattr(
        ai_cli, "find_cli",
        lambda kind, override="": "/usr/bin/x" if kind == "claude" else None)
    monkeypatch.setattr(
        ai_cli, "probe", lambda kind, path: {"ok": True, "detail": "v1"})

    def generate(*args, **kwargs):
        kwargs["on_event"]({"type": "phase", "phase": "Working"})
        return {"text": "[]", "tokens": 0, "duration_s": 0}

    monkeypatch.setattr(ai_cli, "run_generation", generate)
    runner = mock_anki.Runner(anki)
    first = runner.start_protocol(ai_dialog.generate_cards, epoch=14)
    source_id = next(
        node["id"] for node in first["payload"]["contract_tree"]["nodes"]
        if node["kind"] == "textarea"
        and node["placeholder"].startswith("Paste lecture"))
    edited = runner.feed_protocol(_protocol_request(first, 1, [
        {"type": "edit-text", "id": source_id, "value": "study source",
         "selection_start": 12, "selection_end": 12, "composing": False},
    ]))
    generate_id = next(
        node["id"] for node in edited["payload"]["contract_tree"]["nodes"]
        if node["kind"] == "button" and node["text"] == "Generate"
        and node["effective_visible"])
    working = runner.feed_protocol(_protocol_request(edited, 2, [
        {"type": "activate", "id": generate_id},
    ]))

    assert working["status"] == "need"
    assert working["pending"]

    cancel_id = next(
        node["id"] for node in working["payload"]["contract_tree"]["nodes"]
        if node["kind"] == "button" and node["text"] == "Cancel"
        and node["effective_visible"])
    cancelling = runner.feed_protocol(_protocol_request(working, 3, [
        {"type": "activate", "id": cancel_id},
    ]))
    settled = runner.feed_protocol(_protocol_request(cancelling, 4, [
        {"type": "advance", "elapsed_ms": 200, "checkpoint_credits": 0},
    ]))

    assert settled["status"] == "need"
    assert settled["pending"] == []
    assert any(
        node["kind"] == "button" and node["text"] == "Generate"
        and node["effective_visible"]
        for node in settled["payload"]["contract_tree"]["nodes"])

    closed = runner.feed_protocol(_protocol_request(settled, 5, [
        {"type": "close",
         "id": settled["payload"]["contract_tree"]["root_id"]},
    ]))
    reopened = runner.start_protocol(ai_dialog.generate_cards, epoch=15)

    assert closed["status"] == "done", closed
    assert reopened["status"] == "need"
    assert any(
        node["kind"] == "button" and node["text"] == "Generate"
        and node["effective_visible"]
        for node in reopened["payload"]["contract_tree"]["nodes"])


def test_replay_active_close_uses_no_native_cleanup_threads(
        anki, monkeypatch, tmp_path):
    from aqt.qt import QFileDialog
    from internpearls import ai_cli, ai_dialog

    monkeypatch.setattr(
        ai_cli, "find_cli",
        lambda kind, override="": "/usr/bin/x" if kind == "claude" else None)
    monkeypatch.setattr(
        ai_cli, "probe", lambda kind, path: {"ok": True, "detail": "v1"})
    monkeypatch.setattr(
        ai_cli, "run_generation",
        lambda *args, **kwargs: {"text": "[]", "tokens": 0, "duration_s": 0})
    source = tmp_path / "source.png"
    source.write_bytes(b"image bytes")
    replay = ReplayPlatform(epoch=38, scratch_root=str(tmp_path / "replay"))
    started = []

    class ForbiddenThread:
        def __init__(self, *args, **kwargs):
            started.append((args, kwargs))

        def start(self):
            raise AssertionError("replay cleanup started a native thread")

    with use_platform(replay):
        generation = ai_dialog._GenerateDialog()
        generation.source_box.setPlainText("study source")
        generation._start_generation()
        monkeypatch.setattr(ai_dialog.threading, "Thread", ForbiddenThread)
        anki.gui.answers.append(True)
        generation.reject()

        monkeypatch.setattr(
            QFileDialog, "getOpenFileNames",
            lambda *args, **kwargs: ([str(source)], ""), raising=False)
        attachment = ai_dialog._GenerateDialog()
        attachment._attach()
        attachment.reject()

    assert started == []
    assert generation._result == 0
    assert attachment._result == 0


def test_returned_background_work_progresses_through_explicit_advances(anki):
    from internpearls import background

    delivered = []
    runner = mock_anki.Runner(anki)

    def flow():
        background._run_in_background(
            lambda: "fixture", lambda result, error: delivered.append((result, error)))

    started = runner.start_protocol(flow, epoch=39)
    at_complete = runner.feed_protocol(_protocol_request(started, 1, [
        {"type": "advance", "elapsed_ms": 0, "checkpoint_credits": 1},
    ]))
    done = runner.feed_protocol(_protocol_request(at_complete, 2, [
        {"type": "advance", "elapsed_ms": 0, "checkpoint_credits": 1},
    ]))

    assert started["pending"][0]["stage"] == "background-fetch:start"
    assert at_complete["pending"][0]["stage"] == "background-fetch:complete"
    assert done["status"] == "done"
    assert done["pending"] == []
    assert delivered == [("fixture", None)]


def test_typed_file_selection_reaches_production_attachment_worker(
        anki, monkeypatch, tmp_path):
    from internpearls import ai_dialog

    source = tmp_path / "upload.png"
    source.write_bytes(b"image bytes")
    anki.gui.uploads[source.name] = str(source)
    _ready_generate_dialog(monkeypatch)
    runner = mock_anki.Runner(anki)
    first = runner.start_protocol(ai_dialog.generate_cards, epoch=15)
    attach_id = next(
        node["id"] for node in first["payload"]["contract_tree"]["nodes"]
        if node["kind"] == "button" and node["text"] == "Attach images or PDFs")
    picker = runner.feed_protocol(_protocol_request(first, 1, [
        {"type": "activate", "id": attach_id},
    ]))

    assert picker["payload"]["kind"] == "files"
    file_action = {
        "type": "select-files",
        "id": picker["payload"]["id"],
        "accept": picker["payload"]["accept"],
        "files": [{"name": source.name, "type": "image/png",
                   "size": source.stat().st_size}],
    }
    reading = runner.feed_protocol(_protocol_request(picker, 2, [file_action]))

    assert reading["status"] == "need"
    assert [item["kind"] for item in reading["pending"]] == ["attachment"]


def test_typed_single_file_selection_reaches_production_connection_worker(
        anki, monkeypatch, tmp_path):
    from aqt.qt import QFileDialog
    from internpearls import ai_cli, ai_setup

    executable = tmp_path / "assistant"
    executable.write_bytes(b"fixture")
    anki.gui.uploads[executable.name] = str(executable)
    monkeypatch.setattr(
        ai_cli, "test_connection",
        lambda kind, path: {"state": "working", "detail": os.path.basename(path)})
    delivered = []
    runner = mock_anki.Runner(anki)

    def flow():
        path, _ = QFileDialog.getOpenFileName(
            None, "Locate assistant", "", "Programs (*.bin)")
        if path:
            ai_setup.run_connection_test_async(
                anki.mw, "claude", path, delivered.append)

    picker = runner.start_protocol(flow, epoch=40)
    action = {
        "type": "select-files",
        "id": picker["payload"]["id"],
        "accept": picker["payload"]["accept"],
        "files": [{"name": executable.name,
                   "type": "application/octet-stream",
                   "size": executable.stat().st_size}],
    }
    started = runner.feed_protocol(_protocol_request(picker, 1, [action]))
    at_complete = runner.feed_protocol(_protocol_request(started, 2, [
        {"type": "advance", "elapsed_ms": 0, "checkpoint_credits": 1},
    ]))
    done = runner.feed_protocol(_protocol_request(at_complete, 3, [
        {"type": "advance", "elapsed_ms": 0, "checkpoint_credits": 1},
    ]))

    assert picker["payload"]["kind"] == "file"
    assert started["pending"][0]["kind"] == "connection"
    assert done["status"] == "done"
    assert delivered == ["Working: assistant"]


@pytest.mark.parametrize("cancel_kind", ["empty-selection", "close"])
def test_typed_single_file_picker_cancel_resolves_to_none(
        anki, cancel_kind):
    from aqt.qt import QFileDialog

    runner = mock_anki.Runner(anki)

    def flow():
        path, _ = QFileDialog.getOpenFileName(
            None, "Locate assistant", "", "Programs (*.bin)")
        anki.mw._config["selected_file"] = path or None
        anki.gui.next_interaction({"kind": "after-picker"})

    picker = runner.start_protocol(flow, epoch=44)
    if cancel_kind == "empty-selection":
        action = {
            "type": "select-files",
            "id": picker["payload"]["id"],
            "accept": picker["payload"]["accept"],
            "files": [],
        }
    else:
        action = {"type": "close", "id": picker["payload"]["id"]}

    cancelled = runner.feed_protocol(_protocol_request(picker, 1, [action]))

    assert cancelled["status"] == "need"
    assert cancelled["payload"] == {"kind": "after-picker"}
    assert anki.mw._config["selected_file"] is None


def test_protocol_rejects_more_than_one_thousand_advances(anki):
    runner = mock_anki.Runner(anki)
    first = runner.start_protocol(
        lambda: anki.gui.next_interaction({"kind": "wait"}), epoch=7)
    advances = [
        {"type": "advance", "elapsed_ms": 0, "checkpoint_credits": 0}
        for _ in range(1001)
    ]

    response = runner.feed_protocol(_protocol_request(first, 1, advances))

    assert response["status"] == "contract-error"
    assert response["payload"] == {"code": "journal-limit"}
    assert runner.journal == ()


def test_protocol_rejects_more_than_ten_thousand_action_records(anki):
    runner = mock_anki.Runner(anki)
    first = runner.start_protocol(
        lambda: anki.gui.next_interaction({"kind": "wait"}), epoch=7)
    edit = {"type": "edit-text", "id": "input", "value": "fixture",
            "selection_start": 0, "selection_end": 0, "composing": False}

    response = runner.feed_protocol(_protocol_request(
        first, 1, [dict(edit) for _ in range(10001)]))

    assert response["status"] == "contract-error"
    assert response["payload"] == {"code": "journal-limit"}
    assert runner.journal == ()


def test_zero_delay_timer_waits_for_advance_and_timers_use_due_order():
    platform = ReplayPlatform(epoch=2)
    fired = []
    later = platform.create_timer(1, lambda: fired.append("later"), 20,
                                  single_shot=True)
    first = platform.create_timer(1, lambda: fired.append("first"), 0,
                                  single_shot=True)
    second = platform.create_timer(1, lambda: fired.append("second"), 0,
                                   single_shot=True)
    for timer in (later, first, second):
        timer.start()

    assert fired == []
    platform.settle_frontier()
    assert fired == []
    platform.advance(0, 0)
    assert fired == ["first", "second"]
    platform.advance(20, 0)
    assert fired == ["first", "second", "later"]
    assert first.timer_id == (1, 2)


def test_restarted_single_shot_timer_uses_fresh_deadline_and_barrier():
    platform = ReplayPlatform()
    fired = []
    timer = None

    def callback():
        fired.append(platform.now_ms)
        timer.start()

    timer = platform.create_timer(1, callback, 100, single_shot=True)
    timer.start()

    platform.advance(1000, 0)
    assert fired == [1000]
    platform.advance(100, 0)
    assert fired == [1000, 1100]


def test_checkpoint_credit_granted_before_task_exists_is_not_inherited():
    platform = ReplayPlatform()
    delivered = []
    platform.advance(0, 1)

    def compute(context):
        context.checkpoint("background-fetch:start")
        return "done"

    handle = platform.start_work(
        _request(), compute, delivered.append, pytest.fail)
    handle.start()

    assert handle.is_alive()
    assert delivered == []


def test_replay_rejects_unknown_work_kind_and_checkpoint_stage():
    platform = ReplayPlatform()

    with pytest.raises(ReplayError) as kind_error:
        platform.start_work(
            _request(kind="unknown", action="unknown"),
            lambda _context: None, pytest.fail, pytest.fail)
    assert kind_error.value.code == "invalid-action"

    handle = platform.start_work(
        _request(), lambda context: context.checkpoint("unknown-stage"),
        pytest.fail, pytest.fail)
    with pytest.raises(ReplayError) as stage_error:
        handle.start()
    assert stage_error.value.code == "invalid-action"


def test_runtime_work_contract_error_does_not_poison_protocol_journal(anki):
    from aqt.qt import QDialog, QPushButton, QVBoxLayout
    from internpearls.platform import platform

    runner = mock_anki.Runner(anki)

    def flow():
        dialog = QDialog()
        layout = QVBoxLayout(dialog)
        button = QPushButton("Start")

        def start_invalid_work():
            handle = platform().start_work(
                _request(kind="background-fetch"),
                lambda context: context.checkpoint("unknown-stage"),
                pytest.fail, pytest.fail)
            handle.start()

        button.clicked.connect(start_invalid_work)
        layout.addWidget(button)
        dialog.exec()

    first = runner.start_protocol(flow, epoch=29)
    button_id = next(
        node["id"] for node in first["payload"]["contract_tree"]["nodes"]
        if node["kind"] == "button")
    invalid = runner.feed_protocol(_protocol_request(first, 1, [
        {"type": "activate", "id": button_id},
    ]))
    retried = runner.feed_protocol(_protocol_request(
        invalid, 1, [], render_revision=invalid["render_revision"]))

    assert invalid["status"] == "contract-error"
    assert invalid["payload"] == {"code": "invalid-action"}
    assert runner.journal == ()
    assert retried["status"] == "need"


def test_reconstruction_resets_counters_and_uses_fixed_clocks():
    class Owner:
        pass

    first = ReplayPlatform(epoch=5)
    second = ReplayPlatform(epoch=5)
    with use_platform(first):
        first_request = new_work_request(Owner(), "fixture", "test")
    with use_platform(second):
        second_request = new_work_request(Owner(), "fixture", "test")

    assert first_request == second_request
    assert first.monotonic() == 0.0
    assert first.wall_now().isoformat() == "2026-01-01T00:00:00+00:00"
    first.advance(250, 0)
    assert first.monotonic() == 0.25
    assert first.wall_now().isoformat() == "2026-01-01T00:00:00.250000+00:00"


def test_replay_generated_ids_are_stable_within_epoch_and_distinct_between_epochs():
    from internpearls.ai_logic import generated_guid

    with use_platform(ReplayPlatform(epoch=5)):
        first = generated_guid()
        second = generated_guid()
    with use_platform(ReplayPlatform(epoch=5)):
        reconstructed = generated_guid()
    with use_platform(ReplayPlatform(epoch=6)):
        next_epoch = generated_guid()

    assert reconstructed == first
    assert second != first
    assert next_epoch != first


def test_production_usage_display_uses_replay_wall_clock(anki, monkeypatch):
    from internpearls import ai_dialog, ai_logic

    seen = []
    monkeypatch.setattr(
        ai_logic, "usage_line",
        lambda _reg, _backend, now, free_tier=False: seen.append(now) or "usage")
    replay = ReplayPlatform(now_ms=1250)

    with use_platform(replay):
        dialog = _ready_generate_dialog(monkeypatch)

    assert dialog.usage_row.text() == "usage"
    assert seen[-1] == replay.wall_now().timestamp()


def test_production_backup_and_import_temp_names_are_namespaced_by_epoch(
        anki, monkeypatch, tmp_path):
    from internpearls import collection

    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    monkeypatch.setattr(collection, "_deck_backup_folder",
                        lambda: str(backup_root))

    def export(path, _deck):
        with open(path, "wb") as output:
            output.write(b"backup")

    monkeypatch.setattr(collection, "_export_deck_to", export)

    first_platform = ReplayPlatform(
        epoch=32, scratch_root=str(tmp_path / "scratch"))
    with use_platform(first_platform):
        first_backup = os.path.basename(collection._backup_deck("Fixture"))
        next_backup = os.path.basename(collection._backup_deck("Fixture"))
        first_temp = first_platform.allocate_temporary_file(
            1, "sync-import", ".sync.apkg")

    os.remove(os.path.join(backup_root, first_backup))
    second_platform = ReplayPlatform(
        epoch=32, scratch_root=str(tmp_path / "scratch"))
    with use_platform(second_platform):
        second_backup = os.path.basename(collection._backup_deck("Fixture"))
        second_temp = second_platform.allocate_temporary_file(
            1, "sync-import", ".sync.apkg")

    third_platform = ReplayPlatform(
        epoch=33, scratch_root=str(tmp_path / "scratch"))
    with use_platform(third_platform):
        third_backup = os.path.basename(collection._backup_deck("Fixture"))
        third_temp = third_platform.allocate_temporary_file(
            1, "sync-import", ".sync.apkg")

    assert first_backup == second_backup
    assert first_backup != next_backup
    assert first_temp == second_temp
    assert third_backup != second_backup
    assert third_temp != second_temp
    assert os.path.exists(os.path.join(backup_root, second_backup))
    assert os.path.exists(os.path.join(backup_root, third_backup))


def test_fixture_maintenance_is_blocked_during_flow_and_enters_next_baseline(
        anki, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    revision_file = source / "fixture.txt"
    revision_file.write_text("one", encoding="utf8")
    runner = mock_anki.Runner(anki, paths=[str(source)])

    first = runner.start_protocol(
        lambda: anki.gui.next_interaction({"kind": "wait"}), epoch=30)
    with pytest.raises(mock_anki.ProtocolError):
        runner.maintain_fixture(
            lambda: revision_file.write_text("blocked", encoding="utf8"))
    assert revision_file.read_text(encoding="utf8") == "one"

    done = runner.feed_protocol(_protocol_request(first, 1))
    assert done["status"] == "done"
    runner.maintain_fixture(
        lambda: revision_file.write_text("two", encoding="utf8"))
    assert runner.fixture_revision == 1

    next_flow = runner.start_protocol(
        lambda: anki.gui.next_interaction({"kind": "next"}), epoch=31)
    revision_file.write_text("outside", encoding="utf8")
    replayed = runner.feed_protocol(_protocol_request(next_flow, 1))

    assert replayed["status"] == "done"
    assert revision_file.read_text(encoding="utf8") == "two"


def test_real_fixture_manifest_and_source_reads_suspend_until_advances(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    package = source / "fixture.apkg"
    package.write_bytes(b"fixture package")
    (source / "manifest.json").write_text(json.dumps({
        "decks": [{"name": "Fixture", "version": "v1",
                   "apkg": package.name}],
    }), encoding="utf8")
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    environment = dict(os.environ)
    environment["DEMO_SOURCE"] = str(source)
    environment["PYTHONPATH"] = os.pathsep.join(
        [os.path.join(root, "docs"), os.path.join(root, "tests"), root,
         environment.get("PYTHONPATH", "")])
    probe = subprocess.run(
        [sys.executable, "-c", """
import demo_harness as harness

harness._install_demo_net()
base = ("https://api.github.com/repos/" + harness.config.EXAMPLE_REPO
        + "/contents/")

def flow():
    manifest = harness.net._http_get(base + "manifest.json")
    package = harness.net._http_get(base + "fixture.apkg")
    assert harness.json.loads(manifest)["decks"][0]["name"] == "Fixture"
    assert package == b"fixture package"

response = harness.RUNNER.start_protocol(flow, epoch=41)
assert response["pending"][0]["stage"] == "fixture-fetch:start"
expected = [
    "fixture-fetch:complete", "fixture-fetch:start", "fixture-fetch:complete",
]
for sequence, stage in enumerate(expected, 1):
    response = harness.RUNNER.feed_protocol({
        "protocol": 1, "epoch": 41, "sequence": sequence,
        "render_revision": response["render_revision"],
        "actions": [{"type": "advance", "elapsed_ms": 0,
                     "checkpoint_credits": 1}],
    })
    assert response["pending"][0]["stage"] == stage
response = harness.RUNNER.feed_protocol({
    "protocol": 1, "epoch": 41, "sequence": 4,
    "render_revision": response["render_revision"],
    "actions": [{"type": "advance", "elapsed_ms": 0,
                 "checkpoint_credits": 1}],
})
assert response["status"] == "done"
"""],
        cwd=root, env=environment, capture_output=True, text=True)

    assert probe.returncode == 0, probe.stdout + probe.stderr


def test_real_package_cache_reconstructs_before_later_work(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    package = source / "fixture.apkg"
    package.write_bytes(b"fixture package")
    (source / "manifest.json").write_text(json.dumps({
        "decks": [{"name": "Fixture", "version": "v1",
                   "apkg": package.name}],
    }), encoding="utf8")
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    environment = dict(os.environ)
    environment["DEMO_SOURCE"] = str(source)
    environment["PYTHONPATH"] = os.pathsep.join(
        [os.path.join(root, "docs"), os.path.join(root, "tests"), root,
         environment.get("PYTHONPATH", "")])
    probe = subprocess.run(
        [sys.executable, "-c", """
import demo_harness as harness
from internpearls import sync
from internpearls.platform import new_work_request, platform

harness._install_demo_net()
cfg = sync._cfg()
cfg["gh_repo"] = harness.config.EXAMPLE_REPO
cfg["decks_dir"] = ""
delivered = []

def later_work(context):
    context.checkpoint("background-fetch:start")
    context.checkpoint("background-fetch:complete")
    return "later"

def flow():
    manifest, fetch, _ = sync._fetch_manifest(cfg)
    path = sync._cached_fetch(fetch, manifest["decks"][0])
    assert open(path, "rb").read() == b"fixture package"
    harness.MOCK.gui.next_interaction({"kind": "after-package"})
    request = new_work_request(
        harness.MOCK.mw, "background-fetch", "background.fetch")
    handle = platform().start_work(
        request, later_work, delivered.append, delivered.append)
    handle.start()

response = harness.RUNNER.start_protocol(flow, epoch=45)
expected = [
    "fixture-fetch:complete", "fixture-fetch:start", "fixture-fetch:complete",
]
for sequence, stage in enumerate(expected, 1):
    response = harness.RUNNER.feed_protocol({
        "protocol": 1, "epoch": 45, "sequence": sequence,
        "render_revision": response["render_revision"],
        "actions": [{"type": "advance", "elapsed_ms": 0,
                     "checkpoint_credits": 1}],
    })
    assert response["pending"][0]["stage"] == stage
response = harness.RUNNER.feed_protocol({
    "protocol": 1, "epoch": 45, "sequence": 4,
    "render_revision": response["render_revision"],
    "actions": [{"type": "advance", "elapsed_ms": 0,
                 "checkpoint_credits": 1}],
})
assert response["payload"] == {"kind": "after-package"}

response = harness.RUNNER.feed_protocol({
    "protocol": 1, "epoch": 45, "sequence": 5,
    "render_revision": response["render_revision"],
    "actions": [],
})
assert response["pending"] == [{
    "task_id": [45, 1, 3, 1],
    "kind": "background-fetch",
    "stage": "background-fetch:start",
}]
assert delivered == []
"""],
        cwd=root, env=environment, capture_output=True, text=True)

    assert probe.returncode == 0, probe.stdout + probe.stderr


@pytest.mark.parametrize("outcome", [
    "success", "cancel-suspended", "cancel-completed", "failure",
])
def test_auto_sync_package_cache_is_private_until_delivery(tmp_path, outcome):
    source = tmp_path / "source"
    source.mkdir()
    for name in ("first", "second", "existing", "unrelated"):
        (source / (name + ".apkg")).write_bytes(name.encode())
    (source / "manifest.json").write_text(json.dumps({
        "decks": [
            {"name": "First", "version": "v1", "apkg": "first.apkg"},
            {"name": "Second", "version": "v1", "apkg": "second.apkg"},
        ],
    }), encoding="utf8")
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    environment = dict(os.environ)
    environment["DEMO_SOURCE"] = str(source)
    environment["PYTHONPATH"] = os.pathsep.join(
        [os.path.join(root, "docs"), os.path.join(root, "tests"), root,
         environment.get("PYTHONPATH", "")])
    probe = subprocess.run(
        [sys.executable, "-c", """
import os
import sys
from unittest.mock import patch

import demo_harness as harness
from demo.replay import ReplayPlatform
from internpearls import background, sync
from internpearls.platform import new_work_request, use_platform

outcome = sys.argv[1]
harness._install_demo_net()
harness.MOCK.mw._config = {
    "github_decks_repo": harness.config.EXAMPLE_REPO,
    "auto_sync_decks": True,
}
source_key = ("github", harness.config.EXAMPLE_REPO, sync._cfg()["gh_ref"])
original = {
    (source_key, "Existing", "existing.apkg", "v1"):
        os.path.join(harness.SOURCE, "existing.apkg"),
}
sync._apkg_cache.clear()
sync._apkg_cache.update(original)
replay = ReplayPlatform(scratch_root=os.path.join(harness.SOURCE, "scratch"))
delivered, errors, handles, later = [], [], [], []
run_background = background._run_in_background

def later_work(context):
    context.checkpoint("background-fetch:start")
    context.checkpoint("background-fetch:complete")
    return "later"

def finish(result, error):
    if error is not None:
        errors.append(error)
        return
    delivered.append((result, dict(sync._apkg_cache)))
    request = new_work_request(
        harness.MOCK.mw, "background-fetch", "background.fetch")
    handle = replay.start_work(request, later_work, later.append, errors.append)
    handle.start()

def capture(work, on_done):
    def compute():
        result = work()
        if outcome == "failure":
            raise RuntimeError("fetch failed")
        return result
    handles.append(run_background(compute, finish))

with use_platform(replay), patch.object(background, "_run_in_background", capture):
    background._auto_sync_check()
    assert len(handles) == 1
    handle = handles[0]
    assert replay.pending()[0]["stage"] == "background-fetch:start"
    expected = [
        "fixture-fetch:start", "fixture-fetch:complete",
        "fixture-fetch:start", "fixture-fetch:complete",
        "fixture-fetch:start", "fixture-fetch:complete",
        "background-fetch:complete",
    ]
    stop = 5 if outcome == "cancel-suspended" else 7
    for credit, stage in enumerate(expected[:stop], 1):
        replay.advance(0, 1)
        assert sync._apkg_cache == original, (credit, sync._apkg_cache)
        assert delivered == []
        if outcome == "failure" and credit == 7:
            assert not handle.is_alive()
            assert len(errors) == 1 and str(errors[0]) == "fetch failed"
        else:
            assert handle.is_alive(), credit
            assert replay.pending()[0]["stage"] == stage, credit
            assert errors == []

    if outcome in ("success", "cancel-completed"):
        # Observe the completed computation before its queued result is delivered.
        with patch.object(replay, "settle_frontier", return_value=False):
            replay.advance(0, 1)
        assert not handle.is_alive()
        assert delivered == []
        assert sync._apkg_cache == original

    if outcome.startswith("cancel"):
        handle.cancel()
        assert handle.cancellation_acknowledged()
    if outcome != "success":
        replay.settle_frontier()
        replay.advance(0, 1)
        assert sync._apkg_cache == original
        assert delivered == []
        assert replay.pending() == []
    else:
        # Another task's published entry must survive this task's publication.
        unrelated_key = (source_key, "Unrelated", "unrelated.apkg", "v1")
        sync._apkg_cache[unrelated_key] = os.path.join(
            harness.SOURCE, "unrelated.apkg")
        replay.settle_frontier()
        assert len(delivered) == 1
        result, published = delivered[0]
        first_key = (source_key, "First", "first.apkg", "v1")
        second_key = (source_key, "Second", "second.apkg", "v1")
        assert published == {
            **original, unrelated_key: sync._apkg_cache[unrelated_key],
            first_key: result["downloaded"]["First"],
            second_key: result["downloaded"]["Second"],
        }
        assert open(published[first_key], "rb").read() == b"first"
        assert open(published[second_key], "rb").read() == b"second"
        assert replay.pending()[0]["stage"] == "background-fetch:start"
        assert later == []
        replay.advance(0, 1)
        assert replay.pending()[0]["stage"] == "background-fetch:complete"
        assert later == []
        replay.advance(0, 1)
        replay.settle_frontier()
        assert later == ["later"]
        assert len(delivered) == 1
        assert sync._apkg_cache == published
        assert errors == []
""", outcome],
        cwd=root, env=environment, capture_output=True, text=True)

    assert probe.returncode == 0, probe.stdout + probe.stderr


def test_timer_delivery_guard_is_fixed_at_one_thousand():
    platform = ReplayPlatform()
    timer = platform.create_timer(1, lambda: None, 0)
    timer.start()

    with pytest.raises(ReplayError) as error:
        platform.advance(0, 0)

    assert error.value.code == "scheduler-limit"


def test_join_zero_never_progresses_demo_work():
    platform = ReplayPlatform(checkpoint_credits=0)
    delivered = []

    def compute(context):
        context.checkpoint("background-fetch:start")
        return "done"

    handle = platform.start_work(
        _request(), compute, delivered.append, pytest.fail)
    handle.start()
    handle.join(0)

    assert handle.is_alive()
    assert delivered == []


def test_checkpoint_suspends_and_recomputes_from_the_start():
    platform = ReplayPlatform(checkpoint_credits=0)
    runs = []
    events = []
    results = []

    def compute(context):
        runs.append("start")
        context.checkpoint("background-fetch:start")
        context.emit({"step": 1})
        context.checkpoint("background-fetch:complete")
        return "done"

    handle = platform.start_work(
        _request(), compute, results.append, pytest.fail, events.append)
    handle.start()
    assert handle.is_alive()

    platform.advance(0, 1)
    assert handle.is_alive()
    assert runs == ["start", "start"]
    assert events == [{"step": 1}]

    platform.advance(0, 1)
    assert not handle.is_alive()
    assert runs == ["start", "start", "start"]
    assert events == [{"step": 1}]
    assert results == ["done"]
    assert platform.pending() == []


def test_scratch_overlay_publishes_only_when_work_completes(tmp_path):
    platform = ReplayPlatform(checkpoint_credits=0,
                              scratch_root=str(tmp_path / "scratch"))
    published = platform.allocate_scratch(1, "attachment")

    def compute(context):
        path = os.path.join(context.scratch, "result.bin")
        with open(path, "wb") as output:
            output.write(b"partial")
        context.checkpoint("background-fetch:start")
        with open(path, "wb") as output:
            output.write(b"complete")
        return "ok"

    handle = platform.start_work(
        _request(), compute, lambda _result: None, pytest.fail)
    handle.start()

    assert not os.path.exists(os.path.join(published, "result.bin"))
    platform.advance(0, 1)
    assert open(os.path.join(published, "result.bin"), "rb").read() == b"complete"


def test_cancelled_work_discards_its_overlay(tmp_path):
    platform = ReplayPlatform(checkpoint_credits=0,
                              scratch_root=str(tmp_path / "scratch"))
    published = platform.allocate_scratch(1, "attachment")

    def compute(context):
        with open(os.path.join(context.scratch, "partial.bin"), "wb") as output:
            output.write(b"partial")
        context.checkpoint("background-fetch:start")

    handle = platform.start_work(
        _request(), compute, pytest.fail, pytest.fail)
    handle.start()
    handle.cancel()
    platform.advance(0, 1)

    assert not handle.is_alive()
    assert not os.path.exists(os.path.join(published, "partial.bin"))


def test_cancel_before_result_delivery_discards_completed_overlay(tmp_path):
    platform = ReplayPlatform(scratch_root=str(tmp_path / "scratch"))
    published = platform.allocate_scratch(1, "attachment")

    def compute(context):
        with open(os.path.join(context.scratch, "result.bin"), "wb") as output:
            output.write(b"complete")
        return "done"

    handle = platform.start_work(
        _request(), compute, pytest.fail, pytest.fail)
    handle.start()
    handle.cancel()
    platform.settle_frontier()

    assert not os.path.exists(os.path.join(published, "result.bin"))


def test_production_attachment_worker_writes_only_to_private_overlay(
        anki, monkeypatch, tmp_path):
    from aqt.qt import QFileDialog
    from internpearls import ai_logic

    source = tmp_path / "source.png"
    source.write_bytes(b"image bytes")
    monkeypatch.setattr(
        QFileDialog, "getOpenFileNames",
        lambda *args, **kwargs: ([str(source)], ""), raising=False)
    real_extract = ai_logic.extract_attachment
    destinations = []

    def extract(path, destination, **kwargs):
        destinations.append(destination)
        return real_extract(path, destination, **kwargs)

    monkeypatch.setattr(ai_logic, "extract_attachment", extract)
    replay = ReplayPlatform(epoch=20, scratch_root=str(tmp_path / "replay"))
    with use_platform(replay):
        dialog = _ready_generate_dialog(monkeypatch)
        dialog._attach()
        published = dialog._attach_extract_dir
        replay.advance(0, 1)

        assert destinations
        assert os.path.commonpath([destinations[-1], published]) != published
        assert not os.path.exists(published) or list(os.scandir(published)) == []

        dialog._cancel_attachments()
        replay.advance(100, 0)

    assert dialog.session.attachments == []
    assert not os.path.exists(published)


def test_production_image_worker_discards_private_thumbnail_on_cancel(
        anki, monkeypatch, tmp_path):
    from internpearls import ai_dialog
    from internpearls.platform import platform_owner_id

    replay = ReplayPlatform(epoch=21, scratch_root=str(tmp_path / "replay"))
    with use_platform(replay):
        dialog = _ready_generate_dialog(monkeypatch)
        dialog.session.scratch = replay.allocate_scratch(
            platform_owner_id(dialog), "aigen")
        published = dialog.session.scratch
        real_open = open
        thumbnail_writes = []

        def tracking_open(path, mode="r", *args, **kwargs):
            if "_thumb-" in os.fspath(path) and "w" in mode:
                thumbnail_writes.append(os.fspath(path))
            return real_open(path, mode, *args, **kwargs)

        monkeypatch.setattr(ai_dialog, "open", tracking_open, raising=False)
        dialog._run_image_resolution([
            {"images": [{
                "source": "svg:<svg xmlns='http://www.w3.org/2000/svg'></svg>",
                "alt": "", "attribution": "",
            }]},
        ])
        replay.advance(0, 1)

        assert thumbnail_writes
        assert all(path.startswith(published + ".overlay" + os.sep)
                   for path in thumbnail_writes)
        assert (not os.path.exists(published)
                or not any(name.startswith("_thumb-")
                           for name in os.listdir(published)))

        dialog._cancel_generation()
        replay.advance(200, 0)

    assert (not os.path.exists(published)
            or not any(name.startswith("_thumb-") for name in os.listdir(published)))


def test_retries_have_distinct_ids_and_cancel_then_reopen_is_live():
    class Owner:
        pass

    platform = ReplayPlatform(epoch=9, checkpoint_credits=1)
    owner = Owner()
    delivered = []
    with use_platform(platform):
        first = new_work_request(owner, "assistant", "ai.generate")
        retry = new_work_request(owner, "assistant", "ai.generate", attempt=2)
        first_handle = platform.start_work(
            first, lambda context: context.checkpoint("assistant:event:1"), pytest.fail,
            pytest.fail)
        first_handle.start()
        first_handle.cancel()
        retry_handle = platform.start_work(
            retry, lambda _context: "open", delivered.append, pytest.fail)
        retry_handle.start()
        platform.settle_frontier()

    assert first_handle.task_id == "9:1:1:1"
    assert retry_handle.task_id == "9:1:1:2"
    assert delivered == ["open"]


def test_obsolete_completed_callback_is_not_delivered_after_cancel():
    platform = ReplayPlatform()
    delivered = []
    old = platform.start_work(
        _request(operation=1), lambda _context: "old",
        lambda result: delivered.append(result), pytest.fail)
    old.start()
    old.cancel()
    current = platform.start_work(
        _request(operation=2), lambda _context: "current",
        lambda result: delivered.append(result), pytest.fail)
    current.start()
    platform.settle_frontier()

    assert delivered == ["current"]


def test_production_duplicate_rescan_ignores_obsolete_result(anki):
    from internpearls import dupes_dialog

    common = "alpha beta gamma delta epsilon"
    anki.col.add_note("managed", [common, "answer"], ["InternPearls"],
                      deck="Managed")
    anki.col.add_note("other-one", [common + " one", "answer"], ["Other"],
                      deck="Other")
    replay = ReplayPlatform(epoch=33)

    with use_platform(replay):
        dialog = dupes_dialog._DuplicateScanDialog("InternPearls")
        obsolete = dialog._worker
        anki.col.add_note("other-two", [common + " two", "answer"], ["Other"],
                          deck="Other")
        dialog._rescan_fresh()
        current = dialog._worker
        replay.advance(0, 5)

    assert obsolete is not current
    assert not obsolete.is_alive()
    assert not current.is_alive()
    assert len(dialog._scan_result) == 2
    assert {pair[2][0] for pair in dialog._scan_result} == {
        anki.col.note_by_guid("other-one").id,
        anki.col.note_by_guid("other-two").id,
    }


def test_genuine_reconstruction_keeps_populated_collection_and_media_digest(
        anki, tmp_path):
    from internpearls import collection

    media = tmp_path / "media"
    media.mkdir()
    runner = mock_anki.Runner(anki, paths=[str(media)])
    card = {
        "note_type": "Study Deck - Basic",
        "fields": {"Front": "Question", "Back": "Answer"},
        "tags": [], "images": [], "rationale": "",
        "_media_files": ["figure.svg"],
    }

    def flow():
        collection.add_generated_notes(
            [dict(card)], {"figure.svg": b"<svg></svg>"},
            "Generated", "InternPearls")
        anki.gui.next_interaction({"kind": "first"})
        anki.gui.next_interaction({"kind": "second"})

    first = runner.start_protocol(flow, 11)
    before = _state_digest(anki, str(media))
    second = runner.feed_protocol(_protocol_request(first, 1))
    after = _state_digest(anki, str(media))

    assert first["status"] == "need"
    assert second["status"] == "need"
    assert len(anki.col._notes) == 1
    assert anki.col.media._files == {"figure.svg": b"<svg></svg>"}
    assert before == after


def test_repeated_edits_drive_production_widgets_without_work_credit(
        anki, monkeypatch):
    from internpearls import ai_cli, ai_dialog

    monkeypatch.setattr(
        ai_cli, "find_cli",
        lambda kind, override="": "/usr/bin/x" if kind == "claude" else None)
    monkeypatch.setattr(
        ai_cli, "probe", lambda kind, path: {"ok": True, "detail": "v1"})
    generation_calls = []

    def generate(*args, **kwargs):
        generation_calls.append(args[2])
        return {"text": "[]", "tokens": 0, "duration_s": 0}

    monkeypatch.setattr(ai_cli, "run_generation", generate)
    runner = mock_anki.Runner(anki)
    first = runner.start_protocol(ai_dialog.generate_cards, 13)
    input_id = next(
        node["id"] for node in first["payload"]["contract_tree"]["nodes"]
        if node["kind"] == "textarea"
        and node["placeholder"].startswith("Paste lecture"))
    edit = {"type": "edit-text", "id": input_id, "value": "first",
            "selection_start": 5, "selection_end": 5, "composing": False}
    second = runner.feed_protocol(_protocol_request(first, 1, [edit]))
    edit["value"] = "second"
    edit["selection_start"] = edit["selection_end"] = 6
    third = runner.feed_protocol(_protocol_request(second, 2, [edit]))
    current_source = next(
        node for node in third["payload"]["contract_tree"]["nodes"]
        if node["id"] == input_id)
    generate_id = next(
        node["id"] for node in third["payload"]["contract_tree"]["nodes"]
        if node["kind"] == "button" and node["text"] == "Generate"
        and node["effective_visible"])
    assert generation_calls == []
    working = runner.feed_protocol(_protocol_request(third, 3, [
        {"type": "activate", "id": generate_id},
    ]))

    assert current_source["value"] == "second"
    assert len(generation_calls) == 1
    assert working["pending"][0]["stage"] == "assistant:result"


def test_one_accepted_import_is_one_logical_collection_commit(anki, tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    runner = mock_anki.Runner(anki, paths=[str(media)])

    def flow():
        anki.gui.next_interaction({"kind": "confirm"})
        anki.col.add_note("fixture-guid", ["Front"], [],
                          mock_anki.make_model(fields=["Front"]), "Fixture")
        (media / "asset.bin").write_bytes(b"fixture")

    first = runner.start_protocol(flow, 17)
    done = runner.feed_protocol(_protocol_request(first, 1))
    digest = _state_digest(anki, str(media))
    duplicate = runner.feed_protocol(_protocol_request(first, 1))

    assert done["status"] == "done"
    assert duplicate == done
    assert len(anki.col._notes) == 1
    assert _state_digest(anki, str(media)) == digest


def test_production_generate_import_commits_exactly_once(
        anki, monkeypatch, tmp_path):
    from internpearls import ai_cli, ai_dialog, config

    card = [{
        "note_type": "Study Deck - Basic",
        "fields": {"Front": "Question", "Back": "Answer"},
        "tags": [], "images": [], "rationale": "",
    }]
    monkeypatch.setattr(
        ai_cli, "find_cli",
        lambda kind, override="": "/usr/bin/x" if kind == "claude" else None)
    monkeypatch.setattr(
        ai_cli, "probe", lambda kind, path: {"ok": True, "detail": "v1"})
    monkeypatch.setattr(
        ai_cli, "run_generation",
        lambda *args, **kwargs: {
            "text": json.dumps(card), "tokens": 1, "duration_s": 0,
        })
    media = tmp_path / "media"
    media.mkdir()
    runner = mock_anki.Runner(anki, paths=[config.STATE, str(media)])

    first = runner.start_protocol(ai_dialog.generate_cards, epoch=34)
    source_id = next(
        node["id"] for node in first["payload"]["contract_tree"]["nodes"]
        if node["kind"] == "textarea"
        and node["placeholder"].startswith("Paste lecture"))
    edited = runner.feed_protocol(_protocol_request(first, 1, [
        {"type": "edit-text", "id": source_id, "value": "study source",
         "selection_start": 12, "selection_end": 12, "composing": False},
    ]))
    generate_id = next(
        node["id"] for node in edited["payload"]["contract_tree"]["nodes"]
        if node["kind"] == "button" and node["text"] == "Generate"
        and node["effective_visible"])
    working = runner.feed_protocol(_protocol_request(edited, 2, [
        {"type": "activate", "id": generate_id},
    ]))
    review = runner.feed_protocol(_protocol_request(working, 3, [
        {"type": "advance", "elapsed_ms": 200, "checkpoint_credits": 1},
    ]))
    import_id = next(
        node["id"] for node in review["payload"]["contract_tree"]["nodes"]
        if node["kind"] == "button" and node["text"].startswith("Import")
        and node["effective_visible"])
    request = _protocol_request(review, 4, [
        {"type": "activate", "id": import_id},
    ])
    done = runner.feed_protocol(request)
    digest = _state_digest(anki, str(media))
    duplicate = runner.feed_protocol(request)

    assert done["status"] == "done"
    assert duplicate == done
    assert len(anki.col._notes) == 1
    assert len(anki.col._undo_entries) == 1
    assert _state_digest(anki, str(media)) == digest


def test_work_suspended_is_not_a_normal_exception():
    assert issubclass(WorkSuspended, BaseException)
    assert not issubclass(WorkSuspended, Exception)


def test_pdf_extraction_checkpoints_between_pages_and_images(tmp_path):
    from internpearls.ai_logic import _extract_pdf_pages

    class Image:
        name = "figure.png"
        data = b"image"

    class Page:
        def __init__(self, number):
            self.number = number
            self.images = [Image()]

        def extract_text(self):
            return f"page {self.number}"

    stages = []
    result = _extract_pdf_pages(
        [Page(1), Page(2)], str(tmp_path), "fixture.pdf", "fixture",
        checkpoint=stages.append)

    assert stages == [
        "attachment:page:1", "attachment:image:1:1",
        "attachment:page:2", "attachment:image:2:1",
    ]
    assert result["text"] == "page 1\npage 2"
    assert result["images"] == ["fixture-p1-img0.png", "fixture-p2-img0.png"]


def test_duplicate_index_checkpoints_between_bounded_batches():
    from internpearls.dupes import find_candidates

    rows = [(index, f"shared alpha beta token{index}", "", "")
            for index in range(26)]
    stages = []
    find_candidates(rows, rows, threshold=0, min_shared=0,
                    checkpoint=stages.append)

    assert stages == [
        "duplicate-index:right:batch:1", "duplicate-index:right:batch:2",
        "duplicate-index:frequency:batch:1", "duplicate-index:frequency:batch:2",
        "duplicate-index:weights:batch:1", "duplicate-index:weights:batch:2",
        "duplicate-index:postings:batch:1", "duplicate-index:postings:batch:2",
        "duplicate-index:left:batch:1", "duplicate-index:left:batch:2",
    ]


def test_production_duplicate_index_suspends_while_building_right_side():
    from internpearls.dupes import find_candidates

    rows = [(index, f"shared alpha beta token{index}", "", "")
            for index in range(26)]
    platform = ReplayPlatform(epoch=31)
    request = _request(
        epoch=31, kind="duplicate-index", action="dupes.scan")
    handle = platform.start_work(
        request,
        lambda context: find_candidates(
            rows, rows, threshold=0, min_shared=0,
            checkpoint=context.checkpoint),
        pytest.fail, pytest.fail)

    handle.start()

    assert handle.is_alive()
    assert platform.pending() == [{
        "task_id": [31, 1, 1, 1],
        "kind": "duplicate-index",
        "stage": "duplicate-index:right:batch:1",
    }]

    observed = []
    for _ in range(7):
        platform.advance(0, 1)
        observed.append(platform.pending()[0]["stage"])

    assert observed == [
        "duplicate-index:right:batch:2",
        "duplicate-index:frequency:batch:1",
        "duplicate-index:frequency:batch:2",
        "duplicate-index:weights:batch:1",
        "duplicate-index:weights:batch:2",
        "duplicate-index:postings:batch:1",
        "duplicate-index:postings:batch:2",
    ]
