"""Everything that reads or writes the Anki collection directly.

Note-type reconciliation, backups (deck-scoped and whole-collection), the protected-
fields snapshot/restore round trip, .apkg import/export, and the Advanced menu actions
that are thin user-facing wrappers over those helpers. The sync flows in sync.py
compose these; nothing here fetches from the network.
"""
import datetime
import hashlib
import os
import re
import tempfile

from aqt import mw
from aqt.utils import getFile, getSaveFile

from . import ai_logic
from .config import (DECK_BACKUPS_KEEP, INSTALLED, TARGET_FIELDS, _USER_FILES, _cfg,
                     _load_json, _save_json)
from .logic import (apkg_deck_names, apkg_models, apkg_note_types, apkg_notes,
                    changed_templates, declined_drop, empty_cards_dialog_rows,
                    fields_to_carry_over, manifest_decks_for, model_shape,
                    note_display_label, plan_notetype_changes, plural, remap_cards,
                    select_empty_cards, write_personalized)
from .review import _CONFIRM_HEIGHT, append_rows, build_list_body, show_result
from .ui import _ask, _ask_with_widget, _info, _manual_flow, _safe, _warn


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
    # legacy=True on purpose. A modern package holds its collection in a zstd-compressed
    # member, and zstandard is not stdlib and Anki does not ship it, so every reader in
    # this add-on (logic._apkg_db, and everything built on it) refuses one. A backup
    # written in that format is therefore unreadable to the very code meant to use it:
    # import_deck's scoped invalidation always falls back to clearing everything, and
    # Import single deck can't take one at all. The legacy format costs some file size
    # and reads everywhere.
    # Set field by field, guarded, the way _import_apkg sets its own: these are
    # protobuf message fields, and a build that has dropped or renamed one raises on a
    # constructor keyword, taking the whole backup with it rather than the one option.
    opts = ExportAnkiPackageOptions()
    for attr, val in (("with_scheduling", True), ("with_deck_configs", True),
                      ("with_media", True), ("legacy", True)):
        try:
            setattr(opts, attr, val)
        except Exception:
            pass
    return mw.col.export_anki_package(
        out_path=path, options=opts, limit=DeckIdLimit(deck_id=deck_id))


# Every automatic deck backup's filename starts with this, so a prune can tell its own
# files from anything else sitting in the folder (an export someone dropped there).
_BACKUP_PREFIX = "Intern Pearls "


def _backup_label(label):
    """A deck name reduced to something a filename can hold, or "" for no label.

    Deck names nest with "::" and can carry anything else the learner types, including
    a path separator, so the raw name is not a filename. Collapsed to letters, digits,
    spaces, dots, dashes and underscores, then given a short hash of the RAW name.

    The hash is what actually keeps two roots apart, exactly as it does in
    sync._scratch_path: sanitizing alone maps distinct names onto one label ("A/B" and
    "A:B" both become "A_B", and two names of nothing but non-ASCII both become the
    same run of underscores). Two roots sharing a label share a filename, so the
    same-second backups a multi-root run takes overwrite each other while the run
    reports both covered, and the two roots then prune as a single bucket.
    """
    if not label:
        return ""
    clean = re.sub(r"[^A-Za-z0-9 ._-]", "_", label).strip()
    return f"{clean} {hashlib.sha1(label.encode('utf8')).hexdigest()[:8]}".strip()


def _label_of_backup(fname):
    """The label a backup filename carries, "" for an unlabelled one.

    The name is "<prefix><stamp>[ <label>].apkg" and the stamp has no spaces, so the
    label is whatever follows the first one.
    """
    base = fname[len(_BACKUP_PREFIX):-len(".apkg")]
    return base.split(" ", 1)[1] if " " in base else ""


def _in_backup_group(found, label):
    """Whether a backup already in the folder prunes with `label`'s.

    An exact match is the normal answer. A file written before labels carried a hash
    has only the sanitized deck name, so it matches no current label at all and would
    sit in the folder forever; it prunes with the label whose own sanitized name it is,
    which is the bucket that would have written it. Two labels that sanitize alike both
    claim such a file, which is the same ambiguity those older filenames already had,
    and it bounds them either way.

    Compared against the label with its hash removed rather than by prefix: a current
    label is "<sanitized name> <hash>", so a plain startswith let the roots "Foo" and
    "Foo Bar" share a bucket ("Foo Bar <hash>" starts with "Foo "). The older file then
    counted in Foo Bar's prune too and, being the oldest there, was evicted early.
    """
    return found == label or (bool(found) and label.rsplit(" ", 1)[0] == found)


def _backup_deck(deck_name, label=None):
    """Write a timestamped deck backup, pruning old ones.

    This is the fast, targeted counterpart to _backup_collection(): a self-contained
    .apkg of just `deck_name` (with history), not the whole profile. Returns the
    backup's path on success, None if it failed (e.g. the deck doesn't exist in this
    collection yet, which is normal on someone's very first sync).

    `label` distinguishes two backups taken in the same second, which only happens when
    one run touches decks under more than one root and each root needs its own file
    (see _pre_sync_backup_or_confirm_skip). It is sanitized and hashed for the
    filesystem first (see _backup_label), since a deck name is free text and routinely
    holds a "/" or a ":". Left off, the filename is exactly what it has always been, so
    the ordinary single-deck backup is unchanged.

    Pruning keeps DECK_BACKUPS_KEEP per label rather than per folder. Ten unlabelled
    backups plus one run over three roots is fourteen files, and a folder-wide prune
    would evict either the other roots' history or, on a big enough run, files this very
    call just wrote, so the newest backup of a deck could be missing the moment it was
    needed.
    """
    folder = _deck_backup_folder()
    stamp = datetime.datetime.now().strftime("%Y-%m-%d-%H%M%S")
    label = _backup_label(label)
    suffix = f" {label}" if label else ""
    path = os.path.join(folder, f"{_BACKUP_PREFIX}{stamp}{suffix}.apkg")
    try:
        _export_deck_to(path, deck_name)
    except Exception:
        return None
    backups = sorted((f for f in os.listdir(folder)
                      if f.startswith(_BACKUP_PREFIX) and f.endswith(".apkg")
                      and _in_backup_group(_label_of_backup(f), label)),
                     reverse=True)
    for old in backups[DECK_BACKUPS_KEEP:]:
        try:
            os.remove(os.path.join(folder, old))
        except OSError:
            pass
    return path


def _backup_targets(deck_name, decks):
    """Which deck(s) a pre-sync backup has to cover for a run touching `decks`.

    `deck_name` (the configured export_deck) covers the ordinary case and stays the
    answer whenever every deck this run touches sits under it, which keeps the backup as
    small and fast as it has always been. A run reaching outside it (a source that files
    decks somewhere else, a relocation moving a card out from under it) gets each
    touched deck's top-level deck instead, since exporting that covers every subdeck
    below it. `decks` of None means the caller doesn't know or doesn't need to narrow it,
    and gets `deck_name` alone.
    """
    decks = [d for d in (decks or []) if d]
    if not decks or all(d == deck_name or d.startswith(deck_name + "::") for d in decks):
        return [deck_name]
    roots = []
    for d in decks:
        root = d.split("::")[0]
        if root not in roots:
            roots.append(root)
    return roots


def _pre_sync_backup_or_confirm_skip(deck_name, decks=None, scope_tag=None):
    """Back up before Sync/Import touch the collection, or ask to proceed if it can't.

    Defaults to the fast, deck-scoped backup rather than a whole-collection one, since
    that's what most syncs actually need protection against. A full collection backup
    is still one click away under Advanced whenever extra protection is wanted.

    `decks` is the deck names this run will actually change, which is what the backup is
    scoped from (see _backup_targets). It used to always be export_deck's subtree alone,
    so a run touching anything outside that got a backup covering none of it while every
    confirmation promised one.

    Returns (proceed, backed_up): proceed=True with backed_up=False means either there
    was nothing at all to back up (a first sync, where the collection holds none of this
    add-on's cards yet) or the user chose to continue without one, so callers must not
    tell the user a backup was saved in that case. `scope_tag` is what separates those
    two: a collection that already holds cards under it has something to lose, so a run
    that can't back any of it up asks rather than proceeding silently against a
    confirmation that promised a backup.
    """
    targets = [d for d in _backup_targets(deck_name, decks)
               if mw.col.decks.id_for_name(d) is not None]
    if targets:
        many = len(targets) > 1
        saved = {d: _backup_deck(d, d.split("::")[-1] if many else None)
                 for d in targets}
        failed = [d for d, path in saved.items() if not path]
        if not failed:
            return True, True
        # Named, both halves. A run over several roots that backed up two of three has
        # something to restore from and something it doesn't, and answering that with a
        # flat "couldn't create an automatic backup" (then, at the end, "no pre-sync
        # backup was taken") is wrong in both directions: it hides the cover that does
        # exist and hides which deck is the one without it.
        covered = [d for d, path in saved.items() if path]
        done = (f"Backed up: {', '.join(covered)}.<br><br>" if covered else "")
        proceed = _ask(
            f"Couldn't back up: {', '.join(failed)}.<br><br>{done}"
            "Proceed anyway? (You can back up manually first: Advanced → Backup "
            "intern pearls deck, or Advanced → Backup full collection.)",
            yes_label="Continue without a backup", no_label="Cancel")
        return proceed, bool(covered)

    if scope_tag and mw.col.find_notes(f'"tag:{scope_tag}" OR "tag:{scope_tag}::*"'):
        return _ask(
            "Couldn't find a deck to back up: your cards aren't under a deck this "
            "add-on knows how to export on its own.<br><br>Proceed without a backup? "
            "(You can back up manually first: Advanced → Backup full collection.)",
            yes_label="Continue without a backup", no_label="Cancel"), False
    return True, False   # nothing in the collection to back up yet, e.g. a first sync


def _pre_sync_backup_or_skip_silently(deck_name, decks=None):
    """Background counterpart to `_pre_sync_backup_or_confirm_skip`: never blocks with a
    dialog. If a backup is needed and fails, the safe default is to abort the auto-sync
    rather than import unprotected — there's no one watching to answer a prompt, so the
    background path must never proceed without the safety net the interactive path asks
    permission to skip.

    `decks` is scoped exactly as the interactive path scopes it (see _backup_targets):
    an unattended sync imports whatever the manifest lists, so backing up only
    export_deck left a deck filed outside it changed with no backup covering it at all.
    Any target that can't be backed up aborts the whole tick rather than importing the
    rest, which is the same fail-closed answer this has always given for the one deck it
    used to cover: the next poll retries, and a manual Sync decks can ask.
    """
    targets = [d for d in _backup_targets(deck_name, decks)
               if mw.col.decks.id_for_name(d) is not None]
    if not targets:
        return True   # nothing to back up yet, e.g. this deck's very first sync
    many = len(targets) > 1
    saved = [_backup_deck(d, d.split("::")[-1] if many else None) for d in targets]
    return all(saved)


# ----------------------------------------------------------------- notes snapshot
def _note_field(note, name):
    """The note's actual field name matching `name`, or None.

    Case-insensitive on purpose. The preserved-fields box is free text, the real names
    are capitalised (Front, Back, Why, Image, Tag, Dosing, Notes), and an exact-match
    lookup meant typing "dosing" silently protected nothing at all: no error, no
    warning, just annotations quietly overwritten on the next sync.
    """
    if name in note:
        return name
    lowered = name.lower()
    return next((f for f in note.keys() if f.lower() == lowered), None)


def _snapshot(protected, scope_tag):
    search = f'"tag:{scope_tag}" OR "tag:{scope_tag}::*"' if scope_tag else ""
    snap = {}
    for nid in mw.col.find_notes(search):
        note = mw.col.get_note(nid)
        saved = {}
        for name in protected:
            f = _note_field(note, name)
            if f and note[f].strip():
                saved[f] = note[f]
        if saved:
            snap[note.guid] = saved
    return snap


def _capture_shipped(protected, scope_tag, touched):
    """What the deck source's own values are, read straight after an import and before
    anything is restored, which is the one moment the collection holds them.

    Limited to `touched` (the guids an import actually wrote this run) because a note in
    a deck that didn't update still holds the learner's restored value, and recording
    that as "what we shipped" would make her own edit look like ours next time, so the
    following sync would overwrite it. That is the exact failure this baseline exists to
    prevent, so it must not create it.
    """
    if not touched:
        return {}
    search = f'"tag:{scope_tag}" OR "tag:{scope_tag}::*"' if scope_tag else ""
    out = {}
    for nid in mw.col.find_notes(search):
        note = mw.col.get_note(nid)
        if note.guid not in touched:
            continue
        vals = {}
        for name in protected:
            f = _note_field(note, name)
            if f:
                vals[f] = note[f]
        if vals:
            out[note.guid] = vals
    return out


def _restore(snap, baseline=None, touched=None):
    """Put the learner's own annotations back after an import, but only the ones that
    are actually hers.

    Anki's importer overwrites every field on a matched note, so without this her
    annotations are wiped on every sync. Restoring unconditionally is the blunt version
    of that fix, and it has a cost: a preserved field can never receive a correction
    from the deck source again, which is why preserving anything beyond an
    always-empty-by-design Notes field used to mean freezing it forever.

    `baseline` ({guid: {field: value}}, what the source last shipped for that note)
    makes it a three-way merge instead. If her pre-import value still equals what was
    last shipped, she never touched it, so the freshly imported value is allowed to
    stand. If it differs, the field is hers and gets restored. Comparing against the
    INCOMING value can't distinguish those two cases: "she edited it" and "the deck
    author changed it" both read as a difference, which is why the baseline is kept.

    With no baseline for a note (the first sync after this shipped, or a note the
    source has never written) the old always-restore behaviour applies, so the
    conservative direction is the default and an upgrade never loses an annotation.

    `touched` is the guids this run's import actually wrote. Notes outside it are
    skipped entirely: an import that never ran over a note cannot have overwritten
    anything on it, so there is nothing to restore and nothing to conflict with.

    Returns (restored, collisions), where a collision is a field she edited AND the
    source changed since that baseline: hers wins, and the count lets the caller offer
    to send those back rather than let the two versions drift apart unnoticed.
    """
    baseline = baseline or {}
    restored, collisions = 0, []
    for guid, saved in snap.items():
        if touched is not None and guid not in touched:
            # Nothing imported over this note, so none of its fields were overwritten:
            # there is nothing to put back, and comparing it to the baseline would
            # report a conflict against an update that never happened. This is what
            # made a card in a deck that had no update at all read as a collision.
            continue
        nid = mw.col.db.scalar("select id from notes where guid = ?", guid)
        if not nid:
            continue
        note = mw.col.get_note(nid)
        was_shipped = baseline.get(guid, {})
        changed = False
        for f, v in saved.items():
            if f not in note:
                continue
            if f in was_shipped and was_shipped[f] == v:
                continue          # untouched by her; let the source's update stand
            if f in was_shipped and note[f] != was_shipped[f] and note[f] != v:
                # Only when the two versions actually differ. An update that changed a
                # field to exactly what she had already written is agreement, not a
                # conflict, and reporting it asked her to go and reconcile two identical
                # wordings.
                collisions.append((guid, f))
            if note[f] != v:
                note[f] = v
                changed = True
        if changed:
            mw.col.update_note(note)
            restored += 1
    return restored, collisions


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
    # the learner hit. We reconcile note types the idempotent way instead: _ensure_notetypes()
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


def _her_guid_to_fields(scope_tag):
    """{note guid: {field name: value}} for every note under the scope tag.

    Change detection needs her side of the comparison by name, not by position, so this
    zips each note's values against its own note type's field names rather than assuming
    every note type agrees on an order.
    """
    search = f'"tag:{scope_tag}" OR "tag:{scope_tag}::*"' if scope_tag else ""
    out = {}
    for nid in mw.col.find_notes(search):
        note = mw.col.get_note(nid)
        names = [f["name"] for f in note.note_type()["flds"]]
        out[note.guid] = dict(zip(names, note.fields))
    return out


def _her_guid_to_nid(scope_tag):
    """{note guid: note id} for every card under the scope tag. The reconcile flow needs
    to go from a retired card's GUID (what the ledger lists) back to the learner's note
    to archive it."""
    search = f'"tag:{scope_tag}" OR "tag:{scope_tag}::*"' if scope_tag else ""
    return {mw.col.get_note(nid).guid: nid for nid in mw.col.find_notes(search)}


def _her_guid_to_deck(scope_tag):
    """{note guid: current deck name} for every note under the scope tag, keyed off
    its first card's deck (all cards of a note normally share one deck). The deck-move
    step needs this to tell "still where the source last filed it" (safe to relocate)
    from "she's moved or customized it herself" (leave it alone)."""
    search = f'"tag:{scope_tag}" OR "tag:{scope_tag}::*"' if scope_tag else ""
    out = {}
    for nid in mw.col.find_notes(search):
        note = mw.col.get_note(nid)
        cids = note.card_ids()
        if cids:
            out[note.guid] = mw.col.decks.name(mw.col.get_card(cids[0]).did)
    return out


def _her_notes_summary(scope_tag, exclude_tag=None):
    """{guid, nid, model, front, label, reps, deck} for every note under the scope tag,
    the raw material find_duplicate_groups groups into duplicate candidates. `front` is
    the raw first field (the grouping key); `label` is a readable version for dialogs.

    `exclude_tag`, if given, is added as a search exclusion so a note a previous
    duplicate-cleanup run already archived (see sync.clean_up_duplicates) is never
    considered again, making a repeat run idempotent on what it already handled.
    """
    search = f'"tag:{scope_tag}" OR "tag:{scope_tag}::*"' if scope_tag else ""
    if exclude_tag:
        search = f'({search}) -"tag:{exclude_tag}"'
    out = []
    for nid in mw.col.find_notes(search):
        note = mw.col.get_note(nid)
        cids = note.card_ids()
        if not cids:
            continue
        reps = sum(mw.col.get_card(cid).reps for cid in cids)
        deck = mw.col.decks.name(mw.col.get_card(cids[0]).did)
        out.append({
            "guid": note.guid,
            "nid": nid,
            "model": note.note_type()["name"],
            "front": note.fields[0],
            "label": note_display_label(note.fields),
            "reps": reps,
            "deck": deck,
        })
    return out


def installed_matching_collection(installed, scope_tag):
    """Reconcile installed.json against what's actually in the collection.

    installed.json lives in user_files/, entirely outside the collection file, so it
    survives things that don't touch it — most notably restoring an Anki collection
    backup, which rolls mw.col back to an earlier snapshot without touching this
    add-on's own bookkeeping. After that, installed.json can still claim a deck is
    synced when the collection no longer has any of its cards, so Sync decks, Check
    what will sync, and the Manage decks deck rows would all wrongly read "up to
    date" for it — nothing looks pending because nothing was ever compared against the
    collection itself. A first version of this check only detected a *total* wipe
    (every synced note gone at once) and missed the common case: a revert that only
    rolls back part of the collection, leaving some Intern Pearls decks intact and
    others gone. This checks per deck instead: a deck stays "installed" only if the
    collection currently has at least one note under scope_tag actually sitting in an
    Anki deck of that exact name; otherwise it's dropped, so a normal sync re-detects
    and re-applies it.

    "An Anki deck of that exact name" means the manifest's deck name itself OR any
    subdeck under it — a deck spec's `deck_name` is routinely just the parent path,
    with cards actually filed into `deck_name::<subdeck>` (every spec with a
    `subdecks` list works this way; the example deck does too). A first version of
    this required an exact match, which meant it never recognized ANY subdeck-based
    deck as present and treated every one of them as pending on every check, forever
    — a real, silent regression, not the intended behavior.

    Trade-off worth knowing: this can also false-positive (call a deck "missing" when
    it isn't) in two narrow, self-correcting cases — a deck mid-reorg where Sync
    decks has updated content but "Reconcile my decks" hasn't yet relocated the
    learner's existing cards to the new deck name (apply_deck_moves does that, not
    Sync), and a card the learner has manually filed into a deck of her own outside
    the spec's hierarchy entirely. Both just cause a harmless redundant re-sync of
    that one deck (0 new, N kept in place) rather than any data loss, which is the
    failure mode actually worth avoiding here — a false "needs sync" self-corrects;
    a false "up to date" hides a real problem silently.
    """
    present = set(_her_guid_to_deck(scope_tag).values())

    def _has_cards(name):
        return any(d == name or d.startswith(name + "::") for d in present)

    return {name: version for name, version in installed.items() if _has_cards(name)}


def decks_holding(guids, her_guid_to_nid):
    """The decks the given notes' cards are actually sitting in right now.

    What a pre-run backup has to cover is where a card IS, not where a ledger says it
    was filed: a learner who reorganizes her own collection moves a card out from under
    the deck the retirement ledger recorded, and archiving or rewriting it there would
    then happen in a deck no backup covered. Unknown guids are simply absent from the
    result, so the caller can fall back to whatever it does know.
    """
    out = []
    for guid in guids:
        nid = her_guid_to_nid.get(guid)
        if nid is None:
            continue
        for cid in mw.col.get_note(nid).card_ids():
            name = mw.col.decks.name(mw.col.get_card(cid).did)
            if name and name not in out:
                out.append(name)
    return out


def apply_deck_moves(moves, her_guid_to_nid):
    """Relocate a learner's cards to match a pure deck reorg (Local Anesthetics
    moving into a new Regional deck, say) without touching content or scheduling.

    `moves` is the already-filtered list from find_deck_moves_needed — every entry
    is a card confirmed to still be sitting exactly where the source last put it.
    Just like archive_notes, this is schema-neutral (set_deck only), so it never
    forces an AnkiWeb full sync, and it's trivially reversible by hand (drag the
    card back). Returns the number of notes moved.
    """
    n = 0
    for m in moves:
        nid = her_guid_to_nid.get(m["guid"])
        if nid is None:
            continue
        cids = mw.col.get_note(nid).card_ids()
        if cids:
            mw.col.set_deck(cids, mw.col.decks.id(m["to"]))
            n += 1
    return n


def carry_over_protected_fields(retired, her_guid_to_nid, protected_fields):
    """Before a retired note is archived, copy her protected-field text (e.g. Notes)
    onto its replacement(s) so a personal annotation isn't stranded on a card that's
    about to be suspended out of review.

    `retired` is the fresh (not-yet-archived) entries from find_retired_in_collection
    — each has `guid` and `superseded_by`. Only fills a replacement's field if it's
    currently blank (fields_to_carry_over), so this never overwrites something she's
    already written on the new card, and copies to every replacement she already has
    (a symmetric split has no single "primary" to prefer). Returns the number of
    replacement notes updated.
    """
    n = 0
    for r in retired:
        old_nid = her_guid_to_nid.get(r["guid"])
        if old_nid is None:
            continue
        old_note = mw.col.get_note(old_nid)
        saved = {f: old_note[f] for f in protected_fields
                 if f in old_note and old_note[f].strip()}
        if not saved:
            continue
        for target_guid in r["superseded_by"]:
            target_nid = her_guid_to_nid.get(target_guid)
            if target_nid is None:
                continue
            target_note = mw.col.get_note(target_nid)
            current = {f: target_note[f] for f in protected_fields if f in target_note}
            to_write = fields_to_carry_over(saved, current)
            if not to_write:
                continue
            for f, v in to_write.items():
                target_note[f] = v
            mw.col.update_note(target_note)
            n += 1
    return n


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


# The scheduling a card carries: what SM-2/FSRS reads to decide when it comes back, plus
# the counters the learner sees. Deliberately not the deck (`did`) or the note's fields,
# so carrying this forward moves her progress without moving her organization.
_SCHED_FIELDS = ("type", "queue", "due", "ivl", "factor", "reps", "lapses")

# FSRS schedules from the card's memory state, not from ivl/factor, so a card seeded
# with an interval but no memory state is inconsistent under it: the number says one
# thing and the scheduler computes another. These travel with the interval.
_FSRS_FIELDS = ("desired_retention", "decay", "last_review_time")


def _halve_memory_state(parent, card):
    """Copy the parent's FSRS memory state onto a seeded sibling, at half stability.

    Stability is roughly the interval at which the learner still recalls the card at
    their desired retention, so it is the FSRS-side counterpart of the interval and has
    to be halved with it. Copying the interval alone would leave FSRS recomputing from
    a memory state the card never had. Difficulty carries over unchanged: how hard the
    material is does not depend on which blank is asking about it.

    A no-op on a collection not using FSRS, where memory_state is simply absent.
    """
    state = getattr(parent, "memory_state", None)
    if state is None:
        return
    try:
        copy = type(state)()
        copy.CopyFrom(state)
        copy.stability = max(0.5, state.stability / 2)
        card.memory_state = copy
    except Exception:
        # Never let a scheduler detail break the conversion itself; the card still
        # carries the seeded interval, which is what the older scheduler reads.
        pass


def carry_scheduling_forward(pairs, her_guid_to_nid):
    """Move a stranded predecessor's review history onto its live successor.

    `pairs` is find_stranded_pairs' output. For each, the predecessor's cards are copied
    onto the successor's by ordinal, so a cloze whose deletions were renumbered only
    moves the ordinals that still line up rather than smearing card 1's schedule across
    all of them.

    Two guards keep this from ever costing her progress. A successor card is only
    written when it has FEWER reps than the predecessor, so a card she has already been
    studying is never rolled back to an older schedule, and a re-run is a no-op. And
    nothing is cleared from the predecessor: it keeps its own scheduling and is archived
    afterwards, so the worst case is a duplicated schedule on a suspended card, which is
    recoverable by hand, rather than history that no card holds any more.

    Like archiving, every write here is an ordinary card update: no note types, no
    fields, no schema bump, so it does not force the one-way AnkiWeb full sync. Returns
    the number of successor cards updated.
    """
    moved = 0
    for p in pairs:
        old_nid = her_guid_to_nid.get(p["guid"])
        new_nid = her_guid_to_nid.get(p["successor_guid"])
        if old_nid is None or new_nid is None:
            continue
        src = {}
        for cid in mw.col.get_note(old_nid).card_ids():
            card = mw.col.get_card(cid)
            src[getattr(card, "ord", 0)] = card
        for cid in mw.col.get_note(new_nid).card_ids():
            dst = mw.col.get_card(cid)
            card = src.get(getattr(dst, "ord", 0))
            if card is None or getattr(dst, "reps", 0) >= getattr(card, "reps", 0):
                continue
            for f in _SCHED_FIELDS:
                if hasattr(card, f):
                    setattr(dst, f, getattr(card, f))
            mw.col.update_card(dst)
            moved += 1
    return moved


def _her_note_types(scope_tag):
    """{note guid: notetype name} for every note under the scope tag."""
    search = f'"tag:{scope_tag}" OR "tag:{scope_tag}::*"' if scope_tag else ""
    out = {}
    for nid in mw.col.find_notes(search):
        note = mw.col.get_note(nid)
        out[note.guid] = note.note_type()["name"]
    return out


def _field_map(old_model, new_model):
    """new_fields for change_notetype_of_notes: one entry per field of the NEW note
    type, holding that field's index in the OLD one, or -1 to discard.

    Matched by NAME, not position. Anki's own default is positional, which for
    Basic to Cloze would drop Front into Text by luck and then slide Back into Why,
    Why into Image and so on, quietly shifting every remaining field by one. Front maps
    to Text explicitly because they are the same thing under two names; everything else
    (Why, Image, Dosing, Notes) lines up by name, and Back and Tag have nowhere to go.
    """
    old = [f["name"] for f in old_model["flds"]]
    idx = {name: i for i, name in enumerate(old)}
    if "Front" in idx:
        idx.setdefault("Text", idx["Front"])
    if "Text" in idx:
        idx.setdefault("Front", idx["Text"])
    return [idx.get(f["name"], -1) for f in new_model["flds"]]


def seed_converted_siblings(nids):
    """Give the extra cards a conversion generates the parent card's standing.

    Turning one question-and-answer card into a cloze with four blanks produces four
    cards. Anki carries the original's scheduling onto the first and creates the rest
    as brand new, which is right when the blanks are new material and wrong here: the
    learner has been retrieving these same facts off the parent card for months, and
    across a whole deck it would drop a four-figure new-card queue on her, which is the
    workload problem this reformatting is meant to reduce.

    So a sibling inherits the parent's ease, counters and standing, at HALF its
    interval (floored at a day). Half rather than whole because producing one blank
    cold is a harder retrieval than recognising the paragraph the parent tested, so its
    interval is evidence about the fact set rather than about that blank. Halving keeps
    the card out of the new queue while still bringing it back soon enough to prove
    itself, and one failed review resets it properly either way.

    Only cards with no reviews of their own are touched, so this never overwrites real
    history and re-running is a no-op. Returns the number of cards seeded.
    """
    seeded = 0
    for nid in nids:
        cards = [mw.col.get_card(cid) for cid in mw.col.get_note(nid).card_ids()]
        parent = max(cards, key=lambda c: getattr(c, "reps", 0))
        if getattr(parent, "reps", 0) == 0:
            continue                      # nothing to inherit; leave them all new
        for card in cards:
            if card.id == parent.id or getattr(card, "reps", 0) > 0:
                continue
            for f in _SCHED_FIELDS + _FSRS_FIELDS:
                if hasattr(parent, f):
                    setattr(card, f, getattr(parent, f))
            card.ivl = max(1, getattr(parent, "ivl", 0) // 2)
            card.due = getattr(parent, "due", 0)
            _halve_memory_state(parent, card)
            mw.col.update_card(card)
            seeded += 1
    return seeded


def change_note_types(changes):
    """Move the learner's notes onto the note type this update ships for them.

    Runs BEFORE the import: once a note is on the right type, Anki's importer matches it
    by GUID and updates it in place, so the conversion keeps the card and its whole
    review history instead of adding a second one beside it.

    This is the one operation here that DOES bump the collection's schema (Anki's own
    Change Notetype dialog gates on confirm_schema_modification for the same reason), so
    the caller must have consented to the one-time full AnkiWeb sync first, exactly as
    it does for a template change.

    Returns the note ids converted, for the caller to hand to seed_converted_siblings
    AFTER the import: at this point the note still holds its old question-and-answer
    text, so the extra cloze cards do not exist yet and there is nothing to seed. They
    appear only once the import writes the cloze markup in.
    """
    if not changes:
        return []
    from anki.models import ChangeNotetypeRequest

    by_pair = {}
    for c in changes:
        by_pair.setdefault((c["old"], c["new"]), []).append(c["guid"])
    done = []
    for (old_name, new_name), guids in by_pair.items():
        old_model = mw.col.models.by_name(old_name)
        new_model = mw.col.models.by_name(new_name)
        if not old_model or not new_model:
            # A target this collection doesn't hold yet is the caller's to notice
            # before consenting (see missing_notetype_targets); this stays as the
            # backstop, so nothing here can convert onto a note type that isn't there.
            continue
        nids = [nid for nid in (mw.col.db.scalar(
            "select id from notes where guid = ?", g) for g in guids) if nid]
        if not nids:
            continue
        # info.input already carries old/new notetype ids, the current schema and the
        # cloze flag; only the note ids and the field map are ours to set. Do not add
        # fields to this message: it is a protobuf, and assigning one it does not
        # define raises at runtime rather than being ignored.
        info = mw.col.models.change_notetype_info(
            old_notetype_id=old_model["id"], new_notetype_id=new_model["id"])
        req = ChangeNotetypeRequest()
        req.CopyFrom(info.input)
        req.note_ids.extend(nids)
        del req.new_fields[:]
        req.new_fields.extend(_field_map(old_model, new_model))
        mw.col.models.change_notetype_of_notes(req)
        done.extend(nids)
    if done:
        mw.reset()
    return done


def missing_notetype_targets(changes):
    """The note types `changes` would convert onto that this collection doesn't have.

    change_note_types can only move a note onto a note type that already exists, and
    the import that would create one runs after it. Left unchecked, those pairs were a
    bare `continue`: nothing converted, nothing counted, nothing said, and the deck
    recorded as installed at that version, so the conversion was never offered again.
    A caller that finds anything here holds that deck's version back instead and says
    so; the import it still runs is what adds the missing note type, so the next run
    converts and matches.
    """
    return sorted({c["new"] for c in changes if not mw.col.models.by_name(c["new"])})


def notetype_changes(src, her, aliases, scope_tag, declined):
    """Note-type changes this .apkg would need on the learner's own notes.

    Resolves the .apkg's guids through the same matching ladder the import uses, so a
    note matched by front counts, then compares types. Returns plan_notetype_changes'
    list; empty when nothing needs converting, which is the normal case.

    `declined` is logic.declined_guids' set, and a match is left out of the plan
    entirely. A conversion is only worth anything because the import right after it
    writes the new format's content onto the converted note, and a declined note is
    dropped from that import (logic.declined_drop), so converting one moved her note to
    a fill-in-the-blank type holding no blanks. Both identities are tested, the
    package's own guid and the guid it would be remapped to, exactly the pair
    declined_drop tests, so a decline can't be bypassed through either. It is a required
    argument rather than a defaulted one so a fourth caller has to decide about it
    instead of quietly planning a conversion for a card she turned away.
    """
    remap, _in_place, _as_new, _new, _matched = remap_cards(src, her, aliases)
    incoming = apkg_note_types(src)
    by_her = {}
    for rid, _fields, guid in apkg_notes(src):
        her_guid = remap.get(rid, guid)
        if her_guid in declined or guid in declined:
            continue
        if guid in incoming:
            by_her[her_guid] = incoming[guid]
    return plan_notetype_changes(by_her, _her_note_types(scope_tag), TARGET_FIELDS)


def _apply_deck(src, aliases, her, declined=frozenset()):
    """Import one deck, returning (in_place, as_new, touched) where `touched` is the
    guids this import wrote in her collection: the remapped guid where a note matched
    one of hers, the .apkg's own otherwise. _capture_shipped needs exactly that set.

    `declined` is the decline registry's guids; matching notes are dropped from the
    scratch package before Anki sees it, so a skipped or never-imported card never
    lands and a kept-back card's collection copy is never overwritten. Dropped notes
    are excluded from the counts and from `touched`."""
    remap, in_place, as_new, _, _matched = remap_cards(src, her, aliases)
    drop, touched, in_place, as_new = declined_drop(src, remap, her, declined,
                                                     in_place, as_new)
    # A unique name in the system temp directory, rather than a fixed one derived from
    # `src` (two runs can otherwise write and import through the same path, and on a
    # shared machine that path is predictable enough to be pre-created as a symlink) or
    # one beside `src` itself. For a local-folder source `src` is the learner's own
    # configured folder, which may be a read-only share, and is hers rather than ours to
    # leave a file in if the import raises before the cleanup below.
    fd, out = tempfile.mkstemp(suffix=".sync.apkg")
    os.close(fd)
    write_personalized(src, remap, out, drop=drop)
    try:
        _import_apkg(out)
    finally:
        try:
            os.remove(out)
        except OSError:
            pass
    return in_place, as_new, touched


def invalidate_installed(names=None):
    """Drop the add-on's record of which deck versions are applied.

    installed.json is the only record of what was last applied, and it lives in
    user_files/, outside the collection, so a restore rolls the cards back while
    leaving it claiming the newest version. Its usual safety net only checks whether
    a deck still has cards, which a rollback to older content passes, so the restore
    itself is the only reliable signal. `names` limits the drop to those decks; None
    clears everything. Re-imports match by GUID, so the cost of re-offering a deck
    that did not actually change is bandwidth, never history.
    """
    if names is None:
        _save_json(INSTALLED, {})
        return
    installed = _load_json(INSTALLED, {})
    for name in names:
        installed.pop(name, None)
    _save_json(INSTALLED, installed)


# --------------------------------------------------------------- Advanced actions
@_safe
@_manual_flow
def restore_from_backup():
    """Revert the whole collection to a pre-sync (or any other) backup.

    This is Anki's own backup restore, unscoped: it replaces every deck and note in the
    profile, not just the ones this add-on manages, since that's what a real collection
    backup contains. Anki asks for confirmation and reloads the profile itself.

    The interleave guard covers this dialog and the installed.json clear beneath it, so
    an auto-sync tick can't land between the two. It cannot cover Anki's own restore,
    which happens after this returns; nothing here can, and the poll's own `mw.col is
    None` check is what holds during the profile reload.
    """
    if not _ask(
        "This opens Anki's own backup picker so you can revert your whole collection "
        "(every deck, not just Intern Pearls ones) to an earlier point. Anki will ask "
        "you to confirm the specific backup before doing anything. Continue?",
        yes_label="Choose a backup", no_label="Cancel"
    ):
        return
    # Before onOpenBackup, not after: it reloads the profile, so code after it does
    # not reliably run.
    invalidate_installed()
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
    _info(f"Exported <b>{plural(note_count, 'note')}</b> from {deck_name} to:"
          f"<br><code>{path}</code><br><br>"
          "Review history, deck options, and media are all included, this is a "
          "complete, standalone copy of just this deck.")


@_safe
@_manual_flow
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
                "as new. A backup is taken automatically first.",
                yes_label="Import", no_label="Cancel"):
        return
    if not _pre_sync_backup_or_confirm_skip(_cfg()["export_deck"])[0]:
        return
    try:
        _import_apkg(src, with_scheduling=True)
    except Exception as e:
        _warn(f"Import failed: {e}")
        return
    # The imported file holds older cards than the source does, so whatever it restored
    # has to be re-offered. Scope that to the decks actually in the file, falling back to
    # all of them if it cannot be read: a redundant re-offer is recoverable, a missed one
    # silently leaves stale cards looking current. installed.json's own keys are the
    # manifest names last applied, so no fetch is needed here. An empty match (names or
    # None) also clears everything rather than nothing: if the decks were renamed before
    # this backup was taken, the file's deck names won't map to anything current, yet the
    # import did roll back a tracked deck, so clearing all is correct here, not merely a
    # conservative fallback.
    try:
        names = manifest_decks_for(apkg_deck_names(src), list(_load_json(INSTALLED, {})))
        invalidate_installed(names or None)
    except Exception:
        invalidate_installed()
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
@_manual_flow
def update_notetypes():
    """Add any missing managed field to the collection's note types.

    Guarded like the other collection-writing menu actions: adding a field bumps the
    collection schema, and an auto-sync tick landing on top of that is the interleave
    the flag exists to prevent.
    """
    added = _ensure_notetypes()
    if not added:
        _info("Note types are already up to date, no changes needed.")
        return
    items = []
    append_rows(items, [("row", None, line, "") for line in added])
    show_result("Updated note types (cards and scheduling untouched)", items)


def find_empty_cards(scope_tag):
    """Empty cards on the learner's own notes: (rows, skipped_count).

    Asks Anki for its own empty-cards report rather than deciding what "empty" means
    here. That report is a backend render of every card in the collection, so it stays
    right as note types and templates change, and it is the same source Anki's own
    Tools / Empty Cards acts on. Everything after it is narrowing: select_empty_cards
    keeps only notes under the scope tag and refuses any note whose every card is
    empty, then each surviving row gets the label and the deletion numbers the
    confirmation needs.

    `ords` are 1-based to match what the learner reads on the dead card ("No cloze 3
    found on card" is ord 2 internally).
    """
    search = f'"tag:{scope_tag}" OR "tag:{scope_tag}::*"' if scope_tag else ""
    scoped = set(mw.col.find_notes(search))
    report = mw.col.get_empty_cards()
    raw = [{"nid": int(n.note_id), "card_ids": [int(c) for c in n.card_ids],
            "will_delete_note": bool(n.will_delete_note)} for n in report.notes]
    removable, skipped = select_empty_cards(raw, scoped)
    rows = []
    for r in removable:
        note = mw.col.get_note(r["nid"])
        rows.append({
            "nid": r["nid"],
            "card_ids": r["card_ids"],
            "label": note_display_label(note.fields),
            "ords": sorted(mw.col.get_card(cid).ord + 1 for cid in r["card_ids"]),
        })
    return rows, len(skipped)


@_safe
@_manual_flow
def remove_empty_cards():
    """Remove the empty cards on the learner's own notes, after showing exactly which.

    The one place this add-on deletes anything, and it is deliberately narrow. An empty
    card holds no content: its note keeps every field, and the card itself renders as
    "No cloze 3 found on card" because the blank it was generated for is gone. Archiving
    one the way a retired card is archived would leave a dead card sitting in a Retired
    deck forever, which is the thing the learner was already suspending by hand.

    Three guards, in order: the note must be hers (scope tag), the note must keep at
    least one real card (select_empty_cards refuses the rest, so no note can be orphaned
    into deletion), and she sees and confirms every card first. The usual automatic
    backup runs before anything is touched, so an unwanted run is recoverable.
    """
    cfg = _cfg()
    rows, skipped = find_empty_cards(cfg["scope_tag"])
    if not rows:
        _info("No empty cards found." + (
            f"<br><br>({plural(skipped, 'note')} "
            f"{'has' if skipped == 1 else 'have'} no content on any card at all and "
            f"{'was' if skipped == 1 else 'were'} left alone, since removing those "
            "cards would delete the note itself.)"
            if skipped else ""))
        return
    n_cards = sum(len(r["card_ids"]) for r in rows)
    safety = ("Only the empty cards are removed; the notes themselves, and "
              "every card that still shows something, are left exactly as they are. A "
              "backup is taken automatically before anything changes.")
    # One kind of row throughout, so nothing is chipped and nothing lines up against a
    # chip: the caret and chip columns are declined (see widgets.simple_row). The
    # missing deletion numbers are short and read down the list rather than with the
    # card's own name, which is exactly what the trailing column is for. Uncapped,
    # since the rows stream inside a scroll area with the buttons outside it.
    heading, lines, tail = empty_cards_dialog_rows(rows, skipped)
    items = []
    append_rows(items, [("row", None, line["label"], line["gone"]) for line in lines])
    if not _ask_with_widget(
        build_list_body(items, top_html=heading,
                        bottom_html="<br><br>".join(x for x in (tail, safety) if x),
                        card_columns=False),
        yes_label=f"Remove {plural(n_cards, 'card')}", min_height=_CONFIRM_HEIGHT
    ):
        return
    # Scoped from where the affected cards actually sit. The report above is Anki's own,
    # narrowed by tag rather than by deck, so an empty card the learner has filed
    # outside export_deck is still deleted, and a backup of export_deck alone covered
    # none of it while the confirmation promised one.
    affected = {mw.col.get_note(r["nid"]).guid: r["nid"] for r in rows}
    proceed, backed_up = _pre_sync_backup_or_confirm_skip(
        cfg["export_deck"], decks_holding(list(affected), affected), cfg["scope_tag"])
    if not proceed:
        return
    # Asked again after the confirmation, not reused from before it. The report above
    # was computed before a modal dialog the reader can sit in for as long as she likes,
    # and an import landing in the meantime (an auto-sync tick, an undo) can give one of
    # these cards real content back. Acting on the stale list would then delete a card
    # that now shows something, which is the one thing this add-on must never do. Only
    # cards on BOTH lists are removed, so nothing new is deleted without having been
    # shown either.
    still_empty = {cid for r in find_empty_cards(cfg["scope_tag"])[0]
                   for cid in r["card_ids"]}
    cids = [cid for r in rows for cid in r["card_ids"] if cid in still_empty]
    if not cids:
        _info("Those cards aren't empty any more, so nothing was removed.")
        return
    notes = {r["nid"] for r in rows if any(c in still_empty for c in r["card_ids"])}
    mw.col.remove_cards_and_orphaned_notes(cids)
    mw.reset()
    backup_line = ("" if backed_up else
                   "<br><br>(No backup was taken this time: nothing to back up yet, or "
                   "it failed and you chose to continue.)")
    _info(f"Removed <b>{plural(len(cids), 'empty card')}</b> from "
          f"<b>{plural(len(notes), 'note')}</b>. Nothing else changed." + backup_line)


# --------------------------------------------------------------- AI card import

# Note types a generated card may name: the ones this add-on manages (TARGET_FIELDS)
# plus Anki's own core Basic/Cloze. Nothing else is trusted, even if it happens to
# exist in this collection -- a learner's own note type is not this feature's to write.
_GENERATED_ALLOWED_TYPES = frozenset(TARGET_FIELDS) | {"Basic", "Cloze"}


def add_generated_notes(cards, media, deck_name, scope_tag):
    """Write accepted AI-generated cards into `deck_name` as one undoable operation.

    Media contract (the other half lives in the review dialog that calls this): by the
    time a card reaches here, every attached:/url:/svg: image it references has already
    been resolved to bytes by that dialog, and the filenames it chose for THAT card are
    listed in card["_media_files"], in render order. `media` is {filename: bytes} for
    every such file across the whole accepted batch. This function only writes those
    bytes into the collection's media folder and turns each card's _media_files into
    `<img src="...">` tags appended to its Image field (or its primary field, for a
    note type with none) -- it never resolves or fetches an image itself.

    Every note gets a fresh iplocal- GUID (ai_logic.generated_guid()), so it can never
    match -- and a later deck sync's remap_cards/_reconcile_pending can never touch --
    a real synced card. Nothing here reads or modifies an existing note; this only adds.

    Raises RuntimeError, before writing anything (media or notes), if a card names a
    note type outside _GENERATED_ALLOWED_TYPES or one absent from this collection --
    an atomic check, so that failure mode never leaves anything behind. Returns the
    number of notes added; 0 for an empty `cards`.

    A failure part-way through the actual writes (a media write erroring, a backend
    add_note call failing) is NOT rolled back -- Anki gives no cheap way to undo mid
    write -- so a partial import is possible here. What's guaranteed instead: whatever
    already landed, media and notes alike, is still exactly one undo step, so the
    caller (or the user, with Ctrl+Z) can always get back to a clean collection in one
    move. The original exception always propagates; this function never swallows one.
    """
    cards = list(cards or [])
    if not cards:
        return 0
    col = mw.col
    _ensure_notetypes()   # a one-time, separate step; not part of this import's undo

    models, unknown = {}, set()
    for card in cards:
        ntype = card["note_type"]
        if ntype in models or ntype in unknown:
            continue
        model = col.models.by_name(ntype) if ntype in _GENERATED_ALLOWED_TYPES else None
        if model:
            models[ntype] = model
        else:
            unknown.add(ntype)
    if unknown:
        raise RuntimeError(
            "Can't import generated cards: unknown or missing note type(s) "
            + ", ".join(sorted(unknown)))

    undo_target = col.add_custom_undo_entry(f"Import {plural(len(cards), 'generated card')}")
    count = 0
    try:
        did = col.decks.id(deck_name)

        written = {}
        for fname, data in (media or {}).items():
            try:
                written[fname] = col.media.write_data(fname, data)
            except AttributeError:
                with tempfile.TemporaryDirectory() as tmpdir:
                    path = os.path.join(tmpdir, fname)
                    with open(path, "wb") as fh:
                        fh.write(data)
                    written[fname] = col.media.add_file(path)

        tag = f"{scope_tag}::{ai_logic.GENERATED_TAG_LEAF}"
        for card in cards:
            note = col.new_note(models[card["note_type"]])
            for name, value in card["fields"].items():
                if name in note:
                    note[name] = value
            imgs = "".join(f'<img src="{written.get(f, f)}">'
                           for f in card.get("_media_files", []))
            if imgs:
                if "Image" in note:
                    target = "Image"
                else:
                    target = ai_logic.PRIMARY_FIELD.get(
                        card["note_type"], next(iter(card["fields"])))
                note[target] = (note[target] + imgs) if note[target] else imgs
            note.guid = ai_logic.generated_guid()
            note.tags = list(card.get("tags", [])) + [tag]
            col.add_note(note, did)
            count += 1
    finally:
        # Whatever landed before a mid-loop failure is still exactly one undo step.
        col.merge_undo_entries(undo_target)
    return count
