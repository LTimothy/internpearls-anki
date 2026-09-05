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
        dlg.resize(640, 560)
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
