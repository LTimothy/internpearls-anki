"""Real-dialog request capture for duplicate scanning and judging."""
import time

import harness
from internpearls.platform import NativePlatform, use_platform


class _Work:
    task_id = "recorded-work"

    def start(self):
        pass

    def is_alive(self):
        return False

    def join(self, timeout=None):
        pass

    def cancel(self):
        pass


class _Timer:
    def start(self):
        pass

    def stop(self):
        pass

    def is_active(self):
        return False


class _RecordingPlatform:
    def __init__(self):
        self.requests = []
        self._identities = NativePlatform()
        self.epoch = self._identities.epoch

    def allocate_work_identity(self, owner, kind, action, attempt):
        return self._identities.allocate_work_identity(owner, kind, action, attempt)

    def owner_id(self, owner):
        return self._identities.owner_id(owner)

    def start_work(self, request, compute, on_result, on_error, on_event=None):
        self.requests.append(request)
        return _Work()

    def create_timer(self, owner_id, callback, interval_ms, single_shot=False):
        return _Timer()

    def monotonic(self):
        return time.monotonic()

    def wall_now(self):
        raise AssertionError("not used")

    def allocate_scratch(self, owner_id, purpose):
        return ""


def test_duplicate_dialog_callers_capture_safe_work_requests(monkeypatch):
    mock, _ = harness.bootstrap()
    app = harness.app()
    import mock_anki
    from internpearls import ai_cli, dupes_dialog

    mock.mw.col = mock_anki.MockCollection()
    mock.mw.col.add_note("one", ["alpha", "beta"], ["Scope"], deck="one")
    mock.mw.col.add_note("two", ["alpha", "gamma"], ["Other"], deck="two")
    monkeypatch.setattr(ai_cli, "detect_backends", lambda _cfg: {
        "chosen": "claude", "backends": {"claude": {"path": "/bin/true"}}})
    native = _RecordingPlatform()
    with use_platform(native):
        dialog = dupes_dialog._DuplicateScanDialog("Scope")
        dialog._pairs = [{"left": dialog._left_rows()[0],
                          "right": dialog._right_rows()[0]}]
        dialog._judge_with_ai()

    assert [(request.kind, request.inputs) for request in native.requests] == [
        ("duplicate-index", {"action": "dupes.scan", "left_count": 1,
                             "right_count": 1}),
        ("assistant", {"action": "dupes.judge", "pair_count": 1}),
    ]
    dialog.deleteLater()
    app.processEvents()


class _CancellableWork(_Work):
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class _TrackingPlatform(_RecordingPlatform):
    def __init__(self):
        super().__init__()
        self.works = []

    def start_work(self, request, compute, on_result, on_error, on_event=None):
        self.requests.append(request)
        work = _CancellableWork()
        self.works.append(work)
        return work


def test_a_rescan_cancels_the_scan_it_replaces():
    """A scan retired by a new one used to run to completion regardless, so each
    sensitivity or exclusion change stacked another full scan onto the CPU."""
    mock, _ = harness.bootstrap()
    app = harness.app()
    import mock_anki
    from internpearls import dupes_dialog

    mock.mw.col = mock_anki.MockCollection()
    mock.mw.col.add_note("one", ["alpha", "beta"], ["Scope"], deck="one")
    mock.mw.col.add_note("two", ["alpha", "gamma"], ["Other"], deck="two")
    native = _TrackingPlatform()
    with use_platform(native):
        dialog = dupes_dialog._DuplicateScanDialog("Scope")
        dialog._rescan()

    first, second = native.works[-2:]
    assert first.cancelled and not second.cancelled
    dialog.deleteLater()
    app.processEvents()


def test_a_hidden_streaming_list_stops_polling_until_it_is_shown():
    """While hidden and not fully built, the idle tick has nothing to do, so it must
    not keep re-arming itself every 150ms for as long as the widget exists."""
    _mock, q = harness.bootstrap()
    app = harness.app()
    from internpearls import widgets

    def row(_item):
        w = q.QWidget()
        w.setFixedHeight(50)
        return w

    lst = widgets.StreamingList(row, list(range(500)), batch=20)
    lst.resize(300, 200)
    lst._idle._fire()      # the timer's own tick, while the list has never been shown
    assert not lst._idle.is_active()

    lst.show()
    app.processEvents()
    assert lst._idle.is_active()
    lst.close()
    lst.deleteLater()
    app.processEvents()
