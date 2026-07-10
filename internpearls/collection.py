"""Everything that reads or writes the Anki collection directly.

Note-type reconciliation, backups (deck-scoped and whole-collection), the protected-
fields snapshot/restore round trip, .apkg import/export, and the Advanced menu actions
that are thin user-facing wrappers over those helpers. The sync flows in sync.py
compose these; nothing here fetches from the network.
"""
import datetime
import os

from aqt import mw
from aqt.utils import getFile, getSaveFile

from .config import DECK_BACKUPS_KEEP, TARGET_FIELDS, _USER_FILES, _cfg
from .logic import (apkg_models, bullets, changed_templates, model_shape,
                    remap_cards, write_personalized)
from .ui import _ask, _info, _safe, _warn


def _ensure_notetypes():
    mm = mw.col.models
    added = []
    for m in mm.all():
        target = TARGET_FIELDS.get(m["name"])
        if not target:
            continue
        existing = [f["name"] for f in m["flds"]]
        changed = False
        for fname in target:
            if fname not in existing:
                mm.add_field(m, mm.new_field(fname))
                added.append(f'{m["name"]}: +{fname}')
                changed = True
        if changed:
            (mm.update_dict if hasattr(mm, "update_dict") else mm.save)(m)
    if added:
        mw.reset()
    return added


def _template_changes(src):
    """Managed note types whose card templates or CSS differ between the .apkg at
    `src` and this collection. Returns {name: incoming model_shape}.

    This exists because imports run with merge_notetypes=False (see _import_apkg): a
    template change in a rebuilt deck never propagates on its own, so sync detects it
    here and asks, instead of silently shipping cards that render with the old look.
    Only note types in TARGET_FIELDS are checked — a learner's own types are not our
    business.
    """
    incoming = {n: s for n, s in apkg_models(src).items() if n in TARGET_FIELDS}
    existing = {m["name"]: model_shape(m) for m in mw.col.models.all()
                if m["name"] in TARGET_FIELDS}
    return {n: incoming[n] for n in changed_templates(incoming, existing)}


def _apply_template_changes(changes):
    """Write the incoming CSS and template HTML onto the collection's matching note
    types. Anki treats this as a schema change, so the caller must have warned the
    user about the resulting one-time full AnkiWeb sync before calling this.

    Only templates matched by name are updated; templates are never added, removed,
    or reordered here (all managed note types have exactly one).
    """
    mm = mw.col.models
    applied = []
    for m in mm.all():
        inc = changes.get(m["name"])
        if not inc:
            continue
        m["css"] = inc["css"]
        by_name = {name: (qfmt, afmt) for name, qfmt, afmt in inc["tmpls"]}
        for t in m["tmpls"]:
            if t.get("name") in by_name:
                t["qfmt"], t["afmt"] = by_name[t["name"]]
        (mm.update_dict if hasattr(mm, "update_dict") else mm.save)(m)
        applied.append(m["name"])
    if applied:
        mw.reset()
    return applied


# --------------------------------------------------------------------------- backup
def _backup_collection():
    """Take a real, timestamped WHOLE-COLLECTION backup (every deck, not just ours).

    Uses the same mechanism Anki runs on its own (a .colpkg in the profile's backup
    folder). Returns the backup folder path on success, None if it failed for any
    reason.
    """
    try:
        folder = mw.pm.backupFolder()
        if mw.col.create_backup(backup_folder=folder, force=True,
                                 wait_for_completion=True):
            return folder
    except Exception:
        pass
    return None


def _deck_backup_folder():
    folder = os.path.join(_USER_FILES, "deck_backups")
    os.makedirs(folder, exist_ok=True)
    return folder


def _export_deck_to(path, deck_name):
    """Write `deck_name` to `path` as a self-contained .apkg (history, deck options,
    and media all included). Returns the note count. Raises if the deck doesn't exist.
    """
    from anki.collection import DeckIdLimit, ExportAnkiPackageOptions

    deck_id = mw.col.decks.id_for_name(deck_name)
    if deck_id is None:
        raise RuntimeError(f"Couldn't find the {deck_name} deck in this collection.")
    opts = ExportAnkiPackageOptions(
        with_scheduling=True, with_deck_configs=True, with_media=True, legacy=False)
    return mw.col.export_anki_package(
        out_path=path, options=opts, limit=DeckIdLimit(deck_id=deck_id))


def _backup_deck(deck_name):
    """Write a timestamped deck backup, pruning old ones.

    This is the fast, targeted counterpart to _backup_collection(): a self-contained
    .apkg of just `deck_name` (with history), not the whole profile. Returns the
    backup's path on success, None if it failed (e.g. the deck doesn't exist in this
    collection yet, which is normal on someone's very first sync).
    """
    folder = _deck_backup_folder()
    stamp = datetime.datetime.now().strftime("%Y-%m-%d-%H%M%S")
    path = os.path.join(folder, f"Intern Pearls {stamp}.apkg")
    try:
        _export_deck_to(path, deck_name)
    except Exception:
        return None
    backups = sorted((f for f in os.listdir(folder) if f.endswith(".apkg")),
                     reverse=True)
    for old in backups[DECK_BACKUPS_KEEP:]:
        try:
            os.remove(os.path.join(folder, old))
        except OSError:
            pass
    return path


def _pre_sync_backup_or_confirm_skip(deck_name):
    """Back up before Sync/Import touch the collection, or ask to proceed if it failed.

    Defaults to the fast, deck-scoped backup rather than a whole-collection one, since
    that's what most syncs actually need protection against. A full collection backup
    is still one click away under Advanced whenever extra protection is wanted.

    Returns (proceed, backed_up): proceed=True with backed_up=False means either there
    was nothing to back up yet (a first sync) or the backup failed and the user chose
    to continue anyway — callers must not tell the user a backup was saved in that case.
    """
    if mw.col.decks.id_for_name(deck_name) is None:
        return True, False   # nothing to back up yet, e.g. someone's very first sync
    if _backup_deck(deck_name):
        return True, True
    return _ask("Couldn't create an automatic backup.\n\n"
                "Proceed anyway? (You can back up manually first: Advanced → Backup "
                "intern pearls deck, or Advanced → Backup full collection.)"), False


def _pre_sync_backup_or_skip_silently(deck_name):
    """Background counterpart to `_pre_sync_backup_or_confirm_skip`: never blocks with a
    dialog. If a backup is needed and fails, the safe default is to abort the auto-sync
    rather than import unprotected — there's no one watching to answer a prompt, so the
    background path must never proceed without the safety net the interactive path asks
    permission to skip.
    """
    if mw.col.decks.id_for_name(deck_name) is None:
        return True   # nothing to back up yet, e.g. this deck's very first sync
    return _backup_deck(deck_name) is not None


# ----------------------------------------------------------------- notes snapshot
def _snapshot(protected, scope_tag):
    search = f'"tag:{scope_tag}" OR "tag:{scope_tag}::*"' if scope_tag else ""
    snap = {}
    for nid in mw.col.find_notes(search):
        note = mw.col.get_note(nid)
        saved = {f: note[f] for f in protected if f in note and note[f].strip()}
        if saved:
            snap[note.guid] = saved
    return snap


def _restore(snap):
    restored = 0
    for guid, saved in snap.items():
        nid = mw.col.db.scalar("select id from notes where guid = ?", guid)
        if not nid:
            continue
        note = mw.col.get_note(nid)
        changed = False
        for f, v in saved.items():
            if f in note and note[f] != v:
                note[f] = v
                changed = True
        if changed:
            mw.col.update_note(note)
            restored += 1
    return restored


# -------------------------------------------------------------------- apkg helpers
def _import_apkg(path, with_scheduling=False):
    """with_scheduling=False for a spec-authored deck matched onto existing cards (the
    learner's own scheduling should win); True for reimporting our own previously
    exported/backed-up package, where the file's scheduling IS the thing being restored.
    """
    from anki.collection import ImportAnkiPackageRequest, ImportAnkiPackageOptions
    opts = ImportAnkiPackageOptions()
    # merge_notetypes=False on purpose. Merging note types on import rewrites the
    # collection's note types, which bumps Anki's *schema* modification time — and any
    # schema bump forces AnkiWeb into a one-way full sync ("upload from local") on the
    # learner's very next sync, instead of a normal incremental one. That's the friction
    # Jessica hit. We reconcile note types the idempotent way instead: _ensure_notetypes()
    # runs before every import and only touches the schema when it genuinely adds a
    # missing field (a real one-time event), so steady-state syncs leave the schema alone
    # and AnkiWeb stays incremental. Trade-off: template/CSS changes in a rebuilt deck no
    # longer propagate to existing note types automatically — run Advanced → Fix note
    # types (or accept one full sync) if a card template itself needs updating.
    for attr, val in (("with_scheduling", with_scheduling), ("merge_notetypes", False)):
        try:
            setattr(opts, attr, val)
        except Exception:
            pass
    return mw.col.import_anki_package(
        ImportAnkiPackageRequest(package_path=path, options=opts))


def _her_front_to_guid(scope_tag):
    search = f'"tag:{scope_tag}" OR "tag:{scope_tag}::*"' if scope_tag else ""
    out = {}
    for nid in mw.col.find_notes(search):
        note = mw.col.get_note(nid)
        out.setdefault(note.fields[0], note.guid)
    return out


def _her_guid_to_nid(scope_tag):
    """{note guid: note id} for every card under the scope tag. The reconcile flow needs
    to go from a retired card's GUID (what the ledger lists) back to the learner's note
    to archive it."""
    search = f'"tag:{scope_tag}" OR "tag:{scope_tag}::*"' if scope_tag else ""
    return {mw.col.get_note(nid).guid: nid for nid in mw.col.find_notes(search)}


def archive_notes(nids, retired_deck, tag):
    """Get retired cards out of the review rotation without deleting anything.

    Moves every card of each note to `retired_deck` (created if absent), suspends those
    cards, and tags the notes with `tag` so a later reconcile run recognizes them as
    already handled. Every step is a normal, incremental Anki operation — moving decks,
    suspending, and tagging do NOT bump the collection's schema modification time, so
    this never forces the one-way AnkiWeb full sync a note-type change would (the same
    reason imports use merge_notetypes=False). Fully reversible by hand: unsuspend a
    card, or move it back out of the Retired deck. Returns the number of notes archived.
    """
    nids = list(nids)
    if not nids:
        return 0
    did = mw.col.decks.id(retired_deck)   # creates the deck if it doesn't exist yet
    cids = [cid for nid in nids for cid in mw.col.get_note(nid).card_ids()]
    if cids:
        mw.col.set_deck(cids, did)
        mw.col.sched.suspend_cards(cids)
    mw.col.tags.bulk_add(nids, tag)
    return len(nids)


def _apply_deck(src, aliases, her):
    remap, in_place, as_new = remap_cards(src, her, aliases)
    out = src + ".sync.apkg"
    write_personalized(src, remap, out)
    try:
        _import_apkg(out)
    finally:
        try:
            os.remove(out)
        except OSError:
            pass
    return in_place, as_new


# --------------------------------------------------------------- Advanced actions
@_safe
def restore_from_backup():
    """Revert the whole collection to a pre-sync (or any other) backup.

    This is Anki's own backup restore, unscoped: it replaces every deck and note in the
    profile, not just the ones this add-on manages, since that's what a real collection
    backup contains. Anki asks for confirmation and reloads the profile itself.
    """
    if not _ask(
        "This opens Anki's own backup picker so you can revert your whole collection "
        "(every deck, not just Intern Pearls ones) to an earlier point. Anki will ask "
        "you to confirm the specific backup before doing anything. Continue?"
    ):
        return
    mw.onOpenBackup()


@_safe
def export_deck():
    """Export just the Intern Pearls deck as a shareable, self-contained .apkg.

    Unlike a backup (meant to undo a mistake and never opened otherwise), this prompts
    for where to save and is meant to be kept or handed to someone else: a standalone
    copy of just cfg["export_deck"], with scheduling, deck options, and media all
    included, the same as picking that deck in Anki's own File > Export > Anki Deck
    Package dialog with every checkbox on.
    """
    deck_name = _cfg()["export_deck"]
    fname = f"Intern Pearls {datetime.date.today().isoformat()}.apkg"
    path = getSaveFile(mw, "Export Intern Pearls deck", "internPearlsExport",
                       "Anki Deck Package", ".apkg", fname=fname)
    if not path:
        return
    try:
        note_count = _export_deck_to(path, deck_name)
    except Exception as e:
        _warn(f"Export failed: {e}")
        return
    _info(f"Exported <b>{note_count}</b> note(s) from {deck_name} to:"
          f"<br><code>{path}</code><br><br>"
          "Review history, deck options, and media are all included, this is a "
          "complete, standalone copy of just this deck.")


@_safe
def import_deck():
    """Bring an exported/backed-up Intern Pearls .apkg back into this collection.

    The file already carries this collection's own note GUIDs (it was made by Export
    Intern Pearls deck or an automatic pre-sync backup), so Anki's importer matches
    everything by GUID directly: no front-text personalization needed the way Sync and
    Import single deck need it for a spec-authored deck from someone else's collection.
    """
    src = getFile(mw, "Choose an Intern Pearls .apkg", cb=None,
                 filter="*.apkg", dir=_deck_backup_folder())
    if not src:
        return
    if isinstance(src, (list, tuple)):
        src = src[0]
    if not _ask(f"Import {os.path.basename(src)}? Matching cards are updated in "
                "place, keeping their scheduling; anything not already here is added "
                "as new. A backup is taken automatically first."):
        return
    if not _pre_sync_backup_or_confirm_skip(_cfg()["export_deck"])[0]:
        return
    try:
        _import_apkg(src, with_scheduling=True)
    except Exception as e:
        _warn(f"Import failed: {e}")
        return
    mw.reset()
    _info(f"Imported <code>{os.path.basename(src)}</code>.")


@_safe
def backup_deck_now():
    """Manual, on-demand version of the deck-scoped backup Sync/Import take for you
    automatically. Useful right before poking at cards yourself outside the add-on.
    """
    deck_name = _cfg()["export_deck"]
    path = _backup_deck(deck_name)
    if not path:
        _warn(f"Couldn't back up the <b>{deck_name}</b> deck. It may not exist in "
              "this collection yet.")
        return
    _info(f"Backed up the Intern Pearls deck to:<br><code>{path}</code>")


@_safe
def backup_collection_now():
    """Manual, on-demand whole-collection backup, the same kind Sync used to take
    automatically before every sync. Kept available for anyone who wants that broader
    protection on top of the faster, deck-scoped default.
    """
    folder = _backup_collection()
    if not folder:
        _warn("Couldn't create a collection backup.")
        return
    _info(f"Backed up your whole collection (every deck) to:<br><code>{folder}</code>")


@_safe
def update_notetypes():
    added = _ensure_notetypes()
    _info(("<b>Updated note types</b> (cards and scheduling untouched):" +
           bullets(added)) if added else
          "Note types are already up to date, no changes needed.")
