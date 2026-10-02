"""Scan for duplicates at a few hundred pairs, with slow CLI probes, long deck paths and
keyboard use: opening and acting on the list must not rebuild every row, the dialog
must not wait on a CLI probe, and the action links must stay reachable."""
import threading
import time

import harness
import pytest

_PAIRS = 250


def _populate_many(mock, pairs=_PAIRS, deck="Example Shared Deck"):
    import mock_anki
    mock.mw.col = mock_anki.MockCollection()
    mock.mw._config = {}
    col = mock.mw.col
    for i in range(pairs):
        words = f"topicword{i} mechanism{i} receptor{i} clearance{i} threshold{i}"
        col.add_note(f"o{i}", [f"Ours {words}", "answer"], ["InternPearls"],
                     deck="Intern Custom")
        col.add_note(f"t{i}", [f"Theirs {words} variant", "answer"], ["Other"],
                     deck=deck)


def _open(mock):
    from internpearls import dupes_dialog
    dlg = dupes_dialog._DuplicateScanDialog("InternPearls")
    dlg._wait_for_backends()
    dlg._wait_for_scan()
    return dlg


def _show(dlg, width=800, height=560):
    dlg.resize(width, height)
    dlg.show()
    harness.app().processEvents()


def test_many_pairs_open_and_ignore_build_only_a_first_batch():
    mock, _ = harness.bootstrap()
    harness.app()
    harness._ai_backend_available("claude")
    _populate_many(mock)
    start = time.perf_counter()
    dlg = _open(mock)
    _show(dlg)
    opened = time.perf_counter() - start
    assert len(dlg._pairs) == _PAIRS
    lst = dlg._list
    assert lst.shown() < lst.total(), "every pair row was built up front"
    assert opened < 3.0, f"opening {_PAIRS} pairs took {opened:.2f}s"

    start = time.perf_counter()
    dlg._ignore(dlg._pairs[0])
    harness.app().processEvents()
    ignored = time.perf_counter() - start
    assert len(dlg._pairs) == _PAIRS - 1
    assert lst.shown() < lst.total()
    assert ignored < 1.0, f"Ignore at {_PAIRS} pairs took {ignored:.2f}s"
    dlg.close()
    dlg.deleteLater()


def test_ignore_deep_in_the_list_keeps_the_reader_near_the_same_place():
    mock, _ = harness.bootstrap()
    app = harness.app()
    harness._ai_backend_available("claude")
    _populate_many(mock)
    dlg = _open(mock)
    _show(dlg)
    bar = dlg._list.verticalScrollBar()
    for _ in range(6):
        bar.setValue(bar.maximum())
        app.processEvents()
    before = bar.value()
    assert before > 0
    start = time.perf_counter()
    dlg._ignore(dlg._pairs[1])
    app.processEvents()
    app.processEvents()
    elapsed = time.perf_counter() - start
    assert bar.value() > before // 2, "the list jumped back to the top"
    assert elapsed < 2.0, f"Ignore deep in the list took {elapsed:.2f}s"
    dlg.close()
    dlg.deleteLater()


def test_opening_does_not_wait_for_a_backend_probe(monkeypatch):
    from internpearls import ai_cli
    mock, _ = harness.bootstrap()
    harness.app()
    _populate_many(mock, pairs=3)
    release = threading.Event()
    calls = []

    def slow_detect(cfg):
        calls.append(threading.current_thread() is threading.main_thread())
        release.wait(10)
        return {"chosen": "claude", "backends": {"claude": {"path": "/bin/true"}}}

    monkeypatch.setattr(ai_cli, "detect_backends", slow_detect)
    from internpearls import dupes_dialog
    start = time.perf_counter()
    dlg = dupes_dialog._DuplicateScanDialog("InternPearls")
    constructed = time.perf_counter() - start
    try:
        assert constructed < 2.0, f"construction waited {constructed:.2f}s on the probe"
        dlg._wait_for_scan()
        assert dlg._pairs
        assert not dlg.judge_btn.isEnabled()
        assert "Checking" in dlg.judge_btn.toolTip()
    finally:
        release.set()
    dlg._wait_for_backends()
    assert calls == [False], "the probe ran on the main thread"
    assert dlg.judge_btn.isEnabled()
    dlg.deleteLater()


def test_no_backend_leaves_judging_disabled_with_the_setup_hint(monkeypatch):
    from internpearls import ai_cli
    mock, _ = harness.bootstrap()
    harness.app()
    _populate_many(mock, pairs=3)
    monkeypatch.setattr(ai_cli, "detect_backends", lambda cfg: {
        "chosen": None, "backends": {}})
    dlg = _open(mock)
    assert dlg._pairs
    assert not dlg.judge_btn.isEnabled()
    assert "Set up an AI backend" in dlg.judge_btn.toolTip()
    dlg.deleteLater()


def test_backend_answering_after_the_scan_enables_judging(monkeypatch):
    from internpearls import ai_cli, dupes_dialog
    mock, _ = harness.bootstrap()
    harness.app()
    _populate_many(mock, pairs=3)
    release = threading.Event()

    def late_detect(cfg):
        release.wait(10)
        return {"chosen": "claude", "backends": {"claude": {"path": "/bin/true"}}}

    monkeypatch.setattr(ai_cli, "detect_backends", late_detect)
    dlg = dupes_dialog._DuplicateScanDialog("InternPearls")
    dlg._wait_for_scan()
    assert dlg._pairs and not dlg.judge_btn.isEnabled()
    release.set()
    dlg._wait_for_backends()
    assert dlg.judge_btn.isEnabled()
    assert dlg.judge_btn.toolTip() == ""
    dlg.deleteLater()


def test_backend_answering_while_judging_does_not_reenable_the_button(monkeypatch):
    from internpearls import ai_cli
    mock, _ = harness.bootstrap()
    harness.app()
    harness._ai_backend_available("claude")
    _populate_many(mock, pairs=3)
    dlg = _open(mock)
    dlg._judging = True
    dlg._backends_found({"chosen": "claude",
                         "backends": {"claude": {"path": "/bin/true"}}})
    assert not dlg.judge_btn.isEnabled()
    dlg._judging = False
    dlg._sync_judge_button()
    assert dlg.judge_btn.isEnabled()
    dlg.deleteLater()


def test_closing_cancels_the_backend_probe(monkeypatch):
    from internpearls import ai_cli, dupes_dialog
    mock, _ = harness.bootstrap()
    harness.app()
    _populate_many(mock, pairs=2)
    release = threading.Event()
    monkeypatch.setattr(ai_cli, "detect_backends",
                        lambda cfg: release.wait(10) or {"chosen": None, "backends": {}})
    dlg = dupes_dialog._DuplicateScanDialog("InternPearls")
    assert not dlg._probe.cancel_event.is_set()
    dlg._rescan()
    assert not dlg._probe.cancel_event.is_set(), "a rescan must not cancel the probe"
    dlg.reject()
    assert dlg._probe.cancel_event.is_set()
    release.set()
    dlg.deleteLater()


def test_rebuilds_reuse_one_scroll_restore_timer():
    mock, _ = harness.bootstrap()
    harness.app()
    harness._ai_backend_available("claude")
    _populate_many(mock, pairs=60)
    dlg = _open(mock)
    _show(dlg)
    timer = dlg._restore_timer
    bar = dlg._list.verticalScrollBar()
    for i in range(3):
        bar.setValue(bar.maximum())
        harness.app().processEvents()
        dlg._ignore(dlg._pairs[i])
        harness.app().processEvents()
        assert dlg._restore_timer is timer
    dlg.close()
    dlg.deleteLater()


def test_a_contrasting_pair_row_says_what_differs():
    import mock_anki
    from PyQt6.QtWidgets import QLabel
    mock, _ = harness.bootstrap()
    harness.app()
    harness._ai_backend_available("claude")
    mock.mw.col = mock_anki.MockCollection()
    mock.mw._config = {}
    text = ("raises basal metabolic rate and heart rate through nuclear receptor "
            "transcription in most tissues including cardiac muscle liver kidney and "
            "skeletal muscle during prolonged fasting states")
    mock.mw.col.add_note("a", [f"T3 {text}", "x"], ["InternPearls"], deck="Ours")
    mock.mw.col.add_note("b", [f"T4 {text}", "x"], ["Other"], deck="Theirs")
    mock.mw.col.add_note("c", [f"T3 {text} today", "x"], ["Other"], deck="Theirs")
    from internpearls import dupes_dialog, config
    config.set_dupes_threshold(0.4)      # Loose: the only level that offers contrasts
    dlg = _open(mock)
    differs = {p["right"][1][:2]: p["differs"] for p in dlg._pairs}
    assert differs["T4"] == "Differs: T3 vs T4"
    assert differs["T3"] == ""
    _show(dlg)
    shown = {w.text() for row in dlg._list.rows()[::2]
             for w in row.findChildren(QLabel) if "Differs:" in w.text()}
    assert len(shown) == 1 and "Differs: T3 vs T4" in next(iter(shown))
    dlg.close()
    dlg.deleteLater()


def test_long_unbroken_deck_path_wraps_and_keeps_the_action_links_on_screen():
    mock, _ = harness.bootstrap()
    harness.app()
    harness._ai_backend_available("claude")
    deck = "Reference::" + "Extremelylongunbrokensubdeckname" * 8
    _populate_many(mock, pairs=3, deck=deck)
    dlg = _open(mock)
    _show(dlg)
    from PyQt6.QtCore import QPoint
    from PyQt6.QtWidgets import QPushButton
    row = dlg._list.rows()[0]
    links = [b for b in row.findChildren(QPushButton)
             if b.text() in ("Suspend ours", "Suspend theirs", "Keep both",
                             "Ignore pair")]
    assert len(links) == 4
    # The dialog must not have been forced wider than asked, and every link must end
    # inside the list's own viewport rather than behind its clipped edge.
    assert dlg.width() == 800, f"the dialog grew to {dlg.width()}px"
    edge = dlg._list.viewport().mapTo(dlg, QPoint(dlg._list.viewport().width(), 0)).x()
    for b in links:
        right = b.mapTo(dlg, QPoint(b.width(), 0)).x()
        assert right <= edge, f"{b.text()} ends at x={right}, past the list edge {edge}"
    from PyQt6.QtWidgets import QLabel
    primary = next(l for l in row.findChildren(QLabel)
                   if l.text().startswith("<b>ours:</b>"))
    assert primary.minimumSizeHint().width() <= primary.width(), (
        "the deck path cannot wrap, so the label overflows and its text is clipped")
    dlg.close()
    dlg.deleteLater()


def test_link_buttons_show_keyboard_focus_and_disabled_state():
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QPushButton
    mock, _ = harness.bootstrap()
    harness.app()
    harness._ai_backend_available("claude")
    _populate_many(mock, pairs=3)
    dlg = _open(mock)
    _show(dlg)
    dlg.activateWindow()
    harness.app().processEvents()
    row = dlg._list.rows()[0]
    link = next(b for b in row.findChildren(QPushButton) if b.text() == "Ignore pair")
    plain = link.grab().toImage()
    link.setFocus()
    harness.app().processEvents()
    assert link.hasFocus()
    assert link.grab().toImage() != plain, "a focused link looks like an unfocused one"
    assert link.focusPolicy() != Qt.FocusPolicy.NoFocus

    enabled = dlg.judge_btn.grab().toImage()
    dlg.judge_btn.setEnabled(False)
    harness.app().processEvents()
    assert dlg.judge_btn.grab().toImage() != enabled, (
        "a disabled link looks like an enabled one")
    dlg.close()
    dlg.deleteLater()


def test_enter_does_not_rescan_or_default_any_button():
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QPushButton
    mock, _ = harness.bootstrap()
    harness.app()
    harness._ai_backend_available("claude")
    _populate_many(mock, pairs=3)
    dlg = _open(mock)
    _show(dlg)
    assert not any(b.autoDefault() or b.isDefault()
                   for b in dlg.findChildren(QPushButton))
    seq = dlg._scan_seq
    QTest.keyClick(dlg.exclude_edit, Qt.Key.Key_Return)
    QTest.keyClick(dlg, Qt.Key.Key_Return)
    harness.app().processEvents()
    assert dlg._scan_seq == seq
    assert dlg.isVisible()
    dlg.close()
    dlg.deleteLater()


def test_rescan_link_confirms_before_discarding_judged_results(monkeypatch):
    from internpearls import dupes_dialog
    mock, _ = harness.bootstrap()
    harness.app()
    harness._ai_backend_available("claude")
    _populate_many(mock, pairs=3)
    dlg = _open(mock)
    asked = []
    answers = [False, True]
    monkeypatch.setattr(dupes_dialog, "_ask",
                        lambda text, **kw: asked.append(text) or answers.pop(0))
    dlg._rescan_fresh()
    assert not asked, "nothing is judged yet, so nothing is lost"
    seq = dlg._scan_seq
    assert seq > 1
    dlg._wait_for_scan()
    dlg._pairs[0]["judged"] = "same"
    seq = dlg._scan_seq
    dlg._rescan_fresh()
    assert len(asked) == 1 and dlg._scan_seq == seq
    assert dlg._pairs[0]["judged"] == "same"
    dlg._rescan_fresh()
    assert len(asked) == 2 and dlg._scan_seq == seq + 1
    dlg.deleteLater()
