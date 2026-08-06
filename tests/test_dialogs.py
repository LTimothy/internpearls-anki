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
def _advanced_labels(anki):
    """Build the real menu and return just the Advanced submenu's item labels,
    in order."""
    menu = mock_anki.load_addon_init()
    sub = next(n for n in menu.tree() if n["t"] == "menu")
    return [n["label"] for n in sub["items"] if n["t"] == "item"]


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
        "Clean up duplicate cards", "Remove empty cards", "Fix note types",
        "Backup intern pearls deck",
        "Restore intern pearls deck", "Export intern pearls deck",
        "Backup full collection", "Restore full collection",
        "Check for add-on updates"]
    # primary items above the first separator, Settings/About below the last
    assert tree[2]["t"] == "sep" and tree[-3]["t"] == "sep"


def test_advanced_groups_source_actions_then_repair_actions(anki):
    """Advanced held twelve items whose first group mixed running half of Update,
    repairing the collection, and a one-off import, so neither reader had a group to find.
    """
    labels = _advanced_labels(anki)   # follow this file's existing menu-reading helper
    def at(label):
        return labels.index(label)
    assert at("Sync decks") < at("Import single deck (manual)") < at("Clean up duplicate cards")
    assert at("Clean up duplicate cards") < at("Fix note types") < at("Backup intern pearls deck")
    assert "Restore intern pearls deck" in labels
    assert "Import intern pearls deck" not in labels


def test_menu_actions_call_real_functions(anki, tmp_path):
    menu = mock_anki.load_addon_init()
    tree = menu.tree()
    update_item = next(n for n in tree if n.get("label") == "Update my decks")
    # no source configured -> the real update_decks warns about exactly that
    mock_anki.trigger_action(update_item["id"])
    assert any("No deck source configured" in w for w in anki.gui.warnings)


# ----------------------------------------------------------------- manage decks
def test_manage_decks_exclude_and_save(anki, tmp_path):
    from internpearls import dialogs, widgets
    anki.mw._config = {"decks_dir": _write_source(tmp_path)}
    anki.gui.interactive = True

    def respond(p):
        if p["kind"] == "dialog":
            row = find(p["tree"], t="check")
            assert row and "Pharm" in row["label"] and row["checked"]
            assert find(p["tree"], t="label", text=widgets.CHIPS["new"]), \
                "a deck the collection has none of must carry the NEW chip"
            assert find(p["tree"], t="label", text="1 card"), \
                "the row must still show its card count"
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

    seen = []

    def respond(p):
        if p["kind"] == "dialog":
            seen.append(p["tree"])
            update_btn = find(p["tree"], t="button", label="Save and update now")
            if update_btn:
                return {"events": [{"id": update_btn["id"], "click": True}]}
            # the end-of-run summary, a dialog of its own with a single OK on it
            done = find(p["tree"], t="button", label="OK")
            if done:
                return {"events": [{"id": done["id"], "click": True}]}
            # the confirmation from the real update_decks()
            confirm = find(p["tree"], t="button", label="Update")
            assert confirm, "expected update_decks' own confirmation dialog"
            return {"events": [{"id": confirm["id"], "click": True}]}
        return {}   # OK through info dialogs

    drive(anki, dialogs.manage_decks, respond)
    assert anki.col.note_by_guid("g1")["Front"] == "Front one"
    assert any("Update complete" in (n.get("text") or "")
               for n in walk(seen[-1])), "the run should report itself as complete"


def test_manage_decks_status_chip_recovers_after_a_collection_revert(anki, tmp_path):
    """installed.json survives a collection revert (it lives outside the collection
    file), so without reconciliation the row would keep reading "Up to date" for a deck
    whose cards the revert just erased. It should chip as NEW again, same as a deck that
    was never synced, since that's what's actually true of the collection."""
    from internpearls import dialogs, sync, widgets
    anki.mw._config = {"decks_dir": _write_source(tmp_path)}
    # Sync decks confirms through a widget body of deck rows, so it is driven rather
    # than called outright: its Update button is what answers the confirmation.
    def _answer(p):
        if p["kind"] != "dialog":
            return {}
        # Update on the confirmation, OK on the summary the finished run opens after it.
        btn = (find(p["tree"], t="button", label="Update")
               or find(p["tree"], t="button", label="OK"))
        return {"events": [{"id": btn["id"], "click": True}]}

    drive(anki, sync.sync_decks, _answer)
    assert anki.col.note_by_guid("g1")["Front"] == "Front one"

    anki.col._notes.clear()
    anki.col._cards.clear()
    anki.gui.interactive = True

    def respond(p):
        assert p["kind"] == "dialog"
        assert find(p["tree"], t="label", text=widgets.CHIPS["new"]), \
            "the row must chip as NEW again once the collection lost the deck"
        assert find(p["tree"], t="label", text="1 card"), \
            "the row must still show its card count"
        cancel = find(p["tree"], t="button", label="Cancel")
        return {"events": [{"id": cancel["id"], "click": True}]}

    drive(anki, dialogs.manage_decks, respond)


def test_deck_states_reuse_the_shared_chip_kinds():
    """Manage decks and Sync decks' confirmation both list decks and a reader moves
    between them, so a deck's state has to be the same chip on both. "current" maps to
    no chip deliberately: it's the resting state of most rows, and _deck_row leaves it
    as muted text rather than giving it a colour of its own."""
    from internpearls import dialogs, logic, widgets
    for state, kind in dialogs._STATE_CHIP.items():
        assert kind is None or kind in widgets.CHIPS, (
            f"{state} names a chip kind {kind!r} nothing paints")
    assert dialogs._STATE_CHIP["current"] is None
    manifest = {"decks": [{"name": "A", "version": "v2"}, {"name": "B", "version": "v2"},
                          {"name": "C", "version": "v1"}]}
    produced = {r["state"] for r in
                logic.deck_status(manifest, {"B": "v1", "C": "v1"}, ())}
    assert produced == set(dialogs._STATE_CHIP), (
        "a state deck_status returns with no entry here raises when its row builds")


def test_a_deck_row_chips_its_state_and_keeps_its_count_muted():
    """All three states in one place, which no source fixture offers at once: a chipped
    row says its state once (in the chip, not also in words beside it), and the resting
    row says "Up to date" in the same muted trailing text that carries the count."""
    from internpearls import dialogs, palette, widgets

    def labels(state):
        # _deck_row reads its row dict and files the checkbox under self._checks,
        # nothing else, so a bare instance is enough to build one without standing up
        # the whole dialog (and its source fetch) around it.
        panel = dialogs._DeckManagerDialog.__new__(dialogs._DeckManagerDialog)
        panel._checks = {}
        row = panel._deck_row({"name": f"Root::{state}", "short": state, "cards": 4,
                               "enabled": True, "state": state})
        return [n for n in walk(row.node()) if n.get("t") == "label"]

    texts = {state: [n["text"] for n in labels(state)]
             for state in ("new", "update", "current")}
    assert texts["new"] == ["4 cards", widgets.CHIPS["new"]]
    assert texts["update"] == ["4 cards", widgets.CHIPS["changed"]]
    assert texts["current"] == ["4 cards · Up to date"], "the resting row takes no chip"
    trailing = next(n for n in labels("current") if n["text"].startswith("4 cards"))
    assert palette.colors()["muted"] in trailing["style"]


def test_manage_decks_source_label_uses_the_palette_not_a_css_keyword(anki, tmp_path):
    from internpearls import dialogs, palette
    active = palette.colors()
    anki.mw._config = {"decks_dir": _write_source(tmp_path)}
    anki.gui.interactive = True

    def respond(p):
        assert p["kind"] == "dialog"
        source_label = next(n for n in walk(p["tree"])
                            if n.get("t") == "label"
                            and (n.get("text") or "").startswith("Source:"))
        assert "gray" not in source_label["style"]
        assert active["muted"] in source_label["style"]
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
        assert "checks every 30 minutes" in p["text"]
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


def test_card_feedback_is_off_by_default(anki):
    from internpearls.config import _cfg
    assert _cfg()["collect_feedback"] is False


def test_settings_saves_feedback_toggle(anki):
    from internpearls import dialogs

    anki.gui.interactive = True

    def respond(p):
        if p["kind"] == "dialog":
            feedback = find(p["tree"], t="check",
                            label="Let me flag problems with cards as they sync")
            assert feedback is not None
            save = find(p["tree"], t="button", label="Save")
            return {"events": [{"id": feedback["id"], "value": True},
                               {"id": save["id"], "click": True}]}
        return {}

    drive(anki, dialogs.open_settings, respond)
    cfg = anki.mw._config
    assert cfg["collect_card_feedback"] is True


# --------------------------------------------------------- configure source
def _source_buttons(tree):
    """Every button label on the source-choice dialog, in order. Also what tells that
    dialog apart from the GitHub form behind it: both surface as a "dialog" replay
    node, so a responder finds the option it wants rather than assuming which one it
    is looking at."""
    return [n["label"] for n in walk(tree) if n.get("t") == "button"]


def _pick_source(tree, label):
    """Click one of the source-choice dialog's option buttons, or return None if this
    dialog isn't it."""
    btn = find(tree, t="button", label=label)
    return {"events": [{"id": btn["id"], "click": True}]} if btn else None


def test_configure_source_offers_the_three_sources_with_the_example_first(anki):
    """The choice is its own dialog now, one option per line, with Cancel last and by
    itself. The example deck leads: it's the only source someone with no decks of their
    own can pick, and a one-row message box gave it no more weight than the other two.
    """
    from internpearls import dialogs
    anki.gui.interactive = True

    def respond(p):
        assert p["kind"] == "dialog"
        assert _source_buttons(p["tree"]) == [
            "Try the example deck", "GitHub repo", "Local folder", "Cancel"]
        return _pick_source(p["tree"], "Cancel")

    drive(anki, dialogs.configure_source, respond)
    # Cancel writes nothing: no config key, and no attempt to connect.
    assert anki.mw._config == {}
    assert not anki.gui.warnings


def test_configure_source_github_form(anki):
    from internpearls import dialogs

    anki.gui.interactive = True

    def respond(p):
        if p["kind"] == "dialog":
            pick = _pick_source(p["tree"], "GitHub repo")
            if pick:
                return pick
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


def test_configure_source_switching_to_local_folder_clears_repo_and_keeps_token(
        anki, tmp_path):
    """Picking Local folder while a repo is already configured must make the folder
    the effective source (a lingering repo otherwise wins inside _fetch_manifest) and
    must not throw away a token she'll need if she switches back."""
    from internpearls import dialogs
    anki.mw._config = {"github_decks_repo": "example-org/study-decks",
                       "github_token": "test-token-abc123"}
    anki.gui.interactive = True
    path = _write_source(tmp_path)

    def respond(p):
        if p["kind"] == "dialog":
            return _pick_source(p["tree"], "Local folder")
        if p["kind"] == "prompt":
            return {"text": path, "ok": True}
        # If the repo is left set, _fetch_manifest tries GitHub first and (with no
        # network in tests) this comes back "couldn't connect" instead of "info".
        assert p["kind"] == "info" and "Saved and connected" in p["text"]
        return {}

    drive(anki, dialogs.configure_source, respond)
    cfg = anki.mw._config
    assert cfg["decks_dir"] == path
    assert cfg["github_decks_repo"] == ""
    assert cfg["github_token"] == "test-token-abc123"


def test_configure_source_message_uses_the_palette_not_a_css_keyword(anki):
    """The explanation and the option hints are styled labels now rather than one
    rich-text message, so the check reads their styles instead of a message string:
    still the palette's own colours, still never the CSS keyword `gray`."""
    from internpearls import dialogs, palette
    active = palette.colors()
    anki.gui.interactive = True

    def respond(p):
        labels = [n for n in walk(p["tree"]) if n.get("t") == "label"]
        painted = "".join(n.get("style", "") + n.get("text", "") for n in labels)
        assert "color:gray" not in painted and "color: gray" not in painted
        assert active["muted"] in painted, "the explanation reads as muted text"
        assert active["accent"] in painted, "the recommended option is marked"
        return _pick_source(p["tree"], "Cancel")

    drive(anki, dialogs.configure_source, respond)


def test_configure_source_switching_to_github_repo_clears_local_folder(anki, tmp_path):
    """Guard against the fix above breaking symmetry: picking a GitHub repo while a
    local folder is configured must still clear the folder."""
    from internpearls import dialogs
    anki.mw._config = {"decks_dir": _write_source(tmp_path)}
    anki.gui.interactive = True

    def respond(p):
        if p["kind"] == "dialog":
            pick = _pick_source(p["tree"], "GitHub repo")
            if pick:
                return pick
            repo = find(p["tree"], t="line", password=False)
            ok = find(p["tree"], t="button", label="OK")
            return {"events": [{"id": repo["id"], "value": "example-org/study-decks"},
                               {"id": ok["id"], "click": True}]}
        # no network in tests: the real flow warns it saved but couldn't connect
        assert p["kind"] == "warn" and "couldn't connect" in p["text"]
        return {}

    drive(anki, dialogs.configure_source, respond)
    cfg = anki.mw._config
    assert cfg["github_decks_repo"] == "example-org/study-decks"
    assert cfg["decks_dir"] == ""


# --------------------------------------------------------------- feedback digest
def test_copy_again_puts_the_digest_back_on_the_clipboard(anki, monkeypatch):
    """A clipboard clobbered between copying and pasting should not cost the notes:
    clicking Copy again has to put the exact digest text back.

    Drives QDialog.exec()'s interaction hook directly instead of through drive():
    drive() replays a flow from a fresh snapshot on every feed(), which reruns
    offer_feedback_digest from the top and redoes its own initial clipboard copy.
    That copy alone would make the clipboard's last entry match the digest again,
    whether or not Copy again was actually clicked, so it can't tell the two apart.
    Scripting next_interaction directly keeps the run to one pass, so the only way
    the digest reappears on the clipboard after the clobber is the button itself.
    """
    from internpearls import review

    entries = [{"deck": "Intern Pearls::Intern Custom::Pharm", "front": "Front one",
               "guid": "g1", "note": "dose looks off"}]
    rounds = []

    def fake_next_interaction(payload):
        rounds.append(payload)
        tree = payload["tree"]
        if len(rounds) == 1:
            again = find(tree, t="button", label="Copy again")
            close = find(tree, t="button", label="Close")
            assert again and close, "expected a Copy again button beside Close"
            anki.gui.clipboard.append("something else, clobbered")
            return {"events": [{"id": again["id"], "click": True}]}
        close = find(tree, t="button", label="Close")
        assert close
        return {"events": [{"id": close["id"], "click": True}]}

    monkeypatch.setattr(anki.gui, "next_interaction", fake_next_interaction)
    review.offer_feedback_digest(None, entries)

    assert len(rounds) == 2, "Copy again must not close the dialog on its own"
    digest = anki.gui.clipboard[0]
    assert anki.gui.clipboard == [digest, "something else, clobbered", digest]


# ------------------------------------------------------------------------ about
def test_about_is_a_dialog_with_a_single_ok_button(anki):
    """About used to be a bare QMessageBox; it now routes through _ask_scrollable like
    every other dialog here, which means it shows up as a "dialog" replay node (not
    "msgbox") and keeps its one OK button rather than picking up a second, unwanted
    Cancel/Continue from the shared wrapper's usual pair."""
    from internpearls import dialogs
    from internpearls.config import ADDON_VERSION

    anki.gui.interactive = True

    def respond(p):
        assert p["kind"] == "dialog"
        body = find(p["tree"], t="label")
        assert ADDON_VERSION in body["text"] and "Auto-sync: off" in body["text"]
        # Its three settings read as lines of the prose around them. About is the one
        # screen here that genuinely is a block of prose, so they are not rows, but
        # they are not a bulleted list either.
        assert "<ul>" not in body["text"] and "<li>" not in body["text"]
        buttons = [n for n in walk(p["tree"]) if n.get("t") == "button"]
        assert [b["label"] for b in buttons] == ["OK"]
        return {"events": [{"id": buttons[0]["id"], "click": True}]}

    drive(anki, dialogs.about, respond)


def test_about_pending_update_notice_uses_the_warning_role(anki, tmp_path, monkeypatch):
    """dialogs.about() colours its pending-update notice with palette.colors()["warning"].
    Nothing else in either suite drives that branch, so a typo in the role name would
    only ever surface as a KeyError crashing About, for a real user with a pending
    update notice. Seed state.json with a newer "last known" version so the branch
    actually runs, the same way the real notice gets populated by a background update
    check, and drive About the same way the tests above do.

    dialogs.py binds its own STATE name at import time (see conftest.py's `anki`
    fixture, which patches config/background/updates but not dialogs for this reason),
    so the seed has to go through dialogs.STATE directly rather than the shared fixture
    path.
    """
    from internpearls import dialogs, palette
    from internpearls.config import _save_json

    state_path = tmp_path / "state.json"
    monkeypatch.setattr(dialogs, "STATE", str(state_path))
    newer = "9.9.9"
    _save_json(str(state_path), {"last_notified_addon_version": newer})
    anki.gui.interactive = True

    def respond(p):
        assert p["kind"] == "dialog"
        body = find(p["tree"], t="label")
        assert palette.colors()["warning"] in body["text"]
        assert newer in body["text"]
        ok = find(p["tree"], t="button", label="OK")
        return {"events": [{"id": ok["id"], "click": True}]}

    drive(anki, dialogs.about, respond)


def test_about_version_tag_uses_the_palette_not_a_css_keyword(anki):
    from internpearls import dialogs, palette
    active = palette.colors()
    anki.gui.interactive = True

    def respond(p):
        assert p["kind"] == "dialog"
        body = find(p["tree"], t="label")
        assert "color:gray" not in body["text"]
        assert active["muted"] in body["text"]
        ok = find(p["tree"], t="button", label="OK")
        return {"events": [{"id": ok["id"], "click": True}]}

    drive(anki, dialogs.about, respond)


def _write_scoped_source(tmp_path):
    """A local source whose manifest carries the author's suggested scope settings."""
    folder = tmp_path / "scoped"
    folder.mkdir()
    make_apkg(str(folder / "Cardio.apkg"),
              [("c1", ["Front", "back", "", "", "", "", ""], "CardioDeck::Basics")],
              deck="Cardio::Basics")
    (folder / "manifest.json").write_text(json.dumps({
        "schema": 2, "front_aliases": {},
        "scope_tag": "CardioDeck", "export_deck": "Cardio",
        "decks": [{"name": "Cardio::Basics", "apkg": "Cardio.apkg",
                   "version": "v1", "cards": 1}]}), encoding="utf8")
    return str(folder)


def _drive_configure_local_folder(anki, path, answer):
    """Run configure_source picking Local folder at `path`, answering the
    manifest-suggestion confirmation with `answer`.

    Returns what that confirmation showed, one entry per time it opened. It is a row
    list in its own dialog rather than a plain askUser box now, so what it recommends
    is read off its labels; its Apply button is also what tells it apart from the
    source-choice dialog that opens first.
    """
    from internpearls import dialogs
    anki.gui.interactive = True
    shown = []

    def respond(p):
        if p["kind"] == "dialog":
            if not find(p["tree"], t="button", label="Apply"):
                return _pick_source(p["tree"], "Local folder")
            shown.append("\n".join(n.get("text") or "" for n in walk(p["tree"])
                                   if n.get("t") == "label"))
            btn = find(p["tree"], t="button", label="Apply" if answer else "Cancel")
            return {"events": [{"id": btn["id"], "click": True}]}
        if p["kind"] == "prompt":
            return {"text": path, "ok": True}
        assert p["kind"] == "info"
        return {}

    drive(anki, dialogs.configure_source, respond)
    return shown


def test_configure_source_offers_manifest_scope_and_applies_on_yes(anki, tmp_path):
    shown = _drive_configure_local_folder(anki, _write_scoped_source(tmp_path), True)
    assert len(shown) == 1 and "CardioDeck" in shown[0] and "Cardio" in shown[0]
    assert "<li>" not in shown[0] and "<ul>" not in shown[0], \
        "each recommended setting is a row of its own, not a bullet in one label"
    assert anki.mw._config["scope_tag"] == "CardioDeck"
    assert anki.mw._config["export_deck"] == "Cardio"


def test_configure_source_manifest_scope_declined_leaves_config_alone(anki, tmp_path):
    shown = _drive_configure_local_folder(anki, _write_scoped_source(tmp_path), False)
    assert len(shown) == 1
    assert anki.mw._config.get("scope_tag") not in ("CardioDeck",)
    assert anki.mw._config.get("export_deck") not in ("Cardio",)


def test_configure_source_without_manifest_scope_asks_nothing(anki, tmp_path):
    assert _drive_configure_local_folder(anki, _write_source(tmp_path), True) == []


def test_ui_helpers_use_the_palette_not_a_css_keyword():
    """muted_label and hint_label used the CSS keyword `gray`, which is #808080 whatever
    the theme, and fails AA on both. A keyword is a hardcoded colour with a friendlier
    name."""
    from internpearls import palette, ui
    active = palette.colors()
    for helper in (ui.muted_label, ui.hint_label):
        style = helper("some text").styleSheet()
        assert "gray" not in style, f"{helper.__name__} still uses the gray keyword"
        assert active["muted"] in style
