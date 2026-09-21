import hashlib
import json
import os

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
    done = runner.feed_protocol(_protocol_request(prompt, 2, [
        {"type": "edit-text", "id": line_id, "value": "typed",
         "selection_start": 5, "selection_end": 5, "composing": False},
        {"type": "activate", "id": ok_id},
    ]))

    assert done["status"] == "done"
    assert anki.mw._config["answer"] == "typed"


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


def test_replay_generated_ids_use_reset_deterministic_counter():
    from internpearls.ai_logic import generated_guid

    with use_platform(ReplayPlatform(epoch=5)):
        first = generated_guid()
        second = generated_guid()
    with use_platform(ReplayPlatform(epoch=5)):
        reconstructed = generated_guid()

    assert first == "iplocal-00000000000000000001"
    assert second == "iplocal-00000000000000000002"
    assert reconstructed == first


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


def test_production_backup_and_import_temp_names_reset_deterministically(
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

    assert first_backup == second_backup
    assert first_backup != next_backup
    assert first_temp == second_temp


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
        real_resolve = ai_dialog._resolve_one_image
        destinations = []

        def resolve(spec, scratch):
            destinations.append(scratch)
            return real_resolve(spec, scratch)

        monkeypatch.setattr(ai_dialog, "_resolve_one_image", resolve)
        dialog._run_image_resolution([
            {"images": ["svg:<svg xmlns='http://www.w3.org/2000/svg'></svg>"]},
        ])
        replay.advance(0, 1)

        assert destinations
        assert destinations[-1] != published
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
        replay.advance(0, 2)

    assert obsolete is not current
    assert not obsolete.is_alive()
    assert not current.is_alive()
    assert len(dialog._scan_result) == 2
    assert {pair[2][0] for pair in dialog._scan_result} == {
        anki.col.note_by_guid("other-one").id,
        anki.col.note_by_guid("other-two").id,
    }


def test_same_journal_replay_keeps_collection_and_media_digest(anki, tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    runner = mock_anki.Runner(anki, paths=[str(media)])

    def flow():
        response = anki.gui.next_interaction({"kind": "edit"})
        anki.mw._config["value"] = len(response["actions"])
        anki.gui.next_interaction({"kind": "finish"})

    first = runner.start_protocol(flow, 11)
    second = runner.feed_protocol(_protocol_request(first, 1))
    before = _state_digest(anki, str(media))
    duplicate = runner.feed_protocol(_protocol_request(first, 1))
    after = _state_digest(anki, str(media))

    assert duplicate == second
    assert before == after


def test_repeated_frontier_edits_do_not_grant_checkpoint_credit(anki):
    from aqt.qt import QDialog, QLineEdit, QVBoxLayout

    runner = mock_anki.Runner(anki)
    runs = []

    def flow():
        from internpearls.platform import platform

        value = {"text": ""}
        dialog = QDialog()
        layout = QVBoxLayout(dialog)
        input_box = QLineEdit()
        layout.addWidget(input_box)

        def compute(context):
            runs.append("run")
            context.checkpoint("background-fetch:start")
            return "complete"

        handle = platform().start_work(
            _request(epoch=13), compute,
            lambda result: value.update(result=result), pytest.fail)
        handle.start()
        while handle.is_alive():
            response = anki.gui.next_interaction({
                "kind": "edit", **value,
                "contract_tree": mock_anki.serialize_widget(dialog),
            })
            edits = [action for action in response["actions"]
                     if action["type"] == "edit-text"]
            if edits:
                value["text"] = edits[-1]["value"]
            for action in response["actions"]:
                if action["type"] == "advance":
                    platform().advance(action["elapsed_ms"],
                                       action["checkpoint_credits"])
        anki.mw._config["final"] = value

    first = runner.start_protocol(flow, 13)
    input_id = next(
        node["id"] for node in first["payload"]["contract_tree"]["nodes"]
        if node["kind"] == "line")
    edit = {"type": "edit-text", "id": input_id, "value": "first",
            "selection_start": 5, "selection_end": 5, "composing": False}
    second = runner.feed_protocol(_protocol_request(first, 1, [edit]))
    edit["value"] = "second"
    edit["selection_start"] = edit["selection_end"] = 6
    third = runner.feed_protocol(_protocol_request(second, 2, [edit]))

    assert third["status"] == "need"
    assert len(runs) == 3

    advance = {"type": "advance", "elapsed_ms": 200,
               "checkpoint_credits": 1}
    done = runner.feed_protocol(_protocol_request(third, 3, [advance]))
    assert done["status"] == "done"
    assert anki.mw._config["final"] == {"text": "second", "result": "complete"}


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

    rows = [(index, "shared alpha beta", "", "") for index in range(26)]
    stages = []
    find_candidates(rows, rows, threshold=0, min_shared=0,
                    checkpoint=stages.append)

    assert stages == [
        "duplicate-index:right:batch:1", "duplicate-index:right:batch:2",
        "duplicate-index:left:batch:1", "duplicate-index:left:batch:2",
    ]


def test_production_duplicate_index_suspends_while_building_right_side():
    from internpearls.dupes import find_candidates

    rows = [(index, "shared alpha beta", "", "") for index in range(26)]
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
