"""Synchronous waits for the add-on's background work, for tests only.

Each runs a dialog's poll loop to completion without a live QTimer. They live here,
not in internpearls/, so the add-on never ships them; install() attaches them to the
dialog classes, and both conftests call it at session start. Import this module only
after a Qt (mock or real) is installed into aqt.qt.
"""
import time

from aqt.qt import QApplication

from internpearls.platform import wait_for_mock_work


def _wait_for_worker(self, timeout=15):
    """Run the poll loop synchronously, without a live QTimer.

    A draft with images kicks off a second background phase once the CLI
    call itself finishes (see _start_image_phase), still on its own
    worker/timer pair rather than this one: drain that too, so a caller
    gets exactly the same end state a live QTimer eventually reaches,
    without needing to know a second phase happened at all.
    """
    end = time.time() + timeout
    worker = self._worker
    while worker.is_alive() and time.time() < end:
        time.sleep(0.05)
    wait_for_mock_work(worker)
    fire = getattr(self._timer, "fire", None) or self._timer.timeout.emit
    while not self._gen_done and time.time() < end:
        QApplication.processEvents()
        if self._worker is worker and not self._gen_done:
            fire()
        if self._worker is not worker:
            return
        time.sleep(0.005)
    if not self._gen_done:
        self._timer.stop()
        return
    while (hasattr(self, "_img_worker") and not self._img_done
          and time.time() < end):
        while self._img_worker.is_alive() and time.time() < end:
            time.sleep(0.05)
        wait_for_mock_work(self._img_worker)
        while not self._img_ready and time.time() < end:
            QApplication.processEvents()
            time.sleep(0.005)
        fire = getattr(self._img_timer, "fire", None)
        if fire is not None:
            fire()
        else:
            self._img_timer.timeout.emit()
        if not self._img_done:
            self._img_timer.stop()
            self._img_done = True
            if self._img_cancel_flag.is_set():
                self._abandon_image_phase()
                break
            self.session.image_data = self._img_results
            self._apply_review_state()


def _wait_for_near(self, timeout=15):
    """Deliver the duplicate lookup, and any lookup it queued,
    without a live event loop."""
    end = time.time() + timeout
    while self._near_worker is not None and time.time() < end:
        worker = self._near_worker
        worker.join(max(0.0, end - time.time()))
        wait_for_mock_work(worker)
        if self._near_worker is worker:
            QApplication.processEvents()
            time.sleep(0.005)


def _wait_for_backends(self, timeout=15):
    """Let the backend probe finish and deliver, like `_wait_for_scan`."""
    end = time.time() + timeout
    probe = self._probe
    while probe.is_alive() and time.time() < end:
        time.sleep(0.02)
    wait_for_mock_work(probe)
    while not self._backends_ready and time.time() < end:
        QApplication.processEvents()
        time.sleep(0.005)


def _wait_for_scan(self, timeout=15):
    """Run the scan to completion synchronously, without a live
    QTimer, the same shape `_wait_for_worker` uses."""
    end = time.time() + timeout
    while self._worker.is_alive() and time.time() < end:
        time.sleep(0.02)
    wait_for_mock_work(self._worker)
    while not self._scan_ready and time.time() < end:
        QApplication.processEvents()
        time.sleep(0.005)
    fire = getattr(self._timer, "fire", None)
    if fire is not None:
        fire()
    else:
        self._timer.timeout.emit()


def _wait_for_judge(self, timeout=15):
    """Run the judging call to completion synchronously, without a
    live QTimer, the same shape `_wait_for_scan` uses."""
    seq = self._judge_seq
    worker = self._judge_worker
    timer = self._judge_timer
    end = time.time() + timeout
    while worker.is_alive() and time.time() < end:
        time.sleep(0.02)
    wait_for_mock_work(worker)
    while not self._judge_ready and time.time() < end:
        QApplication.processEvents()
        time.sleep(0.005)
    fire = getattr(timer, "fire", None)
    if fire is not None:
        fire()
    else:
        timer.timeout.emit()


def install():
    from internpearls import ai_dialog, dupes_dialog

    ai_dialog._GenerateDialog._wait_for_worker = _wait_for_worker
    ai_dialog._GenerateDialog._wait_for_near = _wait_for_near
    dupes_dialog._DuplicateScanDialog._wait_for_backends = _wait_for_backends
    dupes_dialog._DuplicateScanDialog._wait_for_scan = _wait_for_scan
    dupes_dialog._DuplicateScanDialog._wait_for_judge = _wait_for_judge
