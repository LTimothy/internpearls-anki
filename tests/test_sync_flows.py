"""End-to-end flow tests over the real sync/collection/background modules,
running against the mock Anki from mock_anki.py (see conftest.py for wiring).

These drive the same code paths a click on Sync decks (or the auto-sync timer)
runs, with a real manifest + .apkg local-folder source built per test — the only
things mocked are Anki itself and the dialogs, which are recorded and scripted.
"""
import json

from mock_anki import make_apkg, make_model

SCOPE = "InternPearls"
TAGS = f"{SCOPE}::Pharm"


def _fields(front, back="the back", notes=""):
    return [front, back, "why", "", "Pharm", "", notes]


def _write_source(tmp_path, decks):
    """decks: {deck_name: (version, notes, model_or_None)} -> source folder path."""
    folder = tmp_path / "source"
    folder.mkdir(exist_ok=True)
    manifest = {"schema": 1, "decks": [], "front_aliases": {}}
    for name, (version, notes, model) in decks.items():
        fn = name.split("::")[-1].replace(" ", "_") + ".apkg"
        make_apkg(str(folder / fn), notes, model=model)
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
