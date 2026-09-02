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
        if p["kind"] == "ask":
            return {"answer": True}   # the backup question, when there's nothing to back up
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


def _answer_ask(p, ask):
    """Answer one askUser() question during a replayed flow.

    The automatic-backup question is always answered yes and never routed to a test's
    own `ask`: every flow raises it whenever the collection holds none of the decks the
    run would change (a first sync, most fixtures here), and it is about the backup
    rather than about whatever the test is actually deciding. A test that cares about
    that question drives the flow with its own respond() instead.
    """
    text = p.get("text", "")
    if "back up" in text:
        return {"answer": True}
    return {"answer": True if ask is None else ask(text)}


def _click_update_button(accept, ask=None):
    """respond() for update_decks()'s confirmation. Unlike _click_reconcile_button this
    can't just grab "the first button that isn't Cancel": the confirmation's own card
    rows add their own caret buttons ahead of the accept button in the tree, so that
    search would click a caret instead. It takes the LAST non-Cancel button, which is
    the accept one, since the button box is added after the body. Its label is usually
    "Update" but reads as reconcile's ("Archive and relocate") on a run with no content
    updates pending.

    `ask(text) -> bool` answers the plain askUser() questions the rest of the run raises
    (the backup question when there is nothing to back up yet, and the note-type
    conversion offer), and defaults to yes, the same as _click_sync_button.
    """
    def respond(p):
        if p["kind"] == "ask":
            return _answer_ask(p, ask)
        if p["kind"] != "dialog":
            return {}
        done = _dismiss_result(p["tree"])
        if done:
            return done
        if accept:
            btn = [n for n in _walk(p["tree"])
                   if n.get("t") == "button" and n.get("label") != "Cancel"][-1]
        else:
            btn = _find(p["tree"], t="button", label="Cancel")
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
            return _answer_ask(p, ask)
        return {}   # info/warn: nothing to click, just let it continue
    return respond


def _update(anki, accept=True, ask=None):
    """Run update_decks() to completion, answering its confirmation. Returns every
    widget tree the run showed, in order: the confirmation, then the end-of-run
    summary, which is a dialog rather than an info box now."""
    from internpearls import sync
    trees = []
    click = _click_update_button(accept, ask)

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


def _write_source(tmp_path, decks, retired=None, deck_moves=None, change_notes=None):
    """decks: {deck_name: (version, notes, model_or_None)} -> source folder path.
    `retired`/`deck_moves`/`change_notes`, if given, ride along in the same manifest, so
    update_decks() tests can build a source that carries a content update alongside a
    reconcile ledger or the deck source's own change notes."""
    folder = tmp_path / "source"
    folder.mkdir(exist_ok=True)
    manifest = {"schema": 1, "decks": [], "front_aliases": {},
                "retired": retired or {}, "deck_moves": deck_moves or {},
                "change_notes": change_notes or {}}
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
        [{"name": DECK, "apkg": "Pharm.apkg", "version": "v2"}],
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


# ------------------------------------------------------------- deck-skill consent
def _write_skill(folder, path="skills/deck/SKILL.md", version="1.0",
                 text="# Deck skill v1\nBe concise."):
    """Add a manifest "skill" entry to a source folder _write_source already built,
    and write the skill file itself at that path. Mutates the folder's
    manifest.json in place, same shape update_decks/sync_decks read
    (manifest["skill"] = {"path", "version"})."""
    manifest_path = os.path.join(folder, "manifest.json")
    manifest = json.loads(open(manifest_path, encoding="utf8").read())
    manifest["skill"] = {"path": path, "version": version}
    open(manifest_path, "w", encoding="utf8").write(json.dumps(manifest))
    skill_path = os.path.join(folder, path)
    os.makedirs(os.path.dirname(skill_path), exist_ok=True)
    open(skill_path, "wb").write(text.encode("utf8"))


def _run_with_skill_answer(anki, fn, consent):
    """Drive `fn` (sync_decks or update_decks) to completion. `consent=True/False`
    answers the deck-skill consent dialog ("Use this skill" / "Not now") if one
    appears; `consent=None` asserts NO such dialog appears, so a test asserting
    "no re-ask" fails loudly instead of silently answering a question it didn't
    expect. Every other dialog (an "up to date" info box here, since these tests
    use a source with no deck content) just passes through. Returns the skill
    dialog's own label texts (joined), or "" if none appeared, so a test can check
    what the dialog actually said."""
    seen = []

    def respond(p):
        if p["kind"] != "dialog":
            return {}
        assert consent is not None, f"unexpected dialog: {_label_texts(p['tree'])}"
        seen.append("\n".join(_label_texts(p["tree"])))
        label = "Use this skill" if consent else "Not now"
        btn = _find(p["tree"], t="button", label=label)
        assert btn, f"no {label!r} button in the skill consent dialog"
        return {"events": [{"id": btn["id"], "click": True}]}
    drive(anki, fn, respond)
    return seen[0] if seen else ""


def test_deck_skill_absent_from_manifest_is_a_complete_noop(anki, tmp_path):
    from internpearls import config, sync
    folder = _write_source(tmp_path, {})   # no "skill" key at all
    _configure(anki, folder)

    _run_with_skill_answer(anki, sync.update_decks, consent=None)   # no dialog

    assert config.load_deck_skill() is None


def test_deck_skill_first_appearance_asks_full_text_and_consenting_stores_it(
        anki, tmp_path):
    from internpearls import config, sync
    folder = _write_source(tmp_path, {})
    _write_skill(folder, version="1.0", text="# Deck skill v1\nBe concise.")
    _configure(anki, folder)

    dialog_text = _run_with_skill_answer(anki, sync.update_decks, consent=True)

    stored = config.load_deck_skill()
    assert stored and stored["enabled"] and stored["version"] == "1.0"
    assert "Be concise" in stored["text"]
    assert "Thorough mode" in dialog_text
    assert "Be concise" in dialog_text   # the FULL skill text, not a summary


def test_deck_skill_unchanged_hash_never_reasks(anki, tmp_path):
    from internpearls import config, sync
    folder = _write_source(tmp_path, {})
    _write_skill(folder, version="1.0", text="# Deck skill v1\nBe concise.")
    _configure(anki, folder)
    _run_with_skill_answer(anki, sync.update_decks, consent=True)

    _run_with_skill_answer(anki, sync.update_decks, consent=None)   # no re-ask

    assert "Be concise" in config.load_deck_skill()["text"]


def test_deck_skill_changed_content_reasks_and_declining_keeps_the_old_one(
        anki, tmp_path):
    from internpearls import config, sync
    folder = _write_source(tmp_path, {})
    _write_skill(folder, version="1.0", text="# Deck skill v1\nBe concise.")
    _configure(anki, folder)
    _run_with_skill_answer(anki, sync.update_decks, consent=True)

    _write_skill(folder, version="2.0", text="# Deck skill v2\nNew rules.")
    _run_with_skill_answer(anki, sync.update_decks, consent=False)

    stored = config.load_deck_skill()
    assert stored["enabled"] and "Be concise" in stored["text"]
    assert "New rules" not in stored["text"]


def test_deck_skill_fetch_failure_never_blocks_the_sync(anki, tmp_path):
    """A skill entry pointing at a file that doesn't exist on the source must not
    stop the deck sync itself from completing."""
    from internpearls import config, sync
    folder = _write_source(tmp_path, {
        DECK: ("v1", [("g1", _fields("Front one"), TAGS)], None)})
    manifest_path = os.path.join(folder, "manifest.json")
    manifest = json.loads(open(manifest_path, encoding="utf8").read())
    manifest["skill"] = {"path": "skills/missing/SKILL.md", "version": "1.0"}
    open(manifest_path, "w", encoding="utf8").write(json.dumps(manifest))
    _configure(anki, folder)

    trees = _update(anki)   # no skill dialog fires; the deck-content confirmation does

    assert anki.col.note_by_guid("g1")["Front"] == "Front one"
    assert config.load_deck_skill() is None


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


def test_reconcile_never_archives_a_locally_generated_card_matched_by_front(
        anki, tmp_path):
    """The same front fallback that finds an older-GUID card above must never resolve
    onto one of the learner's own AI-generated cards: its front coinciding with a
    retired entry's identity is not a deck source telling us that card is retired."""
    from internpearls import ai_logic, sync
    card = _her_card(anki, ai_logic.generated_guid(), "bulky crisis card")
    _her_card(anki, "new1a", "focused card A")
    folder = _write_retired_source(tmp_path, {
        DECK: {"canonical_guid": {"identity": "bulky crisis card", "reason": "split",
                                  "superseded_by": ["new1a"]}}})
    _configure(anki, folder)

    sync.reconcile_decks()

    cid = card.card_ids()[0]
    assert anki.col._cards[cid].queue == 0                        # still in review
    assert anki.col.decks.name(anki.col._cards[cid].did) == DECK
    assert any("No retired cards or reorganized decks found" in i for i in anki.gui.infos)


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


def test_reconcile_never_merges_a_locally_generated_card_as_the_stranded_predecessor(
        anki, tmp_path):
    """A generated card's front might coincidentally match the old half of a reworded
    pair. It must never be treated as that stale predecessor: no scheduling carried
    off it onto anything else, and no archiving."""
    from internpearls import ai_logic, sync
    old = _her_card(anki, ai_logic.generated_guid(), "old wording")
    _her_card(anki, "g_new", "new wording")
    _sched(anki, old, reps=4, ivl=12)
    _configure(anki, _stranded_source(tmp_path, {"old wording": "new wording"}))

    sync.reconcile_decks()

    card = anki.col.get_card(old.card_ids()[0])
    assert card.queue == 0 and card.did == anki.col.decks.id_for_name(DECK)
    assert card.reps == 4                                 # her own progress, untouched
    assert any("No retired cards or reorganized decks found" in i for i in anki.gui.infos)


def test_reconcile_never_writes_carried_scheduling_onto_a_generated_successor(
        anki, tmp_path):
    """The other half of the same risk: a generated card's front matching the NEW
    wording of a reworded pair must never receive a predecessor's scheduling, and the
    predecessor must not be archived against it either."""
    from internpearls import ai_logic, sync
    old = _her_card(anki, "g_old", "old wording")
    new = _her_card(anki, ai_logic.generated_guid(), "new wording")
    _sched(anki, old, reps=4, ivl=12, due=90, factor=2300, lapses=1, type=2, queue=2)
    _configure(anki, _stranded_source(tmp_path, {"old wording": "new wording"}))

    sync.reconcile_decks()

    dead = anki.col.get_card(old.card_ids()[0])
    assert dead.queue == 2 and dead.reps == 4             # untouched: not merged/archived
    kept = anki.col.get_card(new.card_ids()[0])
    assert kept.reps == 0                                 # never received old's scheduling
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


def test_reconcile_never_relocates_a_locally_generated_card_matched_by_front(
        anki, tmp_path):
    """Same front-fallback risk on the deck-move ledger as the retired one above: a
    generated card's front must never pull it into a deck source's relocation."""
    from internpearls import ai_logic, sync
    front = "Lidocaine onset time?"
    card = _her_card(anki, ai_logic.generated_guid(), front, deck=DECK)
    folder = _write_retired_source(tmp_path, {}, deck_moves={
        "canonical_guid": {"from": DECK, "to": NEW_DECK, "front": front}})
    _configure(anki, folder)

    sync.reconcile_decks()

    cid = card.card_ids()[0]
    assert anki.col.decks.name(anki.col._cards[cid].did) == DECK
    assert any("No retired cards or reorganized decks found" in i for i in anki.gui.infos)


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


def test_update_decks_never_merges_a_generated_card_in_the_post_sync_recompute(
        anki, tmp_path):
    """update_decks() recomputes stranded pairs a second time after the sync runs (to
    catch a pairing the sync itself just created), and that recompute must carry the
    same generated-card guard the earlier, pre-sync stranded list already has. Without
    it, a generated card whose front coincidentally matches a superseded_fronts pair
    gets merged: the "predecessor's" scheduling overwrites her own card's history and
    her own card gets archived as if it were the stale upstream copy."""
    from internpearls import ai_logic, sync
    gen_guid = ai_logic.generated_guid()
    old = _her_card(anki, gen_guid, "old wording")
    new = _her_card(anki, "g_new", "new wording")
    _sched(anki, old, reps=4, ivl=12, due=90, factor=2300, lapses=1, type=2, queue=2)
    # An unrelated retired card, just so `fresh` is non-empty and update_decks actually
    # enters the archive/merge block the recompute lives inside; a run with nothing
    # pending never reaches it.
    _her_card(anki, "old_other", "an unrelated retiring card")
    _configure(anki, _stranded_and_retired(
        tmp_path, {"old wording": "new wording"},
        {DECK: {"old_other": {"identity": "an unrelated retiring card",
                              "reason": "split", "superseded_by": []}}}))

    trees = _update(anki)

    generated = anki.col.get_card(old.card_ids()[0])
    assert generated.queue == 2 and generated.reps == 4   # her own card, untouched
    assert RETIRED_TAG not in anki.col.note_by_guid(gen_guid).tags  # not archived
    kept = anki.col.get_card(new.card_ids()[0])
    assert kept.reps == 0        # never received the "predecessor's" scheduling
    assert "Merged" not in _summary_text(trees)


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
    # The accept button reads as Reconcile's here, not "Update": this run has no
    # content update pending, only housekeeping, so promising an update would be wrong.
    buttons = [n.get("label") for n in _walk(seen["tree"]) if n.get("t") == "button"]
    assert buttons == ["Archive and relocate", "Cancel"], (
        f"expected only the accept button and Cancel, no expand caret, got {buttons}")


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


def _capture_update_items(monkeypatch):
    """Monkeypatches sync.build_update_body to record the `items` list it's built from
    on every confirmation it renders, while still calling through to the real function
    so the dialog (and drive()'s click-through) behaves exactly as it does normally.

    `items` mixes ("header", ...), ("sep",), and ("card", deck_name, detail) entries;
    this is the only place a test can see a row's raw `detail` dict rather than only
    the text a widget renders from it.
    """
    from internpearls import sync
    captured = []
    real = sync.build_update_body

    def spy(items, *a, **kw):
        captured.append(items)
        return real(items, *a, **kw)

    monkeypatch.setattr(sync, "build_update_body", spy)
    return captured


def _card_detail(items, guid):
    for item in items:
        if item[0] == "card" and item[2]["guid"] == guid:
            return item[2]
    return None


def test_change_notes_attach_to_changed_rows(anki, tmp_path, monkeypatch):
    """A manifest note whose hash matches the incoming content attaches to that
    changed card's detail; a stale-hash note for the same guid does not."""
    from internpearls.logic import note_fields_hash
    anki.col.add_note("g1", _fields("Front one", back="the old answer"), TAGS.split())
    new_fields = _fields("Front one", back="the new answer")
    matching = {"kind": "changed", "note": "rewrote the answer for accuracy",
               "hash": note_fields_hash(new_fields)}
    stale = {"kind": "changed", "note": "an earlier, since-superseded note",
            "hash": note_fields_hash(_fields("Front one", back="a different answer"))}
    folder = _write_source(
        tmp_path, {DECK: ("v2", [("g1", new_fields, TAGS)], None)},
        change_notes={"g1": [stale, matching]})
    _configure(anki, folder)
    captured = _capture_update_items(monkeypatch)

    _update(anki, accept=False)

    detail = _card_detail(captured[0], "g1")
    assert detail["kind"] == "changed"
    assert detail["change_notes"] == [matching]


def test_change_notes_on_new_rows_only_for_installed_decks(anki, tmp_path, monkeypatch):
    """A brand-new card's manifest note is withheld on a deck's first sync (every card
    would carry one, which reads as history it doesn't have) but attached once the deck
    is already installed and this is just one more card arriving."""
    from internpearls.logic import note_fields_hash
    other = "Intern Pearls::Intern Custom::Other"
    _configure(anki, _write_source(tmp_path, {
        DECK: ("v1", [("g1", _fields("Front one"), TAGS)], None)}))
    _sync(anki)   # Pharm is now installed; Other never has been

    new_pharm = _fields("Front two")
    new_other = _fields("Front three")
    change_notes = {
        "g2": [{"kind": "new", "note": "added to round out coverage",
               "hash": note_fields_hash(new_pharm)}],
        "g3": [{"kind": "new", "note": "added to round out coverage",
               "hash": note_fields_hash(new_other)}],
    }
    folder = _write_source(
        tmp_path,
        {DECK: ("v2", [("g1", _fields("Front one"), TAGS),
                       ("g2", new_pharm, TAGS)], None),
         other: ("v1", [("g3", new_other, f"{SCOPE}::Other")], None)},
        change_notes=change_notes)
    _configure(anki, folder)
    captured = _capture_update_items(monkeypatch)

    _update(anki, accept=False)

    pharm_detail = _card_detail(captured[0], "g2")
    other_detail = _card_detail(captured[0], "g3")
    assert pharm_detail["kind"] == "new"
    assert pharm_detail["change_notes"] == change_notes["g2"]
    assert other_detail["kind"] == "new"
    assert "change_notes" not in other_detail


def test_review_box_starts_empty_with_nothing_summarized(anki, tmp_path):
    """Default: the confirmation previews the incoming cards inline, with a cloze
    note's deletions filled in rather than blanked. A row's feedback box is
    contextual, not gated by any setting, so it renders on every row, but starts
    empty; leaving it untouched means nothing ends up summarized or copied at the
    end of the run."""
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
    # The box itself still renders (contextual now, not settings-gated) but starts
    # empty, and nothing is offered to the clipboard.
    assert seen["boxes"], "the feedback box should render on every row"
    assert all(not b["value"] for b in seen["boxes"]), "nothing should pre-fill it"
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


def test_review_collects_feedback_and_offers_the_digest(anki, tmp_path):
    """Boxes appear on every row and the digest is offered on close, no setting
    involved. She sees the answer and the reasoning, not just the front, because
    "this card is wrong" is a judgment you can't make from a prompt alone."""
    from internpearls import sync
    folder = _write_source(tmp_path, {
        DECK: ("v1", [("g2", _fields("Front two", "the answer"), TAGS)], None)})
    _configure(anki, folder)
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

    def counting_fetch(d, **kw):
        calls.append(d["name"])
        return real_fetch(d, **kw)

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

    def counting_fetch(d, **kw):
        calls.append(d["version"])
        return real_fetch(d, **kw)

    sync._preview_content_changes(counting_fetch, todo, her, {})
    assert calls == ["v1"]
    # Source pushes a new version of the same deck.
    folder2 = _write_source(tmp_path, {
        DECK: ("v2", [("g1", _fields("Front one"), TAGS),
                      ("g2", _fields("Front two"), TAGS)], None)})
    _configure(anki, folder2)
    manifest2, real_fetch2, _ = sync._fetch_manifest(sync._cfg())
    todo2 = decks_to_update(manifest2, {}, [])

    def counting_fetch2(d, **kw):
        calls.append(d["version"])
        return real_fetch2(d, **kw)

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
        if p["kind"] == "ask":
            return {"answer": True}   # the backup question, when there's nothing to back up
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
    anki.gui.interactive = True
    seen = {}

    def respond(p):
        if p["kind"] == "ask":
            return _answer_ask(p, None)
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

    # This flow only ever produces these four kinds; CHIPS carries others (the decline
    # kinds) that no row here paints, so the expectation is named explicitly rather
    # than derived from the whole dict.
    flow_kinds = [CHIPS["new"], CHIPS["changed"], CHIPS["retired"], CHIPS["moved"]]
    heading = len(lines) - 1 - lines[::-1].index("Pharm")
    section = lines[heading:]
    chips = [l for l in section if l in set(CHIPS.values())]
    assert sorted(chips) == sorted(flow_kinds), (
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


# ------------------------------------------------- persistence & guards
def test_protected_notes_are_restored_even_if_a_later_step_raises(anki, tmp_path,
                                                                  monkeypatch):
    """The import overwrites every field on a matched note, so the restore that puts her
    annotations back is the only thing standing between a sync and losing them. It works
    off `touched`, and `touched` used to be recorded after seed_converted_siblings ran:
    anything raising in between dropped that deck out of the set entirely, so its notes
    were never restored and never recorded as shipped, silently and permanently."""
    from internpearls import sync

    def boom(_nids):
        raise RuntimeError("seeding blew up")

    anki.col.add_note("g1", _fields("Front one", notes="her mnemonic"), [TAGS], deck=DECK)
    _configure(anki, _write_source(tmp_path, {
        DECK: ("v2", [("g1", _fields("Front one", back="new back"), TAGS)], None)}))
    monkeypatch.setattr(sync, "seed_converted_siblings", boom)

    trees = _sync(anki)

    assert anki.col.note_by_guid("g1")["Back"] == "new back"      # the import landed
    assert anki.col.note_by_guid("g1")["Notes"] == "her mnemonic"  # and so did the restore
    assert "seeding blew up" in _summary_text(trees)               # reported, not swallowed


def test_sync_keeps_a_version_another_run_recorded_while_it_worked(anki, tmp_path,
                                                                   monkeypatch):
    """installed.json used to be written back from the snapshot the run started with,
    which on a multi-deck sync can be minutes old by the time it saves. Anything a
    concurrent run (an auto-sync poll firing mid-flight) recorded in between was
    reverted, so that deck was offered again on the next check."""
    from internpearls import sync
    other = "Intern Pearls::Intern Custom::Other"
    _configure(anki, _write_source(tmp_path, {
        DECK: ("v1", [("g1", _fields("Front one"), TAGS)], None)}))
    real_apply = sync._apply_deck

    def apply_and_interleave(*a, **kw):
        sync._save_json(sync.INSTALLED,
                        {**sync._load_json(sync.INSTALLED, {}), other: "v9"})
        return real_apply(*a, **kw)

    monkeypatch.setattr(sync, "_apply_deck", apply_and_interleave)

    _sync(anki)

    assert json.load(open(sync.INSTALLED, encoding="utf8")) == {DECK: "v1", other: "v9"}


def test_auto_sync_skips_a_tick_while_a_manual_flow_is_running(anki, tmp_path):
    """Both write the collection and both persist installed.json, and the poll's apply
    half arrives from a QueryOp callback that can land inside a manual flow's own modal
    event loop. It should stay quiet and try again next tick."""
    from internpearls import background, ui
    folder = _write_source(tmp_path, {
        DECK: ("v1", [("g1", _fields("Front one"), TAGS)], None)})
    anki.mw._config = {"decks_dir": folder, "auto_sync_decks": True}
    ui._manual_in_progress = True
    try:
        background._auto_sync_check()
    finally:
        ui._manual_in_progress = False

    assert not anki.col.imports
    assert not anki.gui.tooltips
    assert not background._auto_sync_in_progress   # and the flag isn't left stuck on

    background._auto_sync_check()   # nothing held now: the next tick picks it up

    assert anki.col.note_by_guid("g1")["Front"] == "Front one"


def test_auto_sync_releases_its_guard_when_a_poll_finds_nothing(anki, tmp_path):
    """The flag is taken for the whole run now, fetch phase included, so every exit path
    out of the apply callback has to clear it. The no-op path is the one that runs on
    almost every poll, so a leak there wedges auto-sync for the rest of the session."""
    from internpearls import background
    folder = _write_source(tmp_path, {
        DECK: ("v1", [("g1", _fields("Front one"), TAGS)], None)})
    anki.mw._config = {"decks_dir": folder, "auto_sync_decks": True}
    background._auto_sync_check()
    assert not background._auto_sync_in_progress

    background._auto_sync_check()   # this one finds nothing to do

    assert not background._auto_sync_in_progress


def test_a_manual_sync_is_guarded_from_its_first_call_to_its_last(anki, tmp_path,
                                                                  monkeypatch):
    """Observed from inside the flow rather than from the replay driver: Runner unwinds
    the whole call at every dialog and re-runs it from a snapshot, so respond() only
    ever sees the flag after the flow has already returned. Hooking two points that sit
    on either side of the confirmation shows it held across it."""
    from internpearls import sync, ui
    _configure(anki, _write_source(tmp_path, {
        DECK: ("v1", [("g1", _fields("Front one"), TAGS)], None)}))
    held = []
    real_backup, real_apply = sync._pre_sync_backup_or_confirm_skip, sync._apply_deck

    def watch(fn):
        def wrapper(*a, **kw):
            held.append(ui.manual_sync_in_progress())
            return fn(*a, **kw)
        return wrapper

    monkeypatch.setattr(sync, "_pre_sync_backup_or_confirm_skip", watch(real_backup))
    monkeypatch.setattr(sync, "_apply_deck", watch(real_apply))

    _sync(anki)

    # Runner replays the whole flow once per dialog, so both hooks fire more than once.
    assert len(held) >= 2 and all(held)        # before and after the confirmation
    assert not ui.manual_sync_in_progress()    # released once the flow returns


# ------------------------------------------------------------- broken deck source
def _warned(anki, fn):
    fn()
    return " ".join(anki.gui.warnings)


def test_a_missing_local_folder_says_so_rather_than_not_configured(anki, tmp_path):
    from internpearls import sync
    anki.mw._config = {"decks_dir": str(tmp_path / "nope")}

    text = _warned(anki, sync.sync_decks)

    assert "doesn't exist" in text
    assert "No deck source configured" not in text


def test_a_folder_without_a_manifest_says_which_file_is_missing(anki, tmp_path):
    from internpearls import sync
    folder = tmp_path / "empty"
    folder.mkdir()
    anki.mw._config = {"decks_dir": str(folder)}

    text = _warned(anki, sync.sync_decks)

    assert "manifest.json" in text
    assert "No deck source configured" not in text


def test_a_corrupt_manifest_says_it_is_not_valid_json(anki, tmp_path):
    from internpearls import sync
    folder = tmp_path / "broken"
    folder.mkdir()
    (folder / "manifest.json").write_text("{not json at all", encoding="utf8")
    anki.mw._config = {"decks_dir": str(folder)}

    text = _warned(anki, sync.sync_decks)

    assert "isn't valid JSON" in text
    assert "No deck source configured" not in text


def test_nothing_configured_still_reads_as_nothing_configured(anki):
    from internpearls import sync
    anki.mw._config = {}

    text = _warned(anki, sync.sync_decks)

    assert "No deck source configured" in text


def test_load_json_distinguishes_an_absent_file_from_a_corrupt_one(tmp_path):
    import pytest
    from internpearls.config import _load_json
    missing = str(tmp_path / "gone.json")
    corrupt = tmp_path / "bad.json"
    corrupt.write_text("{", encoding="utf8")

    assert _load_json(missing, {"fallback": 1}, strict=True) == {"fallback": 1}
    with pytest.raises(Exception):
        _load_json(str(corrupt), {"fallback": 1}, strict=True)
    assert _load_json(str(corrupt), {"fallback": 1}) == {"fallback": 1}   # unchanged


def test_save_json_leaves_the_old_file_intact_when_the_write_fails(tmp_path, monkeypatch):
    from internpearls import config
    path = str(tmp_path / "installed.json")
    config._save_json(path, {"deck": "v1"})

    def blow_up(*_a, **_k):
        raise RuntimeError("disk full")

    monkeypatch.setattr("json.dump", blow_up)
    try:
        config._save_json(path, {"deck": "v2"})
    except RuntimeError:
        pass

    assert json.load(open(path, encoding="utf8")) == {"deck": "v1"}
    assert [f for f in os.listdir(tmp_path) if f.endswith(".tmp")] == []


# ------------------------------------------------------------------- backup scope
def test_backup_covers_every_root_the_run_touches(anki, tmp_path):
    """The backup used to be export_deck's subtree and nothing else, so a run changing a
    deck filed anywhere else got a backup covering none of what it was about to
    overwrite, while the confirmation promised one."""
    from internpearls import collection
    outside = "Other Root::Extra"
    anki.col.add_note("g1", _fields("Front one"), [TAGS], deck=DECK)
    anki.col.add_note("g2", _fields("Front two"), [TAGS], deck=outside)
    _configure(anki, _write_source(tmp_path, {
        DECK: ("v2", [("g1", _fields("Front one", back="new"), TAGS)], None),
        outside: ("v2", [("g2", _fields("Front two", back="new"), TAGS)], None)}))

    _sync(anki)

    made = os.listdir(collection._deck_backup_folder())
    assert len(made) == 2, made
    labels = {collection._label_of_backup(f) for f in made}
    assert labels == {collection._backup_label("Intern Pearls"),
                      collection._backup_label("Other Root")}


def test_backup_stays_one_file_when_export_deck_covers_the_run(anki, tmp_path):
    """The ordinary case is unchanged: one deck-scoped export of export_deck, named
    exactly as it always was."""
    from internpearls import collection
    anki.col.add_note("g1", _fields("Front one"), [TAGS], deck=DECK)
    _configure(anki, _write_source(tmp_path, {
        DECK: ("v2", [("g1", _fields("Front one", back="new"), TAGS)], None)}))

    _sync(anki)

    made = os.listdir(collection._deck_backup_folder())
    assert len(made) == 1 and made[0].startswith("Intern Pearls 2")


def test_backup_asks_before_a_run_it_cannot_cover(anki, tmp_path):
    """Her cards are under the scope tag but in no deck this add-on can export, so the
    promised backup can't be taken. That used to pass silently."""
    from internpearls import sync
    anki.col.add_note("g1", _fields("Front one"), [TAGS])   # tagged, but in no deck
    _configure(anki, _write_source(tmp_path, {
        DECK: ("v2", [("g1", _fields("Front one", back="new back"), TAGS)], None)}))
    asked = []

    def respond(p):
        if p["kind"] == "ask":
            asked.append(p["text"])
            return {"answer": False}          # she declines to go without one
        if p["kind"] != "dialog":
            return {}
        done = _dismiss_result(p["tree"])
        if done:
            return done
        return {"events": [{"id": _find(p["tree"], t="button", label="Update")["id"],
                            "click": True}]}

    drive(anki, sync.sync_decks, respond)
    anki.gui.interactive = False

    assert any("back up" in t for t in asked), asked
    assert anki.col.note_by_guid("g1")["Back"] == "the back"   # declined, nothing imported


def test_a_first_sync_with_nothing_to_back_up_asks_nothing(anki, tmp_path):
    """An empty collection has nothing at risk, so the step is skipped outright rather
    than asking about a backup that would be of nothing."""
    _configure(anki, _write_source(tmp_path, {
        DECK: ("v1", [("g1", _fields("Front one"), TAGS)], None)}))

    _sync(anki)

    assert anki.gui.asks == []
    assert anki.col.note_by_guid("g1")["Front"] == "Front one"


# ------------------------------------------------------- import single deck (manual)
def _basic_model_missing(field):
    model = make_model()
    model["flds"] = [f for f in model["flds"] if f["name"] != field]
    return model


def test_import_single_declined_leaves_the_schema_alone(anki, tmp_path):
    """Fix-note-types used to run before the "Import now?" question, so saying No could
    still have added a field and bumped the collection schema, which costs a one-time
    full AnkiWeb sync. Nothing may touch the collection before consent."""
    from internpearls import sync
    anki.col.models._models[:] = [_basic_model_missing("Dosing")]
    src = str(tmp_path / "hand.apkg")
    make_apkg(src, [("g1", _fields("Front one"), TAGS)], deck=DECK)
    anki.gui.file_picks = [src]
    anki.gui.answers = [False]        # No to "Import now?"

    sync.import_single()

    assert [f["name"] for f in anki.col.models.all()[0]["flds"]].count("Dosing") == 0
    assert anki.col.scm == 0          # no schema bump
    assert not anki.col.imports


def test_import_single_accepted_still_fixes_note_types_first(anki, tmp_path):
    """Consent given, the same sequence as a sync runs: back up, fix note types, then
    import, so the missing field exists before anything is written into it."""
    from internpearls import sync
    anki.col.models._models[:] = [_basic_model_missing("Dosing")]
    src = str(tmp_path / "hand.apkg")
    make_apkg(src, [("g1", _fields("Front one"), TAGS)], deck=DECK)
    anki.gui.file_picks = [src]

    sync.import_single()

    assert "Dosing" in [f["name"] for f in anki.col.models.all()[0]["flds"]]
    assert anki.col.note_by_guid("g1")["Front"] == "Front one"


def test_import_single_writes_its_personalized_copy_outside_the_source_file(anki,
                                                                            tmp_path):
    """The personalized copy used to be written as `<the file she picked>.sync.apkg`, a
    predictable path in a folder that is hers rather than ours."""
    from internpearls import sync
    src = str(tmp_path / "hand.apkg")
    make_apkg(src, [("g1", _fields("Front one"), TAGS)], deck=DECK)
    anki.gui.file_picks = [src]

    sync.import_single()

    assert not os.path.exists(src + ".sync.apkg")
    assert not [f for f in os.listdir(tmp_path) if f.endswith(".sync.apkg")]


def test_import_single_filters_a_never_declined_card(anki, tmp_path):
    """Import single deck used to build its own scratch import with no decline filter
    at all, so a hand-picked .apkg could re-import a card she said Never to. It must be
    dropped the same way a regular sync drops it, and the registry entry survives."""
    from internpearls import config, sync
    config.save_declined({"g1": {"state": "never", "front": "Front one", "deck": DECK,
                                 "decided": "2026-08-01", "hash": ""}})
    src = str(tmp_path / "hand.apkg")
    make_apkg(src, [("g1", _fields("Front one"), TAGS)], deck=DECK)
    anki.gui.file_picks = [src]

    sync.import_single()

    fronts = {n.fields[0] for n in anki.col._notes.values()}
    assert "Front one" not in fronts
    assert config.load_declined()["g1"]["state"] == "never"


# --------------------------------------------------- reconcile nudge (auto-sync)
def _stranded_and_retired(tmp_path, superseded, retired):
    folder = tmp_path / "source"
    folder.mkdir(exist_ok=True)
    (folder / "manifest.json").write_text(json.dumps({
        "schema": 2, "decks": [], "front_aliases": {}, "retired": retired,
        "deck_moves": {}, "superseded_fronts": superseded}), encoding="utf8")
    return str(folder)


def test_auto_sync_counts_reworded_pairs_as_pending_too(anki, tmp_path):
    """reconcile_decks and update_decks both treat a reworded pair as pending work, so
    a nudge that left them out disagreed with the screen it points at, and a backlog of
    nothing but reworded pairs was never mentioned at all."""
    from internpearls import background, sync
    stub = _StubAction()
    sync.register_reconcile_action(stub)
    _her_card(anki, "g_old", "old wording")
    _her_card(anki, "g_new", "new wording")
    anki.mw._config = {"decks_dir": _stranded_source(tmp_path, {"old wording":
                                                                "new wording"}),
                       "auto_sync_decks": True}

    background._auto_sync_check()

    assert stub.text == "Reconcile my decks (1 pending)"
    assert any("1 card is ready to tidy up" in t for t in anki.gui.tooltips)
    assert anki.col.note_by_guid("g_old").tags == TAGS.split()   # nudged, not acted on


def test_auto_sync_does_not_renag_when_the_pending_count_shrinks(anki, tmp_path):
    """The nudge is documented as firing when a backlog first appears or grows. A plain
    inequality also fired on a shrink, so tidying part of a backlog immediately nagged
    about the smaller remainder."""
    from internpearls import background, sync
    stub = _StubAction()
    sync.register_reconcile_action(stub)
    anki.col.add_note("old1", _fields("first retired card"), TAGS.split())
    anki.col.add_note("old2", _fields("second retired card"), TAGS.split())
    both = {DECK: {
        "old1": {"identity": "first retired card", "reason": "split", "superseded_by": []},
        "old2": {"identity": "second retired card", "reason": "split",
                 "superseded_by": []}}}
    anki.mw._config = {"decks_dir": _write_source(tmp_path, {}, retired=both),
                       "auto_sync_decks": True}
    background._auto_sync_check()
    assert len(anki.gui.tooltips) == 1

    # She tidies one of them up by hand; one is left pending.
    del anki.col._notes[anki.col.note_by_guid("old2").id]

    background._auto_sync_check()

    assert len(anki.gui.tooltips) == 1                      # no nag on the way down
    assert stub.text == "Reconcile my decks (1 pending)"    # label still honest


def test_auto_sync_nags_again_once_a_shrunken_backlog_grows(anki, tmp_path):
    """The watermark follows the count down, so growth after a partial tidy-up is a
    first appearance again rather than being suppressed forever."""
    from internpearls import background, sync
    sync.register_reconcile_action(_StubAction())
    anki.col.add_note("old1", _fields("first retired card"), TAGS.split())
    anki.col.add_note("old2", _fields("second retired card"), TAGS.split())
    entry = {"reason": "split", "superseded_by": []}
    both = {DECK: {"old1": {"identity": "first retired card", **entry},
                   "old2": {"identity": "second retired card", **entry}}}
    anki.mw._config = {"decks_dir": _write_source(tmp_path, {}, retired=both),
                       "auto_sync_decks": True}
    background._auto_sync_check()
    del anki.col._notes[anki.col.note_by_guid("old2").id]
    background._auto_sync_check()          # shrank to 1, quiet
    assert len(anki.gui.tooltips) == 1

    anki.col.add_note("old2", _fields("second retired card"), TAGS.split())
    background._auto_sync_check()          # back up to 2

    assert len(anki.gui.tooltips) == 2
    assert "2 cards are ready to tidy up" in anki.gui.tooltips[-1]


# ----------------------------------------------------------- cancel during a download
def test_cached_fetch_passes_the_pump_through_to_the_fetch(anki, tmp_path):
    """cancellable_progress's step.pump is what keeps Cancel live during a download, and
    it only reaches the socket if every layer between forwards it."""
    from internpearls import sync
    seen = {}

    def fetch(d, on_chunk=None):
        seen["on_chunk"] = on_chunk
        return str(tmp_path / "x.apkg")

    def pump(_n=None):
        return True

    sync._cached_fetch(fetch, {"name": "deck", "version": "v1"}, on_chunk=pump)

    assert seen["on_chunk"] is pump


def test_a_cancelled_download_reads_as_cancelled_not_as_a_failed_deck(anki, tmp_path):
    """Cancel clicked mid-download is the same answer as Cancel between decks: whatever
    finished stays applied, and the run reports itself as stopped rather than showing a
    "✗ deck failed" row for something that did not fail."""
    from internpearls import sync
    from internpearls.net import DownloadCancelled
    manifest = {"decks": [], "front_aliases": {}}
    todo = [{"name": DECK, "apkg": "a.apkg", "version": "v1"}]

    def fetch(_d, **_kw):
        raise DownloadCancelled("cancelled before anything was imported")

    results, _restored, _tpl, _deferred, cancelled, _col, _conv = sync._run_sync(
        sync._cfg(), manifest, fetch, todo)

    assert cancelled is True
    assert results == []
    assert json.load(open(sync.INSTALLED, encoding="utf8")) == {}


# ------------------------------------------ note-type conversions on Update my decks
def test_update_asks_once_about_conversions_before_anything_imports(anki, tmp_path):
    """The question used to pop per deck from inside the apply loop, under the progress
    dialog, after the import had already started. The preview already downloaded every
    pending deck, so the whole run's conversions are known before the loop begins."""
    from internpearls import sync
    other = "Intern Pearls::Intern Custom::Other"
    anki.col.models._models.append(_cloze_model())
    _her_card(anki, "g1", "Old Q and A front")
    anki.col.add_note("g2", _fields("Another Q and A front"), TAGS.split(), deck=other)
    _configure(anki, _write_source(tmp_path, {
        DECK: ("v2", [("g1", ["A {{c1::cloze}} version", "why", "", "", ""], TAGS)],
               _cloze_model()),
        other: ("v2", [("g2", ["A {{c1::second}} version", "why", "", "", ""], TAGS)],
                _cloze_model())}))
    asked = []

    def ask(text):
        if "changed format" in text:
            asked.append(len(anki.col.imports))
        return True

    _update(anki, ask=ask)

    assert asked == [0], "one question, and before the first import"
    assert anki.col.note_by_guid("g1").note_type()["name"] == "Study Deck - Cloze"
    assert anki.col.note_by_guid("g2").note_type()["name"] == "Study Deck - Cloze"


def test_update_declining_the_conversion_still_imports_without_asking_again(anki,
                                                                            tmp_path):
    from internpearls import sync
    anki.col.models._models.append(_cloze_model())
    _her_card(anki, "g1", "Old Q and A front")
    _configure(anki, _write_source(tmp_path, {
        DECK: ("v2", [("g1", ["A {{c1::cloze}} version", "why", "", "", ""], TAGS)],
               _cloze_model())}))
    asked = []

    def ask(text):
        asked.append(text)
        return False

    _update(anki, ask=ask)

    assert sum("changed format" in t for t in asked) == 1
    assert anki.col.note_by_guid("g1").note_type()["name"] == "Study Deck - Basic"
    assert anki.col.imports    # the content still imported, beside her old note


def test_update_confirmation_says_a_conversion_is_coming(anki, tmp_path):
    """The one question is asked after the confirmation, so the confirmation is where
    the reader first hears about it rather than being surprised by it."""
    from internpearls import sync
    anki.col.models._models.append(_cloze_model())
    _her_card(anki, "g1", "Old Q and A front")
    _configure(anki, _write_source(tmp_path, {
        DECK: ("v2", [("g1", ["A {{c1::cloze}} version", "why", "", "", ""], TAGS)],
               _cloze_model())}))

    trees = _update(anki, accept=False)

    assert "changed format" in "\n".join(_label_texts(trees[0]))


def _fetch_that_fails_during_preview(sync, monkeypatch):
    """Make every deck download raise while the preview runs, and succeed afterwards.

    A transient source hiccup, pinned to the phase rather than to the attempt number so
    it survives Runner's replay (which re-runs the whole flow from a snapshot per
    dialog, and would reset any counter-based flakiness).
    """
    phase = {"previewing": False}
    real_preview, real_fetch = sync._preview_content_changes, sync._cached_fetch

    def preview(*a, **kw):
        phase["previewing"] = True
        try:
            return real_preview(*a, **kw)
        finally:
            phase["previewing"] = False

    def flaky(fetch, d, on_chunk=None):
        if phase["previewing"]:
            raise RuntimeError("the source hiccuped")
        return real_fetch(fetch, d, on_chunk=on_chunk)

    monkeypatch.setattr(sync, "_preview_content_changes", preview)
    monkeypatch.setattr(sync, "_cached_fetch", flaky)


def test_a_deck_that_could_not_be_previewed_really_does_still_import(anki, tmp_path,
                                                                     monkeypatch):
    """The row promises the deck still imports, and now it does. The preview's own
    download failure used to be cached and re-raised at apply time, so a deck whose
    preview failed was guaranteed to fail the import too: the row said one thing and the
    run always did the other. The apply step fetches it again instead."""
    from internpearls import sync
    _configure(anki, _write_source(tmp_path, {
        DECK: ("v1", [("g1", _fields("Front one"), TAGS)], None)}))
    _fetch_that_fails_during_preview(sync, monkeypatch)

    trees = _update(anki)

    assert "couldn't preview · still imports" in "\n".join(_label_texts(trees[0]))
    assert anki.col.note_by_guid("g1")["Front"] == "Front one"
    assert json.load(open(sync.INSTALLED, encoding="utf8")) == {DECK: "v1"}


def test_a_deck_the_retry_cannot_fetch_either_reports_a_failed_row(anki, tmp_path):
    """The honest other half: a deck that is broken rather than briefly unreachable
    still ends the run with a ✗ row naming it, rather than the retry papering over a
    real failure."""
    from internpearls import sync
    folder = _write_source(tmp_path, {
        DECK: ("v1", [("g1", _fields("Front one"), TAGS)], None)})
    with open(os.path.join(folder, "Pharm.apkg"), "wb") as fh:
        fh.write(b"not an apkg at all")
    _configure(anki, folder)

    trees = _update(anki)

    assert "couldn't preview · still imports" in "\n".join(_label_texts(trees[0]))
    assert "✗ <b>Pharm</b>" in _summary_text(trees)
    assert not anki.col.find_notes(f'"tag:{SCOPE}"')
    assert json.load(open(sync.INSTALLED, encoding="utf8")) == {}


def test_a_deck_whose_file_only_failed_to_parse_keeps_the_file(anki, tmp_path):
    """The download succeeded and only the read of it failed, so the downloaded file is
    what the apply step should get. Recording the parse error over it instead threw away
    a good file and made the apply step fetch the same deck all over again."""
    from internpearls import sync
    from internpearls.config import _cfg
    folder = _write_source(tmp_path, {
        DECK: ("v1", [("g1", _fields("Front one"), TAGS)], None)})
    apkg = os.path.join(folder, "Pharm.apkg")
    with open(apkg, "wb") as fh:
        fh.write(b"not an apkg at all")
    _configure(anki, folder)
    manifest, fetch, _source = sync._fetch_manifest(_cfg())

    preview, downloaded, cancelled = sync._preview_content_changes(
        fetch, manifest["decks"], {}, {})

    assert not cancelled and preview[DECK] is None
    assert downloaded[DECK] == apkg


# ------------------------------------------------------------- backup scope, part two
def _backed_up_decks(anki):
    """Every deck this run's automatic backups actually exported, by name."""
    return [anki.col.decks.name(limit.deck_id) for _p, _o, limit in anki.col.exports]


def test_backup_covers_the_deck_a_reworded_pair_actually_sits_in(anki, tmp_path):
    """A stranded pair is in neither ledger, so its decks were in no backup target list,
    yet merging one rewrites scheduling on the successor and archives the predecessor.
    Both halves here sit outside export_deck entirely."""
    outside = "Other Root::Extra"
    _her_card(anki, "old", "the older wording", deck=outside)
    _her_card(anki, "new", "the newer wording", deck=outside)
    _configure(anki, _stranded_and_retired(
        tmp_path, {"the older wording": "the newer wording"}, {}))

    _reconcile_tree(anki, accept=True)

    assert "Other Root" in _backed_up_decks(anki)


def test_backup_prefers_where_a_retired_card_is_now_over_the_ledgers_deck(anki,
                                                                          tmp_path):
    """The ledger records the deck the source retired the card OUT of, which stops being
    true the moment the learner refiles her copy. The backup has to cover where the card
    is, since that is the deck about to be written in."""
    hers = "Other Root::Where she filed it"
    _her_card(anki, "old1", "bulky crisis card", deck=hers)
    _her_card(anki, "new1", "focused card", deck=hers)
    _configure(anki, _write_retired_source(tmp_path, {
        DECK: {"old1": {"identity": "bulky crisis card", "reason": "split",
                        "superseded_by": ["new1"]}}}))

    _reconcile_tree(anki, accept=True)

    assert "Other Root" in _backed_up_decks(anki)


def test_auto_sync_backs_up_a_deck_filed_outside_export_deck(anki, tmp_path):
    """Unattended sync imports whatever the manifest lists, exactly like a manual one,
    but it used to back up export_deck and nothing else, so anything filed elsewhere
    was rewritten with no backup covering it and no one there to notice."""
    from internpearls import background
    outside = "Other Root::Extra"
    anki.col.add_note("g2", _fields("Front two"), [TAGS], deck=outside)
    _configure(anki, _write_source(tmp_path, {
        outside: ("v2", [("g2", _fields("Front two", back="new"), TAGS)], None)}))
    anki.mw._config["auto_sync_decks"] = True

    background._auto_sync_check()

    assert "Other Root" in _backed_up_decks(anki)
    assert anki.col.note_by_guid("g2")["Back"] == "new"


def test_import_single_backs_up_the_decks_the_chosen_file_lands_in(anki, tmp_path):
    """The one flow that knew nothing about its own scope: it passed no deck list at
    all, so a hand-picked package filing cards outside export_deck imported over them
    with a backup of somewhere else."""
    from internpearls import sync
    outside = "Other Root::Extra"
    anki.col.add_note("g1", _fields("Front one"), [TAGS], deck=outside)
    src = str(tmp_path / "hand.apkg")
    make_apkg(src, [("g1", _fields("Front one", back="new"), TAGS)], deck=outside)
    anki.gui.file_picks = [src]

    sync.import_single()

    assert "Other Root" in _backed_up_decks(anki)


# ------------------------------------------- the guard around collection-writing work
def test_every_collection_writing_action_holds_the_manual_guard(anki, tmp_path,
                                                                monkeypatch):
    """Auto-sync's apply half arrives from a QueryOp callback, which can land inside the
    modal event loop any of these runs in. Each one writes the collection, so an
    interleaved import can arrive mid-action: worst case Remove empty cards deletes a
    card that import just gave content back to. Only the sync flows used to hold the
    guard, so all four of these were open windows.
    """
    from internpearls import collection, sync, ui
    held = []

    def watch(module, name):
        real = getattr(module, name)

        def wrapper(*a, **kw):
            held.append((name, ui.manual_sync_in_progress()))
            return real(*a, **kw)

        monkeypatch.setattr(module, name, wrapper)

    watch(sync, "_her_notes_summary")          # clean_up_duplicates, before its dialog
    watch(collection, "_import_apkg")          # import_deck, at the write itself
    watch(collection, "invalidate_installed")  # restore_from_backup, before the reload
    watch(collection, "find_empty_cards")      # remove_empty_cards, at the report

    _configure(anki, _write_retired_source(tmp_path, {}))
    anki.col.add_note("dup1", _fields("A shared front"), TAGS.split(), deck=DECK)
    anki.col.add_note("dup2", _fields("A shared front"), TAGS.split(), deck=DECK)
    drive(anki, sync.clean_up_duplicates, _click_duplicate_button(accept=True))
    anki.gui.interactive = False

    src = str(tmp_path / "hand.apkg")
    make_apkg(src, [("dup1", _fields("A shared front"), TAGS)], deck=DECK)
    anki.gui.file_picks = [src]
    collection.import_deck()
    collection.restore_from_backup()
    drive(anki, collection.remove_empty_cards, _click_duplicate_button(accept=True))
    anki.gui.interactive = False

    assert {name for name, _ in held} == {
        "_her_notes_summary", "_import_apkg", "invalidate_installed",
        "find_empty_cards"}
    assert all(ok for _name, ok in held), held
    assert not ui.manual_sync_in_progress()   # and every one of them released it


def test_remove_empty_cards_acts_on_the_cards_still_empty_after_the_confirmation(
        anki, monkeypatch):
    """The report is computed before a modal dialog someone can sit in indefinitely, and
    an import landing while it is open can give one of those cards its content back.
    Acting on the stale list would delete a card that now shows something, which is the
    one thing this add-on must never do."""
    from internpearls import collection
    _her_cloze(anki, "regrouped", "the {{c1::first}} and {{c2::second}}", ords=[0, 1, 2])
    real_backup = collection._pre_sync_backup_or_confirm_skip

    def backup_then_refill(*a, **kw):
        out = real_backup(*a, **kw)
        # Stands in for whatever landed while the confirmation was open: the third
        # blank is back, so the card it generates is no longer empty.
        anki.col.note_by_guid("regrouped").fields[0] = (
            "the {{c1::first}} and {{c2::second}} and {{c3::third}}")
        return out

    monkeypatch.setattr(collection, "_pre_sync_backup_or_confirm_skip",
                        backup_then_refill)

    drive(anki, collection.remove_empty_cards, _click_duplicate_button(accept=True))
    anki.gui.interactive = False

    assert len(anki.col.note_by_guid("regrouped")._card_ids) == 3   # nothing removed
    assert any("aren't empty any more" in i for i in anki.gui.infos)


# ----------------------------------------------------------- a GitHub deck source
def _github_source(anki, monkeypatch, files, repo="someone/decks"):
    """Point the add-on at a GitHub repo and serve `files` ({path: bytes}) for it.

    The fetch itself is the only thing stubbed: everything downstream (the scratch
    download, the cache, the import) is the real code, which is what makes a
    collision between two decks' file paths visible here at all.
    """
    from internpearls import sync
    anki.mw._config = {"github_decks_repo": repo}
    asked = []

    def gh_raw(_repo, path, _token, _ref, timeout=None, on_chunk=None):
        asked.append(path)
        if path not in files:
            raise RuntimeError(f"404 for {path}")
        return files[path]

    monkeypatch.setattr(sync, "_gh_raw", gh_raw)
    return asked


def _apkg_bytes(tmp_path, name, notes, deck):
    path = str(tmp_path / name)
    make_apkg(path, notes, deck=deck)
    return open(path, "rb").read()


def test_two_decks_whose_files_share_a_basename_import_their_own_content(
        anki, tmp_path, monkeypatch):
    """A source can file two decks as decks/basics/Deck.apkg and decks/extra/Deck.apkg.
    The download was keyed by basename in one shared directory, so the second overwrote
    the first and the apply step imported one deck's cards twice."""
    other = "Intern Pearls::Intern Custom::Other"
    manifest = {"schema": 1, "front_aliases": {}, "retired": {}, "deck_moves": {},
                "decks": [{"name": DECK, "apkg": "decks/basics/Deck.apkg",
                           "version": "v1", "cards": 1},
                          {"name": other, "apkg": "decks/extra/Deck.apkg",
                           "version": "v1", "cards": 1}]}
    _github_source(anki, monkeypatch, {
        "manifest.json": json.dumps(manifest).encode("utf8"),
        "decks/basics/Deck.apkg": _apkg_bytes(
            tmp_path, "a.apkg", [("g1", _fields("Front one"), TAGS)], DECK),
        "decks/extra/Deck.apkg": _apkg_bytes(
            tmp_path, "b.apkg", [("g2", _fields("Front two"), TAGS)], other)})

    _sync(anki)

    assert anki.col.note_by_guid("g1")["Front"] == "Front one"
    assert anki.col.note_by_guid("g2")["Front"] == "Front two"


def test_two_deck_paths_never_share_a_scratch_file(anki):
    """The keying itself, stated once: same filename, different folder, different
    download location."""
    from internpearls import sync

    assert (sync._scratch_path("decks/basics/Deck.apkg")
            != sync._scratch_path("decks/extra/Deck.apkg"))
    assert sync._scratch_path("decks/basics/Deck.apkg").endswith("Deck.apkg")


def test_a_download_is_renamed_into_place_rather_than_written_over(anki, monkeypatch):
    """The background poll writes these from a worker thread while an interactive
    preview can be reading the same keyed path. A plain write is readable half finished;
    a rename is not, so the reader sees either the old file or the whole new one."""
    from internpearls import sync
    real_replace = os.replace
    renames = []

    def replace(src, dst):
        renames.append((src, dst))
        assert open(src, "rb").read() == b"the whole file"
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace)

    path = sync._write_scratch("decks/Deck.apkg", b"the whole file")

    assert renames and renames[0][1] == path and renames[0][0] != path
    assert open(path, "rb").read() == b"the whole file"
    assert not [f for f in os.listdir(os.path.dirname(path)) if f.endswith(".part")]


def test_a_github_manifest_that_is_not_json_says_so(anki, monkeypatch):
    """It used to read as "Couldn't reach the deck source: Expecting value line 1", so a
    reachable repo serving a broken file looked like a network or token problem. The
    local-folder branch has always said which of the two it is."""
    from internpearls import sync
    _github_source(anki, monkeypatch, {"manifest.json": b"<html>not json</html>"})

    warned = _warned(anki, sync.update_decks)

    assert "manifest.json in someone/decks isn't valid JSON" in warned


def test_an_empty_github_manifest_reads_as_broken_not_unconfigured(anki, monkeypatch):
    """A repo serving {} is configured and broken. Saying "No deck source configured
    yet" sent the reader off to configure a source that was already there."""
    from internpearls import sync
    _github_source(anki, monkeypatch, {"manifest.json": b"{}"})

    warned = _warned(anki, sync.update_decks)

    assert "manifest.json in someone/decks is empty" in warned
    assert "No deck source configured yet" not in warned


# ------------------------------------------------------------- backups on disk
def test_a_deck_backup_is_written_in_a_format_this_addon_can_read(anki):
    """The backups were written in the modern package format, which holds its
    collection zstd-compressed. zstandard is not stdlib and Anki doesn't ship it, so
    every reader here refuses one: the add-on's own backups could not be read by the
    add-on, and nothing said so."""
    from internpearls import collection
    from internpearls.logic import apkg_deck_names
    _her_card(anki, "g1", "Front one", deck=DECK)

    path = collection._backup_deck(DECK)

    assert path and apkg_deck_names(path)   # readable with no zstd anywhere in sight
    _out, opts, _limit = anki.col.exports[-1]
    # Every option is asked for by name, one guarded setattr each (the same shape
    # _import_apkg uses), so a build that has dropped one of these protobuf fields
    # loses that option rather than the whole backup.
    assert opts.legacy is True
    assert (opts.with_scheduling, opts.with_deck_configs, opts.with_media) == (
        True, True, True)


def test_a_backup_label_is_reduced_to_something_a_filename_can_hold(anki):
    """The label is a deck's own name, which is free text: a "/" in it used to be a
    path separator in the filename, so the export was attempted somewhere that does not
    exist and the backup silently failed."""
    from internpearls import collection
    odd = "Other Root::Sub/Deck: odd"
    _her_card(anki, "g1", "Front one", deck=odd)

    path = collection._backup_deck(odd, "Sub/Deck: odd")

    assert path and os.path.exists(path)
    assert os.path.dirname(path) == collection._deck_backup_folder()
    assert "/" not in os.path.basename(path)[len("Intern Pearls "):]


def test_pruning_keeps_ten_backups_of_each_root_rather_than_ten_in_all(anki):
    """A run over several roots writes a file per root. A folder-wide prune then evicted
    another root's history, and on a big enough run the files the same call had just
    written, so the newest backup of a deck could be gone the moment it was needed."""
    from internpearls import collection
    folder = collection._deck_backup_folder()
    for i in range(12):
        for label in ("Other Root", "Third Root"):
            open(os.path.join(folder, f"Intern Pearls 2020-01-01-0000{i:02d} "
                                      f"{collection._backup_label(label)}.apkg"),
                 "wb").close()
    _her_card(anki, "g1", "Front one", deck=DECK)

    collection._backup_deck("Intern Pearls", "Intern Pearls")

    made = [collection._label_of_backup(f) for f in os.listdir(folder)]
    assert made.count(collection._backup_label("Other Root")) == 12
    assert made.count(collection._backup_label("Third Root")) == 12
    assert made.count(collection._backup_label("Intern Pearls")) == 1


def test_a_partial_backup_failure_names_what_was_and_was_not_covered(anki, tmp_path,
                                                                     monkeypatch):
    """A run over two roots that backed up one of them has something to restore from and
    something it does not. Answering that with a flat "couldn't create an automatic
    backup" hid both halves: the cover that exists, and which deck is without it."""
    from internpearls import collection, sync
    outside = "Other Root::Extra"
    anki.col.add_note("g1", _fields("Front one"), [TAGS], deck=DECK)
    anki.col.add_note("g2", _fields("Front two"), [TAGS], deck=outside)
    _configure(anki, _write_source(tmp_path, {
        DECK: ("v2", [("g1", _fields("Front one", back="new"), TAGS)], None),
        outside: ("v2", [("g2", _fields("Front two", back="new"), TAGS)], None)}))
    real_export = collection._export_deck_to

    def export(path, deck_name):
        if deck_name == "Other Root":
            raise RuntimeError("no room on disk")
        return real_export(path, deck_name)

    monkeypatch.setattr(collection, "_export_deck_to", export)
    asked = []

    def respond(p):
        if p["kind"] == "ask":
            asked.append(p["text"])
            return {"answer": False}
        if p["kind"] != "dialog":
            return {}
        done = _dismiss_result(p["tree"])
        return done or {"events": [
            {"id": _find(p["tree"], t="button", label="Update")["id"], "click": True}]}

    drive(anki, sync.sync_decks, respond)
    anki.gui.interactive = False

    assert any("Couldn't back up: Other Root" in t for t in asked), asked
    assert any("Backed up: Intern Pearls" in t for t in asked), asked


# ---------------------------------------------------- collisions, told accurately
def _collide(anki, tmp_path, second_decks):
    """She has a Dosing note of her own over a value the source shipped; `second_decks`
    is what the source ships next. Returns the trees of the second run."""
    anki.col.add_note("g1", _fields("Front one", dosing="1 mg/kg"), [TAGS], deck=DECK)
    _configure(anki, _write_source(tmp_path, {
        DECK: ("v1", [("g1", _fields("Front one", dosing="1 mg/kg"), TAGS)], None)}))
    anki.mw._config["protected_fields"] = ["Notes", "Dosing"]
    _sync(anki)                                   # records what the source shipped
    anki.col.note_by_guid("g1")["Dosing"] = "2 mg/kg (my attending says so)"
    _configure(anki, _write_source(tmp_path, second_decks))
    anki.mw._config["protected_fields"] = ["Notes", "Dosing"]
    return second_decks


def test_a_cancelled_update_still_reports_the_collisions_it_found(anki, tmp_path):
    """The decks that applied before the cancel can have collided with her own edits
    exactly as a finished run's can, and those cards are the one thing on that screen
    she may want to act on. The stopped-early summary dropped them."""
    import aqt.qt as aqt_qt
    _collide(anki, tmp_path, {
        DECK: ("v2", [("g1", _fields("Front one", dosing="3 mg/kg (corrected)"), TAGS)],
               None),
        NEW_DECK: ("v1", [("g2", _fields("Front two"), TAGS)], None)})
    aqt_qt.QProgressDialog.cancel_after = {"Updating decks": 1}

    trees = _update(anki)

    summary = _summary_text(trees)
    assert "stopped early" in summary.lower()
    assert "changed a field you had also written in yourself" in summary
    assert anki.col.note_by_guid("g1")["Dosing"] == "2 mg/kg (my attending says so)"


def test_an_update_that_agrees_with_her_own_wording_is_not_a_collision(anki, tmp_path):
    """Hers and the source's landed on the same text. There is nothing to reconcile and
    nobody to send it to, so reporting it asked her to go and compare two identical
    wordings."""
    _collide(anki, tmp_path, {
        DECK: ("v2", [("g1", _fields("Front one",
                                     dosing="2 mg/kg (my attending says so)"), TAGS)],
               None)})

    trees = _sync(anki)

    assert "changed a field you had also written in yourself" not in _summary_text(trees)
    assert anki.col.note_by_guid("g1")["Dosing"] == "2 mg/kg (my attending says so)"


# ------------------------------------------------- what a held-back deck is called
def _cloze_conversion_source(anki, tmp_path):
    """Her Q-and-A note, and a source that ships it as a fill-in-the-blank: a note-type
    conversion, with the templates themselves unchanged."""
    anki.col.models._models.append(_cloze_model())
    _her_card(anki, "g1", "Old Q and A front")
    _configure(anki, _write_source(tmp_path, {
        DECK: ("v2", [("g1", ["A {{c1::cloze}} version", "why", "", "", ""], TAGS)],
               _cloze_model())}))


def test_a_deck_held_back_for_a_conversion_says_that_is_what_it_is(anki, tmp_path):
    """Every held-back deck used to read "includes a card-template update", which sends
    the reader looking for a look change the deck doesn't carry."""
    from internpearls import sync
    from internpearls.config import _cfg
    _cloze_conversion_source(anki, tmp_path)
    manifest, fetch, _source = sync._fetch_manifest(_cfg())

    results, _restored, _tpl, deferred, _c, _col, _conv = sync._run_sync(
        _cfg(), manifest, fetch, manifest["decks"], defer_template_changes=True)

    assert deferred == [DECK]
    assert "includes a note-type format update" in results[0]


def test_auto_syncs_tooltip_names_a_held_back_conversion_too(anki, tmp_path):
    """Same wording problem on the only thing an unattended run ever says out loud."""
    from internpearls import background
    _cloze_conversion_source(anki, tmp_path)
    anki.mw._config["auto_sync_decks"] = True

    background._auto_sync_check()

    assert any("card-template or note-type format update" in t
               for t in anki.gui.tooltips), anki.gui.tooltips
    assert anki.col.note_by_guid("g1").note_type()["name"] == "Study Deck - Basic"


# ------------------------------------------------- consent dialogs and their buttons
def test_the_look_change_question_names_the_action_on_its_buttons(anki, tmp_path):
    """Yes/No on a question whose two answers cost different things makes the reader
    scroll back up to the question to work out what No means."""
    _configure(anki, _write_source(tmp_path, {
        DECK: ("v2", [("g1", _fields("Front one"), TAGS)], make_model(css=NEW_CSS))}))

    _sync(anki)

    assert ("Apply the new look", "Keep my current look") in anki.gui.ask_buttons


def test_the_conversion_question_names_the_action_on_its_buttons(anki, tmp_path):
    """The other asymmetric one: moving her cards across, or importing them as new
    beside the ones she has."""
    _cloze_conversion_source(anki, tmp_path)

    _update(anki)

    assert ("Move my cards across", "Import them as new") in anki.gui.ask_buttons


def test_the_look_change_checkbox_sits_above_the_list_it_belongs_with(anki, tmp_path):
    """It used to be added under the body, which on a long update is hundreds of
    streamed rows below the sentence explaining what ticking it costs."""
    _her_card(anki, "g1", "Front one")
    _configure(anki, _write_source(tmp_path, {
        DECK: ("v2", [("g1", _fields("Front one", back="new"), TAGS)],
               make_model(css=NEW_CSS))}))

    trees = _update(anki, accept=False)

    order = [n["t"] for n in _walk(trees[0]) if n["t"] in ("check", "scroll")]
    assert order and order[0] == "check", order


def test_a_sync_writes_its_personalized_copy_outside_the_source_folder(anki, tmp_path,
                                                                       monkeypatch):
    """The personalized copy used to be written into the directory the deck was read
    from. For a local-folder source that is the learner's own configured folder, which
    may be a read-only share, and is hers rather than ours to leave a file in."""
    from internpearls import collection
    folder = _write_source(tmp_path, {
        DECK: ("v1", [("g1", _fields("Front one"), TAGS)], None)})
    _configure(anki, folder)
    written = []
    real_write = collection.write_personalized

    def watch(src, remap, out, drop=frozenset()):
        written.append(out)
        return real_write(src, remap, out, drop=drop)

    monkeypatch.setattr(collection, "write_personalized", watch)

    _sync(anki)

    assert written, "the sync should have written a personalized copy"
    assert not any(os.path.dirname(o) == folder for o in written), written
    assert not [f for f in os.listdir(folder) if f.endswith(".sync.apkg")]


# ------------------------------------------- consent a failed preview used to swallow
def test_a_conversion_in_a_deck_the_preview_could_not_fetch_is_still_asked_about(
        anki, tmp_path, monkeypatch):
    """Update my decks reads the run's schema changes out of the preview's own
    downloads. A deck whose preview download failed contributed nothing to that, so its
    note-type conversion was never disclosed and never asked about, while the apply
    step's retry imported it anyway with convert=False: her cards stayed on the old
    type, no question was ever put on screen, and the deck was recorded as installed, so
    nothing offered it again until the source bumped that deck's version."""
    from internpearls import sync
    _cloze_conversion_source(anki, tmp_path)
    _fetch_that_fails_during_preview(sync, monkeypatch)
    asked = []

    def ask(text):
        asked.append(text)
        return True     # she says yes to the conversion, if ever asked

    _update(anki, ask=ask)

    assert any("changed format" in t for t in asked), asked
    assert anki.col.note_by_guid("g1").note_type()["name"] == "Study Deck - Cloze"


def test_a_look_change_a_failed_preview_hid_is_offered_rather_than_dropped(
        anki, tmp_path, monkeypatch):
    """Same gap, other schema change. The look checkbox is built from the preview too,
    so a deck the preview couldn't fetch had no checkbox naming it. Applying it on a
    tick that never mentioned it would be consent by accident and dropping it would
    mean never offering it again, so it is asked outright, once, after the import."""
    from internpearls import sync
    _her_card(anki, "g1", "Front one")
    _configure(anki, _write_source(tmp_path, {
        DECK: ("v2", [("g1", _fields("Front one"), TAGS)], make_model(css=NEW_CSS))}))
    _fetch_that_fails_during_preview(sync, monkeypatch)

    trees = _update(anki, ask=lambda _text: True)

    assert "how some cards look" not in "\n".join(_label_texts(trees[0]))
    assert any("changes how some cards look" in a for a in anki.gui.asks), anki.gui.asks
    assert anki.col.models.all()[0]["css"] == NEW_CSS


def test_a_look_change_the_confirmation_did_name_is_never_asked_about_twice(
        anki, tmp_path):
    """The checkbox still owns every look change the confirmation disclosed: the
    after-the-run question is only for one it could not."""
    _update_with_look_change(anki, tmp_path, tick=False)

    assert not any("changes how some cards look" in a for a in anki.gui.asks)
    assert anki.col.imports


def test_a_cancelled_update_still_applies_the_look_the_finished_decks_carry(
        anki, tmp_path):
    """Cancelling partway returned before the look change was applied, so a ticked
    checkbox was thrown away for the decks that did import. Those decks are recorded as
    installed, so the next run has no update left to carry that consent on."""
    import aqt.qt as aqt_qt
    from internpearls import sync
    _her_card(anki, "g1", "Front one")
    _configure(anki, _write_source(tmp_path, {
        DECK: ("v2", [("g1", _fields("Front one"), TAGS)], make_model(css=NEW_CSS)),
        NEW_DECK: ("v1", [("g2", _fields("Front two"), TAGS)], None)}))
    anki.gui.interactive = True
    aqt_qt.QProgressDialog.cancel_after = {"Updating decks": 1}

    def respond(p):
        if p["kind"] != "dialog":
            return {}
        done = _dismiss_result(p["tree"])
        if done:
            return done
        events = []
        box = next((n for n in _walk(p["tree"]) if n.get("t") == "check"), None)
        if box:
            events.append({"id": box["id"], "value": True})
        events.append({"id": _find(p["tree"], t="button", label="Update")["id"],
                       "click": True})
        return {"events": events}

    drive(anki, sync.update_decks, respond)

    assert anki.col.models.all()[0]["css"] == NEW_CSS
    assert json.load(open(sync.INSTALLED, encoding="utf8")) == {DECK: "v2"}


# ------------------------------------------------------ backup labels that collide
def test_two_roots_that_sanitize_alike_get_a_backup_each(anki):
    """A deck name is free text, so two different roots can sanitize to one label
    ("A/B" and "A:B" both become "A_B"). Sharing a label means sharing a filename: the
    same-second backups a multi-root run takes overwrote each other while the run
    reported both covered, and the two then pruned as one bucket."""
    from internpearls import collection
    from internpearls.logic import apkg_deck_names
    _her_card(anki, "g1", "Front one", deck="A/B::Sub")
    _her_card(anki, "g2", "Front two", deck="A:B::Sub")

    first = collection._backup_deck("A/B", "A/B")
    second = collection._backup_deck("A:B", "A:B")

    assert first and second and first != second
    assert os.path.exists(first) and os.path.exists(second)
    assert apkg_deck_names(first) and apkg_deck_names(second)   # both restorable
    assert (collection._label_of_backup(os.path.basename(first))
            != collection._label_of_backup(os.path.basename(second)))


def test_two_roots_that_sanitize_alike_prune_as_two_buckets(anki):
    """Following from the labels being distinct: one root's older backups can no longer
    be evicted by the other root's newer ones."""
    from internpearls import collection
    folder = collection._deck_backup_folder()
    _her_card(anki, "g1", "Front one", deck="A/B::Sub")
    _her_card(anki, "g2", "Front two", deck="A:B::Sub")
    for i in range(12):
        open(os.path.join(folder, f"Intern Pearls 2020-01-01-0000{i:02d} "
                                  f"{collection._backup_label('A/B')}.apkg"),
             "wb").close()

    collection._backup_deck("A:B", "A:B")

    labels = [collection._label_of_backup(f) for f in os.listdir(folder)]
    assert labels.count(collection._backup_label("A/B")) == 12
    assert labels.count(collection._backup_label("A:B")) == 1


def test_pruning_bounds_backups_written_before_labels_carried_a_hash(anki):
    """A file from an earlier version carries the bare sanitized deck name, which
    matches no current label, so nothing would ever prune it and the folder grew
    without bound. It prunes with the label that starts with it."""
    from internpearls import collection
    from internpearls.config import DECK_BACKUPS_KEEP
    folder = collection._deck_backup_folder()
    for i in range(12):
        open(os.path.join(folder,
                          f"Intern Pearls 2020-01-01-0000{i:02d} Intern Pearls.apkg"),
             "wb").close()
    _her_card(anki, "g1", "Front one", deck=DECK)

    path = collection._backup_deck("Intern Pearls", "Intern Pearls")

    made = os.listdir(folder)
    assert len(made) == DECK_BACKUPS_KEEP, made
    assert os.path.basename(path) in made   # and the newest one is the survivor


# ------------------------------------------------- backups scoped to what is written
def _record_exports(collection, monkeypatch):
    """Record every deck a run backs up, without writing any of them."""
    exported = []
    monkeypatch.setattr(collection, "_export_deck_to",
                        lambda path, deck_name: exported.append(deck_name) or 1)
    return exported


def test_remove_empty_cards_backs_up_where_the_cards_actually_are(anki, monkeypatch):
    """Its search is Anki's own empty-cards report narrowed by tag, not by deck, so it
    deletes an empty card the learner has filed anywhere. Backing up export_deck alone
    covered none of that while the confirmation promised a backup."""
    from internpearls import collection
    _her_cloze(anki, "regrouped", "the {{c1::first}} and {{c2::second}}",
               deck="Other Root::Extra", ords=[0, 1, 2])
    exported = _record_exports(collection, monkeypatch)

    drive(anki, collection.remove_empty_cards, _click_duplicate_button(accept=True))
    anki.gui.interactive = False

    assert set(exported) == {"Other Root"}, exported


def test_clean_up_duplicates_backs_up_where_the_group_actually_is(anki, tmp_path,
                                                                  monkeypatch):
    """Same class: duplicates are found by tag across the collection, and both halves of
    a group are written (the kept copy receives the personal notes, the loser is
    archived)."""
    from internpearls import collection, sync
    outside = "Other Root::Extra"
    anki.col.add_note("dup1", _fields("A shared front"), TAGS.split(), deck=outside)
    anki.col.add_note("dup2", _fields("A shared front"), TAGS.split(), deck=outside)
    _configure(anki, _write_retired_source(tmp_path, {}))
    exported = _record_exports(collection, monkeypatch)

    drive(anki, sync.clean_up_duplicates, _click_duplicate_button(accept=True))
    anki.gui.interactive = False

    assert "Other Root" in exported, exported


def test_update_backs_up_the_deck_a_refiled_card_actually_sits_in(anki, tmp_path,
                                                                  monkeypatch):
    """An import matches a note by GUID wherever it is, so a card she refiled herself is
    rewritten in a deck the manifest's own names cover nothing of, while every
    confirmation promises a backup covering what the run touches."""
    from internpearls import collection
    _her_card(anki, "g1", "Front one", deck="Other Root::Extra")
    _configure(anki, _write_source(tmp_path, {
        DECK: ("v2", [("g1", _fields("Front one", back="new"), TAGS)], None)}))
    exported = _record_exports(collection, monkeypatch)

    _update(anki)

    assert "Other Root" in exported, exported


def test_auto_sync_backs_up_the_deck_a_refiled_card_actually_sits_in(anki, tmp_path,
                                                                     monkeypatch):
    """The unattended path has its own downloads on disk by the time it backs up, so it
    scopes from the same reading of them the interactive one does."""
    from internpearls import background, collection
    _her_card(anki, "g1", "Front one", deck="Other Root::Extra")
    folder = _write_source(tmp_path, {
        DECK: ("v2", [("g1", _fields("Front one", back="new"), TAGS)], None)})
    anki.mw._config = {"decks_dir": folder, "auto_sync_decks": True}
    exported = _record_exports(collection, monkeypatch)

    background._auto_sync_check()

    assert "Other Root" in exported, exported


# --------------------------------------------------- what an unattended poll repeats
def test_auto_sync_stops_re_downloading_a_deck_it_has_already_held_back(
        anki, tmp_path, monkeypatch):
    """A deferred deck stays pending forever, so every later poll downloaded its whole
    .apkg and took a fresh deck backup to reach the same decision, for as long as Anki
    stayed open."""
    from internpearls import background, collection
    _cloze_conversion_source(anki, tmp_path)
    anki.mw._config["auto_sync_decks"] = True
    fetched = []
    real_fetch = background._cached_fetch
    monkeypatch.setattr(background, "_cached_fetch",
                        lambda fetch, d, **kw: fetched.append(d["name"])
                        or real_fetch(fetch, d, **kw))
    exported = _record_exports(collection, monkeypatch)

    background._auto_sync_check()
    background._auto_sync_check()
    background._auto_sync_check()

    assert fetched == [DECK], fetched
    assert exported == ["Intern Pearls::Intern Custom"], exported


def test_auto_sync_picks_a_held_back_deck_back_up_when_its_version_changes(
        anki, tmp_path, monkeypatch):
    """The skip is keyed by version as well as by name, so a source that pushes a fix
    mid-session is not skipped along with the version that was deferred."""
    from internpearls import background
    _cloze_conversion_source(anki, tmp_path)
    anki.mw._config["auto_sync_decks"] = True
    background._auto_sync_check()

    _configure(anki, _write_source(tmp_path, {
        DECK: ("v3", [("g1", _fields("Front one", back="plain again"), TAGS)], None)}))
    anki.mw._config["auto_sync_decks"] = True
    background._auto_sync_check()

    assert anki.col.note_by_guid("g1")["Back"] == "plain again"


def test_auto_sync_downloads_through_the_session_cache(anki, tmp_path):
    """The poll was the one fetch path that ignored the session cache, so a deck an
    interactive preview had already downloaded was fetched again on the next tick."""
    from internpearls import background, sync
    folder = _write_source(tmp_path, {
        DECK: ("v1", [("g1", _fields("Front one"), TAGS)], None)})
    anki.mw._config = {"decks_dir": folder, "auto_sync_decks": True}

    background._auto_sync_check()

    assert sync._apkg_cache.get(DECK, (None,))[0] == "v1"


def test_a_backup_that_keeps_failing_nags_once_per_session(anki, tmp_path, monkeypatch):
    """Unlike the template-deferral notice, this one re-announced itself every poll
    interval for as long as the backup kept failing."""
    from internpearls import background, collection
    _her_card(anki, "g1", "Front one")
    folder = _write_source(tmp_path, {
        DECK: ("v2", [("g1", _fields("Front one", back="new"), TAGS)], None)})
    anki.mw._config = {"decks_dir": folder, "auto_sync_decks": True}
    monkeypatch.setattr(collection, "_backup_deck", lambda *a, **kw: None)

    background._auto_sync_check()
    background._auto_sync_check()

    assert len([t for t in anki.gui.tooltips if "couldn't create a backup" in t]) == 1
    assert not anki.col.imports   # and it still refuses to import unprotected


# ------------------------------------------------- card fronts that reach a rich row
IMG_FRONT = '<img src="pic.png">'
CLOZE_FRONT = "the {{c1::first}} step"


def test_a_relocated_cards_row_names_the_card_rather_than_its_markup(anki, tmp_path):
    """A row built from a raw first field paints an image note as a broken picture and
    reads a cloze as its own braces, since these rows are rendered as rich text. Every
    other row that names a card goes through note_display_label; these two did not."""
    from internpearls import sync
    from internpearls.logic import note_display_label
    picture = anki.col.add_note("moved_img", _fields(IMG_FRONT), TAGS.split(), deck=DECK)
    _her_cloze(anki, "moved_cloze", CLOZE_FRONT)
    _configure(anki, _write_retired_source(tmp_path, {}, deck_moves={
        "moved_img": {"from": DECK, "to": NEW_DECK},
        "moved_cloze": {"from": DECK, "to": NEW_DECK}}))

    texts = _label_texts(_reconcile_tree(anki, accept=False))

    assert not any("<img" in t for t in texts), [t for t in texts if "<img" in t]
    assert note_display_label(picture.fields) in texts
    assert note_display_label([CLOZE_FRONT]) in texts


def test_a_relocated_cards_row_on_the_update_screen_names_it_the_same_way(anki,
                                                                          tmp_path):
    """The same card, the same row vocabulary, on the screen most people actually
    use: _retired_moved_items built its own row from the raw field too."""
    from internpearls import sync
    anki.col.add_note("moved_img", _fields(IMG_FRONT), TAGS.split(), deck=DECK)
    _configure(anki, _write_source(
        tmp_path, {DECK: ("v2", [("g1", _fields("Front one"), TAGS)], None)},
        deck_moves={"moved_img": {"from": DECK, "to": NEW_DECK}}))

    texts = _label_texts(_update(anki, accept=False)[0])

    assert not any("<img" in t for t in texts), [t for t in texts if "<img" in t]
    assert any("the back" in t for t in texts)   # the picture note's own prompt field


def test_a_reworded_pairs_row_names_both_halves_rather_than_their_markup(anki,
                                                                         tmp_path):
    """Both halves of a stranded pair are raw note fields (the ledger is keyed by front
    text), and both land in one rich-text row."""
    from internpearls import sync
    anki.col.add_note("g_old", _fields(IMG_FRONT), TAGS.split(), deck=DECK)
    anki.col.add_note("g_new", _fields(CLOZE_FRONT), TAGS.split(), deck=DECK)
    _configure(anki, _stranded_source(tmp_path, {IMG_FRONT: CLOZE_FRONT}))

    texts = _label_texts(_reconcile_tree(anki, accept=False))

    assert not any("<img" in t for t in texts), [t for t in texts if "<img" in t]
    assert any(CLOZE_FRONT in t for t in texts)


def test_a_collision_row_names_the_card_rather_than_slicing_its_markup(anki, tmp_path):
    """The collision list sliced the raw first field to 70 characters, which can cut a
    tag in half and take the rest of the row's markup with it."""
    from internpearls import sync
    anki.col.add_note("g1", _fields(IMG_FRONT, dosing="1 mg/kg"), [TAGS], deck=DECK)
    _configure(anki, _write_source(tmp_path, {
        DECK: ("v1", [("g1", _fields(IMG_FRONT, dosing="1 mg/kg"), TAGS)], None)}))
    anki.mw._config["protected_fields"] = ["Notes", "Dosing"]
    _sync(anki)
    anki.col.note_by_guid("g1")["Dosing"] = "1 mg/kg (my attending says 1.5)"
    _configure(anki, _write_source(tmp_path, {
        DECK: ("v2", [("g1", _fields(IMG_FRONT, dosing="2 mg/kg (corrected)"), TAGS)],
               None)}))
    anki.mw._config["protected_fields"] = ["Notes", "Dosing"]

    texts = _label_texts(_sync(anki)[-1])

    assert "changed a field you had also written in yourself" in "\n".join(texts)
    assert not any("<img" in t for t in texts), [t for t in texts if "<img" in t]
    assert any(t.startswith("the back (Dosing)") for t in texts), texts


# ---------------------------------------------------- questions that can be answered
def test_the_backup_skip_question_names_its_buttons_and_defaults_to_cancelling(anki):
    """A bare Yes/No on "proceed without a backup" leaves Yes as the default, which is
    the reader agreeing to the risky half by pressing Return."""
    from internpearls import collection
    _her_card(anki, "g1", "Front one")   # something in the collection to lose

    proceed, backed_up = collection._pre_sync_backup_or_confirm_skip(
        "A Deck That Isn't Here", None, SCOPE)

    assert (proceed, backed_up) == (True, False)   # answers default to yes in tests
    assert anki.gui.ask_buttons[-1] == ("Continue without a backup", "Cancel")
    assert anki.gui.ask_defaults[-1] == "Cancel"


def test_dismissing_the_backup_skip_question_means_cancel(anki):
    """Escape and the close box used to do nothing at all (every button carried
    AcceptRole), so the only way out of one of these was to answer it."""
    from internpearls import collection
    _her_card(anki, "g1", "Front one")
    anki.gui.escape_asks = True

    proceed, backed_up = collection._pre_sync_backup_or_confirm_skip(
        "A Deck That Isn't Here", None, SCOPE)

    assert (proceed, backed_up) == (False, False)


def test_every_consequential_question_defaults_to_its_declining_button(anki, tmp_path):
    """Import single deck raises three of them in one run: continue without the
    reworded-front list, import now, and proceed without a backup."""
    from internpearls import sync
    _her_card(anki, "g1", "Front one")
    anki.mw._config = {"decks_dir": str(tmp_path / "gone")}   # no manifest to fetch
    src = str(tmp_path / "hand.apkg")
    make_apkg(src, [("g1", _fields("Front one", back="new"), TAGS)], deck=DECK)
    anki.gui.file_picks = [src]

    sync.import_single()

    assert [b for _y, b in anki.gui.ask_buttons] == anki.gui.ask_defaults
    assert ("Import", "Cancel") in anki.gui.ask_buttons
    assert ("Import without it", "Cancel") in anki.gui.ask_buttons


def test_dismissing_the_import_question_imports_nothing(anki, tmp_path):
    from internpearls import sync
    _her_card(anki, "g1", "Front one")
    _configure(anki, _write_retired_source(tmp_path, {}))
    src = str(tmp_path / "hand.apkg")
    make_apkg(src, [("g1", _fields("Front one", back="new"), TAGS)], deck=DECK)
    anki.gui.file_picks = [src]
    anki.gui.escape_asks = True

    sync.import_single()

    assert not anki.col.imports
    assert anki.col.note_by_guid("g1")["Back"] == "the back"


# ----------------------------------------------------- what the sweep found (round 3)
def test_a_scratch_download_is_keyed_by_version_as_well_as_path(anki):
    """The scratch path was keyed by the manifest path alone, so a background poll
    fetching a newly pushed version landed on top of the very file an open confirmation
    had read and was about to import, swapping the content out from under a decision
    already made."""
    from internpearls import sync

    assert sync._scratch_path("decks/A.apkg", "v1") != sync._scratch_path(
        "decks/A.apkg", "v2")
    assert sync._scratch_path("decks/A.apkg", "v1") == sync._scratch_path(
        "decks/A.apkg", "v1")


def test_a_download_swept_from_the_tempdir_counts_as_not_fetched(anki, tmp_path):
    """_cached_fetch re-fetches when its file has vanished; the apply step's own
    "already fetched?" check had no such guard, so a long session whose tempdir was
    swept imported a path that was no longer there."""
    from internpearls import sync
    missing = str(tmp_path / "gone.apkg")
    present = str(tmp_path / "here.apkg")
    open(present, "wb").close()

    assert sync._is_local(present) is True
    assert sync._is_local(missing) is False
    assert sync._is_local(None) is False
    assert sync._is_local(RuntimeError("the source hiccuped")) is False


def test_a_manifest_that_is_valid_json_but_not_an_object_reads_as_broken(anki,
                                                                        tmp_path):
    """A JSON array or a bare string parses fine and then raises AttributeError in the
    first reader that calls .get on it ("'list' object has no attribute 'get'"), rather
    than as the message this file's other failure modes already give."""
    from internpearls import sync
    folder = tmp_path / "source"
    folder.mkdir()
    (folder / "manifest.json").write_text('["a deck"]', encoding="utf8")
    _configure(anki, str(folder))

    sync.sync_decks()

    assert anki.gui.warnings[0].startswith(
        f"The deck source couldn't be used: the manifest.json in {folder} isn't valid: "
        "it holds a list where a set of decks was expected.")


def test_a_broken_manifest_from_github_reads_as_broken_too(anki, monkeypatch):
    """Same guard on the other source branch, in the same words."""
    from internpearls import sync
    _github_source(anki, monkeypatch, {"manifest.json": b'"just a string"'})

    sync.sync_decks()

    assert anki.gui.warnings[0].startswith(
        "The deck source couldn't be used: the manifest.json in someone/decks isn't "
        "valid: it holds a str where a set of decks was expected.")


def test_an_unreachable_source_still_reads_as_unreachable(anki, monkeypatch):
    """The other half of the same fix: a source that genuinely could not be reached
    keeps the sentence it always had, so the two diagnoses stay distinguishable.

    Driven through the real net layer rather than a stub raising a bare OSError. That
    shape is one net.py never produces (every failure there is a RuntimeError), so the
    old test green-lit a split production had already lost: an offline learner read
    "The deck source couldn't be used: the network isn't responding" and was told to go
    check her GitHub token."""
    from internpearls import sync
    anki.mw._config = {"github_decks_repo": "someone/decks"}
    _offline_network(monkeypatch)

    sync.sync_decks()

    assert anki.gui.warnings[0].startswith(
        "Couldn't reach the deck source: couldn't reach the network "
        "([Errno 8] nodename nor servname provided)")
    assert "internet connection" in anki.gui.warnings[0]
    assert "GitHub token" not in anki.gui.warnings[0]


def test_a_timed_out_source_reads_as_unreachable_too(anki, monkeypatch):
    """The other shape net.py raises for a host that never answered."""
    from internpearls import sync
    anki.mw._config = {"github_decks_repo": "someone/decks"}
    _urlopen_raising(monkeypatch, TimeoutError("timed out"))

    sync.sync_decks()

    assert anki.gui.warnings[0].startswith(
        "Couldn't reach the deck source: the network isn't responding (timed out).")


def test_a_source_that_answers_with_an_http_error_is_not_called_unreachable(
        anki, monkeypatch):
    """A status code means the host answered, and what it answered is about the repo,
    the branch or the token: exactly the case the "use Change source" advice fits."""
    import urllib.error
    from internpearls import sync
    anki.mw._config = {"github_decks_repo": "someone/decks"}
    _urlopen_raising(monkeypatch, urllib.error.HTTPError(
        "https://api.github.com/", 404, "Not Found", None, None))

    sync.sync_decks()

    assert anki.gui.warnings[0].startswith(
        "The deck source couldn't be used: not found (check the repo name, branch, "
        "and file path).")
    assert "GitHub token" in anki.gui.warnings[0]


def _urlopen_raising(monkeypatch, exc):
    """Make the real net layer's one network call fail with `exc`, so the add-on sees
    whatever net._http_get turns that into rather than a shape hand-written here."""
    import urllib.request

    def boom(*_a, **_kw):
        raise exc

    monkeypatch.setattr(urllib.request, "urlopen", boom)


def _offline_network(monkeypatch):
    import urllib.error
    _urlopen_raising(monkeypatch, urllib.error.URLError(
        "[Errno 8] nodename nor servname provided"))


def test_a_card_in_both_ledgers_is_archived_rather_than_relocated(anki, tmp_path):
    """Archiving moves a card into the Retired deck; a relocation right after pulled it
    straight back out into a live deck, suspended and tagged, which is neither of the
    two outcomes the ledgers asked for."""
    from internpearls import sync
    _her_card(anki, "old1", "bulky crisis card")
    _configure(anki, _write_retired_source(
        tmp_path,
        {DECK: {"old1": {"identity": "bulky crisis card", "reason": "split",
                         "superseded_by": []}}},
        deck_moves={"old1": {"from": DECK, "to": NEW_DECK}}))

    trees = _reconcile_tree(anki, accept=True)

    card = anki.col.get_card(anki.col.note_by_guid("old1").card_ids()[0])
    assert card.did == anki.col.decks.id_for_name(RETIRED_DECK)
    assert card.queue == -1
    # And it was never offered as a relocation either, since it is not going to be one.
    assert not any("belong to a deck that's since been reorganized" in t
                   for t in _label_texts(trees))


def test_reconcile_leaves_an_opted_out_decks_cards_where_they_are(anki, tmp_path):
    """Manage decks says unchecking a deck stops future syncs for it. Archiving and
    relocating her cards is a sync doing something to them, and neither read the
    exclusion list at all."""
    from internpearls import sync
    _her_card(anki, "old1", "bulky crisis card")
    _her_card(anki, "moved1", "a card leaving this deck")
    _configure(anki, _write_retired_source(
        tmp_path,
        {DECK: {"old1": {"identity": "bulky crisis card", "reason": "split",
                         "superseded_by": []}}},
        deck_moves={"moved1": {"from": DECK, "to": NEW_DECK}}))
    anki.mw._config["excluded_decks"] = [DECK]

    sync.reconcile_decks()

    assert any("nothing to tidy up" in i for i in anki.gui.infos), anki.gui.infos
    assert RETIRED_TAG not in anki.col.note_by_guid("old1").tags
    moved = anki.col.get_card(anki.col.note_by_guid("moved1").card_ids()[0])
    assert moved.did == anki.col.decks.id_for_name(DECK)


def test_one_recovered_flag_reads_as_one_card(anki, tmp_path):
    """"1 card flagged. 1 of them carried over" counts something there is only one
    of."""
    from internpearls import review
    review.save_feedback({"gONE": {"note": "wrong dose", "deck": DECK,
                                   "front": "An earlier card"}})
    seen = {}

    def on_screen(tree, _seen, decide):
        seen.setdefault("texts", _label_texts(tree))
        return {"events": [{"id": _find(tree, t="button", label=decide)["id"],
                            "click": True}]}

    _feedback_run(anki, tmp_path, on_screen)

    line = next(t for t in seen["texts"] if "flagged" in t)
    assert "<b>1 card flagged.</b> It carried over from an earlier session." in line


def test_cancelling_the_retry_download_stops_the_run_cleanly(anki, tmp_path,
                                                             monkeypatch):
    """The retry runs after the confirmation but before the backup and the first
    import, so Cancel there stops outright rather than leaving a button that does
    nothing on a dialog nobody asked for."""
    import aqt.qt as aqt_qt
    from internpearls import sync
    _her_card(anki, "g1", "Front one")
    _configure(anki, _write_source(tmp_path, {
        DECK: ("v2", [("g1", _fields("Front one", back="new"), TAGS)], None)}))
    _fetch_that_fails_during_preview(sync, monkeypatch)
    aqt_qt.QProgressDialog.cancel_after = {"Downloading decks": 0}

    _update(anki)

    assert not anki.col.imports
    assert not os.path.exists(sync.INSTALLED)
    assert anki.col.note_by_guid("g1")["Back"] == "the back"


def test_fix_note_types_holds_the_manual_guard_too(anki, monkeypatch):
    """It adds a field, which bumps the collection schema, so an auto-sync tick landing
    on top of it is the same interleave the other collection-writing actions hold the
    guard against. It was the one menu action writing the collection without it."""
    from internpearls import collection, ui
    held = []
    monkeypatch.setattr(collection, "_ensure_notetypes",
                        lambda: held.append(ui.manual_sync_in_progress()) or [])

    collection.update_notetypes()

    assert held == [True]
    assert not ui.manual_sync_in_progress()


def test_declined_registry_round_trips(anki):
    from internpearls import config
    assert config.load_declined() == {}
    entry = {"g1": {"state": "skip", "front": "f", "deck": "IP::A",
                    "decided": "2026-08-25", "hash": "ab" * 8}}
    config.save_declined(entry)
    assert config.load_declined() == entry
    assert os.path.basename(config.DECLINED) == "declined.json"


# ------------------------------------------------------- declined-notes filtering
def _accept_everything(p):
    """respond() that clicks through Sync decks' confirmation and any askUser()
    prompt with yes, same as _click_sync_button's own default."""
    return _click_sync_button()(p)


def _run_unattended_poll(anki):
    """The same unattended driver the auto-sync tests use, turning auto-sync on for
    whatever source _configure already set rather than replacing it."""
    from internpearls import background
    anki.mw._config = {**anki.mw._config, "auto_sync_decks": True}
    background._auto_sync_check()


def _source_with_two_new_cards(anki, tmp_path):
    """A source with two cards neither yet in her collection, for tests that decline
    one of them. Returns the Anki deck name."""
    folder = _write_source(tmp_path, {
        DECK: ("v1", [("guid-new-a", _fields("front a"), TAGS),
                      ("guid-new-b", _fields("front b"), TAGS)], None)})
    _configure(anki, folder)
    return DECK


def _source_updating_card_a(anki, tmp_path):
    """She already holds guid-a under "front a"; the rebuilt deck reissues the same
    GUID with a reworded front. Returns the Anki deck name."""
    anki.col.add_note("guid-a", _fields("front a", notes="her mnemonic"), [TAGS])
    folder = _write_source(tmp_path, {
        DECK: ("v2", [("guid-a", _fields("front a, revised"), TAGS)], None)})
    _configure(anki, folder)
    return DECK


def _her_fields(anki, guid):
    return list(anki.col.note_by_guid(guid).fields)


def test_run_sync_drops_declined_notes_before_import(anki, tmp_path):
    from internpearls import config, sync
    deck = _source_with_two_new_cards(anki, tmp_path)
    config.save_declined({
        "guid-new-b": {"state": "never", "front": "front b", "deck": deck,
                       "decided": "2026-08-25", "hash": ""}})

    drive(anki, sync.sync_decks, respond=_accept_everything)

    fronts = {anki.col.get_note(nid).fields[0] for nid in anki.col.find_notes(f'"tag:{SCOPE}"')}
    assert "front a" in fronts
    assert "front b" not in fronts


def test_run_sync_keep_leaves_her_copy_untouched(anki, tmp_path):
    from internpearls import config, sync
    deck = _source_updating_card_a(anki, tmp_path)   # she has guid-a, source rewords it
    config.save_declined({
        "guid-a": {"state": "keep", "front": "front a", "deck": deck,
                   "decided": "2026-08-25", "hash": ""}})
    before = _her_fields(anki, "guid-a")

    drive(anki, sync.sync_decks, respond=_accept_everything)

    assert _her_fields(anki, "guid-a") == before


def test_unattended_auto_sync_respects_the_registry(anki, tmp_path):
    from internpearls import config
    deck = _source_with_two_new_cards(anki, tmp_path)
    config.save_declined({
        "guid-new-b": {"state": "skip", "front": "front b", "deck": deck,
                       "decided": "2026-08-25", "hash": ""}})

    _run_unattended_poll(anki)

    fronts = {anki.col.get_note(nid).fields[0] for nid in anki.col.find_notes(f'"tag:{SCOPE}"')}
    assert "front b" not in fronts


def test_run_sync_prunes_moot_registry_entries(anki, tmp_path):
    from internpearls import config, sync
    deck = _source_with_two_new_cards(anki, tmp_path)
    config.save_declined({
        "guid-gone": {"state": "skip", "front": "old", "deck": deck,
                      "decided": "2026-08-25", "hash": ""}})

    drive(anki, sync.sync_decks, respond=_accept_everything)

    assert "guid-gone" not in config.load_declined()


def test_run_sync_survives_a_garbage_registry_entry(anki, tmp_path):
    """A hand-edited declined.json can hold a non-dict value for a guid. prune_declined
    sits between the import and the protected-field restore, so a crash there used to
    abort the whole sync after her notes were overwritten but before they were restored.
    It must instead degrade gracefully and let the rest of the run finish."""
    from internpearls import config, sync
    deck = _source_updating_card_a(anki, tmp_path)   # guid-a, her mnemonic on Notes
    config.save_declined({"g-garbage": "not a dict"})

    drive(anki, sync.sync_decks, respond=_accept_everything)

    assert anki.col.note_by_guid("guid-a")["Notes"] == "her mnemonic"
    assert config.load_declined() == {"g-garbage": "not a dict"}


# --------------------------------------------- update_decks: seed, hide, confirm

def _all_text(tree):
    """Every label's text in the tree, joined into one string so a test can look for
    a phrase without caring which row it landed on."""
    return "\n".join(_label_texts(tree))


def _snapshot_update_confirmation(anki):
    """Run update_decks() to its confirmation, capture the tree it showed, then back
    out without applying anything (the same drive+capture shape _reconcile_tree uses
    for reconcile_decks())."""
    from internpearls import sync
    seen = {}
    click = _click_update_button(False)

    def respond(p):
        if p["kind"] == "dialog" and "tree" not in seen:
            seen["tree"] = p["tree"]
        return click(p)

    drive(anki, sync.update_decks, respond)
    return seen["tree"]


def _front_for_new_guid(guid):
    """The front `_source_with_two_new_cards` gave a "guid-new-<letter>" guid."""
    return f"front {guid.rsplit('-', 1)[-1]}"


def _find_row_button(tree, front_substring, label):
    """The decision-control button labeled `label` on whichever card row's primary
    line contains `front_substring`. The control sits right after that row's own
    primary label in document order, ahead of the next row's."""
    nodes = _walk(tree)
    start = next(i for i, n in enumerate(nodes)
                if n.get("t") == "label" and front_substring in (n.get("text") or ""))
    return next(n for n in nodes[start:]
               if n.get("t") == "button" and n.get("label") == label)


def _choose_option_for(guid, label, accept):
    """respond() that clicks the decision control's `label` button on `guid`'s card
    (found by its front text), then answers the confirmation's accept button with
    `accept`."""
    front = _front_for_new_guid(guid)
    state = {}

    def respond(p):
        if p["kind"] == "ask":
            return _answer_ask(p, None)
        if p["kind"] != "dialog":
            return {}
        done = _dismiss_result(p["tree"])
        if done:
            return done
        if not state.get("clicked"):
            state["clicked"] = True
            btn = _find_row_button(p["tree"], front, label)
            return {"events": [{"id": btn["id"], "click": True}]}
        return _click_update_button(accept)(p)
    return respond


def _choose_skip_for(guid):
    """respond() for update_decks(): choose Skip on `guid`'s card, then accept via
    Update."""
    return _choose_option_for(guid, "Skip", True)


def _choose_skip_then_cancel(guid):
    """Same as _choose_skip_for, but declines the confirmation afterward."""
    return _choose_option_for(guid, "Skip", False)


def _choose_import_for(guid):
    """respond() for update_decks(): flip a predeclined "new" card's row back to
    Import (its default), then accept via Update."""
    return _choose_option_for(guid, "Import", True)


def test_update_preview_hides_never_and_presets_skip(anki, tmp_path):
    from internpearls import config, sync
    deck = _source_with_two_new_cards(anki, tmp_path)
    config.save_declined({
        "guid-new-a": {"state": "never", "front": "front a", "deck": deck,
                       "decided": "2026-08-01", "hash": ""},
        "guid-new-b": {"state": "skip", "front": "front b", "deck": deck,
                       "decided": "2026-08-01", "hash": "stale-hash-value1"}})

    tree = _snapshot_update_confirmation(anki)

    texts = _all_text(tree)
    assert "front a" not in texts            # never: hidden entirely
    assert "1 card hidden" in texts          # ...but counted
    assert "SKIPPED" in texts                # skip re-offered, marked as such
    # and the stale hash still says so, on the row's own hint line
    assert "Changed since you skipped it" in texts


def test_deck_summary_counts_exclude_hidden_never_cards(anki, tmp_path):
    """The per-deck summary line must agree with the rows below it: a card hidden by
    a Never decline is not pending, so its deck cannot count it as new."""
    from internpearls import config
    deck = _source_with_two_new_cards(anki, tmp_path)
    config.save_declined({
        "guid-new-a": {"state": "never", "front": "front a", "deck": deck,
                       "decided": "2026-08-01", "hash": ""}})

    tree = _snapshot_update_confirmation(anki)

    texts = _all_text(tree)
    assert "1 new" in texts
    assert "2 new" not in texts, (
        "the deck summary still counts the hidden Never card as pending")


def test_a_row_wears_exactly_one_chip_after_a_kept_decline(anki, tmp_path):
    """A re-offered decline carries one chip, the decline's own: an earlier revision
    stacked the kind chip, the decline chip and a stale-content chip on the same row,
    which is three fixed-width columns plus the decision control on the rows most worth
    reading. The stale-content cue is a line under the row instead."""
    from internpearls import config
    from internpearls.widgets import CHIPS
    deck = _source_updating_card_a(anki, tmp_path)   # guid-a, reworded front
    config.save_declined({
        "guid-a": {"state": "keep", "front": "front a", "deck": deck,
                   "decided": "2026-08-01", "hash": "stale-hash-value1"}})

    tree = _snapshot_update_confirmation(anki)

    chips = [n["text"] for n in _walk(tree)
             if n.get("t") == "label" and n.get("text") in set(CHIPS.values())]
    assert chips == ["KEPT YOURS"], chips
    assert "Changed since you kept yours" in _all_text(tree)


def test_accepting_the_update_writes_decisions_to_the_registry(anki, tmp_path):
    from internpearls import config, sync
    _source_with_two_new_cards(anki, tmp_path)
    drive(anki, sync.update_decks,
          respond=_choose_skip_for("guid-new-b"))   # click skip, then Update
    reg = config.load_declined()
    assert reg["guid-new-b"]["state"] == "skip"
    assert reg["guid-new-b"]["hash"]            # hash captured from the incoming note
    assert "guid-new-a" not in reg              # imported normally
    fronts = {anki.col.get_note(nid).fields[0]
             for nid in anki.col.find_notes(f'"tag:{SCOPE}"')}
    assert "front b" not in fronts


def test_cancelling_writes_nothing_to_the_registry(anki, tmp_path):
    from internpearls import config, sync
    _source_with_two_new_cards(anki, tmp_path)
    drive(anki, sync.update_decks, respond=_choose_skip_then_cancel("guid-new-b"))
    assert config.load_declined() == {}


def test_digest_reports_decisions_made_this_run_only(anki, tmp_path):
    from internpearls import logic
    entries = logic.feedback_entries(
        {"g1": "too verbose"},
        {"g1": ("IP::A", "front a"), "g2": ("IP::A", "front b"),
         "g3": ("IP::A", "front c")},
        decisions={"g1": "skipped", "g2": "never", "g3": "imported after all"})
    digest = logic.build_feedback_digest(entries)
    assert "decision: skipped" in digest
    assert "front b" in digest and "decision: never" in digest
    # An un-decline (Skip/Keep back to the default) is a decision too, per the spec,
    # and gets its own reader-facing word.
    assert "front c" in digest and "decision: imported after all" in digest


def test_an_untouched_decline_keeps_its_stale_cue(anki, tmp_path):
    """A skip already on record when the confirmation opens, left untouched, must not
    have its hash/decided/front rebuilt just because she accepted an unrelated update:
    that would silently clear a pending "changed since decline" cue she never saw."""
    from internpearls import config
    deck = _source_with_two_new_cards(anki, tmp_path)
    config.save_declined({
        "guid-new-b": {"state": "skip", "front": "front b", "deck": deck,
                       "decided": "2026-08-01", "hash": "stale-hash-value1"}})

    _update(anki)   # accepts the confirmation without touching guid-new-b's row

    entry = config.load_declined()["guid-new-b"]
    assert entry["hash"] == "stale-hash-value1"
    assert entry["decided"] == "2026-08-01"
    assert entry["front"] == "front b"


def test_actively_reclicking_skip_refreshes_the_stale_hash(anki, tmp_path):
    """The active-click sibling of test_an_untouched_decline_keeps_its_stale_cue:
    clicking Skip again on a card whose incoming content changed since she last
    declined it is a re-review, so the stored hash/decided/front must refresh even
    though the state itself (skip) doesn't change. Not a new decision, though: it
    must not show up in the digest."""
    from internpearls import config, sync
    deck = _source_with_two_new_cards(anki, tmp_path)
    config.save_declined({
        "guid-new-b": {"state": "skip", "front": "front b", "deck": deck,
                       "decided": "2026-08-01", "hash": "stale-hash-value1"}})

    drive(anki, sync.update_decks, respond=_choose_skip_for("guid-new-b"))

    entry = config.load_declined()["guid-new-b"]
    assert entry["hash"] != "stale-hash-value1" and entry["hash"]
    assert entry["decided"] != "2026-08-01"
    assert anki.gui.clipboard == []   # re-confirming an unchanged state isn't a decision


def test_digest_reports_only_the_state_that_changed_this_run(anki, tmp_path):
    """The this-run-only filter, exercised through the real flow rather than
    logic.feedback_entries directly: a decline already on record when the dialog
    opened, left untouched, must not read as newly decided; one chosen this run
    must."""
    from internpearls import config, sync
    deck = _source_with_two_new_cards(anki, tmp_path)
    config.save_declined({
        "guid-new-a": {"state": "skip", "front": "front a", "deck": deck,
                       "decided": "2026-08-01", "hash": ""}})

    drive(anki, sync.update_decks, respond=_choose_skip_for("guid-new-b"))

    digest = anki.gui.clipboard[-1]
    assert digest.count("decision: skipped") == 1
    assert "front b" in digest
    assert "front a" not in digest


def test_flipping_a_predeclined_row_back_to_import_undeclines_it(anki, tmp_path):
    from internpearls import config, sync
    deck = _source_with_two_new_cards(anki, tmp_path)
    config.save_declined({
        "guid-new-b": {"state": "skip", "front": "front b", "deck": deck,
                       "decided": "2026-08-01", "hash": "some-hash"}})

    drive(anki, sync.update_decks, respond=_choose_import_for("guid-new-b"))

    assert "guid-new-b" not in config.load_declined()


def test_a_kept_decline_survives_when_its_row_becomes_new_again(anki, tmp_path):
    """A "keep" decline only means anything for a "changed" row. If the same guid's
    note vanishes from her collection (she deleted it) and the next update re-offers
    it as brand new instead, the row's control can't even show "keep" (a new row only
    offers Import/Skip/Never), so leaving it untouched must not read as "she flipped
    it back to default" and silently drop the registry entry."""
    from internpearls import config
    deck = _source_updating_card_a(anki, tmp_path)   # guid-a, "front a" -> reworded
    config.save_declined({
        "guid-a": {"state": "keep", "front": "front a", "deck": deck,
                   "decided": "2026-08-01", "hash": "stale-hash-value1"}})
    del anki.col._notes[anki.col.note_by_guid("guid-a").id]   # she deleted her card

    _update(anki)   # accepts without touching guid-a's row (now rendered as new)

    entry = config.load_declined().get("guid-a")
    assert entry is not None and entry["state"] == "keep"


def test_actively_importing_a_kind_flipped_decline_removes_it(anki, tmp_path):
    """The active-click sibling of test_a_kept_decline_survives_when_its_row_becomes_
    new_again: if she actually clicks Import on that re-offered row rather than just
    leaving it, that IS an explicit un-decline (default states are sparse, so nothing
    in `decisions` itself can tell the two apart) and the card must import."""
    from internpearls import config, sync
    deck = _source_updating_card_a(anki, tmp_path)   # guid-a, "front a" -> reworded
    config.save_declined({
        "guid-a": {"state": "keep", "front": "front a", "deck": deck,
                   "decided": "2026-08-01", "hash": "stale-hash-value1"}})
    del anki.col._notes[anki.col.note_by_guid("guid-a").id]   # she deleted her card
    state = {"clicked": False}

    def respond(p):
        if p["kind"] == "ask":
            return {"answer": True}
        if p["kind"] != "dialog":
            return {}
        done = _dismiss_result(p["tree"])
        if done:
            return done
        if not state["clicked"]:
            state["clicked"] = True
            btn = _find_row_button(p["tree"], "front a, revised", "Import")
            return {"events": [{"id": btn["id"], "click": True}]}
        return _click_update_button(True)(p)

    drive(anki, sync.update_decks, respond)

    assert config.load_declined().get("guid-a") is None
    fronts = {n.fields[0] for n in anki.col._notes.values()}
    assert "front a, revised" in fronts
    digest = anki.gui.clipboard[-1]
    assert "decision: imported after all" in digest


# ------------------------------------ declines and the note-type conversion plan
def _declined_conversion_source(anki, tmp_path, state):
    """Her Q-and-A note, a source shipping it as a fill-in-the-blank, and a standing
    decline on that very card."""
    from internpearls import config
    _cloze_conversion_source(anki, tmp_path)
    config.save_declined({"g1": {"state": state, "front": "Old Q and A front",
                                 "deck": DECK, "decided": "2026-08-01", "hash": ""}})


def _recording_ask(asked, answer=True):
    def ask(text):
        asked.append(text)
        return answer
    return ask


def _assert_her_card_was_left_alone(anki, asked):
    """A declined card's note keeps its type, its content and its history, and nothing
    about it was ever put on screen as a question."""
    note = anki.col.note_by_guid("g1")
    assert note.note_type()["name"] == "Study Deck - Basic"
    assert note.fields[0] == "Old Q and A front"
    assert anki.col.notetype_changes == []
    assert not any("changed format" in t for t in asked), asked


def test_a_skip_declined_card_is_never_converted_or_asked_about(anki, tmp_path):
    """The conversion plan used to be computed with no idea the card was declined, so
    her note was moved onto the fill-in-the-blank type while the import that writes the
    blanks in was dropped: a cloze note holding a question and answer, and no blanks."""
    _declined_conversion_source(anki, tmp_path, "skip")
    asked = []

    _sync(anki, ask=_recording_ask(asked))

    _assert_her_card_was_left_alone(anki, asked)


def test_a_keep_declined_card_is_never_converted_or_asked_about(anki, tmp_path):
    _declined_conversion_source(anki, tmp_path, "keep")
    asked = []

    _sync(anki, ask=_recording_ask(asked))

    _assert_her_card_was_left_alone(anki, asked)


def test_a_never_declined_card_is_never_converted_or_asked_about(anki, tmp_path):
    """The worst of the three: a Never card is hidden from the confirmation entirely,
    so the question asked about a card the screen refused to show."""
    _declined_conversion_source(anki, tmp_path, "never")
    asked = []

    _sync(anki, ask=_recording_ask(asked))

    _assert_her_card_was_left_alone(anki, asked)


def test_a_deck_whose_only_conversion_is_declined_is_not_deferred(anki, tmp_path):
    """Unattended auto-sync holds a deck back for a conversion, and a deck whose only
    conversion belonged to a declined card was held back forever: nothing would ever
    convert, so no manual sync could clear it either."""
    from internpearls import sync
    from internpearls.config import _cfg
    _declined_conversion_source(anki, tmp_path, "never")
    manifest, fetch, _source = sync._fetch_manifest(_cfg())

    results, _restored, _tpl, deferred, _c, _col, converted = sync._run_sync(
        _cfg(), manifest, fetch, manifest["decks"], defer_template_changes=True)

    assert deferred == [] and converted == 0
    assert results[0].startswith("✓")
    assert json.load(open(sync.INSTALLED, encoding="utf8")) == {DECK: "v2"}


def test_a_mixed_deck_asks_only_about_the_conversions_left(anki, tmp_path):
    """One declined card and one not: the question covers the one she can still say
    yes to, and only that one's note moves."""
    from internpearls import config
    anki.col.models._models.append(_cloze_model())
    _her_card(anki, "g1", "Old Q and A front")
    _her_card(anki, "g2", "Another Q and A front")
    config.save_declined({"g1": {"state": "never", "front": "Old Q and A front",
                                 "deck": DECK, "decided": "2026-08-01", "hash": ""}})
    _configure(anki, _write_source(tmp_path, {
        DECK: ("v2", [("g1", ["A {{c1::cloze}} version", "why", "", "", ""], TAGS),
                      ("g2", ["Another {{c1::cloze}} version", "why", "", "", ""],
                       TAGS)], _cloze_model())}))
    asked = []

    _sync(anki, ask=_recording_ask(asked))

    assert any("<b>1 card</b> in this update changed format" in t for t in asked), asked
    assert anki.col.note_by_guid("g1").note_type()["name"] == "Study Deck - Basic"
    assert anki.col.note_by_guid("g2").note_type()["name"] == "Study Deck - Cloze"


# ------------------------------------------ a registry file that isn't a registry
def test_a_garbage_registry_file_reads_as_an_empty_registry(anki):
    """Not just a garbage entry: a whole file that parsed as valid JSON and isn't an
    object at all. It used to pass the sync's own set() over it and only raise later,
    after the import had overwritten her protected fields."""
    from internpearls import config
    for junk in (["a deck"], "just a string", 5):
        config._save_json(config.DECLINED, junk)
        assert config.load_declined() == {}


def test_a_garbage_registry_file_still_imports_and_restores(anki, tmp_path):
    from internpearls import config, sync
    _source_updating_card_a(anki, tmp_path)   # guid-a, her mnemonic on Notes
    config._save_json(config.DECLINED, ["not", "a", "registry"])

    drive(anki, sync.sync_decks, respond=_accept_everything)

    note = anki.col.note_by_guid("guid-a")
    assert note.fields[0] == "front a, revised"    # the import ran
    assert note["Notes"] == "her mnemonic"         # ...and so did the restore after it


def test_a_dict_of_garbage_values_still_imports_and_restores(anki, tmp_path):
    from internpearls import config, sync
    _source_updating_card_a(anki, tmp_path)
    config._save_json(config.DECLINED, {"g-one": "not a dict", "g-two": ["nor this"]})

    drive(anki, sync.sync_decks, respond=_accept_everything)

    note = anki.col.note_by_guid("guid-a")
    assert note.fields[0] == "front a, revised"
    assert note["Notes"] == "her mnemonic"


def test_registry_housekeeping_cannot_skip_the_field_restore(anki, tmp_path,
                                                             monkeypatch):
    """The ordering, not the one exception that exposed it. Registry housekeeping used
    to sit between the import and the restore, so anything it raised left her
    annotations exactly as the import had overwritten them, with the decks recorded as
    installed and the snapshot gone."""
    from internpearls import sync

    def boom(*_a, **_kw):
        raise AttributeError("'list' object has no attribute 'items'")

    _source_updating_card_a(anki, tmp_path)
    monkeypatch.setattr(sync, "prune_declined", boom)

    drive(anki, sync.sync_decks, respond=_accept_everything)

    note = anki.col.note_by_guid("guid-a")
    assert note["Notes"] == "her mnemonic"         # the restore still ran
    assert note.fields[0] == "front a, revised"


# --------------------------------------- a conversion with nowhere to convert onto
def test_a_conversion_with_no_target_note_type_holds_the_deck_back(anki, tmp_path):
    """An all-basic collection meeting its source's first fill-in-the-blank card: the
    note type does not exist yet, so the conversion silently converted nothing while
    the deck was still recorded as installed and never offered again."""
    from internpearls import sync
    _her_card(anki, "g1", "Old Q and A front")     # no cloze note type in here
    _configure(anki, _write_source(tmp_path, {
        DECK: ("v2", [("g1", ["A {{c1::cloze}} version", "why", "", "", ""], TAGS)],
               _cloze_model())}))

    trees = _sync(anki)

    assert anki.col.notetype_changes == []
    assert not any("changed format" in a for a in anki.gui.asks)   # nothing asked
    assert json.load(open(sync.INSTALLED, encoding="utf8")) == {}  # not recorded
    # Named as the second pass it is, and naming the button that runs it: reported as a
    # bare absence, this read as a failure to the one person who has ever seen it.
    summary = _summary_text(trees)
    assert "no Study Deck - Cloze note type until this import added it" in summary
    assert "One more pass needed" in summary
    assert "Update my decks</b> again" in summary

    # ...and it is self-correcting: that import is what added the note type, so the
    # next run finds the deck still pending and moves her card across for real.
    _sync(anki)

    assert anki.col.note_by_guid("g1").note_type()["name"] == "Study Deck - Cloze"
    assert json.load(open(sync.INSTALLED, encoding="utf8")) == {DECK: "v2"}


def test_a_finished_conversion_is_reported_rather_than_discarded(anki, tmp_path):
    """The one thing a reader agreed to a one-time full AnkiWeb sync for was the one
    thing no summary mentioned: both callers threw the count away."""
    _cloze_conversion_source(anki, tmp_path)

    trees = _sync(anki)

    assert "Moved 1 card to the new format" in _summary_text(trees)


# --------------------------------- a conversion only the apply step gets to see
def _fetch_that_fails_until_apply(sync, monkeypatch):
    """Every download raises until _run_sync's own fetch: the preview fails, the
    pre-question retry fails too, and only the third attempt succeeds. Pinned to the
    phase rather than to an attempt count so it survives Runner's replay."""
    phase = {"early": True}
    real_fetch, real_run = sync._cached_fetch, sync._run_sync

    def flaky(fetch, d, on_chunk=None):
        if phase["early"]:
            raise RuntimeError("the source hiccuped")
        return real_fetch(fetch, d, on_chunk=on_chunk)

    def run(*a, **kw):
        phase["early"] = False
        try:
            return real_run(*a, **kw)
        finally:
            phase["early"] = True

    monkeypatch.setattr(sync, "_cached_fetch", flaky)
    monkeypatch.setattr(sync, "_run_sync", run)


def test_a_conversion_only_the_apply_step_finds_defers_the_deck(anki, tmp_path,
                                                                monkeypatch):
    """The run's one conversion question is decided before the apply loop. A deck that
    failed both the preview and the retry contributed nothing to it, so the loop found
    its conversion with convert=False and declined it on her behalf: her cards stayed
    on the old type, the new ones landed beside them, and the deck was recorded as
    installed, so nothing offered it again."""
    from internpearls import sync
    _cloze_conversion_source(anki, tmp_path)
    _fetch_that_fails_until_apply(sync, monkeypatch)
    asked = []

    trees = _update(anki, ask=_recording_ask(asked))

    assert not any("changed format" in t for t in asked), asked
    assert anki.col.notetype_changes == []
    note = anki.col.note_by_guid("g1")
    assert note.note_type()["name"] == "Study Deck - Basic"
    assert note.fields[0] == "Old Q and A front"        # nothing landed beside it
    assert json.load(open(sync.INSTALLED, encoding="utf8")) == {}   # still pending
    assert "note-type format update" in _summary_text(trees)


def test_a_swept_preview_download_does_not_double_count_its_conversions(
        anki, tmp_path, monkeypatch):
    """A deck previewed fine and then swept out of the temp directory is downloaded
    again before the question. Adding those conversions to the ones the preview already
    found counted every card in that deck twice."""
    from internpearls import sync
    _cloze_conversion_source(anki, tmp_path)
    real_retry = sync._retry_failed_downloads

    def sweep(fetch, todo, downloaded, *a, **kw):
        downloaded.clear()          # as if the tempdir had been swept since
        return real_retry(fetch, todo, downloaded, *a, **kw)

    monkeypatch.setattr(sync, "_retry_failed_downloads", sweep)
    asked = []

    _update(anki, ask=_recording_ask(asked))

    assert any("<b>1 card</b> in this update changed format" in t for t in asked), asked
    assert not any("<b>2 card" in t for t in asked), asked


# ------------------------------------ preview counts vs. what actually imports
def test_deck_summary_counts_exclude_a_standing_skip(anki, tmp_path):
    """A standing skip drops the card from the import as surely as a Never does, so
    counting it as pending made the preview say "1 new" and the result say none."""
    from internpearls import config
    deck = _source_with_two_new_cards(anki, tmp_path)
    config.save_declined({
        "guid-new-b": {"state": "skip", "front": "front b", "deck": deck,
                       "decided": "2026-08-01", "hash": ""}})

    texts = _all_text(_snapshot_update_confirmation(anki))

    assert "1 new" in texts
    assert "2 new" not in texts


def test_deck_summary_counts_exclude_a_standing_keep(anki, tmp_path):
    """Same on the other side of the row: a kept card is not changing, because the
    update to it is dropped."""
    from internpearls import config
    deck = _source_updating_card_a(anki, tmp_path)
    config.save_declined({
        "guid-a": {"state": "keep", "front": "front a", "deck": deck,
                   "decided": "2026-08-01", "hash": ""}})

    texts = _all_text(_snapshot_update_confirmation(anki))

    assert "changing" not in texts


def test_one_hidden_card_reads_as_one_card(anki, tmp_path):
    """"1 card hidden (Never). Restore them under..." counted one card and then
    pointed at several."""
    from internpearls import config
    deck = _source_with_two_new_cards(anki, tmp_path)
    config.save_declined({
        "guid-new-a": {"state": "never", "front": "front a", "deck": deck,
                       "decided": "2026-08-01", "hash": ""}})

    texts = _all_text(_snapshot_update_confirmation(anki))

    assert "1 card hidden (Never). Restore it under" in texts


# --------------------------------------------------- background poll bookkeeping
def test_a_deck_deferred_again_at_a_new_version_is_announced_again(anki, tmp_path):
    """The deferral skip-list is keyed by (deck, version); the tooltip's own list was
    keyed by deck name alone, so a deck deferred at one version, dealt with by hand,
    and deferred again at the next was never mentioned a second time. The only sign of
    it was the menu, which is exactly what the tooltip exists to point at."""
    from internpearls import background
    folder = _write_source(tmp_path, {
        DECK: ("v2", [("g1", _fields("Front one"), TAGS)], make_model(css=NEW_CSS))})
    anki.mw._config = {"decks_dir": folder, "auto_sync_decks": True}

    background._auto_sync_check()
    assert sum("card-template" in t for t in anki.gui.tooltips) == 1

    background._auto_sync_check()
    assert sum("card-template" in t for t in anki.gui.tooltips) == 1   # no re-nag

    _write_source(tmp_path, {
        DECK: ("v3", [("g1", _fields("Front one"), TAGS)], make_model(css=NEW_CSS))})
    background._auto_sync_check()

    assert sum("card-template" in t for t in anki.gui.tooltips) == 2


def test_an_absurd_poll_interval_is_capped_rather_than_overflowing(anki):
    """A hand-edited interval used to become a QTimer interval too big for a C int,
    which raised out of the startup wiring and gave Anki's own add-on error dialog on
    every launch until config.json was fixed by hand."""
    from internpearls import background

    background._restart_auto_sync_timer(99999999)

    timer = background._auto_sync_timer
    assert timer is not None and timer.started == 7 * 24 * 60 * 60 * 1000
    assert timer.started < 2 ** 31
    background._stop_auto_sync_timer()


def test_startup_scheduling_never_reaches_ankis_own_error_dialog(anki):
    """The scheduler wore no background guard at all, so anything it raised was Anki's
    raw add-on error dialog rather than a quiet failure."""
    from internpearls import background

    anki.mw._config = {"auto_sync_decks": True,
                       "auto_sync_interval_minutes": 99999999}
    background._schedule_background_checks()   # must not raise

    assert background._auto_sync_timer.started == 7 * 24 * 60 * 60 * 1000
    background._stop_auto_sync_timer()


# ------------------------------------------- backup buckets and their neighbours
def test_a_prehash_backup_belongs_only_to_the_deck_that_wrote_it(anki):
    """Labels are "<sanitized deck name> <hash>", and an older file carries the name
    alone. Matching that by prefix let the roots "Foo" and "Foo Bar" share a bucket, so
    Foo's old file counted in Foo Bar's prune and, being the oldest there, was evicted
    early."""
    from internpearls import collection
    foo, foo_bar = collection._backup_label("Foo"), collection._backup_label("Foo Bar")

    assert collection._in_backup_group("Foo", foo)          # the bucket that wrote it
    assert not collection._in_backup_group("Foo", foo_bar)  # not its neighbour's
    assert collection._in_backup_group(foo, foo)            # and an exact match still
    assert not collection._in_backup_group(foo, foo_bar)
    assert not collection._in_backup_group("", foo)


# ---------------------------------------- a failed auto-update that said nothing
def test_a_failed_auto_update_download_still_says_a_version_is_out(anki, monkeypatch):
    """The download raised out of the background work, so the whole result was lost:
    no tooltip, and the menu label never learned the version either. A newer release
    exists whether or not this launch could fetch it."""
    from internpearls import background, updates
    stub = _StubAction()
    updates.register_update_action(stub)
    anki.mw._config = {"auto_update_addon": True, "notify_addon_updates": True}
    monkeypatch.setattr(updates, "_fetch_addon_version_info",
                        lambda timeout=None: {"version": "99.0.0"})

    def boom(timeout=None):
        raise RuntimeError("the download failed")

    monkeypatch.setattr(updates, "_download_addon_package", boom)

    background._check_addon_updates_background()

    assert any("v99.0.0 is available" in t for t in anki.gui.tooltips), anki.gui.tooltips
    assert stub.text == "Check for add-on updates (v99.0.0 available)"


# ------------------------------- a card both ledgers claim is still just one card
def test_a_card_in_the_retired_ledger_and_a_reworded_pair_is_handled_once(anki,
                                                                          tmp_path):
    """Both paths write, both archive, and the summary counted the same card under
    "archived" and "merged" alike. The merge wins: it carries the card's scheduling
    forward as well as its annotations, and then archives it, which is everything the
    retirement path does and more."""
    from internpearls import sync
    old = _her_card(anki, "g_old", "the older wording")
    _her_card(anki, "g_new", "the newer wording")
    _sched(anki, old, reps=4, ivl=12, due=90, factor=2300, lapses=1, type=2, queue=2)
    _configure(anki, _stranded_and_retired(
        tmp_path, {"the older wording": "the newer wording"},
        {DECK: {"g_old": {"identity": "the older wording", "reason": "reworded",
                          "superseded_by": ["g_new"]}}}))

    drive(anki, sync.reconcile_decks, _click_reconcile_button(accept=True))

    summary = "\n".join(anki.gui.infos)
    assert "Merged <b>1 reworded card</b>" in summary
    assert "Archived" not in summary       # counted once, not once per ledger
    kept = anki.col.get_card(anki.col.note_by_guid("g_new").card_ids()[0])
    assert (kept.reps, kept.ivl, kept.due) == (4, 12, 90)   # scheduling carried
    dead = anki.col.get_card(anki.col.note_by_guid("g_old").card_ids()[0])
    assert dead.queue == -1 and RETIRED_TAG in anki.col.note_by_guid("g_old").tags


# ------------------------------------- import single deck and a format change
def test_import_single_offers_the_conversion_and_keeps_the_history(anki, tmp_path):
    """This path never looked for a note-type change at all, so a hand-picked
    fill-in-the-blank rebuild GUID-remapped onto her question-and-answer note, which
    Anki's importer will not update, while the confirmation promised the history would
    carry."""
    from internpearls import sync
    anki.col.models._models.append(_cloze_model())
    _her_card(anki, "g1", "Old Q and A front")
    src = str(tmp_path / "hand.apkg")
    make_apkg(src, [("g1", ["A {{c1::cloze}} version", "why", "", "", ""], TAGS)],
              model=_cloze_model(), deck=DECK)
    anki.gui.file_picks = [src]

    sync.import_single()

    assert any("changed format" in a for a in anki.gui.asks), anki.gui.asks
    note = anki.col.note_by_guid("g1")
    assert note.note_type()["name"] == "Study Deck - Cloze"
    assert note["Text"] == "A {{c1::cloze}} version"
    assert len(anki.col.find_notes(f'"tag:{SCOPE}"')) == 1     # one card, not two
    assert any("Moved 1 card to the new format" in i for i in anki.gui.infos)


def test_import_single_says_so_when_there_is_no_note_type_to_convert_onto(anki,
                                                                          tmp_path):
    """With the target note type absent there is nothing to convert onto, and the
    import is what creates it. Nothing is asked, and the count above no longer claims
    a history those cards cannot keep."""
    from internpearls import sync
    _her_card(anki, "g1", "Old Q and A front")
    src = str(tmp_path / "hand.apkg")
    make_apkg(src, [("g1", ["A {{c1::cloze}} version", "why", "", "", ""], TAGS)],
              model=_cloze_model(), deck=DECK)
    anki.gui.file_picks = [src]

    sync.import_single()

    assert not any("you'll be asked" in a for a in anki.gui.asks), anki.gui.asks
    assert any("can't carry over this time" in a for a in anki.gui.asks), anki.gui.asks
    assert anki.col.notetype_changes == []


def test_update_preview_hides_a_frozen_card_the_way_it_hides_a_never(anki, tmp_path):
    """Stop updating has to mean it. A frozen guid drops out of the confirmation
    entirely rather than coming back every time the deck changes, which is the whole
    difference between it and Keep yours."""
    from internpearls import config
    deck = _source_with_two_new_cards(anki, tmp_path)
    config.save_declined({
        "guid-new-a": {"state": "frozen", "front": "front a", "deck": deck,
                       "decided": "2026-08-31", "hash": ""}})

    texts = _all_text(_snapshot_update_confirmation(anki))
    assert "front a" not in texts
    assert "1 card hidden" in texts
    assert "front b" in texts
