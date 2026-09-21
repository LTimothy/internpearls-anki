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
