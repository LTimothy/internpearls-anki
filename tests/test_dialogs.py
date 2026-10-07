"""Drives the REAL dialogs.py and __init__.py code through mock_anki's widget
layer — the same replay protocol the GitHub Pages demo uses, so anything green
here is exactly what the demo (and Anki) executes.

The driver pattern mirrors the demo's: run the flow; when it raises
NeedInteraction, decide a response from the serialized dialog tree (find
widgets by label, script clicks and edits), append it, and re-run. Flows are
deterministic, so the replay is exact.
"""
import json
import os
import subprocess
import sys
import textwrap

import mock_anki
import pytest
from demo_contract_cases import contract_action_table
from mock_anki import make_apkg


@pytest.mark.parametrize("value", ["version", "field"])
def test_about_escapes_outside_text(anki, monkeypatch, value):
    from internpearls import dialogs
    text = "99 A <b>x</b> & B"
    if value == "version":
        monkeypatch.setattr(dialogs, "_load_json", lambda *a: {
            "last_notified_addon_version": text})
    else:
        anki.mw._config = {"protected_fields": [text]}
    seen = {}
    def respond(p):
        seen["text"] = find(p["tree"], t="label")["text"]
        btn = find(p["tree"], t="button", label="OK")
        return {"events": [{"id": btn["id"], "click": True}]}
    drive(anki, dialogs.about, respond)
    assert "A &lt;b&gt;x&lt;/b&gt; &amp; B" in seen["text"]
    assert "<b>x</b>" not in seen["text"]


def test_manage_decks_saved_message_escapes_field_names(anki, tmp_path):
    from internpearls import dialogs
    anki.mw._config = {"decks_dir": _write_source(tmp_path),
                       "protected_fields": ["A <b>x</b> & B"]}
    anki.col.models.add_field(anki.col.models.all()[0],
                              {"name": "A <b>x</b> & B"})
    def respond(p):
        if p["kind"] == "dialog":
            btn = find(p["tree"], t="button", label="Save")
            return {"events": [{"id": btn["id"], "click": True}]}
        return {}
    drive(anki, dialogs.manage_decks, respond)
    assert "A &lt;b&gt;x&lt;/b&gt; &amp; B" in anki.gui.infos[-1]
    assert "<b>x</b>" not in anki.gui.infos[-1]


def test_dialog_frontier_settles_current_replay_work(anki):
    from aqt.qt import QDialog, QLabel, QVBoxLayout
    from demo.replay import ReplayPlatform
    from internpearls.platform import WorkRequest, use_platform

    dialog = QDialog()
    label = QLabel("waiting")
    QVBoxLayout(dialog).addWidget(label)
    replay = ReplayPlatform(epoch=3)
    request = WorkRequest(
        "background-fetch", 1, 1, 1,
        {"action": "background.fetch"}, epoch=3)
    with use_platform(replay):
        handle = replay.start_work(
            request, lambda _context: "ready", label.setText, pytest.fail)
        handle.start()
        with pytest.raises(mock_anki.NeedInteraction) as needed:
            dialog.exec()

    labels = [node for node in needed.value.payload["contract_tree"]["nodes"]
              if node["kind"] == "label"]
    assert [node["text"] for node in labels] == ["ready"]


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
def _run_menu_probe(script, *args):
    bootstrap = """
import os
import shutil
import sys
import types
sys.path.insert(0, 'tests')
import mock_anki
anki = mock_anki.install()
pkg = types.ModuleType('internpearls')
pkg.__path__ = [os.path.abspath('internpearls')]
sys.modules['internpearls'] = pkg
shutil.which = lambda *a, **kw: None
"""
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(bootstrap) + textwrap.dedent(script),
         *args],
        cwd=os.path.join(os.path.dirname(__file__), ".."),
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_addon_startup_defers_menu_only_modules():
    _run_menu_probe("""
        mock_anki.load_addon_init()
        deferred = ('ai_dialog', 'ai_setup', 'ai_cli', 'ai_fetch',
                    'dupes_dialog', 'dialogs')
        assert not [name for name in deferred
                    if 'internpearls.' + name in sys.modules]
        from aqt import gui_hooks
        assert gui_hooks.card_will_show.count() == 1
        assert gui_hooks.webview_will_set_content.count() == 1
        assert gui_hooks.state_did_undo.count() == 1
        assert gui_hooks.profile_will_close.count() == 1
        assert gui_hooks.main_window_did_init.count() == 2
        assert gui_hooks.profile_did_open.count() == 1
    """)


@pytest.mark.parametrize("module, label, title", [
    ("ai_dialog", "Generate cards (AI)", "Generate cards with AI"),
    ("dupes_dialog", "Scan for duplicates", "Scan for duplicates"),
    ("dialogs", "Settings", "Settings"),
])
def test_menu_click_imports_and_opens_dialog(module, label, title):
    _run_menu_probe("""
        module, label, title = sys.argv[1:]
        menu = mock_anki.load_addon_init()
        assert 'internpearls.' + module not in sys.modules
        items = []
        for node in menu.tree():
            items.extend(node['items'] if node['t'] == 'menu' else [node])
        action = next(node for node in items if node.get('label') == label)
        anki.gui.interactive = True
        try:
            mock_anki.trigger_action(action['id'])
        except mock_anki.NeedInteraction as needed:
            assert needed.payload['kind'] == 'dialog'
            assert title in needed.payload['title']
        else:
            raise AssertionError('menu action did not open a dialog')
        assert 'internpearls.' + module in sys.modules
        if module == 'ai_dialog':
            assert 'internpearls.ai_setup' in sys.modules
            assert 'internpearls.ai_cli' in sys.modules
            assert 'internpearls.ai_fetch' in sys.modules
    """, module, label, title)


def test_lazy_menu_import_error_shows_plain_warning():
    _run_menu_probe("""
        menu = mock_anki.load_addon_init()
        sys.modules.pop('internpearls.ai_dialog', None)
        class BrokenImport:
            def find_spec(self, fullname, path=None, target=None):
                if fullname == 'internpearls.ai_dialog':
                    raise ImportError('<unavailable> & missing')
        sys.meta_path.insert(0, BrokenImport())
        experimental = next(node for node in menu.tree()
                            if node.get('label') == 'Experimental')
        action = next(node for node in experimental['items']
                      if node.get('label') == 'Generate cards (AI)')
        try:
            mock_anki.trigger_action(action['id'])
        except mock_anki.NeedInteraction:
            pass
        assert len(anki.gui.warnings) == 1
        assert ('Something went wrong: &lt;unavailable&gt; &amp; missing'
                in anki.gui.warnings[0])
        assert '<unavailable>' not in anki.gui.warnings[0]
    """)


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
    submenus = {n["label"]: n for n in tree if n["t"] == "menu"}
    assert list(submenus) == ["Advanced", "Experimental"]
    adv_labels = [n["label"] for n in submenus["Advanced"]["items"] if n["t"] == "item"]
    assert adv_labels == [
        "Sync decks", "Reconcile my decks", "Import single deck (manual)",
        "Recent card feedback",
        "Clean up duplicate cards", "Remove empty cards", "Fix note types",
        "Backup intern pearls deck",
        "Restore intern pearls deck", "Export intern pearls deck",
        "Backup full collection", "Restore full collection",
        "Check for add-on updates"]
    exp_labels = [n["label"] for n in submenus["Experimental"]["items"] if n["t"] == "item"]
    assert exp_labels == ["Generate cards (AI)", "Night mode dimming",
                         "Scan for duplicates"]
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
@pytest.mark.parametrize("button", ["Save", "Save and update now"])
@pytest.mark.parametrize("entered, messages", [
    ("Note", ["No field is called 'Note'. Did you mean 'Notes'?"]),
    ("Note,  Dossing", ["No field is called 'Note'. Did you mean 'Notes'?",
                        "No field is called 'Dossing'. Did you mean 'Dosing'?"]),
    ("<zzzzzz>", ["No field is called '<zzzzzz>'."]),
])
def test_manage_decks_refuses_unknown_preserved_fields(
        anki, tmp_path, monkeypatch, button, entered, messages):
    from internpearls import dialogs
    original = {"decks_dir": _write_source(tmp_path), "protected_fields": ["Notes"]}
    anki.mw._config = dict(original)
    monkeypatch.setattr(dialogs, "update_decks", lambda: pytest.fail("updated decks"))
    warning_formats = []
    warn = dialogs._warn
    def record_warning(text, **kw):
        warning_formats.append(kw.get("textFormat"))
        return warn(text, **kw)
    monkeypatch.setattr(dialogs, "_warn", record_warning)
    reopened = []

    def respond(p):
        if p["kind"] == "warn":
            assert p["text"].splitlines() == messages
            assert anki.mw._config == original
            return {}
        assert p["kind"] == "dialog", "invalid names must return to Manage decks"
        fields = find(p["tree"], t="line")
        check = find(p["tree"], t="check")
        if not anki.gui.warnings:
            save = find(p["tree"], t="button", label=button)
            return {"events": [{"id": fields["id"], "value": entered},
                               {"id": check["id"], "value": False},
                               {"id": save["id"], "click": True}]}
        reopened.append((fields["value"], check["checked"]))
        cancel = find(p["tree"], t="button", label="Cancel")
        return {"events": [{"id": cancel["id"], "click": True}]}

    drive(anki, dialogs.manage_decks, respond)
    assert anki.mw._config == original
    assert reopened == [(entered, False)]
    assert warning_formats and all(fmt == "plain" for fmt in warning_formats)


@pytest.mark.parametrize("entered, expected", [
    ("Notes, Extra", ["Notes", "Extra"]),
    ("notes, dosing", ["notes", "dosing"]),
    ("Notes, Prompt", ["Notes", "Prompt"]),
])
def test_manage_decks_saves_known_preserved_fields(anki, tmp_path, entered, expected):
    from internpearls import dialogs
    anki.mw._config = {"decks_dir": _write_source(tmp_path)}
    anki.col.models._models = [mock_anki.make_model("Custom", fields=["Extra"])]
    def respond(p):
        if p["kind"] == "dialog":
            fields = find(p["tree"], t="line")
            save = find(p["tree"], t="button", label="Save")
            return {"events": [{"id": fields["id"], "value": entered},
                               {"id": save["id"], "click": True}]}
        assert p["kind"] == "info"
        return {}
    drive(anki, dialogs.manage_decks, respond)
    assert anki.mw._config["protected_fields"] == expected
    assert not anki.gui.asks
    assert not anki.gui.warnings


@pytest.mark.parametrize("collection_open", [True, False])
@pytest.mark.parametrize("stop", [True, False])
def test_manage_decks_confirms_stopping_notes_protection(
        anki, tmp_path, collection_open, stop):
    from internpearls import dialogs
    original = {"decks_dir": _write_source(tmp_path), "protected_fields": ["notes"]}
    anki.mw._config = dict(original)
    if not collection_open:
        anki.mw.col = None
    reopened = []
    def respond(p):
        if p["kind"] == "ask":
            assert "Notes will no longer be kept through imports" in p["text"]
            assert "Stop keeping Notes?" in p["text"]
            assert p["buttons"] == ["Stop keeping Notes", "Keep protecting Notes"]
            assert anki.gui.ask_defaults[-1] == "Keep protecting Notes"
            assert anki.mw._config == original
            return {"answer": stop}
        if p["kind"] == "info":
            assert stop
            return {}
        assert p["kind"] == "dialog"
        fields = find(p["tree"], t="line")
        check = find(p["tree"], t="check")
        if not anki.gui.asks:
            save = find(p["tree"], t="button", label="Save")
            return {"events": [{"id": fields["id"], "value": ""},
                               {"id": check["id"], "value": False},
                               {"id": save["id"], "click": True}]}
        reopened.append((fields["value"], check["checked"]))
        cancel = find(p["tree"], t="button", label="Cancel")
        return {"events": [{"id": cancel["id"], "click": True}]}
    drive(anki, dialogs.manage_decks, respond)
    assert anki.gui.asks
    if stop:
        assert anki.mw._config["protected_fields"] == []
        assert anki.mw._config["excluded_decks"] == ["Intern Pearls::Intern Custom::Pharm"]
    else:
        assert anki.mw._config == original
        assert reopened == [("", False)]


def test_manage_decks_skips_field_validation_without_a_collection(anki, tmp_path):
    from internpearls import dialogs
    anki.mw._config = {"decks_dir": _write_source(tmp_path)}
    anki.mw.col = None
    def respond(p):
        if p["kind"] == "dialog":
            fields = find(p["tree"], t="line")
            save = find(p["tree"], t="button", label="Save")
            return {"events": [{"id": fields["id"], "value": "Notes, Unknown"},
                               {"id": save["id"], "click": True}]}
        assert p["kind"] == "info"
        return {}
    drive(anki, dialogs.manage_decks, respond)
    assert anki.mw._config["protected_fields"] == ["Notes", "Unknown"]
    assert not anki.gui.warnings


def test_manage_decks_exclude_and_save(anki, tmp_path):
    from internpearls import dialogs, widgets
    anki.mw._config = {"decks_dir": _write_source(tmp_path)}
    anki.gui.interactive = True

    def respond(p):
        if p["kind"] == "dialog":
            contract_tree = p["contract_tree"]
            contract_nodes = _contract_nodes(contract_tree)
            assert contract_tree["root_id"] in contract_nodes
            assert all(node["kind"] in mock_anki.NODE_KINDS
                       for node in contract_nodes.values())
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


def test_manage_decks_save_summary_says_this_save_pulled_nothing(anki, tmp_path):
    """"Nothing pulled yet, run Update my decks..." reads as "you have never pulled",
    when it only ever meant this particular Save. A long-synced collection saw the
    same line."""
    from internpearls import dialogs
    anki.mw._config = {"decks_dir": _write_source(tmp_path)}
    anki.gui.interactive = True

    def respond(p):
        if p["kind"] == "dialog":
            save = find(p["tree"], t="button", label="Save")
            return {"events": [{"id": save["id"], "click": True}]}
        assert p["kind"] == "info"
        assert "Nothing pulled yet" not in p["text"]
        assert "nothing was pulled" in p["text"]
        return {}

    drive(anki, dialogs.manage_decks, respond)


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
                               "enabled": True, "state": state}, state)
        return [n for n in walk(row.node()) if n.get("t") == "label"]

    texts = {state: [n["text"] for n in labels(state)]
             for state in ("new", "update", "current")}
    assert texts["new"] == ["4 cards", widgets.CHIPS["new"]]
    assert texts["update"] == ["4 cards", widgets.CHIPS["changed"]]
    assert texts["current"] == ["4 cards · Up to date"], "the resting row takes no chip"
    trailing = next(n for n in labels("current") if n["text"].startswith("4 cards"))
    assert palette.colors()["muted"] in trailing["style"]


def _bare_deck_row(name, label):
    """One deck row's checkbox node, built off a bare dialog instance the way the row
    test above does, so a label rule can be checked without a source fetch."""
    from internpearls import dialogs
    panel = dialogs._DeckManagerDialog.__new__(dialogs._DeckManagerDialog)
    panel._checks = {}
    row = panel._deck_row({"name": name, "short": name.split("::")[-1], "cards": 1,
                           "enabled": True, "state": "current"}, label)
    return find(row.node(), t="check")


def test_deck_labels_stay_leaf_names_until_two_decks_share_one():
    """Prefixing every row with its parent path to cover the rare collision would cost
    the common case its readable list of names."""
    from internpearls import dialogs
    rows = [{"name": "Cardiology::Basics"}, {"name": "Renal::Physiology"}]
    assert dialogs._deck_labels(rows) == ["Basics", "Physiology"]
    rows.append({"name": "Renal::Basics"})
    assert dialogs._deck_labels(rows) == ["Cardiology::Basics", "Physiology",
                                          "Renal::Basics"]


def test_a_disambiguated_deck_label_takes_only_as_much_path_as_it_needs():
    from internpearls import dialogs
    rows = [{"name": "A::Blocks::Basics"}, {"name": "B::Blocks::Basics"},
            {"name": "A::Airway::Basics"}]
    assert dialogs._deck_labels(rows) == ["A::Blocks::Basics", "B::Blocks::Basics",
                                          "Airway::Basics"]


def test_a_deck_row_carries_its_full_path_as_a_tooltip():
    """Whatever the row ended up showing, the deck is named exactly somewhere: the leaf
    alone is ambiguous and a long label is elided."""
    from internpearls.logic import plain_text
    check = _bare_deck_row("Intern Pearls::Intern Custom::Pharm", "Pharm")
    assert check["label"] == "Pharm"
    assert plain_text(check["tooltip"]) == "Intern Pearls::Intern Custom::Pharm"


def test_a_very_long_deck_label_is_elided_rather_than_widening_the_dialog():
    from internpearls import dialogs
    from internpearls.logic import plain_text
    long_name = "Example Group::" + "A long subdeck name that runs on in detail"
    check = _bare_deck_row(long_name, long_name)
    assert len(check["label"]) <= dialogs._DECK_LABEL_MAX
    assert "…" in check["label"]
    # Elided from the middle: both ends carry meaning, the parent that disambiguates it
    # and the deck's own name.
    assert check["label"].startswith("Example Group::") and check["label"].endswith("detail")
    assert plain_text(check["tooltip"]) == long_name


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


def test_manage_decks_calls_a_broken_source_an_error_and_still_offers_to_change_it(
        anki, tmp_path):
    """A configured source that won't load is still configured. It used to read in the
    same muted grey as a working source's name (so "error: …" looked like what the
    source was called) under a button offering to "Configure source", as though nothing
    had ever been set up.
    """
    from internpearls import dialogs, palette
    anki.mw._config = {"decks_dir": str(tmp_path / "not-there")}
    anki.gui.interactive = True

    def respond(p):
        assert p["kind"] == "dialog"
        source_label = next(n for n in walk(p["tree"])
                            if n.get("t") == "label"
                            and (n.get("text") or "").startswith("Source:"))
        assert "error:" in source_label["text"]
        assert palette.colors()["warning"] in source_label["style"]
        assert find(p["tree"], t="button", label="Change source")
        assert not find(p["tree"], t="button", label="Configure source")
        cancel = find(p["tree"], t="button", label="Cancel")
        return {"events": [{"id": cancel["id"], "click": True}]}

    drive(anki, dialogs.manage_decks, respond)


def test_manage_decks_offers_to_configure_a_source_when_none_is_set(anki):
    """The other half of the rule above: nothing configured is the one case that reads
    as a setup step, and its line is the ordinary muted value, not an error."""
    from internpearls import dialogs, palette

    anki.gui.interactive = True

    def respond(p):
        assert p["kind"] == "dialog"
        source_label = next(n for n in walk(p["tree"])
                            if n.get("t") == "label"
                            and (n.get("text") or "").startswith("Source:"))
        assert source_label["text"] == "Source: not configured"
        assert palette.colors()["muted"] in source_label["style"]
        assert find(p["tree"], t="button", label="Configure source")
        cancel = find(p["tree"], t="button", label="Cancel")
        return {"events": [{"id": cancel["id"], "click": True}]}

    drive(anki, dialogs.manage_decks, respond)


def test_change_source_keeps_the_edits_made_before_it(anki, tmp_path):
    """Changing where decks come from is not a decision to discard the ticks and fields
    already edited: the reopened dialog used to come back at the saved config, silently
    losing them."""
    from internpearls import dialogs
    anki.mw._config = {"decks_dir": _write_source(tmp_path)}
    anki.col.models.add_field(anki.col.models.all()[0], {"name": "Extra"})
    anki.gui.interactive = True
    seen = []

    def respond(p):
        if p["kind"] != "dialog":
            return {}   # the saved-settings info box at the end
        if find(p["tree"], t="button", label="Try the example deck"):
            return _pick_source(p["tree"], "Cancel")   # leave the source as it was
        check = find(p["tree"], t="check")
        fields = find(p["tree"], t="line")
        seen.append((check["checked"], fields["value"]))
        if len(seen) == 1:
            change = find(p["tree"], t="button", label="Change source")
            return {"events": [{"id": check["id"], "value": False},
                               {"id": fields["id"], "value": "Notes, Extra"},
                               {"id": change["id"], "click": True}]}
        save = find(p["tree"], t="button", label="Save")
        return {"events": [{"id": save["id"], "click": True}]}

    drive(anki, dialogs.manage_decks, respond)
    assert seen[0] == (True, "Notes")
    assert seen[1] == (False, "Notes, Extra"), \
        "the reopened dialog came back at the saved config, dropping the edits"
    cfg = anki.mw._config
    assert cfg["excluded_decks"] == ["Intern Pearls::Intern Custom::Pharm"]
    assert cfg["protected_fields"] == ["Notes", "Extra"]


def test_a_source_that_renders_no_decks_cannot_zero_the_saved_exclusions(anki, tmp_path):
    """The dialog's checkbox map is empty when nothing rendered, and Save used to write
    that straight over excluded_decks: opening Manage decks while a source was broken
    silently un-excluded every deck, so the next update re-imported decks that had been
    opted out of."""
    from internpearls import dialogs
    excluded = ["Intern Pearls::Intern Custom::Pharm"]
    anki.mw._config = {"decks_dir": str(tmp_path / "not-there"),
                       "excluded_decks": list(excluded)}
    anki.gui.interactive = True

    def respond(p):
        if p["kind"] != "dialog":
            return {}   # the "no decks available" info box at the end
        assert not find(p["tree"], t="check"), "a broken source renders no deck rows"
        save = find(p["tree"], t="button", label="Save")
        return {"events": [{"id": save["id"], "click": True}]}

    drive(anki, dialogs.manage_decks, respond)
    assert anki.mw._config["excluded_decks"] == excluded


def test_fixing_a_broken_source_through_change_source_keeps_the_exclusions(
        anki, tmp_path):
    """The reproduced regression, end to end: a broken source is the main reason to
    click Change source, and the carry that keeps the ticks across the reopen was
    capturing the empty state of a dialog that had rendered no decks. Fixing the source
    therefore ended with every exclusion gone, and "Save and update now" would have
    re-imported the opted-out deck immediately."""
    from internpearls import dialogs
    pharm = "Intern Pearls::Intern Custom::Pharm"
    anki.mw._config = {"decks_dir": str(tmp_path / "not-there"),
                       "excluded_decks": [pharm]}
    anki.gui.interactive = True
    good = _write_source(tmp_path)
    rows_seen = []

    def respond(p):
        if p["kind"] == "prompt":
            return {"text": good, "ok": True}   # the folder picker, now pointed at a
        if p["kind"] != "dialog":               # source that actually loads
            return {}
        if find(p["tree"], t="button", label="Try the example deck"):
            return _pick_source(p["tree"], "Local folder")
        row = find(p["tree"], t="check")
        if row is None:
            change = find(p["tree"], t="button", label="Change source")
            return {"events": [{"id": change["id"], "click": True}]}
        rows_seen.append(row["checked"])
        save = find(p["tree"], t="button", label="Save")
        return {"events": [{"id": save["id"], "click": True}]}

    drive(anki, dialogs.manage_decks, respond)
    assert rows_seen == [False], \
        "the deck comes back ticked, so its opt-out was lost while the source was broken"
    assert anki.mw._config["excluded_decks"] == [pharm]


def test_saving_keeps_an_exclusion_for_a_deck_the_current_source_never_offered(
        anki, tmp_path):
    """A rendered list only knows the decks its own source publishes, so writing it out
    as the whole truth drops the opt-outs belonging to any other deck. Preserved rather
    than dropped: a deck that has genuinely gone leaves a name matching nothing, which
    excludes nothing, while dropping it lets a deck that was missing for one fetch come
    back ticked."""
    from internpearls import dialogs
    pharm = "Intern Pearls::Intern Custom::Pharm"
    elsewhere = "Some Other Source::Neuro"
    anki.mw._config = {"decks_dir": _write_source(tmp_path),
                       "excluded_decks": [elsewhere, pharm]}
    anki.gui.interactive = True

    def respond(p):
        if p["kind"] != "dialog":
            return {}
        row = find(p["tree"], t="check")
        assert row and not row["checked"], "the excluded deck must render unticked"
        save = find(p["tree"], t="button", label="Save")
        return {"events": [{"id": save["id"], "click": True}]}

    drive(anki, dialogs.manage_decks, respond)
    assert sorted(anki.mw._config["excluded_decks"]) == sorted([elsewhere, pharm])


def test_stale_exclusions_show_a_muted_line_with_a_clear_link(anki, tmp_path):
    """A deck the current source doesn't offer stays excluded forever with no UI
    showing it (the preservation above is correct; this is the visibility gap). Pharm
    IS offered, so it renders as an unticked row, not a stale name; "elsewhere" is
    what the line has to name."""
    from internpearls import dialogs
    pharm = "Intern Pearls::Intern Custom::Pharm"
    elsewhere = "Some Other Source::Neuro"
    anki.mw._config = {"decks_dir": _write_source(tmp_path),
                       "excluded_decks": [elsewhere, pharm]}
    anki.gui.interactive = True

    def respond(p):
        if p["kind"] != "dialog":
            return {}
        assert find(p["tree"], t="label",
                    text=f"Also excluded, not offered by this source: {elsewhere}")
        assert find(p["tree"], t="button", label="Clear")
        cancel = find(p["tree"], t="button", label="Cancel")
        return {"events": [{"id": cancel["id"], "click": True}]}

    drive(anki, dialogs.manage_decks, respond)


def test_no_stale_line_when_every_exclusion_is_still_offered(anki, tmp_path):
    from internpearls import dialogs
    anki.mw._config = {"decks_dir": _write_source(tmp_path)}
    anki.gui.interactive = True

    def respond(p):
        if p["kind"] != "dialog":
            return {}
        assert not find(p["tree"], t="button", label="Clear")
        cancel = find(p["tree"], t="button", label="Cancel")
        return {"events": [{"id": cancel["id"], "click": True}]}

    drive(anki, dialogs.manage_decks, respond)


def test_a_broken_source_shows_no_stale_line_even_though_it_renders_no_rows(
        anki, tmp_path):
    """A source that never loaded is not the same claim as "every deck went stale":
    that would tell a reader every excluded deck vanished when really nothing was
    ever fetched to check against."""
    from internpearls import dialogs
    anki.mw._config = {"decks_dir": str(tmp_path / "not-there"),
                       "excluded_decks": ["Intern Pearls::Intern Custom::Pharm"]}
    anki.gui.interactive = True

    def respond(p):
        if p["kind"] != "dialog":
            return {}
        assert not find(p["tree"], t="button", label="Clear")
        cancel = find(p["tree"], t="button", label="Cancel")
        return {"events": [{"id": cancel["id"], "click": True}]}

    drive(anki, dialogs.manage_decks, respond)


def test_clearing_stale_exclusions_removes_exactly_those_on_save(anki, tmp_path):
    from internpearls import dialogs
    pharm = "Intern Pearls::Intern Custom::Pharm"
    elsewhere = "Some Other Source::Neuro"
    anki.mw._config = {"decks_dir": _write_source(tmp_path),
                       "excluded_decks": [elsewhere, pharm]}
    anki.gui.interactive = True

    def respond(p):
        if p["kind"] != "dialog":
            return {}
        clear = find(p["tree"], t="button", label="Clear")
        save = find(p["tree"], t="button", label="Save")
        return {"events": [{"id": clear["id"], "click": True},
                           {"id": save["id"], "click": True}]}

    drive(anki, dialogs.manage_decks, respond)
    # pharm stays excluded (it's an offered, unticked row); elsewhere is cleared.
    assert anki.mw._config["excluded_decks"] == [pharm]


def test_clearing_stale_exclusions_retires_the_link_and_says_it_waits_for_save(
        anki, tmp_path):
    """Clear used to empty the line and leave its link standing with nothing left to
    clear, while the un-exclusion itself only lands on Save (see excluded_decks), so
    Cancel silently discarded what the screen had shown as already done."""
    from internpearls import dialogs
    elsewhere = "Some Other Source::Neuro"
    anki.mw._config = {"decks_dir": _write_source(tmp_path),
                       "excluded_decks": [elsewhere]}
    anki.gui.interactive = True
    seen = []

    def respond(p):
        if p["kind"] != "dialog":
            return {}
        clear = find(p["tree"], t="button", label="Clear", visible=True)
        if clear:
            return {"events": [{"id": clear["id"], "click": True}]}
        seen.append(p["tree"])
        assert find(p["tree"], t="label", text=f"Cleared when you save: {elsewhere}"), (
            "the line must say the clearing is pending, not show it as done")
        cancel = find(p["tree"], t="button", label="Cancel")
        return {"events": [{"id": cancel["id"], "click": True}]}

    drive(anki, dialogs.manage_decks, respond)
    assert seen, "the Clear link was still offered after it had been used"
    # Cancelled, so the pending clear never landed: the exclusion is still there.
    assert anki.mw._config["excluded_decks"] == [elsewhere]


def test_saving_without_clearing_still_preserves_a_stale_exclusion(anki, tmp_path):
    """The existing merge behavior must survive this change untouched: Save alone,
    with the Clear link never clicked, keeps a stale exclusion exactly as before."""
    from internpearls import dialogs
    pharm = "Intern Pearls::Intern Custom::Pharm"
    elsewhere = "Some Other Source::Neuro"
    anki.mw._config = {"decks_dir": _write_source(tmp_path),
                       "excluded_decks": [elsewhere, pharm]}
    anki.gui.interactive = True

    def respond(p):
        if p["kind"] != "dialog":
            return {}
        assert find(p["tree"], t="button", label="Clear"), \
            "the stale line should be offered even if this run never uses it"
        save = find(p["tree"], t="button", label="Save")
        return {"events": [{"id": save["id"], "click": True}]}

    drive(anki, dialogs.manage_decks, respond)
    assert sorted(anki.mw._config["excluded_decks"]) == sorted([elsewhere, pharm])


def test_the_select_links_are_offered_only_where_there_is_something_to_select(
        anki, tmp_path):
    """Above "No decks available yet" the two links are inert: they read as controls
    beside the one button that empty state actually offers."""
    from internpearls import dialogs
    anki.gui.interactive = True
    found = []

    def respond(p):
        if p["kind"] != "dialog":
            return {}
        found.append(bool(find(p["tree"], t="button", label="Select all")
                          and find(p["tree"], t="button", label="Select none")))
        cancel = find(p["tree"], t="button", label="Cancel")
        return {"events": [{"id": cancel["id"], "click": True}]}

    anki.mw._config = {"decks_dir": _write_source(tmp_path)}
    drive(anki, dialogs.manage_decks, respond)
    anki.mw._config = {"decks_dir": str(tmp_path / "not-there")}
    drive(anki, dialogs.manage_decks, respond)
    assert found == [True, False]


def test_a_deck_label_falls_back_to_the_character_cap_without_font_metrics():
    """What a row draws is elided by pixels, measured in the font it is painted in
    (qt_tests measures that one). The mock Qt has no font engine at all, so the
    character cap is what answers here, and it stays a plain-string helper for exactly
    that reason."""
    from internpearls import dialogs
    long_name = "Example Group::" + "A long subdeck name that runs on in detail"
    assert dialogs._fit_label(long_name) == dialogs._elide(long_name)
    assert dialogs._fit_label("Pharm") == "Pharm"


# ------------------------------------------------------------------ dialog lifetime
def test_a_finished_flow_releases_every_dialog_it_opened(anki, tmp_path):
    """Every dialog here is built with mw as its parent, so Qt owns it for the rest of
    the session unless something asks for it to go: an update screen's thousands of card
    rows and decoded pictures accumulate per run otherwise. Each wrapper releases its own
    dialog once the state on it has been read.
    """
    from internpearls import dialogs
    anki.mw._config = {"decks_dir": _write_source(tmp_path)}
    anki.gui.interactive = True

    def respond(p):
        if p["kind"] == "dialog":
            btn = (find(p["tree"], t="button", label="Save and update now")
                   or find(p["tree"], t="button", label="Update")
                   or find(p["tree"], t="button", label="OK"))
            return {"events": [{"id": btn["id"], "click": True}]}
        return {}

    drive(anki, dialogs.manage_decks, respond)
    opened = [w for w in mock_anki._widgets.values()
              if isinstance(w, mock_anki.QDialog)]
    assert opened, "the flow opened no dialog at all"
    assert all(d.deleted for d in opened), (
        "a dialog was left parented to mw with nothing ever asking for it to go: "
        + ", ".join(sorted({d._title for d in opened if not d.deleted})))


# ------------------------------------------------------------------ declined cards
def _snapshot_declined_dialog(anki):
    """Open Declined cards, capture its first rendered tree, then close it."""
    from internpearls import dialogs
    anki.gui.interactive = True
    captured = {}

    def respond(p):
        assert p["kind"] == "dialog"
        captured.setdefault("tree", p["tree"])
        close = find(p["tree"], t="button", label="Close")
        return {"events": [{"id": close["id"], "click": True}]}

    drive(anki, dialogs.open_declined_cards, respond)
    return captured["tree"]


def _snapshot_manage_decks(anki):
    """Open Manage decks, capture its first rendered tree, then cancel out."""
    from internpearls import dialogs
    anki.gui.interactive = True
    captured = {}

    def respond(p):
        assert p["kind"] == "dialog"
        captured.setdefault("tree", p["tree"])
        cancel = find(p["tree"], t="button", label="Cancel")
        return {"events": [{"id": cancel["id"], "click": True}]}

    drive(anki, dialogs.manage_decks, respond)
    return captured["tree"]


def _drive_declined_offer_again(anki, name):
    """Open Declined cards, click the Offer again button for the row named `name`
    (its front, or its guid when the entry has no readable front), then close."""
    from internpearls import dialogs
    anki.gui.interactive = True
    clicked = {"done": False}

    def respond(p):
        assert p["kind"] == "dialog"
        if not clicked["done"]:
            clicked["done"] = True
            btn = find(p["tree"], t="button", label="Offer again",
                       accessible=f"Offer again: {name}")
            return {"events": [{"id": btn["id"], "click": True}]}
        close = find(p["tree"], t="button", label="Close")
        return {"events": [{"id": close["id"], "click": True}]}

    drive(anki, dialogs.open_declined_cards, respond)


def _write_installed(anki, data):
    from internpearls.config import INSTALLED, _save_json
    _save_json(INSTALLED, data)


def test_save_json_creates_a_missing_directory(tmp_path):
    """D: _save_json mkstemps a temp file into its target's own directory before
    the atomic os.replace, and mkstemp raises FileNotFoundError outright if that
    directory doesn't exist. config.py only creates user_files/ once, at import
    time, so this is reachable only if something removes it afterward: but the
    fix is one line and the failure mode was every state write in the add-on
    (installed.json, the feedback log, ai_usage.json, declined.json, the deck
    skill) crashing instead of just this one. Doesn't touch the target file
    itself beyond confirming a normal round trip still works."""
    from internpearls.config import _load_json, _save_json

    target = tmp_path / "missing" / "nested" / "state.json"
    assert not target.parent.exists()

    _save_json(str(target), {"a": 1})

    assert target.exists()
    assert _load_json(str(target), None) == {"a": 1}


def _all_text(tree):
    """Every "text" (QLabel) and "label" (QPushButton) string anywhere in the tree,
    joined into one blob so a caller can check substrings ("Declined" is part of a
    button labelled "Declined cards (1)") as well as whole strings."""
    out = []
    for n in walk(tree):
        for key in ("text", "label"):
            if n.get(key) is not None:
                out.append(n[key])
    return "\n".join(out)


def test_declined_dialog_lists_entries_grouped_by_state(anki):
    from internpearls import config
    config.save_declined({
        "g1": {"state": "never", "front": "front a", "deck": "IP::A",
               "decided": "2026-08-01", "hash": ""},
        "g2": {"state": "held", "front": "front b", "deck": "IP::A",
               "decided": "2026-08-02", "hash": ""}})
    tree = _snapshot_declined_dialog(anki)
    texts = _all_text(tree)
    assert "Never imported" in texts and "front a" in texts
    assert "Later" in texts.split("\n") and "front b" in texts
    assert "Skipped for now" not in texts and "Held for later" not in texts


def test_offer_again_removes_the_entry_and_invalidates_the_deck(anki):
    from internpearls import config, sync
    config.save_declined({
        "g1": {"state": "never", "front": "front a", "deck": "IP::A",
               "decided": "2026-08-01", "hash": ""}})
    _write_installed(anki, {"IP::A": "1.0"})
    _drive_declined_offer_again(anki, "front a")
    assert config.load_declined() == {}
    assert "IP::A" not in json.load(open(sync.INSTALLED))


def test_manage_decks_names_the_declined_count(anki):
    from internpearls import config
    config.save_declined({"g1": {"state": "skip", "front": "f", "deck": "IP::A",
                                 "decided": "2026-08-01", "hash": ""}})
    tree = _snapshot_manage_decks(anki)
    assert "Declined cards (1)" in _all_text(tree)


def test_manage_decks_declined_count_refreshes_after_the_dialog_closes(anki,
                                                                       monkeypatch):
    """The count is computed when Manage decks builds; Offer again inside the
    Declined cards dialog can shrink the registry while Manage decks stays open, so
    the button has to recompute its label when that dialog closes."""
    from internpearls import config, dialogs
    config.save_declined({"g1": {"state": "skip", "front": "front a", "deck": "IP::A",
                                 "decided": "2026-08-01", "hash": ""}})
    # Stands in for offering every card again inside the nested dialog.
    monkeypatch.setattr(dialogs, "open_declined_cards",
                        lambda: config.save_declined({}))
    anki.gui.interactive = True
    opened = {"clicked": False}

    def respond(p):
        tree = p["tree"]
        btn = find(tree, t="button", label="Declined cards (1)")
        if btn and not opened["clicked"]:
            opened["clicked"] = True
            return {"events": [{"id": btn["id"], "click": True}]}
        assert find(tree, t="button", label="Declined cards") is not None, (
            "the declined count did not refresh after the dialog closed")
        cancel = find(tree, t="button", label="Cancel")
        return {"events": [{"id": cancel["id"], "click": True}]}

    drive(anki, dialogs.manage_decks, respond)


def test_declined_dialog_empty_state(anki):
    tree = _snapshot_declined_dialog(anki)
    assert "You haven't declined any cards." in _all_text(tree)


def test_declined_dialog_degrades_a_malformed_entry_instead_of_crashing(anki):
    """A hand-edited declined.json can carry a garbage (non-dict) value, or a state
    this dialog doesn't otherwise group by. sync.py's own filter keeps a GUID declined
    by its presence in the registry alone, whatever its entry holds, so every entry
    must still render, under Other, with a working Offer again: a dropped one would
    leave no way back for that card at all. The non-dict entry has no front to show,
    so its row names it by its bare guid."""
    from internpearls import config
    config.save_declined({
        "g1": "not a dict",
        "g2": {"state": "future-state", "front": "front c", "deck": "IP::A",
               "decided": "2026-08-03", "hash": ""},
        "g3": {"state": "another-unknown", "front": "front d", "deck": "IP::A",
               "decided": "2026-08-04", "hash": ""}})
    tree = _snapshot_declined_dialog(anki)
    texts = _all_text(tree)
    assert "Other" in texts
    assert "front c" in texts and "front d" in texts
    assert "g1" in texts.split()
    assert "You haven't declined any cards." not in texts
    buttons = [n for n in walk(tree)
              if n.get("t") == "button" and n.get("label") == "Offer again"]
    assert len(buttons) == 3, "one Offer again per entry, the non-dict one included"


def test_a_garbage_only_registry_still_offers_a_way_back(anki):
    """A registry holding only non-dict values used to render an empty dialog with no
    rows and no empty-state line, under a Manage decks button still counting them."""
    from internpearls import config
    config.save_declined({"g-garbage": "not a dict"})
    tree = _snapshot_declined_dialog(anki)
    texts = _all_text(tree)
    assert "Other" in texts and "g-garbage" in texts
    assert "You haven't declined any cards." not in texts
    _drive_declined_offer_again(anki, "g-garbage")
    assert config.load_declined() == {}


def test_offer_again_on_an_other_row_removes_it_without_crashing(anki):
    from internpearls import config
    config.save_declined({
        "g1": {"state": "future-state", "front": "front c", "deck": "IP::A",
               "decided": "2026-08-03", "hash": ""}})
    _drive_declined_offer_again(anki, "front c")
    assert config.load_declined() == {}


def test_offer_again_on_an_entry_missing_a_deck_saves_without_invalidating(
        anki, monkeypatch):
    """A malformed entry can be missing its "deck" key entirely. Offer again must
    still remove and save it (the pop is real work regardless of what else is
    missing), but there is nothing to invalidate: invalidate_installed must not run
    at all, and certainly never with [None]."""
    from internpearls import config, dialogs
    calls = []
    monkeypatch.setattr(dialogs, "invalidate_installed",
                        lambda names=None: calls.append(names))
    config.save_declined({
        "g1": {"state": "skip", "front": "front e", "decided": "2026-08-05",
               "hash": ""}})
    _drive_declined_offer_again(anki, "front e")
    assert config.load_declined() == {}
    assert calls == [], f"invalidate_installed must not run here, got {calls}"


# --------------------------------------------------------------------- settings
def test_settings_interval_range_preserves_the_supported_weekly_limit(anki):
    """Opening and saving Settings must preserve both documented interval limits;
    in particular, a weekly value cannot be silently shortened to one day by Qt."""
    from internpearls import config, dialogs

    weekly = dialogs._SettingsDialog(
        None, True, config.AUTO_SYNC_INTERVAL_CEILING_MIN, True, False)
    spin = find(weekly.node(), t="spin")
    assert spin["min"] == config.AUTO_SYNC_INTERVAL_FLOOR_MIN
    assert spin["max"] == config.AUTO_SYNC_INTERVAL_CEILING_MIN
    assert weekly.values()["auto_sync_interval_minutes"] == \
        config.AUTO_SYNC_INTERVAL_CEILING_MIN

    minimum = dialogs._SettingsDialog(
        None, True, config.AUTO_SYNC_INTERVAL_FLOOR_MIN, True, False)
    assert minimum.values()["auto_sync_interval_minutes"] == \
        config.AUTO_SYNC_INTERVAL_FLOOR_MIN


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
def test_config_reads_a_non_finite_number_as_its_default(anki, bad):
    """Python's json parses Infinity and NaN from a hand-edited config.json; every
    numeric setting must read as its default rather than raise or leak through."""
    from internpearls import config

    anki.mw._config = {"auto_sync_interval_minutes": bad,
                       "dim_images_night_mode_percent": bad,
                       "ai_default_count": bad}
    cfg = config._cfg()
    assert cfg["auto_sync_interval_minutes"] == config.AUTO_SYNC_INTERVAL_DEFAULT_MIN
    assert cfg["dim_images_night_mode_percent"] == config.NIGHT_MODE_DIM_PERCENT_DEFAULT
    assert cfg["ai_default_count"] == 0


@pytest.mark.parametrize("key, result_key, default", [
    ("protected_fields", "protected", ["Notes"]),
    ("excluded_decks", "excluded", []),
    ("dupes_ignored", "dupes_ignored", []),
    ("dupes_excluded_decks", "dupes_excluded_decks", []),
])
@pytest.mark.parametrize("value, expected", [
    ("Notes", ["Notes"]),
    (["Notes", 5, False, None, {}, ["Other"], ""], ["Notes", ""]),
    (["Notes", "Other"], ["Notes", "Other"]),
    ([], []),
    (5, None), (False, None), (None, None), ({}, None), ("", None),
])
def test_config_reads_string_lists(anki, key, result_key, default, value, expected):
    from internpearls import config

    anki.mw._config = {key: value}
    assert config._cfg()[result_key] == (default if expected is None else expected)


@pytest.mark.parametrize("key, default", [
    ("notify_addon_updates", True), ("auto_update_addon", False),
    ("auto_sync_decks", False), ("dim_images_night_mode", False),
])
@pytest.mark.parametrize("value", ["false", "true", "", 0, 1, None, [], {}, True, False])
def test_config_reads_only_boolean_values(anki, key, default, value):
    from internpearls import config

    anki.mw._config = {key: value}
    expected = value if isinstance(value, bool) else default
    assert config._cfg()[key] is expected


@pytest.mark.parametrize("kind", ["claude", "codex", "agy"])
@pytest.mark.parametrize("value", ["false", "true", "", 0, 1, None, [], {}, True, False])
def test_config_reads_only_boolean_backend_values(anki, kind, value):
    from internpearls import config

    anki.mw._config = {"ai_backend_enabled": {kind: value}}
    expected = value if isinstance(value, bool) else True
    assert config._cfg()["ai_backend_enabled"][kind] is expected


@pytest.mark.parametrize("default", [True, False])
@pytest.mark.parametrize("value", ["false", "true", "", 0, 1, None, [], {}])
def test_ai_bool_map_uses_its_default_for_non_booleans(anki, default, value):
    from internpearls import config

    assert config._ai_bool_map({"claude": value}, default) == {
        "claude": default, "codex": default, "agy": default}


@pytest.mark.parametrize("key, result_key, default", [
    ("scope_tag", "scope_tag", "InternPearls"),
    ("decks_dir", "decks_dir", ""),
    ("github_decks_repo", "gh_repo", ""),
    ("github_ref", "gh_ref", "main"),
    ("github_token", "gh_token", ""),
    ("export_deck", "export_deck", "Intern Pearls::Intern Custom"),
    ("ai_backend", "ai_backend", ""),
])
@pytest.mark.parametrize("value", [5, False, None, [], {}, "", "  Custom  "])
def test_config_reads_only_string_values(anki, key, result_key, default, value):
    from internpearls import config

    anki.mw._config = {key: value, "ai_cli_path": "/example/cli"}
    expected = value if isinstance(value, str) else default
    assert config._cfg()[result_key] == expected


@pytest.mark.parametrize("key, index, default", [
    ("github_decks_repo", 0, ""), ("github_ref", 1, "main"),
    ("decks_dir", 2, ""), ("scope_tag", 3, "InternPearls"),
])
@pytest.mark.parametrize("value", [5, False, None, [], {}, "", "  Custom  "])
def test_source_identity_reads_only_string_values(anki, key, index, default, value):
    from internpearls import config

    anki.mw._config = {key: value}
    expected = value if isinstance(value, str) else default
    identity = config.source_identity()
    assert identity[index] == expected
    hash(identity)


def test_config_preserves_well_formed_values(anki):
    from internpearls import config

    anki.mw._config = {
        "protected_fields": ["Notes", "Why"], "scope_tag": "Custom",
        "decks_dir": "/example/decks", "github_decks_repo": "owner/decks",
        "github_ref": "release", "github_token": "", "export_deck": "Custom::Deck",
        "excluded_decks": ["Archive"], "notify_addon_updates": False,
        "auto_update_addon": True, "auto_sync_decks": True,
        "auto_sync_interval_minutes": 30, "dim_images_night_mode": True,
        "dim_images_night_mode_percent": 45, "dim_night_mode_scope": "content",
        "ai_backend": "codex",
        "ai_cli_path": {"claude": "", "codex": "/example/cli", "agy": ""},
        "ai_backend_enabled": {"claude": False, "codex": True, "agy": False},
        "ai_model": {"claude": "sonnet", "codex": "", "agy": ""},
        "ai_effort": {"claude": "high", "codex": "", "agy": ""},
        "ai_default_count": 7, "ai_default_depth": "quick",
        "dupes_ignored": ["pair"], "dupes_excluded_decks": ["Archive"],
        "dupes_threshold": 0.6,
    }
    expected = dict(anki.mw._config)
    for key, result_key in [("protected_fields", "protected"),
                            ("excluded_decks", "excluded"),
                            ("github_decks_repo", "gh_repo"),
                            ("github_ref", "gh_ref"), ("github_token", "gh_token")]:
        expected[result_key] = expected.pop(key)
    assert config._cfg() == expected


def test_settings_spin_controls_have_accessible_names(anki):
    from internpearls import dialogs

    settings = dialogs._SettingsDialog(None, True, 15, True, False)
    assert settings._interval_spin._accessible == "Check every"

    dimming = dialogs._NightModeDimmingDialog(None, True, 30, "images")
    assert dimming._percent_spin._accessible == "Dim by"


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


def test_settings_no_longer_offers_night_mode_dimming(anki):
    """Night mode dimming moved to its own Experimental dialog; Settings must not
    show its controls any more."""
    from internpearls import dialogs

    anki.gui.interactive = True

    def respond(p):
        if p["kind"] == "dialog":
            assert find(p["tree"], t="check",
                       label="Dim in Night Mode") is None
            cancel = find(p["tree"], t="button", label="Cancel")
            return {"events": [{"id": cancel["id"], "click": True}]}
        return {}

    drive(anki, dialogs.open_settings, respond)


# ------------------------------------------------------- night mode dimming
def test_night_mode_dimming_saves_toggle_and_percent(anki):
    from internpearls import dialogs

    anki.gui.interactive = True

    def respond(p):
        if p["kind"] == "dialog":
            dim = find(p["tree"], t="check", label="Dim in Night Mode")
            assert dim is not None
            spin = find(p["tree"], t="spin")
            # 30 is the fixed dim level every build applied before this became
            # configurable, so a fresh install's default must show exactly that.
            assert spin["value"] == 30 and spin["suffix"] == "%"
            save = find(p["tree"], t="button", label="Save")
            return {"events": [{"id": dim["id"], "value": True},
                               {"id": spin["id"], "value": 50},
                               {"id": save["id"], "click": True}]}
        assert "50%" in p["text"]
        return {}

    drive(anki, dialogs.open_night_mode_dimming, respond)
    cfg = anki.mw._config
    assert cfg["dim_images_night_mode"] is True
    assert cfg["dim_images_night_mode_percent"] == 50


def test_night_mode_dimming_percent_spinbox_follows_the_toggle(anki):
    """Nothing to dim by while the toggle is off, so the percent spinbox must not sit
    there editable and inert: same property Settings' own interval spinbox has."""
    from internpearls import dialogs
    anki.gui.interactive = True
    enabled = []

    def respond(p):
        if p["kind"] != "dialog":
            return {}
        dim = find(p["tree"], t="check", label="Dim in Night Mode")
        spin = find(p["tree"], t="spin")
        enabled.append(spin["enabled"])
        if len(enabled) < 3:
            return {"events": [{"id": dim["id"], "value": len(enabled) == 1}]}
        cancel = find(p["tree"], t="button", label="Cancel")
        return {"events": [{"id": cancel["id"], "click": True}]}

    drive(anki, dialogs.open_night_mode_dimming, respond)
    assert enabled == [False, True, False], (
        "the percent spinbox does not track the dim-images checkbox")


def test_night_mode_dimming_percent_range_matches_the_clamp(anki):
    """The spinbox's own range must not let a user pick a value the config layer would
    silently clamp right back down: see logic.clamp_night_mode_dim_percent."""
    from internpearls import config, dialogs
    anki.gui.interactive = True

    def respond(p):
        assert p["kind"] == "dialog"
        spin = find(p["tree"], t="spin")
        assert spin["min"] == config.NIGHT_MODE_DIM_PERCENT_FLOOR
        assert spin["max"] == config.NIGHT_MODE_DIM_PERCENT_CEILING
        cancel = find(p["tree"], t="button", label="Cancel")
        return {"events": [{"id": cancel["id"], "click": True}]}

    drive(anki, dialogs.open_night_mode_dimming, respond)


def test_dimming_dialog_scope_value(anki):
    """_NightModeDimmingDialog has no dedicated save method (open_night_mode_dimming
    reads dlg.values() after exec() and writes that into config itself), so this reads
    values() directly rather than the brief's guessed dlg._save()."""
    from internpearls import dialogs
    dlg = dialogs._NightModeDimmingDialog(anki.mw, True, 30, "images")
    dlg._scope_content.setChecked(True)
    assert dlg.values()["dim_night_mode_scope"] == "content"


def test_night_mode_dimming_saves_scope(anki):
    from internpearls import dialogs
    anki.gui.interactive = True

    def respond(p):
        if p["kind"] == "dialog":
            enabled = find(p["tree"], t="check",
                           label="Dim in Night Mode")
            content = find(p["tree"], t="radio",
                          label="Everything on cards and deck screens")
            assert content is not None
            save = find(p["tree"], t="button", label="Save")
            return {"events": [{"id": enabled["id"], "value": True},
                               {"id": content["id"], "value": True},
                               {"id": save["id"], "click": True}]}
        return {}

    drive(anki, dialogs.open_night_mode_dimming, respond)
    assert anki.mw._config["dim_night_mode_scope"] == "content"


def test_night_mode_dimming_confirmation_describes_saved_scope(anki):
    """The confirmation message after saving must describe the scope that was actually
    saved: 'Cards and deck screens...' for scope content, 'Bright images...' for images."""
    from internpearls import dialogs

    # Test saving with content scope
    anki.gui.interactive = True

    def respond_content(p):
        if p["kind"] == "dialog":
            dim = find(p["tree"], t="check", label="Dim in Night Mode")
            spin = find(p["tree"], t="spin")
            content = find(p["tree"], t="radio",
                          label="Everything on cards and deck screens")
            save = find(p["tree"], t="button", label="Save")
            return {"events": [{"id": dim["id"], "value": True},
                               {"id": spin["id"], "value": 45},
                               {"id": content["id"], "value": True},
                               {"id": save["id"], "click": True}]}
        assert "Cards and deck screens" in p["text"]
        return {}

    drive(anki, dialogs.open_night_mode_dimming, respond_content)

    # Test saving with images scope
    anki.gui.interactive = True

    def respond_images(p):
        if p["kind"] == "dialog":
            dim = find(p["tree"], t="check", label="Dim in Night Mode")
            spin = find(p["tree"], t="spin")
            images = find(p["tree"], t="radio", label="Bright images only")
            save = find(p["tree"], t="button", label="Save")
            return {"events": [{"id": dim["id"], "value": True},
                               {"id": spin["id"], "value": 55},
                               {"id": images["id"], "value": True},
                               {"id": save["id"], "click": True}]}
        assert "Bright images" in p["text"]
        return {}

    drive(anki, dialogs.open_night_mode_dimming, respond_images)


def test_night_mode_dimming_confirmation_when_off(anki):
    """Saving with the toggle left off must say so in the dialog's own name,
    not the old per-images wording ("Night Mode image dimming is off.") that
    predates the content scope."""
    from internpearls import dialogs

    anki.gui.interactive = True

    def respond(p):
        if p["kind"] == "dialog":
            save = find(p["tree"], t="button", label="Save")
            return {"events": [{"id": save["id"], "click": True}]}
        assert "Night mode dimming is off." in p["text"]
        return {}

    drive(anki, dialogs.open_night_mode_dimming, respond)


def test_the_interval_spinbox_follows_the_auto_sync_checkbox(anki):
    """Nothing checks on an interval while auto-sync is off, so the control that sets one
    must not sit there editable and inert. Driven across several rounds of the same
    dialog (a response with no click leaves it open), which is what shows the link is
    live rather than only right at build time.
    """
    from internpearls import dialogs
    anki.gui.interactive = True
    enabled = []

    def respond(p):
        if p["kind"] != "dialog":
            return {}   # the saved-settings info box at the end
        auto = find(p["tree"], t="check",
                    label="Sync decks automatically when updates are available")
        spin = find(p["tree"], t="spin")
        enabled.append(spin["enabled"])
        if len(enabled) < 3:
            return {"events": [{"id": auto["id"], "value": len(enabled) == 1}]}
        save = find(p["tree"], t="button", label="Save")
        return {"events": [{"id": save["id"], "click": True}]}

    drive(anki, dialogs.open_settings, respond)
    assert enabled == [False, True, False], (
        "the interval spinbox does not track the auto-sync checkbox")


def test_the_auto_sync_hint_says_a_template_change_still_waits_for_a_manual_run(anki):
    """The one thing auto-sync never applies unattended is a card-template change, which
    the README treats as the toggle's key safety property. A hint promising only that
    changed decks apply without asking leaves that out."""
    from internpearls import dialogs
    anki.gui.interactive = True

    def respond(p):
        assert p["kind"] == "dialog"
        text = " ".join(n.get("text") or "" for n in walk(p["tree"])
                        if n.get("t") == "label")
        assert "held back for a manual run" in text
        assert "backup is still taken first" in text
        # Both held-back kinds by name: a note-type format conversion bumps the schema
        # exactly as a template change does and is deferred with it (sync._run_sync),
        # so a hint naming only the template describes half of what waits.
        assert "card template" in text and "note-type format" in text
        cancel = find(p["tree"], t="button", label="Cancel")
        return {"events": [{"id": cancel["id"], "click": True}]}

    drive(anki, dialogs.open_settings, respond)


def test_saving_manual_deck_sync_steers_to_update_my_decks(anki):
    """Manage decks' equivalent line already points at Update my decks, the one-click
    front door; this one pointed at Sync decks, an Advanced submenu item that runs half
    of it."""
    from internpearls import dialogs
    anki.gui.interactive = True

    def respond(p):
        if p["kind"] == "dialog":
            save = find(p["tree"], t="button", label="Save")
            return {"events": [{"id": save["id"], "click": True}]}
        assert "Update my decks" in p["text"] and "Sync decks" not in p["text"]
        return {}

    drive(anki, dialogs.open_settings, respond)


def test_the_settings_save_summary_reports_only_settings(anki):
    """It used to close on a fixed sentence about the update screen's decline controls:
    a static line about something Settings has not configured since v0.47.0, which read
    as an extra setting with no control to find. It also called declining the way to
    flag a problem card, which Add note is. Night mode dimming's own line left with it
    when that toggle moved to its own Experimental dialog, so this dialog's summary is
    down to two setting-groups now: sync, and add-on updates."""
    from internpearls import dialogs
    anki.gui.interactive = True
    seen = []

    def respond(p):
        if p["kind"] == "dialog":
            save = find(p["tree"], t="button", label="Save")
            return {"events": [{"id": save["id"], "click": True}]}
        seen.append(p["text"])
        return {}

    drive(anki, dialogs.open_settings, respond)
    assert seen and "Settings saved." in seen[0]
    assert "Decline" not in seen[0] and "flagging" not in seen[0]
    assert "Night Mode" not in seen[0] and "dimmed" not in seen[0]
    assert seen[0].count("<br>") == 3, (
        "the summary should carry one line per setting-group this dialog holds (sync, "
        "add-on updates), plus the leading blank line")


def test_the_auto_sync_hint_reads_as_two_held_back_kinds_not_three(anki):
    """A card's template and its look are one deferral spelled two ways; listing them
    apart read as three separate things auto-sync refuses."""
    from internpearls import dialogs
    anki.gui.interactive = True

    def respond(p):
        assert p["kind"] == "dialog"
        text = " ".join(n.get("text") or "" for n in walk(p["tree"])
                        if n.get("t") == "label")
        assert "card template, look, or note-type format" not in text
        assert "card template or look changed, or whose note-type format did" in text
        cancel = find(p["tree"], t="button", label="Cancel")
        return {"events": [{"id": cancel["id"], "click": True}]}

    drive(anki, dialogs.open_settings, respond)


def test_settings_dialog_has_no_feedback_checkbox(anki):
    """The Card review section (a settings-gated feedback toggle) is retired: feedback
    boxes on the update preview are contextual now, not controlled from here."""
    from internpearls import dialogs

    anki.gui.interactive = True

    def respond(p):
        if p["kind"] == "dialog":
            labels = [n.get("label") or n.get("text") or "" for n in walk(p["tree"])]
            assert not any("Let me flag problems" in l for l in labels)
            assert not any("Card review" in l for l in labels)
            cancel = find(p["tree"], t="button", label="Cancel")
            return {"events": [{"id": cancel["id"], "click": True}]}
        return {}

    drive(anki, dialogs.open_settings, respond)


def test_saving_settings_sheds_the_retired_key(anki):
    """An old install may still carry `collect_card_feedback` in its config; saving
    Settings should shed it silently rather than leave it as dead weight forever."""
    from internpearls import dialogs

    anki.mw._config["collect_card_feedback"] = True
    anki.gui.interactive = True

    def respond(p):
        if p["kind"] == "dialog":
            save = find(p["tree"], t="button", label="Save")
            return {"events": [{"id": save["id"], "click": True}]}
        return {}

    drive(anki, dialogs.open_settings, respond)
    assert "collect_card_feedback" not in anki.mw._config


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


def test_the_local_folder_option_names_which_folder_to_pick(anki):
    """macOS opens its native directory picker with no caption, so the caption naming
    what to pick is invisible on the platform most likely to need it. The instruction
    lives on the option's own line instead, which is read before the picker opens on
    every platform."""
    from internpearls import dialogs
    anki.gui.interactive = True

    def respond(p):
        assert p["kind"] == "dialog"
        text = " ".join(n.get("text") or "" for n in walk(p["tree"])
                        if n.get("t") == "label")
        assert "manifest.json" in text and ".apkg" in text, \
            "the local-folder option has to name the folder the picker will not"
        return _pick_source(p["tree"], "Cancel")

    drive(anki, dialogs.configure_source, respond)


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


@pytest.mark.parametrize("key, value", [
    ("github_decks_repo", 5), ("github_token", None),
])
def test_configure_source_github_form_defaults_non_strings_to_empty(anki, key, value):
    from internpearls import dialogs

    anki.mw._config = {key: value}
    anki.gui.interactive = True

    def respond(p):
        assert p["kind"] == "dialog"
        pick = _pick_source(p["tree"], "GitHub repo")
        if pick:
            return pick
        assert find(p["tree"], t="line", password=False)["value"] == ""
        assert find(p["tree"], t="line", password=True)["value"] == ""
        return _pick_source(p["tree"], "Cancel")

    drive(anki, dialogs.configure_source, respond)
    assert anki.mw._config == {key: value}
    assert not anki.gui.warnings


def test_configure_source_local_picker_defaults_non_string_to_empty(anki):
    from internpearls import dialogs

    anki.mw._config = {"decks_dir": 5}
    anki.gui.interactive = True

    def respond(p):
        if p["kind"] == "dialog":
            return _pick_source(p["tree"], "Local folder")
        assert p["kind"] == "prompt"
        assert p["default"] == ""
        return {"text": "", "ok": False}

    drive(anki, dialogs.configure_source, respond)
    assert anki.mw._config == {"decks_dir": 5}
    assert not anki.gui.warnings


def test_github_source_blank_repo_keeps_the_dialog_open_with_a_warning(anki):
    """OK used to accept unconditionally, so a blank repo silently discarded
    everything typed, a token included, with no way back. Clicking OK with an empty
    repo must leave the dialog open, with the token still there, and name the missing
    field rather than just refusing silently."""
    from internpearls import dialogs
    anki.gui.interactive = True
    rounds = []

    warning_text = "Enter a repo (owner/name) before continuing."

    def respond(p):
        if p["kind"] == "dialog":
            pick = _pick_source(p["tree"], "GitHub repo")
            if pick:
                return pick
            rounds.append(p["tree"])
            token = find(p["tree"], t="line", password=True)
            ok = find(p["tree"], t="button", label="OK")
            if len(rounds) == 1:
                assert not find(p["tree"], t="label", text=warning_text), \
                    "the warning must not show before OK is ever clicked"
                return {"events": [{"id": token["id"], "value": "secret-token"},
                                   {"id": ok["id"], "click": True}]}
            # second round: the blank-repo OK click above must not have closed it
            repo = find(p["tree"], t="line", password=False)
            assert repo["value"] == "", "the dialog closed on a blank repo"
            assert token["value"] == "secret-token", \
                "the token typed before the blank OK click was discarded"
            assert find(p["tree"], t="label", text=warning_text), \
                "expected an inline warning naming the missing field"
            return {"events": [{"id": repo["id"], "value": "someone/decks"},
                               {"id": ok["id"], "click": True}]}
        assert p["kind"] == "warn" and "couldn't connect" in p["text"]
        return {}

    drive(anki, dialogs.configure_source, respond)
    assert len(rounds) == 2, "the blank-repo OK click should not have closed the dialog"
    assert anki.mw._config["github_decks_repo"] == "someone/decks"
    assert anki.mw._config["github_token"] == "secret-token"


def test_the_blank_repo_warning_clears_once_a_repo_is_typed(anki):
    """The warning is about the field as it was. Left standing, it went on telling
    someone to enter a repo while they were looking at the one they had just typed."""
    from internpearls import dialogs
    anki.gui.interactive = True
    warning_text = "Enter a repo (owner/name) before continuing."
    rounds = []

    def respond(p):
        assert p["kind"] == "dialog"
        pick = _pick_source(p["tree"], "GitHub repo")
        if pick:
            return pick
        rounds.append(p["tree"])
        repo = find(p["tree"], t="line", password=False)
        if len(rounds) == 1:
            ok = find(p["tree"], t="button", label="OK")
            return {"events": [{"id": ok["id"], "click": True}]}   # blank: warns
        if len(rounds) == 2:
            assert find(p["tree"], t="label", text=warning_text), (
                "the blank OK click should have warned")
            # Typing alone, with no second OK click, has to clear it.
            return {"events": [{"id": repo["id"], "value": "someone/decks"}]}
        assert not find(p["tree"], t="label", text=warning_text), (
            "the warning survived the repo being typed in")
        cancel = find(p["tree"], t="button", label="Cancel")
        return {"events": [{"id": cancel["id"], "click": True}]}

    drive(anki, dialogs.configure_source, respond)
    assert len(rounds) == 3


def test_configure_source_switching_to_local_folder_clears_repo_and_keeps_token(
        anki, tmp_path):
    """Picking Local folder while a repo is already configured must make the folder
    the effective source (a lingering repo otherwise wins inside _fetch_manifest) and
    must not throw away a token the learner will need if they switch back."""
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


def test_local_folder_is_picked_not_typed_and_opens_at_the_current_one(anki, tmp_path,
                                                                      monkeypatch):
    """A folder that doesn't exist is the one source error a typed path invites, and it
    surfaces later as a broken source rather than at the moment it is entered."""
    from internpearls import dialogs
    path = _write_source(tmp_path)
    anki.mw._config = {"decks_dir": "/a/folder/from/last/time"}
    anki.gui.interactive = True
    opened_at = []

    class _Picker:
        @staticmethod
        def getExistingDirectory(parent=None, caption="", directory="", *a, **k):
            opened_at.append(directory)
            return path

    monkeypatch.setattr(dialogs, "QFileDialog", _Picker)

    def respond(p):
        if p["kind"] == "dialog":
            return _pick_source(p["tree"], "Local folder")
        assert p["kind"] == "info" and "Saved and connected" in p["text"]
        return {}

    drive(anki, dialogs.configure_source, respond)
    assert opened_at and opened_at[-1] == "/a/folder/from/last/time", \
        "the picker must open at the folder already configured"
    assert anki.mw._config["decks_dir"] == path


def test_the_mock_directory_picker_answers_through_the_prompt_payload():
    """What keeps the live demo working: it has no native picker either, so the mock's
    stand-in comes back through the same prompt payload the demo already draws, and a
    cancelled pick reads as an empty path."""
    from aqt.qt import QFileDialog
    import mock_anki as m
    m._gui.file_picks = []
    m._gui.interactive = False
    assert QFileDialog.getExistingDirectory(None, "Pick a folder", "/seed") == ""


def test_configure_source_failure_escapes_exception_text(anki, monkeypatch):
    from internpearls import dialogs
    from test_lifecycle import _raise_markup_error
    monkeypatch.setattr(dialogs, "_fetch_manifest", _raise_markup_error)
    anki.gui.interactive = True

    def respond(p):
        if p["kind"] == "dialog":
            return _pick_source(p["tree"], "Try the example deck")
        assert p["kind"] == "warn"
        assert 'Saved, but couldn\'t connect: &lt;b&gt;x&lt;/b&gt; &amp; "y"<br><br>' in p["text"]
        return {}

    drive(anki, dialogs.configure_source, respond)


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


def test_configure_source_survives_a_manifest_with_no_decks_key(anki, tmp_path):
    """configure_source() indexed manifest['decks'] directly for its own "found N
    decks" message, while every other reader of a manifest here uses .get("decks", []).
    A minimal manifest (no decks key at all) turned a successful connect into a crash."""
    from internpearls import dialogs
    folder = tmp_path / "minimal"
    folder.mkdir()
    (folder / "manifest.json").write_text(json.dumps({"schema": 2}), encoding="utf8")
    anki.gui.interactive = True

    def respond(p):
        if p["kind"] == "dialog":
            return _pick_source(p["tree"], "Local folder")
        if p["kind"] == "prompt":
            return {"text": str(folder), "ok": True}
        assert p["kind"] == "info" and "0 decks" in p["text"], p["text"]
        return {}

    drive(anki, dialogs.configure_source, respond)
    assert not anki.gui.warnings, "a manifest with no decks key should not crash"


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


def _digest_texts(anki, entries):
    """Every label and button string on the digest dialog, from one pass over it.

    Drives next_interaction directly rather than through drive(), for the reason
    test_copy_again_puts_the_digest_back_on_the_clipboard spells out: a replayed flow
    reruns this dialog from the top, and one pass is all these need.
    """
    from internpearls import review
    seen = {}
    original = anki.gui.next_interaction

    def once(payload):
        seen["tree"] = payload["tree"]
        close = find(payload["tree"], t="button", label="Close")
        return {"events": [{"id": close["id"], "click": True}]}

    anki.gui.next_interaction = once
    try:
        review.offer_feedback_digest(None, entries)
    finally:
        anki.gui.next_interaction = original
    return _all_text(seen["tree"])


def test_the_digest_says_nothing_is_sent_and_what_to_do_with_it(anki):
    """Nothing here transmits anything: the digest is copied to the clipboard for the
    learner to paste. "Copied, ready to paste" left it possible to read this and close it
    assuming the deck author now had it."""
    texts = _digest_texts(anki, [{"deck": "IP::A", "guid": "g1", "front": "Front one",
                                  "note": "dose looks off"}])
    assert "Nothing is sent automatically" in texts
    assert "paste it into a message to the deck author" in texts


def test_the_digest_heading_does_not_call_a_decision_a_flag(anki):
    """A reader who skipped two cards and flagged none was told "2 cards flagged"."""
    entries = [{"deck": "IP::A", "guid": "g1", "front": "Front one", "note": "",
                "decision": "skipped"},
               {"deck": "IP::A", "guid": "g2", "front": "Front two", "note": "",
                "decision": "skipped"}]
    texts = _digest_texts(anki, entries)
    assert "2 decisions recorded" in texts
    assert "flagged" not in texts


def test_a_run_with_nothing_to_report_can_still_say_it_changed_nothing(anki):
    """Cancelling the post-confirm download left the reader with a progress bar that
    closed and no word at all, after a click that had said Update."""
    from internpearls import review
    review.show_result_with_feedback(None, (), [],
                                     nothing_note=review.NOTHING_CHANGED)
    assert anki.gui.infos == [review.NOTHING_CHANGED]


def test_a_run_with_nothing_to_report_and_no_note_stays_silent(anki):
    """Declining the confirmation itself is answered by the click; the caller that
    passes no note must still get no dialog."""
    from internpearls import review
    review.show_result_with_feedback(None, (), [])
    assert anki.gui.infos == []


def test_the_kept_state_is_worded_the_same_way_everywhere(anki):
    """Three wordings of one state ("Keep mine", "KEPT YOURS", "Kept your version") is
    three states as far as a reader is concerned. The control's own word is what the
    chip and the Declined heading echo."""
    from internpearls import dialogs, review, widgets
    assert dict(review._CHANGED_OPTIONS)["keep"] == "Keep yours"
    assert widgets.CHIPS["kept"] == "KEPT YOURS"
    assert dict(dialogs._DECLINE_GROUPS)["keep"] == "Kept yours"


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


def test_decision_cell_selects_and_reports(anki):
    from internpearls import widgets
    chosen = []
    cell = widgets.decision_cell(
        [("import", "Import"), ("held", "Later"), ("never", "Never")],
        "import", chosen.append)
    cell.buttons["held"].click()
    assert chosen == ["held"]
    cell.set_state("never")
    assert cell.buttons["never"].isChecked()


def test_decline_chip_kinds_have_labels_and_roles(anki):
    from internpearls import review, widgets
    assert widgets.CHIPS["held"] == "LATER"
    assert widgets.CHIPS["kept"] == "KEPT YOURS"
    assert widgets._ROLES["held"] == "updated"
    assert widgets._ROLES["kept"] == "retired"
    assert "skipped" not in widgets.CHIPS and "skipped" not in widgets._ROLES
    assert "skip" not in review._DECLINE_CHIP and "skip" not in review._TURNED_DOWN


# --------------------------------------------------------- update-body decisions
def _walk_widgets(root, out=None, seen=None):
    """Every live widget under `root`, depth-first, the way test_review.py's helper of
    the same name walks a mock_anki tree: through any attribute that looks like a
    widget, then through the widget's own layout. Node dicts (walk/find above) carry no
    callables, so a decision_cell's `.buttons` or a box's `.isVisible()` need the real
    objects."""
    seen = seen if seen is not None else set()
    out = out if out is not None else []
    if id(root) in seen:
        return out
    seen.add(id(root))
    out.append(root)
    for v in vars(root).values():
        if hasattr(v, "wid"):
            _walk_widgets(v, out, seen)
    layout = getattr(root, "_layout", None)
    if layout is not None:
        for child in getattr(layout, "_children", []) or []:
            _walk_widgets(child, out, seen)
    return out


def _find_decision_cell(body):
    """The first card's decision control, not the filter bar's options."""
    return next(w for w in _walk_widgets(body)
                if hasattr(w, "buttons") and "all" not in w.buttons)


def _find_feedback_box(body):
    return next((w for w in _walk_widgets(body) if isinstance(w, mock_anki.QPlainTextEdit)),
               None)


def _row_texts(body):
    return [n.get("text") for n in walk(body.node()) if n.get("t") == "label"]


def _card_detail(guid, kind, **extra):
    detail = {
        "guid": guid, "kind": kind, "notetype": "Study Deck - Basic",
        "fields": [("Front", "What nerve block covers the anterior thigh?"),
                  ("Back", "Femoral nerve block"), ("Why", ""), ("Image", ""),
                  ("Tag", ""), ("Dosing", ""), ("Notes", "")],
    }
    detail.update(extra)
    return detail


def _new_card_detail(guid="guid-new-a", **extra):
    return _card_detail(guid, "new", **extra)


def _changed_card_detail(guid="guid-changed-a", **extra):
    return _card_detail(guid, "changed", was={"Back": "old back"}, **extra)


def _build_body(details):
    from internpearls import review
    items = [("card", "Example Deck", d) for d in details]
    flags, new_index, decisions = {}, {}, {}
    body, boxes, flush = review.build_update_body(
        items, {}, flags, new_index, decisions, "", lambda: "", "")
    return body, boxes, flush, decisions


def _build_body_with_one_new_card():
    return _build_body([_new_card_detail()])


def _build_body_with_one_changed_card():
    return _build_body([_changed_card_detail()])


def test_new_card_row_offers_import_later_never(anki):
    body, boxes, flush, decisions = _build_body_with_one_new_card()
    cell = _find_decision_cell(body)
    assert list(cell.buttons) == ["import", "held", "never"]
    assert [b.text() for b in cell.buttons.values()] == ["Import", "Later", "Never"]
    assert cell.buttons["import"].isChecked()


def test_choosing_later_records_it_and_opens_no_note_box(anki):
    """Later is a deferral, not a judgment, so it does not ask why."""
    body, boxes, flush, decisions = _build_body_with_one_new_card()
    cell = _find_decision_cell(body)
    cell.buttons["held"].click()
    assert decisions == {"guid-new-a": "held"}
    box = _find_feedback_box(body)
    assert box is None
    texts = " ".join(t or "" for t in _row_texts(body))
    assert "next time you run Update my decks, set to Import" in texts
    assert "With a note, it waits until the card is updated" in texts


_WAITING = {"declined_state": "held", "later_wait": True, "later_note": "fix the dose",
            "later_note_status": "not_updated"}


def test_a_row_waiting_on_its_note_says_so_beside_later(anki):
    body, _boxes, _flush, decisions = _build_body([_new_card_detail(**_WAITING)])
    assert decisions == {"guid-new-a": "held"}
    texts = " ".join(t or "" for t in _row_texts(body))
    assert ("Waiting for this card to be updated. Choose Import to take it as it is."
            in texts)
    assert "Comes back the next time" not in texts


def test_a_changed_row_waiting_on_its_note_names_apply(anki):
    body, _boxes, _flush, decisions = _build_body([_changed_card_detail(**_WAITING)])
    assert decisions == {"guid-changed-a": "held"}
    texts = " ".join(t or "" for t in _row_texts(body))
    assert ("Waiting for this card to be updated. Choose Apply to take it as it is."
            in texts)


def test_a_migrated_row_seeded_at_later_keeps_the_general_caption(anki):
    body, _boxes, _flush, decisions = _build_body([_new_card_detail(
        declined_state="held", later_wait=True, later_migrated=True)])
    assert decisions == {"guid-new-a": "held"}
    texts = " ".join(t or "" for t in _row_texts(body))
    assert "Comes back the next time you run Update my decks, set to Import." in texts
    assert "Waiting for this card" not in texts


def test_later_on_a_changed_row_says_it_comes_back_set_to_apply(anki):
    body, boxes, flush, decisions = _build_body_with_one_changed_card()
    _find_decision_cell(body).buttons["held"].click()
    assert decisions == {"guid-changed-a": "held"}
    assert _find_feedback_box(body) is None
    texts = " ".join(t or "" for t in _row_texts(body))
    assert "next time you run Update my decks, set to Apply" in texts


def test_choosing_never_on_a_new_card_opens_the_note_box(anki):
    body, boxes, flush, decisions = _build_body_with_one_new_card()
    _find_decision_cell(body).buttons["never"].click()
    box = _find_feedback_box(body)
    assert box is not None and box.isVisible()


def test_choosing_never_collapses_the_row(anki):
    body, boxes, flush, decisions = _build_body_with_one_new_card()
    cell = _find_decision_cell(body)
    cell.buttons["never"].click()
    assert decisions == {"guid-new-a": "never"}
    assert "won't be offered again" in _row_texts(body)


def test_changed_card_row_offers_apply_keep_and_never(anki):
    """Keep yours is a not-this-time; Never is its permanent form. Without the
    third option the only way to stop being asked about one card was to keep declining
    it every time the deck changed."""
    body, boxes, flush, decisions = _build_body_with_one_changed_card()
    cell = _find_decision_cell(body)
    assert list(cell.buttons) == ["apply", "keep", "held", "frozen"]
    assert [b.text() for b in cell.buttons.values()] == [
        "Apply", "Keep yours", "Later", "Never"]
    assert cell.buttons["apply"].isChecked()


def test_choosing_never_on_a_changed_card_says_it_is_permanent(anki):
    body, boxes, flush, decisions = _build_body_with_one_changed_card()
    cell = _find_decision_cell(body)
    cell.buttons["frozen"].click()
    assert decisions == {"guid-changed-a": "frozen"}
    texts = _row_texts(body)
    assert "won't be offered again" in texts
    # The two soft declines promise the card comes back on its own. This one must not.
    assert "the next time this deck changes" not in texts


def test_never_on_a_changed_card_opens_the_note_box(anki):
    body, boxes, flush, decisions = _build_body_with_one_changed_card()
    _find_decision_cell(body).buttons["frozen"].click()
    box = _find_feedback_box(body)
    assert box is not None and box.isVisible()


def test_predeclined_detail_renders_one_chip_and_its_preset_state(anki):
    """A re-offered decline wears the decline, and only that: UPDATED + KEPT YOURS
    stacked fixed-width columns in front of the card's own words, beside a decision
    control, which is what crushed the front of exactly the rows the "Worth another
    look" hint points at. The change since the decline is said by that hint, not by a
    second pill."""
    from internpearls import widgets
    detail = _changed_card_detail(declined_state="keep", changed_since_decline=True)
    body, boxes, flush, decisions = _build_body(details=[detail])
    cell = _find_decision_cell(body)
    assert cell.buttons["keep"].isChecked()
    texts = _row_texts(body)
    chips = [t for t in texts if t in set(widgets.CHIPS.values())]
    assert chips == ["KEPT YOURS"], f"expected one chip, got {chips}"
    assert "Changed since you kept yours. Worth another look." in texts
    # The preset state has to actually land in the dict the run persists, not just
    # drive what the control shows.
    assert decisions == {"guid-changed-a": "keep"}
    cell.buttons["apply"].click()
    assert decisions == {}


def test_a_waiting_later_row_is_seeded_at_later_and_wears_the_later_chip(anki):
    from internpearls import widgets
    detail = _new_card_detail(declined_state="held", later_wait=True,
                              later_note="fix the dose", later_note_status="not_updated")
    body, boxes, flush, decisions = _build_body(details=[detail])
    assert _find_decision_cell(body).buttons["held"].isChecked()
    assert decisions == {"guid-new-a": "held"}
    texts = _row_texts(body)
    assert [t for t in texts if t in set(widgets.CHIPS.values())] == ["LATER"]
    assert "Your note: fix the dose" in texts and "Not updated yet" in texts
    assert _find_feedback_box(body) is None, "a seeded Later asks nothing"


def test_a_returning_later_row_that_does_not_wait_starts_at_its_default(anki):
    from internpearls import widgets
    plain = _new_card_detail(declined_state="held")
    updated = _changed_card_detail(declined_state="held", later_note="fix the dose",
                                   later_note_status="updated")
    body, boxes, flush, decisions = _build_body(details=[plain, updated])
    assert decisions == {}
    texts = _row_texts(body)
    assert texts.count(widgets.CHIPS["held"]) == 2
    assert "Your note: fix the dose" in texts and "Updated since your note" in texts
    assert "Not updated yet" not in texts


def test_a_returning_note_is_shown_as_text_not_markup(anki):
    detail = _new_card_detail(declined_state="held", later_wait=True,
                              later_note="<b>dose</b> & route",
                              later_note_status="not_updated")
    row_labels = [w for w in _walk_widgets(_build_body([detail])[0])
                  if isinstance(w, mock_anki.QLabel)
                  and "Your note:" in (w.text() or "")]
    assert len(row_labels) == 1
    from aqt.qt import Qt
    assert row_labels[0].text() == "Your note: <b>dose</b> & route"
    assert row_labels[0]._format == Qt.TextFormat.PlainText


def test_the_migration_line_shows_once_above_the_list(anki):
    from internpearls import logic, widgets
    details = [_new_card_detail(guid=f"guid-{i}", declined_state="held",
                                later_wait=True, later_migrated=True)
               for i in range(3)] + [_new_card_detail(guid="guid-plain")]
    body = _build_body(details)[0]
    line = ("Skip is now Later. 3 cards you skipped earlier are below, still set to "
            "Later.")
    texts = _row_texts(body)
    assert texts.count(line) == 1
    labels = [w for w in body._layout._children
              if isinstance(w, mock_anki.QLabel) and w.text() == line]
    stream = next(w for w in body._layout._children
                  if isinstance(w, widgets.StreamingList))
    assert labels and body._layout._children.index(labels[0]) < (
        body._layout._children.index(stream)), "the line sits above the list"
    assert logic.later_migration_line(1) == (
        "Skip is now Later. 1 card you skipped earlier is below, still set to Later.")
    assert logic.later_migration_line(0) == ""


def test_no_migration_line_without_a_migrated_row(anki):
    body = _build_body([_new_card_detail(declined_state="held", later_wait=True)])[0]
    assert not any("Skip is now Later" in (t or "") for t in _row_texts(body))


def test_a_kept_row_wears_the_kept_chip_rather_than_its_kind(anki):
    from internpearls import widgets
    detail = _changed_card_detail(declined_state="keep")
    body, boxes, flush, decisions = _build_body(details=[detail])
    chips = [t for t in _row_texts(body) if t in set(widgets.CHIPS.values())]
    assert chips == [widgets.CHIPS["kept"]]


def test_an_expanded_body_indents_by_the_columns_its_row_actually_draws(anki):
    """The body lines up under the row's own text, and says so by measuring the same
    leading columns the header laid out rather than assuming there is one chip. A row
    that grows a second column and leaves this alone hangs its whole body a chip-width
    left of the line it belongs to."""
    from internpearls import review, widgets
    detail = _changed_card_detail(declined_state="keep", changed_since_decline=True)
    row = review._card_row(detail, {}, {}, {}, lambda *a: None,
                           chips=review._chip_kinds([("card", "D", detail)]))
    # [header, the changed-since hint, the body, its caption, its feedback box]
    body = row._layout._children[-3]
    assert body._layout._margins[0] == widgets.row_text_indent(
        1, ("kept",)), "the expanded body no longer indents by its own columns"


def test_a_re_offered_decline_opens_with_no_empty_feedback_box(anki):
    """Ten re-offered kept cards used to arrive as ten open empty boxes: the parked
    -open state _apply_decision_visuals avoids everywhere else. Add note is what opens
    one on a row like this."""
    body, boxes, flush, decisions = _build_body(
        [_changed_card_detail(declined_state="keep")])
    box = _find_feedback_box(body)
    assert box is None
    add_note = next(w for w in _walk_widgets(body)
                    if getattr(w, "text", None) and w.text() == "Add note")
    assert add_note.isVisible(), "a re-offered decline must still be able to add a note"


def test_a_re_offered_decline_with_a_saved_note_opens_showing_it(anki):
    from internpearls import review
    detail = _changed_card_detail(declined_state="keep")
    items = [("card", "Example Deck", detail)]
    flags = {detail["guid"]: "dose looks off"}
    body, boxes, flush = review.build_update_body(
        items, {}, flags, {}, {}, "", lambda: "", "")
    box = _find_feedback_box(body)
    assert box.isVisible() and box.toPlainText() == "dose looks off"


def test_declining_here_and_now_still_opens_the_box(anki):
    body, boxes, flush, decisions = _build_body_with_one_changed_card()
    _find_decision_cell(body).buttons["keep"].click()
    assert _find_feedback_box(body).isVisible()


def test_a_decline_caption_names_the_way_back_rather_than_promising_next_update(anki):
    """A kept card is offered again only when its source content changes. Declined
    cards is the way back that does not depend on that ever happening, so the caption
    names it."""
    from internpearls import review
    for state, caption in review._DECLINE_CAPTION.items():
        assert "Declined cards" in caption, f"{state} caption names no way back"
        assert "next update" not in caption, (
            f"{state} caption still promises a re-offer nothing here can schedule")


def test_returning_to_default_pops_the_guid_and_unstrikes_the_row(anki):
    """decisions stays sparse: choosing a non-default state records it, and clicking
    back to the row's default removes the entry rather than writing it back in. The
    strikeout Never put on the primary line has to come off again with it."""
    body, boxes, flush, decisions = _build_body_with_one_new_card()
    cell = _find_decision_cell(body)
    primary = next(w for w in _walk_widgets(body)
                  if callable(getattr(w, "text", None))
                  and "anterior thigh" in (w.text() or ""))
    cell.buttons["never"].click()
    assert decisions == {"guid-new-a": "never"}
    assert primary.font().strikeOut()
    cell.buttons["import"].click()
    assert decisions == {}
    assert not primary.font().strikeOut()


def test_returning_to_default_closes_an_empty_box_and_restores_add_note(anki):
    """An empty feedback box a decline opened closes again on return to the row's
    default, since nothing was actually written into it worth keeping open, and the
    quiet Add note affordance comes back so feedback is still reachable."""
    body, boxes, flush, decisions = _build_body_with_one_new_card()
    cell = _find_decision_cell(body)
    cell.buttons["never"].click()
    box = _find_feedback_box(body)
    assert box.isVisible()
    cell.buttons["import"].click()
    assert not box.isVisible(), "an empty box should close again on return to default"
    add_note = next(w for w in _walk_widgets(body)
                    if getattr(w, "text", None) and w.text() == "Add note")
    assert add_note.isVisible(), "Add note should reappear once the box is closed"


def test_typed_but_unsaved_text_keeps_the_box_open_on_return_to_default(anki):
    """The empty-box-closes fix must not also swallow a note the learner's mid-typing:
    text sitting in the box, even before it has reached `flags`, keeps it open."""
    body, boxes, flush, decisions = _build_body_with_one_new_card()
    cell = _find_decision_cell(body)
    cell.buttons["never"].click()
    box = _find_feedback_box(body)
    box.setPlainText("wrong dose")
    cell.buttons["import"].click()
    assert box.isVisible(), "typed text should keep the box open on return to default"


def test_a_predeclined_card_past_the_first_streaming_batch_still_reaches_decisions(anki):
    """_card_row alone cannot be trusted to seed `decisions`: StreamingList only builds
    the first batch of rows up front and the rest only once the reader scrolls near the
    bottom, which never happens here. build_update_body has to seed the dict itself,
    from `items`, before any row widget exists."""
    from internpearls import review
    details = [_new_card_detail(guid=f"guid-{i}") for i in range(60)]
    details[55] = _new_card_detail(guid="guid-55", declined_state="held",
                                   later_wait=True)
    details[56] = _new_card_detail(guid="guid-56", declined_state="held")
    items = [("card", "Example Deck", d) for d in details]
    flags, new_index, decisions = {}, {}, {}
    body, boxes, flush = review.build_update_body(
        items, {}, flags, new_index, decisions, "", lambda: "", "")
    assert "guid-56" not in decisions, "a Later row that does not wait starts at Import"
    assert decisions.get("guid-55") == "held", (
        "a predeclined card past the first StreamingList batch never reached decisions")


def test_scroll_actions_cross_four_streaming_batches_without_losing_a_decision(anki):
    """The browser scroll action must drive StreamingList itself, while an earlier
    production decision remains in the same replayed state."""
    from internpearls import review
    details = [_new_card_detail(guid=f"guid-{i}") for i in range(230)]
    items = [("card", "Example Deck", detail) for detail in details]
    flags, new_index, decisions = {}, {}, {}
    body, boxes, flush = review.build_update_body(
        items, {}, flags, new_index, decisions, "", lambda: "", "")
    streaming = next(widget for widget in _walk_widgets(body)
                     if callable(getattr(widget, "shown", None))
                     and callable(getattr(widget, "total", None)))
    first = _find_decision_cell(body)
    first.buttons["held"].click()
    shown = [streaming.shown()]

    for offset in range(1, 5):
        mock_anki.apply_actions({"actions": [{
            "type": "scroll", "id": streaming.wid, "offset": offset,
        }]})
        shown.append(streaming.shown())

    assert shown == [50, 100, 150, 200, 230]
    assert decisions == {"guid-0": "held"}


def test_import_row_offers_a_quiet_add_note_that_reveals_the_box(anki):
    """An Import/Apply row is not declined, so its box starts hidden, but a small "Add
    note" affordance, at the end of the row's expanded body, can still reveal it
    (flag-without-decline)."""
    from internpearls import review
    body, boxes, flush, decisions = _build_body_with_one_new_card()
    box = _find_feedback_box(body)
    assert box is None
    caret = next(w for w in _walk_widgets(body)
                if getattr(w, "text", None) and w.text() in (review._CARET_CLOSED,
                                                             review._CARET_OPEN))
    caret.clicked.emit()   # Add note lives in the expanded body now
    add_note = next(w for w in _walk_widgets(body)
                    if getattr(w, "text", None) and w.text() == "Add note")
    add_note.click()
    box = _find_feedback_box(body)
    assert box.isVisible()


def test_status_line_is_recomputed_after_a_decision_change(anki):
    """status_line() (renamed from flagged_line) is re-rendered on a decision change
    the same way it already was on every feedback keystroke."""
    from internpearls import review
    calls = []

    def status_line():
        calls.append(True)
        return f"{len(calls)} calls"

    items = [("card", "Example Deck", _new_card_detail())]
    body, boxes, flush = review.build_update_body(
        items, {}, {}, {}, {}, "", status_line, "")
    before = len(calls)
    cell = _find_decision_cell(body)
    cell.buttons["held"].click()
    assert len(calls) > before, "status_line was not recomputed after a decision change"


def test_declined_dialog_names_a_frozen_card_for_what_it_is(anki):
    """"Never imported" is the wrong words for a card the learner has and kept: what
    the learner turned down was every future version of it, so it needs its own
    heading or the only way back is under Other."""
    from internpearls import config
    config.save_declined({
        "g1": {"state": "frozen", "front": "front a", "deck": "IP::A",
               "decided": "2026-08-31", "hash": ""}})
    texts = _all_text(_snapshot_declined_dialog(anki))
    assert "Kept yours, no more updates" in texts and "front a" in texts
    assert "Never imported" not in texts


# --------------------------------------------------------- demo widget contract
def _contract_nodes(tree):
    return {node["id"]: node for node in tree["nodes"]}


def _build_contract_action_scene():
    from aqt.qt import (QComboBox, QDialog, QLabel, QLineEdit, QPushButton,
                        QRadioButton, QScrollArea, QVBoxLayout, QWidget)

    root = QWidget()
    layout = QVBoxLayout(root)

    button = QPushButton("Toggle")
    button.setCheckable(True)
    layout.addWidget(button)

    radio_a = QRadioButton("First")
    radio_b = QRadioButton("Second")
    layout.addWidget(radio_a)
    layout.addWidget(radio_b)
    radio_a.setChecked(True)

    combo = QComboBox()
    combo.addItem("First", "option-first")
    combo.addItem("Second", "option-second")
    layout.addWidget(combo)

    line = QLineEdit()
    layout.addWidget(line)

    link = QLabel('<a href="details">Details</a>')
    link.setOpenExternalLinks(False)
    layout.addWidget(link)

    scroll = QScrollArea()
    scroll.verticalScrollBar().setMaximum(10)
    layout.addWidget(scroll)

    dialog = QDialog()
    return {
        "root": root,
        "button": button,
        "radio_a": radio_a,
        "radio_b": radio_b,
        "combo": combo,
        "line": line,
        "link": link,
        "scroll": scroll,
        "dialog": dialog,
    }


def _observe_contract_scene(scene):
    events = []
    scene["button"].clicked.connect(
        lambda checked: events.append(
            ("button", checked, scene["button"].isChecked())))
    scene["radio_a"].toggled.connect(
        lambda checked: events.append(("radio-a", checked)))
    scene["radio_b"].toggled.connect(
        lambda checked: events.append(("radio-b", checked)))
    scene["combo"].currentIndexChanged.connect(
        lambda index: events.append(("combo-index", index)))
    scene["combo"].currentTextChanged.connect(
        lambda text: events.append(("combo-text", text)))
    scene["line"].textEdited.connect(
        lambda text: events.append(("edited", text)))
    scene["line"].textChanged.connect(
        lambda text: events.append(("changed", text)))
    scene["line"].editingFinished.connect(
        lambda: events.append(("finished",)))
    scene["link"].linkActivated.connect(
        lambda action_id: events.append(("link", action_id)))
    scene["scroll"].verticalScrollBar().valueChanged.connect(
        lambda offset: events.append(("scroll", offset)))
    scene["dialog"].rejected.connect(
        lambda: events.append(("escape",)))
    return events


def test_demo_serializer_preserves_layout_state_and_stable_ids(anki):
    from aqt.qt import (QButtonGroup, QComboBox, QFormLayout, QGridLayout,
                        QLabel, QLineEdit, QRadioButton, QStackedWidget, Qt,
                        QVBoxLayout, QWidget)

    root = QWidget()
    outer = QVBoxLayout(root)

    grid = QGridLayout()
    grid_value = QLabel("Grid value")
    grid.addWidget(grid_value, 2, 3, 2, 4, Qt.AlignmentFlag.AlignCenter)
    grid.setColumnMinimumWidth(3, 80)
    grid.setColumnStretch(3, 2)
    outer.addLayout(grid)

    form = QFormLayout()
    form_field = QLineEdit()
    form.addRow("Name", form_field)
    outer.addLayout(form)

    stack = QStackedWidget()
    first_page = QWidget()
    second_page = QWidget()
    stack.addWidget(first_page)
    stack.addWidget(second_page)
    stack.setCurrentWidget(second_page)
    outer.addWidget(stack)

    state_parent = QWidget()
    state_layout = QVBoxLayout(state_parent)
    state_child = QLineEdit()
    state_layout.addWidget(state_child)
    state_parent.setEnabled(False)
    state_parent.hide()
    outer.addWidget(state_parent)

    combo = QComboBox()
    combo.addItem("First", "option-first")
    combo.addItem("Second", "option-second")
    outer.addWidget(combo)

    implicit_parent = QWidget()
    implicit_layout = QVBoxLayout(implicit_parent)
    implicit_left = QVBoxLayout()
    implicit_right = QVBoxLayout()
    implicit_a = QRadioButton("Implicit A")
    implicit_b = QRadioButton("Implicit B")
    implicit_left.addWidget(implicit_a)
    implicit_right.addWidget(implicit_b)
    implicit_layout.addLayout(implicit_left)
    implicit_layout.addLayout(implicit_right)
    implicit_a.setChecked(True)
    implicit_b.setChecked(True)
    outer.addWidget(implicit_parent)

    explicit_a = QRadioButton("Explicit A")
    explicit_b = QRadioButton("Explicit B")
    explicit_group = QButtonGroup(root)
    explicit_group.addButton(explicit_a)
    explicit_group.addButton(explicit_b)
    outer.addWidget(explicit_a)
    outer.addWidget(explicit_b)

    tree = mock_anki.serialize_widget(root)
    nodes = _contract_nodes(tree)

    assert tree["root_id"] == root.wid
    assert nodes[grid.wid]["cells"] == [{
        "id": grid_value.wid,
        "row": 2,
        "column": 3,
        "row_span": 2,
        "column_span": 4,
        "alignment": "center",
    }]
    assert nodes[grid.wid]["column_minimums"] == [0, 0, 0, 80]
    assert nodes[grid.wid]["column_stretches"] == [0, 0, 0, 2]

    form_label = next(node for node in nodes.values()
                      if node["kind"] == "label" and node["text"] == "Name")
    assert nodes[form.wid]["rows"] == [{
        "label_id": form_label["id"],
        "field_id": form_field.wid,
    }]
    assert nodes[stack.wid]["pages"] == [first_page.wid, second_page.wid]
    assert nodes[stack.wid]["current_page"] == second_page.wid
    assert first_page.wid in nodes and second_page.wid in nodes
    assert nodes[first_page.wid]["effective_visible"] is False
    assert nodes[second_page.wid]["effective_visible"] is True

    assert nodes[state_child.wid]["visible"] is True
    assert nodes[state_child.wid]["enabled"] is True
    assert nodes[state_child.wid]["effective_visible"] is False
    assert nodes[state_child.wid]["effective_enabled"] is False
    assert nodes[combo.wid]["options"] == [
        {"id": "option-first", "label": "First"},
        {"id": "option-second", "label": "Second"},
    ]
    assert all(node.get("role") != "action" for node in nodes.values())

    assert nodes[implicit_a.wid]["exclusive"] is True
    assert nodes[implicit_a.wid]["group_id"] == nodes[implicit_b.wid]["group_id"]
    assert implicit_a.isChecked() is False
    assert implicit_b.isChecked() is True
    assert nodes[explicit_a.wid]["exclusive"] is True
    assert nodes[explicit_a.wid]["group_id"] == explicit_group.wid
    assert nodes[explicit_b.wid]["group_id"] == explicit_group.wid


def test_demo_serializer_keeps_explicit_plain_label_text_plain(anki):
    from aqt.qt import QLabel, Qt

    label = QLabel("<b>plain</b>")
    label.setTextFormat(Qt.TextFormat.PlainText)

    tree = mock_anki.serialize_widget(label)

    assert tree["nodes"][0]["format"] == "plain"
    assert tree["nodes"][0]["text"] == "<b>plain</b>"


def test_demo_serializer_preserves_streaming_scroll_counts(anki):
    from aqt.qt import QScrollArea

    class ProgressScroll(QScrollArea):
        def shown(self):
            return 2

        def total(self):
            return 5

    scroll = ProgressScroll()
    scroll.verticalScrollBar().setMaximum(10)

    tree = mock_anki.serialize_widget(scroll)

    assert tree["nodes"][0]["shown_count"] == 2
    assert tree["nodes"][0]["total_count"] == 5


def test_demo_actions_match_qt_signal_order_and_values(anki):
    scene = _build_contract_action_scene()
    events = _observe_contract_scene(scene)
    ids = {name: widget.wid for name, widget in scene.items()}

    assert mock_anki.apply_actions(
        {"actions": contract_action_table(ids)}) is None

    assert events == [
        ("button", True, True),
        ("radio-a", False),
        ("radio-b", True),
        ("combo-index", 1),
        ("combo-text", "Second"),
        ("edited", "x"),
        ("changed", "x"),
        ("finished",),
        ("link", "details"),
        ("scroll", 7),
        ("escape",),
    ]
    assert scene["button"].isChecked() is True
    assert scene["radio_a"].isChecked() is False
    assert scene["radio_b"].isChecked() is True
    assert scene["combo"].currentData() == "option-second"
    assert scene["line"].text() == "x"
    assert scene["scroll"].verticalScrollBar().value() == 7
    assert scene["dialog"]._result == 0


def test_demo_actions_reject_unknown_and_unavailable_targets(anki):
    from aqt.qt import QPushButton, QStackedWidget, QVBoxLayout, QWidget

    with pytest.raises(mock_anki.ProtocolError, match="unknown widget id: missing"):
        mock_anki.apply_actions(
            {"actions": [{"type": "activate", "id": "missing"}]})

    root = QWidget()
    layout = QVBoxLayout(root)
    disabled = QPushButton("Disabled")
    disabled.setEnabled(False)
    hidden_parent = QWidget()
    hidden_layout = QVBoxLayout(hidden_parent)
    hidden = QPushButton("Hidden")
    hidden_layout.addWidget(hidden)
    hidden_parent.hide()
    stack = QStackedWidget()
    inactive_page = QWidget()
    inactive_layout = QVBoxLayout(inactive_page)
    inactive = QPushButton("Inactive")
    inactive_layout.addWidget(inactive)
    active_page = QWidget()
    stack.addWidget(inactive_page)
    stack.addWidget(active_page)
    stack.setCurrentWidget(active_page)
    layout.addWidget(disabled)
    layout.addWidget(hidden_parent)
    layout.addWidget(stack)

    for widget, message in (
            (disabled, "widget is effectively disabled"),
            (hidden, "widget is effectively hidden"),
            (inactive, "widget is effectively hidden")):
        with pytest.raises(mock_anki.ProtocolError, match=message):
            mock_anki.apply_actions({
                "actions": [{"type": "activate", "id": widget.wid}],
            })


def test_legacy_value_events_keep_fixture_semantics_while_actions_reject_hidden(anki):
    from aqt.qt import QPlainTextEdit, QVBoxLayout, QWidget

    root = QWidget()
    layout = QVBoxLayout(root)
    field = QPlainTextEdit()
    layout.addWidget(field)
    field.hide()

    with pytest.raises(mock_anki.ProtocolError, match="effectively hidden"):
        mock_anki.apply_actions(
            {"actions": [{
                "type": "edit-text", "id": field.wid, "value": "strict",
                "selection_start": 0, "selection_end": 0, "composing": False,
            }]}
        )

    mock_anki._apply_events(
        {"events": [{"id": field.wid, "value": "legacy"}]}
    )

    assert field.toPlainText() == "legacy"


def test_demo_actions_enforce_type_and_membership_rules(anki):
    from aqt.qt import QComboBox, QLabel, QPushButton
    from tests.demo_contract_generated import ACTION_KINDS

    button = QPushButton("Button")
    with pytest.raises(mock_anki.ProtocolError, match="unknown action type"):
        mock_anki.apply_actions({
            "actions": [{"type": "not-an-action", "id": button.wid}],
        })
    with pytest.raises(mock_anki.ProtocolError, match="action not permitted"):
        mock_anki.apply_actions({
            "actions": [{"type": "finish-edit", "id": button.wid}],
        })

    combo = QComboBox()
    combo.addItem("First", "option-first")
    with pytest.raises(mock_anki.ProtocolError, match="unknown combo option"):
        mock_anki.apply_actions({"actions": [{
            "type": "select-option", "id": combo.wid,
            "option_id": "option-missing",
        }]})

    label = QLabel('<a href="details">Details</a>')
    with pytest.raises(mock_anki.ProtocolError, match="unknown link action"):
        mock_anki.apply_actions({"actions": [{
            "type": "activate-link", "id": label.wid,
            "action_id": "missing",
        }]})

    assert tuple(ACTION_KINDS) == mock_anki.ACTION_KINDS


def test_demo_serializer_rejects_duplicate_tree_option_and_grid_ids(anki):
    from aqt.qt import QComboBox, QGridLayout, QLabel, QVBoxLayout, QWidget

    root = QWidget()
    layout = QVBoxLayout(root)
    first = QLabel("First")
    second = QLabel("Second")
    second.wid = first.wid
    layout.addWidget(first)
    layout.addWidget(second)
    with pytest.raises(mock_anki.ProtocolError, match="duplicate tree node id"):
        mock_anki.serialize_widget(root)

    combo = QComboBox()
    combo.addItem("First", "duplicate")
    combo.addItem("Second", "duplicate")
    with pytest.raises(mock_anki.ProtocolError, match="duplicate combo option id"):
        mock_anki.serialize_widget(combo)

    grid = QGridLayout()
    repeated = QLabel("Repeated")
    grid.addWidget(repeated, 0, 0)
    grid.addWidget(repeated, 1, 0)
    with pytest.raises(mock_anki.ProtocolError, match="duplicate grid cell id"):
        mock_anki.serialize_widget(grid)


def test_legacy_events_use_strict_actions_and_real_button_click(anki):
    from aqt.qt import QPushButton

    button = QPushButton("Toggle")
    button.setCheckable(True)
    observed = []
    button.clicked.connect(
        lambda checked: observed.append((checked, button.isChecked())))

    mock_anki._apply_events({
        "events": [{"id": button.wid, "click": True}],
    })

    assert observed == [(True, True)]


def test_combo_option_with_empty_item_data_gets_a_usable_id(anki):
    """Qt lets an item carry empty data, and the add-on uses that for a "no
    explicit choice" row (the AI effort dropdown's "Default"). Protocol v1 needs a
    nonempty id, and an id the serializer advertises has to be one the resolver can
    match: an empty one made the whole AI Backends dialog unrepresentable, which
    took the browser demo's session down with it."""
    from aqt.qt import QComboBox, QVBoxLayout, QWidget
    import mock_anki

    root = QWidget()
    layout = QVBoxLayout(root)
    combo = QComboBox()
    combo.addItem("Default (medium)", "")     # the empty-data row
    combo.addItem("high", "high")
    layout.addWidget(combo)

    tree = mock_anki.serialize_widget(root)
    node = next(n for n in tree["nodes"] if n["id"] == combo.wid)
    ids = [option["id"] for option in node["options"]]

    assert all(ids), f"every option needs a nonempty id, got {ids}"
    assert len(ids) == len(set(ids))
    # The resolver has to agree with what the tree advertised, or the id is unusable.
    assert [option_id for option_id, _ in mock_anki._combo_options(combo)] == ids

    mock_anki.apply_actions(
        {"actions": [{"type": "select-option", "id": combo.wid, "option_id": ids[0]}]},
        allowed_ids={combo.wid})
    assert combo.currentIndex() == 0
    mock_anki.apply_actions(
        {"actions": [{"type": "select-option", "id": combo.wid, "option_id": ids[1]}]},
        allowed_ids={combo.wid})
    assert combo.currentIndex() == 1


def test_a_group_note_header_renders_and_gives_its_note_the_stretch(anki):
    """The header over a change group is a real row, and building it is the one path
    no mock test walked: the group_note tests in test_sync_flows.py all stop at the
    items list. qt_tests/ renders this header with real Qt; the browser demo renders
    it with this mock, so the mock has to answer the same calls real Qt does.
    """
    from internpearls import review
    note = {"kind": "feedback", "note": "make both of these tables", "on": "2026-09-21"}
    items = [("group_note", note, 2),
             ("card", "Example Deck", _changed_card_detail("guid-changed-a")),
             ("sep", "grouped"),
             ("card", "Example Deck", _changed_card_detail("guid-changed-b"))]
    body, boxes, flush = review.build_update_body(
        items, {}, {}, {}, {}, "", lambda: "", "")
    assert body is not None
    header = _find_group_note_row(body)
    assert header.layout().stretch(0) == 1


def _find_group_note_row(body):
    """The group header is the row whose only child is a label carrying the shared
    note's own text."""
    for widget in _walk_widgets(body):
        lay = getattr(widget, "_layout", None)
        if lay is None or lay.count() != 1:
            continue
        child = lay.itemAt(0).widget()
        if (isinstance(child, mock_anki.QLabel)
                and "make both of these tables" in child.text()):
            return widget
    raise AssertionError("no group note row found")


def _extra_dialog(extra):
    def run():
        from aqt.qt import QWidget
        from internpearls.ui import _ask_with_widget
        extra["answer"] = _ask_with_widget(QWidget(), yes_label="Update", extra=extra)
    return run


def test_ask_with_widget_extra_button_accepts_and_records_the_click(anki):
    extra = {"label": "Hold some", "tooltip": "why", "visible": True}

    def respond(p):
        btn = find(p["tree"], t="button", label="Hold some")
        assert btn["tooltip"] == "why"
        return {"events": [{"id": btn["id"], "click": True}]}

    drive(anki, _extra_dialog(extra), respond)
    assert extra["answer"] is True and extra["clicked"] is True
    assert "button" not in extra


def test_ask_with_widget_extra_button_sits_before_the_accept_button(anki):
    extra = {"label": "Hold some", "tooltip": "", "visible": False}
    seen = {}

    def respond(p):
        buttons = [n for n in walk(p["tree"]) if n.get("t") == "button"]
        seen["labels"] = [b["label"] for b in buttons]
        seen["hold_visible"] = next(b for b in buttons if b["label"] == "Hold some")["visible"]
        return {"events": [{"id": find(p["tree"], t="button", label="Update")["id"],
                            "click": True}]}

    drive(anki, _extra_dialog(extra), respond)
    assert seen["labels"] == ["Hold some", "Update", "Cancel"]
    assert seen["hold_visible"] is False
    assert extra["answer"] is True and not extra.get("clicked")


def test_declined_dialog_lists_later_cards_first_in_one_group(anki):
    """An old Skip and a Later card are one group, listed ahead of the declines."""
    from internpearls import config
    config.save_declined({
        "g0": {"state": "never", "front": "front z", "deck": "IP::A",
               "decided": "2026-08-01", "hash": ""},
        "g1": {"state": "skip", "front": "front a", "deck": "IP::A",
               "decided": "2026-08-01", "hash": ""},
        "g2": {"state": "held", "front": "front b", "deck": "IP::A",
               "decided": "2026-09-23", "hash": ""}})
    lines = _all_text(_snapshot_declined_dialog(anki)).split("\n")
    assert lines.count("Later") == 1
    later, never = lines.index("Later"), lines.index("Never imported")
    assert later < lines.index("front a") < never
    assert later < lines.index("front b") < never


def test_declined_dialog_puts_a_legacy_skip_entry_under_later(anki):
    """Loading migrates every skip, but the dialog does not depend on that."""
    from internpearls import dialogs
    assert dialogs._decline_group({"state": "skip"}) == "held"
    assert dialogs._decline_group({"state": "keep"}) == "keep"
    assert dialogs._decline_group("not a dict") is None


def _folded_group_body(kind="changed", note_kind="maintainer", n=5,
                       status_line=lambda: "", on_review=None):
    from internpearls import review
    note = {"kind": note_kind, "note": "one change across these cards", "on": "2026-09-27"}
    items = [("group_note", note, n)]
    for i in range(n):
        items += [("sep", "grouped"),
                  ("card", "Example Deck", _card_detail(
                      f"guid-g{i}", kind, **({"was": {"Back": "old"}} if kind == "changed"
                                             else {})))]
    decisions, touched, opened = {}, set(), set()
    body, _boxes, _flush = review.build_update_body(
        items, {}, {}, {}, decisions, "", status_line, "", touched, opened=opened,
        on_review=on_review)
    return body, decisions, touched, opened


def _button(body, text):
    return next(w for w in _walk_widgets(body)
                if isinstance(w, mock_anki.QPushButton) and w.text() == text)


def test_a_group_decision_reaches_members_that_were_never_built(anki):
    body, decisions, touched, _opened = _folded_group_body()
    _button(body, "Keep all yours").click()
    assert decisions == {f"guid-g{i}": "keep" for i in range(5)}
    assert touched == {f"guid-g{i}" for i in range(5)}
    _button(body, "Apply all").click()
    assert decisions == {}, "back to the default leaves no entry, like a row's own control"


def test_a_new_card_group_offers_import_all_and_later_all(anki):
    from internpearls import review
    assert review._GROUP_OPTIONS["new"] == [("import", "Import all"),
                                            ("held", "Later all")]
    body, decisions, _t, _o = _folded_group_body(kind="new")
    _button(body, "Later all").click()
    assert set(decisions.values()) == {"held"} and len(decisions) == 5


def test_a_changed_card_group_offers_later_all_too(anki):
    from internpearls import review
    assert review._GROUP_OPTIONS["changed"] == [
        ("apply", "Apply all"), ("keep", "Keep all yours"), ("held", "Later all")]
    body, decisions, _t, _o = _folded_group_body()
    _button(body, "Later all").click()
    assert decisions == {f"guid-g{i}": "held" for i in range(5)}
    assert not any(w.text() == "Never" for w in _walk_widgets(body)
                   if isinstance(w, mock_anki.QPushButton))


@pytest.mark.parametrize("expanded", [False, True])
@pytest.mark.parametrize("kind", ["new", "changed"])
def test_later_all_notifies_once_after_deciding_two_hundred_cards(
        anki, monkeypatch, expanded, kind):
    from internpearls import review
    status_calls, review_calls, listener_calls = [], [], []
    decision_cell = review.decision_cell

    def counted_cell(options, state, on_change, card_label=""):
        cell = decision_cell(options, state, on_change, card_label)
        if any(label == "Later all" for _, label in options):
            set_state = cell.set_state

            def refresh(value):
                listener_calls.append(dict(decisions))
                set_state(value)

            cell.set_state = refresh
        return cell

    monkeypatch.setattr(review, "decision_cell", counted_cell)
    decisions = {}
    body, decisions, touched, _opened = _folded_group_body(
        kind=kind, n=200,
        status_line=lambda: (status_calls.append(dict(decisions)) or ""),
        on_review=lambda: review_calls.append(dict(decisions)))
    if expanded:
        _button(body, "Show 200 cards").click()
    status_calls.clear()
    review_calls.clear()
    listener_calls.clear()

    _button(body, "Later all").click()

    expected = {f"guid-g{i}": "held" for i in range(200)}
    assert decisions == expected
    assert touched == set(expected)
    if expanded:
        cells = [w for w in _walk_widgets(body)
                 if {"never", "frozen"} & set(getattr(w, "buttons", {}))]
        assert len(cells) == 200
        assert all(cell.buttons["held"].isChecked() for cell in cells)
    assert status_calls == [expected]
    assert review_calls == [expected]
    assert listener_calls == [expected]


def test_expanding_a_folded_group_leaves_its_cards_unopened(anki):
    from internpearls import review
    refreshes, status_calls, review_calls = [], [], []
    body, decisions, touched, opened = _folded_group_body(
        n=10, status_line=lambda: (status_calls.append(True) or ""),
        on_review=lambda: (review_calls.append(True), [fn() for fn in refreshes]))
    kinds = {f"guid-g{i}": "changed" for i in range(10)}
    extra, refresh = review.hold_control({}, kinds, lambda: touched | opened, decisions)
    extra["button"] = button = mock_anki.QPushButton(extra["label"])
    refreshes.append(refresh)
    status_calls.clear()
    assert opened == set()
    _button(body, "Show 10 cards").click()
    assert button.text() == "Update, and leave 10 unopened for later"
    assert button.isVisible()
    assert opened == set()
    assert status_calls == [True] and review_calls == [True]


def test_opening_two_folded_group_members_leaves_eight_holdable(anki):
    from internpearls import review
    from internpearls.logic import holdable_guids, unopened_line
    body, decisions, touched, opened = _folded_group_body(n=10)
    _button(body, "Show 10 cards").click()
    carets = [w for w in _walk_widgets(body)
              if isinstance(w, mock_anki.QPushButton) and w.text() == review._CARET_CLOSED]
    assert len(carets) == 10
    for caret in carets[:2]:
        caret.click()
    kinds = {f"guid-g{i}": "changed" for i in range(10)}
    assert opened == {"guid-g0", "guid-g1"}
    assert holdable_guids({}, kinds, touched | opened, decisions) == [
        f"guid-g{i}" for i in range(2, 10)]
    extra, _refresh = review.hold_control({}, kinds, lambda: touched | opened, decisions)
    assert extra["label"] == "Update, and leave 8 unopened for later"
    assert unopened_line(kinds, touched | opened, kinds).startswith(
        "<b>8 of 10 cards not opened yet</b>, 8 of them in folded groups.")


@pytest.mark.parametrize("expanded", [False, True])
@pytest.mark.parametrize("label, state", [("Later all", "held"), ("Apply all", "apply")])
def test_deciding_all_folded_group_members_leaves_none_holdable(anki, expanded, label, state):
    from internpearls import review
    from internpearls.logic import holdable_guids, unopened_line
    body, decisions, touched, opened = _folded_group_body(n=10)
    if expanded:
        _button(body, "Show 10 cards").click()
    _button(body, label).click()
    kinds = {f"guid-g{i}": "changed" for i in range(10)}
    assert touched == set(kinds)
    assert decisions == ({g: "held" for g in kinds} if state == "held" else {})
    assert holdable_guids({}, kinds, touched | opened, decisions) == []
    assert unopened_line(kinds, touched | opened, kinds) == ""


def test_a_feedback_group_is_not_folded(anki):
    from internpearls import review
    note = {"kind": "feedback", "note": "make these tables"}
    items = [("group_note", note, 6)] + [
        x for i in range(6) for x in (("sep", "grouped"),
                                      ("card", "D", _card_detail(f"g{i}", "changed")))]
    assert review.folded_guids(items) == {}
    items[0] = ("group_note", dict(note, kind="maintainer"), 6)
    assert set(review.folded_guids(items)) == {f"g{i}" for i in range(6)}


def test_unopened_line_counts_rows_left_and_those_folded():
    from internpearls.logic import unopened_line
    kinds = {"a": "new", "b": "changed", "c": "changed", "r": None}
    line = unopened_line(kinds, {"a"}, {"b"})
    assert line.startswith("<b>2 of 3 cards not opened yet</b>, 1 of them in folded groups.")
    assert "Showing a group's cards does not count them as opened." in line
    assert unopened_line(kinds, {"a", "b", "c"}, ()) == ""


def test_unopened_line_says_how_many_carry_an_earlier_decision():
    from internpearls.logic import unopened_line
    kinds = {"a": "new", "b": "changed", "c": "changed", "d": "new"}
    reg = {"a": {"state": "never"}, "b": {"state": "keep"}, "d": {"state": "keep"}}
    line = unopened_line(kinds, set(), {"c"}, reg)
    assert line.startswith("<b>4 of 4 cards not opened yet</b>, 1 of them in folded "
                           "groups and 2 already decided on an earlier update.")
    assert "already decided" not in unopened_line(kinds, set(), (), {})


def test_unopened_line_counts_a_row_seeded_at_later_as_decided(anki):
    """A seeded Later row is a decision from an earlier update, though the registry
    alone (a held entry) does not say so; the screen's seeded `decisions` does."""
    from internpearls.logic import unopened_line
    kinds = {"a": "new", "b": "changed", "c": "new"}
    reg = {"a": {"state": "held", "migrated": True}, "b": {"state": "keep"}}
    line = unopened_line(kinds, set(), (), reg, {"a": "held", "b": "keep"})
    assert line.startswith("<b>3 of 3 cards not opened yet</b>, 2 already decided on "
                           "an earlier update.")
    assert "1 already decided" in unopened_line(kinds, set(), (), reg)


def _filter_body(n_new=12, n_changed=12, group=0, group_kind=None):
    """An update body over `n_new` new and `n_changed` changed cards, plus a folded
    group of `group` cards (alternating kinds unless `group_kind` fixes one)."""
    from internpearls import review
    rows = [("card", "Example Deck", _card_detail(
        f"new-{i}", "new", fields=[("Front", f"New card {i}?"), ("Back", "A")]))
        for i in range(n_new)]
    rows += [("card", "Example Deck", _card_detail(
        f"chg-{i}", "changed", was={"Back": "old"},
        fields=[("Front", f"Changed card {i}?"), ("Back", "A")]))
        for i in range(n_changed)]
    items = [("header", "Example Deck")]
    for i, row in enumerate(rows):
        items += ([("sep",)] if i else []) + [row]
    if group:
        note = {"kind": "maintainer", "note": "one change across these cards"}
        items += [("header", "Grouped Deck"), ("group_note", note, group)]
        for i in range(group):
            kind = group_kind or ("changed" if i % 2 else "new")
            items += [("sep", "grouped"), ("card", "Grouped Deck", _card_detail(
                f"grp-{i}", kind, **({"was": {"Back": "old"}} if kind == "changed"
                                     else {}),
                fields=[("Front", f"Grouped card {i}?"), ("Back", "A")]))]
    decisions, touched = {}, set()
    body, _boxes, _flush = review.build_update_body(
        items, {}, {}, {}, decisions, "", lambda: "", "", touched)
    return body, decisions, touched


def _stream(body):
    from internpearls import widgets
    return next(w for w in _walk_widgets(body) if isinstance(w, widgets.StreamingList))


def _bar(body):
    from internpearls import widgets
    return next((w for w in _walk_widgets(body) if isinstance(w, widgets.FilterBar)),
                None)


def _count_line(bar):
    return bar._count.text() if bar._count.isVisible() else ""


def _push_buttons(body, text):
    return [w for w in _walk_widgets(body)
            if isinstance(w, mock_anki.QPushButton) and w.text() == text]


def test_the_filter_bar_appears_from_twenty_cards_and_not_before(anki):
    assert _bar(_filter_body(10, 9)[0]) is None
    bar = _bar(_filter_body(10, 10)[0])
    assert bar is not None
    assert list(bar.options.buttons) == ["all", "new", "changed", "held", "unreviewed"]
    assert bar.search._placeholder == "Search cards"


def test_the_filter_bar_sits_above_the_list_not_inside_it(anki):
    body, _d, _t = _filter_body()
    children = body._layout._children
    assert children.index(_bar(body)) < children.index(_stream(body))


def test_a_filter_narrows_the_list_and_all_restores_it(anki):
    body, _d, _t = _filter_body(12, 12)
    bar, stream = _bar(body), _stream(body)
    full = stream.total()
    assert _count_line(bar) == ""
    bar.options.buttons["changed"].click()
    assert stream.total() == 1 + 12 + 11    # header, rows, hairlines between them
    assert _count_line(bar) == "Showing 12 of 24 cards"
    bar.options.buttons["all"].click()
    assert stream.total() == full
    assert _count_line(bar) == ""


def test_search_narrows_the_list_after_the_typing_delay(anki):
    body, _d, _t = _filter_body(12, 12)
    bar, stream = _bar(body), _stream(body)
    bar.search.setText("CHANGED CARD 7")
    assert _count_line(bar) == "", "the list waits for typing to stop"
    bar._timer._timer.fire()
    assert stream.total() == 2
    assert _count_line(bar) == "Showing 1 of 24 cards"
    bar.search.setText("")
    bar._timer._timer.fire()
    assert _count_line(bar) == ""


def test_decisions_survive_switching_filters_back_and_forth(anki):
    body, decisions, touched = _filter_body(12, 12)
    bar = _bar(body)
    _find_decision_cell(body).buttons["held"].click()   # not the filter bar's Later tab
    later = dict(decisions)
    assert len(later) == 1
    for mode in ("changed", "held", "new", "all"):
        bar.options.buttons[mode].click()
    assert decisions == later
    assert touched == set(later)
    checked = [w for w in _walk_widgets(body) if hasattr(w, "buttons")
               and "all" not in w.buttons
               and "held" in w.buttons and w.buttons["held"].isChecked()]
    assert len(checked) == 1, "the rebuilt row still shows the decision"


def test_not_reviewed_leaves_out_rows_the_learner_decided(anki):
    body, _decisions, touched = _filter_body(12, 12)
    bar, stream = _bar(body), _stream(body)
    _find_decision_cell(body).buttons["held"].click()   # not the filter bar's Later tab
    bar.options.buttons["unreviewed"].click()
    assert _count_line(bar) == "Showing 23 of 24 cards"
    shown = {it[2]["guid"] for it in stream._items if it[0] == "card"}
    assert len(shown) == 23 and not shown & touched


def test_feedback_typed_before_a_filter_is_kept_after_it(anki):
    body, _decisions, _touched = _filter_body(12, 12)
    bar = _bar(body)
    _button(body, "Never").click()
    _find_feedback_box(body).setPlainText("this one is off")
    bar.options.buttons["held"].click()
    assert _find_feedback_box(body) is None
    bar.options.buttons["all"].click()
    assert _find_feedback_box(body).toPlainText() == "this one is off"


def test_a_group_decision_still_lands_after_a_filter_rebuilt_the_list(anki):
    body, decisions, touched = _filter_body(8, 8, group=8, group_kind="changed")
    bar = _bar(body)
    bar.options.buttons["changed"].click()
    bar.options.buttons["all"].click()
    _push_buttons(body, "Keep all yours")[0].click()
    assert decisions == {f"grp-{i}": "keep" for i in range(8)}
    assert touched == set(decisions)


def test_a_folded_group_under_a_filter_counts_and_builds_only_matches(anki):
    body, _decisions, _touched = _filter_body(6, 6, group=8)
    bar, stream = _bar(body), _stream(body)
    assert _push_buttons(body, "Show 8 cards")
    bar.options.buttons["changed"].click()
    assert _push_buttons(body, "Show 4 cards") and not _push_buttons(body, "Show 8 cards")
    _push_buttons(body, "Show 4 cards")[0].click()
    shown = " ".join(w.text() for w in _walk_widgets(body)
                     if isinstance(w, mock_anki.QLabel))
    assert "Grouped card 1?" in shown and "Grouped card 0?" not in shown
    bar.options.buttons["held"].click()
    assert stream.total() == 0


def test_the_list_stays_lazy_under_a_filter(anki):
    body, _d, _t = _filter_body(150, 150)
    bar, stream = _bar(body), _stream(body)
    bar.options.buttons["changed"].click()
    assert stream.total() > stream.shown() > 0


def test_the_scope_offer_escapes_what_the_manifest_suggests(anki):
    from internpearls import dialogs
    anki.gui.interactive = True
    captured = {}

    def respond(p):
        captured.setdefault("tree", p["tree"])
        return {"events": [{"id": find(p["tree"], t="button", label="Cancel")["id"],
                            "click": True}]}

    drive(anki, lambda: dialogs._offer_manifest_scope(
        {"scope_tag": "Tag<i>", "export_deck": "Deck <b>bold</b> & more"}), respond)
    texts = _all_text(captured["tree"])
    assert "<b>Tag&lt;i&gt;</b>" in texts
    assert "<b>Deck &lt;b&gt;bold&lt;/b&gt; &amp; more</b>" in texts
