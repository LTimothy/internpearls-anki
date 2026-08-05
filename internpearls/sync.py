"""The deck-sync and reconcile flows: source resolution, Sync decks, Reconcile my
decks, the unified Update my decks front door, and Import single deck.

_run_sync is the one implementation of the history-preserving sequence (fix note
types, snapshot protected fields, remap and import, restore, persist versions) —
shared by the interactive Sync decks flow, update_decks(), and the unattended
auto-sync poll in background.py, so the part that matters for not losing anyone's
review history exists exactly once. _reconcile_pending is the equivalent single
source of truth for what "Reconcile my decks" would find pending, shared by
reconcile_decks() and update_decks() so the two can never disagree.
"""
import html
import json
import os
import tempfile

from aqt import mw
from aqt.utils import getFile

from .collection import (_apply_deck, _apply_template_changes, _capture_shipped,
                         _ensure_notetypes, change_note_types,
                         notetype_changes, seed_converted_siblings,
                         _her_front_to_guid, _her_guid_to_deck, _her_guid_to_nid,
                         _her_notes_summary, _import_apkg,
                         _pre_sync_backup_or_confirm_skip, _restore,
                         _snapshot, _template_changes, apply_deck_moves,
                         archive_notes, carry_over_protected_fields,
                         carry_scheduling_forward,
                         installed_matching_collection)
from .config import (ADDON_VERSION, DUPLICATE_TAG_LEAF, INSTALLED, RETIRED_DECK_LEAF,
                     RETIRED_TAG_LEAF, SHIPPED, SUPPORTED_MANIFEST_SCHEMA, _cfg,
                     _load_json, _save_json)
from .logic import (apkg_note_details, apkg_notes, bullets, decks_to_update,
                    feedback_entries, merge_saved_feedback,
                    duplicate_dialog_html, find_deck_moves_needed,
                    find_duplicate_groups, find_retired_in_collection,
                    find_stranded_pairs, manifest_needs_newer_addon,
                    note_display_label, remap_cards, write_personalized)
from .net import _CONNECT_TIMEOUT, _DOWNLOAD_TIMEOUT, _gh_raw
from .review import (clear_saved_feedback, load_saved_feedback,
                     review_new_cards, show_result_with_feedback)
from .ui import _ask, _ask_scrollable, _info, _safe, _warn, cancellable_progress, wait_cursor


# The "Reconcile my decks" QAction, set once by __init__.py right after building the
# menu. Mutated from here and from background.py's auto-sync poll, mirroring
# updates.py's register_update_action/_refresh_update_action_label for the same
# reason: auto-sync only ever applies content on its own (archiving/relocating always
# stays a consented action — see _run_sync's history-preserving-but-additive-only
# design), so without a persistent nudge here, a retired/relocated backlog could pile
# up silently between manual checks, which is exactly the divergence problem this
# whole flow exists to close.
_reconcile_action = None


def register_reconcile_action(action):
    """Called once by __init__.py right after building the menu."""
    global _reconcile_action
    _reconcile_action = action


def _refresh_reconcile_action_label(pending):
    """Show a pending count on the menu item itself, or reset to the plain label once
    there's nothing left to reconcile. No-op before the menu exists — safe to call
    from anywhere that just learned a fresh count.
    """
    if _reconcile_action is None:
        return
    if pending:
        _reconcile_action.setText(f"Reconcile my decks ({pending} pending)")
    else:
        _reconcile_action.setText("Reconcile my decks")


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


def _fetch_manifest_gated(cfg):
    """_fetch_manifest, plus the "you need a newer add-on" schema gate, plus the
    unreachable/unconfigured warnings — all three callers that need a gated fetch
    (Sync decks, Update my decks) want the exact same behavior here, so there's one
    place that can disagree with itself. Returns (manifest, fetch, source) on success,
    or None after already showing the user a warning; the caller should just return.
    """
    try:
        with wait_cursor():
            manifest, fetch, source = _fetch_manifest(cfg)
    except Exception as e:
        _warn(f"Couldn't reach the deck source: {e}<br><br>"
              "Open <b>Intern Pearls → Manage decks</b> and use Change source to check "
              "your GitHub token or local folder.")
        return None
    if not manifest:
        _warn("No deck source configured yet.<br><br>"
              "Open <b>Intern Pearls → Manage decks</b> and use Configure source.")
        return None
    if manifest_needs_newer_addon(manifest, SUPPORTED_MANIFEST_SCHEMA):
        _warn(
            f"This deck source needs a newer version of Intern Pearls Deck Tools than "
            f"the one installed (v{ADDON_VERSION}).<br><br>"
            "Update the add-on first — <b>Intern Pearls → Advanced → Check for add-on "
            "updates</b> — then try again. Syncing against a manifest format this "
            "version doesn't understand is refused rather than attempted, so nothing "
            "here has been touched."
        )
        return None
    return manifest, fetch, source


@_safe
def sync_decks():
    cfg = _cfg()
    fetched = _fetch_manifest_gated(cfg)
    if not fetched:
        return
    manifest, fetch, source = fetched

    installed = installed_matching_collection(_load_json(INSTALLED, {}), cfg["scope_tag"])
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

    # A cancellable, determinate progress window while each deck downloads and
    # imports: the fetches run on the main thread here (unlike auto-sync's
    # background poll), and a multi-deck sync on a slow link otherwise looks like a
    # hang with no way out.
    with cancellable_progress("Syncing decks", len(todo)) as step:
        results, restored, tpl_changes, _, cancelled, collisions, _conv = _run_sync(
            cfg, manifest, fetch, todo, installed,
            on_progress=lambda i, n, name: step(i, f"Syncing {name} ({i} of {n})"))
    _offer_template_changes(tpl_changes)
    fields_line = (f"Preserved fields restored on {restored} card(s).<br><br>"
                  if restored else "")
    fields_line += _collision_note(collisions)
    backup_line = (
        "A pre-sync backup of the Intern Pearls deck was saved; use "
        "<i>Advanced → Import intern pearls deck</i> to revert to it if needed."
        if backed_up else
        "No pre-sync backup was taken this time (nothing to back up yet, or it "
        "failed and you chose to continue).")
    title = "Sync stopped early" if cancelled else "Sync complete"
    stopped_note = ("<br><br>Nothing else was touched; run <b>Sync decks</b> again "
                    "anytime to pick up where this left off." if cancelled else "")
    _info(f"<b>{title}</b> (source: {source})" + bullets(results) +
          fields_line + backup_line + stopped_note)


def _collision_note(collisions):
    """A line naming the cards where her own edit and a source update landed on the
    same field. Hers is kept; this exists so the two versions don't quietly diverge
    with nobody knowing, which is the one thing the three-way restore can't decide on
    its own.
    """
    if not collisions:
        return ""
    fronts = []
    for guid, field in collisions[:10]:
        nid = mw.col.db.scalar("select id from notes where guid = ?", guid)
        if nid:
            fronts.append(f"{mw.col.get_note(nid).fields[0][:70]} ({field})")
    more = f" and {len(collisions) - len(fronts)} more" if len(collisions) > len(fronts) else ""
    return (f"<br><br>On <b>{len(collisions)}</b> card(s), this update changed a field "
            "you had also written in yourself. <b>Your version was kept</b> and the "
            "update to that field was skipped, so nothing you wrote was lost. Worth "
            "passing these on to whoever maintains the decks if you want your wording "
            "folded in, or theirs applied instead:" + bullets(fronts) + more)


def _offer_notetype_changes(changes):
    """Ask before converting the learner's notes to the note type an update ships.

    Declining is a real choice with a real consequence, and it says so: the cards still
    import, but as new notes beside her existing ones, so the history stays on a copy
    that is no longer what the deck teaches. Accepting keeps one card with its history.
    Mirrors _offer_template_changes because it costs the same thing, a one-time full
    AnkiWeb sync, for the same reason.
    """
    if not changes:
        return []
    return change_note_types(changes) if _ask(
        f"<b>{len(changes)}</b> card(s) in this update changed format (a question and "
        "answer became a fill-in-the-blank).<br><br>Move your existing cards to the new "
        "format? They keep their review history and stay one card each. Anki treats "
        "this as a schema change, so your next AnkiWeb sync will be a one-time full "
        "sync, choose \"Upload to AnkiWeb\" when asked.<br><br>Choosing No still "
        "imports them, but as separate new cards, leaving your progress on the old "
        "versions."
    ) else []


def _run_sync(cfg, manifest, fetch, todo, installed, on_progress=None,
              defer_template_changes=False):
    """Apply every deck in `todo`: fix note types, snapshot protected fields, remap and
    import each deck (keeping the learner's scheduling), restore the snapshotted fields,
    and persist the new installed versions.

    The caller must already have confirmed (if interactive) and taken a backup — this is
    the one place the actual history-preserving sequence lives, shared by the interactive
    Sync decks flow and the unattended auto-sync poll, so there's exactly one
    implementation of the part that matters for not losing anyone's review history.
    Returns (results, restored, tpl_changes, deferred, cancelled): per-deck outcome
    lines, the note-restore count, template/CSS changes detected in the imported
    decks (for the interactive caller to offer applying — imports never propagate
    them on their own, see _import_apkg), the names of decks skipped because of such
    a change, and whether `on_progress` asked to stop partway through.
    `on_progress(i, total, deck_short_name)`, if given, fires before each deck is
    fetched and applied and must return a truthy value to continue; the interactive
    flow uses it to drive a cancellable progress window (a False return means the
    learner clicked Cancel), the unattended auto-sync poll passes nothing.

    A False from `on_progress` stops *before* that deck's fetch/import, never
    partway through one, so whatever decks already completed are already fully
    applied — the loop below still runs its snapshot-restore and persists
    `installed` for exactly those, same as a clean finish, just for fewer decks.

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
    results, tpl_changes, deferred, touched = [], {}, [], set()
    converted = 0
    cancelled = False
    for i, d in enumerate(todo, 1):
        short = d["name"].split("::")[-1]
        if on_progress and not on_progress(i, len(todo), short):
            cancelled = True
            break
        try:
            src = fetch(d)
            tpl = _template_changes(src)
            # A note-type conversion is the same class of thing as a template change:
            # it bumps the schema, so it needs consent and must never happen
            # unattended. Same deferral, one deck held back rather than half-applied.
            nt = notetype_changes(src, her, aliases, cfg["scope_tag"])
            if (tpl or nt) and defer_template_changes:
                deferred.append(d["name"])
                results.append(f"• <b>{short}</b>: includes a card-template update, "
                               "waiting for a manual Sync decks")
                continue
            tpl_changes.update(tpl)
            # Before the import, not after: once the note is on the right type, the
            # import matches it by GUID and updates it in place, which is the whole
            # point. Afterwards it would be converting a duplicate.
            changed_nids = _offer_notetype_changes(nt)
            converted += len(changed_nids)
            in_place, as_new, wrote = _apply_deck(src, aliases, her)
            # After the import, not before: the extra cloze cards only exist once the
            # cloze markup has actually landed on the note.
            seed_converted_siblings(changed_nids)
            touched |= wrote
            installed[d["name"]] = d["version"]
            results.append(f"✓ <b>{short}</b>: {in_place} kept history, {as_new} new")
        except Exception as e:
            results.append(f"✗ <b>{short}</b>: {e}")
    _save_json(INSTALLED, installed)
    # Read what the source shipped BEFORE restoring her annotations over it: after
    # _restore, hers is what the note holds, and recording that as the baseline would
    # make her own edit indistinguishable from the source's own value next time.
    shipped = _capture_shipped(cfg["protected"], cfg["scope_tag"], touched)
    restored, collisions = _restore(snap, _load_json(SHIPPED, {}), touched)
    if shipped:
        _save_json(SHIPPED, {**_load_json(SHIPPED, {}), **shipped})
    mw.reset()
    return results, restored, tpl_changes, deferred, cancelled, collisions, converted


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


def _reconcile_pending(manifest, cfg):
    """Everything "Reconcile my decks" would find pending: retired cards still in the
    collection (split into fresh vs. already-archived) and cards sitting in a
    since-reorganized deck. Shared by reconcile_decks() and update_decks() so the two
    can never disagree about what's pending.

    Returns (her, fresh, already, moves, retired_deck, tag, stranded) — `her` is
    {guid: nid} for every note currently under scope_tag, which the caller needs again
    to act on `fresh`/`moves` afterward (or, for update_decks(), to refetch post-sync —
    see its docstring for why that refetch matters). `stranded` is the reworded-card
    pairs she holds both halves of (see find_stranded_pairs); its predecessors are
    archived like `fresh`, but only after their scheduling has been carried forward.
    """
    her = _her_guid_to_nid(cfg["scope_tag"])
    # her_front lets both ledgers act on a card whose GUID no longer matches them (an
    # id_seed change, or a reword that predates the GUID freeze), by its front, the
    # same way remap_cards matches content. Without it a moved card stays stuck at
    # `from` with its new deck re-offered forever, and a retired card is never found
    # to archive, so it duplicates its replacements in every review indefinitely.
    her_front = _her_front_to_guid(cfg["scope_tag"])
    found = find_retired_in_collection(manifest.get("retired", {}), set(her), her_front)
    her_deck = _her_guid_to_deck(cfg["scope_tag"])
    moves = [m for m in find_deck_moves_needed(manifest.get("deck_moves", {}), her_deck,
                                               her_front)
             if m["guid"] in her]

    tag = f'{cfg["scope_tag"]}::{RETIRED_TAG_LEAF}'
    retired_deck = f'{cfg["export_deck"]}::{RETIRED_DECK_LEAF}'
    # A previous run tags what it archives; skip those so re-running is a no-op on them.
    fresh, already = [], 0
    for r in found:
        if tag in mw.col.get_note(her[r["guid"]]).tags:
            already += 1
        else:
            fresh.append(r)
    stranded = [p for p in find_stranded_pairs(manifest.get("superseded_fronts", {}),
                                               her_front)
                if p["guid"] in her and p["successor_guid"] in her
                and tag not in mw.col.get_note(her[p["guid"]]).tags]
    return her, fresh, already, moves, retired_deck, tag, stranded


def _stranded_block(stranded, her):
    """The confirmation section for reworded cards she holds both halves of.

    Worded around what she'll notice (two versions of the same card, progress on the
    one that's out of date) rather than around GUIDs, which is the actual cause but not
    something she should have to know about to say yes to this.
    """
    if not stranded:
        return ""
    lines = [f"{p['front']} <span style='color:gray;'>→ {p['successor_front']}</span>"
             for p in stranded]
    return (f"<b>{len(stranded)}</b> card(s) are in your collection twice, in an older "
            "and a newer wording of the same question, because the wording changed "
            "after you first imported them. Your progress on the older copy moves to "
            "the newer one, then the older copy is archived."
            + bullets(lines, cap=15))


def _merge_stranded(stranded, her, protected, retired_deck, tag):
    """Carry each stranded predecessor's scheduling and personal notes onto its live
    successor, then archive the predecessor. Returns the number of pairs merged.

    Scheduling first, archiving second, and both before the caller's own archive pass:
    a predecessor that somehow failed to hand its history over should still be sitting
    in her review queue afterwards rather than suspended with nothing carrying it.
    Reuses carry_over_protected_fields by describing each pair the way it describes a
    retirement (one predecessor, one replacement), so her Notes field follows the same
    single path here as everywhere else.
    """
    if not stranded:
        return 0
    carry_scheduling_forward(stranded, her)
    carry_over_protected_fields(
        [{"guid": p["guid"], "superseded_by": [p["successor_guid"]]} for p in stranded],
        her, protected)
    archive_notes([her[p["guid"]] for p in stranded], retired_deck, tag)
    return len(stranded)


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

    Kept as an Advanced-menu escape hatch for running just this half on its own;
    "Update my decks" is the recommended front door and runs this right after a sync
    in one pass — see update_decks().
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

    her, fresh, already, moves, retired_deck, tag, stranded = _reconcile_pending(
        manifest, cfg)
    if not fresh and not moves and not stranded:
        _refresh_reconcile_action_label(0)
        if already:
            _info(f"All {already} retired card(s) in your collection are already "
                  f"archived (suspended and moved to <b>{RETIRED_DECK_LEAF}</b>). "
                  "Nothing more to do.")
        else:
            _info("No retired cards or reorganized decks found in your collection — "
                  f"nothing to tidy up. (Source: {source}.)")
        return

    # A big first run (a large reorg landed before Reconcile was run even once) reads as
    # alarming without context — say up front that it's a one-time catch-up, not what to
    # expect going forward, so the length itself doesn't feel like something went wrong.
    catch_up_note = (
        "<i>This looks like a one-time catch-up — likely your first Reconcile since a "
        "larger update. Future runs should be much shorter.</i><br><br>"
        if len(fresh) + len(moves) + len(stranded) > 20 else "")

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

    stranded_block = _stranded_block(stranded, her)
    safety_note = (
        "<br><br>Nothing is deleted. Archived cards keep their review history and can "
        "be brought back anytime by unsuspending them or moving them out of the "
        "Retired deck" +
        (", and any personal notes on them carry over to the replacement first."
         if fresh else ".") +
        " A backup is taken automatically before anything changes."
    )
    body = "<br><br>".join(b for b in (archive_block, stranded_block, moves_block) if b)
    yes_label = " and ".join(
        x for x in ("Archive" if fresh or stranded else None,
                    "relocate" if moves else None) if x) or "Apply"
    if not _ask_scrollable(catch_up_note + body + safety_note, yes_label=yes_label):
        return

    proceed, backed_up = _pre_sync_backup_or_confirm_skip(cfg["export_deck"])
    if not proceed:
        return
    carried = carry_over_protected_fields(fresh, her, cfg["protected"])
    n_merged = _merge_stranded(stranded, her, cfg["protected"], retired_deck, tag)
    n_archived = archive_notes([her[r["guid"]] for r in fresh], retired_deck, tag)
    n_moved = apply_deck_moves(moves, her)
    mw.reset()
    _refresh_reconcile_action_label(0)   # this run just handled everything found
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
    if n_merged:
        result_lines.append(
            f"Merged <b>{n_merged}</b> reworded card(s): your progress moved onto the "
            "current wording, and the older copy was archived alongside the rest.")
    if n_moved:
        result_lines.append(f"Moved <b>{n_moved}</b> card(s) to their reorganized deck — "
                            "content and scheduling untouched.")
    _info("<br><br>".join(result_lines) + backup_line)


@_safe
def clean_up_duplicates():
    """Find sync duplicates (two notes, same type and front text, different GUIDs)
    and archive the losing copy of each, using the same retire machinery Reconcile my
    decks already uses: carry over any personal notes first, then suspend, move to the
    Retired deck, and tag so a later run skips it.

    A duplicate happens when a sync fails to match an incoming note to one the learner
    already has, by GUID or front text, and imports it fresh instead of updating her
    existing copy in place, most commonly right after a deck reorg. See
    logic.find_duplicate_groups for the ranking rule: most reviews wins, ties prefer
    the copy already under the deck source's current canonical deck path.
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

    tag = f'{cfg["scope_tag"]}::{DUPLICATE_TAG_LEAF}'
    canonical_deck_names = [d["name"] for d in manifest.get("decks", [])]
    her_notes = _her_notes_summary(cfg["scope_tag"], exclude_tag=tag)
    groups = find_duplicate_groups(her_notes, canonical_deck_names)
    if not groups:
        _info(f"No duplicate cards found. (Source: {source}.)")
        return

    block = duplicate_dialog_html(groups)
    safety_note = (
        "<br><br>Nothing is deleted. Archived cards keep their review history and can "
        "be brought back anytime by unsuspending them or moving them out of the "
        "Retired deck, and any personal notes on them carry over to the kept copy "
        "first. A backup is taken automatically before anything changes.")
    if not _ask_scrollable(block + safety_note, yes_label="Archive duplicates"):
        return

    proceed, backed_up = _pre_sync_backup_or_confirm_skip(cfg["export_deck"])
    if not proceed:
        return
    retired_deck = f'{cfg["export_deck"]}::{RETIRED_DECK_LEAF}'
    retired = [{"guid": a["guid"], "superseded_by": [g["keep"]["guid"]]}
               for g in groups for a in g["archive"]]
    her_guid_to_nid = {n["guid"]: n["nid"] for g in groups for n in [g["keep"], *g["archive"]]}
    carried = carry_over_protected_fields(retired, her_guid_to_nid, cfg["protected"])
    n_archived = archive_notes([a["nid"] for g in groups for a in g["archive"]],
                               retired_deck, tag)
    mw.reset()
    backup_line = ("" if backed_up else
                   "<br><br>(No backup was taken this time: nothing to back up yet, or "
                   "it failed and you chose to continue.)")
    _info(f"Archived <b>{n_archived}</b> duplicate card(s) to <b>{retired_deck}</b>: "
          f"suspended and tagged <code>{tag}</code>, review history kept"
          + (f" ({carried} personal note(s) carried over to the kept copy)"
             if carried else "") + ". Bring any back by unsuspending it or moving "
          "it out of the Retired deck." + backup_line)


# A session-lived cache of preview downloads, keyed by deck name to (version, path).
# Opening Update my decks, looking at the preview, and cancelling used to re-download
# every pending deck's .apkg the next time it opened: pure repeated network cost for an
# unchanged deck, and (since v0.26.1 made the preview a real per-deck download) the main
# reason a "just checking" habit runs into sporadic GitHub hiccups more often. The
# version is a content hash, so a cached entry can only ever satisfy a deck whose content
# is byte-for-byte what it was: a real push changes the version, misses the cache, and
# re-downloads. Cleared on Anki restart (it's only in memory), and the cached temp file
# is re-fetched if it's been swept from the tempdir since.
_apkg_cache = {}


def _cached_fetch(fetch, d):
    hit = _apkg_cache.get(d["name"])
    if hit and hit[0] == d.get("version") and os.path.exists(hit[1]):
        return hit[1]
    path = fetch(d)
    _apkg_cache[d["name"]] = (d.get("version"), path)
    return path


def _preview_content_changes(fetch, todo, her, aliases):
    """Download every pending deck and match it against the collection, so the
    confirmation can show real "N kept · M new" counts instead of just each deck's
    total card count. A cancellable progress window covers it, since this is a live
    network fetch per deck and a multi-deck update on a slow link otherwise looks
    like a hang, with no way out, before the confirmation even appears.

    Returns ({deck_name: (kept, new, new_notes) | None}, downloaded, cancelled).
    `new_notes` is remap_cards' own list of the notes that will import as new, carried
    through so the confirmation can name them and the review dialog can show them in
    full: it costs nothing extra here, since remap_cards already reads every note to
    count them. `downloaded` is {deck_name: local_path_or_Exception}, in the same shape
    background.py's auto-sync poll already uses, so the caller can hand it straight to
    _run_sync afterward instead of downloading every deck a second time. A per-deck
    fetch failure here is recorded, not raised, so one bad download only blanks that
    deck's preview ("couldn't preview") rather than blocking the whole confirmation;
    the same failure surfaces for real if Sync then tries to apply it. `cancelled` means
    the learner clicked Cancel partway through: nothing has touched the collection at
    this point, so the caller can just stop outright.

    Downloads go through _cached_fetch, so re-opening Update my decks without applying
    doesn't re-fetch a deck whose version hasn't changed.
    """
    preview, downloaded = {}, {}
    with cancellable_progress("Checking for updates", len(todo)) as step:
        for i, d in enumerate(todo, 1):
            short = d["name"].split("::")[-1]
            if not step(i, f"Checking {short} ({i} of {len(todo)})"):
                return preview, downloaded, True
            try:
                src = _cached_fetch(fetch, d)
                downloaded[d["name"]] = src
                _, kept, new, new_notes = remap_cards(src, her, aliases)
                preview[d["name"]] = (kept, new, new_notes)
            except Exception as e:
                downloaded[d["name"]] = e
                preview[d["name"]] = None
    return preview, downloaded, False


@_safe
def update_decks():
    """The one-click front door: computes everything pending — deck content updates,
    retired cards still in the collection, and cards a deck reorg needs to relocate —
    in a single pass, shows one confirmation covering all of it, then applies content
    updates before archiving/relocating, so a retired card's replacement is already in
    place before the old card archives out (the ordering reconcile_decks' own "run Sync
    decks first" note asks the learner to do by hand, done automatically here instead).

    Composes sync_decks/reconcile_decks' own machinery (_run_sync, _reconcile_pending,
    carry_over_protected_fields, archive_notes, apply_deck_moves) rather than
    reimplementing any of it — this only adds the combined preview/confirm/summary
    layer around them. Sync decks and Reconcile my decks remain as separate Advanced
    items for anyone who wants either half on its own.
    """
    cfg = _cfg()
    fetched = _fetch_manifest_gated(cfg)
    if not fetched:
        return
    manifest, fetch, source = fetched

    installed = installed_matching_collection(_load_json(INSTALLED, {}), cfg["scope_tag"])
    todo = decks_to_update(manifest, installed, cfg["excluded"])
    her, fresh, already, moves, retired_deck, tag, stranded = _reconcile_pending(
        manifest, cfg)

    if not todo and not fresh and not moves and not stranded:
        _refresh_reconcile_action_label(0)
        _info(f"You're all up to date (source: {source}).")
        return

    preview, downloaded, collisions = {}, {}, []
    if todo:
        preview, downloaded, cancelled = _preview_content_changes(
            fetch, todo, _her_front_to_guid(cfg["scope_tag"]), manifest.get("front_aliases", {}))
        if cancelled:
            _info("Update cancelled — nothing was changed.")
            return

    # Every card this update would add, in deck order then .apkg order. `new_index` maps
    # each one's GUID back to where it came from, because the review dialog only knows
    # GUIDs: without it a flag she writes couldn't say which deck or card it was about.
    new_cards = []
    for d in todo:
        pc = preview.get(d["name"])
        if not pc:
            continue          # this deck's download failed; it already reads "couldn't preview"
        for _, fields, guid in pc[2]:
            new_cards.append((d["name"], note_display_label(fields), guid))
    # Detected here, not mid-import, because the download that reveals it has already
    # happened: the preview above fetched every pending deck. Knowing it now is what
    # lets the one confirmation carry the decision, instead of interrupting the run
    # with its own question after the import has started.
    pending_templates = {}
    for d in todo:
        src = downloaded.get(d["name"])
        if not src or isinstance(src, Exception):
            continue
        try:
            pending_templates.update(_template_changes(src))
        except Exception:
            pass    # a deck we can't read here still imports; it just can't offer this

    new_index = {guid: (deck, label) for deck, label, guid in new_cards}
    flags = {}
    # Notes from a run that never reached its digest (a crash, a force quit, an error
    # mid-import) come back here rather than needing their own recovery prompt: they
    # ride along in this run's flags and land in the summary at the end like anything
    # written just now. merge_saved_feedback carries each one's deck and front too,
    # since the card itself may have imported last time and so appear nowhere in this
    # run's own index.
    recovered = merge_saved_feedback(load_saved_feedback(), flags, new_index)

    def _line(d):
        short = d["name"].split("::")[-1]
        pc = preview.get(d["name"])
        return f"{short} (couldn't preview)" if pc is None else f"{short} ({pc[0]} kept · {pc[1]} new)"

    sections = []
    if todo:
        sections.append(
            f"<b>{len(todo)}</b> deck(s) have updates:" + bullets([_line(d) for d in todo], cap=15))
    if new_cards:
        # New cards get named here, not just counted, and the count alone is what this
        # section exists to replace: retired and relocated cards were already listed by
        # front, so a card being ADDED was the one kind that used to arrive as a bare
        # number. note_display_label is plain text (tags stripped, entities decoded), so
        # it has to be escaped on the way back into this HTML: an unescaped front like
        # "SpO2 <94%" would otherwise be parsed as a tag and swallow the rest of the line.
        new_lines = [f"{html.escape(label)} <span style='color:gray;'>"
                     f"({deck.split('::')[-1]})</span>" for deck, label, _ in new_cards]
        sections.append(
            f"<b>{len(new_cards)}</b> card(s) will be added that you don't have yet."
            + bullets(new_lines, cap=15))
    if fresh:
        lines = [f"{r['identity']} <span style='color:gray;'>"
                 f"({r['deck'].split('::')[-1]})</span>" for r in fresh]
        already_note = f" ({already} more were already archived earlier.)" if already else ""
        sections.append(
            f"<b>{len(fresh)}</b> retired card(s) are still in your collection — split "
            "or reworded since, with the replacements added separately, so these just "
            f"duplicate your reviews now.{already_note}" + bullets(lines, cap=15))
    if stranded:
        sections.append(_stranded_block(stranded, her))
    if moves:
        move_lines = [f"{mw.col.get_note(her[m['guid']]).fields[0]} <span "
                      f"style='color:gray;'>→ {m['to'].split('::')[-1]}</span>" for m in moves]
        sections.append(
            f"<b>{len(moves)}</b> card(s) belong to a deck that's since been "
            "reorganized." + bullets(move_lines, cap=15))

    # A big first run (a large backlog accumulated before Update was run even once)
    # reads as alarming without context — say up front it's a one-time catch-up.
    catch_up_note = (
        "<i>This looks like a one-time catch-up — likely your first update in a "
        "while. Future updates should be much shorter.</i><br><br>"
        if len(fresh) + len(moves) + len(stranded) > 20 else "")
    safety_note = (
        "<br><br>This is a preview: nothing above has been applied yet. Your "
        "review history and any personal notes on existing cards are kept (matched "
        "by card, not overwritten). Archived cards keep their history too and can "
        "be brought back anytime by unsuspending them or moving them out of the "
        "Retired deck, nothing here is ever deleted. A backup is taken "
        "automatically first.")

    def _body():
        carried = (f" {recovered} of them carried over from an earlier session."
                   if recovered else "")
        flagged = (f"<br><br><b>{len(flags)} card(s) flagged.</b>{carried} You'll get a "
                   "summary to send back when this finishes."
                   if cfg["collect_feedback"] and flags else "")
        return catch_up_note + "<br><br>".join(sections) + flagged + safety_note

    def _open_review(parent):
        """Read the new cards in full and hand them to the review dialog.

        This is the only place that pays for apkg_note_details, and only when she asks
        for it: the .apkg is already downloaded and cached by the preview above, so the
        cost is reading a local file, not another fetch. A deck that can't be read is
        reported and skipped rather than blocking the rest, matching how a failed
        preview download already degrades: the update itself doesn't depend on any of
        this, so a broken preview must never be able to stop it.

        That same downloaded file is what a row's pictures are extracted from when it is
        opened, so showing one costs a local read rather than another fetch.
        """
        decks, failed, sources = [], [], {}
        for d in todo:
            pc = preview.get(d["name"])
            src = downloaded.get(d["name"])
            if not pc or not pc[2] or isinstance(src, Exception) or not src:
                continue
            try:
                decks.append((d["name"], apkg_note_details(src, [r for r, _, _ in pc[2]])))
                sources[d["name"]] = src
            except Exception as e:
                failed.append(f"{d['name'].split('::')[-1]} ({e})")
        if not decks:
            if failed:
                _warn("Couldn't read the new cards from:" + bullets(failed) +
                      "<br>The update itself is unaffected; there is just nothing to "
                      "show here.")
            return None
        # Named inside the review dialog rather than as a warning box in front of it:
        # it is context for the list she asked to see, and the update does not depend
        # on any of it.
        review_new_cards(parent, decks, flags, unreadable=failed, sources=sources)
        return _body()

    def _finish(summary_html=None):
        """End the run: the summary and her notes as one dialog, then drop the saved
        copy of those notes.

        Called on every exit path including Cancel, deliberately: if she read the new
        cards, flagged three of them and backed out, those flags are the most
        interesting thing that happened, and dropping them because she said no would
        throw away the only part of the run that couldn't be reproduced by clicking
        Update again later.

        The saved copy is cleared only once the digest has actually been built and
        shown. An exit with nothing flagged leaves the file alone rather than deleting
        it: there is nothing to clear in that case anyway, and treating "no flags this
        run" as "safe to forget" is exactly how a recovered note would get thrown away
        a second time.
        """
        entries = feedback_entries(flags, new_index)
        show_result_with_feedback(summary_html, entries)
        if entries:
            clear_saved_feedback()

    # Unticked by default: applying it forces a one-time full AnkiWeb sync, which is
    # not something a reader should be able to agree to by not reading a checkbox.
    # Declining still imports the content; only the card's appearance stays as it is,
    # and the next update carrying a look change offers it again.
    tpl_choice = {"label": "Also apply the new card look (forces a one-time full "
                           "AnkiWeb sync)", "checked": False} if pending_templates else None
    if tpl_choice:
        sections.append(
            "This update also changes how some cards look (template or styling) for: "
            + ", ".join(f"<b>{n}</b>" for n in sorted(pending_templates))
            + ". Your review history and card content are unaffected either way.")

    if not _ask_scrollable(
            _body(), yes_label="Update",
            extra_label=(f"Review {len(new_cards)} new card(s)" if new_cards else None),
            on_extra=_open_review, checkbox=tpl_choice):
        _finish()
        return

    proceed, backed_up = _pre_sync_backup_or_confirm_skip(cfg["export_deck"])
    if not proceed:
        _finish()
        return

    results, restored, tpl_changes = [], 0, {}
    if todo:
        def _already_fetched(d):
            # Reuses _preview_content_changes' download above instead of fetching
            # every deck a second time — same pattern background.py's auto-sync
            # poll uses for the same reason. A deck whose preview download failed
            # re-raises that same exception here, so _run_sync's own per-deck
            # try/except reports it exactly like a live fetch failure would.
            v = downloaded[d["name"]]
            if isinstance(v, Exception):
                raise v
            return v

        # A cancellable progress window while each deck imports: the preview step
        # above already covered the download itself.
        with cancellable_progress("Updating decks", len(todo)) as step:
            results, restored, tpl_changes, _, cancelled, collisions, _conv = _run_sync(
                cfg, manifest, _already_fetched, todo, installed,
                on_progress=lambda i, n, name: step(i, f"Applying {name} ({i} of {n})"))

        if cancelled:
            # Stop here rather than falling through to archive/relocate: that step
            # assumes every content update already landed, so a retired card's
            # replacement is in place before the old one archives out. Whatever
            # decks _run_sync did finish are already fully applied and persisted
            # (see its docstring) — only the decks after the cancel point, and the
            # reconcile pass, are what's left pending for next time.
            fields_line = (f"Preserved fields restored on {restored} card(s).<br><br>"
                          if restored else "")
            backup_line = (
                "A pre-sync backup of the Intern Pearls deck was saved; use "
                "<i>Advanced → Import intern pearls deck</i> to revert to it if needed."
                if backed_up else
                "No pre-sync backup was taken this time (nothing to back up yet, or "
                "it failed and you chose to continue).")
            _finish(f"<b>Update stopped early</b> (source: {source})" + bullets(results) +
                    "<br><br>Archiving or relocating retired cards was skipped, since "
                    "that assumes every update above already landed. Nothing else was "
                    "touched; run <b>Update my decks</b> again anytime to pick up where "
                    "this left off." + fields_line + backup_line)
            return
        # Consented to on the confirmation, so nothing is asked here. tpl_changes is
        # what the import actually found; the checkbox is what she agreed to, and the
        # intersection is what gets applied.
        if tpl_choice and tpl_choice["checked"] and tpl_changes:
            _apply_template_changes(tpl_changes)

    n_archived = n_moved = carried = n_merged = 0
    if fresh or moves or stranded:
        # Refetched, not the pre-sync `her` from _reconcile_pending above: the sync
        # step just above may have imported a retired card's replacement for the
        # first time, and carry_over_protected_fields needs the replacement's current
        # nid to find it and copy her annotation over.
        her = _her_guid_to_nid(cfg["scope_tag"])
        # Recomputed against the post-sync collection for the same reason, and for one
        # more: this very sync can CREATE a stranding, by importing a reworded front as
        # a second note when her GUID didn't match. Recomputing means such a pair is
        # merged in the same run that made it, rather than surfacing as a duplicate she
        # has to see once before the next update tidies it away.
        stranded = [p for p in find_stranded_pairs(
            manifest.get("superseded_fronts", {}), _her_front_to_guid(cfg["scope_tag"]))
            if p["guid"] in her and p["successor_guid"] in her
            and tag not in mw.col.get_note(her[p["guid"]]).tags]
        carried = carry_over_protected_fields(fresh, her, cfg["protected"])
        n_merged = _merge_stranded(stranded, her, cfg["protected"], retired_deck, tag)
        n_archived = archive_notes([her[r["guid"]] for r in fresh], retired_deck, tag)
        n_moved = apply_deck_moves(moves, her)
        mw.reset()
        _refresh_reconcile_action_label(0)   # this run just handled everything found

    result_lines = list(results)
    if n_archived:
        result_lines.append(
            f"✓ Archived <b>{n_archived}</b> retired card(s) to <b>{retired_deck}</b>"
            + (f" ({carried} personal note(s) carried over)" if carried else "") + ".")
    if n_merged:
        result_lines.append(
            f"✓ Merged <b>{n_merged}</b> reworded card(s): your progress moved onto the "
            "current wording, older copy archived.")
    if n_moved:
        result_lines.append(f"✓ Moved <b>{n_moved}</b> card(s) to their reorganized deck.")

    fields_line = (f"Preserved fields restored on {restored} card(s).<br><br>"
                  if restored else "")
    fields_line += _collision_note(collisions)
    backup_line = (
        "A pre-sync backup of the Intern Pearls deck was saved; use "
        "<i>Advanced → Import intern pearls deck</i> to revert to it if needed."
        if backed_up else
        "No pre-sync backup was taken this time (nothing to back up yet, or it "
        "failed and you chose to continue).")
    _finish(f"<b>Update complete</b> (source: {source})" + bullets(result_lines) +
            fields_line + backup_line)


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
    remap, in_place, as_new, _ = remap_cards(src, her, aliases)
    if not _ask(f"{in_place} card(s) will keep their history, {as_new} will be added "
                "as new. A backup is taken automatically first. Import now?"):
        return
    if not _pre_sync_backup_or_confirm_skip(cfg["export_deck"])[0]:
        return
    tpl = _template_changes(src)
    snap = _snapshot(cfg["protected"], cfg["scope_tag"])
    touched = {remap.get(rid, guid) for rid, _f, guid in apkg_notes(src)}
    out = src + ".sync.apkg"
    write_personalized(src, remap, out)
    try:
        _import_apkg(out)
    finally:
        try:
            os.remove(out)
        except OSError:
            pass
    shipped = _capture_shipped(cfg["protected"], cfg["scope_tag"], touched)
    restored, _ = _restore(snap, _load_json(SHIPPED, {}), touched)
    if shipped:
        _save_json(SHIPPED, {**_load_json(SHIPPED, {}), **shipped})
    mw.reset()
    _offer_template_changes(tpl)
    fields_line = f" Preserved fields restored on {restored} card(s)." if restored else ""
    _info(f"Imported {os.path.basename(src)}: {in_place} kept history, {as_new} new."
          f"{fields_line}")
