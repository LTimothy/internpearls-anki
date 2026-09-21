"""Deterministic work, timer, clock, and scratch scheduling for the demo."""
from __future__ import annotations

import os
import re
import shutil
from collections import deque
from datetime import datetime, timedelta, timezone


_CHECKPOINT_STAGES = {
    "assistant": (
        re.compile(r"assistant:event:[1-9][0-9]*"),
        re.compile(r"assistant:result"),
        re.compile(r"duplicate-judge:(start|complete)"),
    ),
    "attachment": (
        re.compile(r"attachment:file:[1-9][0-9]*:(start|complete)"),
        re.compile(r"attachment:parse"),
        re.compile(r"attachment:page:[1-9][0-9]*"),
        re.compile(r"attachment:image:[1-9][0-9]*:[1-9][0-9]*"),
    ),
    "background-fetch": (
        re.compile(r"background-fetch:(start|complete)"),
        re.compile(r"fixture-fetch:(start|complete)"),
    ),
    "connection": (re.compile(r"connection:(start|complete)"),),
    "duplicate-index": (
        re.compile(
            r"duplicate-index:(left|right|frequency|weights|postings):batch:"
            r"[1-9][0-9]*"),
    ),
    "image": (
        re.compile(r"image:[1-9][0-9]*:[1-9][0-9]*:(resolve|publish)"),
    ),
    "list-prefetch": (re.compile(r"list-prefetch:(start|complete)"),),
}


class WorkSuspended(BaseException):
    """Private control flow used to stop work at a replay checkpoint."""

    def __init__(self, stage, ordinal):
        super().__init__(stage)
        self.stage = stage
        self.ordinal = ordinal


class ReplayError(Exception):
    """A fixed, public-safe replay contract failure."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class _ReplayWorkContext:
    def __init__(self, handle):
        self._handle = handle
        self._checkpoint_ordinal = 0
        self._event_ordinal = 0

    def cancelled(self):
        return self._handle._cancelled

    def checkpoint(self, stage):
        self._handle._platform._validate_checkpoint(
            self._handle.request.kind, stage)
        self._checkpoint_ordinal += 1
        self._handle._stage = str(stage)
        if self._handle._cancelled:
            raise WorkSuspended(stage, self._checkpoint_ordinal)
        if self._checkpoint_ordinal > self._handle._checkpoint_credits:
            raise WorkSuspended(stage, self._checkpoint_ordinal)

    def emit(self, event):
        if self._handle._cancelled:
            return
        self._event_ordinal += 1
        self._handle._run_events.append((self._event_ordinal, dict(event)))

    @property
    def scratch(self):
        return self._handle._prepare_overlay()


class _ReplayWorkHandle:
    def __init__(self, replay, request, compute, on_result, on_error, on_event,
                 scratch):
        self._platform = replay
        self.request = request
        self.task_id = (f"{request.epoch}:{request.owner_id}:"
                        f"{request.operation_ordinal}:{request.attempt}")
        self._task_tuple = (request.epoch, request.owner_id,
                            request.operation_ordinal, request.attempt)
        self._compute = compute
        self._on_result = on_result
        self._on_error = on_error
        self._on_event = on_event
        self._scratch = scratch
        self._overlay = None
        self._started = False
        self._alive = False
        self._cancelled = False
        self._cancellation_acknowledged = False
        self._stage = "queued"
        self._run_events = []
        self._delivered_event_ordinals = set()
        self._checkpoint_credits = 0

    def _prepare_overlay(self):
        if self._scratch is None:
            self._scratch = self._platform._allocate_scratch(
                self.request.owner_id, self.request.kind, claim=False)
        if self._overlay is not None:
            return self._overlay
        self._overlay = self._scratch + ".overlay"
        shutil.rmtree(self._overlay, ignore_errors=True)
        if os.path.isdir(self._scratch):
            shutil.copytree(self._scratch, self._overlay)
        else:
            os.makedirs(self._overlay, exist_ok=True)
        return self._overlay

    def _discard_overlay(self):
        if self._overlay is not None:
            shutil.rmtree(self._overlay, ignore_errors=True)
            self._overlay = None

    def _publish_overlay(self):
        if self._overlay is None:
            return
        backup = self._scratch + ".previous"
        shutil.rmtree(backup, ignore_errors=True)
        if os.path.exists(self._scratch):
            os.replace(self._scratch, backup)
        try:
            os.replace(self._overlay, self._scratch)
        except BaseException:
            if os.path.exists(backup):
                os.replace(backup, self._scratch)
            raise
        finally:
            shutil.rmtree(backup, ignore_errors=True)
        self._overlay = None

    def _published_value(self, value, overlay):
        if isinstance(value, str):
            if value == overlay:
                return self._scratch
            prefix = overlay + os.sep
            if value.startswith(prefix):
                return self._scratch + value[len(overlay):]
            return value
        if isinstance(value, list):
            return [self._published_value(item, overlay) for item in value]
        if isinstance(value, tuple):
            return tuple(self._published_value(item, overlay) for item in value)
        if isinstance(value, dict):
            return {key: self._published_value(item, overlay)
                    for key, item in value.items()}
        return value

    def _queue_events(self):
        if self._on_event is None:
            return
        for ordinal, event in self._run_events:
            if ordinal in self._delivered_event_ordinals:
                continue
            self._delivered_event_ordinals.add(ordinal)
            self._platform._queue_delivery(self, self._on_event, event)

    def _complete(self, result):
        if self._cancelled:
            self._discard_overlay()
            return
        overlay = self._overlay
        self._publish_overlay()
        if overlay is not None:
            result = self._published_value(result, overlay)
        self._on_result(result)

    def _run(self):
        if self._cancelled:
            self._alive = False
            return
        self._platform._remove_deliveries(self)
        self._discard_overlay()
        self._run_events = []
        self._stage = "running"
        self._alive = True
        context = _ReplayWorkContext(self)
        external = self._platform._take_external_overlay()
        try:
            from internpearls.platform import use_work_context
            with use_work_context(context):
                result = self._compute(context)
        except WorkSuspended as suspended:
            self._stage = suspended.stage
            self._discard_overlay()
            self._platform._restore_external_overlay(external)
            self._queue_events()
            return
        except ReplayError:
            self._alive = False
            self._stage = "error"
            self._discard_overlay()
            self._platform._restore_external_overlay(external)
            raise
        except Exception as error:
            self._alive = False
            self._stage = "error"
            self._discard_overlay()
            self._platform._restore_external_overlay(external)
            self._queue_events()
            if not self._cancelled:
                self._platform._queue_delivery(self, self._on_error, error)
            return
        self._queue_events()
        if self._cancelled:
            self._alive = False
            self._discard_overlay()
            self._platform._restore_external_overlay(external)
            return
        self._alive = False
        self._stage = "complete"
        self._platform._queue_delivery(self, self._complete, result)

    def start(self):
        if self._started:
            return
        self._started = True
        self._run()

    def is_alive(self):
        return self._alive

    def join(self, timeout=None):
        return None

    def cancel(self):
        self._cancelled = True
        self._cancellation_acknowledged = True
        self._alive = False
        self._stage = "cancelled"
        self._discard_overlay()
        self._platform._remove_deliveries(self)

    def cancellation_acknowledged(self):
        return self._cancellation_acknowledged


class _ReplayTimerHandle:
    def __init__(self, replay, owner_id, ordinal, callback, interval_ms,
                 single_shot):
        self._platform = replay
        self.owner_id = owner_id
        self.registration_ordinal = ordinal
        self.timer_id = (owner_id, ordinal)
        self._callback = callback
        self._interval_ms = interval_ms
        self._single_shot = bool(single_shot)
        self._active = False
        self._due_ms = None
        self._started_advance = None
        self._start_ordinal = 0

    def start(self):
        self._start_ordinal += 1
        self._active = True
        self._due_ms = self._platform.now_ms + self._interval_ms
        self._started_advance = self._platform._advance_ordinal

    def stop(self):
        self._active = False

    def is_active(self):
        return self._active

    def isActive(self):
        return self.is_active()

    def deleteLater(self):
        self.stop()

    def _fire(self):
        previous_due = self._due_ms
        previous_start = self._start_ordinal
        if self._single_shot:
            self._active = False
        self._callback()
        if (self._active and not self._single_shot
                and self._start_ordinal == previous_start):
            self._due_ms = previous_due + self._interval_ms
            self._started_advance = -1


class ReplayPlatform:
    """A deterministic Platform implementation rebuilt for each replay."""

    MAX_TIMER_DELIVERIES = 1000
    _WALL_ORIGIN = datetime(2026, 1, 1, tzinfo=timezone.utc)
    reconstructing = True

    def __init__(self, epoch=1, checkpoint_credits=0, now_ms=0,
                 scratch_root=None, take_overlay=None, restore_overlay=None):
        self.epoch = int(epoch)
        int(checkpoint_credits)
        self.now_ms = int(now_ms)
        self._take_overlay = take_overlay
        self._restore_overlay = restore_overlay
        self._owners = []
        self._owner_ordinals = {}
        self._operations = {}
        self._handles = []
        self._deliveries = deque()
        self._timers = []
        self._timer_ordinal = 0
        self._advance_ordinal = 0
        self._scratch_ordinals = {}
        self._token_ordinal = 0
        self._unclaimed_scratch = {}
        self._scratch_root = scratch_root or os.path.join(
            os.path.abspath(os.getenv("TMPDIR", "/tmp")),
            "internpearls-demo-replay")
        os.makedirs(self._scratch_root, exist_ok=True)
        prefix = f"e{self.epoch}-"
        for name in os.listdir(self._scratch_root):
            if name.startswith(prefix):
                path = os.path.join(self._scratch_root, name)
                if os.path.isdir(path):
                    shutil.rmtree(path)
                else:
                    os.remove(path)

    def owner_id(self, owner):
        for stored, owner_id in self._owners:
            if stored is owner:
                return owner_id
        owner_id = len(self._owners) + 1
        self._owners.append((owner, owner_id))
        self._owner_ordinals[owner_id] = 0
        return owner_id

    def allocate_work_identity(self, owner, kind, action, attempt):
        owner_id = self.owner_id(owner)
        key = (owner_id, kind, action)
        if attempt > 1 and key in self._operations:
            return owner_id, self._operations[key]
        ordinal = self._owner_ordinals[owner_id] + 1
        self._owner_ordinals[owner_id] = ordinal
        self._operations[key] = ordinal
        return owner_id, ordinal

    def _claim_scratch(self, owner_id):
        available = self._unclaimed_scratch.get(owner_id, [])
        return available.pop() if available else None

    def start_work(self, request, compute, on_result, on_error, on_event=None,
                   scratch_path=None):
        if request.kind not in _CHECKPOINT_STAGES:
            raise ReplayError("invalid-action")
        handle = _ReplayWorkHandle(
            self, request, compute, on_result, on_error, on_event,
            scratch_path or self._claim_scratch(request.owner_id))
        self._handles.append(handle)
        return handle

    @staticmethod
    def _validate_checkpoint(kind, stage):
        if (not isinstance(stage, str)
                or not any(pattern.fullmatch(stage)
                           for pattern in _CHECKPOINT_STAGES.get(kind, ()))):
            raise ReplayError("invalid-action")

    def create_timer(self, owner_id, callback, interval_ms, single_shot=False):
        if not isinstance(interval_ms, int) or interval_ms < 0:
            raise ReplayError("invalid-action")
        self._timer_ordinal += 1
        timer = _ReplayTimerHandle(
            self, owner_id, self._timer_ordinal, callback, interval_ms,
            single_shot)
        self._timers.append(timer)
        return timer

    def monotonic(self):
        return self.now_ms / 1000.0

    def wall_now(self):
        return self._WALL_ORIGIN + timedelta(milliseconds=self.now_ms)

    def deterministic_token(self):
        self._token_ordinal += 1
        return f"{self.epoch:08x}{self._token_ordinal:012x}"

    @staticmethod
    def _safe_purpose(purpose):
        return re.sub(r"[^a-zA-Z0-9_-]+", "-", purpose).strip("-") or "work"

    def _allocate_scratch(self, owner_id, purpose, claim):
        ordinal = self._scratch_ordinals.get(owner_id, 0) + 1
        self._scratch_ordinals[owner_id] = ordinal
        path = os.path.join(
            self._scratch_root,
            f"e{self.epoch}-o{owner_id}-s{ordinal}-{self._safe_purpose(purpose)}")
        os.makedirs(path, exist_ok=True)
        if claim:
            self._unclaimed_scratch.setdefault(owner_id, []).append(path)
        return path

    def allocate_scratch(self, owner_id, purpose):
        return self._allocate_scratch(owner_id, purpose, claim=True)

    def allocate_temporary_file(self, owner_id, purpose, suffix):
        folder = self._allocate_scratch(owner_id, purpose, claim=False)
        path = os.path.join(folder, "temporary" + suffix)
        with open(path, "xb"):
            pass
        return path

    def _take_external_overlay(self):
        scratch = {}
        for root, _, files in os.walk(self._scratch_root):
            for name in files:
                path = os.path.join(root, name)
                with open(path, "rb") as source:
                    scratch[os.path.relpath(path, self._scratch_root)] = source.read()
        external = self._take_overlay() if self._take_overlay is not None else None
        return external, scratch

    def _restore_external_overlay(self, snapshot):
        external, scratch = snapshot
        for name in os.listdir(self._scratch_root):
            path = os.path.join(self._scratch_root, name)
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
        for relative, data in scratch.items():
            path = os.path.join(self._scratch_root, relative)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as output:
                output.write(data)
        if self._restore_overlay is not None:
            self._restore_overlay(external)

    def _queue_delivery(self, handle, callback, value):
        self._deliveries.append((handle, callback, value))

    def _remove_deliveries(self, handle):
        self._deliveries = deque(
            delivery for delivery in self._deliveries
            if delivery[0] is not handle)

    def settle_frontier(self):
        delivered = False
        while self._deliveries:
            handle, callback, value = self._deliveries.popleft()
            if handle._cancelled:
                continue
            callback(value)
            delivered = True
        return delivered

    def _next_due_timer(self):
        eligible = [
            timer for timer in self._timers
            if timer._active and timer._due_ms <= self.now_ms
            and timer._started_advance < self._advance_ordinal
        ]
        return min(eligible,
                   key=lambda timer: (timer._due_ms,
                                      timer.registration_ordinal)) if eligible else None

    def advance(self, elapsed_ms, checkpoint_credits):
        if (not isinstance(elapsed_ms, int) or elapsed_ms < 0
                or not isinstance(checkpoint_credits, int)
                or checkpoint_credits < 0):
            raise ReplayError("invalid-action")
        self._advance_ordinal += 1
        self.now_ms += elapsed_ms
        eligible = [
            handle for handle in self._handles
            if handle._started and handle._alive and not handle._cancelled
        ]
        for handle in eligible:
            handle._checkpoint_credits += checkpoint_credits
        for handle in eligible:
            if handle._alive and not handle._cancelled:
                handle._run()
        changed = self.settle_frontier()
        deliveries = 0
        while True:
            timer = self._next_due_timer()
            if timer is None:
                break
            deliveries += 1
            if deliveries > self.MAX_TIMER_DELIVERIES:
                raise ReplayError("scheduler-limit")
            timer._fire()
            changed = True
            changed = self.settle_frontier() or changed
        return changed

    def pending(self):
        return [
            {"task_id": list(handle._task_tuple), "kind": handle.request.kind,
             "stage": handle._stage}
            for handle in self._handles
            if handle._started and handle._alive and not handle._cancelled
        ]
