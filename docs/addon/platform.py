"""Platform boundary for background work, timers, clocks, and scratch space."""
from __future__ import annotations

import re
import tempfile
import threading
import time
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import count
from typing import Any, Callable, ContextManager, Mapping, Optional, Protocol

from aqt.qt import QTimer


@dataclass(frozen=True)
class WorkRequest:
    kind: str
    owner_id: int
    operation_ordinal: int
    attempt: int
    inputs: Mapping[str, Any]


_OWNER_IDS = count(1)


def platform_owner_id(owner):
    """Return the stable, session-scoped identity for one platform owner."""
    owner_id = getattr(owner, "_platform_owner_id", None)
    if owner_id is None:
        owner_id = next(_OWNER_IDS)
        setattr(owner, "_platform_owner_id", owner_id)
        setattr(owner, "_platform_operation_ordinal", 0)
    return owner_id


def new_work_request(owner, kind, action, *, attempt=1, inputs=None):
    """Create a deterministic request identity scoped to one UI owner."""
    owner_id = platform_owner_id(owner)
    ordinal = getattr(owner, "_platform_operation_ordinal") + 1
    setattr(owner, "_platform_operation_ordinal", ordinal)
    metadata = {"action": action}
    if inputs:
        metadata.update(inputs)
    return WorkRequest(kind, owner_id, ordinal, attempt, metadata)


def wait_for_mock_work(handle):
    """Complete native work synchronously in the lightweight Qt mock only."""
    if hasattr(QTimer, "registry"):
        handle.join()


class WorkContext(Protocol):
    def cancelled(self) -> bool:
        raise NotImplementedError

    def checkpoint(self, stage: str) -> None:
        raise NotImplementedError

    def emit(self, event: Mapping[str, Any]) -> None:
        raise NotImplementedError

    @property
    def scratch(self) -> str:
        raise NotImplementedError


class WorkHandle(Protocol):
    task_id: str

    def start(self) -> None:
        raise NotImplementedError

    def is_alive(self) -> bool:
        raise NotImplementedError

    def join(self, timeout=None) -> None:
        raise NotImplementedError

    def cancel(self) -> None:
        raise NotImplementedError


class TimerHandle(Protocol):
    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def is_active(self) -> bool:
        raise NotImplementedError


class Platform(Protocol):
    def start_work(self, request: WorkRequest, compute: Callable,
                   on_result: Callable, on_error: Callable,
                   on_event: Optional[Callable] = None) -> WorkHandle:
        raise NotImplementedError

    def create_timer(self, owner_id: int, callback: Callable[[], None],
                     interval_ms: int, single_shot: bool = False) -> TimerHandle:
        raise NotImplementedError

    def monotonic(self) -> float:
        raise NotImplementedError

    def wall_now(self) -> datetime:
        raise NotImplementedError

    def allocate_scratch(self, owner_id: int, purpose: str) -> str:
        raise NotImplementedError


class _WorkCancelled(Exception):
    pass


class _NativeTimerHandle:
    def __init__(self, callback, interval_ms, single_shot=False, parent=None):
        self._timer = QTimer(parent)
        self._callback = callback
        self._interval_ms = interval_ms
        self._single_shot = single_shot
        self._active = False
        self._timer.setSingleShot(single_shot)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._fire)

    def _fire(self):
        if self._single_shot:
            self.stop()
        self._callback()

    def start(self):
        self._active = True
        self._timer.start()

    def stop(self):
        self._active = False
        self._timer.stop()

    def is_active(self):
        return self._active

    def isActive(self):
        return self.is_active()

    def fire(self):
        """Drive a timer once in the lightweight Anki test harness."""
        fire = getattr(self._timer, "fire", None)
        if fire is not None:
            fire()
        else:
            self._timer.timeout.emit()

    @property
    def started(self):
        return getattr(self._timer, "started", None)

    @property
    def timeout(self):
        return self._timer.timeout


class _NativeWorkContext:
    def __init__(self, native, request, cancelled, events):
        self._native = native
        self._request = request
        self._cancelled = cancelled
        self._events = events
        self._scratch = request.inputs.get("scratch")

    def cancelled(self):
        return self._cancelled.is_set()

    def checkpoint(self, stage):
        if self.cancelled():
            raise _WorkCancelled(stage)

    def emit(self, event):
        if not self.cancelled():
            self._events.append(dict(event))

    @property
    def scratch(self):
        if self._scratch is None:
            self._scratch = self._native.allocate_scratch(
                self._request.owner_id, self._request.kind)
        return self._scratch


class _NativeWorkHandle:
    _POLL_MS = 0

    def __init__(self, native, request, compute, on_result, on_error, on_event):
        self.task_id = (f"{request.kind}:{request.owner_id}:"
                        f"{request.operation_ordinal}:{request.attempt}")
        self.request = request
        self._cancelled = threading.Event()
        self._events = deque()
        self._context = _NativeWorkContext(
            native, request, self._cancelled, self._events)
        self._compute = compute
        self._on_result = on_result
        self._on_error = on_error
        self._on_event = on_event
        self._result = None
        self._error = None
        self._finished = False
        self._delivered = False
        self._started = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._timer_handle = native.create_timer(
            request.owner_id, self._deliver, self._POLL_MS)

    @property
    def cancel_event(self):
        return self._cancelled

    @property
    def ident(self):
        return self._thread.ident

    def _run(self):
        try:
            self._result = self._compute(self._context)
        except _WorkCancelled:
            self._cancelled.set()
        except Exception as error:
            self._error = error
        finally:
            self._finished = True

    def _deliver(self):
        if self._delivered:
            return
        while self._events:
            event = self._events.popleft()
            if not self._cancelled.is_set() and self._on_event is not None:
                self._on_event(event)
        if not self._finished:
            return
        self._timer_handle.stop()
        self._delivered = True
        if self._cancelled.is_set():
            return
        if self._error is not None:
            self._on_error(self._error)
        else:
            self._on_result(self._result)

    def start(self):
        if self._started:
            return
        self._started = True
        self._timer_handle.start()
        self._thread.start()

    def is_alive(self):
        return self._thread.is_alive()

    def join(self, timeout=None):
        if self._started:
            self._thread.join(timeout)
        if not self._thread.is_alive():
            fire = getattr(self._timer_handle._timer, "fire", None)
            if fire is not None:
                fire()
        if not self._thread.is_alive():
            fire = getattr(self._timer_handle._timer, "fire", None)
            if fire is not None:
                fire()

    def cancel(self):
        self._cancelled.set()


class NativePlatform:
    def start_work(self, request, compute, on_result, on_error, on_event=None):
        return _NativeWorkHandle(
            self, request, compute, on_result, on_error, on_event)

    def create_timer(self, owner_id, callback, interval_ms, single_shot=False):
        return _NativeTimerHandle(callback, interval_ms, single_shot)

    def monotonic(self):
        return time.monotonic()

    def wall_now(self):
        return datetime.now(timezone.utc)

    def allocate_scratch(self, owner_id, purpose):
        safe_purpose = re.sub(r"[^a-zA-Z0-9_-]+", "-", purpose).strip("-")
        return tempfile.mkdtemp(prefix=f"ip-{safe_purpose or 'work'}-")


_NATIVE_PLATFORM = NativePlatform()
_PLATFORM_OVERRIDE = ContextVar("internpearls_platform", default=None)


def platform() -> Platform:
    return _PLATFORM_OVERRIDE.get() or _NATIVE_PLATFORM


@contextmanager
def use_platform(value: Platform) -> ContextManager[None]:
    token = _PLATFORM_OVERRIDE.set(value)
    try:
        yield
    finally:
        _PLATFORM_OVERRIDE.reset(token)
