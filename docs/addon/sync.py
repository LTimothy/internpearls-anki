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
import json
import os
import tempfile

from aqt import mw
from aqt.utils import getFile

from .collection import (_apply_deck, _apply_template_changes, _ensure_notetypes,
                         _her_front_to_guid, _her_guid_to_deck, _her_guid_to_nid,
                         _import_apkg, _pre_sync_backup_or_confirm_skip, _restore,
                         _snapshot, _template_changes, apply_deck_moves,
                         archive_notes, carry_over_protected_fields,
                         installed_matching_collection)
from .config import (ADDON_VERSION, INSTALLED, RETIRED_DECK_LEAF, RETIRED_TAG_LEAF,
                     SUPPORTED_MANIFEST_SCHEMA, _cfg, _load_json, _save_json)
from .logic import (bullets, decks_to_update, find_deck_moves_needed,
                    find_retired_in_collection, manifest_needs_newer_addon,
                    remap_cards, write_personalized)
from .net import _CONNECT_TIMEOUT, _DOWNLOAD_TIMEOUT, _gh_raw
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
        results, restored, tpl_changes, _, cancelled = _run_sync(
            cfg, manifest, fetch, todo, installed,
            on_progress=lambda i, n, name: step(i, f"Syncing {name} ({i} of {n})"))
    _offer_template_changes(tpl_changes)
    fields_line = (f"Preserved fields restored on {restored} card(s).<br><br>"
                  if restored else "")
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
    results, tpl_changes, deferred = [], {}, []
    cancelled = False
    for i, d in enumerate(todo, 1):
        short = d["name"].split("::")[-1]
        if on_progress and not on_progress(i, len(todo), short):
            cancelled = True
            break
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
    return results, restored, tpl_changes, deferred, cancelled


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

    Returns (her, fresh, already, moves, retired_deck, tag) — `her` is {guid: nid} for
    every note currently under scope_tag, which the caller needs again to act on
    `fresh`/`moves` afterward (or, for update_decks(), to refetch post-sync — see its
    docstring for why that refetch matters).
    """
    her = _her_guid_to_nid(cfg["scope_tag"])
    found = find_retired_in_collection(manifest.get("retired", {}), set(her))
    her_deck = _her_guid_to_deck(cfg["scope_tag"])
    moves = [m for m in find_deck_moves_needed(manifest.get("deck_moves", {}), her_deck)
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
    return her, fresh, already, moves, retired_deck, tag


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

    her, fresh, already, moves, retired_deck, tag = _reconcile_pending(manifest, cfg)
    if not fresh and not moves:
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
    if n_moved:
        result_lines.append(f"Moved <b>{n_moved}</b> card(s) to their reorganized deck — "
                            "content and scheduling untouched.")
    _info("<br><br>".join(result_lines) + backup_line)


def _preview_content_changes(fetch, todo, her, aliases):
    """Download every pending deck and match it against the collection, so the
    confirmation can show real "N kept · M new" counts instead of just each deck's
    total card count. A cancellable progress window covers it, since this is a live
    network fetch per deck and a multi-deck update on a slow link otherwise looks
    like a hang, with no way out, before the confirmation even appears.

    Returns ({deck_name: (kept, new) | None}, downloaded, cancelled) — `downloaded`
    is {deck_name: local_path_or_Exception}, in the same shape background.py's
    auto-sync poll already uses, so the caller can hand it straight to _run_sync
    afterward instead of downloading every deck a second time. A per-deck fetch
    failure here is recorded, not raised, so one bad download only blanks that
    deck's preview ("couldn't preview") rather than blocking the whole
    confirmation; the same failure surfaces for real if Sync then tries to apply it.
    `cancelled` means the learner clicked Cancel partway through — nothing has
    touched the collection at this point, so the caller can just stop outright.
    """
    preview, downloaded = {}, {}
    with cancellable_progress("Checking for updates", len(todo)) as step:
        for i, d in enumerate(todo, 1):
            short = d["name"].split("::")[-1]
            if not step(i, f"Checking {short} ({i} of {len(todo)})"):
                return preview, downloaded, True
            try:
                src = fetch(d)
                downloaded[d["name"]] = src
                _, kept, new = remap_cards(src, her, aliases)
                preview[d["name"]] = (kept, new)
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
    her, fresh, already, moves, retired_deck, tag = _reconcile_pending(manifest, cfg)

    if not todo and not fresh and not moves:
        _refresh_reconcile_action_label(0)
        _info(f"You're all up to date (source: {source}).")
        return

    preview, downloaded = {}, {}
    if todo:
        preview, downloaded, cancelled = _preview_content_changes(
            fetch, todo, _her_front_to_guid(cfg["scope_tag"]), manifest.get("front_aliases", {}))
        if cancelled:
            _info("Update cancelled — nothing was changed.")
            return

    def _line(d):
        short = d["name"].split("::")[-1]
        pc = preview.get(d["name"])
        return f"{short} (couldn't preview)" if pc is None else f"{short} ({pc[0]} kept · {pc[1]} new)"

    sections = []
    if todo:
        sections.append(
            f"<b>{len(todo)}</b> deck(s) have updates:" + bullets([_line(d) for d in todo], cap=15))
    if fresh:
        lines = [f"{r['identity']} <span style='color:gray;'>"
                 f"({r['deck'].split('::')[-1]})</span>" for r in fresh]
        already_note = f" ({already} more were already archived earlier.)" if already else ""
        sections.append(
            f"<b>{len(fresh)}</b> retired card(s) are still in your collection — split "
            "or reworded since, with the replacements added separately, so these just "
            f"duplicate your reviews now.{already_note}" + bullets(lines, cap=15))
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
        if len(fresh) + len(moves) > 20 else "")
    safety_note = (
        "<br><br>This is a preview: nothing above has been applied yet. Your "
        "review history and any personal notes on existing cards are kept (matched "
        "by card, not overwritten). Archived cards keep their history too and can "
        "be brought back anytime by unsuspending them or moving them out of the "
        "Retired deck, nothing here is ever deleted. A backup is taken "
        "automatically first.")

    if not _ask_scrollable(catch_up_note + "<br><br>".join(sections) + safety_note,
                           yes_label="Update"):
        return

    proceed, backed_up = _pre_sync_backup_or_confirm_skip(cfg["export_deck"])
    if not proceed:
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
            results, restored, tpl_changes, _, cancelled = _run_sync(
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
            _info(f"<b>Update stopped early</b> (source: {source})" + bullets(results) +
                  "<br><br>Archiving or relocating retired cards was skipped, since "
                  "that assumes every update above already landed. Nothing else was "
                  "touched; run <b>Update my decks</b> again anytime to pick up where "
                  "this left off." + fields_line + backup_line)
            return
        _offer_template_changes(tpl_changes)

    n_archived = n_moved = carried = 0
    if fresh or moves:
        # Refetched, not the pre-sync `her` from _reconcile_pending above: the sync
        # step just above may have imported a retired card's replacement for the
        # first time, and carry_over_protected_fields needs the replacement's current
        # nid to find it and copy her annotation over.
        her = _her_guid_to_nid(cfg["scope_tag"])
        carried = carry_over_protected_fields(fresh, her, cfg["protected"])
        n_archived = archive_notes([her[r["guid"]] for r in fresh], retired_deck, tag)
        n_moved = apply_deck_moves(moves, her)
        mw.reset()
        _refresh_reconcile_action_label(0)   # this run just handled everything found

    result_lines = list(results)
    if n_archived:
        result_lines.append(
            f"✓ Archived <b>{n_archived}</b> retired card(s) to <b>{retired_deck}</b>"
            + (f" ({carried} personal note(s) carried over)" if carried else "") + ".")
    if n_moved:
        result_lines.append(f"✓ Moved <b>{n_moved}</b> card(s) to their reorganized deck.")

    fields_line = (f"Preserved fields restored on {restored} card(s).<br><br>"
                  if restored else "")
    backup_line = (
        "A pre-sync backup of the Intern Pearls deck was saved; use "
        "<i>Advanced → Import intern pearls deck</i> to revert to it if needed."
        if backed_up else
        "No pre-sync backup was taken this time (nothing to back up yet, or it "
        "failed and you chose to continue).")
    _info(f"<b>Update complete</b> (source: {source})" + bullets(result_lines) +
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
