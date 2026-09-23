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


def test_streaming_prefetch_does_not_overlap_itself(anki):
    """Only one chunk may be in flight. A tick that beats the previous chunk's
    delivery reads the same unchanged built(), so without the guard it queued the
    same rows again and left another task pending on every tick."""
    from internpearls import widgets

    native = _RecordingPlatform()
    with use_platform(native):
        listing = widgets.StreamingList(
            lambda _item: widgets.QWidget(), list(range(20)), batch=1)
        listing.isVisible = lambda: True
        listing._last_scroll = -1
        listing._idle_extend()
        listing._idle_extend()   # a tick landing before the first chunk delivered
        listing._idle_extend()

    assert len(native.requests) == 1
    assert native.requests[0].inputs["start"] == 1


class _DeferredPlatform(_RecordingPlatform):
    """Holds each piece of work so a test decides when its result is delivered."""

    def __init__(self):
        super().__init__()
        self.pending = []

    def start_work(self, request, compute, on_result, on_error, on_event=None):
        self.requests.append(request)
        self.pending.append((compute, on_result))
        return _Work()


class _Checkpoints:
    def checkpoint(self, _name):
        pass


def test_a_prefetch_that_lands_after_a_scroll_is_dropped(anki):
    """A scroll can build rows while a prefetch for those same rows is in flight.
    Delivering that batch anyway renders them twice and pushes every later row out of
    place, so each row must appear exactly once, in order."""
    from internpearls import widgets

    def row(item):
        widget = widgets.QWidget()
        widget.item = item
        return widget

    native = _DeferredPlatform()
    with use_platform(native):
        listing = widgets.StreamingList(row, list(range(20)), batch=5)
        listing.isVisible = lambda: True
        listing._last_scroll = -1
        listing._idle_extend()
        listing._extend()
        compute, deliver = native.pending[0]
        deliver(compute(_Checkpoints()))
        listing.fill_all()

    lay = listing._rows_layout
    assert [lay.itemAt(i).widget().item for i in range(lay.count())] == list(range(20))
