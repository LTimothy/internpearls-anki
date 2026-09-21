"""Native platform behavior shared by Anki and later platform adapters."""
import os
import shutil
import threading
import time
from datetime import timedelta

import internpearls.platform as platform_module
from internpearls.platform import (NativePlatform, WorkRequest, new_work_request,
                                   platform, platform_owner_id, use_platform,
                                   wait_for_mock_work)


def _request(kind="test.work"):
    return WorkRequest(
        kind=kind,
        owner_id=17,
        operation_ordinal=3,
        attempt=1,
        inputs={"value": 4},
    )


def test_default_platform_uses_native_clock_and_scratch(anki):
    native = platform()

    assert isinstance(native, NativePlatform)
    assert native.monotonic() <= time.monotonic()
    assert native.wall_now().utcoffset() == timedelta(0)

    scratch = native.allocate_scratch(17, "assistant")
    try:
        assert os.path.isdir(scratch)
        assert os.path.basename(scratch).startswith("ip-assistant-")
    finally:
        shutil.rmtree(scratch)


def test_use_platform_scopes_and_restores_the_override(anki):
    original = platform()
    replacement = object()

    with use_platform(replacement):
        assert platform() is replacement

    assert platform() is original


def test_platform_allocates_replay_stable_owner_and_retry_identity(anki):
    class Owner:
        pass

    class ReplayPlatform(NativePlatform):
        pass

    owner = Owner()
    other_owner = Owner()
    native = NativePlatform()

    with use_platform(native):
        first = new_work_request(owner, "assistant", "ai.generate")
        retry = new_work_request(owner, "assistant", "ai.generate", attempt=2)
        next_operation = new_work_request(owner, "assistant", "ai.generate")
        other = new_work_request(other_owner, "assistant", "ai.generate")

    with use_platform(ReplayPlatform()):
        replayed = new_work_request(Owner(), "assistant", "ai.generate")

    assert (first.owner_id, first.operation_ordinal, first.attempt) == (1, 1, 1)
    assert (retry.owner_id, retry.operation_ordinal, retry.attempt) == (1, 1, 2)
    assert (next_operation.owner_id, next_operation.operation_ordinal) == (1, 2)
    assert (other.owner_id, other.operation_ordinal) == (2, 1)
    assert (replayed.owner_id, replayed.operation_ordinal) == (1, 1)


def test_owner_lookup_does_not_consume_an_operation_ordinal(anki):
    class Owner:
        pass

    owner = Owner()
    with use_platform(NativePlatform()):
        assert platform_owner_id(owner) == 1
        request = new_work_request(owner, "assistant", "ai.generate")

    assert (request.owner_id, request.operation_ordinal) == (1, 1)


def test_task_id_encodes_epoch_owner_operation_and_attempt_without_kind(anki):
    class Owner:
        pass

    native = NativePlatform()
    owner = Owner()
    with use_platform(native):
        first = new_work_request(owner, "assistant", "ai.generate")
        retry = new_work_request(owner, "assistant", "ai.generate", attempt=2)
    direct = WorkRequest("test.work", 17, 3, 1, {"value": 4})

    first_handle = native.start_work(
        first, lambda _context: None, lambda _result: None, lambda _error: None)
    retry_handle = native.start_work(
        retry, lambda _context: None, lambda _result: None, lambda _error: None)
    direct_handle = native.start_work(
        direct, lambda _context: None, lambda _result: None, lambda _error: None)

    assert first_handle.task_id == "1:1:1:1"
    assert retry_handle.task_id == "1:1:1:2"
    assert direct_handle.task_id == "0:17:3:1"


def test_nondefault_platform_epoch_appears_in_work_request_and_task_id(anki):
    class Owner:
        pass

    native = NativePlatform(epoch=41)
    with use_platform(native):
        request = new_work_request(Owner(), "assistant", "ai.generate")
    handle = native.start_work(
        request, lambda _context: None, lambda _result: None, lambda _error: None)

    assert (request.epoch, request.owner_id, request.operation_ordinal,
            request.attempt) == (41, 1, 1, 1)
    assert handle.task_id == "41:1:1:1"


def test_fresh_native_platform_resets_default_identity_counters(anki):
    class Owner:
        pass

    with use_platform(NativePlatform()):
        first = new_work_request(Owner(), "assistant", "ai.generate")
    with use_platform(NativePlatform()):
        reconstructed = new_work_request(Owner(), "assistant", "ai.generate")

    assert (first.epoch, first.owner_id, first.operation_ordinal) == (1, 1, 1)
    assert (reconstructed.epoch, reconstructed.owner_id,
            reconstructed.operation_ordinal) == (1, 1, 1)


def test_distinct_live_owners_do_not_collide_on_a_shared_runtime_key(anki, monkeypatch):
    class Owner:
        pass

    monkeypatch.setattr(platform_module, "id", lambda _owner: 7, raising=False)
    native = NativePlatform()
    first = Owner()
    second = Owner()

    assert native.owner_id(first) == 1
    assert native.owner_id(second) == 2


def test_released_owner_key_cannot_be_inherited_by_a_replacement(anki, monkeypatch):
    class Owner:
        pass

    monkeypatch.setattr(platform_module, "id", lambda _owner: 7, raising=False)
    native = NativePlatform()
    released = Owner()
    assert native.owner_id(released) == 1
    del released

    replacement = Owner()
    assert native.owner_id(replacement) == 2


def test_nonweakrefable_owners_receive_distinct_stable_ids(anki):
    native = NativePlatform()
    first = []
    second = []

    assert native.owner_id(first) == 1
    assert native.owner_id(second) == 2


def test_work_request_inputs_are_immutable(anki):
    class Owner:
        pass

    with use_platform(NativePlatform()):
        request = new_work_request(
            Owner(), "assistant", "ai.generate", inputs={"mode": "quick"})

    try:
        request.inputs["mode"] = "other"
    except TypeError:
        pass
    else:
        raise AssertionError("work request inputs must be immutable")


def test_work_request_freezes_nested_request_metadata(anki):
    request = WorkRequest(
        "test.work", 1, 1, 1, {"flags": {"enabled": True}, "counts": [1]})

    try:
        request.inputs["flags"]["enabled"] = False
    except TypeError:
        pass
    else:
        raise AssertionError("nested request metadata must be immutable")
    assert request.inputs["counts"] == (1,)


def test_work_waits_for_start_and_delivers_events_then_result_on_timer(anki):
    native = NativePlatform()
    ran = threading.Event()
    delivered = []

    def compute(context):
        ran.set()
        context.emit({"type": "progress", "value": 1})
        return {"answer": 5}

    handle = native.start_work(
        _request(), compute,
        lambda result: delivered.append(("result", result)),
        lambda error: delivered.append(("error", error)),
        lambda event: delivered.append(("event", event)),
    )

    handle.join(0)
    assert not ran.is_set()
    assert not handle.is_alive()
    assert delivered == []

    handle.start()
    assert ran.wait(timeout=1)
    handle.join(timeout=1)
    assert delivered == []
    anki.qt_timers[-1].fire()
    assert delivered == [
        ("event", {"type": "progress", "value": 1}),
        ("result", {"answer": 5}),
    ]
    assert handle.task_id


def test_join_leaves_completed_work_pending_for_timer_delivery(anki):
    delivered = []
    handle = NativePlatform().start_work(
        _request(), lambda _context: "done", delivered.append, delivered.append)

    handle.start()
    handle.join(timeout=1)

    assert delivered == []
    anki.qt_timers[-1].fire()
    assert delivered == ["done"]


def test_work_handle_exposes_only_the_platform_work_contract(anki):
    handle = NativePlatform().start_work(
        _request(), lambda _context: None, lambda _result: None,
        lambda _error: None)

    assert not hasattr(handle, "native_timer")


def test_wait_for_mock_work_delivers_without_exposing_a_native_timer(anki):
    delivered = []
    handle = NativePlatform().start_work(
        _request(), lambda _context: "done", delivered.append, delivered.append)

    handle.start()
    wait_for_mock_work(handle)

    assert delivered == ["done"]


def test_work_delivers_errors_on_timer(anki):
    native = NativePlatform()
    delivered = []

    def compute(_context):
        raise RuntimeError("broken")

    handle = native.start_work(
        _request(), compute,
        lambda result: delivered.append(("result", result)),
        lambda error: delivered.append(("error", str(error))),
    )
    handle.start()
    handle.join(timeout=1)
    assert delivered == []
    anki.qt_timers[-1].fire()
    assert delivered == [("error", "broken")]


def test_cancel_is_visible_to_compute_and_discards_delivery(anki):
    native = NativePlatform()
    entered = threading.Event()
    release = threading.Event()
    observed = []
    delivered = []

    def compute(context):
        entered.set()
        release.wait(timeout=1)
        observed.append(context.cancelled())
        context.checkpoint("after release")
        return "late"

    handle = native.start_work(
        _request(), compute, delivered.append, delivered.append)
    handle.start()
    assert entered.wait(timeout=1)
    handle.cancel()
    release.set()
    handle.join(timeout=1)
    anki.qt_timers[-1].fire()

    assert observed == [True]
    assert delivered == []


def test_join_zero_does_not_advance_running_work(anki):
    native = NativePlatform()
    entered = threading.Event()
    release = threading.Event()

    def compute(_context):
        entered.set()
        release.wait(timeout=1)
        return "done"

    handle = native.start_work(_request(), compute, lambda _result: None,
                               lambda _error: None)
    handle.start()
    assert entered.wait(timeout=1)

    started = time.monotonic()
    handle.join(0)
    elapsed = time.monotonic() - started

    assert elapsed < 0.1
    assert handle.is_alive()
    release.set()
    handle.join(timeout=1)


def test_native_timer_runs_qtimer_callback_and_honors_single_shot(anki):
    native = NativePlatform()
    calls = []
    timer = native.create_timer(17, lambda: calls.append("tick"), 25,
                                single_shot=True)

    assert not timer.is_active()
    timer.start()
    assert timer.is_active()
    anki.qt_timers[-1].fire()

    assert calls == ["tick"]
    assert not timer.is_active()


def test_native_work_poll_interval_is_bounded_and_nonzero(anki):
    handle = NativePlatform().start_work(
        _request(), lambda _context: None, lambda _result: None,
        lambda _error: None)

    assert 0 < anki.qt_timers[-1].interval <= 100
    assert not handle.is_alive()


def test_the_platform_owns_started_work_until_it_is_delivered(anki):
    """No call site holds a work handle, so the platform has to: otherwise the
    handle, its timer handle and the QTimer are only a reference cycle once the
    thread exits, and a collection before the next poll destroys the timer with
    the result still undelivered. This mock cannot show that collection (its
    QTimer stand-in keeps a registry of every timer ever built), so it pins the
    ownership itself; qt_tests/test_platform.py reproduces the dropped delivery
    under real Qt."""
    native = NativePlatform()
    delivered = []
    handle = native.start_work(_request(), lambda _context: "value",
                               delivered.append, lambda _error: None)

    assert native._live_work == []   # not yet started, nothing to keep alive
    handle.start()
    assert native._live_work == [handle]

    handle.join(timeout=1)
    assert native._live_work == [handle]   # still owed a delivery
    handle._deliver_for_mock()

    assert delivered == ["value"]
    assert native._live_work == []


def test_cancelled_work_is_released_once_its_thread_finishes(anki):
    native = NativePlatform()
    handle = native.start_work(_request(), lambda _context: "value",
                               lambda _result: None, lambda _error: None)
    handle.start()
    handle.cancel()
    handle.join(timeout=1)
    handle._deliver_for_mock()

    assert native._live_work == []
