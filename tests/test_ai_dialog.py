"""Focused regressions for AI wizard attachment behavior."""
import os
import threading
import time

from internpearls import ai_cli, ai_dialog, ai_logic
from aqt.qt import QFileDialog


def _ready_dialog(monkeypatch):
    monkeypatch.setattr(
        ai_cli, "find_cli",
        lambda kind, override="": "/usr/bin/x" if kind == "claude" else None)
    monkeypatch.setattr(
        ai_cli, "probe", lambda kind, path: {"ok": True, "detail": "v1"})
    return ai_dialog._GenerateDialog()


def _finish_attachment_worker(dlg):
    dlg._attach_worker.join(timeout=2)
    assert not dlg._attach_worker.is_alive()
    dlg._attach_timer.fire()


def test_attachment_only_input_enables_generate_and_removal_disables_it(
        anki, monkeypatch, tmp_path):
    path = str(tmp_path / "lecture.png")
    monkeypatch.setattr(
        QFileDialog, "getOpenFileNames",
        lambda *args, **kwargs: ([path], ""), raising=False)
    def extract(source, dest, **kwargs):
        open(os.path.join(dest, "lecture.png"), "wb").write(b"image")
        return {"text": "", "images": ["lecture.png"],
                "images_undecoded": False}

    monkeypatch.setattr(ai_logic, "extract_attachment", extract)

    dlg = _ready_dialog(monkeypatch)
    dlg._attach()
    assert os.path.basename(dlg._attach_extract_dir).startswith(ai_logic.SCRATCH_PREFIX)
    _finish_attachment_worker(dlg)

    assert dlg.generate_btn.isEnabled()
    assert dlg.session.attachments[0][0] == path
    committed_name = dlg.session.attachments[0][1]["images"][0]
    committed_path = os.path.join(dlg.session.scratch, committed_name)
    assert os.path.exists(committed_path)
    remove = dlg._attachment_remove_buttons[path]
    assert remove._accessible == "Remove attachment: lecture.png"

    remove.clicked.emit()

    assert dlg.session.attachments == []
    assert not dlg.generate_btn.isEnabled()
    assert path not in dlg._attachment_remove_buttons
    assert not os.path.exists(committed_path)


def test_attachment_outputs_with_the_same_name_are_committed_without_collision(
        anki, monkeypatch, tmp_path):
    first = str(tmp_path / "first.pdf")
    second = str(tmp_path / "second.pdf")
    monkeypatch.setattr(
        QFileDialog, "getOpenFileNames",
        lambda *args, **kwargs: ([first, second], ""), raising=False)

    def extract(source, dest, **kwargs):
        payload = os.path.basename(source).encode()
        with open(os.path.join(dest, "image.png"), "wb") as image:
            image.write(payload)
        return {"text": "", "images": ["image.png"],
                "images_undecoded": False}

    monkeypatch.setattr(ai_logic, "extract_attachment", extract)
    dlg = _ready_dialog(monkeypatch)

    dlg._attach()
    _finish_attachment_worker(dlg)

    assert len(dlg.session.attachments) == 2
    names = [meta["images"][0] for _path, meta in dlg.session.attachments]
    assert len(set(names)) == 2
    contents = {open(os.path.join(dlg.session.scratch, name), "rb").read()
                for name in names}
    assert contents == {b"first.pdf", b"second.pdf"}


def test_removed_attachment_is_omitted_from_backend_material(
        anki, monkeypatch, tmp_path):
    omitted = str(tmp_path / "omit.pdf")
    kept = str(tmp_path / "keep.pdf")
    monkeypatch.setattr(
        QFileDialog, "getOpenFileNames",
        lambda *args, **kwargs: ([omitted, kept], ""), raising=False)

    def extract(source, dest, **kwargs):
        stem = os.path.splitext(os.path.basename(source))[0]
        open(os.path.join(dest, f"{stem}.png"), "wb").write(stem.encode())
        return {"text": f"{stem}-text", "images": [f"{stem}.png"],
                "images_undecoded": False}

    monkeypatch.setattr(ai_logic, "extract_attachment", extract)
    seen = {}

    def run_generation(kind, path, prompt, mode, scratch, **kwargs):
        seen["prompt"] = prompt
        seen["image_paths"] = kwargs["image_paths"]
        seen["redact_texts"] = kwargs["redact_texts"]
        return {"text": "[]", "tokens": 0, "duration_s": 0}

    monkeypatch.setattr(ai_cli, "run_generation", run_generation)
    dlg = _ready_dialog(monkeypatch)
    dlg._attach()
    _finish_attachment_worker(dlg)
    dlg._attachment_remove_buttons[omitted].clicked.emit()

    dlg._start_generation()
    dlg._worker.join(timeout=2)
    assert not dlg._worker.is_alive()

    assert "keep-text" in seen["prompt"]
    assert "omit-text" not in seen["prompt"]
    assert [os.path.basename(p) for p in seen["image_paths"]] == ["keep.png"]
    assert "keep-text" in seen["redact_texts"]
    assert "omit-text" not in seen["redact_texts"]
    dlg._cleanup_scratch()


def test_attachment_extraction_runs_off_thread_and_cancel_commits_nothing(
        anki, monkeypatch, tmp_path):
    path = str(tmp_path / "slow.pdf")
    monkeypatch.setattr(
        QFileDialog, "getOpenFileNames",
        lambda *args, **kwargs: ([path], ""), raising=False)
    started = threading.Event()
    worker_thread = []

    def extract(source, dest, cancel=None):
        worker_thread.append(threading.get_ident())
        started.set()
        deadline = time.monotonic() + 2
        while not (cancel and cancel()) and time.monotonic() < deadline:
            time.sleep(0.001)
        return {"text": "must not be committed", "images": [],
                "images_undecoded": False}

    monkeypatch.setattr(ai_logic, "extract_attachment", extract)
    dlg = _ready_dialog(monkeypatch)
    gui_thread = threading.get_ident()

    dlg._attach()
    assert started.wait(timeout=1)
    assert worker_thread == [dlg._attach_worker.ident]
    assert worker_thread[0] != gui_thread

    dlg._cancel_attachments()
    _finish_attachment_worker(dlg)

    assert dlg.session.attachments == []
    assert not dlg.generate_btn.isEnabled()
    assert dlg.attach_btn.isEnabled()


def test_close_waits_for_uninterruptible_extractor_before_private_cleanup(
        anki, monkeypatch, tmp_path):
    path = str(tmp_path / "slow.pdf")
    monkeypatch.setattr(
        QFileDialog, "getOpenFileNames",
        lambda *args, **kwargs: ([path], ""), raising=False)
    started = threading.Event()
    release = threading.Event()
    extract_dirs = []
    write_errors = []

    def extract(source, dest, cancel=None):
        extract_dirs.append(dest)
        started.set()
        release.wait(timeout=2)  # models a parser call that cannot be preempted
        try:
            with open(os.path.join(dest, "late.png"), "wb") as image:
                image.write(b"late")
        except OSError as e:
            write_errors.append(e)
        return {"text": "late text", "images": ["late.png"],
                "images_undecoded": False}

    monkeypatch.setattr(ai_logic, "extract_attachment", extract)
    dlg = _ready_dialog(monkeypatch)
    dlg._attach()
    assert started.wait(timeout=1)
    private_dir = os.path.dirname(extract_dirs[0])
    real_join = dlg._attach_worker.join
    join_timeouts = []

    def recording_join(timeout=None):
        join_timeouts.append(timeout)
        return real_join(timeout)

    dlg._attach_worker.join = recording_join

    dlg.reject()
    assert os.path.isdir(private_dir)
    release.set()
    real_join(timeout=2)
    deadline = time.monotonic() + 2
    while os.path.exists(private_dir) and time.monotonic() < deadline:
        time.sleep(0.001)

    assert write_errors == []
    assert join_timeouts == [None]
    assert not os.path.exists(private_dir)
    assert dlg.session.attachments == []


def test_failed_attachment_deletion_does_not_claim_removal(anki, monkeypatch, tmp_path):
    dlg = _ready_dialog(monkeypatch)
    dlg.session.scratch = str(tmp_path)
    image = tmp_path / "retained.png"
    image.write_bytes(b"private attachment")
    dlg.session.attachments = [("lecture.pdf", {"images": [image.name], "text": "source"})]
    remove = os.remove
    def denied(path):
        if path == str(image):
            raise PermissionError("cannot delete")
        return remove(path)
    monkeypatch.setattr(ai_dialog.os, "remove", denied)
    dlg._remove_attachment("lecture.pdf")
    assert len(dlg.session.attachments) == 1
    assert image.exists()


def test_attachment_only_summary_counts_only_real_sources(anki, monkeypatch):
    dlg = _ready_dialog(monkeypatch)
    dlg.session.source = ""
    dlg.session.attachments = [("lecture.pdf", {"images": [], "text": "source"})]
    dlg._update_review_summary()
    assert "from 1 source" in dlg.review_header.text()
    assert "2 sources" not in dlg.review_header.text()


def test_same_backend_new_cli_path_drops_stale_connection_result(
        anki, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        ai_dialog, "run_connection_test_async",
        lambda *args, **kwargs: captured.update(kwargs))
    dlg = _ready_dialog(monkeypatch)
    dlg.session.backend = "claude"
    dlg.session.cli_path = "/old/claude"

    dlg._test_backend_connection()
    dlg.session.cli_path = "/new/claude"

    assert captured["is_live"]() is False
    captured["on_done"]()
    assert dlg._testing_kinds == set()


def test_generate_cards_retires_attachment_timer_and_deletes_dialog(
        anki, monkeypatch, tmp_path):
    path = str(tmp_path / "slow.pdf")
    monkeypatch.setattr(
        QFileDialog, "getOpenFileNames",
        lambda *args, **kwargs: ([path], ""), raising=False)
    started = threading.Event()
    release = threading.Event()

    def extract(source, dest, cancel=None):
        started.set()
        release.wait(timeout=2)
        return {"text": "", "images": [], "images_undecoded": False}

    monkeypatch.setattr(ai_logic, "extract_attachment", extract)
    dlg = _ready_dialog(monkeypatch)
    dlg._attach()
    assert started.wait(timeout=1)
    private_dir = dlg._attach_extract_dir
    dlg.exec = lambda: 0
    monkeypatch.setattr(ai_dialog, "_GenerateDialog", lambda: dlg)

    ai_dialog.generate_cards()

    assert dlg._attach_cancel_flag.is_set()
    assert dlg._attach_timer.started is None
    assert dlg.deleted
    release.set()
    dlg._attach_worker.join(timeout=2)
    deadline = time.monotonic() + 2
    while os.path.exists(private_dir) and time.monotonic() < deadline:
        time.sleep(0.001)
    assert not os.path.exists(private_dir)
