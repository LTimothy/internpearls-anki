"""Real-Qt render checks for Scan for duplicates: three candidate rows, light and dark."""
import os
import shutil
import threading
from pathlib import Path

import harness
import pytest
from internpearls import palette as colors_module


def _populate(mock):
    import mock_anki
    mock.mw.col = mock_anki.MockCollection()   # a fresh collection per test
    mock.mw._config = {}
    col = mock.mw.col
    col.add_note("g1", ["Fenoldopam mechanism of action, selective D1 receptor agonist",
                        "Acts on renal dopamine receptors"],
                 ["InternPearls"], deck="Intern Custom")
    col.add_note("g2", ["Fenoldopam is a selective D1 receptor agonist drug",
                        "Acts on renal dopamine receptors in hypertensive emergencies"],
                 ["Other"], deck="Ankisthesia")

    col.add_note("g3", ["Ketamine induction dose one to two mg per kilogram IV",
                        "Dissociative anesthetic"],
                 ["InternPearls"], deck="Intern Custom")
    col.add_note("g4", ["The induction dose of ketamine is one to two mg per kilogram",
                        "Given intravenously"],
                 ["Other"], deck="Ankisthesia")

    col.add_note("g5", ["Metoclopramide increases lower esophageal sphincter tone",
                        "Promotes gastric emptying"],
                 ["InternPearls"], deck="Intern Custom")
    col.add_note("g6", ["Metoclopramide increases gastroesophageal sphincter tone",
                        "Promotes gastric emptying as a prokinetic agent"],
                 ["Other"], deck="Ankisthesia")


def _build_dialog(mock):
    from internpearls import dupes_dialog
    dlg = dupes_dialog._DuplicateScanDialog("InternPearls")
    dlg._wait_for_scan()
    return dlg


def _populate_with_reference_deck(mock):
    """The base three pairs, plus a fourth candidate whose other side sits in a
    reference deck the learner wants to exclude from the comparison."""
    _populate(mock)
    col = mock.mw.col
    col.add_note("g7", ["Sugammadex reverses rocuronium blockade by encapsulation",
                        "Selectively binds rocuronium and vecuronium"],
                 ["InternPearls"], deck="Intern Custom")
    col.add_note("g8", ["Sugammadex reverses rocuronium blockade through encapsulation",
                        "Chelates rocuronium and vecuronium molecules"],
                 ["Other"], deck="Reference decks")


def test_duplicate_scan_finds_three_candidates():
    mock, _ = harness.bootstrap()
    harness.app()
    _populate(mock)
    dlg = _build_dialog(mock)
    assert len(dlg._pairs) == 3
    assert dlg._left_count == 3
    assert dlg._right_count == 3
    assert "3 scanned against 3" in dlg.summary_label.text()
    assert "3 candidates" in dlg.summary_label.text()
    dlg.deleteLater()


def test_duplicate_scan_renders_light_and_dark(tmp_path):
    mock, _ = harness.bootstrap()
    harness.app()
    _populate(mock)

    out_dir = os.environ.get("IP_SHOT_DIR") or str(tmp_path)
    os.makedirs(out_dir, exist_ok=True)
    saved = []
    for theme, fname in (("light", "dupes-scan-light.png"),
                         ("dark", "dupes-scan-dark.png")):
        harness.apply_theme(theme)
        dlg = _build_dialog(mock)
        dlg.exclude_edit.setText("Reference decks")
        dlg.resize(800, 560)
        dlg.show()
        harness.app().processEvents()
        png = os.path.join(out_dir, fname)
        dlg.grab().toImage().save(png, "PNG")
        saved.append(png)
        dlg.close()
        dlg.deleteLater()
    for png in saved:
        assert os.path.exists(png)
    print("Duplicate scan PNGs:", saved)


def test_ignore_pair_persists_and_removes_row():
    mock, _ = harness.bootstrap()
    harness.app()
    _populate(mock)
    dlg = _build_dialog(mock)
    from internpearls.config import _cfg
    n = len(dlg._pairs)
    pair = dlg._pairs[0]
    key = pair["key"]
    dlg._ignore(pair)
    assert len(dlg._pairs) == n - 1
    assert key in _cfg()["dupes_ignored"]
    dlg.deleteLater()


def test_ignored_top_results_are_replenished_by_later_candidates():
    import mock_anki
    mock, _ = harness.bootstrap()
    harness.app()
    mock.mw.col = mock_anki.MockCollection()
    mock.mw._config = {}
    col = mock.mw.col
    col.add_note("ours", ["alpha beta gamma", "delta"], ["InternPearls"],
                 deck="Ours")
    for i in range(5):
        col.add_note(f"theirs{i}", ["alpha beta gamma", "delta"], ["Other"],
                     deck="Other")

    dlg = _build_dialog(mock)
    assert len(dlg._pairs) == 3
    for pair in list(dlg._pairs):
        dlg._ignore(pair)
    dlg._rescan()
    dlg._wait_for_scan()

    assert len(dlg._pairs) == 2
    dlg.deleteLater()


def test_suspend_marks_row_and_calls_scheduler():
    mock, _ = harness.bootstrap()
    harness.app()
    _populate(mock)
    dlg = _build_dialog(mock)
    pair = dlg._pairs[0]
    left_nid = pair["left"][0]
    cid = mock.mw.col.get_note(left_nid).card_ids()[0]
    dlg._suspend(pair, "left")
    assert mock.mw.col.get_card(cid).queue == -1
    assert "left" in pair["suspended"]
    dlg.deleteLater()


def test_rescan_reads_both_suspensions_and_offers_explicit_recovery():
    from internpearls import dupes_dialog
    mock, q = harness.bootstrap()
    harness.app()
    _populate(mock)
    dlg = _build_dialog(mock)
    original = dlg._pairs[0]
    key = original["key"]
    left_cid = mock.mw.col.get_note(original["left"][0]).card_ids()[0]
    right_cid = mock.mw.col.get_note(original["right"][0]).card_ids()[0]

    # Simulate suspension through Anki/Browse rather than through this dialog, then
    # rescan. The row must resolve the collection's state instead of remembering only
    # clicks made in this dialog instance.
    mock.mw.col.sched.suspend_cards([left_cid, right_cid])
    dlg._rescan()
    dlg._wait_for_scan()
    pair = next(p for p in dlg._pairs if p["key"] == key)
    assert pair["suspended"] == {"left", "right"}
    assert dupes_dialog._chip_label(pair, dlg._min_shared)[0] == "BOTH SUSPENDED"

    row = dlg._build_row(pair)
    buttons = {button.text(): button for button in row.findChildren(q.QPushButton)}
    assert {"Unsuspend ours", "Unsuspend theirs", "Keep both"} <= buttons.keys()
    buttons["Keep both"].click()
    assert mock.mw.col.get_card(left_cid).queue == -1
    assert mock.mw.col.get_card(right_cid).queue == -1

    buttons["Unsuspend theirs"].click()
    assert mock.mw.col.get_card(right_cid).queue != -1
    assert pair["suspended"] == {"left"}
    assert dupes_dialog._chip_label(pair, dlg._min_shared)[0] == "OURS SUSPENDED"

    refreshed = dlg._build_row(pair)
    buttons = {button.text(): button for button in refreshed.findChildren(q.QPushButton)}
    buttons["Unsuspend ours"].click()
    assert mock.mw.col.get_card(left_cid).queue != -1
    assert pair["suspended"] == set()
    row.deleteLater()
    refreshed.deleteLater()
    dlg.deleteLater()


def test_rescan_marks_partly_suspended_cloze_and_unsuspends_all_siblings():
    from internpearls import dupes_dialog
    import mock_anki

    mock, q = harness.bootstrap()
    harness.app()
    _populate(mock)
    dlg = _build_dialog(mock)
    original = dlg._pairs[0]
    key = original["key"]
    note = mock.mw.col.get_note(original["left"][0])

    # A two-deletion cloze has two sibling cards. Suspending only one outside this
    # dialog must not make the note look fully suspended, while recovery remains the
    # deliberately note-level operation used by suspend_notes/unsuspend_notes.
    note.model = mock_anki.make_model(
        "Study Deck - Cloze", qfmt="{{cloze:Front}}", afmt="{{cloze:Front}}")
    note.fields[0] = ("{{c1::Fenoldopam}} mechanism of action, selective "
                      "{{c2::D1 receptor agonist}}")
    mock.mw.col._generate_cloze_cards(note)
    card_ids = note.card_ids()
    assert len(card_ids) == 2
    mock.mw.col.sched.suspend_cards([card_ids[0]])

    dlg._rescan()
    dlg._wait_for_scan()
    pair = next(p for p in dlg._pairs if p["key"] == key)
    assert pair["suspended"] == set()
    assert pair["partly_suspended"] == {"left"}
    assert dupes_dialog._chip_label(pair, dlg._min_shared)[0] == \
        "OURS PARTLY SUSPENDED"

    row = dlg._build_row(pair)
    buttons = {button.text(): button for button in row.findChildren(q.QPushButton)}
    action = buttons["Unsuspend ours"]
    assert action.toolTip() == action.accessibleName()
    assert "1 of 2 currently suspended" in action.accessibleName()
    action.click()

    assert all(mock.mw.col.get_card(cid).queue != -1 for cid in card_ids)
    assert pair["suspended"] == set()
    assert pair["partly_suspended"] == set()
    row.deleteLater()
    dlg.deleteLater()


def test_copy_list_puts_pairs_on_clipboard():
    mock, _ = harness.bootstrap()
    harness.app()
    _populate(mock)
    dlg = _build_dialog(mock)
    dlg._copy_list()
    from PyQt6.QtWidgets import QApplication
    text = QApplication.clipboard().text()
    assert "|" in text
    dlg.deleteLater()


def test_judge_button_disabled_without_backend():
    mock, _ = harness.bootstrap()
    harness.app()
    from internpearls import ai_cli
    ai_cli.find_cli = lambda kind, override="": None
    _populate(mock)
    dlg = _build_dialog(mock)
    assert not dlg.judge_btn.isEnabled()
    dlg.deleteLater()


def test_judge_with_ai_updates_chips_and_folds_different(monkeypatch):
    import json as _json
    mock, _ = harness.bootstrap()
    harness.app()
    harness._ai_backend_available("claude")
    _populate(mock)
    dlg = _build_dialog(mock)
    assert dlg.judge_btn.isEnabled()

    reply = _json.dumps({"verdicts": [
        {"pair": 0, "verdict": "same", "note": "same fact, different wording"},
        {"pair": 1, "verdict": "overlaps", "note": "shares part of the fact"},
        {"pair": 2, "verdict": "different", "note": "not the same fact"},
    ]})

    from internpearls import ai_cli

    scratch_paths = []

    def fake_run_generation(kind, path, prompt, mode, scratch, **kw):
        assert "note_id" not in prompt.lower()
        scratch_paths.append(scratch)
        return {"text": reply, "tokens": 10, "rate_limits": None, "duration_s": 0.1}
    monkeypatch.setattr(ai_cli, "run_generation", fake_run_generation)

    dlg._judge_with_ai()
    dlg._wait_for_judge()

    judged = {p["key"]: p["judged"] for p in dlg._pairs}
    assert "same" in judged.values()
    assert "overlaps" in judged.values()
    assert "different" in judged.values()
    # a "different" pair sits under the fold, not among the shown rows
    shown = [p for p in dlg._pairs if p["judged"] != "different"]
    assert len(shown) == 2
    assert len(scratch_paths) == 1
    assert not Path(scratch_paths[0]).exists()
    dlg.deleteLater()


def test_judge_close_cancels_worker_stops_timer_and_cleans_scratch(monkeypatch):
    from internpearls import ai_cli
    mock, _ = harness.bootstrap()
    harness.app()
    harness._ai_backend_available("claude")
    _populate(mock)
    dlg = _build_dialog(mock)
    entered = threading.Event()
    release = threading.Event()
    call = {}

    def fake_run_generation(kind, path, prompt, mode, scratch, **kw):
        call.update(scratch=scratch, cancel=kw.get("cancel"))
        entered.set()
        release.wait(5)
        if call["cancel"] and call["cancel"]():
            raise ai_cli.GenerationCancelled()
        return {"text": "{}"}

    monkeypatch.setattr(ai_cli, "run_generation", fake_run_generation)
    dlg._judge_with_ai()
    assert entered.wait(2)
    close_button = next(button for button in dlg.findChildren(type(dlg.judge_btn))
                        if button.text() == "Close")
    close_button.click()
    release.set()
    dlg._judge_worker.join(2)

    try:
        assert call["cancel"] is not None
        assert call["cancel"]()
        assert not dlg._judge_timer.isActive()
        assert not dlg._judge_worker.is_alive()
        assert not Path(call["scratch"]).exists()
    finally:
        release.set()
        dlg._judge_worker.join(2)
        shutil.rmtree(call["scratch"], ignore_errors=True)
        dlg.deleteLater()


def test_judge_accept_cancels_worker_and_cleans_scratch(monkeypatch):
    from internpearls import ai_cli
    mock, _ = harness.bootstrap()
    harness.app()
    harness._ai_backend_available("claude")
    _populate(mock)
    dlg = _build_dialog(mock)
    entered = threading.Event()
    release = threading.Event()
    call = {}

    def fake_run_generation(kind, path, prompt, mode, scratch, **kw):
        call.update(scratch=scratch, cancel=kw.get("cancel"))
        entered.set()
        release.wait(5)
        if call["cancel"] and call["cancel"]():
            raise ai_cli.GenerationCancelled()
        return {"text": "{}"}

    monkeypatch.setattr(ai_cli, "run_generation", fake_run_generation)
    dlg._judge_with_ai()
    assert entered.wait(2)
    dlg.accept()
    release.set()
    dlg._judge_worker.join(2)

    try:
        assert call["cancel"] is not None
        assert call["cancel"]()
        assert not dlg._judge_timer.isActive()
        assert not Path(call["scratch"]).exists()
    finally:
        release.set()
        dlg._judge_worker.join(2)
        shutil.rmtree(call["scratch"], ignore_errors=True)
        dlg.deleteLater()


def test_rescan_cancels_and_retires_a_late_judge_result(monkeypatch):
    import json
    from PyQt6.QtTest import QTest
    from internpearls import ai_cli
    mock, _ = harness.bootstrap()
    harness.app()
    harness._ai_backend_available("claude")
    _populate(mock)
    dlg = _build_dialog(mock)
    old_pairs = list(dlg._pairs)
    entered = threading.Event()
    release = threading.Event()
    call = {}
    reply = json.dumps({"verdicts": [
        {"pair": i, "verdict": "same", "note": "stale result"}
        for i in range(len(old_pairs))
    ]})

    def fake_run_generation(kind, path, prompt, mode, scratch, **kw):
        call.update(scratch=scratch, cancel=kw.get("cancel"))
        entered.set()
        release.wait(5)
        # Deliberately return a result even after cancellation, to verify the dialog's
        # own generation guard rather than relying on cooperative backend behavior.
        return {"text": reply}

    monkeypatch.setattr(ai_cli, "run_generation", fake_run_generation)
    dlg._judge_with_ai()
    assert entered.wait(2)
    dlg._rescan()
    dlg._wait_for_scan()
    current_pairs = list(dlg._pairs)
    summary = dlg.summary_label.text()
    release.set()
    dlg._judge_worker.join(2)
    QTest.qWait(300)

    try:
        assert call["cancel"] is not None
        assert call["cancel"]()
        assert not dlg._judge_timer.isActive()
        assert all(pair["judged"] is None for pair in old_pairs)
        assert all(pair["judged"] is None for pair in current_pairs)
        assert dlg.summary_label.text() == summary
        assert not Path(call["scratch"]).exists()
    finally:
        release.set()
        dlg._judge_worker.join(2)
        shutil.rmtree(call["scratch"], ignore_errors=True)
        dlg.deleteLater()


def test_judge_error_cleans_scratch(monkeypatch):
    from internpearls import ai_cli
    mock, _ = harness.bootstrap()
    harness.app()
    harness._ai_backend_available("claude")
    _populate(mock)
    dlg = _build_dialog(mock)
    scratch_paths = []

    def fake_run_generation(kind, path, prompt, mode, scratch, **kw):
        scratch_paths.append(scratch)
        raise ai_cli.GenerationCancelled()

    monkeypatch.setattr(ai_cli, "run_generation", fake_run_generation)
    dlg._judge_with_ai()
    dlg._wait_for_judge()

    try:
        assert len(scratch_paths) == 1
        assert not Path(scratch_paths[0]).exists()
    finally:
        shutil.rmtree(scratch_paths[0], ignore_errors=True)
        dlg.deleteLater()


def test_exclude_decks_empty_by_default_excludes_nothing():
    mock, _ = harness.bootstrap()
    harness.app()
    _populate_with_reference_deck(mock)
    dlg = _build_dialog(mock)
    assert dlg.exclude_edit.text() == ""
    fronts = {p["right"][1] for p in dlg._pairs}
    assert any("Sugammadex" in f for f in fronts)
    assert "Excluding" not in dlg.summary_label.text()
    dlg.deleteLater()


def test_exclude_decks_by_substring():
    mock, _ = harness.bootstrap()
    harness.app()
    _populate_with_reference_deck(mock)
    dlg = _build_dialog(mock)
    dlg.exclude_edit.setText("Reference")
    dlg._exclude_edited()
    dlg._wait_for_scan()
    fronts = {p["right"][1] for p in dlg._pairs}
    assert not any("Sugammadex" in f for f in fronts)
    assert "Excluding 1 deck" in dlg.summary_label.text()
    dlg.deleteLater()


def test_exclude_decks_case_insensitive():
    mock, _ = harness.bootstrap()
    harness.app()
    _populate_with_reference_deck(mock)
    dlg = _build_dialog(mock)
    dlg.exclude_edit.setText("reference decks")
    dlg._exclude_edited()
    dlg._wait_for_scan()
    fronts = {p["right"][1] for p in dlg._pairs}
    assert not any("Sugammadex" in f for f in fronts)
    dlg.deleteLater()


def test_exclude_decks_persisted_across_dialog_reopen():
    mock, _ = harness.bootstrap()
    harness.app()
    _populate_with_reference_deck(mock)
    dlg = _build_dialog(mock)
    dlg.exclude_edit.setText("Reference decks")
    dlg._exclude_edited()
    dlg._wait_for_scan()
    dlg.deleteLater()

    dlg2 = _build_dialog(mock)
    assert dlg2.exclude_edit.text() == "Reference decks"
    fronts = {p["right"][1] for p in dlg2._pairs}
    assert not any("Sugammadex" in f for f in fronts)
    dlg2.deleteLater()


def test_row_actions_are_visible_while_collapsed():
    mock, _ = harness.bootstrap()
    harness.app()
    _populate(mock)
    dlg = _build_dialog(mock)
    row = dlg._rows_layout.itemAt(0).widget()
    header = row.layout().itemAt(0).widget()
    body = row.layout().itemAt(1).widget()
    assert not body.isVisible()
    labels = {w.text() for w in header.findChildren(type(dlg.judge_btn))}
    assert {"Suspend ours", "Suspend theirs", "Keep both", "Ignore pair"} <= labels
    dlg.deleteLater()


def test_scope_controls_have_distinct_accessible_names():
    mock, _ = harness.bootstrap()
    harness.app()
    _populate(mock)
    dlg = _build_dialog(mock)

    left_name = dlg.left_combo.accessibleName().lower()
    right_name = dlg.right_combo.accessibleName().lower()
    assert "scan" in left_name
    assert "compare" in right_name
    assert left_name != right_name
    dlg.deleteLater()


def test_caret_accessible_name_identifies_pair_and_expand_state():
    from internpearls import dupes_dialog
    mock, q = harness.bootstrap()
    harness.app()
    _populate(mock)
    dlg = _build_dialog(mock)
    dlg.show()
    harness.app().processEvents()
    pair = dlg._pairs[0]
    left_front, _ = dupes_dialog._note_texts(pair["left"][0])
    right_front, _ = dupes_dialog._note_texts(pair["right"][0])
    row = dlg._rows_layout.itemAt(0).widget()
    header = row.layout().itemAt(0).widget()
    body = row.layout().itemAt(1).widget()
    caret = header.layout().itemAt(0).widget()

    assert caret.accessibleName().startswith("Expand duplicate pair:")
    assert left_front in caret.accessibleName()
    assert right_front in caret.accessibleName()
    caret.click()
    assert body.isVisible()
    assert caret.accessibleName().startswith("Collapse duplicate pair:")

    keep_both = next(button for button in row.findChildren(q.QPushButton)
                     if button.text() == "Keep both")
    keep_both.click()
    assert not body.isVisible()
    assert caret.accessibleName().startswith("Expand duplicate pair:")
    dlg.deleteLater()


def test_ours_label_starts_at_row_text_indent():
    from internpearls import dupes_dialog
    mock, q = harness.bootstrap()
    harness.app()
    _populate(mock)
    dlg = _build_dialog(mock)
    dlg.resize(800, 560)
    dlg.show()
    harness.app().processEvents()
    row = dlg._rows_layout.itemAt(0).widget()
    header = row.layout().itemAt(0).widget()
    primary = header.layout().itemAt(2).widget()
    x = primary.mapTo(row, q.QPoint(0, 0)).x()
    assert x == dupes_dialog._row_text_indent(), (
        f"the ours: label starts at x={x}, not this window's own row_text_indent "
        f"({dupes_dialog._row_text_indent()}): the chip column and the body indent "
        "have drifted apart")
    dlg.close()
    dlg.deleteLater()


def test_collapsed_row_height_matches_content():
    from PyQt6.QtGui import QFontMetrics
    mock, _ = harness.bootstrap()
    harness.app()
    _populate(mock)
    dlg = _build_dialog(mock)
    dlg.resize(800, 560)
    dlg.show()
    harness.app().processEvents()
    row = dlg._rows_layout.itemAt(0).widget()
    fm = QFontMetrics(row.font())
    # Four text lines' worth of height (the "ours:"/"theirs:"/"shares:" trio plus
    # headroom for wrapping), plus the row's own outer margins, is generous slack for
    # a collapsed row's real content; anything past it means the row is sizing to
    # something other than what it actually shows.
    limit = fm.height() * 4 + 20
    assert row.height() < limit, (
        f"a collapsed row is {row.height()}px tall, past {limit}px (3 text lines "
        "plus margins): it is not sizing to its own content")
    dlg.close()
    dlg.deleteLater()


def test_link_slots_accept_the_clicked_flag(monkeypatch):
    """A flat link is a real QPushButton, so its clicked signal hands the slot a
    checked flag; the error guard's wrapper forwards it as-is."""
    mock, _ = harness.bootstrap()
    _populate(mock)
    dlg = _build_dialog(mock)
    dlg._rescan(False)
    dlg._wait_for_scan()
    assert "3 candidates" in dlg.summary_label.text()
    dlg._toggle_fold(False)
    dlg._copy_list(False)


def test_scroll_area_scrollbar_policy_is_fixed():
    """Always-on vertical, always-off horizontal, so the bar can't appear and
    disappear underfoot as word-wrapped rows reflow."""
    from PyQt6.QtCore import Qt as _Qt
    mock, _ = harness.bootstrap()
    harness.app()
    _populate(mock)
    dlg = _build_dialog(mock)
    from PyQt6.QtWidgets import QScrollArea
    scroll = dlg.findChild(QScrollArea)
    assert scroll.verticalScrollBarPolicy() == _Qt.ScrollBarPolicy.ScrollBarAlwaysOn
    assert scroll.horizontalScrollBarPolicy() == _Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    dlg.deleteLater()


def test_exclude_edited_does_not_rescan_on_unchanged_text():
    """editingFinished fires on any focus loss. A second one with the same text
    (clicking around the list after typing, say) must not trigger another rescan."""
    mock, _ = harness.bootstrap()
    harness.app()
    _populate_with_reference_deck(mock)
    dlg = _build_dialog(mock)

    calls = []
    real_rescan = dlg._rescan

    def spy(*a):
        calls.append(a)
        return real_rescan(*a)
    dlg._rescan = spy

    dlg.exclude_edit.setText("Reference decks")
    dlg._exclude_edited()
    dlg._wait_for_scan()
    assert len(calls) == 1

    dlg._exclude_edited()   # same text again: no second rescan
    assert len(calls) == 1
    dlg.deleteLater()


def test_exclusion_feedback_reports_deck_and_card_count():
    mock, _ = harness.bootstrap()
    harness.app()
    _populate_with_reference_deck(mock)
    dlg = _build_dialog(mock)
    dlg.exclude_edit.setText("Reference decks")
    dlg._exclude_edited()
    dlg._wait_for_scan()
    text = dlg.summary_label.text()
    assert "Excluding 1 deck (1 card)" in text
    dlg.deleteLater()


def test_exclusion_feedback_warns_on_unmatched_entry():
    mock, _ = harness.bootstrap()
    harness.app()
    _populate_with_reference_deck(mock)
    dlg = _build_dialog(mock)
    dlg.exclude_edit.setText("CC Anki")
    dlg._exclude_edited()
    dlg._wait_for_scan()
    text = dlg.summary_label.text()
    assert "No deck matches 'CC Anki'" in text
    c = colors_module.colors()
    assert c["warning"] in text
    dlg.deleteLater()


def test_exclusion_feedback_escapes_entry_text():
    mock, _ = harness.bootstrap()
    harness.app()
    _populate_with_reference_deck(mock)
    dlg = _build_dialog(mock)
    dlg.exclude_edit.setText("<script>")
    dlg._exclude_edited()
    dlg._wait_for_scan()
    text = dlg.summary_label.text()
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    dlg.deleteLater()


def test_score_band_labels():
    from internpearls.dupes_dialog import _band_label
    # With an evidence floor in effect (Strict and Normal both min_shared=2),
    # the band is decided by shared-token count, not the raw score: one token past
    # the floor is "Likely duplicate", right at the floor is "Possible".
    assert _band_label(0.7, shared_count=3, min_shared=2) == "Likely duplicate 0.70"
    assert _band_label(0.7, shared_count=2, min_shared=2) == "Possible 0.70"
    # Loose (min_shared falsy) has no evidence floor to grade against, so it falls
    # back to the original score-only bands.
    assert _band_label(0.7, shared_count=1, min_shared=0) == "Likely duplicate 0.70"
    assert _band_label(0.85, shared_count=1, min_shared=0) == "Likely duplicate 0.85"
    assert _band_label(0.6, shared_count=1, min_shared=0) == "Similar 0.60"
    assert _band_label(0.65, shared_count=1, min_shared=0) == "Similar 0.65"
    assert _band_label(0.5, shared_count=1, min_shared=0) == "Weak match 0.50"
    assert _band_label(0.55, shared_count=1, min_shared=0) == "Weak match 0.55"


def test_row_shows_shares_line():
    mock, _ = harness.bootstrap()
    harness.app()
    _populate(mock)
    dlg = _build_dialog(mock)
    pair = dlg._pairs[0]
    assert pair["shares"]
    row = dlg._rows_layout.itemAt(0).widget()
    header = row.layout().itemAt(0).widget()
    primary = header.layout().itemAt(2).widget()
    assert "shares:" in primary.text()
    dlg.deleteLater()


def test_sensitivity_combo_defaults_to_normal_and_persists():
    from internpearls.config import _cfg
    mock, _ = harness.bootstrap()
    harness.app()
    _populate(mock)
    dlg = _build_dialog(mock)
    assert dlg.sensitivity_combo.currentText() == "Normal"
    assert _cfg()["dupes_threshold"] == 0.5

    dlg.sensitivity_combo.setCurrentIndex(0)   # Strict
    dlg._wait_for_scan()
    assert _cfg()["dupes_threshold"] == 0.6
    dlg.deleteLater()

    dlg2 = _build_dialog(mock)
    assert dlg2.sensitivity_combo.currentText() == "Strict"
    dlg2.deleteLater()


def _populate_with_left_side_reference_deck(mock):
    """The base three pairs, plus a fourth candidate whose *left* (add-on-managed)
    side sits in a reference deck the learner wants to exclude from the comparison,
    exercising exclusion on the side that isn't a single pinned deck."""
    _populate(mock)
    col = mock.mw.col
    col.add_note("g7", ["Sugammadex reverses rocuronium blockade by encapsulation",
                        "Selectively binds rocuronium and vecuronium"],
                 ["InternPearls"], deck="Reference decks")
    col.add_note("g8", ["Sugammadex reverses rocuronium blockade through encapsulation",
                        "Chelates rocuronium and vecuronium molecules"],
                 ["Other"], deck="Ankisthesia")


def test_exclude_decks_applies_to_the_left_side_too():
    """Exclusion used to only apply to the left side when it was pinned to a single
    deck; with the left combo on its default ("Cards this add-on manages"), an
    excluded deck's own add-on-managed cards must still drop out of the comparison."""
    mock, _ = harness.bootstrap()
    harness.app()
    _populate_with_left_side_reference_deck(mock)
    dlg = _build_dialog(mock)
    dlg.exclude_edit.setText("Reference decks")
    dlg._exclude_edited()
    dlg._wait_for_scan()
    fronts = {p["left"][1] for p in dlg._pairs}
    assert not any("Sugammadex" in f for f in fronts)
    assert dlg._left_count == 3
    text = dlg.summary_label.text()
    assert "Excluding 1 deck (1 card)" in text
    dlg.deleteLater()


def test_sensitivity_strict_drops_a_single_shared_word_pair():
    """A pair sharing only one rare word in a tiny pool used to score high enough to
    show up as 'Likely duplicate' on a single shared word; Strict's evidence floor
    drops it, Loose (the raw threshold) still shows it."""
    mock, _ = harness.bootstrap()
    harness.app()
    import mock_anki
    mock.mw.col = mock_anki.MockCollection()
    mock.mw._config = {}
    col = mock.mw.col
    col.add_note("w1", ["Phenylephrine phenylephrine bolus", ""],
                 ["InternPearls"], deck="Intern Custom")
    col.add_note("w2", ["Phenylephrine phenylephrine allergy", ""],
                 ["Other"], deck="Ankisthesia")

    dlg = _build_dialog(mock)
    dlg.sensitivity_combo.setCurrentIndex(0)   # Strict
    dlg._wait_for_scan()
    assert dlg._pairs == []

    dlg.sensitivity_combo.setCurrentIndex(2)   # Loose
    dlg._wait_for_scan()
    assert len(dlg._pairs) == 1
    dlg.deleteLater()


def test_sensitivity_normal_requires_two_shared_informative_words():
    import mock_anki
    mock, _ = harness.bootstrap()
    harness.app()
    mock.mw.col = mock_anki.MockCollection()
    mock.mw._config = {}
    col = mock.mw.col
    col.add_note("w1", ["orchid amber", "Answer"], ["InternPearls"], deck="Ours")
    col.add_note("w2", ["orchid cobalt", "Answer"], ["Other"], deck="Theirs")

    dlg = _build_dialog(mock)
    assert dlg.sensitivity_combo.currentText() == "Normal"
    assert dlg._pairs == []
    dlg.deleteLater()


def test_thin_pool_warns_when_a_side_has_fewer_than_50_cards():
    mock, _ = harness.bootstrap()
    harness.app()
    _populate(mock)
    dlg = _build_dialog(mock)
    text = dlg.summary_label.text()
    assert "Cards this add-on manages has only 3 cards; pick a deck to compare " \
           "against" in text
    assert "Everything else has only 3 cards; pick a deck to compare against" in text
    dlg.deleteLater()


def test_picture_only_front_is_named_not_blank():
    mock, _ = harness.bootstrap()
    _populate(mock)
    col = mock.mw.col
    col.add_note("g7", ['<img src="landmarks.png">', "The interscalene groove"],
                 ["InternPearls"], deck="Intern Custom")
    from internpearls import dupes_dialog
    nid = next(n.id for n in col._notes.values() if n.guid == "g7")
    front, back = dupes_dialog._note_texts(nid)
    assert "landmarks.png" in front
    assert back == "The interscalene groove"


def test_right_side_offers_the_other_cards_under_the_deck_root():
    """Jessica's collection keeps two filtered decks of other people's cards under the
    Intern Pearls deck. The second right-hand option compares our cards against just
    those, ignoring the rest of her collection."""
    mock, _ = harness.bootstrap()
    harness.app()
    _populate(mock)
    col = mock.mw.col
    col.add_note("g9", ["Fenoldopam mechanism of action, selective D1 receptor agonist",
                        "Acts on renal dopamine receptors"],
                 ["Other"], deck="Intern Pearls::Intern goodies")
    dlg = _build_dialog(mock)
    assert dlg.right_combo.itemText(1) == "Other cards under Intern Pearls"
    assert dlg._right_count == 4          # everything else: g2, g4, g6, g9
    dlg.right_combo.setCurrentIndex(1)
    dlg._wait_for_scan()
    assert dlg._right_count == 1          # only g9 sits under the root
    assert {p["right"][0] for p in dlg._pairs} == {col.find_notes('deck:"Intern Pearls"')[0]}
    dlg.close()


@pytest.mark.parametrize("changed_side", ["left", "right"])
def test_scope_change_immediately_invalidates_actions_and_rescans(monkeypatch,
                                                                  changed_side):
    import mock_anki
    from internpearls import dupes_dialog
    mock, _ = harness.bootstrap()
    harness.app()
    mock.mw.col = mock_anki.MockCollection()
    mock.mw._config = {}
    col = mock.mw.col
    col.add_note("ours", ["alpha beta gamma", "delta"], ["InternPearls"],
                 deck="Ours")
    col.add_note("theirs", ["alpha beta gamma", "delta"], ["Other"],
                 deck="Reference A")
    col.add_note("third", ["unrelated astronomy constellation", "nebula"],
                 ["Other"], deck="Reference B")
    dlg = _build_dialog(mock)
    assert len(dlg._pairs) == 1
    old_seq = dlg._scan_seq

    entered = threading.Event()
    release = threading.Event()
    real_find = dupes_dialog.find_candidates

    def gated_find(*args, **kwargs):
        entered.set()
        release.wait(5)
        return real_find(*args, **kwargs)

    monkeypatch.setattr(dupes_dialog, "find_candidates", gated_find)
    deck_index = dlg._deck_names.index("Reference B")
    if changed_side == "left":
        dlg.left_combo.setCurrentIndex(deck_index + 1)
    else:
        dlg.right_combo.setCurrentIndex(len(dlg._right_fixed) + deck_index)

    try:
        assert entered.wait(2)
        assert dlg._scan_seq == old_seq + 1
        assert dlg._pairs == []
        assert dlg.summary_label.text().startswith("Scanning")
        release.set()
        dlg._wait_for_scan()
        assert dlg._pairs == []
    finally:
        release.set()
        dlg._worker.join(2)
        dlg.deleteLater()


def test_sensitivity_and_exclusion_changes_reuse_the_cached_rows(monkeypatch):
    """Reading the collection is the slow part of a scan, so it happens once per
    dialog; only Rescan reads it again."""
    mock, _ = harness.bootstrap()
    harness.app()
    _populate(mock)
    from internpearls import dupes_dialog
    calls = []
    real = dupes_dialog.note_rows
    monkeypatch.setattr(dupes_dialog, "note_rows",
                        lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    dlg = _build_dialog(mock)
    assert len(calls) == 1
    dlg.sensitivity_combo.setCurrentIndex(0)
    dlg._wait_for_scan()
    dlg.exclude_edit.setText("Reference")
    dlg._exclude_edited()
    dlg._wait_for_scan()
    assert len(calls) == 1
    dlg._rescan_fresh()
    dlg._wait_for_scan()
    assert len(calls) == 2
    dlg.close()


def test_the_same_deck_on_both_sides_never_pairs_a_note_with_itself():
    """A deck named on the right used to skip the subtraction the fixed scopes
    do, so choosing one deck on both sides scored every note against itself at
    a perfect 1.00, and both Suspend links on such a row pointed at one note."""
    mock, _ = harness.bootstrap()
    harness.app()
    _populate(mock)
    dlg = _build_dialog(mock)
    deck = "Ankisthesia"
    dlg.left_combo.setCurrentIndex(dlg._deck_names.index(deck) + 1)
    dlg.right_combo.setCurrentIndex(
        len(dlg._right_fixed) + dlg._deck_names.index(deck))
    dlg._rescan()
    dlg._wait_for_scan()
    assert dlg._left_count == 3
    assert dlg._right_count == 0
    assert dlg._pairs == []
    dlg.deleteLater()


def test_a_rescan_retires_the_scan_it_replaces(monkeypatch):
    """Sensitivity and the exclude field stay live while a scan runs, so a
    second scan can start on top of a first. The first used to keep its timer,
    which then stopped the live scan's timer instead of its own and rebuilt the
    list ten times a second, wiping the verdicts and suspensions already
    recorded on those rows."""
    import threading
    mock, _ = harness.bootstrap()
    harness.app()
    _populate(mock)
    from internpearls import dupes_dialog
    dlg = _build_dialog(mock)

    release = threading.Event()
    monkeypatch.setattr(dupes_dialog, "find_candidates",
                        lambda *a, **k: (release.wait(15), "STALE")[1])
    dlg._rescan()
    stale_timer, stale_worker = dlg._timer, dlg._worker

    monkeypatch.setattr(dupes_dialog, "find_candidates", lambda *a, **k: [])
    dlg._rescan()
    assert dlg._timer is not stale_timer
    assert not stale_timer.isActive()

    release.set()
    stale_worker.join(timeout=15)
    assert dlg._scan_result != "STALE"
    dlg._wait_for_scan()
    assert dlg._pairs == []
    dlg.deleteLater()
