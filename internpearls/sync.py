"""The deck-sync flows: source resolution, Sync decks, and Import single deck.

_run_sync is the one implementation of the history-preserving sequence (fix note
types, snapshot protected fields, remap and import, restore, persist versions) —
shared by the interactive Sync decks flow and the unattended auto-sync poll in
background.py, so the part that matters for not losing anyone's review history
exists exactly once.
"""
import json
import os
import tempfile

from aqt import mw
from aqt.utils import getFile

from .collection import (_apply_deck, _apply_template_changes, _ensure_notetypes,
                         _her_front_to_guid, _her_guid_to_deck, _her_guid_to_nid,
                         _import_apkg, _pre_sync_backup_or_confirm_skip, _restore,
                         _snapshot, _template_changes, apply_deck_moves,
                         archive_notes, carry_over_protected_fields)
from .config import (ADDON_VERSION, INSTALLED, RETIRED_DECK_LEAF, RETIRED_TAG_LEAF,
                     SUPPORTED_MANIFEST_SCHEMA, _cfg, _load_json, _save_json)
from .logic import (bullets, decks_to_update, find_deck_moves_needed,
                    find_retired_in_collection, manifest_needs_newer_addon,
                    remap_cards, write_personalized)
from .net import _CONNECT_TIMEOUT, _DOWNLOAD_TIMEOUT, _gh_raw
from .ui import _ask, _ask_scrollable, _info, _safe, _warn, wait_cursor


def _fetch_manifest(cfg, timeout=_CONNECT_TIMEOUT):
    """Return (manifest, fetch_apkg, source_label) where fetch_apkg(deck) -> local
    .apkg path.

    A GitHub source needs only the repo; the token is optional (blank is fine for a
    public repo — _http_get simply sends no Authorization header). `timeout` bounds the
    manifest fetch itself; deck downloads always get the generous _DOWNLOAD_TIMEOUT,
    since they only happen after first contact already proved the source reachable.
    """
    if cfg["gh_repo"]:
        manifest = json.loads(_gh_raw(cfg["gh_repo"], "manifest.json",
                                      cfg["gh_token"], cfg["gh_ref"], timeout=timeout))

        def fetch(d):
            data = _gh_raw(cfg["gh_repo"], d["apkg"], cfg["gh_token"], cfg["gh_ref"],
                           timeout=_DOWNLOAD_TIMEOUT)
            # d["apkg"] may include subfolders (e.g. decks/Foo.apkg); flatten to just the
            # filename for the scratch download location, since /tmp/decks/ won't exist.
            tmp = os.path.join(tempfile.gettempdir(), os.path.basename(d["apkg"]))
            with open(tmp, "wb") as fh:
                fh.write(data)
            return tmp

        return manifest, fetch, "GitHub"

    if cfg["decks_dir"] and os.path.isdir(cfg["decks_dir"]):
        manifest = _load_json(os.path.join(cfg["decks_dir"], "manifest.json"), None)

        def fetch(d):
            return os.path.join(cfg["decks_dir"], d["apkg"])

        return manifest, fetch, "local folder"

    return None, None, None


@_safe
def sync_decks():
    cfg = _cfg()
    try:
        with wait_cursor():
            manifest, fetch, source = _fetch_manifest(cfg)
    except Exception as e:
        _warn(f"Couldn't reach the deck source: {e}<br><br>"
              "Open <b>Intern Pearls → Manage decks</b> and use Change source to check "
              "your GitHub token or local folder.")
        return
    if not manifest:
        _warn("No deck source configured yet.<br><br>"
              "Open <b>Intern Pearls → Manage decks</b> and use Configure source.")
        return
    if manifest_needs_newer_addon(manifest, SUPPORTED_MANIFEST_SCHEMA):
        _warn(
            f"This deck source needs a newer version of Intern Pearls Deck Tools than "
            f"the one installed (v{ADDON_VERSION}).<br><br>"
            "Update the add-on first — <b>Intern Pearls → Advanced → Check for add-on "
            "updates</b> — then try Sync decks again. Syncing against a manifest format "
            "this version doesn't understand is refused rather than attempted, so "
            "nothing here has been touched."
        )
        return

    installed = _load_json(INSTALLED, {})
    todo = decks_to_update(manifest, installed, cfg["excluded"])
    if not todo:
        _info(f"All selected decks are up to date (source: {source}).")
        return

    def _line(d):
        short = d["name"].split("::")[-1]
        cards = d.get("cards")
        tag = "new deck" if d["name"] not in installed else None
        detail = ", ".join(x for x in (f"{cards} cards" if cards is not None else None, tag) if x)
        return f"{short} ({detail})" if detail else short

    if not _ask(
        "Update these decks?\n\n  • " + "\n  • ".join(_line(d) for d in todo) +
        "\n\nYour review history and any personal notes on existing cards are kept "
        "(matched by card, not overwritten). A backup is taken automatically first, "
        "so this is safe to undo if anything looks wrong afterward."
    ):
        return
    proceed, backed_up = _pre_sync_backup_or_confirm_skip(cfg["export_deck"])
    if not proceed:
        return

    # A visible progress window while each deck downloads and imports: the fetches run
    # on the main thread here (unlike auto-sync's background poll), and a multi-deck
    # sync on a slow link otherwise looks like a hang.
    mw.progress.start(label="Syncing decks", immediate=True)
    try:
        results, restored, tpl_changes, _ = _run_sync(
            cfg, manifest, fetch, todo, installed,
            on_progress=lambda i, n, name: mw.progress.update(
                label=f"Syncing {name} ({i} of {n})"))
    finally:
        mw.progress.finish()
    _offer_template_changes(tpl_changes)
    fields_line = (f"Preserved fields restored on {restored} card(s).<br><br>"
                  if restored else "")
    backup_line = (
        "A pre-sync backup of the Intern Pearls deck was saved; use "
        "<i>Advanced → Import intern pearls deck</i> to revert to it if needed."
        if backed_up else
        "No pre-sync backup was taken this time (nothing to back up yet, or it "
        "failed and you chose to continue).")
    _info(f"<b>Sync complete</b> (source: {source})" + bullets(results) +
          fields_line + backup_line)


def _run_sync(cfg, manifest, fetch, todo, installed, on_progress=None,
              defer_template_changes=False):
    """Apply every deck in `todo`: fix note types, snapshot protected fields, remap and
    import each deck (keeping the learner's scheduling), restore the snapshotted fields,
    and persist the new installed versions.

    The caller must already have confirmed (if interactive) and taken a backup — this is
    the one place the actual history-preserving sequence lives, shared by the interactive
    Sync decks flow and the unattended auto-sync poll, so there's exactly one
    implementation of the part that matters for not losing anyone's review history.
    Returns (results, restored, tpl_changes, deferred): per-deck outcome lines, the
    note-restore count, template/CSS changes detected in the imported decks (for the
    interactive caller to offer applying — imports never propagate them on their own,
    see _import_apkg), and the names of decks skipped because of such a change.
    `on_progress(i, total, deck_short_name)`, if given, fires before each deck is
    fetched and applied; the interactive flow uses it to drive Anki's progress window,
    the unattended auto-sync poll passes nothing.

    `defer_template_changes` is the unattended-caller policy: applying a template bumps
    the collection schema (a one-time full AnkiWeb sync), which must never happen
    without someone there to consent — so auto-sync passes True, and a deck whose
    update includes a template change is left un-imported and NOT marked installed,
    keeping it pending for the next interactive Sync decks where the user can decide.
    """
    aliases = manifest.get("front_aliases", {})   # from the (private) manifest, not config
    _ensure_notetypes()
    snap = _snapshot(cfg["protected"], cfg["scope_tag"])
    her = _her_front_to_guid(cfg["scope_tag"])
    results, tpl_changes, deferred = [], {}, []
    for i, d in enumerate(todo, 1):
        short = d["name"].split("::")[-1]
        if on_progress:
            on_progress(i, len(todo), short)
        try:
            src = fetch(d)
            tpl = _template_changes(src)
            if tpl and defer_template_changes:
                deferred.append(d["name"])
                results.append(f"• <b>{short}</b>: includes a card-template update, "
                               "waiting for a manual Sync decks")
                continue
            tpl_changes.update(tpl)
            in_place, as_new = _apply_deck(src, aliases, her)
            installed[d["name"]] = d["version"]
            results.append(f"✓ <b>{short}</b>: {in_place} kept history, {as_new} new")
        except Exception as e:
            results.append(f"✗ <b>{short}</b>: {e}")
    _save_json(INSTALLED, installed)
    restored = _restore(snap)
    mw.reset()
    return results, restored, tpl_changes, deferred


def _offer_template_changes(tpl_changes):
    """Interactive follow-up to a sync that found template/CSS changes: explain the
    one-time full-sync consequence, apply only if the user says yes. Declining is
    saying "keep my current card look" — the deck content itself already imported, and
    the next template change will offer again.
    """
    if not tpl_changes:
        return
    names = ", ".join(f"<b>{n}</b>" for n in sorted(tpl_changes))
    if _ask(
        f"This update also changes how some cards look (template or styling) for: "
        f"{names}.<br><br>Apply the new look now? Anki treats this as a schema "
        "change, so your next AnkiWeb sync will be a one-time full sync — choose "
        "\"Upload to AnkiWeb\" when asked.<br><br>Choosing No keeps your current "
        "card appearance; your review history and card content are unaffected "
        "either way."
    ):
        _apply_template_changes(tpl_changes)


@_safe
def reconcile_decks():
    """Find retired cards still in the learner's collection and archive them, and
    relocate any cards a pure deck reorg has moved to a new deck.

    When a deck splits, merges, or reword-replaces a card, the old card's GUID leaves
    the canonical set — but a sync only ever ADDS the replacements, it never removes her
    copy of the old one. So the old card lingers, duplicated against its replacements in
    every review. This reads the retirement ledger (shipped in the manifest), finds the
    retired cards she still has, carries over any personal notes onto their
    replacement(s), and archives them: moved to a Retired subdeck, suspended, tagged.
    It never deletes anything — the worst a bug here can do is suspend/move a card,
    which is trivially reversible.

    Separately, when a deck source reorganizes a card into a different deck without
    changing its identity (e.g. Local Anesthetics moving into a new Regional deck),
    a normal sync updates the card's content in place but never relocates it — Anki's
    importer only assigns a deck to a brand-new note, never an already-existing one.
    This reads the deck-moves ledger and relocates any card still sitting exactly
    where the source last filed it (find_deck_moves_needed skips anything she's since
    moved herself, so her own organization is never overridden).
    """
    cfg = _cfg()
    try:
        with wait_cursor():
            manifest, _, source = _fetch_manifest(cfg)
    except Exception as e:
        _warn(f"Couldn't reach the deck source: {e}<br><br>"
              "Open <b>Intern Pearls → Manage decks</b> and use Change source to check "
              "your GitHub token or local folder.")
        return
    if not manifest:
        _warn("No deck source configured yet.<br><br>"
              "Open <b>Intern Pearls → Manage decks</b> and use Configure source.")
        return

    her = _her_guid_to_nid(cfg["scope_tag"])
    found = find_retired_in_collection(manifest.get("retired", {}), set(her))
    her_deck = _her_guid_to_deck(cfg["scope_tag"])
    moves = [m for m in find_deck_moves_needed(manifest.get("deck_moves", {}), her_deck)
             if m["guid"] in her]

    if not found and not moves:
        _info("No retired cards or reorganized decks found in your collection — "
              f"nothing to tidy up. (Source: {source}.)")
        return

    tag = f'{cfg["scope_tag"]}::{RETIRED_TAG_LEAF}'
    retired_deck = f'{cfg["export_deck"]}::{RETIRED_DECK_LEAF}'
    # A previous run tags what it archives; skip those so re-running is a no-op on them.
    fresh, already = [], 0
    for r in found:
        if tag in mw.col.get_note(her[r["guid"]]).tags:
            already += 1
        else:
            fresh.append(r)
    if not fresh and not moves:
        _info(f"All {already} retired card(s) in your collection are already archived "
              f"(suspended and moved to <b>{RETIRED_DECK_LEAF}</b>). Nothing more to do.")
        return

    # A big first run (a large reorg landed before Reconcile was run even once) reads as
    # alarming without context — say up front that it's a one-time catch-up, not what to
    # expect going forward, so the length itself doesn't feel like something went wrong.
    catch_up_note = (
        "<i>This looks like a one-time catch-up — likely your first Reconcile since a "
        "larger update. Future runs should be much shorter.</i><br><br>"
        if len(fresh) + len(moves) > 20 else "")

    # Both lists are capped for readability, and the confirmation below uses the
    # scrollable dialog rather than a plain askUser() — a bare QMessageBox has no
    # scroll area, so a long enough uncapped list (dozens of relocated cards from a
    # single reorg, as happened here) can push the Yes/No buttons off-screen with no
    # way to reach them. Capping keeps the dialog itself short in the common case;
    # the scroll area is the structural guarantee that it can never happen again even
    # if some future list grows past the cap.
    lines = [f"{r['identity']} <span style='color:gray;'>"
             f"({r['deck'].split('::')[-1]})</span>" for r in fresh]
    missing = sum(1 for r in fresh
                  if r["superseded_by"] and r["replacements_present"] == 0)
    sync_note = (f"<br><b>Note:</b> {missing} of these don't have their replacement "
                 "cards in your collection yet — run <b>Sync decks</b> first if you "
                 "want the new versions before archiving the old ones."
                if missing else "")
    already_note = f" ({already} more were already archived earlier.)" if already else ""
    archive_block = (
        f"<b>{len(fresh)}</b> retired card(s) are still in your collection — split or "
        "reworded since, with the replacements already added separately, so these "
        f"just duplicate your reviews now.{already_note}"
        + bullets(lines, cap=15) + sync_note
    ) if fresh else ""

    move_lines = [f"{mw.col.get_note(her[m['guid']]).fields[0]} <span "
                  f"style='color:gray;'>→ {m['to'].split('::')[-1]}</span>" for m in moves]
    moves_block = (
        f"<b>{len(moves)}</b> card(s) belong to a deck that's since been reorganized."
        + bullets(move_lines, cap=15)
    ) if moves else ""

    sep = "<br><br>" if fresh and moves else ""
    safety_note = (
        "<br><br>Nothing is deleted. Archived cards keep their review history and can "
        "be brought back anytime by unsuspending them or moving them out of the "
        "Retired deck" +
        (", and any personal notes on them carry over to the replacement first."
         if fresh else ".") +
        " A backup is taken automatically before anything changes."
    )
    if fresh and moves:
        yes_label = "Archive and relocate"
    elif fresh:
        yes_label = "Archive"
    else:
        yes_label = "Relocate"
    if not _ask_scrollable(catch_up_note + archive_block + sep + moves_block + safety_note,
                           yes_label=yes_label):
        return

    proceed, backed_up = _pre_sync_backup_or_confirm_skip(cfg["export_deck"])
    if not proceed:
        return
    carried = carry_over_protected_fields(fresh, her, cfg["protected"])
    n_archived = archive_notes([her[r["guid"]] for r in fresh], retired_deck, tag)
    n_moved = apply_deck_moves(moves, her)
    mw.reset()
    backup_line = ("" if backed_up else
                   "<br><br>(No backup was taken this time — nothing to back up yet, or "
                   "it failed and you chose to continue.)")
    result_lines = []
    if n_archived:
        result_lines.append(
            f"Archived <b>{n_archived}</b> retired card(s) to <b>{retired_deck}</b>: "
            f"suspended and tagged <code>{tag}</code>, review history kept"
            + (f" ({carried} personal note(s) carried over to their replacement)"
               if carried else "") + ". Bring any back by unsuspending it or moving "
            "it out of the Retired deck.")
    if n_moved:
        result_lines.append(f"Moved <b>{n_moved}</b> card(s) to their reorganized deck — "
                            "content and scheduling untouched.")
    _info("<br><br>".join(result_lines) + backup_line)


@_safe
def import_single():
    """Import one hand-picked, spec-authored .apkg outside the configured source.

    For a deck someone sent you directly, or a build you're testing before pushing it
    to the source repo. Does the same personalization, backup, and note-restore Sync
    does, just for one file you choose instead of everything the manifest lists.
    """
    cfg = _cfg()
    src = getFile(mw, "Choose an Intern Pearls .apkg", cb=None,
                  filter="*.apkg", key="internpearls")
    if not src:
        return
    if isinstance(src, (list, tuple)):
        src = src[0]
    aliases = {}
    try:
        manifest, _, _ = _fetch_manifest(cfg)
        if manifest:
            aliases = manifest.get("front_aliases", {})
    except Exception as e:
        if not _ask(f"Couldn't fetch the reworded-front list from your deck source "
                    f"({e}).\n\nWithout it, any card whose front text changed there "
                    "will be treated as new instead of matching your existing card, "
                    "so its history won't carry over. Continue anyway?"):
            return
    _ensure_notetypes()
    her = _her_front_to_guid(cfg["scope_tag"])
    remap, in_place, as_new = remap_cards(src, her, aliases)
    if not _ask(f"{in_place} card(s) will keep their history, {as_new} will be added "
                "as new. A backup is taken automatically first. Import now?"):
        return
    if not _pre_sync_backup_or_confirm_skip(cfg["export_deck"])[0]:
        return
    tpl = _template_changes(src)
    snap = _snapshot(cfg["protected"], cfg["scope_tag"])
    out = src + ".sync.apkg"
    write_personalized(src, remap, out)
    try:
        _import_apkg(out)
    finally:
        try:
            os.remove(out)
        except OSError:
            pass
    restored = _restore(snap)
    mw.reset()
    _offer_template_changes(tpl)
    fields_line = f" Preserved fields restored on {restored} card(s)." if restored else ""
    _info(f"Imported {os.path.basename(src)}: {in_place} kept history, {as_new} new."
          f"{fields_line}")
