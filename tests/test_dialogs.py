"""Drives the REAL dialogs.py and __init__.py code through mock_anki's widget
layer — the same replay protocol the GitHub Pages demo uses, so anything green
here is exactly what the demo (and Anki) executes.

The driver pattern mirrors the demo's: run the flow; when it raises
NeedInteraction, decide a response from the serialized dialog tree (find
widgets by label, script clicks and edits), append it, and re-run. Flows are
deterministic, so the replay is exact.
"""
import json

import mock_anki
from mock_anki import make_apkg


def drive(anki, fn, respond):
    """Run `fn` to completion via the same snapshot-and-replay Runner the demo
    driver uses, answering each surfaced dialog through `respond`."""
    from internpearls import collection, sync
    runner = mock_anki.Runner(anki, paths=[sync.INSTALLED, collection._USER_FILES])
    runner.drive(fn, respond)


def walk(node, out=None):
    out = out if out is not None else []
    out.append(node)
    for c in node.get("children", []) or []:
        walk(c, out)
    return out


def find(tree, **want):
    for n in walk(tree):
        if all(n.get(k) == v for k, v in want.items()):
            return n
    return None


def _write_source(tmp_path, deck="Intern Pearls::Intern Custom::Pharm", version="v1",
                  retired=None, deck_moves=None):
    folder = tmp_path / "source"
    folder.mkdir(exist_ok=True)
    make_apkg(str(folder / "Pharm.apkg"),
              [("g1", ["Front one", "back", "", "", "", "", ""],
                "InternPearls::Pharm")], deck=deck)
    (folder / "manifest.json").write_text(json.dumps({
        "schema": 2, "front_aliases": {},
        "decks": [{"name": deck, "apkg": "Pharm.apkg", "version": version,
                   "cards": 1}],
        "retired": retired or {}, "deck_moves": deck_moves or {}}), encoding="utf8")
    return str(folder)


# ------------------------------------------------------------------------ menu
def test_real_menu_structure():
    menu = mock_anki.load_addon_init()
    tree = menu.tree()
    labels = [n.get("label") for n in tree if n["t"] == "item"]
    assert labels == ["Update my decks", "Manage decks", "Settings", "About"]
    sub = next(n for n in tree if n["t"] == "menu")
    assert sub["label"] == "Advanced"
    sub_labels = [n["label"] for n in sub["items"] if n["t"] == "item"]
    assert sub_labels == [
        "Sync decks", "Reconcile my decks", "Import single deck (manual)",
        "Fix note types", "Backup intern pearls deck", "Import intern pearls deck",
        "Export intern pearls deck", "Backup full collection",
        "Restore full collection", "Check for add-on updates"]
    # primary items above the first separator, Settings/About below the last
    assert tree[2]["t"] == "sep" and tree[-3]["t"] == "sep"


def test_menu_actions_call_real_functions(anki, tmp_path):
    menu = mock_anki.load_addon_init()
    tree = menu.tree()
    update_item = next(n for n in tree if n.get("label") == "Update my decks")
    # no source configured -> the real update_decks warns about exactly that
    mock_anki.trigger_action(update_item["id"])
    assert any("No deck source configured" in w for w in anki.gui.warnings)


# ----------------------------------------------------------------- manage decks
def test_manage_decks_exclude_and_save(anki, tmp_path):
    from internpearls import dialogs
    anki.mw._config = {"decks_dir": _write_source(tmp_path)}
    anki.gui.interactive = True

    def respond(p):
        if p["kind"] == "dialog":
            row = find(p["tree"], t="check")
            assert row and "Pharm" in row["label"] and row["checked"]
            pill = find(p["tree"], t="label", text="1 cards · New")
            assert pill, "status pill must show card count and New state"
            save = find(p["tree"], t="button", label="Save")
            return {"events": [{"id": row["id"], "value": False},
                               {"id": save["id"], "click": True}]}
        assert p["kind"] == "info" and "1 excluded" in p["text"]
        return {}

    drive(anki, dialogs.manage_decks, respond)
    cfg = anki.mw._config
    assert cfg["excluded_decks"] == ["Intern Pearls::Intern Custom::Pharm"]
    assert cfg["protected_fields"] == ["Notes"]


def test_manage_decks_save_and_update_now_runs_update_decks(anki, tmp_path):
    """Manage decks no longer previews or syncs on its own — "Save and update now"
    hands off to the real update_decks(), whose own confirmation is where the actual
    pending-work detail (and the retired/moves summary, covered by update_decks' own
    tests in test_sync_flows.py) now lives."""
    from internpearls import dialogs
    anki.mw._config = {"decks_dir": _write_source(tmp_path)}
    anki.gui.interactive = True

    def respond(p):
        if p["kind"] == "dialog":
            update_btn = find(p["tree"], t="button", label="Save and update now")
            if update_btn:
                return {"events": [{"id": update_btn["id"], "click": True}]}
            # the confirmation from the real update_decks()
            confirm = find(p["tree"], t="button", label="Update")
            assert confirm, "expected update_decks' own confirmation dialog"
            return {"events": [{"id": confirm["id"], "click": True}]}
        return {}   # OK through info dialogs

    drive(anki, dialogs.manage_decks, respond)
    assert anki.col.note_by_guid("g1")["Front"] == "Front one"
    assert any("Update complete" in i for i in anki.gui.infos)


def test_manage_decks_status_pill_recovers_after_a_collection_revert(anki, tmp_path):
    """installed.json survives a collection revert (it lives outside the collection
    file), so without reconciliation the status pill would keep reading "Current" for
    a deck whose cards the revert just erased. It should read "New" again, same as a
    deck that was never synced, since that's what's actually true of the collection."""
    from internpearls import dialogs, sync
    anki.mw._config = {"decks_dir": _write_source(tmp_path)}
    sync.sync_decks()
    assert anki.col.note_by_guid("g1")["Front"] == "Front one"

    anki.col._notes.clear()
    anki.col._cards.clear()
    anki.gui.interactive = True

    def respond(p):
        assert p["kind"] == "dialog"
        pill = find(p["tree"], t="label", text="1 cards · New")
        assert pill, "status pill must revert to New once the collection lost the deck"
        cancel = find(p["tree"], t="button", label="Cancel")
        return {"events": [{"id": cancel["id"], "click": True}]}

    drive(anki, dialogs.manage_decks, respond)


# --------------------------------------------------------------------- settings
def test_settings_saves_all_four_values(anki):
    from internpearls import dialogs

    anki.gui.interactive = True

    def respond(p):
        if p["kind"] == "dialog":
            auto = find(p["tree"], t="check",
                        label="Sync decks automatically when updates are available")
            spin = find(p["tree"], t="spin")
            assert spin["value"] == 15 and spin["suffix"] == " min"
            save = find(p["tree"], t="button", label="Save")
            return {"events": [{"id": auto["id"], "value": True},
                               {"id": spin["id"], "value": 30},
                               {"id": save["id"], "click": True}]}
        assert "checks every 30 minute(s)" in p["text"]
        return {}

    drive(anki, dialogs.open_settings, respond)
    cfg = anki.mw._config
    assert cfg["auto_sync_decks"] is True
    assert cfg["auto_sync_interval_minutes"] == 30


def test_settings_saves_dim_images_toggle(anki):
    from internpearls import dialogs

    anki.gui.interactive = True

    def respond(p):
        if p["kind"] == "dialog":
            dim = find(p["tree"], t="check", label="Dim bright images in Night Mode")
            assert dim is not None
            save = find(p["tree"], t="button", label="Save")
            return {"events": [{"id": dim["id"], "value": True},
                               {"id": save["id"], "click": True}]}
        return {}

    drive(anki, dialogs.open_settings, respond)
    cfg = anki.mw._config
    assert cfg["dim_images_night_mode"] is True


# --------------------------------------------------------- configure source
def test_configure_source_github_form(anki):
    from internpearls import dialogs

    anki.gui.interactive = True

    def respond(p):
        if p["kind"] == "msgbox":
            assert "Where should decks come from?" in p["text"]
            gh = next(b for b in p["buttons"] if b["label"] == "GitHub repo")
            return {"events": [{"id": gh["id"], "click": True}]}
        if p["kind"] == "dialog":
            repo = find(p["tree"], t="line", password=False)
            token = find(p["tree"], t="line", password=True)
            assert repo and token, "repo and masked token fields"
            ok = find(p["tree"], t="button", label="OK")
            return {"events": [{"id": repo["id"], "value": "someone/decks"},
                               {"id": ok["id"], "click": True}]}
        # no network in tests: the real flow warns it saved but couldn't connect
        assert p["kind"] == "warn" and "couldn't connect" in p["text"]
        return {}

    drive(anki, dialogs.configure_source, respond)
    assert anki.mw._config["github_decks_repo"] == "someone/decks"


# ------------------------------------------------------------------------ about
def test_about_shows_version_and_live_settings(anki):
    from internpearls import dialogs
    from internpearls.config import ADDON_VERSION

    anki.gui.interactive = True

    def respond(p):
        assert p["kind"] == "msgbox"
        assert ADDON_VERSION in p["text"] and "Auto-sync: off" in p["text"]
        ok = p["buttons"][0]
        return {"events": [{"id": ok["id"], "click": True}]}

    drive(anki, dialogs.about, respond)
