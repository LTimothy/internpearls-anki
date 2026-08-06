"""End-to-end flow tests over the real sync/collection/background modules,
running against the mock Anki from mock_anki.py (see conftest.py for wiring).

These drive the same code paths a click on Sync decks (or the auto-sync timer)
runs, with a real manifest + .apkg local-folder source built per test — the only
things mocked are Anki itself and the dialogs, which are recorded and scripted.
"""
import json
import os

import mock_anki
from mock_anki import make_apkg, make_model

SCOPE = "InternPearls"
TAGS = f"{SCOPE}::Pharm"


def drive(anki, fn, respond):
    """Run `fn` to completion via the same snapshot-and-replay Runner test_dialogs.py
    uses — needed here too now that reconcile_decks() confirms through a custom
    scrollable QDialog rather than the plain askUser() the simple
    anki.gui.answers=[...] shortcut answers."""
    from internpearls import collection, sync
    runner = mock_anki.Runner(anki, paths=[sync.INSTALLED, collection._USER_FILES])
    runner.drive(fn, respond)


def _walk(node, out=None):
    out = out if out is not None else []
    out.append(node)
    for c in node.get("children", []) or []:
        _walk(c, out)
    return out


def _find(tree, **want):
    for n in _walk(tree):
        if all(n.get(k) == v for k, v in want.items()):
            return n
    return None


def _label_nodes(tree):
    return [n for n in _walk(tree) if n.get("t") == "label"]


def _label_texts(tree):
    return [n["text"] for n in _label_nodes(tree)]


def _dismiss_result(tree):
    """The click that closes an end-of-run summary, or None when this dialog isn't one.

    A finished run reports itself through review.show_result now, a dialog of its own
    with a single OK, rather than the plain info box the flow helpers below used to be
    able to wave through with an empty response. Only the summary carries an OK: every
    confirmation in these flows answers with its own action label or Cancel.
    """
    ok = _find(tree, t="button", label="OK")
    return {"events": [{"id": ok["id"], "click": True}]} if ok else None


def _reconcile_tree(anki, accept=True):
    """Run reconcile_decks() to completion and hand back the widget tree its
    confirmation showed, so a test can assert on the rows it built rather than on the
    HTML of one label."""
    from internpearls import sync
    seen = {}

    def respond(p):
        if p["kind"] != "dialog":
            return {}
        seen["tree"] = p["tree"]
        return _click_reconcile_button(accept)(p)

    drive(anki, sync.reconcile_decks, respond)
    anki.gui.interactive = False
    return seen["tree"]


def _click_reconcile_button(accept):
    """respond() for reconcile_decks()'s confirmation dialog: click whichever button
    isn't labeled "Cancel" to accept (its label varies — "Archive", "Relocate", or
    "Archive and relocate" — depending on what's pending), or click Cancel to decline.

    Runner.start() flips gui.interactive on for the whole replay, so the info/warn
    calls reconcile_decks() also makes (e.g. the final "Archived N cards..." result)
    need a response too, not just the confirmation dialog itself — pass those straight
    through, there's nothing to click.
    """
    def respond(p):
        if p["kind"] != "dialog":
            return {}   # info/warn: nothing to click, just let it continue
        tree = p["tree"]
        if accept:
            btn = next(n for n in _walk(tree)
                      if n.get("t") == "button" and n.get("label") != "Cancel")
        else:
            btn = _find(tree, t="button", label="Cancel")
        return {"events": [{"id": btn["id"], "click": True}]}
    return respond


def _click_update_button(accept):
    """respond() for update_decks()'s confirmation. Update and Cancel are always
    exactly those two labels (unlike reconcile's, which vary), and unlike
    _click_reconcile_button this can't just grab "the first button that isn't
    Cancel": the confirmation's own card rows add their own caret buttons ahead of
    Update in the tree, so that search would click a caret instead."""
    def respond(p):
        if p["kind"] != "dialog":
            return {}
        done = _dismiss_result(p["tree"])
        if done:
            return done
        label = "Update" if accept else "Cancel"
        btn = _find(p["tree"], t="button", label=label)
        return {"events": [{"id": btn["id"], "click": True}]}
    return respond


def _click_sync_button(accept=True, ask=None):
    """respond() for sync_decks()'s confirmation, which is a list of deck rows in a
    widget body now rather than a plain askUser(), so it needs the same replay driver
    reconcile and update already use instead of the gui.answers shortcut. Its accept
    button reads Update, same as update_decks'.

    `ask(text) -> bool` answers whatever askUser() questions the rest of the run raises
    (the template-change and note-type-conversion offers), and defaults to yes.
    """
    def respond(p):
        if p["kind"] == "dialog":
            done = _dismiss_result(p["tree"])
            if done:
                return done
            btn = _find(p["tree"], t="button", label="Update" if accept else "Cancel")
            return {"events": [{"id": btn["id"], "click": True}]}
        if p["kind"] == "ask":
            return {"answer": True if ask is None else ask(p.get("text", ""))}
        return {}   # info/warn: nothing to click, just let it continue
    return respond


def _update(anki, accept=True):
    """Run update_decks() to completion, answering its confirmation. Returns every
    widget tree the run showed, in order: the confirmation, then the end-of-run
    summary, which is a dialog rather than an info box now."""
    from internpearls import sync
    trees = []
    click = _click_update_button(accept)

    def respond(p):
        if p["kind"] == "dialog":
            trees.append(p["tree"])
        return click(p)

    drive(anki, sync.update_decks, respond)
    return trees


def _sync(anki, accept=True, ask=None):
    """Run sync_decks() to completion, answering its confirmation. Returns every widget
    tree the run showed, in order: the confirmation, then the end-of-run summary (which
    is a dialog of its own now, not an info box, so what it said is read off the tree
    rather than off anki.gui.infos).

    Leaves the gui non-interactive again afterward, so a test that syncs and then calls
    something else directly still gets the plain shortcut rather than the replay
    protocol Runner.start() switched on.
    """
    from internpearls import sync
    trees = []
    click = _click_sync_button(accept, ask)

    def respond(p):
        if p["kind"] == "dialog":
            trees.append(p["tree"])
        return click(p)

    drive(anki, sync.sync_decks, respond)
    anki.gui.interactive = False
    return trees


def _sync_tree(anki, accept=True, ask=None):
    """_sync(), handing back just the widget tree the confirmation showed, so a test can
    assert on the rows it built. None when the run never got that far (nothing pending,
    or an unreachable source)."""
    trees = _sync(anki, accept, ask)
    return trees[0] if trees else None


def _summary_text(trees):
    """Everything the end-of-run summary said, joined. A finished run's last dialog is
    review.show_result's, so a test that used to read the outcome out of anki.gui.infos
    reads it off that tree instead."""
    return "\n".join(_label_texts(trees[-1])) if trees else ""


def _fields(front, back="the back", notes="", dosing=""):
    return [front, back, "why", "", "Pharm", dosing, notes]


def _write_source(tmp_path, decks, retired=None, deck_moves=None):
    """decks: {deck_name: (version, notes, model_or_None)} -> source folder path.
    `retired`/`deck_moves`, if given, ride along in the same manifest — update_decks()
    tests need a source that carries both a content update and a reconcile ledger."""
    folder = tmp_path / "source"
    folder.mkdir(exist_ok=True)
    manifest = {"schema": 1, "decks": [], "front_aliases": {},
                "retired": retired or {}, "deck_moves": deck_moves or {}}
    for name, spec in decks.items():
        version, notes, model = spec[0], spec[1], spec[2]
        media = spec[3] if len(spec) > 3 else None
        fn = name.split("::")[-1].replace(" ", "_") + ".apkg"
        make_apkg(str(folder / fn), notes, model=model, deck=name, media=media)
        manifest["decks"].append({"name": name, "apkg": fn, "version": version,
                                  "cards": len(notes)})
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf8")
    return str(folder)


def _configure(anki, folder):
    anki.mw._config = {"decks_dir": folder}


DECK = "Intern Pearls::Intern Custom::Pharm"


# ------------------------------------------------------------------- sync decks
def test_sync_confirmation_lists_each_deck_as_a_row_under_one_heading(anki, tmp_path):
    """The confirmation used to be a hand-indented bullet per deck inside one
    askUser() string, with "(12 cards, new deck)" spelled out in a parenthesis. It
    reads in the same row vocabulary as every other screen now: a heading, then one row
    per deck, its size in the trailing column and a chip for whether the deck is
    arriving or being refreshed."""
    from internpearls import widgets
    other = "Intern Pearls::Intern Custom::Other"
    _configure(anki, _write_source(tmp_path, {
        DECK: ("v1", [("g1", _fields("Front one"), TAGS)], None)}))
    _sync(anki)                     # Pharm is installed; Other is not
    _configure(anki, _write_source(tmp_path, {
        DECK:  ("v2", [("g1", _fields("Front one"), TAGS)], None),
        other: ("v1", [("g2", _fields("Front two"), f"{SCOPE}::Other")], None)}))

    texts = _label_texts(_sync_tree(anki))

    assert not [t for t in texts if "<li>" in t or "<ul>" in t]
    assert "Update these decks?" in texts
    assert "Pharm" in texts and "Other" in texts
    assert texts.count("1 card") == 2                 # each deck's own size, trailing
    # Pharm is already in the collection, so it reads as an update; Other is arriving.
    assert [t for t in texts if t in widgets.CHIPS.values()] == [
        widgets.CHIPS["changed"], widgets.CHIPS["new"]]


def test_sync_cancelled_imports_nothing(anki, tmp_path):
    """Cancel on the confirmation is still a clean no-op: nothing imported, and no
    version recorded that would make the next run think this deck was applied."""
    from internpearls import sync
    _configure(anki, _write_source(tmp_path, {
        DECK: ("v1", [("g1", _fields("Front one"), TAGS)], None)}))

    _sync(anki, accept=False)

    assert not anki.col.find_notes(f'"tag:{SCOPE}"')
    assert not anki.col.imports
    assert not os.path.exists(sync.INSTALLED)


def test_first_sync_imports_everything_and_persists_versions(anki, tmp_path):
    from internpearls import sync
    folder = _write_source(tmp_path, {
        DECK: ("v1", [("g1", _fields("Front one"), TAGS),
                      ("g2", _fields("Front two"), TAGS)], None)})
    _configure(anki, folder)

    trees = _sync(anki)

    assert len(anki.col.find_notes(f'"tag:{SCOPE}"')) == 2
    assert anki.col.note_by_guid("g1")["Front"] == "Front one"
    installed = json.load(open(sync.INSTALLED, encoding="utf8"))
    assert installed == {DECK: "v1"}
    assert "Sync complete" in _summary_text(trees)


def test_second_sync_with_same_versions_is_a_no_op(anki, tmp_path):
    from internpearls import sync
    folder = _write_source(tmp_path, {
        DECK: ("v1", [("g1", _fields("Front one"), TAGS)], None)})
    _configure(anki, folder)
    _sync(anki)
    imports_after_first = len(anki.col.imports)

    _sync(anki)

    assert len(anki.col.imports) == imports_after_first
    assert any("up to date" in i for i in anki.gui.infos)


def test_second_sync_is_a_no_op_when_the_deck_uses_subdecks(anki, tmp_path):
    """A deck spec's deck_name is routinely just the parent path, with cards filed
    into deck_name::<subdeck> — every spec with a subdecks list works this way,
    including the real production decks and the live demo's example deck.
    installed_matching_collection must recognize that as "this deck is installed"
    rather than treating it as perpetually pending on every check — regression test
    for exactly that bug (found via the live demo constantly re-offering an update
    with nothing actually changed)."""
    from internpearls import sync
    subdeck = f"{DECK}::1. Basics"
    folder = tmp_path / "source"
    folder.mkdir()
    make_apkg(str(folder / "Pharm.apkg"), [("g1", _fields("Front one"), TAGS)],
              deck=subdeck)
    (folder / "manifest.json").write_text(json.dumps({
        "schema": 1, "decks": [{"name": DECK, "apkg": "Pharm.apkg", "version": "v1",
                               "cards": 1}],
        "front_aliases": {}, "retired": {}, "deck_moves": {}}), encoding="utf8")
    _configure(anki, str(folder))
    _sync(anki)
    imports_after_first = len(anki.col.imports)

    _sync(anki)

    assert len(anki.col.imports) == imports_after_first
    assert any("up to date" in i for i in anki.gui.infos)


def test_sync_recovers_after_a_collection_revert_undoes_a_prior_sync(anki, tmp_path):
    """installed.json lives in user_files/, outside the collection file, so restoring
    an earlier collection backup ("collection revert") rolls the collection back to
    before a sync while leaving installed.json still claiming that deck is current.
    Sync decks must notice the collection no longer has anything under the scope tag
    and re-treat every deck as pending, instead of reporting "up to date" against
    content that no longer exists (the reported bug)."""
    from internpearls import sync
    folder = _write_source(tmp_path, {
        DECK: ("v1", [("g1", _fields("Front one"), TAGS)], None)})
    _configure(anki, folder)
    _sync(anki)
    assert len(anki.col.find_notes(f'"tag:{SCOPE}"')) == 1

    # Simulate a collection revert: the collection rolls back to before the sync, but
    # installed.json — outside the collection — is untouched.
    anki.col._notes.clear()
    anki.col._cards.clear()
    anki.gui.infos.clear()

    trees = _sync(anki)

    assert not any("up to date" in i for i in anki.gui.infos)
    assert "Sync complete" in _summary_text(trees)
    assert len(anki.col.find_notes(f'"tag:{SCOPE}"')) == 1
    assert anki.col.note_by_guid("g1")["Front"] == "Front one"


def test_sync_recovers_a_single_deck_lost_to_a_partial_collection_revert(anki, tmp_path):
    """A revert to a backup taken between two syncs only erases the more recent
    deck's cards, leaving the earlier deck's cards (and its installed.json entry)
    intact — the common case, and the one an earlier, whole-collection-only version
    of this fix missed. Only the actually-missing deck should re-sync."""
    from internpearls import sync
    deck_b = "Intern Pearls::Intern Custom::Other"
    folder = _write_source(tmp_path, {
        DECK: ("v1", [("g1", _fields("Front one"), TAGS)], None),
        deck_b: ("v1", [("g2", _fields("Front two"), f"{SCOPE}::Other")], None)})
    _configure(anki, folder)
    _sync(anki)
    assert len(anki.col.find_notes(f'"tag:{SCOPE}"')) == 2

    # Only deck_b's card is erased by the revert; DECK's card and installed entry for
    # DECK survive untouched.
    lost_nid = next(nid for nid in anki.col.find_notes(f'"tag:{SCOPE}"')
                    if anki.col.get_note(nid).guid == "g2")
    del anki.col._notes[lost_nid]
    anki.gui.infos.clear()

    trees = _sync(anki)

    assert not any("up to date" in i for i in anki.gui.infos)
    assert "Sync complete" in _summary_text(trees)
    assert anki.col.note_by_guid("g1")["Front"] == "Front one"   # untouched, not re-imported
    assert anki.col.note_by_guid("g2")["Front"] == "Front two"   # recovered
    installed = json.load(open(sync.INSTALLED, encoding="utf8"))
    assert installed == {DECK: "v1", deck_b: "v1"}


def test_sync_overwrites_content_but_restores_protected_notes(anki, tmp_path):
    from internpearls import sync
    # She has the v1 card with her own annotation in Notes.
    anki.col.add_note("g1", _fields("Front one", back="old back",
                                    notes="her personal mnemonic"), [TAGS])
    folder = _write_source(tmp_path, {
        DECK: ("v2", [("g1", _fields("Front one", back="NEW back"), TAGS)], None)})
    _configure(anki, folder)

    _sync(anki)

    note = anki.col.note_by_guid("g1")
    assert note["Back"] == "NEW back"                    # content updated
    assert note["Notes"] == "her personal mnemonic"      # her field survived
    assert len(anki.col.find_notes(f'"tag:{SCOPE}"')) == 1   # updated, not duplicated


def test_preserved_field_she_never_touched_still_receives_updates(anki, tmp_path):
    """The point of preserving a field she can edit: protecting Dosing must not mean
    freezing it. First sync establishes what the source shipped; the second one is
    free to correct it, because her copy still matches that baseline."""
    from internpearls import sync
    anki.col.add_note("g1", _fields("Front one", dosing="1 mg/kg"), [TAGS], deck=DECK)
    _configure(anki, _write_source(tmp_path, {
        DECK: ("v1", [("g1", _fields("Front one", dosing="1 mg/kg"), TAGS)], None)}))
    anki.mw._config["protected_fields"] = ["Notes", "Dosing"]
    _sync(anki)

    _configure(anki, _write_source(tmp_path, {
        DECK: ("v2", [("g1", _fields("Front one", dosing="2 mg/kg (corrected)"), TAGS)],
               None)}))
    anki.mw._config["protected_fields"] = ["Notes", "Dosing"]
    _sync(anki)

    assert anki.col.note_by_guid("g1")["Dosing"] == "2 mg/kg (corrected)"


def test_preserved_field_she_edited_is_kept_and_the_collision_reported(anki, tmp_path):
    from internpearls import sync
    anki.col.add_note("g1", _fields("Front one", dosing="1 mg/kg"), [TAGS], deck=DECK)
    _configure(anki, _write_source(tmp_path, {
        DECK: ("v1", [("g1", _fields("Front one", dosing="1 mg/kg"), TAGS)], None)}))
    anki.mw._config["protected_fields"] = ["Notes", "Dosing"]
    _sync(anki)
    anki.col.note_by_guid("g1")["Dosing"] = "1 mg/kg (my attending says 1.5)"

    _configure(anki, _write_source(tmp_path, {
        DECK: ("v2", [("g1", _fields("Front one", dosing="2 mg/kg (corrected)"), TAGS)],
               None)}))
    anki.mw._config["protected_fields"] = ["Notes", "Dosing"]
    trees = _sync(anki)

    assert anki.col.note_by_guid("g1")["Dosing"] == "1 mg/kg (my attending says 1.5)"
    assert "changed a field you had also written in yourself" in _summary_text(trees)


def test_no_collision_reported_for_a_deck_this_run_never_touched(anki, tmp_path):
    """The false positive: a card in a deck with no update at all was reported as
    conflicting. Nothing imported over it, so nothing of hers was overwritten and
    there was no update to conflict with."""
    from internpearls import sync
    anki.col.add_note("g1", _fields("Front one", notes="her mnemonic"), [TAGS], deck=DECK)
    other = "Intern Pearls::Intern Custom::Other"
    anki.col.add_note("g2", _fields("Front two", notes="another of hers"), [TAGS],
                      deck=other)
    _configure(anki, _write_source(tmp_path, {
        DECK:  ("v1", [("g1", _fields("Front one"), TAGS)], None),
        other: ("v1", [("g2", _fields("Front two"), TAGS)], None)}))
    anki.mw._config["protected_fields"] = ["Notes"]
    _sync(anki)                       # first run establishes the baseline

    # only one deck changes now; the other is untouched
    _configure(anki, _write_source(tmp_path, {
        DECK:  ("v2", [("g1", _fields("Front one", back="NEW"), TAGS)], None),
        other: ("v1", [("g2", _fields("Front two"), TAGS)], None)}))
    anki.mw._config["protected_fields"] = ["Notes"]
    anki.gui.infos.clear()
    _sync(anki)

    assert not any("written in yourself" in i for i in anki.gui.infos)
    assert anki.col.note_by_guid("g2")["Notes"] == "another of hers"


def test_preserved_field_falls_back_to_always_restoring_without_a_baseline(anki, tmp_path):
    """Upgrading into this feature must never cost an annotation: with no record of
    what was last shipped, her value wins exactly as it did before."""
    from internpearls import sync
    anki.col.add_note("g1", _fields("Front one", notes="her mnemonic"), [TAGS])
    _configure(anki, _write_source(tmp_path, {
        DECK: ("v2", [("g1", _fields("Front one", notes="shipped placeholder"), TAGS)],
               None)}))

    _sync(anki)

    assert anki.col.note_by_guid("g1")["Notes"] == "her mnemonic"


def test_preserved_field_name_matches_regardless_of_case(anki, tmp_path):
    """A lowercase field name used to protect nothing at all, silently."""
    from internpearls import sync
    anki.col.add_note("g1", _fields("Front one", notes="her mnemonic"), [TAGS])
    _configure(anki, _write_source(tmp_path, {
        DECK: ("v2", [("g1", _fields("Front one"), TAGS)], None)}))
    anki.mw._config["protected_fields"] = ["notes"]

    _sync(anki)

    assert anki.col.note_by_guid("g1")["Notes"] == "her mnemonic"


CLOZE_FIELDS = ["Text", "Why", "Image", "Dosing", "Notes"]


def _cloze_model():
    from mock_anki import make_model
    return make_model(name="Study Deck - Cloze", fields=CLOZE_FIELDS)


def _convert(answer):
    """_sync()'s `ask` for a run that also converts note types: `answer` to the
    "changed format" question, yes to anything else."""
    return lambda text: answer if "changed format" in text else True


def test_qa_card_converted_to_cloze_keeps_its_card_and_history(anki, tmp_path):
    """The whole point: a question-and-answer card becoming a fill-in-the-blank used to
    mean retiring hers and starting the new one from zero. Converting her note's type
    first means the import matches it by GUID and updates it in place instead."""
    from internpearls import sync
    anki.col.models._models.append(_cloze_model())
    her = anki.col.add_note("g1", _fields("Old Q and A front", notes="her mnemonic"),
                            [TAGS], deck=DECK)
    card = anki.col.get_card(her.card_ids()[0])
    card.reps, card.ivl, card.due = 6, 21, 140
    _configure(anki, _write_source(tmp_path, {
        DECK: ("v2", [("g1", ["A {{c1::cloze}} version", "why", "", "", ""], TAGS)],
               _cloze_model())}))

    _sync(anki)

    assert len(anki.col.find_notes(f'"tag:{SCOPE}"')) == 1        # one card, not two
    note = anki.col.note_by_guid("g1")
    assert note.id == her.id                                      # same note
    assert note.note_type()["name"] == "Study Deck - Cloze"
    assert note["Text"] == "A {{c1::cloze}} version"              # Front mapped to Text
    assert note["Notes"] == "her mnemonic"                        # annotation survived
    kept = anki.col.get_card(note.card_ids()[0])
    assert (kept.reps, kept.ivl, kept.due) == (6, 21, 140)        # history survived
    assert anki.col.notetype_changes == [[her.id]]


def test_extra_blanks_inherit_the_parent_card_rather_than_starting_new(anki, tmp_path):
    """One card becoming four blanks must not become one card plus three new ones. She
    has been retrieving these same facts off the parent for months, and across a deck it
    would drop a four-figure new queue on her. Each extra blank inherits the parent's
    standing at half its interval, since producing one blank cold is harder than the
    paragraph the parent tested."""
    from internpearls import sync
    anki.col.models._models.append(_cloze_model())
    her = anki.col.add_note("g1", _fields("Old Q and A front"), [TAGS], deck=DECK)
    parent = anki.col.get_card(her.card_ids()[0])
    parent.reps, parent.ivl, parent.due, parent.factor, parent.type = 5, 20, 90, 2400, 2
    parent.memory_state = mock_anki.FsrsMemoryState(stability=18.0, difficulty=7.5)
    parent.desired_retention, parent.decay = 0.9, 0.1542
    _configure(anki, _write_source(tmp_path, {
        DECK: ("v2", [("g1", ["{{c1::one}} and {{c2::two}} and {{c3::three}}",
                              "why", "", "", ""], TAGS)], _cloze_model())}))

    _sync(anki)

    cards = sorted((anki.col.get_card(c) for c in anki.col.note_by_guid("g1").card_ids()),
                   key=lambda c: c.ord)
    assert len(cards) == 3
    assert (cards[0].reps, cards[0].ivl, cards[0].due) == (5, 20, 90)   # untouched
    for sib in cards[1:]:
        assert sib.ivl == 10                       # half the parent's, not new
        assert (sib.reps, sib.factor, sib.type) == (5, 2400, 2)
        assert sib.queue != 0 or sib.ivl > 0       # not sitting in the new queue
        # FSRS schedules from memory state, not ivl, so it has to travel too, and
        # stability is the interval's counterpart so it halves with it.
        assert sib.memory_state.stability == 9.0
        assert sib.memory_state.difficulty == 7.5  # difficulty is not per-blank
        assert (sib.desired_retention, sib.decay) == (0.9, 0.1542)


def test_extra_blanks_stay_new_when_the_parent_was_never_studied(anki, tmp_path):
    """Nothing to inherit means nothing is fabricated."""
    from internpearls import sync
    anki.col.models._models.append(_cloze_model())
    anki.col.add_note("g1", _fields("Old Q and A front"), [TAGS], deck=DECK)
    _configure(anki, _write_source(tmp_path, {
        DECK: ("v2", [("g1", ["{{c1::one}} and {{c2::two}}", "why", "", "", ""], TAGS)],
               _cloze_model())}))

    _sync(anki)

    for c in (anki.col.get_card(x) for x in anki.col.note_by_guid("g1").card_ids()):
        assert (c.reps, c.ivl) == (0, 0)


def test_declining_the_conversion_imports_alongside_instead(anki, tmp_path):
    """Declining is a real choice with a real consequence, and the dialog says so: the
    cards still arrive, just as separate notes, leaving her progress on the old ones."""
    from internpearls import sync
    anki.col.models._models.append(_cloze_model())
    her = anki.col.add_note("g1", _fields("Old Q and A front"), [TAGS], deck=DECK)
    _configure(anki, _write_source(tmp_path, {
        DECK: ("v2", [("g1", ["A {{c1::cloze}} version", "why", "", "", ""], TAGS)],
               _cloze_model())}))

    _sync(anki, ask=_convert(False))

    assert anki.col.notetype_changes == []
    assert anki.col.note_by_guid("g1").note_type()["name"] == "Study Deck - Basic"


def test_conversion_is_never_applied_by_unattended_auto_sync(anki, tmp_path):
    """Same rule as a template change: it bumps the schema, so it never happens with
    nobody there to consent. The deck is held back, not half-applied."""
    from internpearls import sync
    anki.col.add_note("g1", _fields("Old Q and A front"), [TAGS], deck=DECK)
    folder = _write_source(tmp_path, {
        DECK: ("v2", [("g1", ["A {{c1::cloze}} version", "why", "", "", ""], TAGS)],
               _cloze_model())})
    _configure(anki, folder)
    cfg = sync._cfg()

    results, _restored, _tpl, deferred, _c, _col, converted = sync._run_sync(
        cfg, json.load(open(os.path.join(folder, "manifest.json"))),
        lambda d: os.path.join(folder, d["apkg"]),
        [{"name": DECK, "apkg": "Pharm.apkg", "version": "v2"}], {},
        defer_template_changes=True)

    assert deferred == [DECK] and converted == 0
    assert anki.col.notetype_changes == []
    assert anki.col.note_by_guid("g1").note_type()["name"] == "Study Deck - Basic"


def test_conversion_sees_through_ankis_collision_suffix(anki, tmp_path):
    """Re-importing a deck across field additions leaves a learner's notes on
    "Study Deck - Basic+", "++" and so on, all still ours. Measured on a real
    collection: 595 of 625 notes were on a suffixed variant, so an exact-name check
    would convert almost nothing."""
    from internpearls import logic
    managed = {"Study Deck - Basic", "Study Deck - Cloze"}
    (c,) = logic.plan_notetype_changes(
        {"g1": "Study Deck - Cloze"}, {"g1": "Study Deck - Basic+++"}, managed)
    assert c == {"guid": "g1", "old": "Study Deck - Basic+++",
                 "new": "Study Deck - Cloze"}
    # same family on both sides is not a format change, whatever the suffixes
    assert logic.plan_notetype_changes(
        {"g1": "Study Deck - Cloze"}, {"g1": "Study Deck - Cloze+++++"}, managed) == []


def test_conversion_request_only_sets_fields_the_real_message_has(anki, tmp_path):
    """Regression: the add-on set new_notetype_name, which exists on no real
    ChangeNotetypeRequest. The stub accepted it, every test passed, and a real sync
    failed with 'Protocol message ChangeNotetypeRequest has no "new_notetype_name"
    field.' The stub now raises the way protobuf does, so this can only be green if the
    add-on stays inside the real field set."""
    import mock_anki as m
    req = m.ChangeNotetypeRequest()
    assert set(req._FIELDS) == {
        "note_ids", "new_fields", "new_templates", "old_notetype_id",
        "new_notetype_id", "current_schema", "old_notetype_name", "is_cloze"}
    try:
        req.new_notetype_name = "Study Deck - Cloze"
    except AttributeError as e:
        assert "has no" in str(e)
    else:
        raise AssertionError("the stub must reject a field the real message lacks")


def test_a_learners_own_note_type_is_never_converted(anki, tmp_path):
    from internpearls import logic
    assert logic.plan_notetype_changes(
        {"g1": "Study Deck - Cloze"}, {"g1": "Her Own Custom Type"},
        {"Study Deck - Basic", "Study Deck - Cloze"}) == []
    assert logic.plan_notetype_changes(
        {"g1": "Some Unknown Incoming Type"}, {"g1": "Study Deck - Basic"},
        {"Study Deck - Basic", "Study Deck - Cloze"}) == []
    assert logic.plan_notetype_changes(
        {"g1": "Study Deck - Basic"}, {"g1": "Study Deck - Basic"},
        {"Study Deck - Basic"}) == []


def test_reworded_front_with_stable_guid_updates_in_place_without_alias(anki, tmp_path):
    from internpearls import sync
    # Her card still shows the old wording; the rebuilt deck kept the GUID (the
    # stable-id convention) and ships a new front with NO front_aliases entry.
    her = anki.col.add_note("g1", _fields("Old wording",
                                          notes="annotation to keep"), [TAGS])
    folder = _write_source(tmp_path, {
        DECK: ("v2", [("g1", _fields("New wording, third revision"), TAGS)], None)})
    _configure(anki, folder)

    _sync(anki)

    assert len(anki.col.find_notes(f'"tag:{SCOPE}"')) == 1
    note = anki.col.note_by_guid("g1")
    assert note.id == her.id                              # same note: history kept
    assert note["Front"] == "New wording, third revision"
    assert note["Notes"] == "annotation to keep"


def test_front_alias_still_bridges_a_guid_mismatch(anki, tmp_path):
    from internpearls import sync
    # Her card predates stable GUIDs (guid differs) AND still shows the old front:
    # only the front_aliases fallback can match it.
    her = anki.col.add_note("her-old-guid", _fields("Old wording"), [TAGS])
    folder = _write_source(tmp_path, {
        DECK: ("v2", [("new-guid", _fields("New wording"), TAGS)], None)})
    manifest = json.loads(open(folder + "/manifest.json", encoding="utf8").read())
    manifest["front_aliases"] = {"New wording": "Old wording"}
    open(folder + "/manifest.json", "w", encoding="utf8").write(json.dumps(manifest))
    _configure(anki, folder)

    _sync(anki)

    assert len(anki.col.find_notes(f'"tag:{SCOPE}"')) == 1
    note = anki.col.get_note(her.id)
    assert note["Front"] == "New wording"
    assert note.guid == "her-old-guid"   # incoming guid was rewritten to hers


# ------------------------------------------------------------- template changes
NEW_CSS = ".card { color: rebeccapurple; }"


def test_template_change_applied_when_user_says_yes(anki, tmp_path):
    from internpearls import sync
    folder = _write_source(tmp_path, {
        DECK: ("v2", [("g1", _fields("Front one"), TAGS)],
               make_model(css=NEW_CSS))})
    _configure(anki, folder)
    _sync(anki)   # accepts the sync, then the template offer

    assert anki.col.models.all()[0]["css"] == NEW_CSS
    assert any("schema" in a or "full sync" in a for a in anki.gui.asks)


def test_template_change_declined_keeps_look_but_imports_content(anki, tmp_path):
    from internpearls import sync
    old_css = anki.col.models.all()[0]["css"]
    folder = _write_source(tmp_path, {
        DECK: ("v2", [("g1", _fields("Front one"), TAGS)],
               make_model(css=NEW_CSS))})
    _configure(anki, folder)
    _sync(anki, ask=lambda _text: False)   # accepts the sync, declines the template

    assert anki.col.models.all()[0]["css"] == old_css      # look unchanged
    assert anki.col.note_by_guid("g1")["Front"] == "Front one"   # content imported


def test_unchanged_template_never_prompts(anki, tmp_path):
    from internpearls import sync
    folder = _write_source(tmp_path, {
        DECK: ("v1", [("g1", _fields("Front one"), TAGS)], None)})
    _configure(anki, folder)

    _sync(anki)

    assert not any("Apply the new look" in a for a in anki.gui.asks)


class _StubAction:
    """Stands in for the real "Reconcile my decks" QAction in tests that don't build
    the actual menu (conftest.py deliberately never runs __init__.py) — just enough
    of QAction's surface (setText) for register_reconcile_action's caller."""
    def __init__(self):
        self.text = ""

    def setText(self, t):
        self.text = t


# ------------------------------------------------------------------- auto-sync
def test_auto_sync_applies_decks_inline_and_reports_by_tooltip(anki, tmp_path):
    from internpearls import background
    folder = _write_source(tmp_path, {
        DECK: ("v1", [("g1", _fields("Front one"), TAGS)], None)})
    anki.mw._config = {"decks_dir": folder, "auto_sync_decks": True}

    background._auto_sync_check()

    assert anki.col.note_by_guid("g1")["Front"] == "Front one"
    assert any("auto-synced 1 deck" in t for t in anki.gui.tooltips)
    assert anki.gui.asks == []   # unattended: must never open a dialog


def test_auto_sync_defers_a_template_change_and_nags_once(anki, tmp_path):
    from internpearls import background, sync
    folder = _write_source(tmp_path, {
        DECK: ("v2", [("g1", _fields("Front one"), TAGS)],
               make_model(css=NEW_CSS))})
    anki.mw._config = {"decks_dir": folder, "auto_sync_decks": True}

    background._auto_sync_check()

    # Deck NOT imported, NOT marked installed, template untouched, one tooltip.
    assert len(anki.col.find_notes(f'"tag:{SCOPE}"')) == 0
    assert json.load(open(background.INSTALLED, encoding="utf8")) == {}
    assert anki.col.models.all()[0]["css"] != NEW_CSS
    assert sum("card-template" in t for t in anki.gui.tooltips) == 1

    background._auto_sync_check()   # next poll: same pending deck

    assert sum("card-template" in t for t in anki.gui.tooltips) == 1   # no re-nag

    # A manual sync then picks it up and asks.
    _sync(anki)
    assert anki.col.models.all()[0]["css"] == NEW_CSS
    assert anki.col.note_by_guid("g1")["Front"] == "Front one"


def test_auto_sync_recovers_after_a_collection_revert_undoes_a_prior_sync(anki, tmp_path):
    """Same collection-revert scenario as the interactive sync test, driven through
    the unattended auto-sync poll instead — that path reads installed.json on the
    main thread (see background._auto_sync_check) before handing work to the
    background-thread-safe closures, so it needs its own regression coverage."""
    from internpearls import background
    folder = _write_source(tmp_path, {
        DECK: ("v1", [("g1", _fields("Front one"), TAGS)], None)})
    anki.mw._config = {"decks_dir": folder, "auto_sync_decks": True}
    background._auto_sync_check()
    assert len(anki.col.find_notes(f'"tag:{SCOPE}"')) == 1

    anki.col._notes.clear()
    anki.col._cards.clear()
    anki.gui.tooltips.clear()

    background._auto_sync_check()

    assert any("auto-synced 1 deck" in t for t in anki.gui.tooltips)
    assert len(anki.col.find_notes(f'"tag:{SCOPE}"')) == 1
    assert anki.col.note_by_guid("g1")["Front"] == "Front one"


def test_auto_sync_nudges_about_retired_cards_without_touching_them(anki, tmp_path):
    """Auto-sync never archives or relocates on its own (that stays a consented
    action — see reconcile_decks), so a retired/reorganized backlog can accumulate
    even while content stays fully synced, since a retirement or reorg can ship
    without bumping any deck's version. This is the one place that would ever notice
    such a backlog between manual checks: it should nudge (menu label + a one-time
    tooltip), never act on its own."""
    from internpearls import background, sync
    stub = _StubAction()
    sync.register_reconcile_action(stub)
    anki.col.add_note("old1", _fields("bulky crisis card"), TAGS.split())
    folder = _write_source(tmp_path, {}, retired={
        DECK: {"old1": {"identity": "bulky crisis card", "reason": "split",
                        "superseded_by": []}}})
    anki.mw._config = {"decks_dir": folder, "auto_sync_decks": True}

    background._auto_sync_check()

    assert stub.text == "Reconcile my decks (1 pending)"
    assert any("1 card is ready to tidy up" in t for t in anki.gui.tooltips)
    old = anki.col.note_by_guid("old1")
    assert anki.col._cards[old.card_ids()[0]].queue == 0   # untouched, not suspended
    assert not anki.col.imports


def test_auto_sync_does_not_renag_at_the_same_pending_count(anki, tmp_path):
    from internpearls import background, sync
    stub = _StubAction()
    sync.register_reconcile_action(stub)
    anki.col.add_note("old1", _fields("bulky crisis card"), TAGS.split())
    folder = _write_source(tmp_path, {}, retired={
        DECK: {"old1": {"identity": "bulky crisis card", "reason": "split",
                        "superseded_by": []}}})
    anki.mw._config = {"decks_dir": folder, "auto_sync_decks": True}
    background._auto_sync_check()
    assert len(anki.gui.tooltips) == 1

    background._auto_sync_check()

    assert len(anki.gui.tooltips) == 1               # same count, no repeat nag
    assert stub.text == "Reconcile my decks (1 pending)"   # label still reflects it


def test_auto_sync_renags_when_the_pending_count_grows(anki, tmp_path):
    from internpearls import background, sync
    stub = _StubAction()
    sync.register_reconcile_action(stub)
    anki.col.add_note("old1", _fields("bulky crisis card"), TAGS.split())
    folder = _write_source(tmp_path, {}, retired={
        DECK: {"old1": {"identity": "bulky crisis card", "reason": "split",
                        "superseded_by": []}}})
    anki.mw._config = {"decks_dir": folder, "auto_sync_decks": True}
    background._auto_sync_check()

    anki.col.add_note("old2", _fields("another retired card"), TAGS.split())
    folder2 = _write_source(tmp_path, {}, retired={DECK: {
        "old1": {"identity": "bulky crisis card", "reason": "split", "superseded_by": []},
        "old2": {"identity": "another retired card", "reason": "split",
                 "superseded_by": []}}})
    anki.mw._config = {"decks_dir": folder2, "auto_sync_decks": True}

    background._auto_sync_check()

    assert len(anki.gui.tooltips) == 2
    assert "2 cards are ready to tidy up" in anki.gui.tooltips[-1]
    assert stub.text == "Reconcile my decks (2 pending)"


# -------------------------------------------------------------- reconcile decks
RETIRED_DECK = "Intern Pearls::Intern Custom::Retired"
RETIRED_TAG = f"{SCOPE}::retired"


def _write_retired_source(tmp_path, retired, deck_moves=None):
    """A source folder whose manifest carries a `retired` ledger and/or a
    `deck_moves` ledger (schema 2) and no decks — reconcile only reads the
    ledgers, never downloads apkgs."""
    folder = tmp_path / "source"
    folder.mkdir(exist_ok=True)
    manifest = {"schema": 2, "decks": [], "front_aliases": {}, "retired": retired,
                "deck_moves": deck_moves or {}}
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf8")
    return str(folder)


def _her_card(anki, guid, front, deck=DECK):
    return anki.col.add_note(guid, _fields(front), TAGS.split(), deck=deck)


def test_reconcile_archives_retired_cards(anki, tmp_path):
    from internpearls import sync
    # She has the retired card old1 plus both its replacements and an unrelated card.
    _her_card(anki, "old1", "bulky crisis card")
    _her_card(anki, "new1a", "focused card A")
    _her_card(anki, "new1b", "focused card B")
    _her_card(anki, "keep", "an untouched card")
    folder = _write_retired_source(tmp_path, {
        DECK: {"old1": {"identity": "bulky crisis card", "reason": "split",
                        "superseded_by": ["new1a", "new1b"]}}})
    _configure(anki, folder)
    scm_before, notes_before = anki.col.scm, len(anki.col._notes)

    drive(anki, sync.reconcile_decks, _click_reconcile_button(accept=True))

    # old1 archived: suspended, moved to the Retired deck, tagged — never deleted.
    # Looked up fresh via guid rather than a note reference captured before drive():
    # the Runner's replay deepcopies mw.col on every pass, so a reference from before
    # drive() points at an orphaned pre-replay collection, not the one actually mutated.
    old = anki.col.note_by_guid("old1")
    cid = old.card_ids()[0]
    assert anki.col._cards[cid].queue == -1                       # suspended
    assert anki.col._cards[cid].did == anki.col.decks.id_for_name(RETIRED_DECK)
    assert RETIRED_TAG in old.tags
    # replacements and unrelated cards untouched (still in the review queue)
    for g in ("new1a", "new1b", "keep"):
        c = anki.col.note_by_guid(g).card_ids()[0]
        assert anki.col._cards[c].queue == 0
    # nothing deleted, and no schema bump (so no forced AnkiWeb full sync)
    assert len(anki.col._notes) == notes_before
    assert anki.col.scm == scm_before
    assert any("Archived <b>1 retired card</b>" in i for i in anki.gui.infos)


def test_reconcile_archives_a_retired_card_she_holds_under_an_older_guid(anki, tmp_path):
    """The counterpart bug to the stuck deck move: a learner whose copy predates the
    identity the retirement ledger is keyed by holds a different GUID, so a pure GUID
    match never finds her card. The replacements sync in, the retired one is never
    archived, and it duplicates them in every review indefinitely. Matching by front
    finds it, and archives HER note rather than looking for a GUID she doesn't have."""
    from internpearls import sync
    front = "bulky crisis card"
    card = _her_card(anki, "her_older_guid", front)
    _her_card(anki, "new1a", "focused card A")
    folder = _write_retired_source(tmp_path, {
        DECK: {"canonical_guid": {"identity": front, "reason": "split",
                                  "superseded_by": ["new1a"]}}})
    _configure(anki, folder)

    drive(anki, sync.reconcile_decks, _click_reconcile_button(accept=True))

    cid = card.card_ids()[0]
    assert anki.col._cards[cid].queue == -1                       # suspended
    assert anki.col._cards[cid].did == anki.col.decks.id_for_name(RETIRED_DECK)
    assert RETIRED_TAG in anki.col.note_by_guid("her_older_guid").tags
    assert anki.col.note_by_guid("new1a") is not None             # replacement untouched
    assert any("Archived <b>1 retired card</b>" in i for i in anki.gui.infos)


def test_reconcile_leaves_a_retired_card_alone_when_neither_guid_nor_front_match(
        anki, tmp_path):
    """The fallback only ever acts on a front it actually finds. A retired entry whose
    identity matches nothing in her collection (an image note's "image||answer", say)
    leaves every card of hers exactly as it was, the same as before front matching."""
    from internpearls import sync
    card = _her_card(anki, "her_older_guid", "a card she keeps")
    folder = _write_retired_source(tmp_path, {
        DECK: {"canonical_guid": {"identity": "pic.jpg||Femoral block",
                                  "reason": "split", "superseded_by": []}}})
    _configure(anki, folder)

    sync.reconcile_decks()

    cid = card.card_ids()[0]
    assert anki.col._cards[cid].queue == 0                        # still in review
    assert anki.col.decks.name(anki.col._cards[cid].did) == DECK
    assert any("No retired cards or reorganized decks found" in i for i in anki.gui.infos)


def _stranded_source(tmp_path, superseded):
    folder = tmp_path / "source"
    folder.mkdir(exist_ok=True)
    (folder / "manifest.json").write_text(json.dumps({
        "schema": 2, "decks": [], "front_aliases": {}, "retired": {},
        "deck_moves": {}, "superseded_fronts": superseded}), encoding="utf8")
    return str(folder)


def _sched(anki, note, **vals):
    card = anki.col.get_card(note.card_ids()[0])
    for k, v in vals.items():
        setattr(card, k, v)
    return card


def test_reconcile_moves_progress_onto_the_reworded_card_and_archives_the_old(
        anki, tmp_path):
    """The real-world case: her GUID drifted before the reword was frozen, so the
    reword imported as a second note and her review history sat on the dead copy while
    the live one started from zero. She should end up with one card, current wording,
    her progress intact."""
    from internpearls import sync
    old = _her_card(anki, "g_old", "old wording")
    new = _her_card(anki, "g_new", "new wording")
    _sched(anki, old, reps=4, ivl=12, due=90, factor=2300, lapses=1, type=2, queue=2)
    _configure(anki, _stranded_source(tmp_path, {"old wording": "new wording"}))

    drive(anki, sync.reconcile_decks, _click_reconcile_button(accept=True))

    kept = anki.col.get_card(anki.col.note_by_guid("g_new").card_ids()[0])
    assert (kept.reps, kept.ivl, kept.due, kept.factor, kept.lapses) == (4, 12, 90, 2300, 1)
    dead = anki.col.get_card(anki.col.note_by_guid("g_old").card_ids()[0])
    assert dead.queue == -1                                    # archived, not deleted
    assert dead.did == anki.col.decks.id_for_name(RETIRED_DECK)
    assert RETIRED_TAG in anki.col.note_by_guid("g_old").tags
    assert dead.reps == 4          # its own history is left on it, never cleared
    assert anki.col.note_by_guid("g_new").id in anki.col.updated_cards   # persisted
    assert len(anki.col._notes) == 2                           # nothing deleted
    assert any("Merged <b>1 reworded card</b>" in i for i in anki.gui.infos)


def test_reconcile_never_rolls_back_a_reworded_card_she_already_studied(anki, tmp_path):
    """If she's further along on the new wording than the old one, her progress there
    is what counts. The old copy still archives; it just doesn't overwrite anything."""
    from internpearls import sync
    old = _her_card(anki, "g_old", "old wording")
    new = _her_card(anki, "g_new", "new wording")
    _sched(anki, old, reps=1, ivl=2, due=10)
    _sched(anki, new, reps=9, ivl=40, due=200)
    _configure(anki, _stranded_source(tmp_path, {"old wording": "new wording"}))

    drive(anki, sync.reconcile_decks, _click_reconcile_button(accept=True))

    kept = anki.col.get_card(anki.col.note_by_guid("g_new").card_ids()[0])
    assert (kept.reps, kept.ivl, kept.due) == (9, 40, 200)     # untouched
    assert anki.col.get_card(anki.col.note_by_guid("g_old").card_ids()[0]).queue == -1


def test_reconcile_stranded_merge_is_idempotent(anki, tmp_path):
    from internpearls import sync
    old = _her_card(anki, "g_old", "old wording")
    _her_card(anki, "g_new", "new wording")
    _sched(anki, old, reps=4, ivl=12)
    folder = _stranded_source(tmp_path, {"old wording": "new wording"})
    _configure(anki, folder)
    drive(anki, sync.reconcile_decks, _click_reconcile_button(accept=True))
    kept_before = dict(vars(anki.col.get_card(
        anki.col.note_by_guid("g_new").card_ids()[0])))

    anki.gui.infos.clear()
    anki.gui.interactive = False
    sync.reconcile_decks()          # second run: the predecessor is tagged, so skipped

    assert vars(anki.col.get_card(
        anki.col.note_by_guid("g_new").card_ids()[0])) == kept_before
    assert any("nothing to tidy up" in i for i in anki.gui.infos)
    assert len(anki.col._notes) == 2


def test_reconcile_leaves_a_reworded_card_alone_when_she_only_has_the_old_one(
        anki, tmp_path):
    """Holding just the old wording is the import's job, not this one's: its front
    matching merges her card in place. Touching it here would fight that."""
    from internpearls import sync
    old = _her_card(anki, "g_old", "old wording")
    _configure(anki, _stranded_source(tmp_path, {"old wording": "new wording"}))

    sync.reconcile_decks()

    card = anki.col.get_card(old.card_ids()[0])
    assert card.queue == 0 and card.did == anki.col.decks.id_for_name(DECK)
    assert any("No retired cards or reorganized decks found" in i for i in anki.gui.infos)


def test_reconcile_run_manually_clears_the_auto_sync_nudge_label(anki, tmp_path):
    """A manual Reconcile run (bypassing auto-sync entirely, e.g. auto-sync is off)
    should also reset the persistent "N pending" menu label, not leave it stuck
    showing a stale count until some future auto-sync poll happens to run."""
    from internpearls import sync
    stub = _StubAction()
    sync.register_reconcile_action(stub)
    stub.setText("Reconcile my decks (1 pending)")
    _her_card(anki, "old1", "bulky crisis card")
    folder = _write_retired_source(tmp_path, {
        DECK: {"old1": {"identity": "bulky crisis card", "reason": "split",
                        "superseded_by": []}}})
    _configure(anki, folder)

    drive(anki, sync.reconcile_decks, _click_reconcile_button(accept=True))

    assert stub.text == "Reconcile my decks"


def test_reconcile_is_idempotent(anki, tmp_path):
    from internpearls import sync
    _her_card(anki, "old1", "bulky crisis card")
    folder = _write_retired_source(tmp_path, {
        DECK: {"old1": {"identity": "bulky crisis card", "reason": "split",
                        "superseded_by": []}}})
    _configure(anki, folder)
    drive(anki, sync.reconcile_decks, _click_reconcile_button(accept=True))
    # Fresh guid lookups throughout, not a note reference from before drive() — see the
    # comment in test_reconcile_archives_retired_cards for why that reference is stale.
    cid = anki.col.note_by_guid("old1").card_ids()[0]
    tags_after_first = list(anki.col.note_by_guid("old1").tags)

    anki.gui.infos.clear()
    anki.gui.interactive = False   # back to the plain shortcut for this simple re-run
    sync.reconcile_decks()                       # second run must not re-act

    assert anki.col.note_by_guid("old1").tags == tags_after_first  # no duplicate tag
    assert anki.col._cards[cid].queue == -1       # still suspended, untouched
    assert any("already archived" in i for i in anki.gui.infos)


def test_reconcile_reports_nothing_when_no_retired_cards_present(anki, tmp_path):
    from internpearls import sync
    _her_card(anki, "mine", "a card of my own")
    folder = _write_retired_source(tmp_path, {
        DECK: {"old1": {"identity": "bulky", "reason": "split",
                        "superseded_by": []}}})   # she doesn't have old1
    _configure(anki, folder)

    sync.reconcile_decks()

    assert any("No retired cards or reorganized decks found" in i for i in anki.gui.infos)
    assert not anki.col.imports                   # reconcile never imports


def test_reconcile_declined_leaves_everything_untouched(anki, tmp_path):
    from internpearls import sync
    _her_card(anki, "old1", "bulky crisis card")
    folder = _write_retired_source(tmp_path, {
        DECK: {"old1": {"identity": "bulky crisis card", "reason": "split",
                        "superseded_by": []}}})
    _configure(anki, folder)

    drive(anki, sync.reconcile_decks, _click_reconcile_button(accept=False))

    old = anki.col.note_by_guid("old1")   # fresh lookup — see note above about staleness
    cid = old.card_ids()[0]
    assert anki.col._cards[cid].queue == 0        # not suspended
    assert RETIRED_TAG not in old.tags            # not tagged


def test_reconcile_carries_notes_over_to_replacement_before_archiving(anki, tmp_path):
    from internpearls import sync
    anki.col.add_note("old1", _fields("bulky crisis card", notes="her mnemonic"), TAGS.split())
    _her_card(anki, "new1a", "focused card A")
    folder = _write_retired_source(tmp_path, {
        DECK: {"old1": {"identity": "bulky crisis card", "reason": "split",
                        "superseded_by": ["new1a"]}}})
    _configure(anki, folder)

    drive(anki, sync.reconcile_decks, _click_reconcile_button(accept=True))

    assert anki.col.note_by_guid("new1a")["Notes"] == "her mnemonic"
    assert any("1 personal note carried over" in i for i in anki.gui.infos)


def test_reconcile_does_not_overwrite_replacements_own_notes(anki, tmp_path):
    from internpearls import sync
    anki.col.add_note("old1", _fields("bulky crisis card", notes="her old mnemonic"),
                      TAGS.split())
    anki.col.add_note("new1a", _fields("focused card A", notes="a note she already wrote"),
                      TAGS.split(), deck=DECK)
    folder = _write_retired_source(tmp_path, {
        DECK: {"old1": {"identity": "bulky crisis card", "reason": "split",
                        "superseded_by": ["new1a"]}}})
    _configure(anki, folder)

    drive(anki, sync.reconcile_decks, _click_reconcile_button(accept=True))

    assert anki.col.note_by_guid("new1a")["Notes"] == "a note she already wrote"


def test_reconcile_names_every_card_as_a_row_and_stays_clickable(anki, tmp_path):
    """A large first-run backlog (e.g. 90 cards relocated by a single reorg) used to
    build a bullet list inside a plain askUser() box, which has no scroll area, so a
    long enough one grew the dialog past the screen with its buttons unreachable. It
    was capped at 15 for that reason. The rows stream and the buttons sit outside the
    scroll area now, so every card is named, none of them is a bullet, and the accept
    button is still reachable with a backlog well past that old cap."""
    from internpearls import widgets
    retired = {}
    for i in range(25):
        _her_card(anki, f"old{i}", f"bulky card {i}")
        retired[f"old{i}"] = {"identity": f"bulky card {i}", "reason": "split",
                              "superseded_by": []}
    folder = _write_retired_source(tmp_path, {DECK: retired})
    _configure(anki, folder)

    texts = _label_texts(_reconcile_tree(anki))

    assert not [t for t in texts if "<li>" in t or "<ul>" in t]
    assert set(t for t in texts if t.startswith("bulky card ")) == {
        f"bulky card {i}" for i in range(25)}
    assert sum(1 for t in texts if t == widgets.CHIPS["retired"]) == 25
    assert any("one-time catch-up" in t for t in texts)
    assert any("Archived <b>25 retired cards</b>" in i for i in anki.gui.infos)


def test_reconcile_marks_each_row_with_what_happens_to_it(anki, tmp_path):
    """The same two chips Update my decks puts on these very cards, since both screens
    read the same two ledgers: a card being archived is RETIRED, one being relocated is
    MOVED, and each row's deck detail is the trailing column rather than a parenthesis
    in its own sentence."""
    from internpearls import widgets
    _her_card(anki, "old1", "bulky crisis card")
    _her_card(anki, "g1", "a card whose deck moved", deck=DECK)
    folder = _write_retired_source(
        tmp_path,
        {DECK: {"old1": {"identity": "bulky crisis card", "reason": "split",
                         "superseded_by": []}}},
        deck_moves={"g1": {"from": DECK, "to": NEW_DECK}})
    _configure(anki, folder)

    texts = _label_texts(_reconcile_tree(anki))

    assert [t for t in texts if t in widgets.CHIPS.values()] == [
        widgets.CHIPS["retired"], widgets.CHIPS["moved"]]
    assert "bulky crisis card" in texts and "a card whose deck moved" in texts
    assert "Pharm" in texts                  # the retired card's own deck
    assert "→ Regional" in texts             # where the moved card is going


# ---------------------------------------------------------- reconcile: deck moves
NEW_DECK = "Intern Pearls::Intern Custom::Regional"


def test_reconcile_moves_card_to_reorganized_deck(anki, tmp_path):
    from internpearls import sync
    card = _her_card(anki, "g1", "Lidocaine — onset time?", deck=DECK)
    folder = _write_retired_source(tmp_path, {}, deck_moves={
        "g1": {"from": DECK, "to": NEW_DECK}})
    _configure(anki, folder)
    scm_before = anki.col.scm

    drive(anki, sync.reconcile_decks, _click_reconcile_button(accept=True))

    cid = card.card_ids()[0]
    assert anki.col.decks.name(anki.col._cards[cid].did) == NEW_DECK
    assert anki.col.scm == scm_before              # schema-neutral, no forced full sync
    assert any("Moved <b>1 card</b>" in i for i in anki.gui.infos)


def test_reconcile_move_is_idempotent(anki, tmp_path):
    from internpearls import sync
    _her_card(anki, "g1", "Lidocaine — onset time?", deck=DECK)
    folder = _write_retired_source(tmp_path, {}, deck_moves={
        "g1": {"from": DECK, "to": NEW_DECK}})
    _configure(anki, folder)
    drive(anki, sync.reconcile_decks, _click_reconcile_button(accept=True))

    anki.gui.infos.clear()
    anki.gui.interactive = False   # back to the plain shortcut for this simple re-run
    sync.reconcile_decks()                        # second run: card is already at `to`

    assert any("No retired cards or reorganized decks found" in i for i in anki.gui.infos)


def test_reconcile_does_not_move_a_card_she_relocated_herself(anki, tmp_path):
    from internpearls import sync
    her_own_deck = "My Own Custom Deck"
    card = _her_card(anki, "g1", "Lidocaine — onset time?", deck=her_own_deck)
    folder = _write_retired_source(tmp_path, {}, deck_moves={
        "g1": {"from": DECK, "to": NEW_DECK}})
    _configure(anki, folder)

    sync.reconcile_decks()

    cid = card.card_ids()[0]
    assert anki.col.decks.name(anki.col._cards[cid].did) == her_own_deck
    assert any("No retired cards or reorganized decks found" in i for i in anki.gui.infos)


def test_reconcile_move_declined_leaves_deck_untouched(anki, tmp_path):
    from internpearls import sync
    card = _her_card(anki, "g1", "Lidocaine — onset time?", deck=DECK)
    folder = _write_retired_source(tmp_path, {}, deck_moves={
        "g1": {"from": DECK, "to": NEW_DECK}})
    _configure(anki, folder)

    drive(anki, sync.reconcile_decks, _click_reconcile_button(accept=False))

    cid = card.card_ids()[0]
    assert anki.col.decks.name(anki.col._cards[cid].did) == DECK


def test_reconcile_relocates_a_stuck_card_by_front_when_guid_changed(anki, tmp_path):
    """The real-world bug: a card whose deck source changed its id_seed has a
    different GUID in a long-time learner's collection than the move ledger is keyed
    by. A pure GUID match misses it, so it sits stuck at `from` and its new deck is
    re-offered forever. With the ledger carrying the note's front, reconcile matches
    the learner's card by front (like content-sync does) and relocates it."""
    from internpearls import sync
    front = "Lidocaine onset time?"
    # She holds the card under an OLD guid (pre-seed-change), sitting at `from`.
    card = _her_card(anki, "old_v1_guid", front, deck=DECK)
    # The ledger is keyed by the CURRENT (v2) guid she doesn't have, but carries front.
    folder = _write_retired_source(tmp_path, {}, deck_moves={
        "new_v2_guid": {"from": DECK, "to": NEW_DECK, "front": front}})
    _configure(anki, folder)

    drive(anki, sync.reconcile_decks, _click_reconcile_button(accept=True))

    cid = card.card_ids()[0]
    assert anki.col.decks.name(anki.col._cards[cid].did) == NEW_DECK
    assert any("Moved <b>1 card</b>" in i for i in anki.gui.infos)


def test_reconcile_leaves_guid_mismatched_card_alone_without_front(anki, tmp_path):
    """Graceful fallback: an older manifest whose deck_moves entries carry no front
    can't match a GUID-mismatched card, so reconcile leaves it exactly where it is
    rather than guessing. Same as the behavior before front matching existed."""
    from internpearls import sync
    card = _her_card(anki, "old_v1_guid", "Lidocaine onset time?", deck=DECK)
    folder = _write_retired_source(tmp_path, {}, deck_moves={
        "new_v2_guid": {"from": DECK, "to": NEW_DECK}})   # no "front" key
    _configure(anki, folder)

    sync.reconcile_decks()

    cid = card.card_ids()[0]
    assert anki.col.decks.name(anki.col._cards[cid].did) == DECK
    assert any("No retired cards or reorganized decks found" in i for i in anki.gui.infos)


def test_reconcile_dialog_uses_the_palette_not_a_css_keyword(anki, tmp_path):
    """Both the retired-card list and the deck-move list in the confirmation used to
    hardcode the CSS keyword gray for their secondary detail text. That detail is each
    row's own trailing column now, so the colour is on the label rather than inside the
    string, and it still has to be asked for by role."""
    from internpearls import palette
    active = palette.colors()
    _her_card(anki, "old1", "bulky crisis card")
    _her_card(anki, "g1", "Lidocaine onset time?", deck=DECK)
    folder = _write_retired_source(
        tmp_path,
        {DECK: {"old1": {"identity": "bulky crisis card", "reason": "split",
                         "superseded_by": []}}},
        deck_moves={"g1": {"from": DECK, "to": NEW_DECK}})
    _configure(anki, folder)

    tree = _reconcile_tree(anki)

    trailing = [n for n in _label_nodes(tree) if n["text"] in ("Pharm", "→ Regional")]
    assert len(trailing) == 2, "both rows carry their detail in a trailing column"
    for node in trailing:
        assert "gray" not in node["style"]
        assert active["muted"] in node["style"]


def test_stranded_block_uses_the_palette_not_a_css_keyword(anki, tmp_path):
    """The reworded-pair section of the confirmation (a card she holds in both an old
    and a new wording) used to hardcode the CSS keyword gray too. Both wordings stay in
    the row's own primary line rather than splitting across the trailing column, since
    a card front is long enough to wrap and that column is not."""
    from internpearls import palette
    active = palette.colors()
    _her_card(anki, "g_old", "old wording")
    _her_card(anki, "g_new", "new wording")
    _configure(anki, _stranded_source(tmp_path, {"old wording": "new wording"}))

    texts = _label_texts(_reconcile_tree(anki))

    line = next(t for t in texts if "old wording" in t and "new wording" in t)
    assert "color:gray" not in line
    assert active["muted"] in line


# ------------------------------------------------------------- update decks (unified)
def test_update_confirmation_lists_reworded_pairs_as_rows(anki, tmp_path):
    """The reworded pairs were the last group on this screen to arrive as a paragraph
    with a bulleted list inside it, sitting above the list rather than in it. They are
    cards like everything else here, so each pair is a row, chipped RETIRED the way the
    same finding reads on Reconcile my decks: that is what happens to the older wording
    once its progress has moved across."""
    from internpearls import sync
    from internpearls.widgets import CHIPS
    _her_card(anki, "g_old", "old wording")
    _her_card(anki, "g_new", "new wording")
    _configure(anki, _stranded_source(tmp_path, {"old wording": "new wording"}))
    anki.gui.interactive = True
    seen = {}

    def respond(p):
        if p["kind"] != "dialog":
            return {}
        seen.setdefault("texts", _label_texts(p["tree"]))
        return {"events": [{"id": _find(p["tree"], t="button", label="Cancel")["id"],
                            "click": True}]}

    drive(anki, sync.update_decks, respond)

    texts = seen["texts"]
    assert not [t for t in texts if "<li>" in t or "<ul>" in t]
    assert any("old wording" in t and "new wording" in t for t in texts), \
        "the pair belongs in one row: a card front is long enough to wrap"
    assert CHIPS["retired"] in texts, "the row says what happens to the older wording"


def test_update_decks_syncs_and_reconciles_in_one_pass(anki, tmp_path):
    """The unified flow's whole point: one confirmation, one click, and content sync
    runs before archiving so a retired card's replacement — synced in during this
    exact same call — is there to carry her personal note onto, and a reorganized
    card relocates too. Three independent effects from one accepted dialog."""
    from internpearls import sync
    anki.col.add_note("old1", _fields("bulky crisis card", notes="her mnemonic"),
                      TAGS.split())
    moved_deck = "Intern Pearls::Intern Custom::Regional (old)"
    _her_card(anki, "moved1", "a card that moved decks", deck=moved_deck)
    folder = _write_source(
        tmp_path, {DECK: ("v1", [("new1a", _fields("focused card A"), TAGS)], None)},
        retired={DECK: {"old1": {"identity": "bulky crisis card", "reason": "split",
                                 "superseded_by": ["new1a"]}}},
        deck_moves={"moved1": {"from": moved_deck, "to": DECK}})
    _configure(anki, folder)

    trees = _update(anki)

    # Content synced.
    assert anki.col.note_by_guid("new1a")["Front"] == "focused card A"
    # Retired card archived, and her note carried over onto the replacement that was
    # only imported moments earlier in this same call.
    old = anki.col.note_by_guid("old1")
    assert anki.col._cards[old.card_ids()[0]].queue == -1
    assert RETIRED_TAG in old.tags
    assert anki.col.note_by_guid("new1a")["Notes"] == "her mnemonic"
    # Reorganized card relocated.
    moved = anki.col.note_by_guid("moved1")
    assert anki.col.decks.name(anki.col.get_card(moved.card_ids()[0]).did) == DECK
    assert "Update complete" in _summary_text(trees)


def test_update_decks_confirmation_shows_real_kept_new_counts(anki, tmp_path):
    """The confirmation must download and match each pending deck before showing
    it, the same way Manage decks' old "Check what will sync" preview did — a
    static total card count can't tell the learner how much of an update is
    actually new to them versus already-matched content."""
    from internpearls import sync
    anki.col.add_note("g1", _fields("Front one"), TAGS.split())   # she already has g1
    folder = _write_source(tmp_path, {
        DECK: ("v2", [("g1", _fields("Front one"), TAGS),
                      ("g2", _fields("Front two"), TAGS)], None)})
    _configure(anki, folder)
    anki.gui.interactive = True
    seen = {}

    def respond(p):
        if p["kind"] != "dialog":
            return {}
        done = _dismiss_result(p["tree"])   # the end-of-run summary: nothing to inspect
        if done:
            return done
        label = next((n for n in _walk(p["tree"]) if n.get("t") == "label"
                     and "kept" in (n.get("text") or "")), None)
        seen["text"] = label["text"] if label else None
        seen.setdefault("all", _labels(p["tree"]))
        btn = _find(p["tree"], t="button", label="Update")
        return {"events": [{"id": btn["id"], "click": True}]}

    drive(anki, sync.update_decks, respond)

    assert seen.get("text"), "expected a kept/new preview line in the confirmation"
    assert "1 kept" in seen["text"] and "1 new" in seen["text"]
    # A row per deck under its own heading, not an indented bullet list dropped into
    # the label above the card rows it introduces.
    assert "1 deck has updates" in seen["all"]
    assert "<li>" not in seen["all"] and "<ul>" not in seen["all"], (
        "the per-deck counts read as a row's trailing column now, not as a bulleted "
        "list inside one label")


def _labels(tree):
    """Every label's text and every button's label in one dialog, joined, for asserting
    on what it showed. Buttons are included because a button's own wording (e.g. which
    kind of cards "Review" offers) is part of what the dialog tells the reader."""
    return "\n".join((n.get("text") or n.get("label") or "")
                     for n in _walk(tree) if n.get("t") in ("label", "button"))


def test_update_decks_confirmation_names_the_new_cards_not_just_a_count(anki, tmp_path):
    """Retired and relocated cards were always listed by name; a card being ADDED used
    to arrive as a bare count in its own bullet section. Now every new card is a row of
    its own, right on the confirmation, with a NEW chip. The kept card must NOT show a
    row: this list is only what's new, and padding it with cards she already has would
    bury that."""
    from internpearls import sync
    from internpearls.widgets import CHIPS
    anki.col.add_note("g1", _fields("Front one"), TAGS.split())   # she already has g1
    folder = _write_source(tmp_path, {
        DECK: ("v2", [("g1", _fields("Front one"), TAGS),
                      ("g2", _fields("Front two"), TAGS)], None)})
    _configure(anki, folder)
    anki.gui.interactive = True
    seen = {}

    def respond(p):
        if p["kind"] != "dialog":
            return {}
        done = _dismiss_result(p["tree"])
        if done:
            return done
        seen.setdefault("text", _labels(p["tree"]))
        btn = _find(p["tree"], t="button", label="Update")
        return {"events": [{"id": btn["id"], "click": True}]}

    drive(anki, sync.update_decks, respond)

    assert "Front two" in seen["text"], "the new card should show as a row, not just a count"
    assert CHIPS["new"] in seen["text"], "the new row should carry its NEW chip"
    assert "Front one" not in seen["text"], "a card she already has isn't new, so it has no row"


def test_update_decks_confirmation_counts_and_names_changed_cards(anki, tmp_path):
    """A card whose Back was rewritten upstream used to import silently: it matched, so
    it counted as "kept" and nothing said its content had moved. It must still show up,
    now as a row of its own with an UPDATED chip rather than its own bullet section."""
    from internpearls import sync
    from internpearls.widgets import CHIPS
    anki.col.add_note("g1", _fields("Front one", back="the old answer"), TAGS.split())
    folder = _write_source(tmp_path, {
        DECK: ("v2", [("g1", _fields("Front one", back="the new answer"), TAGS)], None)})
    _configure(anki, folder)
    anki.gui.interactive = True
    seen = {}

    def respond(p):
        if p["kind"] != "dialog":
            return {}
        seen.setdefault("text", _labels(p["tree"]))
        return {"events": [{"id": _find(p["tree"], t="button", label="Cancel")["id"],
                            "click": True}]}

    drive(anki, sync.update_decks, respond)

    assert "1 changing" in seen["text"], "the per-deck line should count changed cards"
    assert CHIPS["changed"] in seen["text"], "the changed row should carry its chip"
    assert "Front one" in seen["text"]


def test_update_decks_confirmation_shows_retired_and_moved_cards_as_rows(anki, tmp_path):
    """Retired and relocated cards used to sit below the confirmation as their own
    bulleted sections; now they're rows in the same list as the new and changed cards,
    chipped RETIRED and MOVED. A moved row names the deck it's heading to, and neither
    kind carries a caret to open, since a retired or relocated card is known only by
    its front and a deck, with nothing more to read out of the collection for it."""
    from internpearls import sync
    from internpearls.widgets import CHIPS
    old_deck = "Intern Pearls::Intern Custom::Regional (old)"
    anki.col.add_note("old1", _fields("bulky crisis card"), TAGS.split())
    _her_card(anki, "moved1", "a card that moved decks", deck=old_deck)
    folder = _write_source(
        tmp_path, {},
        retired={DECK: {"old1": {"identity": "bulky crisis card", "reason": "split",
                                 "superseded_by": []}}},
        deck_moves={"moved1": {"from": old_deck, "to": DECK}})
    _configure(anki, folder)
    anki.gui.interactive = True
    seen = {}

    def respond(p):
        if p["kind"] != "dialog":
            return {}
        seen["tree"] = p["tree"]
        seen["text"] = _labels(p["tree"])
        return {"events": [{"id": _find(p["tree"], t="button", label="Cancel")["id"],
                            "click": True}]}

    drive(anki, sync.update_decks, respond)

    assert "bulky crisis card" in seen["text"] and CHIPS["retired"] in seen["text"]
    assert "a card that moved decks" in seen["text"] and CHIPS["moved"] in seen["text"]
    assert "→ Pharm" in seen["text"]     # the moved row names its destination deck

    # Neither kind is bulleted text below the summary anymore. Matched on the wording
    # each block opens with rather than on its count, which reads differently for one
    # card than for several.
    assert "still in your collection" not in seen["text"]
    assert "since been reorganized" not in seen["text"]

    # Nor does either kind have a heading of its own: the chip on the row already says
    # what it is, so the heading is free to say which deck it belongs to. The retired
    # card's own deck heads its row here, and the moved card's the deck it is leaving.
    lines = seen["text"].split("\n")
    assert "Retired" not in lines and "Moved" not in lines, (
        f"a kind still heads its own section: {lines}")
    assert "Pharm" in lines and "Regional (old)" in lines

    # Neither row expands: no card in this run has a caret to open.
    buttons = [n.get("label") for n in _walk(seen["tree"]) if n.get("t") == "button"]
    assert buttons == ["Update", "Cancel"], (
        f"expected only Update/Cancel, no expand caret, got {buttons}")


def test_update_decks_confirmation_uses_the_palette_not_a_css_keyword(anki, tmp_path):
    """The retired-cards and deck-move sections of the confirmation used to hardcode
    the CSS keyword gray for their secondary detail text. Both are rows in the same
    list as the new and changed cards now, chipped RETIRED and MOVED rather than their
    own bulleted sections, so a moved row's destination is what carries the colour
    check today; the fix landed in widgets.simple_row, which sets it through the row's
    own stylesheet rather than an inline span, so this reads both a rendered label's
    text and its stylesheet. One source carrying a new card, a changed card, a retired
    card, and a moved card at once exercises every row kind in a single render of the
    confirmation."""
    from internpearls import palette, sync
    from internpearls.widgets import CHIPS
    active = palette.colors()
    old_deck = "Intern Pearls::Intern Custom::Regional (old)"
    anki.col.add_note("g1", _fields("Front one", back="the old answer"), TAGS.split())
    anki.col.add_note("old1", _fields("bulky crisis card"), TAGS.split())
    _her_card(anki, "moved1", "a card that moved decks", deck=old_deck)
    folder = _write_source(
        tmp_path,
        {DECK: ("v2", [("g1", _fields("Front one", back="the new answer"), TAGS),
                       ("g2", _fields("Front two"), TAGS)], None)},
        retired={DECK: {"old1": {"identity": "bulky crisis card", "reason": "split",
                                 "superseded_by": []}}},
        deck_moves={"moved1": {"from": old_deck, "to": DECK}})
    _configure(anki, folder)
    anki.gui.interactive = True
    seen = {}

    def respond(p):
        if p["kind"] != "dialog":
            return {}
        seen.setdefault("text", _labels(p["tree"]))
        seen.setdefault("styles", "\n".join(n.get("style") or "" for n in _walk(p["tree"])))
        return {"events": [{"id": _find(p["tree"], t="button", label="Cancel")["id"],
                            "click": True}]}

    drive(anki, sync.update_decks, respond)

    assert "Front two" in seen["text"] and CHIPS["new"] in seen["text"]      # new row
    assert "Front one" in seen["text"] and CHIPS["changed"] in seen["text"]  # changed row
    assert "bulky crisis card" in seen["text"] and CHIPS["retired"] in seen["text"]
    assert "a card that moved decks" in seen["text"] and CHIPS["moved"] in seen["text"]
    assert "→ Pharm" in seen["text"]     # the moved row names its destination deck

    # Ordering: added and changing cards read first, then what is archived, then what
    # is moving, the sequence the brief for this row asks for.
    new_idx = seen["text"].index(CHIPS["new"])
    changed_idx = seen["text"].index(CHIPS["changed"])
    retired_idx = seen["text"].index(CHIPS["retired"])
    moved_idx = seen["text"].index(CHIPS["moved"])
    assert max(new_idx, changed_idx) < retired_idx < moved_idx, (
        "retired cards should read after the content updates, and moved cards after "
        "the retired ones")

    assert "color:gray" not in seen["text"] and "color:gray" not in seen["styles"]
    assert active["muted"] in seen["text"] or active["muted"] in seen["styles"]


def test_update_decks_does_not_call_an_untouched_card_changed(anki, tmp_path):
    from internpearls import sync
    from internpearls.widgets import CHIPS
    anki.col.add_note("g1", _fields("Front one"), TAGS.split())
    folder = _write_source(tmp_path, {
        DECK: ("v2", [("g1", _fields("Front one"), TAGS),
                      ("g2", _fields("Front two"), TAGS)], None)})
    _configure(anki, folder)
    anki.gui.interactive = True
    seen = {}

    def respond(p):
        if p["kind"] != "dialog":
            return {}
        seen.setdefault("text", _labels(p["tree"]))
        return {"events": [{"id": _find(p["tree"], t="button", label="Cancel")["id"],
                            "click": True}]}

    drive(anki, sync.update_decks, respond)

    assert "will change" not in seen["text"]
    assert "Front two" in seen["text"] and CHIPS["new"] in seen["text"], (
        "with nothing changed the untouched card's row should still show, tagged new")
    assert "Review" not in seen["text"], "the old Review button is gone"


def test_update_decks_lists_both_new_and_changed_cards_inline(anki, tmp_path):
    """The old "Review N card(s)" button opened a second window listing exactly these
    two cards; now they are rows right on the confirmation itself, one tagged NEW and
    one tagged UPDATED."""
    from internpearls import sync
    from internpearls.widgets import CHIPS
    anki.col.add_note("g1", _fields("Front one", back="the old answer"), TAGS.split())
    folder = _write_source(tmp_path, {
        DECK: ("v2", [("g1", _fields("Front one", back="the new answer"), TAGS),
                      ("g2", _fields("Front two"), TAGS)], None)})
    _configure(anki, folder)
    anki.gui.interactive = True
    seen = {}

    def respond(p):
        if p["kind"] != "dialog":
            return {}
        seen.setdefault("text", _labels(p["tree"]))
        return {"events": [{"id": _find(p["tree"], t="button", label="Cancel")["id"],
                            "click": True}]}

    drive(anki, sync.update_decks, respond)

    assert "Front one" in seen["text"] and CHIPS["changed"] in seen["text"]
    assert "Front two" in seen["text"] and CHIPS["new"] in seen["text"]
    assert "Review" not in seen["text"], "the old Review button is gone"


def test_update_decks_lists_a_changed_only_card_inline(anki, tmp_path):
    """A deck where every pending card is a rewrite of one she already has must still
    show that card, tagged UPDATED, with no NEW chip anywhere since nothing is new."""
    from internpearls import sync
    from internpearls.widgets import CHIPS
    anki.col.add_note("g1", _fields("Front one", back="the old answer"), TAGS.split())
    folder = _write_source(tmp_path, {
        DECK: ("v2", [("g1", _fields("Front one", back="the new answer"), TAGS)], None)})
    _configure(anki, folder)
    anki.gui.interactive = True
    seen = {}

    def respond(p):
        if p["kind"] != "dialog":
            return {}
        seen.setdefault("text", _labels(p["tree"]))
        return {"events": [{"id": _find(p["tree"], t="button", label="Cancel")["id"],
                            "click": True}]}

    drive(anki, sync.update_decks, respond)

    assert "Front one" in seen["text"] and CHIPS["changed"] in seen["text"]
    assert CHIPS["new"] not in seen["text"]
    assert "Review" not in seen["text"], "the old Review button is gone"


def test_review_is_read_only_when_feedback_is_off(anki, tmp_path):
    """Default: the confirmation previews the incoming cards inline, with a cloze
    note's deletions filled in rather than blanked, and collects nothing."""
    from internpearls import sync
    cloze_model = make_model(name="Study Deck - Cloze",
                             fields=["Text", "Why", "Image", "Dosing", "Notes"])
    folder = _write_source(tmp_path, {
        DECK: ("v1", [("g4", ["The {{c1::lumbar}} plexus is compressed.",
                              "why text", "", "500 mg", ""], TAGS)], cloze_model)})
    _configure(anki, folder)
    anki.gui.interactive = True
    seen = {}

    def respond(p):
        if p["kind"] != "dialog":
            return {}
        done = _dismiss_result(p["tree"])
        if done:
            return done
        seen["screen"] = _labels(p["tree"])
        seen["boxes"] = [n for n in _walk(p["tree"]) if n.get("t") == "textarea"]
        return {"events": [{"id": _find(p["tree"], t="button", label="Update")["id"],
                            "click": True}]}

    drive(anki, sync.update_decks, respond)

    # The deletion is filled in, not blanked, and the raw cloze markup never leaks,
    # in the deck's own cloze color so the row reads as study.
    from internpearls import palette
    assert "lumbar" in seen["screen"]
    assert "{{c1::" not in seen["screen"]
    assert palette.colors()["accent"] in seen["screen"]
    # No feedback box anywhere, and nothing offered to the clipboard.
    assert not seen["boxes"], "no feedback box should render when the toggle is off"
    assert "flagged" not in seen["screen"]
    assert not anki.gui.clipboard, "nothing should reach the clipboard"
    # It's still just a preview: the card still imports once Update is chosen.
    assert len(anki.col.find_notes(f'"tag:{SCOPE}"')) == 1


def test_review_renders_a_new_cards_picture_once_its_row_is_opened(anki, tmp_path):
    """The .apkg the preview already downloaded is what the pictures come out of, so
    opening a row costs a local read rather than another fetch."""
    from internpearls import sync
    fields = ["Front with a picture", 'see <img src="sample-a.jpg">', "why", "",
              "Pharm", "", ""]
    folder = _write_source(tmp_path, {
        DECK: ("v1", [("g1", fields, TAGS)], None, {"sample-a.jpg": b"bytes"})})
    _configure(anki, folder)
    anki.gui.interactive = True
    seen = {}

    def respond(p):
        if p["kind"] != "dialog":
            return {}
        tree = p["tree"]
        if "opened" not in seen:
            caret = next(n for n in _walk(tree) if n.get("t") == "button"
                        and n.get("label") in ("▸", "▾"))
            seen["opened"] = True
            return {"events": [{"id": caret["id"], "click": True}]}
        seen["screen"] = _labels(tree)
        return {"events": [{"id": _find(tree, t="button", label="Cancel")["id"],
                            "click": True}]}

    drive(anki, sync.update_decks, respond)

    assert "<img src=" in seen["screen"], (
        "the row was opened but its picture is still only named: the .apkg path is not "
        "reaching the row")
    assert "sample-a.jpg" in seen["screen"]


def test_review_rules_separate_cards_without_trailing_the_last_one(anki, tmp_path):
    """One hairline between each pair of cards: three cards means two rules, not
    three, and never one per widget. The rule used to be a border on the row itself,
    where a selector-less stylesheet propagated into the row's children and drew a
    second, inset copy under every card."""
    from internpearls import sync
    cloze_model = make_model(name="Study Deck - Cloze",
                             fields=["Text", "Why", "Image", "Dosing", "Notes"])
    notes = [(f"g{i}", [f"Card {i} has a {{{{c1::deletion}}}}.", "", "", "", ""], TAGS)
             for i in range(3)]
    folder = _write_source(tmp_path, {DECK: ("v1", notes, cloze_model)})
    _configure(anki, folder)
    anki.gui.interactive = True
    seen = {}

    def respond(p):
        if p["kind"] != "dialog":
            return {}
        done = _dismiss_result(p["tree"])
        if done:
            return done
        seen.setdefault("hlines", [n for n in _walk(p["tree"]) if n.get("t") == "hline"])
        return {"events": [{"id": _find(p["tree"], t="button", label="Update")["id"],
                            "click": True}]}

    drive(anki, sync.update_decks, respond)

    assert len(seen["hlines"]) == 2, "three cards separate with two rules"


def test_review_collects_feedback_when_the_toggle_is_on(anki, tmp_path):
    """Existing behavior, now opt-in: boxes appear and the digest is offered on close.
    She sees the answer and the reasoning, not just the front, because "this card is
    wrong" is a judgment you can't make from a prompt alone."""
    from internpearls import sync
    folder = _write_source(tmp_path, {
        DECK: ("v1", [("g2", _fields("Front two", "the answer"), TAGS)], None)})
    _configure(anki, folder)
    anki.mw._config["collect_card_feedback"] = True
    anki.gui.interactive = True
    seen = {}

    def respond(p):
        if p["kind"] != "dialog":
            return {}
        title, tree = p.get("title") or "", p["tree"]
        if "card feedback" in title:
            return {"events": [{"id": _find(tree, t="button", label="Close")["id"],
                                "click": True}]}
        if "typed" not in seen:
            seen["screen"] = _labels(tree)
            box = next(n for n in _walk(tree) if n.get("t") == "textarea")
            seen["typed"] = True
            return {"events": [{"id": box["id"], "value": "dose is wrong"}]}
        seen["after"] = _labels(tree)
        return {"events": [{"id": _find(tree, t="button", label="Update")["id"],
                            "click": True}]}

    drive(anki, sync.update_decks, respond)

    # Her card's primary line, tag, answer, and why are all there, but the old field
    # captions are gone: dropping that chrome is exactly what this rework did.
    assert "Front two" in seen["screen"]
    assert "Pharm" in seen["screen"]
    assert "the answer" in seen["screen"] and "why" in seen["screen"]
    assert "Back" not in seen["screen"], "field names shouldn't be captioned anymore"
    # Typing into her box updates the confirmation's own flagged count live, since
    # there is no longer a separate review dialog whose closing used to trigger it.
    assert "1 card flagged" in seen.get("after", "")
    # And the digest names the deck, the card, its id, and what she said.
    digest = anki.gui.clipboard[-1]
    assert "Front two" in digest and "dose is wrong" in digest and "g2" in digest
    assert "Pharm" in digest
    # Flagging is feedback, not a veto: the card still imported.
    assert len(anki.col.find_notes(f'"tag:{SCOPE}"')) == 1


def test_update_decks_declined_still_returns_the_feedback_she_wrote(anki, tmp_path):
    """If she reads the new cards, flags one and then backs out, the flag is the most
    interesting thing that happened in the whole run. Dropping it because she said no
    would throw away the only part that clicking Update again later can't reproduce."""
    from internpearls import sync
    folder = _write_source(tmp_path, {
        DECK: ("v1", [("g2", _fields("Front two"), TAGS)], None)})
    _configure(anki, folder)
    anki.mw._config["collect_card_feedback"] = True
    anki.gui.interactive = True
    seen = {}

    def respond(p):
        if p["kind"] != "dialog":
            return {}
        title, tree = p.get("title") or "", p["tree"]
        if "card feedback" in title:
            return {"events": [{"id": _find(tree, t="button", label="Close")["id"],
                                "click": True}]}
        if "typed" not in seen:
            box = next(n for n in _walk(tree) if n.get("t") == "textarea")
            seen["typed"] = True
            return {"events": [{"id": box["id"], "value": "too bulky"}]}
        return {"events": [{"id": _find(tree, t="button", label="Cancel")["id"],
                            "click": True}]}

    drive(anki, sync.update_decks, respond)

    assert anki.gui.clipboard, "declining the update must not discard her notes"
    assert "too bulky" in anki.gui.clipboard[-1]
    assert not anki.col.find_notes(f'"tag:{SCOPE}"'), "Cancel must still import nothing"


def test_update_decks_reports_up_to_date_when_nothing_pending(anki, tmp_path):
    from internpearls import sync
    folder = _write_source(tmp_path, {})
    _configure(anki, folder)

    sync.update_decks()

    assert any("up to date" in i for i in anki.gui.infos)
    assert not anki.col.imports


def test_update_decks_with_only_content_pending_skips_reconcile_cleanly(anki, tmp_path):
    from internpearls import sync
    folder = _write_source(
        tmp_path, {DECK: ("v1", [("g1", _fields("Front one"), TAGS)], None)})
    _configure(anki, folder)

    trees = _update(anki)

    assert anki.col.note_by_guid("g1")["Front"] == "Front one"
    assert "Update complete" in _summary_text(trees)


def test_update_decks_with_only_reconcile_pending_skips_sync_cleanly(anki, tmp_path):
    from internpearls import sync
    _her_card(anki, "g1", "Lidocaine — onset time?", deck=DECK)
    folder = _write_source(tmp_path, {}, deck_moves={"g1": {"from": DECK, "to": NEW_DECK}})
    _configure(anki, folder)

    trees = _update(anki)

    assert not anki.col.imports
    cid = anki.col.note_by_guid("g1").card_ids()[0]
    assert anki.col.decks.name(anki.col._cards[cid].did) == NEW_DECK
    assert "Update complete" in _summary_text(trees)


def test_update_decks_declined_leaves_everything_untouched(anki, tmp_path):
    from internpearls import sync
    anki.col.add_note("old1", _fields("bulky crisis card"), TAGS.split())
    folder = _write_source(
        tmp_path, {DECK: ("v1", [("g1", _fields("Front one"), TAGS)], None)},
        retired={DECK: {"old1": {"identity": "bulky crisis card", "reason": "split",
                                 "superseded_by": []}}})
    _configure(anki, folder)

    drive(anki, sync.update_decks, _click_update_button(accept=False))

    assert not anki.col.imports
    old = anki.col.note_by_guid("old1")
    assert anki.col._cards[old.card_ids()[0]].queue == 0
    assert RETIRED_TAG not in old.tags


def test_update_decks_cancel_during_preview_touches_nothing(anki, tmp_path):
    """Clicking Cancel on the "Checking for updates" dialog is a download-and-diff
    step only — nothing has touched the collection yet, so cancelling there must
    leave everything exactly as it was, not partially apply anything."""
    import aqt.qt as aqt_qt
    from internpearls import sync
    folder = _write_source(tmp_path, {
        DECK: ("v1", [("g1", _fields("Front one"), TAGS)], None),
        NEW_DECK: ("v1", [("g2", _fields("Front two"), TAGS)], None)})
    _configure(anki, folder)
    aqt_qt.QProgressDialog.cancel_after = {"Checking for updates": 1}

    sync.update_decks()

    assert not anki.col.imports
    assert not any(n.guid in ("g1", "g2") for n in anki.col._notes.values())
    assert any("cancelled" in i.lower() for i in anki.gui.infos)


def test_update_decks_cancel_during_apply_keeps_completed_decks_and_skips_reconcile(
        anki, tmp_path):
    """Cancelling mid-apply must leave whatever deck(s) already finished fully
    persisted (installed version, restored fields) and never start the deck after
    the cancel point. Archiving/relocating must be skipped entirely rather than run
    against a partial sync — it assumes every content update already landed, so a
    retired card's replacement is in place before the old one archives out."""
    import aqt.qt as aqt_qt
    from internpearls import sync
    anki.col.add_note("old1", _fields("bulky crisis card"), TAGS.split())
    folder = _write_source(
        tmp_path, {
            DECK: ("v1", [("g1", _fields("Front one"), TAGS)], None),
            NEW_DECK: ("v1", [("g2", _fields("Front two"), TAGS)], None)},
        retired={DECK: {"old1": {"identity": "bulky crisis card", "reason": "split",
                                 "superseded_by": ["g1"]}}})
    _configure(anki, folder)
    aqt_qt.QProgressDialog.cancel_after = {"Updating decks": 1}

    trees = _update(anki)

    assert anki.col.note_by_guid("g1")["Front"] == "Front one"
    assert not any(n.guid == "g2" for n in anki.col._notes.values())
    installed = json.load(open(sync.INSTALLED, encoding="utf8"))
    assert installed == {DECK: "v1"}
    old = anki.col.note_by_guid("old1")
    assert RETIRED_TAG not in old.tags   # reconcile skipped, nothing archived
    assert "stopped early" in _summary_text(trees).lower()


def test_preview_reuses_cached_download_for_an_unchanged_deck(anki, tmp_path):
    """Opening Update my decks, previewing, and cancelling repeatedly must not
    re-download a deck whose version hasn't changed. The v0.26.1 preview download is
    the main reason a "just checking" habit runs into sporadic source hiccups, so a
    second preview of the same version has to be a cache hit, not another fetch."""
    from internpearls import sync
    from internpearls.collection import _her_front_to_guid
    from internpearls.logic import decks_to_update

    folder = _write_source(tmp_path, {
        DECK: ("v1", [("g1", _fields("Front one"), TAGS)], None),
        NEW_DECK: ("v1", [("g2", _fields("Front two"), TAGS)], None)})
    _configure(anki, folder)
    manifest, real_fetch, _ = sync._fetch_manifest(sync._cfg())
    todo = decks_to_update(manifest, {}, [])
    her = _her_front_to_guid(SCOPE)

    calls = []

    def counting_fetch(d):
        calls.append(d["name"])
        return real_fetch(d)

    sync._preview_content_changes(counting_fetch, todo, her, {})
    assert len(calls) == 2   # both decks fetched the first time
    sync._preview_content_changes(counting_fetch, todo, her, {})
    assert len(calls) == 2   # second preview is all cache hits, no new fetches


def test_preview_refetches_a_deck_whose_version_changed(anki, tmp_path):
    """The cache is keyed by content-hash version, so a real push (new version) must
    miss it and re-download, never serve a stale .apkg."""
    from internpearls import sync
    from internpearls.collection import _her_front_to_guid
    from internpearls.logic import decks_to_update

    folder = _write_source(tmp_path, {
        DECK: ("v1", [("g1", _fields("Front one"), TAGS)], None)})
    _configure(anki, folder)
    manifest, real_fetch, _ = sync._fetch_manifest(sync._cfg())
    todo = decks_to_update(manifest, {}, [])
    her = _her_front_to_guid(SCOPE)
    calls = []

    def counting_fetch(d):
        calls.append(d["version"])
        return real_fetch(d)

    sync._preview_content_changes(counting_fetch, todo, her, {})
    assert calls == ["v1"]
    # Source pushes a new version of the same deck.
    folder2 = _write_source(tmp_path, {
        DECK: ("v2", [("g1", _fields("Front one"), TAGS),
                      ("g2", _fields("Front two"), TAGS)], None)})
    _configure(anki, folder2)
    manifest2, real_fetch2, _ = sync._fetch_manifest(sync._cfg())
    todo2 = decks_to_update(manifest2, {}, [])

    def counting_fetch2(d):
        calls.append(d["version"])
        return real_fetch2(d)

    sync._preview_content_changes(counting_fetch2, todo2, her, {})
    assert calls == ["v1", "v2"]   # version changed, cache missed, re-fetched


# ---------------------------------------------------------- duplicate cleanup
def _write_duplicate_source(tmp_path, deck_name):
    """A source folder whose manifest lists one deck by name only (no apkg content
    needed; clean_up_duplicates only reads manifest["decks"] for the canonical
    deck-name list, it never downloads anything)."""
    folder = tmp_path / "source"
    folder.mkdir(exist_ok=True)
    manifest = {"schema": 2, "decks": [{"name": deck_name, "apkg": "x.apkg",
                                        "version": "v1", "cards": 1}],
                "front_aliases": {}, "retired": {}, "deck_moves": {}}
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf8")
    return str(folder)


def _click_duplicate_button(accept):
    def respond(p):
        if p["kind"] != "dialog":
            return {}
        tree = p["tree"]
        if accept:
            btn = next(n for n in _walk(tree)
                      if n.get("t") == "button" and n.get("label") != "Cancel")
        else:
            btn = _find(tree, t="button", label="Cancel")
        return {"events": [{"id": btn["id"], "click": True}]}
    return respond


def test_clean_up_duplicates_archives_the_copy_with_fewer_reviews(anki, tmp_path):
    from internpearls import sync
    old_deck = "Intern Pearls::Intern Custom::Upper Extremity Nerve Blocks"
    new_deck = "Intern Pearls::Intern Custom::Regional::Upper Extremity Nerve Blocks"
    old_note = _her_card(anki, "old", "same front text", deck=old_deck)
    _her_card(anki, "new", "same front text", deck=new_deck)
    anki.col._cards[old_note.card_ids()[0]].reps = 3   # she has actually studied this one
    folder = _write_duplicate_source(tmp_path, new_deck)
    _configure(anki, folder)

    drive(anki, sync.clean_up_duplicates, _click_duplicate_button(accept=True))

    # Fresh lookups by guid throughout, not note/cid references captured before drive():
    # the Runner's replay deepcopies mw.col on every pass, so a reference from before
    # drive() points at an orphaned pre-replay collection, not the one actually mutated.
    lost_cid = anki.col.note_by_guid("new").card_ids()[0]
    assert anki.col._cards[lost_cid].queue == -1     # suspended
    assert anki.col._cards[lost_cid].did == anki.col.decks.id_for_name(
        "Intern Pearls::Intern Custom::Retired")
    assert f"{SCOPE}::retired-duplicate" in anki.col.note_by_guid("new").tags
    kept_cid = anki.col.note_by_guid("old").card_ids()[0]
    assert anki.col._cards[kept_cid].queue == 0       # kept copy untouched
    assert any("Archived <b>1 duplicate card</b>" in i for i in anki.gui.infos)


def test_clean_up_duplicates_names_every_card_however_many_there_are(anki, tmp_path):
    """The old bullet list was capped at 15 because a plain message box has no scroll
    area, so a longer one pushed its own buttons off-screen. The rows stream inside a
    scroll area with the buttons outside it, so every duplicate is named."""
    from internpearls import sync
    old_deck = "Intern Pearls::Intern Custom::Upper Extremity Nerve Blocks"
    new_deck = "Intern Pearls::Intern Custom::Regional::Upper Extremity Nerve Blocks"
    for i in range(20):
        _her_card(anki, f"old{i}", f"duplicated card {i}", deck=old_deck)
        _her_card(anki, f"new{i}", f"duplicated card {i}", deck=new_deck)
    _configure(anki, _write_duplicate_source(tmp_path, new_deck))
    anki.gui.interactive = True
    seen = {}

    def respond(p):
        if p["kind"] != "dialog":
            return {}
        seen.setdefault("texts", _label_texts(p["tree"]))
        return {"events": [{"id": _find(p["tree"], t="button", label="Cancel")["id"],
                            "click": True}]}

    drive(anki, sync.clean_up_duplicates, respond)

    named = [t for t in seen["texts"] if "duplicated card " in t]
    assert len(named) == 20, f"expected every duplicate named, got {len(named)}"
    assert not any("more" in t and "..." in t for t in seen["texts"]), \
        "nothing is left behind an \"and N more\" line now"


def test_clean_up_duplicates_dialog_uses_the_palette_not_a_css_keyword(anki, tmp_path):
    from internpearls import palette, sync
    active = palette.colors()
    old_deck = "Intern Pearls::Intern Custom::Upper Extremity Nerve Blocks"
    new_deck = "Intern Pearls::Intern Custom::Regional::Upper Extremity Nerve Blocks"
    _her_card(anki, "old", "same front text", deck=old_deck)
    _her_card(anki, "new", "same front text", deck=new_deck)
    folder = _write_duplicate_source(tmp_path, new_deck)
    _configure(anki, folder)
    anki.gui.interactive = True
    seen = {}

    def respond(p):
        if p["kind"] != "dialog":
            return {}
        # Every label, not just the first: the detail this checks rides inside a row's
        # own primary text now, with the heading above it in a label of its own.
        seen["text"] = "\n".join(_label_texts(p["tree"]))
        return {"events": [{"id": _find(p["tree"], t="button", label="Cancel")["id"],
                            "click": True}]}

    drive(anki, sync.clean_up_duplicates, respond)

    assert "color:gray" not in seen["text"]
    assert active["muted"] in seen["text"]


def test_clean_up_duplicates_breaks_a_zero_review_tie_by_canonical_deck(anki, tmp_path):
    from internpearls import sync
    old_deck = "Intern Pearls::Intern Custom::Upper Extremity Nerve Blocks"
    new_deck = "Intern Pearls::Intern Custom::Regional::Upper Extremity Nerve Blocks"
    _her_card(anki, "old", "same front text", deck=old_deck)
    _her_card(anki, "new", "same front text", deck=new_deck)
    folder = _write_duplicate_source(tmp_path, new_deck)
    _configure(anki, folder)

    drive(anki, sync.clean_up_duplicates, _click_duplicate_button(accept=True))

    # Fresh lookups by guid, same reason as the test above.
    kept_cid = anki.col.note_by_guid("new").card_ids()[0]
    assert anki.col._cards[kept_cid].queue == 0
    lost_cid = anki.col.note_by_guid("old").card_ids()[0]
    assert anki.col._cards[lost_cid].queue == -1


def test_clean_up_duplicates_carries_notes_to_the_kept_copy(anki, tmp_path):
    from internpearls import sync
    old_deck = "Intern Pearls::Intern Custom::Upper Extremity Nerve Blocks"
    new_deck = "Intern Pearls::Intern Custom::Regional::Upper Extremity Nerve Blocks"
    old_note = _her_card(anki, "old", "same front text", deck=old_deck)
    new_note = _her_card(anki, "new", "same front text", deck=new_deck)
    new_note["Notes"] = "my personal mnemonic"   # written on the copy that will lose
    anki.col._cards[old_note.card_ids()[0]].reps = 2   # old has more reviews, so it wins
    folder = _write_duplicate_source(tmp_path, new_deck)
    _configure(anki, folder)

    drive(anki, sync.clean_up_duplicates, _click_duplicate_button(accept=True))

    # old was kept (more reviews) and started with a blank Notes field, so this only
    # passes if the losing copy's text actually carried over, not merely survived.
    assert anki.col.note_by_guid("old")["Notes"] == "my personal mnemonic"


def test_clean_up_duplicates_is_idempotent(anki, tmp_path):
    from internpearls import sync
    old_deck = "Intern Pearls::Intern Custom::Upper Extremity Nerve Blocks"
    new_deck = "Intern Pearls::Intern Custom::Regional::Upper Extremity Nerve Blocks"
    _her_card(anki, "old", "same front text", deck=old_deck)
    _her_card(anki, "new", "same front text", deck=new_deck)
    folder = _write_duplicate_source(tmp_path, new_deck)
    _configure(anki, folder)
    drive(anki, sync.clean_up_duplicates, _click_duplicate_button(accept=True))

    anki.gui.infos.clear()
    anki.gui.interactive = False
    sync.clean_up_duplicates()   # second run must find nothing left to do

    assert any("No duplicate" in i for i in anki.gui.infos)


def test_clean_up_duplicates_declined_leaves_everything_untouched(anki, tmp_path):
    from internpearls import sync
    old_deck = "Intern Pearls::Intern Custom::Upper Extremity Nerve Blocks"
    new_deck = "Intern Pearls::Intern Custom::Regional::Upper Extremity Nerve Blocks"
    _her_card(anki, "old", "same front text", deck=old_deck)
    _her_card(anki, "new", "same front text", deck=new_deck)
    folder = _write_duplicate_source(tmp_path, new_deck)
    _configure(anki, folder)

    drive(anki, sync.clean_up_duplicates, _click_duplicate_button(accept=False))

    old_cid = anki.col.note_by_guid("old").card_ids()[0]
    new_cid = anki.col.note_by_guid("new").card_ids()[0]
    assert anki.col._cards[old_cid].queue == 0
    assert anki.col._cards[new_cid].queue == 0


def test_clean_up_duplicates_reports_nothing_when_no_duplicates_present(anki, tmp_path):
    from internpearls import sync
    _her_card(anki, "mine", "a unique card")
    folder = _write_duplicate_source(tmp_path, DECK)
    _configure(anki, folder)

    sync.clean_up_duplicates()

    assert any("No duplicate" in i for i in anki.gui.infos)


def test_restore_from_backup_clears_installed_so_the_next_sync_re_offers(anki, tmp_path):
    """A collection restore rolls content back but installed.json lives outside the
    collection, so nothing else can tell the add-on its versions are now stale."""
    from internpearls import collection
    from internpearls.config import INSTALLED, _load_json, _save_json

    _save_json(INSTALLED, {DECK: "v1"})
    anki.gui.answers.append(True)          # "Continue?" on the restore confirmation

    collection.restore_from_backup()

    assert _load_json(INSTALLED, {}) == {}


def test_sync_re_offers_a_deck_whose_cards_were_rolled_back_to_older_content(
        anki, tmp_path):
    """The reported bug: a restore that rolls a deck back to older content leaves its
    cards present, so the presence check passes and installed.json still claims the
    newest version. Only invalidating on restore can catch this."""
    from internpearls import collection, sync

    folder = _write_source(tmp_path, {
        DECK: ("v2", [("g1", _fields("Front two"), TAGS)], None)})
    _configure(anki, folder)
    _sync(anki)
    assert anki.col.note_by_guid("g1")["Front"] == "Front two"

    # A restore rolls the card back to older content. It is still present and still
    # tagged, so the presence check is satisfied and nothing else can notice.
    anki.col.note_by_guid("g1")["Front"] = "Front one"
    anki.gui.infos.clear()
    anki.gui.answers.append(True)   # "Continue?" on the restore confirmation
    collection.restore_from_backup()

    _sync(anki)

    assert not any("up to date" in i for i in anki.gui.infos)
    assert anki.col.note_by_guid("g1")["Front"] == "Front two"


def test_import_deck_invalidates_only_the_decks_in_the_imported_file(anki, tmp_path):
    from internpearls import collection
    from internpearls.config import INSTALLED, _load_json, _save_json
    from test_logic import _legacy_apkg

    src = _legacy_apkg(tmp_path / "one.apkg", [f"{DECK}::1. Basics"])
    _save_json(INSTALLED, {DECK: "v1", "Intern Pearls::Intern Custom::Other": "v9"})
    anki.gui.file_picks.append(str(src))
    anki.gui.answers.append(True)          # "Import ...?"

    collection.import_deck()

    assert not anki.gui.warnings   # the import itself succeeded
    # Only the imported deck is re-offered; the untouched one stays current.
    assert _load_json(INSTALLED, {}) == {"Intern Pearls::Intern Custom::Other": "v9"}


def test_import_deck_falls_back_to_clearing_everything_when_deck_names_unreadable(
        anki, tmp_path):
    """The file itself imports fine (its notes table reads clean) but its deck
    names can't be read (no col table). A read failure here must never mean "no
    invalidation" - that's the exact bug this release exists to fix."""
    from internpearls import collection
    from internpearls.config import INSTALLED, _load_json, _save_json
    from test_logic import _legacy_apkg

    src = _legacy_apkg(tmp_path / "unreadable.apkg", [DECK], with_col=False)
    _save_json(INSTALLED, {DECK: "v1", "Intern Pearls::Intern Custom::Other": "v9"})
    anki.gui.file_picks.append(str(src))
    anki.gui.answers.append(True)

    collection.import_deck()

    # Proves this took the "imported fine, deck names unreadable" fallback path,
    # not the earlier "import itself failed" early return, which also clears
    # nothing but for an unrelated reason.
    assert not anki.gui.warnings
    assert _load_json(INSTALLED, {}) == {}


def test_import_deck_clears_everything_when_no_deck_name_maps_to_a_tracked_deck(
        anki, tmp_path):
    """A backup taken before a deck was renamed carries the old deck path, which maps
    to nothing tracked today. The import still rolled cards back, by GUID, so the
    empty match has to mean "clear all", not "clear nothing".

    This pins a deliberate choice, not an incidental one: `invalidate_installed(names
    or None)` reads like a latent bug (empty mapping clears everything) right up until
    you ask what an empty mapping means here. Clearing nothing would leave rolled-back
    cards looking current, which is the failure this whole path exists to prevent. The
    cost of being wrong the other way is one redundant re-verify.
    """
    from internpearls import collection
    from internpearls.config import INSTALLED, _load_json, _save_json
    from test_logic import _legacy_apkg

    renamed_before_backup = "Intern Pearls::Intern Custom::Its Older Name"
    src = _legacy_apkg(tmp_path / "old-name.apkg", [renamed_before_backup])
    _save_json(INSTALLED, {DECK: "v1", "Intern Pearls::Intern Custom::Other": "v9"})
    anki.gui.file_picks.append(str(src))
    anki.gui.answers.append(True)          # "Import ...?"

    collection.import_deck()

    # Not the unreadable-file fallback: this file's deck names read fine, they just
    # match nothing. Both paths clear everything, so the warning check is what tells
    # them apart.
    assert not anki.gui.warnings
    assert _load_json(INSTALLED, {}) == {}


def test_invalidate_installed_drops_only_the_named_decks(anki):
    from internpearls.collection import invalidate_installed
    from internpearls.config import INSTALLED, _load_json, _save_json

    _save_json(INSTALLED, {"A": "v1", "B": "v2", "C": "v3"})
    invalidate_installed(["A", "C"])
    assert _load_json(INSTALLED, {}) == {"B": "v2"}


def test_invalidate_installed_with_no_names_clears_everything(anki):
    from internpearls.collection import invalidate_installed
    from internpearls.config import INSTALLED, _load_json, _save_json

    _save_json(INSTALLED, {"A": "v1", "B": "v2"})
    invalidate_installed()
    assert _load_json(INSTALLED, {}) == {}


def test_invalidate_installed_ignores_a_deck_it_never_had(anki):
    # The caller maps deck names it read from a file; one that was never synced is
    # simply not our business, not an error.
    from internpearls.collection import invalidate_installed
    from internpearls.config import INSTALLED, _load_json, _save_json

    _save_json(INSTALLED, {"A": "v1"})
    invalidate_installed(["Never::Synced"])
    assert _load_json(INSTALLED, {}) == {"A": "v1"}


# --------------------------------------------------------------- empty cards
def _her_cloze(anki, guid, text, deck=DECK, ords=None):
    """A cloze note of hers, with a card per ordinal in `ords` (defaults to the
    ordinals the text actually has). Passing `ords` wider than the text is how a
    collection looks after the deck source regrouped a live cloze into fewer
    deletions: the surplus cards are still there with nothing left to render."""
    import types
    if not anki.col.models.by_name("Study Deck - Cloze"):
        anki.col.models._models.append(_cloze_model())
    model = anki.col.models.by_name("Study Deck - Cloze")
    note = anki.col.add_note(guid, [text, "why", "", "", ""], TAGS.split(),
                             model=model, deck=deck)
    for o in (ords or [])[1:]:
        cid = anki.col._next_cid
        anki.col._next_cid += 1
        first = anki.col._cards[note._card_ids[0]]
        anki.col._cards[cid] = types.SimpleNamespace(
            id=cid, nid=note.id, did=first.did, queue=0, reps=0, ord=o, type=0,
            due=0, ivl=0, factor=0, lapses=0, memory_state=None,
            desired_retention=None, decay=None, last_review_time=None)
        note._card_ids.append(cid)
    return note


def test_remove_empty_cards_removes_only_the_orphaned_ordinals(anki):
    from internpearls import collection
    # Regrouped from five deletions down to two: c3/c4/c5 are left with nothing.
    _her_cloze(anki, "regrouped", "the {{c1::first}} and {{c2::second}}",
               ords=[0, 1, 2, 3, 4])
    _her_cloze(anki, "intact", "an {{c1::untouched}} card", ords=[0])

    drive(anki, collection.remove_empty_cards, _click_duplicate_button(accept=True))

    note = anki.col.note_by_guid("regrouped")
    assert sorted(anki.col._cards[cid].ord for cid in note._card_ids) == [0, 1]
    assert len(anki.col.note_by_guid("intact")._card_ids) == 1
    assert any("Removed <b>3 empty cards</b>" in i for i in anki.gui.infos)


def test_remove_empty_cards_leaves_other_peoples_notes_alone(anki):
    from internpearls import collection
    _her_cloze(anki, "hers", "the {{c1::first}} only", ords=[0, 1])
    theirs = _her_cloze(anki, "theirs", "the {{c1::first}} only", ords=[0, 1])
    theirs.tags = ["SomeoneElsesDeck"]

    drive(anki, collection.remove_empty_cards, _click_duplicate_button(accept=True))

    assert len(anki.col.note_by_guid("hers")._card_ids) == 1
    assert len(anki.col.note_by_guid("theirs")._card_ids) == 2   # untouched


def test_remove_empty_cards_never_deletes_a_note_whose_cards_are_all_empty(anki):
    from internpearls import collection
    # No deletions at all, so every card is empty: removing them would take the note
    # and its content with it. Reported, never acted on.
    _her_cloze(anki, "contentless", "a card with no deletions in it", ords=[0])

    drive(anki, collection.remove_empty_cards, _click_duplicate_button(accept=True))

    assert anki.col.note_by_guid("contentless") is not None
    assert any("no content on any card at all" in i for i in anki.gui.infos)


def test_update_notetypes_lists_each_added_field_as_a_row(anki):
    """The Fix note types result used to be a bulleted list inside an info box. It is
    the same run summary every other flow ends on now: a heading and a row per field
    that was added."""
    from internpearls import collection
    basic = anki.col.models.by_name("Study Deck - Basic")
    basic["flds"] = [f for f in basic["flds"] if f["name"] != "Dosing"]
    anki.gui.interactive = True
    seen = {}

    def respond(p):
        if p["kind"] != "dialog":
            return {}
        seen.setdefault("texts", _label_texts(p["tree"]))
        return {"events": [{"id": _find(p["tree"], t="button", label="OK")["id"],
                            "click": True}]}

    drive(anki, collection.update_notetypes, respond)

    assert any("Updated note types" in t for t in seen["texts"])
    assert "Study Deck - Basic: +Dosing" in seen["texts"], \
        f"the added field should be a row of its own: {seen['texts']}"
    assert not [t for t in seen["texts"] if "<li>" in t or "<ul>" in t]


def test_remove_empty_cards_names_every_note_however_many_there_are(anki):
    """Uncapped for the same reason Clean up duplicates is: the rows stream inside a
    scroll area, so a long list can no longer put the buttons out of reach."""
    from internpearls import collection
    for i in range(20):
        _her_cloze(anki, f"regrouped{i}", f"the {{{{c1::first}}}} of card {i}",
                  ords=[0, 1])
    anki.gui.interactive = True
    seen = {}

    def respond(p):
        if p["kind"] != "dialog":
            return {}
        seen.setdefault("texts", _label_texts(p["tree"]))
        return {"events": [{"id": _find(p["tree"], t="button", label="Cancel")["id"],
                            "click": True}]}

    drive(anki, collection.remove_empty_cards, respond)

    named = [t for t in seen["texts"] if "of card " in t]
    assert len(named) == 20, f"expected every note named, got {len(named)}"


def test_remove_empty_cards_dialog_uses_the_palette_not_a_css_keyword(anki):
    from internpearls import collection, palette
    active = palette.colors()
    _her_cloze(anki, "regrouped", "the {{c1::first}} and {{c2::second}}",
              ords=[0, 1, 2, 3, 4])
    anki.gui.interactive = True
    seen = {}

    def respond(p):
        if p["kind"] != "dialog":
            return {}
        # The missing deletion numbers are the row's trailing column now, so the colour
        # they are drawn in is that label's own stylesheet rather than inline markup.
        seen["detail"] = next(n for n in _walk(p["tree"])
                             if n.get("t") == "label" and n.get("text") == "c3, c4, c5")
        return {"events": [{"id": _find(p["tree"], t="button", label="Cancel")["id"],
                            "click": True}]}

    drive(anki, collection.remove_empty_cards, respond)

    assert "gray" not in seen["detail"]["style"]
    assert active["muted"] in seen["detail"]["style"]


def test_remove_empty_cards_declines_cleanly(anki):
    from internpearls import collection
    _her_cloze(anki, "regrouped", "the {{c1::first}} only", ords=[0, 1])

    drive(anki, collection.remove_empty_cards, _click_duplicate_button(accept=False))

    assert len(anki.col.note_by_guid("regrouped")._card_ids) == 2
    assert not getattr(anki.col, "removed_cards", [])


# ------------------------------------------- feedback persistence & popup merge
def _feedback_run(anki, tmp_path, on_screen, decide="Cancel"):
    """Drive update_decks through its confirmation, letting the caller decide what to
    type there and when to click `decide` (via `on_screen(tree, seen, decide)`), and
    answering the end-of-run feedback digest generically. Returns the responder's own
    scratch dict.

    There is only one dialog to answer on the way there now, so `on_screen` is called
    for every pause of that same confirmation, as many times as it likes (typing a box
    reopens the same dialog with an updated tree rather than closing it, the same way
    it always did) - it decides for itself when it is done and clicks `decide`.
    """
    from internpearls import sync
    folder = _write_source(tmp_path, {
        DECK: ("v1", [("g2", _fields("Front two"), TAGS)], None)})
    _configure(anki, folder)
    anki.mw._config["collect_card_feedback"] = True
    anki.gui.interactive = True
    seen = {}

    def respond(p):
        if p["kind"] != "dialog":
            return {}
        title, tree = p.get("title") or "", p["tree"]
        if "card feedback" in title:
            seen["digest"] = tree
            return {"events": [{"id": _find(tree, t="button", label="Close")["id"],
                                "click": True}]}
        return on_screen(tree, seen, decide)

    drive(anki, sync.update_decks, respond)
    return seen


def test_feedback_is_saved_without_waiting_for_the_dialog_to_close(anki, tmp_path):
    """The whole point: what she types has to survive the run dying before the digest,
    and the digest is several steps and one fallible import later.

    Answered in two passes on purpose. The first types into the box and does NOT close
    the dialog, so the second pass sees exactly the state a crash would interrupt: text
    entered, dialog still open, nothing closed. The debounce timer is fired by hand
    there because the mock has no event loop, which is also what proves the box is
    wired to it at all.
    """
    from internpearls import review
    saved = {}

    def on_screen(tree, seen, decide):
        box = next(n for n in _walk(tree) if n.get("t") == "textarea")
        if not seen.get("typed"):
            seen["typed"] = True
            return {"events": [{"id": box["id"], "value": "far too bulky"}]}
        assert any(t.started for t in anki.qt_timers), "typing must arm the save"
        for t in anki.qt_timers:
            t.fire()
        saved.update(review.load_saved_feedback())
        return {"events": [{"id": _find(tree, t="button", label=decide)["id"],
                            "click": True}]}

    _feedback_run(anki, tmp_path, on_screen)

    assert [e["note"] for e in saved.values()] == ["far too bulky"], \
        "the note must be on disk while the dialog is still open"
    assert [e["front"] for e in saved.values()] == ["Front two"], "saved with its card"


def test_feedback_from_an_interrupted_run_comes_back_in_the_next_one(anki, tmp_path):
    """A run that died before the digest left her notes on disk. The next update picks
    them up on its own, with no recovery prompt of its own to click through."""
    from internpearls import review
    review.save_feedback({"gONE": {"note": "wrong dose", "deck": DECK,
                                   "front": "An earlier card"}})

    def on_screen(tree, seen, decide):
        return {"events": [{"id": _find(tree, t="button", label=decide)["id"],
                            "click": True}]}

    _feedback_run(anki, tmp_path, on_screen)

    assert any("wrong dose" in c for c in anki.gui.clipboard)
    assert any("An earlier card" in c for c in anki.gui.clipboard)


def test_saved_feedback_is_cleared_once_the_digest_has_been_shown(anki, tmp_path):
    from internpearls import review
    review.save_feedback({"gONE": {"note": "wrong dose", "deck": DECK,
                                   "front": "An earlier card"}})

    def on_screen(tree, seen, decide):
        return {"events": [{"id": _find(tree, t="button", label=decide)["id"],
                            "click": True}]}

    _feedback_run(anki, tmp_path, on_screen)

    assert review.load_saved_feedback() == {}, "shown once, not offered forever"


def test_completion_summary_and_feedback_arrive_as_one_dialog(anki, tmp_path):
    """They used to be two boxes back to back at the end of the run."""
    def on_screen(tree, seen, decide):
        box = next(n for n in _walk(tree) if n.get("t") == "textarea")
        return {"events": [{"id": box["id"], "value": "too bulky"},
                           {"id": _find(tree, t="button", label=decide)["id"],
                            "click": True}]}

    seen = _feedback_run(anki, tmp_path, on_screen, decide="Update")

    assert seen.get("digest"), "the digest dialog should have opened"
    text = " ".join(str(n) for n in _walk(seen["digest"]))
    assert "Update complete" in text, "the summary belongs in the same dialog"
    assert not any("Update complete" in i for i in anki.gui.infos), \
        "and must not also arrive as its own info box"


def test_result_screen_uses_the_shared_title_and_row_components(anki, tmp_path):
    """The end-of-run summary used to be one QLabel holding a whole
    <ul><li>...</li></ul> blob. It now reads with the same vocabulary as the
    confirmation it follows: a title_label heading, one widgets.simple_row per result
    line (archived count included), and the digest's own payload block untouched."""
    from internpearls import sync
    anki.col.add_note("old1", _fields("bulky crisis card"), TAGS.split())
    folder = _write_source(
        tmp_path, {DECK: ("v1", [("g1", _fields("Front one"), TAGS)], None)},
        retired={DECK: {"old1": {"identity": "bulky crisis card", "reason": "split",
                                 "superseded_by": ["g1"]}}})
    _configure(anki, folder)
    anki.mw._config["collect_card_feedback"] = True
    anki.gui.interactive = True
    seen = {}

    def respond(p):
        if p["kind"] != "dialog":
            return {}
        title, tree = p.get("title") or "", p["tree"]
        if "card feedback" in title:
            seen["digest"] = tree
            return {"events": [{"id": _find(tree, t="button", label="Close")["id"],
                                "click": True}]}
        box = next((n for n in _walk(tree) if n.get("t") == "textarea"), None)
        events = [{"id": box["id"], "value": "too bulky"}] if box else []
        events.append(
            {"id": _find(tree, t="button", label="Update")["id"], "click": True})
        return {"events": events}

    drive(anki, sync.update_decks, respond)

    assert seen.get("digest"), "the digest dialog should have opened"
    labels = [n.get("text") or "" for n in _walk(seen["digest"]) if n.get("t") == "label"]
    joined = "\n".join(labels)

    assert "Update complete" in joined
    assert any("Archived" in l and "retired card" in l for l in labels), \
        "the archived-card count should be its own row, not folded into the title"
    assert not any("<li>" in l or "<ul>" in l for l in labels), \
        "result lines are separate rows now, not one bulleted list inside one label"


def test_result_screen_shows_only_the_digest_when_the_run_was_cancelled(anki, tmp_path):
    """She backed out of the update but still flagged a card on the way: the digest is
    the whole dialog, with no leftover title or result rows above it, since there is
    no completed run to summarize."""
    def on_screen(tree, seen, decide):
        box = next(n for n in _walk(tree) if n.get("t") == "textarea")
        return {"events": [{"id": box["id"], "value": "too bulky"},
                           {"id": _find(tree, t="button", label=decide)["id"],
                            "click": True}]}

    seen = _feedback_run(anki, tmp_path, on_screen, decide="Cancel")

    assert seen.get("digest"), "the digest dialog should have opened"
    labels = [n.get("text") or "" for n in _walk(seen["digest"]) if n.get("t") == "label"]
    assert not any("Update complete" in l for l in labels), \
        "a cancelled run has no completion summary to show"
    assert not any("kept" in l or "Archived" in l for l in labels)


def _update_with_look_change(anki, tmp_path, tick):
    """Run update_decks against a deck whose card template changed, ticking (or not)
    the new-look checkbox on the one confirmation."""
    from internpearls import sync
    _her_card(anki, "g1", "Front one")
    folder = _write_source(tmp_path, {
        DECK: ("v2", [("g1", _fields("Front one"), TAGS)], make_model(css=NEW_CSS))})
    _configure(anki, folder)
    anki.gui.interactive = True

    def respond(p):
        if p["kind"] != "dialog":
            return {}
        tree = p["tree"]
        done = _dismiss_result(tree)
        if done:
            return done
        events = []
        box = next((n for n in _walk(tree) if n.get("t") == "check"), None)
        if box and tick:
            events.append({"id": box["id"], "value": True})
        update = _find(tree, t="button", label="Update")
        events.append({"id": update["id"], "click": True})
        return {"events": events}

    drive(anki, sync.update_decks, respond)


def test_update_applies_the_new_look_when_the_box_is_ticked(anki, tmp_path):
    _update_with_look_change(anki, tmp_path, tick=True)

    assert anki.col.models.all()[0]["css"] == NEW_CSS


def test_update_keeps_the_old_look_when_the_box_is_left_alone(anki, tmp_path):
    old_css = anki.col.models.all()[0]["css"]

    _update_with_look_change(anki, tmp_path, tick=False)

    assert anki.col.models.all()[0]["css"] == old_css
    assert anki.col.imports, "declining the look must still import the content"


def test_update_never_asks_about_the_look_in_its_own_dialog(anki, tmp_path):
    """It used to interrupt mid-run, after the import had already started. The
    confirmation carries the decision now, so nothing new appears behind it."""
    _update_with_look_change(anki, tmp_path, tick=True)

    assert not any("full sync" in a for a in anki.gui.asks)



def test_update_decks_groups_every_pending_row_under_its_own_deck(anki, tmp_path):
    """One deck, one section, holding everything pending for it.

    Retired and relocated cards used to hang below the list under headings of their
    own, so a single deck's work read as three separate lists and its name appeared
    three times. They belong to a deck like any other row: a retired card to the deck
    it is retired out of, a relocated one to the deck it is currently sitting in, with
    only its destination named on the row itself. Within the section the content
    updates read first, then what is being archived, then what is moving.
    """
    from internpearls import sync
    from internpearls.widgets import CHIPS
    anki.col.add_note("g1", _fields("Front one", back="the old answer"), TAGS.split())
    anki.col.add_note("old1", _fields("bulky crisis card"), TAGS.split())
    _her_card(anki, "moved1", "a card leaving this deck")
    folder = _write_source(
        tmp_path,
        {DECK: ("v2", [("g1", _fields("Front one", back="the new answer"), TAGS),
                       ("g2", _fields("Front two"), TAGS)], None)},
        retired={DECK: {"old1": {"identity": "bulky crisis card", "reason": "split",
                                 "superseded_by": []}}},
        deck_moves={"moved1": {"from": DECK, "to": NEW_DECK}})
    _configure(anki, folder)
    anki.gui.interactive = True
    seen = {}

    def respond(p):
        if p["kind"] != "dialog":
            return {}
        seen.setdefault("lines", _labels(p["tree"]).split("\n"))
        return {"events": [{"id": _find(p["tree"], t="button", label="Cancel")["id"],
                            "click": True}]}

    drive(anki, sync.update_decks, respond)

    lines = seen["lines"]
    # One "Pharm" heading, the summary row that counts it aside, and no heading named
    # after a kind rather than a deck.
    assert lines.count("Pharm") == 2, f"expected one summary row and one heading: {lines}"
    assert "Retired" not in lines and "Moved" not in lines

    heading = len(lines) - 1 - lines[::-1].index("Pharm")
    section = lines[heading:]
    chips = [l for l in section if l in set(CHIPS.values())]
    assert sorted(chips) == sorted(CHIPS.values()), (
        f"the deck's section does not hold all four kinds: {chips}")
    assert chips[-2:] == [CHIPS["retired"], CHIPS["moved"]], (
        f"archived and relocated cards should close the section: {chips}")

    # The retired row no longer repeats the deck name its heading already gives; the
    # moved row still names where it is heading, which the heading cannot say.
    assert "→ Regional" in section
    assert "bulky crisis card" in section


def test_update_decks_gives_a_retired_only_deck_its_own_heading(anki, tmp_path):
    """A deck with no content update at all still heads its own section when the only
    thing pending for it is an archive or a relocation: those rows belong to a deck,
    and no other section would carry them."""
    from internpearls import sync
    from internpearls.widgets import CHIPS
    anki.col.add_note("old1", _fields("bulky crisis card"), TAGS.split())
    folder = _write_source(
        tmp_path, {},
        retired={DECK: {"old1": {"identity": "bulky crisis card", "reason": "split",
                                 "superseded_by": []}}})
    _configure(anki, folder)
    anki.gui.interactive = True
    seen = {}

    def respond(p):
        if p["kind"] != "dialog":
            return {}
        seen.setdefault("lines", _labels(p["tree"]).split("\n"))
        return {"events": [{"id": _find(p["tree"], t="button", label="Cancel")["id"],
                            "click": True}]}

    drive(anki, sync.update_decks, respond)

    lines = seen["lines"]
    assert "Pharm" in lines, f"the retired card's deck should head its row: {lines}"
    assert lines.index("Pharm") < lines.index(CHIPS["retired"])
    assert "1 deck has updates:" not in lines, \
        "nothing is downloading, so there is no deck summary to open the list with"
