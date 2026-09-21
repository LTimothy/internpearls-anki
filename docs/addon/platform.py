"""Platform boundary for background work, timers, clocks, and scratch space."""
from __future__ import annotations

import re
import tempfile
import threading
import time
import weakref
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import count
from types import MappingProxyType
from typing import Any, Callable, ContextManager, Mapping, Optional, Protocol

from aqt.qt import QTimer


def _freeze_metadata(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_metadata(item)
                                 for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_metadata(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_metadata(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze_metadata(item) for item in value)
    return value


@dataclass(frozen=True)
class WorkRequest:
    kind: str
    owner_id: int
    operation_ordinal: int
    attempt: int
    inputs: Mapping[str, Any]
    epoch: int = 0

    def __post_init__(self):
        object.__setattr__(self, "inputs", _freeze_metadata(self.inputs))


def platform_owner_id(owner):
    """Return the stable, session-scoped identity for one platform owner."""
    return platform().owner_id(owner)


def new_work_request(owner, kind, action, *, attempt=1, inputs=None):
    """Create a deterministic request identity scoped to one UI owner."""
    owner_id, ordinal = platform().allocate_work_identity(owner, kind, action, attempt)
    metadata = {"action": action}
    if inputs:
        metadata.update(inputs)
    return WorkRequest(kind, owner_id, ordinal, attempt, metadata,
                       epoch=platform().epoch)


def wait_for_mock_work(handle):
    """Complete native work synchronously in the lightweight Qt mock only."""
    if hasattr(QTimer, "registry"):
        handle.join()
        deliver = getattr(handle, "_deliver_for_mock", None)
        if deliver is not None:
            deliver()


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
    epoch: int

    def owner_id(self, owner) -> int:
        raise NotImplementedError

    def allocate_work_identity(self, owner, kind: str, action: str,
                               attempt: int) -> tuple[int, int]:
        raise NotImplementedError

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


class _WorkCancelled(BaseException):
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
    _POLL_MS = 20

    def __init__(self, native, request, compute, on_result, on_error, on_event):
        self.task_id = (f"{request.epoch}:{request.owner_id}:"
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

    def _deliver_for_mock(self):
        self._deliver()

    def cancel(self):
        self._cancelled.set()


class NativePlatform:
    def __init__(self, epoch=1):
        self.epoch = epoch
        self._weak_owner_ids = {}
        self._strong_owner_ids = []
        self._owner_ordinals = {}
        self._operations = {}
        self._next_owner_id = count(1)

    def _clear_owner_state(self, owner_id):
        self._owner_ordinals.pop(owner_id, None)
        for key in [key for key in self._operations if key[0] == owner_id]:
            del self._operations[key]

    def _release_weak_owner(self, owner_key, owner_ref, owner_id):
        entries = self._weak_owner_ids.get(owner_key, ())
        kept = [entry for entry in entries if entry[0] is not owner_ref]
        if kept:
            self._weak_owner_ids[owner_key] = kept
        else:
            self._weak_owner_ids.pop(owner_key, None)
        self._clear_owner_state(owner_id)

    def _next_id(self):
        owner_id = next(self._next_owner_id)
        self._owner_ordinals[owner_id] = 0
        return owner_id

    def owner_id(self, owner):
        owner_key = id(owner)
        entries = self._weak_owner_ids.get(owner_key, ())
        live_entries = []
        for owner_ref, owner_id in entries:
            target = owner_ref()
            if target is owner:
                return owner_id
            if target is not None:
                live_entries.append((owner_ref, owner_id))
            else:
                self._clear_owner_state(owner_id)
        if live_entries:
            self._weak_owner_ids[owner_key] = live_entries
        else:
            self._weak_owner_ids.pop(owner_key, None)

        for stored_owner, owner_id in self._strong_owner_ids:
            if stored_owner is owner:
                return owner_id

        owner_id = self._next_id()
        try:
            native_ref = weakref.ref(self)

            def release(owner_ref):
                native = native_ref()
                if native is not None:
                    native._release_weak_owner(owner_key, owner_ref, owner_id)

            owner_ref = weakref.ref(owner, release)
        except TypeError:
            self._strong_owner_ids.append((owner, owner_id))
        else:
            self._weak_owner_ids.setdefault(owner_key, []).append(
                (owner_ref, owner_id))
        return owner_id

    def allocate_work_identity(self, owner, kind, action, attempt):
        owner_id = self.owner_id(owner)
        operation_key = (owner_id, kind, action)
        if attempt > 1 and operation_key in self._operations:
            return owner_id, self._operations[operation_key]
        ordinal = self._owner_ordinals[owner_id] + 1
        self._owner_ordinals[owner_id] = ordinal
        self._operations[operation_key] = ordinal
        return owner_id, ordinal

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
