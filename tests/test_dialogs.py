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
    check = _bare_deck_row("Intern Pearls::Intern Custom::Pharm", "Pharm")
    assert check["label"] == "Pharm"
    assert check["tooltip"] == "Intern Pearls::Intern Custom::Pharm"


def test_a_very_long_deck_label_is_elided_rather_than_widening_the_dialog():
    from internpearls import dialogs
    long_name = "Regional::" + "Ultrasound guided lower limb blocks in detail"
    check = _bare_deck_row(long_name, long_name)
    assert len(check["label"]) <= dialogs._DECK_LABEL_MAX
    assert "…" in check["label"]
    # Elided from the middle: both ends carry meaning, the parent that disambiguates it
    # and the deck's own name.
    assert check["label"].startswith("Regional::") and check["label"].endswith("detail")
    assert check["tooltip"] == long_name


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
    long_name = "Regional::" + "Ultrasound guided lower limb blocks in detail"
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


def test_settings_saved_summary_reports_the_feedback_toggle(anki):
    """The saved-summary reported sync/add-on-update/dim lines but never the
    card-feedback toggle, the one setting invisible until the next run."""
    from internpearls import dialogs

    anki.gui.interactive = True

    def respond(p):
        if p["kind"] == "dialog":
            feedback = find(p["tree"], t="check",
                            label="Let me flag problems with cards as they sync")
            save = find(p["tree"], t="button", label="Save")
            return {"events": [{"id": feedback["id"], "value": True},
                               {"id": save["id"], "click": True}]}
        assert "flag problems" in p["text"]
        return {}

    drive(anki, dialogs.open_settings, respond)


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
        [("import", "Import"), ("skip", "Skip for now"), ("never", "Never")],
        "import", chosen.append)
    cell.buttons["skip"].click()
    assert chosen == ["skip"]
    cell.set_state("never")
    assert cell.buttons["never"].isChecked()


def test_new_chip_kinds_have_labels_and_roles(anki):
    from internpearls import widgets
    assert widgets.CHIPS["skipped"] == "SKIPPED"
    assert widgets.CHIPS["kept"] == "KEPT YOURS"
    assert widgets._ROLES["skipped"] == "retired"
    assert widgets._ROLES["kept"] == "retired"


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
    return next(w for w in _walk_widgets(body) if hasattr(w, "buttons"))


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


def test_new_card_row_offers_import_skip_never(anki):
    body, boxes, flush, decisions = _build_body_with_one_new_card()
    cell = _find_decision_cell(body)
    assert set(cell.buttons) == {"import", "skip", "never"}
    assert cell.buttons["import"].isChecked()


def test_choosing_skip_reveals_the_feedback_box_and_records_the_decision(anki):
    body, boxes, flush, decisions = _build_body_with_one_new_card()
    cell = _find_decision_cell(body)
    cell.buttons["skip"].click()
    assert decisions == {"guid-new-a": "skip"}
    box = _find_feedback_box(body)
    assert box is not None and box.isVisible()


def test_choosing_never_collapses_the_row(anki):
    body, boxes, flush, decisions = _build_body_with_one_new_card()
    cell = _find_decision_cell(body)
    cell.buttons["never"].click()
    assert decisions == {"guid-new-a": "never"}
    assert "won't be offered again" in _row_texts(body)


def test_changed_card_row_offers_apply_and_keep_only(anki):
    body, boxes, flush, decisions = _build_body_with_one_changed_card()
    cell = _find_decision_cell(body)
    assert set(cell.buttons) == {"apply", "keep"}


def test_predeclined_detail_renders_its_chips_and_preset_state(anki):
    detail = _new_card_detail(declined_state="skip", changed_since_decline=True)
    body, boxes, flush, decisions = _build_body(details=[detail])
    cell = _find_decision_cell(body)
    assert cell.buttons["skip"].isChecked()
    texts = _row_texts(body)
    assert "SKIPPED" in texts and "UPDATED" in texts
    # decisions is the interface the next task persists verbatim, so the preset state
    # has to actually land in the dict, not just drive what the control shows.
    assert decisions == {"guid-new-a": "skip"}
    cell.buttons["import"].click()
    assert decisions == {}


def test_returning_to_default_pops_the_guid_and_unstrikes_the_row(anki):
    """decisions stays sparse: choosing a non-default state records it, and clicking
    back to the row's default removes the entry rather than writing it back in."""
    body, boxes, flush, decisions = _build_body_with_one_new_card()
    cell = _find_decision_cell(body)
    cell.buttons["never"].click()
    assert decisions == {"guid-new-a": "never"}
    cell.buttons["import"].click()
    assert decisions == {}


def test_returning_to_default_closes_an_empty_box_and_restores_add_note(anki):
    """An empty feedback box a decline opened closes again on return to the row's
    default, since nothing was actually written into it worth keeping open, and the
    quiet Add note affordance comes back so feedback is still reachable."""
    body, boxes, flush, decisions = _build_body_with_one_new_card()
    cell = _find_decision_cell(body)
    cell.buttons["skip"].click()
    box = _find_feedback_box(body)
    assert box.isVisible()
    cell.buttons["import"].click()
    assert not box.isVisible(), "an empty box should close again on return to default"
    add_note = next(w for w in _walk_widgets(body)
                    if getattr(w, "text", None) and w.text() == "Add note")
    assert add_note.isVisible(), "Add note should reappear once the box is closed"


def test_typed_but_unsaved_text_keeps_the_box_open_on_return_to_default(anki):
    """The empty-box-closes fix must not also swallow a note she's mid-typing: text
    sitting in the box, even before it has reached `flags`, keeps it open."""
    body, boxes, flush, decisions = _build_body_with_one_new_card()
    cell = _find_decision_cell(body)
    cell.buttons["skip"].click()
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
    details[55] = _new_card_detail(guid="guid-55", declined_state="skip")
    items = [("card", "Example Deck", d) for d in details]
    flags, new_index, decisions = {}, {}, {}
    body, boxes, flush = review.build_update_body(
        items, {}, flags, new_index, decisions, "", lambda: "", "")
    assert decisions.get("guid-55") == "skip", (
        "a predeclined card past the first StreamingList batch never reached decisions")


def test_import_row_offers_a_quiet_add_note_that_reveals_the_box(anki):
    """An Import/Apply row is not declined, so its box starts hidden, but a small "Add
    note" affordance can still reveal it (flag-without-decline)."""
    body, boxes, flush, decisions = _build_body_with_one_new_card()
    box = _find_feedback_box(body)
    assert box is not None and not box.isVisible()
    add_note = next(w for w in _walk_widgets(body)
                    if getattr(w, "text", None) and w.text() == "Add note")
    add_note.click()
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
    cell.buttons["skip"].click()
    assert len(calls) > before, "status_line was not recomputed after a decision change"
