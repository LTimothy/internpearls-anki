"""End-to-end wizard flows against the mock Anki and the fake CLI."""
import os
import shutil
import sys
import time

import pytest

from internpearls import ai_cli, ai_dialog, config

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


def test_setup_page_has_a_close_button(anki, monkeypatch):
    monkeypatch.setattr(ai_cli, "find_cli", lambda kind, override="": None)
    dlg = ai_dialog._GenerateDialog()
    assert dlg.stack.currentWidget() is dlg.setup_page
    anki.gui.answers = []
    dlg.close_btn.clicked.emit()   # a no-backend user's only other way out is window chrome
    assert dlg._result == 0


def test_image_id_note_type_is_not_offered(anki, monkeypatch):
    # Minor: its primary field IS the image, which a generated card has no
    # way to fill (images travel separately) -- one such card used to poison
    # the whole reply. Decision: don't offer it for generation at all.
    monkeypatch.setattr(
        ai_cli, "find_cli",
        lambda kind, override="": "/usr/bin/x" if kind == "claude" else None)
    monkeypatch.setattr(ai_cli, "probe", lambda kind, path: {"ok": True, "detail": "v1"})
    dlg = ai_dialog._GenerateDialog()
    assert "Study Deck - Image ID" not in dlg.type_boxes


def test_input_page_disables_a_note_type_missing_from_the_collection(anki, monkeypatch):
    """A managed type only exists once its deck has been synced at least once
    (_ensure_notetypes reconciles an existing type's fields, it never creates
    one) -- offering "Study Deck - Cloze" checked by default before that first
    sync let someone write source material, wait through a whole generation,
    and only discover at the very last click (Import) that add_generated_notes
    rejects the whole batch. The `anki` fixture's mock collection seeds only
    "Study Deck - Basic" (mock_anki.MockCollection's default model), the exact
    shape of the real run this reproduces -- see collection.py's
    test_add_generated_notes_core_type_missing_from_collection_raises for the
    matching import-time check, which must keep raising regardless of this."""
    monkeypatch.setattr(
        ai_cli, "find_cli",
        lambda kind, override="": "/usr/bin/x" if kind == "claude" else None)
    monkeypatch.setattr(ai_cli, "probe", lambda kind, path: {"ok": True, "detail": "v1"})
    dlg = ai_dialog._GenerateDialog()

    present = dlg.type_boxes["Study Deck - Basic"]
    assert present.isEnabled() and present.isChecked()

    for missing in ("Study Deck - Cloze", "Basic", "Cloze"):
        box = dlg.type_boxes[missing]
        assert not box.isEnabled(), f"{missing} isn't in the collection yet"
        assert not box.isChecked()

    # What actually gets sent to the model: only the type that really exists.
    dlg.source_box.setPlainText("some source text")
    note_types = [n for n, b in dlg.type_boxes.items() if b.isChecked()]
    assert note_types == ["Study Deck - Basic"]


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


# -- I7: Test connection -----------------------------------------------------

def _drain_conn_test(dlg, timeout=15):
    """Test helper mirroring _wait_for_worker: joins the background thread a
    Test connection click started, then fires its poll timer (the mock has no
    live event loop) to run the completion callback."""
    t, timer = dlg._conn_test_refs[-1]
    t.join(timeout=timeout)
    timer.fire()


def test_setup_test_connection_button_disabled_until_a_cli_is_found(anki, monkeypatch):
    monkeypatch.setattr(ai_cli, "find_cli", lambda kind, override="": None)
    dlg = ai_dialog._GenerateDialog()
    assert not dlg.test_buttons["claude"].isEnabled()


def test_setup_test_connection_reports_working(anki, monkeypatch):
    monkeypatch.setattr(
        ai_cli, "find_cli",
        lambda kind, override="": sys.executable if kind == "claude" else None)
    monkeypatch.setattr(ai_cli, "probe", lambda kind, path: {"ok": True, "detail": "v1"})
    monkeypatch.setattr(
        ai_cli, "build_argv",
        lambda kind, path, mode, scratch, imgs: ([sys.executable, FAKE, "badjson"], True))
    dlg = ai_dialog._GenerateDialog()
    assert dlg.test_buttons["claude"].isEnabled()
    dlg._test_setup_connection("claude")
    assert not dlg.test_buttons["claude"].isEnabled()   # disabled while the test runs
    _drain_conn_test(dlg)
    assert "working" in dlg.test_status["claude"].text().lower()
    assert dlg.test_buttons["claude"].isEnabled()        # re-enabled once it's done


def test_recheck_mid_test_connection_does_not_reenable_or_double_run(anki, monkeypatch):
    # Minor fix: Re-check used to unconditionally reset every test button/status,
    # so pressing it while a Test connection run was in flight re-enabled that
    # button and cleared its "Testing connection..." text -- a second click could
    # then start a concurrent test racing the first to write the same label.
    monkeypatch.setattr(
        ai_cli, "find_cli",
        lambda kind, override="": sys.executable if kind == "claude" else None)
    monkeypatch.setattr(ai_cli, "probe", lambda kind, path: {"ok": True, "detail": "v1"})
    monkeypatch.setattr(
        ai_cli, "build_argv",
        lambda kind, path, mode, scratch, imgs: ([sys.executable, FAKE, "badjson"], True))
    dlg = ai_dialog._GenerateDialog()
    dlg._test_setup_connection("claude")
    assert not dlg.test_buttons["claude"].isEnabled()
    status_mid_test = dlg.test_status["claude"].text()
    n_refs = len(dlg._conn_test_refs)

    dlg._detect(config._cfg())   # simulates a Re-check click mid-test
    assert not dlg.test_buttons["claude"].isEnabled()          # still disabled
    assert dlg.test_status["claude"].text() == status_mid_test  # not wiped

    dlg._test_setup_connection("claude")   # a second click while still running
    assert len(dlg._conn_test_refs) == n_refs   # no second test was started

    _drain_conn_test(dlg)
    assert "working" in dlg.test_status["claude"].text().lower()
    assert dlg.test_buttons["claude"].isEnabled()


def test_setup_test_connection_not_signed_in_shows_readable_message(anki, monkeypatch):
    monkeypatch.setattr(
        ai_cli, "find_cli",
        lambda kind, override="": sys.executable if kind == "claude" else None)
    monkeypatch.setattr(ai_cli, "probe", lambda kind, path: {"ok": True, "detail": "v1"})
    monkeypatch.setattr(
        ai_cli, "build_argv",
        lambda kind, path, mode, scratch, imgs:
            ([sys.executable, FAKE, "not_signed_in"], True))
    dlg = ai_dialog._GenerateDialog()
    dlg._test_setup_connection("claude")
    _drain_conn_test(dlg)
    text = dlg.test_status["claude"].text().lower()
    assert "not working" in text and "sign in" in text
    assert "traceback" not in text and "run `claude login`" not in text  # not raw stderr


def test_input_page_test_connection_button_works(anki, monkeypatch):
    dlg = _ready_dialog(anki, monkeypatch, cli_mode="badjson")
    dlg._test_backend_connection()
    _drain_conn_test(dlg)
    assert "working" in dlg.backend_test_status.text().lower()


def test_detect_status_reads_as_one_of_the_readmes_three_states(anki, monkeypatch):
    # I7: the row text must say something semantically matching the README's
    # three states, not render --version's raw output as if it were one.
    monkeypatch.setattr(
        ai_cli, "find_cli",
        lambda kind, override="": sys.executable if kind == "claude" else None)
    monkeypatch.setattr(ai_cli, "probe", lambda kind, path: {"ok": True, "detail": "9.9.9"})
    dlg = ai_dialog._GenerateDialog()
    assert "installed and working" in dlg.setup_rows["claude"].text()


def test_mode_radio_labels_are_the_backends_own_truthful_text(anki, monkeypatch):
    # C1: one hardcoded claim for all three backends used to overclaim what
    # codex/agy actually enforce; each backend's radios must show its own text.
    monkeypatch.setattr(
        ai_cli, "find_cli",
        lambda kind, override="": "/usr/bin/x" if kind == "claude" else None)
    monkeypatch.setattr(
        ai_cli, "probe", lambda kind, path: {"ok": True, "detail": "v1"})
    dlg = ai_dialog._GenerateDialog()
    modes = ai_cli.BACKENDS["claude"]["modes"]
    assert dlg.thorough_radio.text() == modes["thorough"]
    assert dlg.quick_radio.text() == modes["quick"]


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


def test_completion_exception_reaches_dialog_and_recovers_to_input(anki, monkeypatch):
    """B/C: the QTimer poll -> _finish_generation path had no guard at all, so an
    exception raised while processing an already-finished generation reached
    Anki's raw crash box instead of this add-on's own dialog. Worse, by the time
    the poll calls _finish_generation it has already set _gen_done and stopped its
    own timer (so the cycle can't run twice) -- so if _finish_generation itself
    then raised, nothing was left running for Cancel to act on and nothing ever
    moved the stack off the progress page: the wizard was stuck, permanently,
    with a Cancel button wired to a no-op.

    Fires the real timer callback (dlg._timer.fire(), the mock's stand-in for a
    real Qt timeout signal) rather than calling _finish_generation directly, so
    this exercises the actual wiring the bug lived in -- a raise inside a
    directly-invoked method call would never have caught this class of bug,
    exactly like test_import_button_click_shows_dialog_instead_of_raising above
    for the button-click guard.
    """
    from internpearls import ai_logic
    dlg = _ready_dialog(anki, monkeypatch)

    real_parse = ai_logic.parse_cards_json

    def boom(*a, **k):
        raise RuntimeError("boom in completion path")
    monkeypatch.setattr(ai_logic, "parse_cards_json", boom)

    warnings = []
    monkeypatch.setattr(ai_dialog, "_warn", lambda text, **kw: warnings.append(text))

    dlg._start_generation()
    dlg._worker.join(timeout=15)
    assert not dlg._worker.is_alive()

    dlg._timer.fire()   # the real wiring: timeout -> _guard_completion(_poll_worker)

    assert warnings, "the failure must reach the add-on's own dialog, not vanish"
    assert "boom in completion path" in warnings[0]
    assert dlg._gen_done is True
    assert dlg.stack.currentWidget() is dlg.input_page   # landed somewhere usable
    assert dlg.source_box.toPlainText()                  # inputs weren't thrown away

    dlg._cancel_generation()   # Cancel is still callable -- a no-op now, but never raises

    # The wizard itself isn't broken: a later generation completes normally.
    monkeypatch.setattr(ai_logic, "parse_cards_json", real_parse)
    dlg._start_generation()
    dlg._wait_for_worker(timeout=15)
    assert dlg.stack.currentWidget() is dlg.review_page
    assert dlg.session.cards


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


# -- Minor: a fresh Generate after an existing draft is not a revision ------

def test_fresh_generate_after_back_does_not_report_a_bogus_diff(anki, monkeypatch):
    dlg = _ready_dialog(anki, monkeypatch)
    dlg._start_generation()
    dlg._wait_for_worker()
    assert dlg.stack.currentWidget() is dlg.review_page
    dlg.stack.setCurrentWidget(dlg.input_page)   # Back, with an unrelated draft in session

    dlg.source_box.setPlainText("a completely different topic")
    dlg._start_generation()          # revision=False: a genuinely fresh request
    dlg._wait_for_worker()
    assert dlg.session.updated == set()             # nothing "updated" against the old draft
    assert not dlg.session.revision_shape_mismatch
    assert "kept" not in dlg.review_header.text()
    assert "verbatim" not in dlg.review_header.text()


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


def test_undo_shortcut_asks_qt_for_the_native_undo_key_sequence(anki):
    """Finding 2, isolated from the dialog flow: _undo_shortcut() must ask Qt
    for StandardKey.Undo rendered as NativeText, not a bare/default-format
    toString() -- the mock's QKeySequence only renders the platform glyph for
    NativeText specifically (see mock_anki._QKeySequence), so this catches
    either a hardcoded string or a call that drops the format argument."""
    expected = "⌘Z" if sys.platform == "darwin" else "Ctrl+Z"
    assert ai_dialog._undo_shortcut() == expected


# -- a successful import notifies the UI: undo becomes reachable, the deck list
# refreshes -- see ai_dialog._do_import and mock_anki.MockMW.update_undo_actions.

def test_import_enables_undo_and_refreshes_the_deck_list(anki, monkeypatch):
    """The bug this guards: add_generated_notes wrote a real, mergeable undo
    entry, but nothing told mw about it, so Edit > Undo stayed greyed out and
    the deck browser never refreshed. Before the import there is nothing to
    undo yet (a fresh collection); after it, mw's undo action is enabled and
    mw.reset() (which also refreshes the deck list, the same call every other
    collection-writing action in this add-on already makes) has fired."""
    assert not anki.mw.form.actionUndo.isEnabled()
    dlg = _ready_dialog(anki, monkeypatch)
    dlg._start_generation()
    dlg._wait_for_worker()
    resets_before = anki.mw.reset_count
    n = dlg._do_import()
    assert n == 1
    assert anki.mw.reset_count > resets_before
    assert anki.mw.form.actionUndo.isEnabled()


def test_import_success_message_uses_the_platform_undo_shortcut(anki, monkeypatch):
    """Finding 2: the message used to hardcode "Ctrl+Z" on every platform. It
    should instead name whatever ai_dialog._undo_shortcut() (Qt's own native
    rendering of the standard Undo key sequence) actually returns."""
    dlg = _ready_dialog(anki, monkeypatch)
    dlg._start_generation()
    dlg._wait_for_worker()
    dlg._do_import()
    message = anki.gui.infos[-1]
    assert f"{ai_dialog._undo_shortcut()} reverts it" in message


# -- Finding 3: singular counts must not read "1 cards" / "1 notes" ----------

def test_import_button_label_pluralizes_a_single_card(anki, monkeypatch):
    dlg = _ready_dialog(anki, monkeypatch)
    dlg._start_generation()
    dlg._wait_for_worker()
    assert len(dlg.session.cards) == 1
    assert dlg.import_btn.text() == "Import 1 card"


def test_review_header_pluralizes_a_single_draft_card(anki, monkeypatch):
    dlg = _ready_dialog(anki, monkeypatch)
    dlg._start_generation()
    dlg._wait_for_worker()
    assert "1 draft card " in dlg.review_header.text()
    assert "1 draft cards" not in dlg.review_header.text()


def test_revise_all_label_pluralizes_a_single_queued_note(anki, monkeypatch):
    dlg = _ready_dialog(anki, monkeypatch)
    dlg._start_generation()
    dlg._wait_for_worker()
    dlg.session.notes[0] = "make it a cloze"
    dlg._update_review_summary()
    assert dlg.revise_btn.text() == "Revise all (1 note)"


def test_import_success_message_pluralizes_a_single_card(anki, monkeypatch):
    dlg = _ready_dialog(anki, monkeypatch)
    dlg._start_generation()
    dlg._wait_for_worker()
    dlg._do_import()
    assert "1 card added" in anki.gui.infos[-1]
    assert "1 cards added" not in anki.gui.infos[-1]


def test_core_cloze_card_review_row_renders_non_empty(anki, monkeypatch):
    """I6: PRIMARY_FIELD had no "Cloze" entry, so the review row fell back to
    "Front", which a Cloze note lacks, and rendered empty -- approving a card
    whose text the user could not see."""
    from internpearls import ai_logic
    dlg = _ready_dialog(anki, monkeypatch)
    s = dlg.session
    s.cards = [{"note_type": "Cloze",
               "fields": {"Text": "{{c1::halothane}} sensitizes the heart",
                         "Back Extra": ""},
               "tags": [], "images": [], "rationale": ""}]
    s.included = [True]
    s.checks = ai_logic.mechanical_checks(s.cards, {})
    dlg._rebuild_review()
    row = dlg.cards_lay.itemAt(0).widget()
    label = row.layout().itemAt(1).widget()
    assert label.text().strip() != ""
    assert "halothane" in label.text()


def test_excluded_card_is_not_imported(anki, monkeypatch):
    dlg = _ready_dialog(anki, monkeypatch)
    dlg._start_generation()
    dlg._wait_for_worker()
    dlg.session.included[0] = False
    n = dlg._do_import()
    assert n == 0
    assert not anki.col._notes


# -- I4: importing zero cards must not close the wizard or claim an undo step -

def test_importing_zero_cards_does_not_close_the_wizard(anki, monkeypatch):
    dlg = _ready_dialog(anki, monkeypatch)
    dlg._start_generation()
    dlg._wait_for_worker()
    dlg.session.included[0] = False   # everything excluded
    n = dlg._do_import()
    assert n == 0
    assert dlg._result is None        # still open, not accept()ed
    assert dlg.stack.currentWidget() is dlg.review_page
    assert not anki.col._notes
    assert not any("undo" in i.lower() for i in anki.gui.infos)
    assert any("nothing" in i.lower() and "selected" in i.lower()
              for i in anki.gui.infos)


def _basic_card(front, source):
    return {"note_type": "Study Deck - Basic",
            "fields": {"Front": front, "Back": "A", "Why": "", "Image": "",
                      "Tag": "", "Dosing": "", "Notes": ""},
            "tags": [], "images": [{"source": source, "alt": "", "attribution": ""}],
            "rationale": ""}


def test_two_svg_images_get_distinct_media_filenames(anki, monkeypatch):
    # _do_import only ever reuses what review already resolved (see I2) -- it
    # never calls svg_to_media itself -- so the test seeds session.image_data
    # the way _run_image_resolution would have, rather than the raw source.
    dlg = _ready_dialog(anki, monkeypatch)
    s = dlg.session
    svg = "<svg xmlns='http://www.w3.org/2000/svg'><rect/></svg>"
    s.cards = [_basic_card("Q1", f"svg:{svg}"), _basic_card("Q2", f"svg:{svg}")]
    s.included = [True, True]
    ok_check = [{"code": "ok", "level": "ok", "message": "checks pass"}]
    s.checks = [ok_check, ok_check]
    resolved = {"state": "ok", "kind": "svg", "bytes": svg.encode("utf8"),
               "name": "generated-0.svg"}
    s.image_data = {0: [dict(resolved)], 1: [dict(resolved)]}
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
    # A card the user included anyway despite a failed resolution (the normal
    # path excludes it by default, per I2's mechanical-check gate) -- import
    # must still skip just the image, not the whole card.
    s.image_data = {0: [{"state": "error", "kind": "url", "error": "network is down",
                         "host": "example.com"}]}
    n = dlg._do_import()
    assert n == 1   # the card still imports; only its image is skipped
    note = next(iter(anki.col._notes.values()))
    assert note["Image"] == ""
    assert any("Skipping an image" in w for w in anki.gui.warnings)


# -- I2: images are resolved and gated at review time, not import time -----

def _stub_fetch_image(monkeypatch, data=b"PNGDATA", ext="png", error=None):
    def fake(url):
        if error:
            raise RuntimeError(error)
        return data, ext

    monkeypatch.setattr(ai_dialog, "fetch_card_image", fake)


def test_image_card_resolves_off_thread_and_reaches_review(anki, monkeypatch):
    _stub_fetch_image(monkeypatch)
    dlg = _ready_dialog(anki, monkeypatch, cli_mode="with_image")
    dlg._start_generation()
    assert dlg.stack.currentWidget() is dlg.progress_page   # not frozen: still polling
    dlg._wait_for_worker(timeout=15)   # drains generation, then image resolution
    assert dlg.stack.currentWidget() is dlg.review_page
    assert dlg.session.image_data[0][0]["state"] == "ok"


def test_image_card_starts_excluded_by_default(anki, monkeypatch):
    # I2: "excluded by default until the user has seen the rendered
    # thumbnail" -- true even when resolution succeeds and no mechanical
    # check would otherwise block it.
    _stub_fetch_image(monkeypatch)
    dlg = _ready_dialog(anki, monkeypatch, cli_mode="with_image")
    dlg._start_generation()
    dlg._wait_for_worker(timeout=15)
    assert dlg.session.included == [False]
    assert all(c["level"] != "block" for c in dlg.session.checks[0])   # not blocked, just gated


def test_review_row_shows_thumbnail_and_host_for_a_web_image(anki, monkeypatch):
    _stub_fetch_image(monkeypatch)
    dlg = _ready_dialog(anki, monkeypatch, cli_mode="with_image")
    dlg._start_generation()
    dlg._wait_for_worker(timeout=15)
    row = dlg.cards_lay.itemAt(0).widget()
    label = row.layout().itemAt(1).widget()
    text = label.text()
    assert "example.com" in text                      # the URL's host, per I2
    assert "<img" in text or "[image" in text          # a real indication of the image


def test_failed_image_download_becomes_a_mechanical_check_not_a_modal(anki, monkeypatch):
    _stub_fetch_image(monkeypatch, error="network is down")
    dlg = _ready_dialog(anki, monkeypatch, cli_mode="with_image")
    dlg._start_generation()
    dlg._wait_for_worker(timeout=15)
    assert dlg.stack.currentWidget() is dlg.review_page   # no modal interrupted the flow
    assert not anki.gui.warnings and not anki.gui.infos
    assert any(c["code"] == "image" and c["level"] == "block"
              for c in dlg.session.checks[0])
    assert dlg.session.included == [False]
    row = dlg.cards_lay.itemAt(0).widget()
    label = row.layout().itemAt(1).widget()
    assert "network is down" in label.text()


def test_import_reuses_review_resolved_bytes_without_a_second_fetch(anki, monkeypatch):
    calls = []

    def fake(url):
        calls.append(url)
        return b"PNGDATA", "png"

    monkeypatch.setattr(ai_dialog, "fetch_card_image", fake)
    dlg = _ready_dialog(anki, monkeypatch, cli_mode="with_image")
    dlg._start_generation()
    dlg._wait_for_worker(timeout=15)
    assert len(calls) == 1                 # resolved once, at review time
    dlg.session.included[0] = True         # the user explicitly opted in after seeing it
    n = dlg._do_import()
    assert n == 1
    assert len(calls) == 1                 # import did not fetch it again
    note = next(iter(anki.col._notes.values()))
    assert '<img src="generated-0-0.png">' in note["Image"]


def test_cards_with_no_images_are_unaffected_by_the_gate(anki, monkeypatch):
    # The common case (no images anywhere) must stay exactly as fast/simple as
    # before: no resolution phase, no forced exclusion.
    dlg = _ready_dialog(anki, monkeypatch)
    dlg._start_generation()
    dlg._wait_for_worker(timeout=15)
    assert dlg.session.included == [True]
    assert not hasattr(dlg, "_img_worker")


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


# -- I1: closing mid-generation must cancel the run, not orphan it ----------

def test_close_mid_generation_asks_and_declining_keeps_it_running(anki, monkeypatch):
    dlg = _ready_dialog(anki, monkeypatch, cli_mode="slow")
    dlg._start_generation()
    anki.gui.answers = [False]   # "Keep waiting"
    dlg.reject()
    assert dlg._result is None
    assert dlg.stack.currentWidget() is dlg.progress_page
    assert dlg._worker.is_alive()
    assert not dlg._cancel_flag.is_set()
    dlg._cancel_generation()   # clean up: actually cancel so the test doesn't leak a thread
    dlg._wait_for_worker(timeout=15)


def test_close_mid_generation_confirmed_kills_process_stops_timer_and_no_late_modal(
        anki, monkeypatch):
    dlg = _ready_dialog(anki, monkeypatch, cli_mode="slow")
    dlg._start_generation()
    scratch = dlg.session.scratch
    assert scratch and os.path.isdir(scratch)

    anki.gui.answers = [True]   # "Cancel and close"
    dlg.reject()

    assert dlg._result == 0                # the dialog closed right away
    assert dlg._timer.started is None       # the poll timer was stopped, not left running
    assert dlg._gen_done is True            # latched immediately, before the worker exited

    # The worker (and the subprocess it owns) actually dies, promptly --
    # this bounds how long "cancel" takes to really take effect.
    dlg._worker.join(timeout=15)
    assert not dlg._worker.is_alive()

    # A stray _poll_worker firing after close must be a no-op: no modal, no
    # navigation, on a dialog the user already closed.
    dlg._poll_worker()
    assert not anki.gui.warnings
    assert not anki.gui.infos
    assert dlg.stack.currentWidget() is dlg.progress_page   # never touched again

    # The scratch dir -- the dead child's own cwd -- is removed once the
    # background reaper has actually caught up with the worker's exit.
    for _ in range(100):
        if dlg.session.scratch is None:
            break
        time.sleep(0.05)
    assert dlg.session.scratch is None
    assert not os.path.exists(scratch)


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


# -- I5: dismissing View skills must never itself flip deck-skill consent -----

def _seed_deck_skill(enabled=True):
    config.save_deck_skill({"text": "do X", "version": "1",
                            "consented_on": "2026-01-01", "enabled": enabled})


def test_view_skills_dismiss_never_changes_consent(anki, monkeypatch):
    _seed_deck_skill(enabled=True)
    dlg = ai_dialog._GenerateDialog.__new__(ai_dialog._GenerateDialog)
    calls = {}

    def fake_ask_scrollable(body, yes_label=None, no_label=None,
                            extra_label=None, on_extra=None, **kw):
        calls["no_label"] = no_label
        # Dismissing (Close, Escape, or the window's close box) never reaches
        # on_extra -- that's the whole point of the extra_label mechanism.
        return True

    monkeypatch.setattr(ai_dialog, "_ask_scrollable", fake_ask_scrollable)
    dlg._view_skills()
    # No reject-role button at all, so Escape/close can't be mistaken for one.
    assert calls["no_label"] is None
    assert config.load_deck_skill()["enabled"] is True


def test_view_skills_extra_button_is_the_only_thing_that_toggles_consent(anki,
                                                                         monkeypatch):
    _seed_deck_skill(enabled=True)
    dlg = ai_dialog._GenerateDialog.__new__(ai_dialog._GenerateDialog)

    def fake_ask_scrollable(body, yes_label=None, no_label=None,
                            extra_label=None, on_extra=None, **kw):
        assert "disable" in extra_label.lower()
        on_extra(None)   # simulate an explicit click on the toggle button
        return True

    monkeypatch.setattr(ai_dialog, "_ask_scrollable", fake_ask_scrollable)
    dlg._view_skills()
    assert config.load_deck_skill()["enabled"] is False


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


# -- I8: cleanup must not run ahead of the call that can raise ---------------

def test_failing_import_leaves_scratch_dir_intact_for_a_retry(anki, monkeypatch):
    """add_generated_notes raises RuntimeError for an unknown/missing note type
    (real on a non-English profile where the stock Basic/Cloze names are
    localized). Cleaning the scratch dir up before that call, as this used to,
    meant a retry after the fix could no longer resolve any attached: image."""
    dlg = _ready_dialog(anki, monkeypatch)
    dlg._start_generation()
    dlg._wait_for_worker()
    scratch = dlg.session.scratch
    assert scratch and os.path.isdir(scratch)
    dlg.session.cards[0]["note_type"] = "Nonexistent Type"
    with pytest.raises(RuntimeError):
        dlg._do_import()
    assert os.path.isdir(scratch)
    assert dlg.session.scratch == scratch


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


# -- I3: cancelling or failing a revision must not strand the reviewed draft -

def _revisable_dialog(anki, monkeypatch, cli_mode_box):
    """Like _ready_dialog, but build_argv reads its mode from a mutable box so
    a test can run one CLI mode for the initial draft and a different one for
    the revision that follows it."""
    monkeypatch.setattr(
        ai_cli, "find_cli",
        lambda kind, override="": sys.executable if kind == "claude" else None)
    monkeypatch.setattr(
        ai_cli, "probe", lambda kind, path: {"ok": True, "detail": "v1"})
    monkeypatch.setattr(
        ai_cli, "build_argv",
        lambda kind, path, mode, scratch, imgs:
            ([sys.executable, FAKE, cli_mode_box[0]], True))
    dlg = ai_dialog._GenerateDialog()
    dlg.source_box.setPlainText("LAST toxicity source text")
    return dlg


def test_cancel_during_revision_returns_to_review_with_draft_intact(anki, monkeypatch):
    cli_mode = ["ok"]
    dlg = _revisable_dialog(anki, monkeypatch, cli_mode)
    dlg._start_generation()
    dlg._wait_for_worker()
    assert dlg.stack.currentWidget() is dlg.review_page

    dlg.session.cards[0]["fields"]["Front"] = "hand-edited front"
    dlg.session.included[0] = False
    dlg.session.notes[0] = "make it a cloze"

    cli_mode[0] = "slow"
    dlg._revise_all()
    dlg._cancel_generation()
    dlg._wait_for_worker(timeout=15)

    assert dlg.stack.currentWidget() is dlg.review_page
    assert dlg.session.cards[0]["fields"]["Front"] == "hand-edited front"
    assert dlg.session.included[0] is False
    assert dlg.session.notes[0] == "make it a cloze"


def test_failed_revision_after_retry_returns_to_review_with_draft_intact(anki, monkeypatch):
    cli_mode = ["ok"]
    dlg = _revisable_dialog(anki, monkeypatch, cli_mode)
    dlg._start_generation()
    dlg._wait_for_worker()
    assert dlg.stack.currentWidget() is dlg.review_page

    dlg.session.cards[0]["fields"]["Front"] = "hand-edited front"
    dlg.session.notes[0] = "make it a cloze"

    cli_mode[0] = "badjson"
    dlg._revise_all()
    dlg._wait_for_worker(timeout=15)   # first attempt: malformed, triggers a retry
    dlg._wait_for_worker(timeout=15)   # retry: also malformed, gives up

    assert dlg.stack.currentWidget() is dlg.review_page
    assert dlg.session.cards[0]["fields"]["Front"] == "hand-edited front"
    assert dlg.session.notes[0] == "make it a cloze"


def test_first_generation_cancel_still_falls_back_to_input_with_no_draft(anki, monkeypatch):
    # No prior draft exists yet on a first generation, so cancelling it has
    # nothing to return to -- the input page fallback is still correct here.
    dlg = _ready_dialog(anki, monkeypatch, cli_mode="slow")
    dlg._start_generation()
    dlg._cancel_generation()
    dlg._wait_for_worker(timeout=15)
    assert dlg.stack.currentWidget() is dlg.input_page
    assert not dlg.session.cards


# -- I8: the entry point itself must be guarded like every other menu action --

def test_generate_cards_entry_point_surfaces_a_bug_as_a_dialog(anki, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(ai_dialog, "_GenerateDialog", boom)
    ai_dialog.generate_cards()   # must not raise past the menu action
    assert any("boom" in w for w in anki.gui.warnings)


def test_import_buttons_own_click_signal_surfaces_a_bug_as_a_dialog(anki, monkeypatch):
    """@_safe on generate_cards() only catches an exception that unwinds back
    through its own call stack -- a button's clicked signal doesn't go through
    that stack at all, so _do_import used to reach Anki's raw crash box instead
    of this add-on's dialog (see ai_dialog._GenerateDialog._guard). Fires the
    button's actual clicked signal (like test_setup_page_has_a_close_button
    does above), not dlg._do_import() directly, so this exercises the same
    connected-callback path the bug lived in."""
    dlg = _ready_dialog(anki, monkeypatch)
    dlg._start_generation()
    dlg._wait_for_worker()
    dlg.session.cards[0]["note_type"] = "Nonexistent Type"   # add_generated_notes rejects this

    dlg.import_btn.clicked.emit()   # must not raise past the signal connection
    assert any("Nonexistent Type" in w for w in anki.gui.warnings)
    assert not anki.col._notes                     # nothing written
    assert dlg._result is None                      # the dialog is still open
