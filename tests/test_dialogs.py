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
        "Sync decks", "Reconcile my decks", "Clean up duplicate cards",
        "Remove empty cards", "Import single deck (manual)", "Fix note types",
        "Backup intern pearls deck",
        "Import intern pearls deck", "Export intern pearls deck",
        "Backup full collection", "Restore full collection",
        "Check for add-on updates"]
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


def test_deck_state_pills_use_the_palette():
    from internpearls import dialogs, palette
    active = palette.colors()
    for state in ("new", "update", "current"):
        label, role = dialogs._STATE_STYLE[state]
        assert role in active, f"{state} pill uses an unknown palette role {role!r}"


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


def test_card_feedback_is_off_by_default(anki):
    from internpearls.config import _cfg
    assert _cfg()["collect_feedback"] is False


def test_settings_saves_feedback_toggle(anki):
    from internpearls import dialogs

    anki.gui.interactive = True

    def respond(p):
        if p["kind"] == "dialog":
            feedback = find(p["tree"], t="check",
                            label="Let me flag problems with new cards as they sync")
            assert feedback is not None
            save = find(p["tree"], t="button", label="Save")
            return {"events": [{"id": feedback["id"], "value": True},
                               {"id": save["id"], "click": True}]}
        return {}

    drive(anki, dialogs.open_settings, respond)
    cfg = anki.mw._config
    assert cfg["collect_card_feedback"] is True


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
        if p["kind"] == "msgbox":
            btn = next(b for b in p["buttons"] if b["label"] == "Local folder")
            return {"events": [{"id": btn["id"], "click": True}]}
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
    from internpearls import dialogs, palette
    active = palette.colors()
    anki.gui.interactive = True

    def respond(p):
        if p["kind"] == "msgbox":
            assert "color:gray" not in p["text"]
            assert active["muted"] in p["text"]
            cancel = next(b for b in p["buttons"] if b["label"] == "Cancel")
            return {"events": [{"id": cancel["id"], "click": True}]}
        return {}

    drive(anki, dialogs.configure_source, respond)


def test_configure_source_switching_to_github_repo_clears_local_folder(anki, tmp_path):
    """Guard against the fix above breaking symmetry: picking a GitHub repo while a
    local folder is configured must still clear the folder."""
    from internpearls import dialogs
    anki.mw._config = {"decks_dir": _write_source(tmp_path)}
    anki.gui.interactive = True

    def respond(p):
        if p["kind"] == "msgbox":
            btn = next(b for b in p["buttons"] if b["label"] == "GitHub repo")
            return {"events": [{"id": btn["id"], "click": True}]}
        if p["kind"] == "dialog":
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


def test_review_row_starts_collapsed_and_the_caret_expands_it(anki, monkeypatch):
    """The headline interaction of the new-card review dialog: a card's answer
    stays hidden until the reader asks for it. Confirms the row's body starts
    collapsed, and that clicking the caret both reveals it and flips the glyph."""
    from internpearls import review

    detail = {
        "guid": "g1",
        "notetype": "Study Deck - Basic",
        "fields": [
            ("Front", "What nerve block covers the anterior thigh?"),
            ("Back", "Femoral nerve block"),
            ("Why", ""), ("Image", ""), ("Tag", ""), ("Dosing", ""), ("Notes", ""),
        ],
    }
    decks = [("Intern Pearls::Intern Custom::Pharm", [detail])]
    rounds = []
    body_id = []

    def fake_next_interaction(payload):
        rounds.append(payload)
        tree = payload["tree"]
        if len(rounds) == 1:
            caret = find(tree, t="button", label=review._CARET_CLOSED)
            body = find(tree, t="box", visible=False)
            assert caret is not None, "row must start with the closed-caret glyph"
            assert body is not None, "row's body must start collapsed"
            body_id.append(body["id"])
            return {"events": [{"id": caret["id"], "click": True}]}
        caret = find(tree, t="button", label=review._CARET_OPEN)
        assert caret is not None, "clicking the caret must flip it to the open glyph"
        assert find(tree, t="button", label=review._CARET_CLOSED) is None
        body = find(tree, t="box", id=body_id[0])
        assert body["visible"] is True, "clicking the caret must reveal the body"
        done = find(tree, t="button", label="Done")
        return {"events": [{"id": done["id"], "click": True}]}

    monkeypatch.setattr(anki.gui, "next_interaction", fake_next_interaction)
    review.review_cards(None, decks, {})

    assert len(rounds) == 2, "expected one round to open the row, one to finish"


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
        buttons = [n for n in walk(p["tree"]) if n.get("t") == "button"]
        assert [b["label"] for b in buttons] == ["OK"]
        return {"events": [{"id": buttons[0]["id"], "click": True}]}

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
    manifest-suggestion question with `answer`. Returns the asks seen."""
    from internpearls import dialogs
    anki.gui.interactive = True
    asks = []

    def respond(p):
        if p["kind"] == "msgbox":
            btn = next(b for b in p["buttons"] if b["label"] == "Local folder")
            return {"events": [{"id": btn["id"], "click": True}]}
        if p["kind"] == "prompt":
            return {"text": path, "ok": True}
        if p["kind"] == "ask":
            asks.append(p["text"])
            return {"answer": answer}
        assert p["kind"] == "info"
        return {}

    drive(anki, dialogs.configure_source, respond)
    return asks


def test_configure_source_offers_manifest_scope_and_applies_on_yes(anki, tmp_path):
    asks = _drive_configure_local_folder(anki, _write_scoped_source(tmp_path), True)
    assert len(asks) == 1 and "CardioDeck" in asks[0] and "Cardio" in asks[0]
    assert anki.mw._config["scope_tag"] == "CardioDeck"
    assert anki.mw._config["export_deck"] == "Cardio"


def test_configure_source_manifest_scope_declined_leaves_config_alone(anki, tmp_path):
    asks = _drive_configure_local_folder(anki, _write_scoped_source(tmp_path), False)
    assert len(asks) == 1
    assert anki.mw._config.get("scope_tag") not in ("CardioDeck",)
    assert anki.mw._config.get("export_deck") not in ("Cardio",)


def test_configure_source_without_manifest_scope_asks_nothing(anki, tmp_path):
    asks = _drive_configure_local_folder(anki, _write_source(tmp_path), True)
    assert asks == []


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
