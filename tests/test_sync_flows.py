"""End-to-end flow tests over the real sync/collection/background modules,
running against the mock Anki from mock_anki.py (see conftest.py for wiring).

These drive the same code paths a click on Sync decks (or the auto-sync timer)
runs, with a real manifest + .apkg local-folder source built per test — the only
things mocked are Anki itself and the dialogs, which are recorded and scripted.
"""
import json

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


def _click_reconcile_button(accept):
    """respond() for reconcile_decks()'s confirmation dialog: click whichever button
    isn't labeled "Cancel" to accept (its label varies — "Archive", "Relocate", or
    "Archive and relocate" — depending on what's pending), or click Cancel to decline.

    Runner.start() flips gui.interactive on for the whole replay, so the info/warn
    calls reconcile_decks() also makes (e.g. the final "Archived N card(s)..." result)
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


def _fields(front, back="the back", notes=""):
    return [front, back, "why", "", "Pharm", "", notes]


def _write_source(tmp_path, decks, retired=None, deck_moves=None):
    """decks: {deck_name: (version, notes, model_or_None)} -> source folder path.
    `retired`/`deck_moves`, if given, ride along in the same manifest — update_decks()
    tests need a source that carries both a content update and a reconcile ledger."""
    folder = tmp_path / "source"
    folder.mkdir(exist_ok=True)
    manifest = {"schema": 1, "decks": [], "front_aliases": {},
                "retired": retired or {}, "deck_moves": deck_moves or {}}
    for name, (version, notes, model) in decks.items():
        fn = name.split("::")[-1].replace(" ", "_") + ".apkg"
        make_apkg(str(folder / fn), notes, model=model, deck=name)
        manifest["decks"].append({"name": name, "apkg": fn, "version": version,
                                  "cards": len(notes)})
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf8")
    return str(folder)


def _configure(anki, folder):
    anki.mw._config = {"decks_dir": folder}


DECK = "Intern Pearls::Intern Custom::Pharm"


# ------------------------------------------------------------------- sync decks
def test_first_sync_imports_everything_and_persists_versions(anki, tmp_path):
    from internpearls import sync
    folder = _write_source(tmp_path, {
        DECK: ("v1", [("g1", _fields("Front one"), TAGS),
                      ("g2", _fields("Front two"), TAGS)], None)})
    _configure(anki, folder)

    sync.sync_decks()

    assert len(anki.col.find_notes(f'"tag:{SCOPE}"')) == 2
    assert anki.col.note_by_guid("g1")["Front"] == "Front one"
    installed = json.load(open(sync.INSTALLED, encoding="utf8"))
    assert installed == {DECK: "v1"}
    assert any("Sync complete" in i for i in anki.gui.infos)


def test_second_sync_with_same_versions_is_a_no_op(anki, tmp_path):
    from internpearls import sync
    folder = _write_source(tmp_path, {
        DECK: ("v1", [("g1", _fields("Front one"), TAGS)], None)})
    _configure(anki, folder)
    sync.sync_decks()
    imports_after_first = len(anki.col.imports)

    sync.sync_decks()

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
    sync.sync_decks()
    imports_after_first = len(anki.col.imports)

    sync.sync_decks()

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
    sync.sync_decks()
    assert len(anki.col.find_notes(f'"tag:{SCOPE}"')) == 1

    # Simulate a collection revert: the collection rolls back to before the sync, but
    # installed.json — outside the collection — is untouched.
    anki.col._notes.clear()
    anki.col._cards.clear()
    anki.gui.infos.clear()

    sync.sync_decks()

    assert not any("up to date" in i for i in anki.gui.infos)
    assert any("Sync complete" in i for i in anki.gui.infos)
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
    sync.sync_decks()
    assert len(anki.col.find_notes(f'"tag:{SCOPE}"')) == 2

    # Only deck_b's card is erased by the revert; DECK's card and installed entry for
    # DECK survive untouched.
    lost_nid = next(nid for nid in anki.col.find_notes(f'"tag:{SCOPE}"')
                    if anki.col.get_note(nid).guid == "g2")
    del anki.col._notes[lost_nid]
    anki.gui.infos.clear()
    anki.gui.answers[:] = [True]

    sync.sync_decks()

    assert not any("up to date" in i for i in anki.gui.infos)
    assert any("Sync complete" in i for i in anki.gui.infos)
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

    sync.sync_decks()

    note = anki.col.note_by_guid("g1")
    assert note["Back"] == "NEW back"                    # content updated
    assert note["Notes"] == "her personal mnemonic"      # her field survived
    assert len(anki.col.find_notes(f'"tag:{SCOPE}"')) == 1   # updated, not duplicated


def test_reworded_front_with_stable_guid_updates_in_place_without_alias(anki, tmp_path):
    from internpearls import sync
    # Her card still shows the old wording; the rebuilt deck kept the GUID (the
    # stable-id convention) and ships a new front with NO front_aliases entry.
    her = anki.col.add_note("g1", _fields("Old wording",
                                          notes="annotation to keep"), [TAGS])
    folder = _write_source(tmp_path, {
        DECK: ("v2", [("g1", _fields("New wording, third revision"), TAGS)], None)})
    _configure(anki, folder)

    sync.sync_decks()

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

    sync.sync_decks()

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
    anki.gui.answers[:] = [True, True]   # confirm sync, then apply template

    sync.sync_decks()

    assert anki.col.models.all()[0]["css"] == NEW_CSS
    assert any("schema" in a or "full sync" in a for a in anki.gui.asks)


def test_template_change_declined_keeps_look_but_imports_content(anki, tmp_path):
    from internpearls import sync
    old_css = anki.col.models.all()[0]["css"]
    folder = _write_source(tmp_path, {
        DECK: ("v2", [("g1", _fields("Front one"), TAGS)],
               make_model(css=NEW_CSS))})
    _configure(anki, folder)
    anki.gui.answers[:] = [True, False]   # confirm sync, decline template

    sync.sync_decks()

    assert anki.col.models.all()[0]["css"] == old_css      # look unchanged
    assert anki.col.note_by_guid("g1")["Front"] == "Front one"   # content imported


def test_unchanged_template_never_prompts(anki, tmp_path):
    from internpearls import sync
    folder = _write_source(tmp_path, {
        DECK: ("v1", [("g1", _fields("Front one"), TAGS)], None)})
    _configure(anki, folder)

    sync.sync_decks()

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
    assert any("auto-synced 1 deck(s)" in t for t in anki.gui.tooltips)
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
    anki.gui.answers[:] = [True, True]
    sync.sync_decks()
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

    assert any("auto-synced 1 deck(s)" in t for t in anki.gui.tooltips)
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
    assert any("1 card(s) are ready to tidy up" in t for t in anki.gui.tooltips)
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
    assert "2 card(s)" in anki.gui.tooltips[-1]
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
    assert any("Archived <b>1</b>" in i for i in anki.gui.infos)


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
    assert any("1 personal note(s) carried over" in i for i in anki.gui.infos)


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


def test_reconcile_dialog_caps_a_large_list_and_stays_clickable(anki, tmp_path):
    """Regression test for the bug this whole scrollable-dialog change exists to fix:
    a large first-run backlog (e.g. 90 cards relocated by a single reorg) used to build
    an uncapped bullet list inside a plain askUser() box, which has no scroll area — the
    dialog could grow taller than the screen with its buttons unreachable. This confirms
    the list is capped in the rendered text and the accept button is still found and
    clickable even with a backlog well past the cap."""
    from internpearls import sync
    retired = {}
    for i in range(25):
        _her_card(anki, f"old{i}", f"bulky card {i}")
        retired[f"old{i}"] = {"identity": f"bulky card {i}", "reason": "split",
                              "superseded_by": []}
    folder = _write_retired_source(tmp_path, {DECK: retired})
    _configure(anki, folder)

    seen = {}

    def respond(p):
        if p["kind"] == "dialog":
            label = next(n for n in _walk(p["tree"]) if n.get("t") == "label")
            seen["text"] = label["text"]
            btn = next(n for n in _walk(p["tree"])
                      if n.get("t") == "button" and n.get("label") != "Cancel")
            return {"events": [{"id": btn["id"], "click": True}]}
        return {}

    drive(anki, sync.reconcile_decks, respond)

    assert seen["text"].count("<li>") == 16          # 15 shown + 1 "...and N more" line
    assert "...and 10 more" in seen["text"]
    assert "one-time catch-up" in seen["text"]
    assert any("Archived <b>25</b>" in i for i in anki.gui.infos)


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
    assert any("Moved <b>1</b>" in i for i in anki.gui.infos)


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


# ------------------------------------------------------------- update decks (unified)
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

    drive(anki, sync.update_decks, _click_reconcile_button(accept=True))

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
    assert any("Update complete" in i for i in anki.gui.infos)


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
            return {}   # the completion info dialog: nothing to inspect, just continue
        label = next((n for n in _walk(p["tree"]) if n.get("t") == "label"
                     and "kept" in (n.get("text") or "")), None)
        seen["text"] = label["text"] if label else None
        btn = next(n for n in _walk(p["tree"])
                  if n.get("t") == "button" and n.get("label") != "Cancel")
        return {"events": [{"id": btn["id"], "click": True}]}

    drive(anki, sync.update_decks, respond)

    assert seen.get("text"), "expected a kept/new preview line in the confirmation"
    assert "1 kept" in seen["text"] and "1 new" in seen["text"]


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

    drive(anki, sync.update_decks, _click_reconcile_button(accept=True))

    assert anki.col.note_by_guid("g1")["Front"] == "Front one"
    assert any("Update complete" in i for i in anki.gui.infos)


def test_update_decks_with_only_reconcile_pending_skips_sync_cleanly(anki, tmp_path):
    from internpearls import sync
    _her_card(anki, "g1", "Lidocaine — onset time?", deck=DECK)
    folder = _write_source(tmp_path, {}, deck_moves={"g1": {"from": DECK, "to": NEW_DECK}})
    _configure(anki, folder)

    drive(anki, sync.update_decks, _click_reconcile_button(accept=True))

    assert not anki.col.imports
    cid = anki.col.note_by_guid("g1").card_ids()[0]
    assert anki.col.decks.name(anki.col._cards[cid].did) == NEW_DECK
    assert any("Update complete" in i for i in anki.gui.infos)


def test_update_decks_declined_leaves_everything_untouched(anki, tmp_path):
    from internpearls import sync
    anki.col.add_note("old1", _fields("bulky crisis card"), TAGS.split())
    folder = _write_source(
        tmp_path, {DECK: ("v1", [("g1", _fields("Front one"), TAGS)], None)},
        retired={DECK: {"old1": {"identity": "bulky crisis card", "reason": "split",
                                 "superseded_by": []}}})
    _configure(anki, folder)

    drive(anki, sync.update_decks, _click_reconcile_button(accept=False))

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

    drive(anki, sync.update_decks, _click_reconcile_button(accept=True))

    assert anki.col.note_by_guid("g1")["Front"] == "Front one"
    assert not any(n.guid == "g2" for n in anki.col._notes.values())
    installed = json.load(open(sync.INSTALLED, encoding="utf8"))
    assert installed == {DECK: "v1"}
    old = anki.col.note_by_guid("old1")
    assert RETIRED_TAG not in old.tags   # reconcile skipped, nothing archived
    assert any("stopped early" in i.lower() for i in anki.gui.infos)


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
    assert any("Archived <b>1</b>" in i for i in anki.gui.infos)


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
