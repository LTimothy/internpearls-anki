"""Request capture regressions for every platform work caller."""
import tempfile
import time

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
        return tempfile.mkdtemp(prefix="platform-request-")


def _request(native):
    return native.requests[-1]


def test_background_fetch_captures_safe_request_metadata(anki):
    from internpearls import background

    native = _RecordingPlatform()
    with use_platform(native):
        background._run_in_background(lambda: "done", lambda result, error: None)

    request = _request(native)
    assert (request.kind, request.inputs) == (
        "background-fetch", {"action": "background.fetch"})


def test_streaming_prefetch_captures_safe_request_metadata(anki):
    from internpearls import widgets

    native = _RecordingPlatform()
    with use_platform(native):
        listing = widgets.StreamingList(lambda _item: widgets.QWidget(), [1, 2], batch=1)
        listing.isVisible = lambda: True
        listing._last_scroll = -1
        listing._idle_extend()

    request = _request(native)
    assert (request.kind, request.inputs) == (
        "list-prefetch", {"action": "streaming-list.prefetch", "count": 1,
                           "start": 1})
