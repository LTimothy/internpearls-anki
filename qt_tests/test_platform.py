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
