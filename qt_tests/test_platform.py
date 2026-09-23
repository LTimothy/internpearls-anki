"""Real-Qt thread-affinity checks for the native platform."""
import threading
import time

import harness
from internpearls.platform import NativePlatform, WorkRequest


def _request(ordinal):
    return WorkRequest("test", 1, ordinal, 1, {"action": "test"})


def test_native_work_delivers_callbacks_on_the_gui_event_loop_thread():
    harness.bootstrap()
    app = harness.app()
    gui_thread = threading.get_ident()
    compute_threads = []
    delivery_threads = []
    callbacks = []
    native = NativePlatform()

    def compute(context):
        compute_threads.append(threading.get_ident())
        context.emit({"stage": "halfway"})
        return "done"

    def fail(_context):
        compute_threads.append(threading.get_ident())
        raise RuntimeError("expected")

    success = native.start_work(
        _request(1), compute,
        lambda result: (delivery_threads.append(threading.get_ident()),
                        callbacks.append(("result", result))),
        lambda error: (delivery_threads.append(threading.get_ident()),
                       callbacks.append(("error", str(error)))),
        lambda event: (delivery_threads.append(threading.get_ident()),
                       callbacks.append(("event", event["stage"]))))
    failure = native.start_work(
        _request(2), fail,
        lambda result: callbacks.append(("unexpected", result)),
        lambda error: (delivery_threads.append(threading.get_ident()),
                       callbacks.append(("error", str(error)))))

    success.start()
    failure.start()
    deadline = time.monotonic() + 2
    while len(callbacks) < 3 and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)

    success.join(timeout=1)
    failure.join(timeout=1)
    assert callbacks == [("event", "halfway"), ("result", "done"),
                         ("error", "expected")]
    assert compute_threads and all(thread != gui_thread for thread in compute_threads)
    assert delivery_threads and all(thread == gui_thread for thread in delivery_threads)


def test_work_result_survives_a_collection_with_no_caller_reference():
    """The direct reproduction of the dropped-delivery bug, which only shows up
    under real Qt: PyQt holds a bound-method slot's receiver weakly, so a handle
    nothing else references is just a cycle once its thread exits, and a
    collection before the next poll destroyed the QTimer with the result still
    undelivered. The platform owning started work is what closes it."""
    import gc

    harness.bootstrap()
    app = harness.app()
    native = NativePlatform()
    delivered = []

    def start_and_forget():
        handle = native.start_work(
            _request(1), lambda _context: "value", delivered.append,
            lambda error: delivered.append(error))
        handle.start()
        handle.join(timeout=2)   # the thread is done; nothing has delivered yet

    start_and_forget()           # the only caller reference is gone
    assert delivered == []
    gc.collect()                 # a cycle collection lands in the delivery window

    deadline = time.monotonic() + 5
    while not delivered and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)

    assert delivered == ["value"]
    assert native._live_work == []


def test_a_timer_is_destroyed_with_the_widget_that_owns_it():
    """A timer that outlived its widget fired into a deleted Qt object, which PyQt6
    turns into an abort. Parented to its owner, it goes when the owner goes."""
    harness.bootstrap()
    harness.app()
    from PyQt6 import sip
    from PyQt6.QtTest import QTest
    from aqt.qt import QWidget

    native = NativePlatform()
    fired = []
    owner = QWidget()
    timer = native.create_timer(native.owner_id(owner), lambda: fired.append(1), 10)
    timer.start()
    sip.delete(owner)
    QTest.qWait(60)
    assert fired == []


def test_work_whose_owner_dies_mid_flight_is_released_not_delivered():
    """Poll timers are parented to their owner, so closing a dialog while its
    work runs takes the delivery timer with it. The result must not reach the
    deleted widget, and the platform must stop holding the handle: without the
    release it stayed in _live_work, with its closures, for the whole session."""
    import threading
    from PyQt6 import sip
    from aqt.qt import QWidget

    harness.bootstrap()
    app = harness.app()
    native = NativePlatform()
    owner = QWidget()
    release, delivered = threading.Event(), []
    handle = native.start_work(
        WorkRequest("test", native.owner_id(owner), 1, 1, {"action": "test"}),
        lambda _context: (release.wait(3), "late")[1],
        delivered.append, delivered.append)
    handle.start()
    assert native._live_work == [handle]

    sip.delete(owner)            # the dialog closes with the work in flight
    release.set()
    handle.join(timeout=3)
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)

    assert delivered == []
    assert native._live_work == []
