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
import datetime
import hashlib
import json
import os
import re
import tempfile

from aqt import mw
from aqt.utils import getFile

from .collection import (_apply_deck, _apply_template_changes, _capture_shipped,
                         _ensure_notetypes, change_note_types,
                         notetype_changes, seed_converted_siblings,
                         _her_front_to_guid, _her_guid_to_deck, _her_guid_to_fields,
                         _her_guid_to_nid,
                         _her_notes_summary, _import_apkg,
                         _pre_sync_backup_or_confirm_skip, _restore,
                         _snapshot, _template_changes, apply_deck_moves,
                         archive_notes, carry_over_protected_fields,
                         carry_scheduling_forward, decks_holding,
                         installed_matching_collection)
from .config import (ADDON_VERSION, DUPLICATE_TAG_LEAF, INSTALLED, RETIRED_DECK_LEAF,
                     RETIRED_TAG_LEAF, SHIPPED, SUPPORTED_MANIFEST_SCHEMA, _cfg,
                     _load_json, _save_json, load_declined, save_declined)
from .logic import (apkg_deck_names, apkg_note_details, apkg_notes, declined_drop,
                    decks_to_update, feedback_entries, merge_saved_feedback,
                    duplicate_dialog_rows, find_changed_notes, find_deck_moves_needed,
                    find_duplicate_groups, find_retired_in_collection,
                    find_stranded_pairs, manifest_needs_newer_addon,
                    note_display_label, note_fields_hash, plain_text, plural,
                    prune_declined, remap_cards, write_personalized)
from .net import _CONNECT_TIMEOUT, _DOWNLOAD_TIMEOUT, DownloadCancelled, _gh_raw
from .palette import colors
from .review import (_CONFIRM_HEIGHT, append_rows, build_list_body, build_update_body,
                     clear_saved_feedback, load_saved_feedback, show_result,
                     show_result_with_feedback)
from .ui import (_ask, _ask_with_widget, _info, _manual_flow, _safe, _warn,
                 cancellable_progress, wait_cursor)

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


_scratch_dir = None


def _scratch():
    """A private, per-session directory for downloaded decks and the personalized copies
    written beside them.

    mkdtemp is mode 0700 and its name is unguessable, unlike the fixed
    /tmp/<deck>.apkg these downloads used to land on: on a shared machine anyone could
    pre-create that path as a symlink and have the add-on write (or import) through it.
    One directory per session rather than one temp file per download, so `_cached_fetch`
    can keep handing the same preview download to the apply step by path, exactly as it
    did before.
    """
    global _scratch_dir
    if _scratch_dir is None or not os.path.isdir(_scratch_dir):
        _scratch_dir = tempfile.mkdtemp(prefix="internpearls-")
    return _scratch_dir


def _scratch_path(apkg_path, version=None):
    """Where one manifest .apkg path downloads to inside the session's scratch dir.

    Keyed by the whole manifest path rather than its basename: a source can file two
    decks as decks/basics/Foo.apkg and decks/advanced/Foo.apkg, and a basename key had
    the second download overwrite the first, so the apply step imported one deck's
    content for both. The scratch dir has no subfolders, so the path is flattened to a
    short hash of itself plus a sanitized filename, which keeps the name recognizable
    while the hash is what actually keeps two decks apart.

    The version (a content hash) is part of that key too, so two versions of one deck
    are two files. Path alone meant a background poll's fetch of a newly pushed version
    landed on top of the file an open confirmation had already read and was about to
    import, swapping the content out from under a decision the reader had already made.
    """
    key = f"{apkg_path}\n{version}" if version else apkg_path
    digest = hashlib.sha1(key.encode("utf8")).hexdigest()[:8]
    name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(apkg_path)) or "deck.apkg"
    return os.path.join(_scratch(), f"{digest}-{name}")


def _write_scratch(apkg_path, data, version=None):
    """Land a downloaded deck at its keyed scratch path, atomically.

    The background poll's fetch runs on a worker thread and writes the same keyed path
    an interactive preview writes, so a plain open-and-write can be read half finished
    by the other side, which is a corrupt .apkg to whoever gets there first. Written to
    a temp file in the same directory and renamed into place instead: os.replace is
    atomic, so a concurrent reader sees either the previous file or the whole new one.
    """
    fd, tmp = tempfile.mkstemp(dir=_scratch(), suffix=".part")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        path = _scratch_path(apkg_path, version)
        os.replace(tmp, path)
        return path
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _require_manifest_object(manifest, where):
    """Refuse a manifest that parsed as valid JSON but isn't an object.

    A JSON array or a bare string is a broken source, not an unreadable one, and every
    reader downstream calls .get on it: without this the first of them raises a bare
    AttributeError, which surfaces as "'list' object has no attribute 'get'" rather
    than as the message this same file's other two failure modes already give. Guarded
    here at the source boundary rather than inside logic.py, so the pure-Python half
    keeps taking the shape it is given.
    """
    if not isinstance(manifest, dict):
        raise RuntimeError(f"the manifest.json in {where} isn't valid: it holds a "
                           f"{type(manifest).__name__} where a set of decks was "
                           "expected")


def _source_warning(e):
    """Warn about a manifest fetch that failed, saying which of the two it was.

    _fetch_manifest raises RuntimeError for a source it found and could not use (a
    folder with no manifest.json, a manifest that isn't valid JSON, one that isn't an
    object at all) and lets the transport's own error through for a source it could not
    reach. Leading both with "Couldn't reach the deck source" contradicted the very
    message it was quoting, and sent someone looking at their network for a problem
    sitting in their manifest.
    """
    lead = (f"The deck source couldn't be used: {e}."
            if isinstance(e, RuntimeError) else
            f"Couldn't reach the deck source: {e}")
    _warn(f"{lead}<br><br>"
          "Open <b>Intern Pearls → Manage decks</b> and use Change source to check "
          "your GitHub token or local folder.")


def _fetch_manifest(cfg, timeout=_CONNECT_TIMEOUT, download_timeout=_DOWNLOAD_TIMEOUT):
    """Return (manifest, fetch_apkg, source_label) where fetch_apkg(deck, on_chunk=None)
    returns a local .apkg path.

    A GitHub source needs only the repo; the token is optional (blank is fine for a
    public repo, since _http_get simply sends no Authorization header). `timeout` bounds
    the manifest fetch itself; `download_timeout` bounds each deck download, defaulting
    to the generous _DOWNLOAD_TIMEOUT since those only happen after first contact
    already proved the source reachable. The unattended poll overrides it when it has to
    run those downloads inline on the main thread (see background._auto_sync_check).

    `on_chunk` is net._http_get's, passed straight through, so an interactive caller can
    hand it `cancellable_progress`'s own `step.pump` and have Cancel work during a
    download rather than only between decks. The local-folder source takes it and
    ignores it: there is no transfer to interrupt.

    (None, None, None) means nothing is configured at all, and only that. A source that
    IS configured but can't be loaded raises instead, with a message naming the actual
    problem, so a typo'd folder or a corrupt manifest reads as the error it is rather
    than as "no deck source configured yet". Callers already show a raised message
    as-is (_fetch_manifest_gated, dialogs.manage_decks' Source line), so both cases
    surface correctly without either of them special-casing this.
    """
    if cfg["gh_repo"]:
        raw = _gh_raw(cfg["gh_repo"], "manifest.json", cfg["gh_token"], cfg["gh_ref"],
                      timeout=timeout)
        try:
            manifest = json.loads(raw)
        except Exception as e:
            # A repo that answers with something that isn't JSON is reachable and
            # broken, not unreachable: say which of the two it is, in the same words
            # the local-folder branch below uses for the same file.
            raise RuntimeError(f"the manifest.json in {cfg['gh_repo']} isn't valid "
                               f"JSON ({e})") from e
        if not manifest:
            raise RuntimeError(f"the manifest.json in {cfg['gh_repo']} is empty")
        _require_manifest_object(manifest, cfg["gh_repo"])

        def fetch(d, on_chunk=None):
            data = _gh_raw(cfg["gh_repo"], d["apkg"], cfg["gh_token"], cfg["gh_ref"],
                           timeout=download_timeout, on_chunk=on_chunk)
            return _write_scratch(d["apkg"], data, d.get("version"))

        return manifest, fetch, "GitHub"

    if cfg["decks_dir"]:
        folder = cfg["decks_dir"]
        if not os.path.isdir(folder):
            raise RuntimeError(
                f"the folder {folder} doesn't exist (check the path, or pick a "
                "different source)")
        path = os.path.join(folder, "manifest.json")
        if not os.path.exists(path):
            raise RuntimeError(
                f"{folder} has no manifest.json (point this at the folder that holds "
                "the manifest and the .apkg files)")
        try:
            manifest = _load_json(path, None, strict=True)
        except Exception as e:
            raise RuntimeError(f"the manifest.json in {folder} isn't valid JSON "
                               f"({e})") from e
        if not manifest:
            raise RuntimeError(f"the manifest.json in {folder} is empty")
        _require_manifest_object(manifest, folder)

        def fetch(d, on_chunk=None):
            return os.path.join(folder, d["apkg"])

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
        _source_warning(e)
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
@_manual_flow
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

    def _deck_row(d):
        """One deck's row: the deck as the primary text, its size as the trailing
        column, and a chip for which of the two things this deck is.

        The chip carries what the old line spelled out in a parenthesis: a deck you
        have none of yet is NEW, one already in your collection is UPDATED. The count
        is the manifest's own total for the deck rather than a kept/new split, since
        Sync decks downloads nothing before this confirmation.
        """
        short = d["name"].split("::")[-1]
        cards = d.get("cards")
        kind = "changed" if d["name"] in installed else "new"
        return ("row", kind, short,
                plural(cards, "card") if cards is not None else "")

    items = [("header", "Update these decks?")]
    for i, d in enumerate(todo):
        if i:
            items.append(("sep",))
        items.append(_deck_row(d))
    if not _ask_with_widget(
        build_list_body(items, bottom_html=(
            "Your review history and any personal notes on existing cards are kept "
            "(matched by card, not overwritten). A backup is taken automatically "
            "first, so this is safe to undo if anything looks wrong afterward.")),
        yes_label="Update", min_height=_CONFIRM_HEIGHT
    ):
        return
    proceed, backed_up = _pre_sync_backup_or_confirm_skip(
        cfg["export_deck"], [d["name"] for d in todo], cfg["scope_tag"])
    if not proceed:
        return

    # A cancellable, determinate progress window while each deck downloads and
    # imports: the fetches run on the main thread here (unlike auto-sync's
    # background poll), and a multi-deck sync on a slow link otherwise looks like a
    # hang with no way out. step.pump goes to the fetch so Cancel is answered during a
    # download too, not only in the gap between two decks.
    with cancellable_progress("Syncing decks", len(todo)) as step:
        results, restored, tpl_changes, _, cancelled, collisions, _conv = _run_sync(
            cfg, manifest, lambda d: fetch(d, on_chunk=step.pump), todo,
            on_progress=lambda i, n, name: step(i, f"Syncing {name} ({i} of {n})"))
    _offer_template_changes(tpl_changes)
    backup_line = (
        "A pre-sync backup of the Intern Pearls deck was saved; use "
        "<i>Advanced → Restore intern pearls deck</i> to revert to it if needed."
        if backed_up else
        "No pre-sync backup was taken this time (nothing to back up yet, or it "
        "failed and you chose to continue).")
    title = "Sync stopped early" if cancelled else "Sync complete"
    # Each deck's outcome is a row of its own, and everything the run has to say about
    # the whole of it reads as a paragraph between them, in the same vocabulary the
    # confirmation this follows was built from.
    items = []
    append_rows(items, [("row", None, line, "") for line in results])
    if restored:
        items.append(("note", f"Preserved fields restored on {plural(restored, 'card')}."))
    items += _collision_items(collisions)
    items.append(("note", backup_line))
    if cancelled:
        items.append(("note", "Nothing else was touched; run <b>Sync decks</b> again "
                              "anytime to pick up where this left off."))
    show_result(f"{title} (source: {source})", items)


def _collision_items(collisions):
    """The cards where her own edit and a source update landed on the same field: the
    explanation, then one row per card. Hers is kept; this exists so the two versions
    don't quietly diverge with nobody knowing, which is the one thing the three-way
    restore can't decide on its own.

    Capped at ten, with the remainder counted on a row of its own. The cap is a lookup
    cost rather than a readability one: naming a card means reading its note out of the
    collection, and a run where dozens collided has a bigger problem than a short list.

    Each card is named through note_display_label, like every other row that names one:
    a raw first field is HTML, so an image card read as a broken picture and a cloze
    read as its own braces, and slicing that raw field to fit could cut a tag in half
    and take the rest of the row's markup with it.
    """
    if not collisions:
        return []
    fronts = []
    for guid, field in collisions[:10]:
        nid = mw.col.db.scalar("select id from notes where guid = ?", guid)
        if nid:
            label = note_display_label(mw.col.get_note(nid).fields, max_len=70)
            fronts.append(f"{label} ({field})")
    if len(collisions) > len(fronts):
        fronts.append(f"<i>and {len(collisions) - len(fronts)} more</i>")
    items = [("note",
              f"On <b>{plural(len(collisions), 'card')}</b>, this update changed a "
              "field you had also written in yourself. <b>Your version was kept</b> "
              "and the update to that field was skipped, so nothing you wrote was "
              "lost. Worth passing these on to whoever maintains the decks if you want "
              "your wording folded in, or theirs applied instead:")]
    append_rows(items, [("row", None, front, "") for front in fronts])
    return items


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
        f"<b>{plural(len(changes), 'card')}</b> in this update changed format (a "
        "question and answer became a fill-in-the-blank).<br><br>Move your existing "
        "cards to the new format? They keep their review history and stay one card "
        "each. Anki treats this as a schema change, so your next AnkiWeb sync will be "
        "a one-time full sync, choose \"Upload to AnkiWeb\" when asked.<br><br>"
        "Choosing to import them as new still imports them, as separate new cards "
        "beside the ones you have, leaving your progress on the old versions.",
        yes_label="Move my cards across", no_label="Import them as new"
    ) else []


def _run_sync(cfg, manifest, fetch, todo, on_progress=None,
              defer_template_changes=False, convert_notetypes=None):
    """Apply every deck in `todo`: fix note types, snapshot protected fields, remap and
    import each deck (keeping the learner's scheduling), restore the snapshotted fields,
    and persist the new installed versions.

    The caller must already have confirmed (if interactive) and taken a backup — this is
    the one place the actual history-preserving sequence lives, shared by the interactive
    Sync decks flow and the unattended auto-sync poll, so there's exactly one
    implementation of the part that matters for not losing anyone's review history.
    Returns (results, restored, tpl_changes, deferred, cancelled, collisions,
    converted): per-deck outcome lines, the note-restore count, template/CSS changes
    detected in the imported decks (for the interactive caller to offer applying,
    imports never propagate them on their own, see _import_apkg), the names of decks
    skipped because of such a change, whether `on_progress` asked to stop partway
    through, the fields where the learner's own edit met an update (see _restore), and
    how many notes were converted to a new note type.
    `on_progress(i, total, deck_short_name)`, if given, fires before each deck is
    fetched and applied and must return a truthy value to continue; the interactive
    flow uses it to drive a cancellable progress window (a False return means the
    learner clicked Cancel), the unattended auto-sync poll passes nothing.

    A False from `on_progress` stops *before* that deck's fetch/import, never
    partway through one, so whatever decks already completed are already fully
    applied — the loop below still runs its snapshot-restore and persists their
    versions for exactly those, same as a clean finish, just for fewer decks.

    `defer_template_changes` is the unattended-caller policy: applying a template bumps
    the collection schema (a one-time full AnkiWeb sync), which must never happen
    without someone there to consent — so auto-sync passes True, and a deck whose
    update includes a template change is left un-imported and NOT marked installed,
    keeping it pending for the next interactive Sync decks where the user can decide.

    `convert_notetypes` is who owns the note-type-conversion decision. None (Sync decks)
    means ask per deck, here, via _offer_notetype_changes. True or False means the
    caller already asked once for the whole run and this loop must not put another
    question on screen (Update my decks detects the conversions up front from the
    preview's own downloads and asks before the apply loop starts). Either way the
    consent is explicit: False converts nothing, and an unattended caller never gets
    this far, since `defer_template_changes` holds the deck back instead.
    """
    aliases = manifest.get("front_aliases", {})   # from the (private) manifest, not config
    _ensure_notetypes()
    snap = _snapshot(cfg["protected"], cfg["scope_tag"])
    her = _her_front_to_guid(cfg["scope_tag"])
    reg = load_declined()
    declined = set(reg)
    seen = {}
    results, tpl_changes, deferred, touched = [], {}, [], set()
    # This run's own per-deck versions, and the only thing written back: the save below
    # merges them into whatever installed.json holds at that moment (see there), so no
    # caller has to hand its own snapshot of it in or take a half-updated one back.
    applied = {}
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
                # Named for what is actually being held back: a template change and a
                # note-type conversion defer for the same reason, and calling a
                # conversion a "card-template update" sends the reader looking for a
                # look change that isn't in this deck at all.
                what = " and ".join(x for x in ("card-template" if tpl else None,
                                                "note-type format" if nt else None) if x)
                results.append(f"• <b>{short}</b>: includes a {what} update, "
                               "waiting for a manual Sync decks")
                continue
            tpl_changes.update(tpl)
            # Before the import, not after: once the note is on the right type, the
            # import matches it by GUID and updates it in place, which is the whole
            # point. Afterwards it would be converting a duplicate.
            if convert_notetypes is None:
                changed_nids = _offer_notetype_changes(nt)
            else:
                changed_nids = change_note_types(nt) if (nt and convert_notetypes) else []
            converted += len(changed_nids)
            in_place, as_new, wrote = _apply_deck(src, aliases, her, declined)
            # Recorded the moment the import returns, before anything else in this
            # iteration can raise. `touched` is what _restore and _capture_shipped work
            # from, so a deck missing from it has its protected fields left as the
            # import overwrote them: the learner's annotations, gone for good. Every
            # later step here is best-effort by comparison.
            touched |= wrote
            seen[d["name"]] = {g for _, _f, g in apkg_notes(src)}
            # After the import, not before: the extra cloze cards only exist once the
            # cloze markup has actually landed on the note.
            seed_converted_siblings(changed_nids)
            applied[d["name"]] = d["version"]
            results.append(f"✓ <b>{short}</b>: {in_place} kept history, {as_new} new")
        except DownloadCancelled:
            # The learner clicked Cancel while this deck was still downloading, so
            # nothing of it has been imported. Same branch as a Cancel between decks
            # (whatever finished stays applied, and update_decks skips archive/relocate),
            # rather than a "✗ deck failed" row for something that did not fail.
            cancelled = True
            break
        except Exception as e:
            results.append(f"✗ <b>{short}</b>: {e}")
    # Merged into whatever is on disk now, not written back wholesale: a caller's own
    # view of installed.json is taken before the fetch phase, which can be minutes old
    # by the time a multi-deck run gets here, and saving that as-is would revert any
    # version another sync recorded in the meantime.
    _save_json(INSTALLED, {**_load_json(INSTALLED, {}), **applied})
    retired_guids = {g for per_deck in (manifest.get("retired") or {}).values()
                     for g in per_deck}
    if prune_declined(reg, retired_guids, seen):
        save_declined(reg)
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
    the next template change will offer again. Returns the note types actually changed.
    """
    if not tpl_changes:
        return []
    names = ", ".join(f"<b>{n}</b>" for n in sorted(tpl_changes))
    if _ask(
        f"This update also changes how some cards look (template or styling) for: "
        f"{names}.<br><br>Apply the new look now? Anki treats this as a schema "
        "change, so your next AnkiWeb sync will be a one-time full sync: choose "
        "\"Upload to AnkiWeb\" when asked.<br><br>Keeping your current look keeps "
        "your card appearance exactly as it is; your review history and card content "
        "are unaffected either way.",
        yes_label="Apply the new look", no_label="Keep my current look"
    ):
        return _apply_template_changes(tpl_changes)
    return []


def _retry_failed_downloads(fetch, todo, downloaded, her_fronts, aliases, cfg):
    """Download the decks whose preview download failed, before this run asks anything.

    `downloaded` is updated in place, so the apply step reuses these files instead of
    fetching a third time. Returns (conversions, cancelled): the note-type conversions
    found in them, for the run's single conversion question to cover, and whether the
    reader clicked Cancel. Nothing has been backed up or imported at this point, so a
    cancel here is the caller stopping outright rather than carrying on with a button
    that did nothing.

    This runs where it does because of what a conversion costs. Detected inside the
    apply loop instead, it is first seen under the progress dialog, with nowhere left
    to ask: the deck imports with the question never put on screen, the conversion
    silently declined, and, since the import records the deck as installed anyway,
    nothing offers it again until the source bumps that deck's version. The look change
    in such a deck is handled after the run instead (see _apply_consented_look), since
    the checkbox that would have carried it has already been answered.

    A deck that fails again is left exactly as it was: the apply loop fetches it once
    more, with Cancel live, and reports a second failure as the per-deck failure it is.
    """
    missing = [d for d in todo if not _is_local(downloaded.get(d["name"]))]
    if not missing:
        return [], False
    conversions, cancelled = [], False
    with cancellable_progress("Downloading decks", len(missing)) as step:
        for i, d in enumerate(missing, 1):
            short = d["name"].split("::")[-1]
            if not step(i, f"Downloading {short} ({i} of {len(missing)})"):
                cancelled = True
                break
            try:
                src = _cached_fetch(fetch, d, on_chunk=step.pump)
                downloaded[d["name"]] = src
                conversions += notetype_changes(src, her_fronts, aliases,
                                                cfg["scope_tag"])
            except DownloadCancelled:
                cancelled = True
                break
            except Exception:
                pass
    return conversions, cancelled


def _apply_consented_look(tpl_changes, tpl_choice, disclosed):
    """Apply the look change the reader agreed to, and ask about any she never saw.

    `tpl_changes` is what the import actually found, `disclosed` is what the
    confirmation named beside its checkbox, and the intersection is what the tick
    covers. A note type outside it came from a deck whose preview download failed, so
    this run only learned of its look change at import time: it is asked about here, in
    _offer_template_changes' own words, rather than applied on a tick that never named
    it or dropped silently, which would mean never offering it again, since the deck is
    recorded as installed at that version now. Returns the note types actually changed.
    """
    applied = []
    if tpl_choice and tpl_choice["checked"]:
        agreed = {n: s for n, s in tpl_changes.items() if n in disclosed}
        if agreed:
            applied += _apply_template_changes(agreed) or []
    applied += _offer_template_changes(
        {n: s for n, s in tpl_changes.items() if n not in disclosed}) or []
    return applied


def _deck_opted_out(deck, excluded):
    """Whether a card sitting in `deck` belongs to a deck the learner has unchecked.

    Manage decks says unchecking a deck stops future syncs for it, and archiving or
    relocating her cards is a sync doing something to them. Matched by prefix, since an
    excluded name is the manifest's, and her card is routinely in a subdeck of it (the
    same reason installed_matching_collection matches by prefix).
    """
    return bool(deck) and any(deck == x or deck.startswith(x + "::") for x in excluded)


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

    Two things are filtered out of all three before they leave here, so the screens and
    the actions can't disagree about them: a card sitting in a deck the learner has
    unchecked in Manage decks (unchecking one promises to stop future syncs for it, and
    archiving or relocating her cards is not what "stopped" means), and a relocation of
    a card this same pass is about to archive.
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

    def _opted_out(guid):
        """Read against where her copy lives, not against the ledger's own deck: the
        exclusion is about her card, and a ledger deck is only ever where the source
        filed it."""
        return _deck_opted_out(her_deck.get(guid), cfg["excluded"])

    fresh = [r for r in fresh if not _opted_out(r["guid"])]
    stranded = [p for p in stranded
                if not (_opted_out(p["guid"]) or _opted_out(p["successor_guid"]))]
    archived = {r["guid"] for r in fresh} | {p["guid"] for p in stranded}
    # A card in both ledgers is retired, not relocated: archiving moves it into the
    # Retired deck and a relocation immediately afterward pulls it straight back out
    # into a live deck, suspended and tagged, which is neither of the two outcomes.
    moves = [m for m in moves
             if m["guid"] not in archived and not _opted_out(m["guid"])]
    return her, fresh, already, moves, retired_deck, tag, stranded


def _stranded_lead(stranded):
    """What the reworded-pair section says before naming any of them.

    Worded around what she'll notice (two versions of the same card, progress on the
    one that's out of date) rather than around GUIDs, which is the actual cause but not
    something she should have to know about to say yes to this.
    """
    return (f"<b>{plural(len(stranded), 'card')}</b> "
            f"{'is' if len(stranded) == 1 else 'are'} in your collection twice, in an "
            "older and a newer wording of the same question, because the wording "
            "changed after your first import. Your progress on the older copy moves to "
            "the newer one, then the older copy is archived.")


def _stranded_lines(stranded):
    """One line per reworded pair: the wording she holds, then the one it becomes.

    Both halves stay in the one line rather than the newer one moving to a trailing
    column, because a card front is long enough to wrap and the trailing column does
    not: the pair is a single sentence about one card, not a value to compare down the
    list.

    Both halves are raw note fields (find_stranded_pairs is keyed by the front text
    _her_front_to_guid reads straight off the note), so each goes through
    note_display_label before it meets the row's own markup, exactly as a new or
    changed card's does.
    """
    muted = colors()["muted"]
    return [f"{note_display_label([p['front']])} <span style='color:{muted};'>→ "
            f"{note_display_label([p['successor_front']])}</span>"
            for p in stranded]


def _stranded_items(stranded):
    """The reworded-pair group for Update my decks' own list: the sentence that
    explains it, then one row per pair.

    The same group reconcile_decks builds from the same finding, in the row vocabulary
    build_update_body's list takes rather than build_list_body's. Chipped RETIRED for
    the same reason it is there: that is what happens to the half she is looking at,
    once its progress has moved across.
    """
    if not stranded:
        return []
    items = [("note", _stranded_lead(stranded))]
    append_rows(items, [("retired", line) for line in _stranded_lines(stranded)])
    return items


def _reconcile_backup_decks(fresh, moves, stranded, her):
    """Every deck a reconcile pass actually writes in, for the pre-run backup to cover.

    A retired card's ledger deck is where the deck source retired it FROM, which stops
    being true the moment the learner refiles her copy, so her copy's live deck wins and
    the ledger's is the fallback for a card the collection can't place. A stranded pair
    appears in neither ledger at all: _merge_stranded rewrites scheduling and protected
    fields on the successor and archives the predecessor, wherever those two currently
    sit, which is how a run could rewrite a card in a deck nothing had backed up.
    """
    decks = []
    for r in fresh:
        decks += decks_holding([r["guid"]], her) or [r["deck"]]
    decks += [m["from"] for m in moves]
    for p in stranded:
        decks += decks_holding([p["guid"], p["successor_guid"]], her)
    return decks


def _content_backup_decks(srcs, aliases, scope_tag):
    """Decks holding the learner's cards that an import of `srcs` would rewrite.

    A manifest's deck names say where the source files a card, not where she keeps it.
    An import matches by GUID (then front, then alias) wherever the note actually sits,
    so a card she has refiled herself is rewritten in a deck a manifest-name backup
    covers nothing of, which is the one card most likely to be worth restoring. Asked
    through remap_cards so this is the same match the import will make, rather than a
    second opinion about it.

    Only for a caller that already has the packages on disk (Update my decks' preview
    downloads, the auto-sync poll's own). A deck that can't be read is skipped: the
    apply step reports it as the failure it is, and it imports nothing to protect.
    """
    her = _her_front_to_guid(scope_tag)
    guids = set()
    for src in srcs:
        try:
            guids |= {g for _rid, _apkg_guid, g in remap_cards(src, her, aliases)[4]}
        except Exception:
            pass
    return decks_holding(guids, _her_guid_to_nid(scope_tag)) if guids else []


def _reworded_backup_decks(superseded, scope_tag):
    """Decks holding either wording of any pair the source has ever reworded.

    Wider than the pairs found before a run, and deliberately: update_decks recomputes
    stranded pairs AFTER its import, because the import itself can create one (a
    reworded front lands as a second note when her GUID didn't match), and that pair is
    merged in the same run. Its predecessor is a card she already holds, so it can be
    found and covered now, before the backup is taken; its successor is either already
    hers or arrives in a deck this run is backing up anyway.
    """
    if not superseded:
        return []
    wanted = set(superseded) | set(superseded.values())
    her_deck = _her_guid_to_deck(scope_tag)
    decks = []
    for front, guid in _her_front_to_guid(scope_tag).items():
        deck = her_deck.get(guid) if front in wanted else None
        if deck:
            decks.append(deck)
    return decks


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
@_manual_flow
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
        _source_warning(e)
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
            _info(f"{plural(already, 'retired card')} in your collection "
                  f"{'is' if already == 1 else 'are'} already archived (suspended and "
                  f"moved to <b>{RETIRED_DECK_LEAF}</b>). Nothing more to do.")
        else:
            _info("No retired cards or reorganized decks found in your collection — "
                  f"nothing to tidy up. (Source: {source}.)")
        return

    # A big first run (a large reorg landed before Reconcile was run even once) reads as
    # alarming without context — say up front that it's a one-time catch-up, not what to
    # expect going forward, so the length itself doesn't feel like something went wrong.
    catch_up_note = (
        "<i>This looks like a one-time catch-up — likely your first Reconcile since a "
        "larger update. Future runs should be much shorter.</i>"
        if len(fresh) + len(moves) + len(stranded) > 20 else "")

    # Every card here is a row, marked by what is about to happen to it, exactly as the
    # same cards read on Update my decks' own list: a retirement is RETIRED and a
    # relocation is MOVED there too, and the two screens act on the same two ledgers
    # (see _reconcile_pending), so they show the same thing the same way.
    #
    # Each group keeps the sentence that explains it, sitting directly above its own
    # rows rather than collected at the top, so a reader meets the explanation and the
    # cards it is about together. The lists themselves are uncapped: the cap existed
    # because a bare QMessageBox has no scroll area, so a long enough list (dozens of
    # relocated cards from a single reorg, as happened here) pushed the Yes/No buttons
    # off-screen with nothing to reach them. The rows stream and the buttons sit
    # outside the scroll area now, which is the structural version of that fix.
    items = []
    missing = sum(1 for r in fresh
                  if r["superseded_by"] and r["replacements_present"] == 0)
    already_note = (f" ({already} more {'was' if already == 1 else 'were'} already "
                    "archived earlier.)" if already else "")
    if fresh:
        items.append(("note",
                      f"<b>{plural(len(fresh), 'retired card')}</b> "
                      f"{'is' if len(fresh) == 1 else 'are'} still in your collection — "
                      "split or reworded since, with the replacements already added "
                      "separately, so "
                      f"{'it just duplicates' if len(fresh) == 1 else 'these just duplicate'} "
                      f"your reviews now.{already_note}"))
        append_rows(items, [("row", "retired", r["identity"],
                             r["deck"].split("::")[-1]) for r in fresh])
        if missing:
            items.append(("note",
                          f"<b>Note:</b> {missing} of these don't have their "
                          "replacement cards in your collection yet — run <b>Sync "
                          "decks</b> first if you want the new versions before "
                          "archiving the old ones."))
    if stranded:
        items.append(("note", _stranded_lead(stranded)))
        # Chipped RETIRED because that is what happens to the half she is looking at:
        # the older wording is archived once its progress has moved across.
        append_rows(items, [("row", "retired", line, "")
                            for line in _stranded_lines(stranded)])
    if moves:
        items.append(("note",
                      f"<b>{plural(len(moves), 'card')}</b> "
                      f"{'belongs' if len(moves) == 1 else 'belong'} to a deck that's "
                      "since been reorganized."))
        # Named through note_display_label like every other row that names a card: a
        # raw first field is HTML, so an image card renders as a broken picture here
        # and a cloze as its own braces.
        append_rows(items, [
            ("row", "moved", note_display_label(mw.col.get_note(her[m["guid"]]).fields),
             f"→ {m['to'].split('::')[-1]}") for m in moves])

    safety_note = (
        "Nothing is deleted. Archived cards keep their review history and can "
        "be brought back anytime by unsuspending them or moving them out of the "
        "Retired deck" +
        (", and any personal notes on them carry over to the replacement first."
         if fresh else ".") +
        " A backup is taken automatically before anything changes."
    )
    yes_label = " and ".join(
        x for x in ("Archive" if fresh or stranded else None,
                    "relocate" if moves else None) if x) or "Apply"
    if not _ask_with_widget(
        build_list_body(items, top_html=catch_up_note, bottom_html=safety_note),
        yes_label=yes_label, min_height=_CONFIRM_HEIGHT
    ):
        return

    proceed, backed_up = _pre_sync_backup_or_confirm_skip(
        cfg["export_deck"],
        _reconcile_backup_decks(fresh, moves, stranded, her), cfg["scope_tag"])
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
            f"Archived <b>{plural(n_archived, 'retired card')}</b> to "
            f"<b>{retired_deck}</b>: suspended and tagged <code>{tag}</code>, review "
            "history kept"
            + (f" ({plural(carried, 'personal note')} carried over to the replacement)"
               if carried else "") + ". Bring any back by unsuspending it or moving "
            "it out of the Retired deck.")
    if n_merged:
        result_lines.append(
            f"Merged <b>{plural(n_merged, 'reworded card')}</b>: your progress moved "
            "onto the current wording, and the older copy was archived alongside the "
            "rest.")
    if n_moved:
        result_lines.append(
            f"Moved <b>{plural(n_moved, 'card')}</b> to "
            f"{'its' if n_moved == 1 else 'their'} reorganized deck — content and "
            "scheduling untouched.")
    _info("<br><br>".join(result_lines) + backup_line)


@_safe
@_manual_flow
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
        _source_warning(e)
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

    # Every row here is the same thing happening to the same kind of card, so there is
    # nothing to mark one apart from another and nothing to line up against: no chip,
    # and the caret and chip columns declined (see widgets.simple_row). The list is
    # uncapped for the reason Reconcile's is: it streams inside a scroll area with the
    # buttons outside it, so naming every duplicate can no longer push them off-screen.
    heading, rows = duplicate_dialog_rows(groups)
    muted = colors()["muted"]
    items = []
    append_rows(items, [
        ("row", None,
         f"{r['label']} <span style='color:{muted};'>{r['detail']}</span>", "")
        for r in rows])
    safety_note = (
        "Nothing is deleted. Archived cards keep their review history and can "
        "be brought back anytime by unsuspending them or moving them out of the "
        "Retired deck, and any personal notes on them carry over to the kept copy "
        "first. A backup is taken automatically before anything changes.")
    if not _ask_with_widget(
        build_list_body(items, top_html=heading, bottom_html=safety_note,
                        card_columns=False),
        yes_label="Archive duplicates", min_height=_CONFIRM_HEIGHT
    ):
        return

    # Scoped from where each group's notes actually sit, not from export_deck: a
    # duplicate is found by tag across the whole collection, and both halves of a group
    # are written here (the kept copy receives the personal notes, the loser is
    # archived), so a group the learner keeps outside export_deck was rewritten with no
    # backup covering it while the confirmation above promised one.
    proceed, backed_up = _pre_sync_backup_or_confirm_skip(
        cfg["export_deck"],
        [n["deck"] for g in groups for n in [g["keep"], *g["archive"]]],
        cfg["scope_tag"])
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
    _info(f"Archived <b>{plural(n_archived, 'duplicate card')}</b> to "
          f"<b>{retired_deck}</b>: suspended and tagged <code>{tag}</code>, review "
          "history kept"
          + (f" ({plural(carried, 'personal note')} carried over to the kept copy)"
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


def _cached_fetch(fetch, d, on_chunk=None):
    hit = _apkg_cache.get(d["name"])
    if hit and hit[0] == d.get("version") and os.path.exists(hit[1]):
        return hit[1]
    path = fetch(d, on_chunk=on_chunk)
    _apkg_cache[d["name"]] = (d.get("version"), path)
    return path


def _is_local(entry):
    """Whether a {deck: path or Exception} entry is a file that is actually there.

    The path check is the same one _cached_fetch makes: the scratch directory is a
    temp directory, and a long-lived session can outlive a sweep of it, so an entry
    recorded minutes ago is not proof the file still exists.
    """
    return (entry is not None and not isinstance(entry, Exception)
            and os.path.exists(entry))


def _preview_content_changes(fetch, todo, her, aliases, her_fields=None):
    """Download every pending deck and match it against the collection, so the
    confirmation can show real "N kept · M new" counts instead of just each deck's
    total card count. A cancellable progress window covers it, since this is a live
    network fetch per deck and a multi-deck update on a slow link otherwise looks
    like a hang, with no way out, before the confirmation even appears.

    Returns ({deck_name: (kept, new, new_notes, changed) | None}, downloaded, cancelled).
    `new_notes` is remap_cards' own list of the notes that will import as new, carried
    through so the confirmation can name them and the review dialog can show them in
    full: it costs nothing extra here, since remap_cards already reads every note to
    count them. `downloaded` is {deck_name: local_path_or_Exception}, in the same shape
    background.py's auto-sync poll already uses, so the caller can hand it straight to
    _run_sync afterward instead of downloading every deck a second time. A per-deck
    failure here is recorded, not raised, so one bad download only blanks that deck's
    preview ("couldn't preview") rather than blocking the whole confirmation; the deck
    stays in the run and the apply step fetches it again (see update_decks), which is
    what makes that row's "still imports" true rather than a promise it can't keep. A
    deck whose file downloaded fine and only failed to parse keeps the file.
    `cancelled` means the learner clicked Cancel partway through: nothing has touched
    the collection at this point, so the caller can just stop outright.

    Downloads go through _cached_fetch, so re-opening Update my decks without applying
    doesn't re-fetch a deck whose version hasn't changed.

    Each entry is (kept, new, new_notes, changed), where `changed` is
    {note id: {field: her current value}} for the cards this deck would rewrite. That
    costs one extra labeled read of the same already-downloaded file, which is the price
    of the confirmation being able to say a card is about to change rather than only that
    it matched.
    """
    preview, downloaded = {}, {}
    with cancellable_progress("Checking for updates", len(todo)) as step:
        for i, d in enumerate(todo, 1):
            short = d["name"].split("::")[-1]
            if not step(i, f"Checking {short} ({i} of {len(todo)})"):
                return preview, downloaded, True
            try:
                # step.pump keeps Cancel live during the download itself, not just
                # between decks: one deck's fetch is a single blocking read on the main
                # thread, so without it the button is decorative for that whole stretch.
                src = _cached_fetch(fetch, d, on_chunk=step.pump)
                downloaded[d["name"]] = src
                _, kept, new, new_notes, matched = remap_cards(src, her, aliases)
                changed = {}
                if her_fields and matched:
                    changed = find_changed_notes(
                        matched, apkg_note_details(src), her_fields,
                        protected=_cfg()["protected"])
                preview[d["name"]] = (kept, new, new_notes, changed)
            except DownloadCancelled:
                # Cancel clicked mid-download rather than between decks. That is the
                # same answer, not a deck that failed to preview, so report it as the
                # cancel it is instead of leaving one row reading "couldn't preview".
                return preview, downloaded, True
            except Exception as e:
                # setdefault, not assignment: the fetch may well have succeeded and only
                # the read of it failed, and overwriting a perfectly good local file with
                # the parse error would make the apply step re-download a deck it already
                # has (or, before the retry below existed, refuse to import it at all).
                downloaded.setdefault(d["name"], e)
                preview[d["name"]] = None
    return preview, downloaded, False


def _gather_pending_items(todo, preview, downloaded, extra=None, registry=None):
    """Every card this update would touch, grouped under the deck it belongs to and
    ready for the inline card list on the confirmation.

    This always runs, unlike the old Review button's lazy read on a click: the rows are
    always on screen now, and widgets.StreamingList is what keeps building them cheap
    regardless of how many are pending, not deferring the read itself.

    `extra` is `_retired_moved_items`' {deck: [row, ...]}: rows that belong to a deck
    but are read from the ledgers rather than from a downloaded .apkg. They are folded
    in under that deck's own heading, after its added and changed cards, so one deck
    reads as one section covering everything happening to it. A deck whose only pending
    work is a retirement or a relocation still gets a heading of its own at the end,
    since it has rows to show and no other section would carry them.

    `registry` is config.load_declined()'s own {guid: entry}. A card previously
    declined "never" is dropped from the list entirely and counted in `hidden` instead
    of shown; one declined "skip"/"keep" is kept but tagged `detail["declined_state"]`
    (its own badge, see review._card_row) and, when the incoming fields hash differs
    from what was declined, `detail["changed_since_decline"]` too.

    Returns (items, failed, sources, hidden). `items` is a mix of ("header",
    deck_short_name), ("sep",), and ("card", deck_name, detail) entries plus whatever
    `extra` supplied, one card per pending row, each detail tagged "kind" ("new" or
    "changed") and, for a changed one, "was" (what her copy currently says), the same
    two things the old Review button's dialog used to tag before this screen replaced
    it. `failed` names decks whose pending cards could not be read; the update itself is
    unaffected, those decks just are not shown here. `sources` is {deck_name: .apkg
    path}, for the row pictures' resolvers.
    """
    items, failed, sources, hidden = [], [], {}, 0
    extra = dict(extra or {})

    def _extra_for(deck_name):
        """This deck's ledger rows, including any filed under a subdeck of it.

        A deck spec's name is routinely just the parent path, with cards actually
        landing in `deck_name::<subdeck>`, so a moved card's own deck path is often a
        subdeck of the manifest name the card rows are headed by. Matching by prefix as
        well as exactly is what keeps those in the same section rather than spawning a
        near-duplicate heading beside it (the same reason installed_matching_collection
        matches by prefix).
        """
        keys = [k for k in extra if k == deck_name or k.startswith(deck_name + "::")]
        return [row for k in keys for row in extra.pop(k)]

    def _section(heading, rows):
        items.append(("header", heading))
        for i, row in enumerate(rows):
            if i:
                items.append(("sep",))   # between rows, not before the first
            items.append(row)

    for d in todo:
        card_rows = []
        pc = preview.get(d["name"])
        src = downloaded.get(d["name"])
        if pc and src and not isinstance(src, Exception) and (pc[2] or pc[3]):
            try:
                rids = [r for r, _, _ in pc[2]] + list(pc[3])
                details = apkg_note_details(src, rids)
            except Exception as e:
                failed.append(f"{d['name'].split('::')[-1]} ({e})")
                details = []
            new_rids = {r for r, _, _ in pc[2]}
            for detail in details:
                detail["kind"] = "new" if detail["rid"] in new_rids else "changed"
                detail["was"] = pc[3].get(detail["rid"], {})
                entry = (registry or {}).get(detail["guid"])
                state = entry.get("state") if isinstance(entry, dict) else None
                if state == "never":
                    hidden += 1
                    continue
                if state:
                    detail["declined_state"] = state
                    incoming = [v for _, v in detail["fields"]]
                    if entry.get("hash") and entry["hash"] != note_fields_hash(incoming):
                        detail["changed_since_decline"] = True
                card_rows.append(("card", d["name"], detail))
            if details:
                sources[d["name"]] = src
        rows = card_rows + _extra_for(d["name"])
        if rows:
            _section(d["name"].split("::")[-1], rows)

    for deck, rows in extra.items():
        _section(deck.split("::")[-1], rows)
    return items, failed, sources, hidden


def _retired_moved_items(fresh, moves, her):
    """Retired and relocated cards as rows for the same inline list `_gather_pending_items`
    builds the new and changed rows for, so a retired split/reword and a deck reorg read
    as more cards in one list rather than as bulleted asides below it.

    Keyed by the deck each card belongs to, so `_gather_pending_items` can file them
    under that deck's own heading beside its added and changed cards. The two kinds know
    their deck from different places: a retired card carries the retirement ledger's own
    deck (the deck it was retired out of), and a relocated card has only the two ends of
    its move, of which `from` is where it sits today and so where a reader looking at
    that deck's section expects to find it. Its destination is the one thing that
    heading cannot say, so a moved row keeps naming it; a retired row does not repeat
    the deck name the heading above it already gives.

    Single-line and never expanding (see widgets.simple_row): these are the learner's own
    cards, known only by front (or identity) and a deck, so there is nothing more to read
    out of the collection for either kind. Within a deck they come after its cards and in
    the order archived, then moved, so the reader meets all four kinds in one sensible
    sequence: what is being added, what is changing, what is being archived, what is
    moving.

    Returns {deck: [row, ...]} where each row is ("retired", identity) or ("moved",
    front, dest_deck_short), empty when there is nothing of either kind pending. `her`
    is `_reconcile_pending`'s own {guid: nid} map, needed to read a moved card's current
    front out of the collection.
    """
    by_deck = {}
    for r in fresh:
        by_deck.setdefault(r["deck"], []).append(("retired", r["identity"]))
    for m in moves:
        # note_display_label, not the raw first field: these rows sit in the same list
        # as the new and changed cards, which are labeled that way for the reason an
        # image note's first field is an <img> and a cloze's is its own braces.
        front = note_display_label(mw.col.get_note(her[m["guid"]]).fields)
        by_deck.setdefault(m["from"], []).append(
            ("moved", front, m["to"].split("::")[-1]))
    return by_deck


# The fixed reassurance below the update confirmation's list. Module-level so the
# real-Qt harness renders this exact string in its paint and layout tests rather
# than a paraphrase that drifts shorter than what the screen actually wraps.
_UPDATE_SAFETY_NOTE = (
    "This is a preview: nothing above has been applied yet. Your "
    "review history and any personal notes on existing cards are kept (matched "
    "by card, not overwritten). Archived cards keep their history too and can "
    "be brought back anytime by unsuspending them or moving them out of the "
    "Retired deck, nothing here is ever deleted. A backup is taken "
    "automatically first. Skipped cards come back next update, already marked. "
    "Cards you never import can be restored under Manage decks > Declined "
    "cards.")


@_safe
@_manual_flow
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
    reg = load_declined()
    fetched = _fetch_manifest_gated(cfg)
    if not fetched:
        return
    manifest, fetch, source = fetched

    installed = installed_matching_collection(_load_json(INSTALLED, {}), cfg["scope_tag"])
    todo = decks_to_update(manifest, installed, cfg["excluded"])
    her, fresh, _already, moves, retired_deck, tag, stranded = _reconcile_pending(
        manifest, cfg)

    if not todo and not fresh and not moves and not stranded:
        _refresh_reconcile_action_label(0)
        _info(f"You're all up to date (source: {source}).")
        return

    preview, downloaded, collisions = {}, {}, []
    if todo:
        preview, downloaded, cancelled = _preview_content_changes(
            fetch, todo, _her_front_to_guid(cfg["scope_tag"]),
            manifest.get("front_aliases", {}), _her_guid_to_fields(cfg["scope_tag"]))
        if cancelled:
            _info("Update cancelled — nothing was changed.")
            return

    # Every card this update would add, in deck order then .apkg order. `new_index` maps
    # each one's GUID back to where it came from, because the review dialog only knows
    # GUIDs: without it a flag she writes couldn't say which deck or card it was about.
    # `incoming_hashes` rides along the same two loops, since both already hold each
    # card's fields: it's what a Skip/Keep this run records into the declined registry.
    new_cards, incoming_hashes = [], {}
    for d in todo:
        pc = preview.get(d["name"])
        if not pc:
            # This deck couldn't be previewed; its row already says so, and the apply
            # step fetches it again, so it is only missing from this list, not the run.
            continue
        for _, fields, guid in pc[2]:
            new_cards.append((d["name"], note_display_label(fields), guid))
            incoming_hashes[guid] = note_fields_hash(fields)
    # The cards this update would rewrite, gathered the same way as the added ones: the
    # review dialog only knows note ids and guids, so the deck and a readable label have
    # to be carried alongside. A deck whose download failed, or whose details can't be
    # read for some other reason, is skipped here: the update itself still applies, it
    # just can't be listed in this section.
    changed_cards = []
    for d in todo:
        pc = preview.get(d["name"])
        src = downloaded.get(d["name"])
        if not pc or not pc[3] or not src or isinstance(src, Exception):
            continue
        try:
            details = apkg_note_details(src, list(pc[3]))
        except Exception:
            continue    # this deck still imports; it just cannot be listed here
        for detail in details:
            incoming = [v for _, v in detail["fields"]]
            changed_cards.append((d["name"], note_display_label(incoming), detail["guid"]))
            incoming_hashes[detail["guid"]] = note_fields_hash(incoming)
    # Detected here, not mid-import, because the download that reveals it has already
    # happened: the preview above fetched every pending deck. Knowing it now is what
    # lets the one confirmation carry the decision, instead of interrupting the run
    # with its own question after the import has started.
    # Note-type conversions are read from the same already-downloaded packages, for the
    # same reason and at the same moment: knowing the whole run's total now is what lets
    # one question cover it, asked before the apply loop starts, instead of
    # _offer_notetype_changes interrupting each deck's import with its own modal from
    # under the progress dialog. Sync decks still asks per deck (see _run_sync's
    # `convert_notetypes`), exactly as the two flows already differ over templates.
    pending_templates, pending_conversions = {}, []
    her_fronts = _her_front_to_guid(cfg["scope_tag"])
    aliases = manifest.get("front_aliases", {})
    for d in todo:
        src = downloaded.get(d["name"])
        if not src or isinstance(src, Exception):
            continue
        try:
            pending_templates.update(_template_changes(src))
        except Exception:
            pass    # a deck we can't read here still imports; it just can't offer this
        try:
            pending_conversions += notetype_changes(src, her_fronts, aliases,
                                                    cfg["scope_tag"])
        except Exception:
            pass    # same: unreadable here, still imported, just not offered up front

    new_index = {guid: (deck, label) for deck, label, guid in new_cards + changed_cards}
    flags = {}
    # Notes from a run that never reached its digest (a crash, a force quit, an error
    # mid-import) come back here rather than needing their own recovery prompt: they
    # ride along in this run's flags and land in the summary at the end like anything
    # written just now. merge_saved_feedback carries each one's deck and front too,
    # since the card itself may have imported last time and so appear nowhere in this
    # run's own index.
    recovered = merge_saved_feedback(load_saved_feedback(), flags, new_index)

    # Cards already declined Never are hidden from the list below, so the per-deck
    # counts must not pitch them as pending either. A new card's guid reads straight
    # off its deck's preview; a changed card's guid only exists in `changed_cards`,
    # so those hides are tallied per deck here.
    never_guids = {g for g, e in reg.items()
                   if isinstance(e, dict) and e.get("state") == "never"}
    hidden_changed = {}
    for deck_name, _label, g in changed_cards:
        if g in never_guids:
            hidden_changed[deck_name] = hidden_changed.get(deck_name, 0) + 1

    def _deck_summary_row(d):
        """One deck's line in the summary that opens the list: the deck as the row's
        primary text, its counts as the trailing muted column.

        The changed count is a subset of kept, not a third bucket beside it: those
        cards count as "changed" precisely because they matched an existing card. So
        it's parenthesized onto "kept" rather than joined with the same "·" new gets,
        which would read as three disjoint piles adding up to more cards than the deck
        actually has.
        """
        short = d["name"].split("::")[-1]
        pc = preview.get(d["name"])
        if pc is None:
            # Say what happens anyway: the download failed here, but the deck is still
            # in this run and Update still tries to import it, so a bare "couldn't
            # preview" reads as "this deck is being skipped", which it isn't.
            return ("deck", short, "couldn't preview · still imports")
        changing = len(pc[3]) - hidden_changed.get(d["name"], 0)
        kept = f"{pc[0]} kept" + (f" ({changing} changing)" if changing else "")
        new_count = sum(1 for _rid, _fields, g in pc[2] if g not in never_guids)
        return ("deck", short, f"{kept} · {new_count} new")

    muted = colors()["muted"]
    sections = []
    # The deck summary is the list's own first section rather than a bulleted paragraph
    # above it (see the `items` assembly below), which is also what keeps its heading
    # next to the rows it counts however many other notes end up in this fixed text.
    # new_cards and changed_cards get no section here either, and neither do the
    # retired or relocated cards: all four kinds are what the inline card list is built
    # from (see _gather_pending_items and _retired_moved_items), which is the whole
    # point of this screen replacing the old "Review N card(s)" button and the bulleted
    # lists that used to sit beside it. new_cards/changed_cards themselves are still
    # needed above, to build new_index. The reworded pairs are the one group that used
    # to sit up here as prose; they are cards like everything else on this screen, so
    # they read as rows at the end of the list instead.

    # A big first run (a large backlog accumulated before Update was run even once)
    # reads as alarming without context — say up front it's a one-time catch-up.
    catch_up_note = (
        "<i>This looks like a one-time catch-up — likely your first update in a "
        "while. Future updates should be much shorter.</i>"
        if len(fresh) + len(moves) + len(stranded) > 20 else "")
    # {"skip": "...", "keep": "...", "never": "..."} counts, read off `decisions` below
    # (defined just ahead of build_update_body): a row's own control words, not the
    # digest's reader-facing ones, since this line sits beside the rows themselves.
    # A fixed word order, not decisions.values()'s own insertion order (click order),
    # so the tally reads the same regardless of which row she touched first.
    _TALLY_WORDS = (("skip", "skipped for now"), ("keep", "kept mine for now"),
                    ("never", "never"))

    def _decision_tally():
        counts = {}
        for state in decisions.values():
            counts[state] = counts.get(state, 0) + 1
        parts = [f"{counts[s]} {word}" for s, word in _TALLY_WORDS if s in counts]
        return ", ".join(parts) + "." if parts else ""

    def _status_line():
        parts = []
        if flags:
            if not recovered:
                carried_txt = ""
            elif len(flags) == 1:
                # "1 card flagged. 1 of them carried over" read as a count of something
                # there is only one of.
                carried_txt = " It carried over from an earlier session."
            else:
                carried_txt = f" {recovered} of them carried over from an earlier session."
            parts.append(f"<b>{plural(len(flags), 'card')} flagged.</b>{carried_txt} "
                         "You'll get a summary to send back when this finishes.")
        tally = _decision_tally()
        if tally:
            parts.append(tally)
        if hidden:
            parts.append(f"{plural(hidden, 'card')} hidden (Never). Restore them under "
                         "Manage decks > Declined cards.")
        return ("<br><br>".join(parts) + "<br><br>") if parts else ""

    def _finish(title=None, items=(), run_decisions=None):
        """End the run: the summary and her notes as one dialog, then drop the saved
        copy of those notes.

        Called on every exit path including Cancel, deliberately: if she read the new
        cards, flagged three of them and backed out, those flags are the most
        interesting thing that happened, and dropping them because she said no would
        throw away the only part of the run that couldn't be reproduced by clicking
        Update again later.

        `title` is the run's own headline and `items` is everything below it in
        build_list_body's vocabulary: a row per deck or archive/merge/move outcome, and
        a paragraph for each of the preserved-field and collision notes and the backup
        line. Passed straight through to show_result_with_feedback, which renders them
        in the same row vocabulary the confirmation used. Left at their defaults on a
        Cancel or declined backup, where there is no completed run to summarize at all.
        `run_decisions` is the reader-facing {guid: "skipped"/"kept yours"/"never"} for
        whatever this run actually decided (computed once, right after the registry
        write below). Left at its default (none) only on declining the confirmation
        itself, the one exit before that write ever runs; every later exit, even one
        that stops before anything imports, already has decisions on disk by then and
        passes this through so the digest agrees with what the registry now holds.

        The saved copy is cleared only once the digest has actually been built and
        shown. An exit with nothing flagged leaves the file alone rather than deleting
        it: there is nothing to clear in that case anyway, and treating "no flags this
        run" as "safe to forget" is exactly how a recovered note would get thrown away
        a second time.
        """
        entries = feedback_entries(flags, new_index, run_decisions)
        show_result_with_feedback(title, items, entries)
        if entries:
            clear_saved_feedback()

    # Unticked by default: applying it forces a one-time full AnkiWeb sync, which is
    # not something a reader should be able to agree to by not reading a checkbox.
    # Declining still imports the content; only the card's appearance stays as it is,
    # and the next update carrying a look change offers it again.
    tpl_choice = {"label": "Also apply the new card look (forces a one-time full "
                           "AnkiWeb sync)", "checked": False} if pending_templates else None
    # Disclosed here so the one question asked after this (see below) isn't the first
    # the reader hears of it. It can't be a second checkbox: _ask_with_widget carries
    # one, and the look change already has it.
    if pending_conversions:
        sections.append(
            f"<b>{plural(len(pending_conversions), 'card')}</b> in this update changed "
            "format (a question and answer became a fill-in-the-blank). You'll be asked "
            "once, before anything imports, whether to move your existing cards across.")

    items, unreadable, sources, hidden = _gather_pending_items(
        todo, preview, downloaded, _retired_moved_items(fresh, moves, her),
        registry=reg)
    if todo:
        summary = [("header", f"{plural(len(todo), 'deck')} "
                              f"{'has' if len(todo) == 1 else 'have'} updates:")]
        for i, d in enumerate(todo):
            if i:
                summary.append(("sep",))
            summary.append(_deck_summary_row(d))
        items = summary + items
    items += _stranded_items(stranded)
    if unreadable:
        sections.append(
            f"<span style='color:{muted};'>Couldn't read the pending cards from "
            + ", ".join(unreadable) + ". The update itself is unaffected; those decks "
            "just aren't shown here.</span>")
    # Last of the blocks above the list, and deliberately: ui._place_checkbox puts the
    # look-change box directly under all of this fixed text, so whichever paragraph
    # ends up last is the one it reads as answering. With a format change pending too,
    # that used to be the format paragraph, whose own question has its own dialog.
    if tpl_choice:
        sections.append(
            "This update also changes how some cards look (template or styling) for: "
            + ", ".join(f"<b>{n}</b>" for n in sorted(pending_templates))
            + ". Your review history and card content are unaffected either way.")

    # The catch-up note reads as the first of these blocks rather than carrying its own
    # trailing break: with the per-deck summary now inside the list, every one of these
    # blocks is optional, and a fixed break belonging to one of them leaves a blank
    # line hanging above the list on the runs where it is the only thing here.
    top_html = "<br><br>".join(b for b in [catch_up_note] + sections if b)
    # Seeded by build_update_body itself from `items`' own predeclined details before
    # any row is built; read back here once the dialog closes to know what she decided.
    decisions = {}
    touched = set()
    body, _boxes, flush = build_update_body(
        items, sources, flags, new_index, decisions, top_html,
        _status_line, _UPDATE_SAFETY_NOTE, touched)

    # "Update" only when there is content to update. With nothing pending but retired
    # and relocated cards, this run does exactly what Reconcile my decks does, so it
    # says what that says rather than promising an update that isn't part of it.
    yes_label = "Update" if todo else (" and ".join(
        x for x in ("Archive" if fresh or stranded else None,
                    "relocate" if moves else None) if x) or "Apply")

    # flush runs through on_close rather than after this call: it reads the notes typed
    # into the cards and stops their save timer, and every one of those widgets belongs
    # to the dialog, so by the time this returns Qt has already freed them.
    #
    # min_width raised past _ask_with_widget's own 560px default: a card's decision
    # control sits at the right of its header, beside the primary text it shares that
    # row with, and 560 left too little of the row for the card's own words.
    accepted = _ask_with_widget(body, yes_label=yes_label, checkbox=tpl_choice,
                                on_close=flush, min_width=660)

    if not accepted:
        _finish()
        return

    # Folded into the declined registry now, before anything else about this run
    # happens: a Skip/Keep/Never she chose has to survive even if the apply loop below
    # gets cancelled partway through. `row_kind` is every card row's own kind, which is
    # what decides both what its control could show and what "she flipped it back to
    # default" can mean below.
    row_kind = {item[2]["guid"]: item[2].get("kind")
               for item in items if item[0] == "card"}
    prior = dict(reg)
    today = datetime.date.today().isoformat()

    def _prior_entry(g):
        # A hand-edited registry can hold a garbage (non-dict) value for a guid; that
        # must read as "no prior decline" here rather than crash the write below.
        e = prior.get(g)
        return e if isinstance(e, dict) else {}

    def _registry_entry(guid, state):
        # Every guid written below belongs to a card row on the confirmation, and
        # every row's card was read into incoming_hashes by the preview above, so the
        # empty-hash default is unreachable in practice and only costs one missing
        # changed-since-decline cue if that ever changes.
        deck, front = new_index.get(guid, ("", guid))
        return {"state": state, "front": plain_text(front), "deck": deck,
                "decided": today, "hash": incoming_hashes.get(guid, "")}

    # "Decided this run" is the one predicate the write below and the digest both key
    # off of: a guid whose state actually changed from what the registry held when the
    # dialog opened. A row left exactly as it was seeded (the common case for a
    # re-offered decline nobody touched) is neither rewritten nor reported as decided
    # again, carrying its prior hash/decided/front forward untouched, which is what
    # keeps a pending "changed since decline" cue from being silently cleared by an
    # unrelated accepted update.
    run_decisions = {g: {"skip": "skipped", "keep": "kept yours", "never": "never"}[s]
                     for g, s in decisions.items() if _prior_entry(g).get("state") != s}
    for guid in run_decisions:
        reg[guid] = _registry_entry(guid, decisions[guid])
    # A guid can also sit in `decisions` at the very state the registry already held,
    # while still being one she actively re-decided: `touched` (review._on_decide)
    # fires on every click, even one that lands back on the state it was already
    # showing. That is what a re-review of a stale-hash card looks like, and the
    # stored hash/front must refresh to match, but it is not a new decision, so it
    # stays out of `run_decisions` and off the digest.
    for guid, s in decisions.items():
        if guid in touched and _prior_entry(guid).get("state") == s:
            reg[guid] = _registry_entry(guid, s)
    # A guid drops out of `decisions` (review._card_row's _on_change) only when her own
    # click set its control back to that row's default, so absence here normally means
    # she chose that. But the only prior state a visible row can have been seeded with
    # is the one `_EXPRESSIBLE_DECLINE` names for its current kind (build_update_body's
    # seeding accepts any non-default state the control offers, which on a new row
    # includes "never" too, but a "never" entry hides its row upstream and so never
    # reaches this loop at all), so a "keep" entry whose row has since gone back to
    # being new, for instance, was never a candidate for seeding in the first place:
    # its absence from `decisions` says nothing about her intent this run by itself. `touched` is what
    # tells that apart from an actual un-decline on such a row: an active click that
    # confirms the (now different) default is just as much a decision as flipping a
    # kind-matched row back to default always was.
    _EXPRESSIBLE_DECLINE = {"new": "skip", "changed": "keep"}
    for guid in [g for g in reg
                 if g not in decisions and g in row_kind
                 and (_prior_entry(g).get("state") == _EXPRESSIBLE_DECLINE.get(row_kind.get(g))
                      or (g in touched and _prior_entry(g).get("state") is not None))]:
        del reg[guid]        # she flipped a standing decline back to the default
        run_decisions[guid] = "imported after all"
    save_declined(reg)

    if todo:
        late_conversions, cancelled = _retry_failed_downloads(
            fetch, todo, downloaded, her_fronts, aliases, cfg)
        if cancelled:
            # Nothing has been backed up or imported yet, so this is the same clean
            # stop cancelling the confirmation itself is, except the registry write
            # above already happened by this point, so whatever she decided is
            # reported through run_decisions rather than silently going unreported.
            _finish(run_decisions=run_decisions)
            return
        pending_conversions += late_conversions

    # The decks this run will write in: the ones it imports into, the ones its imports
    # will actually rewrite a card in (which is not the same list, see
    # _content_backup_decks), the ones the cards it archives and relocates actually sit
    # in, and the ones holding either wording of a reworded pair, since the merge step
    # below runs against a list recomputed after the import and so can act on a pair
    # this very run created.
    proceed, backed_up = _pre_sync_backup_or_confirm_skip(
        cfg["export_deck"],
        [d["name"] for d in todo]
        + _content_backup_decks([v for v in downloaded.values() if _is_local(v)],
                                aliases, cfg["scope_tag"])
        + _reconcile_backup_decks(fresh, moves, stranded, her)
        + _reworded_backup_decks(manifest.get("superseded_fronts", {}), cfg["scope_tag"]),
        cfg["scope_tag"])
    if not proceed:
        # Same as the retry-cancel above: the registry write already happened, so
        # report what she decided rather than dropping it from the digest.
        _finish(run_decisions=run_decisions)
        return

    # Asked once, here, for the whole run: after the backup and before the first import,
    # so nothing interrupts the apply loop from under its own progress dialog. Declining
    # is a real choice with a real cost, which is why it stays a question rather than
    # becoming a default either way.
    convert = bool(pending_conversions) and _ask(
        f"<b>{plural(len(pending_conversions), 'card')}</b> in this update changed "
        "format (a question and answer became a fill-in-the-blank).<br><br>Move your "
        "existing cards to the new format? They keep their review history and stay one "
        "card each. Anki treats this as a schema change, so your next AnkiWeb sync will "
        "be a one-time full sync, choose \"Upload to AnkiWeb\" when asked.<br><br>"
        "Choosing to import them as new still imports them, as separate new cards "
        "beside the ones you have, leaving your progress on the old versions.",
        yes_label="Move my cards across", no_label="Import them as new")

    results, restored, tpl_changes = [], 0, {}
    if todo:
        # A cancellable progress window while each deck imports: the preview step
        # above already covered the download itself.
        with cancellable_progress("Updating decks", len(todo)) as step:
            def _already_fetched(d):
                # Reuses _preview_content_changes' download above instead of fetching
                # every deck a second time, the same pattern background.py's auto-sync
                # poll uses for the same reason. A deck the preview couldn't fetch is
                # fetched again here rather than re-raising the cached failure: its row
                # said the deck still imports, and a failed download is usually a
                # transient hiccup that a second attempt clears, whereas re-raising made
                # that row a promise the run could never keep. Cancel stays live during
                # the retry through the same step.pump the preview used, and a retry
                # that fails again raises into _run_sync's per-deck try/except, which
                # reports it as the honest per-deck failure it is. A file swept out of
                # the tempdir since it was downloaded counts as not fetched (_is_local),
                # the same check _cached_fetch makes before trusting its own entry.
                v = downloaded.get(d["name"])
                return v if _is_local(v) else _cached_fetch(fetch, d,
                                                            on_chunk=step.pump)

            results, restored, tpl_changes, _, cancelled, collisions, _conv = _run_sync(
                cfg, manifest, _already_fetched, todo,
                on_progress=lambda i, n, name: step(i, f"Applying {name} ({i} of {n})"),
                convert_notetypes=convert)

        if cancelled:
            # Stop here rather than falling through to archive/relocate: that step
            # assumes every content update already landed, so a retired card's
            # replacement is in place before the old one archives out. Whatever
            # decks _run_sync did finish are already fully applied and persisted
            # (see its docstring) — only the decks after the cancel point, and the
            # reconcile pass, are what's left pending for next time.
            #
            # The look change is one of the things those finished decks landed, so a
            # ticked checkbox is honored for them here too. Returning without it left
            # the cards imported and looking as they did before, with the consent
            # thrown away and nothing to offer it again: the decks that did apply are
            # recorded as installed, so the next run has no update to carry it on.
            looks = _apply_consented_look(tpl_changes, tpl_choice, pending_templates)
            backup_line = (
                "A pre-sync backup of the Intern Pearls deck was saved; use "
                "<i>Advanced → Restore intern pearls deck</i> to revert to it if needed."
                if backed_up else
                "No pre-sync backup was taken this time (nothing to back up yet, or "
                "it failed and you chose to continue).")
            items = []
            append_rows(items, [("row", None, line, "") for line in results])
            items.append(("note",
                          "Archiving or relocating retired cards was skipped, since "
                          "that assumes every update above already landed. Nothing else "
                          "was touched; run <b>Update my decks</b> again anytime to pick "
                          "up where this left off."))
            if restored:
                items.append(
                    ("note", f"Preserved fields restored on {plural(restored, 'card')}."))
            if looks:
                items.append(("note", "The new card look was applied for the decks "
                                      "that finished before you stopped."))
            # The decks that did apply before the cancel can have collided with her own
            # edits exactly as a finished run's can, and those cards are the one thing
            # here she may want to act on, so a stopped run reports them too.
            items += _collision_items(collisions)
            items.append(("note", backup_line))
            _finish(f"Update stopped early (source: {source})", items, run_decisions)
            return
        # Consented to on the confirmation, so nothing is normally asked here: the
        # checkbox is what she agreed to and tpl_changes is what the import found. A
        # change the confirmation never named is the one exception (see
        # _apply_consented_look).
        _apply_consented_look(tpl_changes, tpl_choice, pending_templates)

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
        # Filtered against the decks she has unchecked exactly as _reconcile_pending
        # filters its own, so a pair this recompute finds in an opted-out deck isn't
        # merged by the one path that doesn't go through it.
        her_deck = _her_guid_to_deck(cfg["scope_tag"])
        stranded = [p for p in find_stranded_pairs(
            manifest.get("superseded_fronts", {}), _her_front_to_guid(cfg["scope_tag"]))
            if p["guid"] in her and p["successor_guid"] in her
            and tag not in mw.col.get_note(her[p["guid"]]).tags
            and not _deck_opted_out(her_deck.get(p["guid"]), cfg["excluded"])
            and not _deck_opted_out(her_deck.get(p["successor_guid"]), cfg["excluded"])]
        carried = carry_over_protected_fields(fresh, her, cfg["protected"])
        n_merged = _merge_stranded(stranded, her, cfg["protected"], retired_deck, tag)
        n_archived = archive_notes([her[r["guid"]] for r in fresh], retired_deck, tag)
        n_moved = apply_deck_moves(moves, her)
        mw.reset()
        _refresh_reconcile_action_label(0)   # this run just handled everything found

    result_lines = list(results)
    if n_archived:
        result_lines.append(
            f"✓ Archived <b>{plural(n_archived, 'retired card')}</b> to "
            f"<b>{retired_deck}</b>"
            + (f" ({plural(carried, 'personal note')} carried over)" if carried else "")
            + ".")
    if n_merged:
        result_lines.append(
            f"✓ Merged <b>{plural(n_merged, 'reworded card')}</b>: your progress moved "
            "onto the current wording, older copy archived.")
    if n_moved:
        result_lines.append(
            f"✓ Moved <b>{plural(n_moved, 'card')}</b> to "
            f"{'its' if n_moved == 1 else 'their'} reorganized deck.")

    backup_line = (
        "A pre-sync backup of the Intern Pearls deck was saved; use "
        "<i>Advanced → Restore intern pearls deck</i> to revert to it if needed."
        if backed_up else
        "No pre-sync backup was taken this time (nothing to back up yet, or it "
        "failed and you chose to continue).")
    items = []
    append_rows(items, [("row", None, line, "") for line in result_lines])
    if restored:
        items.append(("note", f"Preserved fields restored on {plural(restored, 'card')}."))
    items += _collision_items(collisions)
    items.append(("note", backup_line))
    _finish(f"Update complete (source: {source})", items, run_decisions)


@_safe
@_manual_flow
def import_single():
    """Import one hand-picked, spec-authored .apkg outside the configured source.

    For a deck someone sent you directly, or a build you're testing before pushing it
    to the source repo. Does the same personalization, backup, and note-restore Sync
    does, just for one file you choose instead of everything the manifest lists, and in
    the same order _run_sync does it: confirm, back up, then fix note types. That order
    matters, since _ensure_notetypes can bump the collection schema (a one-time full
    AnkiWeb sync), and answering No to "Import now?" must not have cost anyone that.
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
                    f"({e}).<br><br>Without it, any card whose front text changed there "
                    "will be treated as new instead of matching your existing card, "
                    "so its history won't carry over. Continue anyway?",
                    yes_label="Import without it", no_label="Cancel"):
            return
    her = _her_front_to_guid(cfg["scope_tag"])
    remap, in_place, as_new, _, _matched = remap_cards(src, her, aliases)
    # Filtered the same way _apply_deck filters a regular sync's import: a declined
    # note must never land through this path either, whatever counts get shown next.
    declined = set(load_declined())
    drop, touched, in_place, as_new = declined_drop(src, remap, her, declined,
                                                     in_place, as_new)
    if not _ask(f"{plural(in_place, 'card')} will keep "
                f"{'its' if in_place == 1 else 'their'} history, {as_new} will be added "
                "as new. A backup is taken automatically first. Import now?",
                yes_label="Import", no_label="Cancel"):
        return
    # Scoped from the file's own deck names, so a package filing cards outside
    # export_deck is backed up where those cards will actually land. A package this
    # build can't read (a modern, zstd-compressed export) falls back to the configured
    # deck, exactly as this always did.
    try:
        file_decks = apkg_deck_names(src)
    except Exception:
        file_decks = None
    if not _pre_sync_backup_or_confirm_skip(cfg["export_deck"], file_decks,
                                            cfg["scope_tag"])[0]:
        return
    _ensure_notetypes()
    tpl = _template_changes(src)
    snap = _snapshot(cfg["protected"], cfg["scope_tag"])
    # Written into this session's own scratch directory rather than beside the file the
    # learner picked: that path is hers, may not be writable, and a fixed derived name
    # in a shared folder is the same predictable-target problem the downloads had.
    fd, out = tempfile.mkstemp(suffix=".sync.apkg", dir=_scratch())
    os.close(fd)
    write_personalized(src, remap, out, drop=drop)
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
    fields_line = (f" Preserved fields restored on {plural(restored, 'card')}."
                   if restored else "")
    _info(f"Imported {os.path.basename(src)}: {in_place} kept history, {as_new} new."
          f"{fields_line}")
