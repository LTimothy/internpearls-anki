"""End-to-end wizard flows against the mock Anki and the fake CLI."""
import os
import shutil
import sys

from internpearls import ai_cli, ai_dialog

FAKE = os.path.join(os.path.dirname(__file__), "fake_cli.py")


def test_setup_page_shown_when_no_backend(anki, monkeypatch):
    monkeypatch.setattr(ai_cli, "find_cli", lambda kind, override="": None)
    dlg = ai_dialog._GenerateDialog()
    assert dlg.stack.currentWidget() is dlg.setup_page
    # every backend's row names it and states its own safety plainly
    for kind, meta in ai_cli.BACKENDS.items():
        text = dlg.setup_rows[kind].text()
        assert meta["label"] in text
        assert meta["safety"] in text


def test_input_page_shown_when_backend_found(anki, monkeypatch):
    monkeypatch.setattr(
        ai_cli, "find_cli",
        lambda kind, override="": "/usr/bin/x" if kind == "claude" else None)
    monkeypatch.setattr(
        ai_cli, "probe", lambda kind, path: {"ok": True, "detail": "v1"})
    dlg = ai_dialog._GenerateDialog()
    assert dlg.stack.currentWidget() is dlg.input_page
    assert not dlg.generate_btn.isEnabled()   # empty source
    dlg.source_box.setPlainText("some source")
    assert dlg.generate_btn.isEnabled()


def _ready_dialog(anki, monkeypatch, cli_mode="ok"):
    monkeypatch.setattr(
        ai_cli, "find_cli",
        lambda kind, override="": sys.executable if kind == "claude" else None)
    monkeypatch.setattr(
        ai_cli, "probe", lambda kind, path: {"ok": True, "detail": "v1"})
    monkeypatch.setattr(
        ai_cli, "build_argv",
        lambda kind, path, mode, scratch, imgs:
            ([sys.executable, FAKE, cli_mode], True))
    dlg = ai_dialog._GenerateDialog()
    dlg.source_box.setPlainText("LAST toxicity source text")
    return dlg


def test_generation_reaches_review_with_cards(anki, monkeypatch):
    dlg = _ready_dialog(anki, monkeypatch)
    dlg._start_generation()
    dlg._wait_for_worker(timeout=15)   # test helper: joins thread + drains timer
    assert dlg.stack.currentWidget() is dlg.review_page
    assert len(dlg.session.cards) == 1
    assert dlg.session.included == [True]


def test_generation_failure_returns_to_input(anki, monkeypatch):
    dlg = _ready_dialog(anki, monkeypatch, cli_mode="fail")
    dlg._start_generation()
    dlg._wait_for_worker(timeout=15)
    assert dlg.stack.currentWidget() is dlg.input_page
    assert dlg.source_box.toPlainText()   # inputs intact


def test_cancel_generation_preserves_inputs(anki, monkeypatch):
    dlg = _ready_dialog(anki, monkeypatch, cli_mode="slow")
    dlg._start_generation()
    dlg._cancel_generation()
    dlg._wait_for_worker(timeout=15)
    assert dlg.stack.currentWidget() is dlg.input_page
    assert dlg.source_box.toPlainText() == "LAST toxicity source text"
    assert dlg.instructions_box.toPlainText() == ""
    assert not dlg.session.cards   # nothing touched the collection


def _counting_run_generation(monkeypatch):
    """Wrap ai_cli.run_generation to count real invocations, so a test can pin
    "exactly one model call" rather than just "it eventually finished"."""
    calls = []
    real_run = ai_cli.run_generation

    def counting(*a, **kw):
        calls.append(1)
        return real_run(*a, **kw)

    monkeypatch.setattr(ai_cli, "run_generation", counting)
    return calls


def test_review_note_queue_and_revise_all(anki, monkeypatch):
    calls = _counting_run_generation(monkeypatch)
    dlg = _ready_dialog(anki, monkeypatch)
    dlg._start_generation()
    dlg._wait_for_worker()
    assert len(calls) == 1
    dlg.session.notes[0] = "make it a cloze"
    dlg.feedback_box.setPlainText("shorter answers")
    dlg._revise_all()
    dlg._wait_for_worker()
    # One CLI turn for the whole revision, not one per queued note.
    assert len(calls) == 2
    assert dlg.stack.currentWidget() is dlg.review_page
    assert dlg.session.notes == {}          # consumed by the revision


def test_edit_card_updates_fields_via_prompt(anki, monkeypatch):
    dlg = _ready_dialog(anki, monkeypatch)
    dlg._start_generation()
    dlg._wait_for_worker()
    card = dlg.session.cards[0]
    order = ai_dialog.FIELD_MAP[card["note_type"]]
    anki.gui.interactive = True
    anki.gui.interactions = [
        {"text": "NEW FRONT" if name == "Front" else card["fields"].get(name, ""),
         "ok": True}
        for name in order]
    dlg._edit_card(0)
    assert dlg.session.cards[0]["fields"]["Front"] == "NEW FRONT"


def test_note_card_queues_and_clears_a_revision_note(anki, monkeypatch):
    dlg = _ready_dialog(anki, monkeypatch)
    dlg._start_generation()
    dlg._wait_for_worker()
    anki.gui.interactive = True
    anki.gui.interactions.append({"text": "make it a cloze", "ok": True})
    dlg._note_card(0)
    assert dlg.session.notes[0] == "make it a cloze"
    anki.gui.interactions.append({"text": "", "ok": True})
    dlg._note_card(0)
    assert 0 not in dlg.session.notes


def test_import_writes_notes_and_closes(anki, monkeypatch):
    dlg = _ready_dialog(anki, monkeypatch)
    dlg._start_generation()
    dlg._wait_for_worker()
    n = dlg._do_import()
    assert n == 1
    notes = [x for x in anki.col._notes.values() if x.guid.startswith("iplocal-")]
    assert len(notes) == 1
    assert dlg._result == 1   # the dialog closed (accepted)


def test_excluded_card_is_not_imported(anki, monkeypatch):
    dlg = _ready_dialog(anki, monkeypatch)
    dlg._start_generation()
    dlg._wait_for_worker()
    dlg.session.included[0] = False
    n = dlg._do_import()
    assert n == 0
    assert not anki.col._notes


def _basic_card(front, source):
    return {"note_type": "Study Deck - Basic",
            "fields": {"Front": front, "Back": "A", "Why": "", "Image": "",
                      "Tag": "", "Dosing": "", "Notes": ""},
            "tags": [], "images": [{"source": source, "alt": "", "attribution": ""}],
            "rationale": ""}


def test_two_svg_images_get_distinct_media_filenames(anki, monkeypatch):
    dlg = _ready_dialog(anki, monkeypatch)
    s = dlg.session
    svg = "<svg xmlns='http://www.w3.org/2000/svg'><rect/></svg>"
    s.cards = [_basic_card("Q1", f"svg:{svg}"), _basic_card("Q2", f"svg:{svg}")]
    s.included = [True, True]
    ok_check = [{"code": "ok", "level": "ok", "message": "checks pass"}]
    s.checks = [ok_check, ok_check]
    n = dlg._do_import()
    assert n == 2
    names = [f for c in s.cards for f in c["_media_files"]]
    assert len(names) == 2
    assert len(set(names)) == 2   # no collision even though both cards drew the same SVG
    imgs = sorted(note["Image"] for note in anki.col._notes.values())
    assert len(set(imgs)) == 2


def test_one_failed_image_does_not_abort_import(anki, monkeypatch):
    dlg = _ready_dialog(anki, monkeypatch)
    s = dlg.session
    s.cards = [_basic_card("Q1", "url:https://example.com/x.png")]
    s.included = [True]
    s.checks = [[{"code": "ok", "level": "ok", "message": "checks pass"}]]

    def boom(url):
        raise RuntimeError("network is down")

    monkeypatch.setattr(ai_dialog, "fetch_card_image", boom)
    n = dlg._do_import()
    assert n == 1   # the card still imports; only its image is skipped
    note = next(iter(anki.col._notes.values()))
    assert note["Image"] == ""
    assert any("Skipping an image" in w for w in anki.gui.warnings)


def test_close_at_review_confirms_discard(anki, monkeypatch):
    dlg = _ready_dialog(anki, monkeypatch)
    dlg._start_generation()
    dlg._wait_for_worker()
    anki.gui.answers = [False]   # "Keep editing"
    dlg.reject()
    assert dlg.stack.currentWidget() is dlg.review_page
    assert dlg.session.cards            # nothing discarded
    assert dlg._result is None          # the dialog is still open
    assert any("Discard" in b[0] for b in anki.gui.ask_buttons)


def test_close_at_review_discards_when_confirmed(anki, monkeypatch):
    dlg = _ready_dialog(anki, monkeypatch)
    dlg._start_generation()
    dlg._wait_for_worker()
    anki.gui.answers = [True]    # "Discard"
    dlg.reject()
    assert dlg._result == 0


def test_close_before_generation_does_not_ask(anki, monkeypatch):
    dlg = _ready_dialog(anki, monkeypatch)
    dlg.reject()
    assert dlg._result == 0
    assert not anki.gui.asks


def test_malformed_json_retries_once_then_fails(anki, monkeypatch):
    calls = _counting_run_generation(monkeypatch)
    dlg = _ready_dialog(anki, monkeypatch, cli_mode="badjson")
    dlg._start_generation()
    dlg._wait_for_worker(timeout=15)   # first attempt: malformed, triggers a retry
    dlg._wait_for_worker(timeout=15)   # retry: also malformed, gives up
    assert len(calls) == 2
    assert dlg.stack.currentWidget() is dlg.input_page
    assert not dlg.session.cards
    assert any("retry" in w.lower() or "valid" in w.lower()
              for w in anki.gui.warnings)


def test_view_skills_escapes_raw_html_and_preserves_line_breaks(anki):
    parts = ["line one", "",
            "Comparisons use an HTML <table>. Four or more use <ul>."]
    body = ai_dialog._skills_html(parts)
    assert "<table>" not in body            # not real markup...
    assert "&lt;table&gt;" in body          # ...shown literally instead
    assert body.count("<br>") >= 2          # newlines survived as real line breaks


# -- scratch dir cleanup --------------------------------------------------

def test_scratch_dir_removed_after_import(anki, monkeypatch):
    dlg = _ready_dialog(anki, monkeypatch)
    dlg._start_generation()
    dlg._wait_for_worker()
    scratch = dlg.session.scratch
    assert scratch and os.path.isdir(scratch)
    dlg._do_import()
    assert not os.path.exists(scratch)
    assert dlg.session.scratch is None


def test_scratch_dir_removed_on_discard(anki, monkeypatch):
    dlg = _ready_dialog(anki, monkeypatch)
    dlg._start_generation()
    dlg._wait_for_worker()
    scratch = dlg.session.scratch
    assert scratch and os.path.isdir(scratch)
    anki.gui.answers = [True]   # confirm discard
    dlg.reject()
    assert not os.path.exists(scratch)
    assert dlg.session.scratch is None


def test_scratch_dir_kept_when_discard_is_declined(anki, monkeypatch):
    """Cleanup only runs once the dialog actually agrees to close -- declining
    the discard confirmation must not blow away work still being reviewed."""
    dlg = _ready_dialog(anki, monkeypatch)
    dlg._start_generation()
    dlg._wait_for_worker()
    scratch = dlg.session.scratch
    anki.gui.answers = [False]   # "Keep editing"
    dlg.reject()
    assert os.path.isdir(scratch)
    assert dlg.session.scratch == scratch


def test_scratch_cleanup_is_defensive_about_an_already_missing_directory(anki, monkeypatch):
    dlg = _ready_dialog(anki, monkeypatch)
    dlg._start_generation()
    dlg._wait_for_worker()
    shutil.rmtree(dlg.session.scratch)   # e.g. removed out from under us somehow
    dlg._cleanup_scratch()               # must not raise
    assert dlg.session.scratch is None


# -- revision: preserving manual choices, and shape-mismatch honesty ------

def test_revision_preserves_manual_include_choices_for_unchanged_cards(anki, monkeypatch):
    # Seed an existing note so the second card's front collides with it,
    # giving that card a block-level "duplicate" check and, by default, excluded.
    anki.col.add_note("her-guid", ["Duplicate front", "b", "", "", "", "", ""],
                      ["InternPearls"], deck="Intern Pearls::Intern Custom")
    dlg = _ready_dialog(anki, monkeypatch, cli_mode="two_cards")
    dlg._start_generation()
    dlg._wait_for_worker()
    assert len(dlg.session.cards) == 2
    # Defaults: the clean card in, the flagged duplicate out.
    assert dlg.session.included == [True, False]
    # The user overrides both defaults by hand.
    dlg.session.included[0] = False   # excludes the clean card on purpose
    dlg.session.included[1] = True    # includes the blocked one on purpose
    dlg._revise_all()
    dlg._wait_for_worker()
    # fake_cli's two_cards mode returns the identical two cards every time, so
    # the revision changed neither -- both manual choices must survive it.
    assert dlg.session.updated == set()
    assert dlg.session.included == [False, True]


def test_revision_with_different_card_count_does_not_claim_per_card_updates(anki, monkeypatch):
    monkeypatch.setattr(
        ai_cli, "find_cli",
        lambda kind, override="": sys.executable if kind == "claude" else None)
    monkeypatch.setattr(
        ai_cli, "probe", lambda kind, path: {"ok": True, "detail": "v1"})
    calls = []

    def build_argv(kind, path, mode, scratch, imgs):
        # First call (the initial draft) returns one card; the "revision"
        # comes back with two -- the shape mismatch nothing here can prevent.
        calls.append(1)
        cli_mode = "ok" if len(calls) == 1 else "two_cards"
        return [sys.executable, FAKE, cli_mode], True

    monkeypatch.setattr(ai_cli, "build_argv", build_argv)
    dlg = ai_dialog._GenerateDialog()
    dlg.source_box.setPlainText("LAST toxicity source text")
    dlg._start_generation()
    dlg._wait_for_worker()
    assert len(dlg.session.cards) == 1
    dlg._revise_all()
    dlg._wait_for_worker()
    assert len(dlg.session.cards) == 2
    assert dlg.session.revision_shape_mismatch is True
    # No card is presented as confidently kept-verbatim when the count itself
    # moved -- every index is treated as changed, not silently mismatched.
    assert dlg.session.updated == {0, 1}
    assert dlg.session.included == [True, True]   # falls back to the mechanical-check default
    header = dlg.review_header.text()
    assert "different number of cards" in header
    assert "kept 2 verbatim" not in header
    assert "kept 1 verbatim" not in header
