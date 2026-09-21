import hashlib
import json
import os

import mock_anki
import pytest

from demo.replay import ReplayError, ReplayPlatform, WorkSuspended
from internpearls.platform import WorkRequest, new_work_request, use_platform


def _request(epoch=4, owner_id=1, operation=1, attempt=1, kind="fixture"):
    return WorkRequest(kind, owner_id, operation, attempt, {"action": "test"},
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
        context.checkpoint("one")
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
        context.checkpoint("one")
        context.emit({"step": 1})
        context.checkpoint("two")
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
        context.checkpoint("publish")
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
        context.checkpoint("wait")

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
            first, lambda context: context.checkpoint("wait"), pytest.fail,
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
    runner = mock_anki.Runner(anki)
    runs = []

    def flow():
        from internpearls.platform import platform

        value = {"text": ""}

        def compute(context):
            runs.append("run")
            context.checkpoint("one")
            return "complete"

        handle = platform().start_work(
            _request(epoch=13), compute,
            lambda result: value.update(result=result), pytest.fail)
        handle.start()
        while handle.is_alive():
            response = anki.gui.next_interaction({"kind": "edit", **value})
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
    edit = {"type": "edit-text", "id": "input", "value": "first",
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
        "duplicate-index:batch:1", "duplicate-index:batch:2",
    ]
