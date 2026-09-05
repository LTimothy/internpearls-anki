"""Real-Qt render checks for Scan for duplicates: three candidate rows, light and dark."""
import os

import harness


def _populate(mock):
    import mock_anki
    mock.mw.col = mock_anki.MockCollection()   # a fresh collection per test
    mock.mw._config = {}
    col = mock.mw.col
    col.add_note("g1", ["Fenoldopam mechanism of action, selective D1 receptor agonist",
                        "Acts on renal dopamine receptors"],
                 ["InternPearls"], deck="Intern Custom")
    col.add_note("g2", ["Fenoldopam is a selective D1 receptor agonist drug",
                        "Used for hypertensive emergencies"],
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
                        "Antiemetic and prokinetic agent"],
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

    def fake_run_generation(kind, path, prompt, mode, scratch, **kw):
        assert "note_id" not in prompt.lower()
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
    dlg.deleteLater()


def test_exclude_decks_empty_by_default_excludes_nothing():
    mock, _ = harness.bootstrap()
    harness.app()
    _populate_with_reference_deck(mock)
    dlg = _build_dialog(mock)
    assert dlg.exclude_edit.text() == ""
    fronts = {p["right"][1] for p in dlg._pairs}
    assert any("Sugammadex" in f for f in fronts)
    assert "decks excluded" not in dlg.summary_label.text()
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
    assert "(1 decks excluded)" in dlg.summary_label.text()
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
    # Three text lines' worth of height (the "ours:"/"theirs:" pair plus headroom for
    # wrapping), plus the row's own outer margins, is generous slack for a collapsed
    # row's real content; anything past it means the row is sizing to something other
    # than what it actually shows.
    limit = fm.height() * 3 + 20
    assert row.height() < limit, (
        f"a collapsed row is {row.height()}px tall, past {limit}px (3 text lines "
        "plus margins): it is not sizing to its own content")
    dlg.close()
    dlg.deleteLater()
