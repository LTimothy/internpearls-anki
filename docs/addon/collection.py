"""Everything that reads or writes the Anki collection directly.

Note-type reconciliation, backups (deck-scoped and whole-collection), the protected-
fields snapshot/restore round trip, .apkg import/export, and the Advanced menu actions
that are thin user-facing wrappers over those helpers. The sync flows in sync.py
compose these; nothing here fetches from the network.
"""
import copy
import datetime
import hashlib
import html
import json
import os
import re
import tempfile
import uuid
import weakref
import zipfile
import zlib

from aqt import gui_hooks, mw
from aqt.utils import getFile, getSaveFile

from . import ai_logic
from .config import (DECK_BACKUPS_KEEP, DUPLICATE_TAG_LEAF, INSTALLED, RETIRED_TAG_LEAF,
                     SHIPPED, TARGET_FIELDS, _USER_FILES, _cfg, _collection_state_path,
                     _load_json, _save_json)
from .logic import (HASH_KEY, apkg_deck_names, apkg_model_fields, apkg_models,
                    apkg_note_types, apkg_notes, base_notetype_name,
                    cards_lost_in_conversion, changed_templates, check_apkg_limits,
                    copy_apkg_checked, declined_drop,
                    empty_cards_dialog_rows, fields_to_carry_over, manifest_decks_for,
                    learner_owns, merge_learner_tags, model_shape, note_display_label,
                    plan_notetype_changes, plural, protected_for, remap_cards,
                    scope_search, select_empty_cards, source_changed,
                    write_personalized)
from .platform import platform, platform_owner_id
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
    """Managed note types whose card templates or CSS differ between the .apkg at `src`
    and this collection. Returns {name: incoming model_shape}.

    Imports run with merge_notetypes=False (see _import_apkg), so a template change
    never propagates on its own; sync detects it here and asks. Only note types in
    TARGET_FIELDS are checked.
    """
    incoming = {n: s for n, s in apkg_models(src).items() if n in TARGET_FIELDS}
    existing = {m["name"]: model_shape(m) for m in mw.col.models.all()
                if m["name"] in TARGET_FIELDS}
    # The field names ride along: a new template may name a field this collection's
    # note type has never been given.
    fields = apkg_model_fields(src)
    return {n: dict(incoming[n], flds=fields[n])
            for n in changed_templates(incoming, existing)}


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
        have = {f["name"] for f in m["flds"]}
        for fname in inc.get("flds", ()):
            if fname not in have:   # Anki rejects a template naming a missing field
                mm.add_field(m, mm.new_field(fname))
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
    folder). Returns (folder, written), or (None, False) if it failed. Even when forced,
    Anki writes nothing when the collection is unchanged since its last backup, so
    `written` False with a folder means the latest backup is already current.
    """
    try:
        folder = mw.pm.backupFolder()
        written = mw.col.create_backup(backup_folder=folder, force=True,
                                       wait_for_completion=True)
        return folder, bool(written)
    except Exception:
        return None, False


def _deck_backup_folder():
    state = _collection_state_path(os.path.join(_USER_FILES, "installed.json"))
    folder = os.path.join(os.path.dirname(state), "deck_backups")
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
    # legacy=True: a modern package holds its collection in a zstd-compressed member,
    # which no reader in this add-on can open (zstandard is not stdlib and Anki does not
    # ship it), so import_deck and Import single deck could not use such a backup.
    #
    # Set field by field, guarded: these are protobuf fields, and a build that dropped
    # or renamed one would raise on a constructor keyword.
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
# files from anything else in the folder.
_BACKUP_PREFIX = "Intern Pearls "


def _backup_label(label):
    """A deck name reduced to something a filename can hold, or "" for no label.

    Collapsed to letters, digits, spaces, dots, dashes and underscores, then given a
    short hash of the raw name. The hash keeps apart names that sanitize alike ("A/B"
    and "A:B"), which would otherwise share a filename and a prune bucket.
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

    Normally an exact match. A file whose label has no hash (an older format) prunes
    with the label whose sanitized name it is. Compared against the label with its hash
    removed, not by prefix, so the roots "Foo" and "Foo Bar" never share a bucket.
    """
    return found == label or (bool(found) and label.rsplit(" ", 1)[0] == found)


def _backup_deck(deck_name, label=None, keep=None):
    """Write a timestamped .apkg backup of `deck_name` (with history), pruning old ones.
    Returns the backup's path, or None if it failed (e.g. the deck doesn't exist yet,
    which is normal on a first sync).

    Every backup carries its deck identity (sanitized and hashed) and a unique suffix,
    so two runs in the same second cannot overwrite each other.

    `keep` is a backup file that must survive this call's prune: the one a restore is
    about to import.

    Pruning keeps DECK_BACKUPS_KEEP per label, not per folder, so a run over several
    roots cannot evict another root's history or its own new files.
    """
    folder = _deck_backup_folder()
    deterministic_token = getattr(platform(), "deterministic_token", None)
    if deterministic_token is None:
        stamp = (datetime.datetime.now().strftime("%Y-%m-%d-%H%M%S") + "-"
                 + uuid.uuid4().hex[:12])
    else:
        stamp = (platform().wall_now().strftime("%Y-%m-%d-%H%M%S") + "-"
                 + deterministic_token())
    label = _backup_label(label or deck_name)
    suffix = f" {label}" if label else ""
    path = os.path.join(folder, f"{_BACKUP_PREFIX}{stamp}{suffix}.apkg")
    try:
        _export_deck_to(path, deck_name)
    except Exception:
        return None
    try:
        _save_json(_baseline_path(path), _load_json(SHIPPED, {}))
    except Exception:
        pass   # a restore without it keeps the restored values, see _reset_baseline
    kept = os.path.realpath(keep) if keep else None
    backups = sorted((f for f in os.listdir(folder)
                      if f.startswith(_BACKUP_PREFIX) and f.endswith(".apkg")
                      and _in_backup_group(_label_of_backup(f), label)
                      and os.path.realpath(os.path.join(folder, f)) != kept),
                     key=lambda f: (f == os.path.basename(path),
                                    f[len(_BACKUP_PREFIX):len(_BACKUP_PREFIX) + 17],
                                    os.stat(os.path.join(folder, f)).st_mtime_ns),
                     reverse=True)
    for old in backups[DECK_BACKUPS_KEEP:]:
        for victim in (os.path.join(folder, old), _baseline_path(old)):
            try:
                os.remove(victim)
            except OSError:
                pass
    return path


def _baseline_path(backup):
    """Where the shipped-field baseline a deck backup was taken with is kept: a sibling
    folder, so the backup folder holds only backups."""
    folder = os.path.join(os.path.dirname(_deck_backup_folder()), "deck_backup_baselines")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, os.path.basename(backup)[:-len(".apkg")] + ".json")


def _backup_targets(deck_name, decks):
    """Which deck(s) a pre-sync backup has to cover for a run touching `decks`.

    `deck_name` (the configured export_deck) is the answer whenever every touched deck
    sits under it. A run reaching outside it gets each touched deck's top-level deck
    instead, since exporting that covers every subdeck. `decks` of None gets `deck_name`
    alone.
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


def _pre_sync_backup_or_confirm_skip(deck_name, decks=None, scope_tag=None, keep=None):
    """Back up before Sync/Import touch the collection, or ask to proceed if it can't.
    Deck-scoped, not whole-collection.

    `decks` is the deck names this run will change, which the backup is scoped from (see
    _backup_targets). `keep` is a backup file the prune must leave alone.

    Returns (proceed, backed_up). proceed=True with backed_up=False means either there
    was nothing to back up (a first sync) or the user chose to continue without one, so
    callers must not say a backup was saved. `scope_tag` separates those two: when the
    collection already holds cards under it and none can be backed up, this asks rather
    than proceeding silently.
    """
    targets = [d for d in _backup_targets(deck_name, decks)
               if mw.col.decks.id_for_name(d) is not None]
    if targets:
        saved = {d: _backup_deck(d, d, keep) for d in targets}
        failed = [d for d, path in saved.items() if not path]
        if not failed:
            return True, True
        # Name both halves: which decks were backed up and which were not.
        covered = [d for d, path in saved.items() if path]
        covered_text = ", ".join(html.escape(str(d), quote=False) for d in covered)
        failed_text = ", ".join(html.escape(str(d), quote=False) for d in failed)
        done = (f"Backed up: {covered_text}.<br><br>" if covered else "")
        proceed = _ask(
            f"Couldn't back up: {failed_text}.<br><br>{done}"
            "Proceed anyway? (You can back up manually first: Advanced → Backup "
            "intern pearls deck, or Advanced → Backup full collection.)",
            yes_label="Continue without a backup", no_label="Cancel")
        return proceed, bool(covered)

    if scope_tag and mw.col.find_notes(scope_search(scope_tag)):
        return _ask(
            "Couldn't find a deck to back up: your cards aren't under a deck this "
            "add-on knows how to export on its own.<br><br>Proceed without a backup? "
            "(You can back up manually first: Advanced → Backup full collection.)",
            yes_label="Continue without a backup", no_label="Cancel"), False
    return True, False   # nothing in the collection to back up yet, e.g. a first sync


def _pre_sync_backup_or_skip_silently(deck_name, decks=None, scope_tag=None):
    """Background counterpart to `_pre_sync_backup_or_confirm_skip`: never shows a
    dialog. If a needed backup fails, the auto-sync aborts rather than importing
    unprotected; the next poll retries.

    `decks` is scoped as on the interactive path (see _backup_targets). Any target that
    can't be backed up aborts the whole tick.

    Returns "ok" to proceed, "no-deck" when cards exist but no deck holding them does
    yet, or "failed" when a backup couldn't be written.
    """
    targets = [d for d in _backup_targets(deck_name, decks)
               if mw.col.decks.id_for_name(d) is not None]
    if not targets:
        # Nothing to back up on a first sync. Cards under `scope_tag` with no deck to
        # export are what the interactive path asks about, so skip the round instead.
        if scope_tag and mw.col.find_notes(scope_search(scope_tag)):
            return "no-deck"
        return "ok"
    saved = [_backup_deck(d, d) for d in targets]
    return "ok" if all(saved) else "failed"


# ----------------------------------------------------------------- notes snapshot
def _note_field(note, name):
    """The note's actual field name matching `name`, or None. Case-insensitive: the
    preserved-fields box is free text, and a missed match would silently protect
    nothing.
    """
    if name in note:
        return name
    lowered = name.lower()
    return next((f for f in note.keys() if f.lower() == lowered), None)


def _snapshot(protected, scope_tag, per_note=None):
    search = scope_search(scope_tag)
    snap = {}
    for nid in mw.col.find_notes(search):
        note = mw.col.get_note(nid)
        saved = {}
        for name in protected_for(note.guid, protected, per_note):
            f = _note_field(note, name)
            if f:
                saved[f] = note[f]
        if saved:
            snap[note.guid] = saved
    return snap


def _snapshot_fields(guid, fields):
    """Current values of the named fields on one note, keyed by the note's own field
    name, blanks included.

    For topping up a snapshot already taken: a note matched by front reveals its guid
    only once its package is fetched, so its per-note declared fields (see
    per_note_for_package) are read then.
    """
    nid = mw.col.db.scalar("select id from notes where guid = ?", guid)
    if not nid:
        return {}
    note = mw.col.get_note(nid)
    saved = {}
    for name in fields:
        f = _note_field(note, name)
        if f:
            saved[f] = note[f]
    return saved


def _snapshot_tags(scope_tag):
    search = scope_search(scope_tag)
    snap = {}
    for nid in mw.col.find_notes(search):
        note = mw.col.get_note(nid)
        snap[note.guid] = list(note.tags)
    return snap


def _restore_tags(snap, scope_tag, touched):
    """Put back the tags an import replaced that the source does not manage (Anki's
    importer overwrites a matched note's tags with the package's). Returns the count."""
    markers = [f"{scope_tag}::{RETIRED_TAG_LEAF}", f"{scope_tag}::{DUPLICATE_TAG_LEAF}"]
    notes = []
    nids = _guid_to_nid()
    for guid in touched:
        if guid not in snap:
            continue
        nid = nids.get(guid)
        if not nid:
            continue
        note = mw.col.get_note(nid)
        merged = merge_learner_tags(snap[guid], note.tags, scope_tag, markers)
        if sorted(t.lower() for t in merged) != sorted(t.lower() for t in note.tags):
            note.tags = merged
            notes.append(note)
    # One write, one undo step: Anki keeps only 30, and the caller merges them.
    if notes:
        mw.col.update_notes(notes)
    return len(notes)


def _capture_shipped(protected, scope_tag, touched, per_note=None):
    """The deck source's own values, read straight after an import and before anything
    is restored: the one moment the collection holds them.

    Limited to `touched` (the guids this import wrote). A note outside it still holds
    the learner's restored value, and recording that as shipped would let the next sync
    overwrite their edit.
    """
    if not touched:
        return {}
    search = scope_search(scope_tag)
    out = {}
    touched_ids = set()
    guids = list(touched)
    for start in range(0, len(guids), 500):
        chunk = guids[start:start + 500]
        placeholders = ', '.join('?' for _ in chunk)
        touched_ids.update(mw.col.db.list(
            f"select id from notes where guid in ({placeholders})", *chunk))
    for nid in mw.col.find_notes(search):
        if nid not in touched_ids:
            continue
        note = mw.col.get_note(nid)
        vals = {}
        for name in protected_for(note.guid, protected, per_note):
            f = _note_field(note, name)
            if f:
                vals[f] = note[f]
        if vals:
            out[note.guid] = vals
    return out


def _restore(snap, baseline=None, touched=None):
    """Put the learner's own annotations back after an import, but only the ones that
    are theirs. Anki's importer overwrites every field on a matched note.

    `baseline` ({guid: {field: value}}, what the source last shipped for that note)
    makes this a three-way merge. If the learner's pre-import value equals what was last
    shipped, they never touched it and the imported value stands. If it differs, the
    field is theirs and is restored.

    With no baseline for a note, every non-blank value is restored. A blank is the
    exception: without a baseline it can't be told from a field nobody has written, so
    the source's value stands. With one, a blank where the source shipped text is the
    learner's own deletion and is kept. A field with no text baseline but a recorded
    hash is judged against the hash.

    `touched` is the guids this run's import wrote. Notes outside it are skipped:
    nothing was overwritten on them.

    Returns (restored, collisions). A collision is a field the learner edited and the
    source changed since the baseline; the learner's edit wins.
    """
    baseline = baseline or {}
    notes, collisions = [], []
    nids = _guid_to_nid()
    for guid, saved in snap.items():
        if touched is not None and guid not in touched:
            # Nothing imported over this note, so there is nothing to put back, and
            # comparing it to the baseline would report a false conflict.
            continue
        nid = nids.get(guid)
        if not nid:
            continue
        note = mw.col.get_note(nid)
        was_shipped = baseline.get(guid, {})
        changed = False
        for f, v in saved.items():
            if f not in note:
                continue
            if not learner_owns(was_shipped, f, v):
                continue          # untouched by the learner; let the source's update stand
            if source_changed(was_shipped, f, note[f]) and note[f] != v:
                # Only when the two versions differ: an update matching what the learner
                # already wrote is not a conflict.
                collisions.append((guid, f))
            if note[f] != v:
                note[f] = v
                changed = True
        if changed:
            notes.append(note)
    if notes:
        mw.col.update_notes(notes)
    return len(notes), collisions


# -------------------------------------------------------------------- apkg helpers
def _import_apkg(path, with_scheduling=False):
    """with_scheduling=False for a spec-authored deck; True for reimporting our own
    exported or backed-up package. Either way Anki applies a file's scheduling only to
    cards the import creates, so a matched card keeps its own.
    """
    from anki.collection import ImportAnkiPackageRequest, ImportAnkiPackageOptions
    check_apkg_limits(path)
    opts = ImportAnkiPackageOptions()
    # merge_notetypes=False: merging rewrites the collection's note types, which bumps
    # the schema and forces a one-way full AnkiWeb sync. _ensure_notetypes() runs before
    # every import instead and touches the schema only to add a missing field.
    # Template/CSS changes therefore do not propagate on import (see _template_changes).
    #
    # Both sync and backup restoration intentionally replace older source content.
    # IF_NEWER silently skips notes edited locally (including our own conversion).
    opts.update_notes = 1  # ImportAnkiPackageUpdateCondition.ALWAYS
    for attr, val in (("with_scheduling", with_scheduling), ("merge_notetypes", False)):
        try:
            setattr(opts, attr, val)
        except Exception:
            pass
    return mw.col.import_anki_package(
        ImportAnkiPackageRequest(package_path=path, options=opts))


class _NoteIdentity(dict):
    """Unambiguous front matches plus the complete scoped GUID membership."""
    def __init__(self):
        super().__init__()
        self.guids = set()
        self.sources = {}


def scope_note_count(scope_tag):
    """How many notes carry `scope_tag` or one of its `::` children."""
    return len(mw.col.find_notes(scope_search(scope_tag)))

def _scoped_notes(scope_tag, exclude_tag=None):
    """`[(nid, guid, mid, fields, cards)]` for every note under the scope tag, in
    find_notes order, `cards` being `[(home did, reps)]` in card_ids() order. One query:
    per-note lookups are too slow for the main thread.
    """
    search = scope_search(scope_tag, exclude_tag)
    nids = mw.col.find_notes(search)
    if not nids:
        return []
    notes = {}
    # Ids inlined rather than bound: a large scope would pass SQLite's variable limit.
    for nid, guid, mid, flds, cid, did, odid, reps in mw.col.db.all(
            "select n.id, n.guid, n.mid, n.flds, c.id, c.did, c.odid, c.reps "
            "from notes n left join cards c on c.nid = n.id "
            f"where n.id in ({','.join(str(int(i)) for i in nids)}) "
            "order by n.id, c.ord, c.id"):
        note = notes.setdefault(nid, (nid, guid, mid, flds.split("\x1f"), []))
        if cid is not None:
            note[4].append((odid or did, reps))
    return [notes[nid] for nid in nids if nid in notes]


def _existing_front_to_guid(scope_tag):
    out = _NoteIdentity()
    ambiguous = set()
    for _nid, guid, _mid, fields, _cards in _scoped_notes(scope_tag):
        out.guids.add(guid)
        front = fields[0]
        if front in out and out[front] != guid:
            # A generated card is never matched by sync, so it yields to a synced one
            # rather than making the front ambiguous.
            if ai_logic.is_generated_guid(guid):
                continue
            if ai_logic.is_generated_guid(out[front]):
                out[front] = guid
                continue
            ambiguous.add(front)
        else:
            out[front] = guid
    for front in ambiguous:
        out.pop(front, None)
    shipped = _load_json(SHIPPED, {})
    # The deck's guid each drifted card last synced under, so a card whose front the
    # learner reworded still matches (see remap_cards).
    sources = {guid: entry.get(HASH_KEY)
               for guid, entry in (shipped if isinstance(shipped, dict) else {}).items()
               if isinstance(entry, dict) and isinstance(entry.get(HASH_KEY), str)
               and guid in out.guids}
    archived = _archived_guids(scope_tag) if sources else set()
    for guid, source in sources.items():
        if guid not in archived:
            out.sources[source] = guid
    return out


def _archived_guids(scope_tag):
    """Guids of notes archived under the scope's retired or duplicate leaves."""
    nids = {nid for leaf in (RETIRED_TAG_LEAF, DUPLICATE_TAG_LEAF)
            for nid in mw.col.find_notes(scope_search(f"{scope_tag}::{leaf}"))}
    return {mw.col.get_note(nid).guid for nid in nids}


def existing_front_map(scope_tag):
    """{normalized front: original front} for every note under scope_tag, for
    mechanical_checks' duplicate detection. Never blocks generation: any
    failure (e.g. a fresh profile with no notes yet) yields {}."""
    try:
        return {ai_logic._norm_front(front): front
                for front in _existing_front_to_guid(scope_tag)}
    except Exception:
        return {}


def _existing_guid_to_fields(scope_tag):
    """{note guid: {field name: value}} for every note under the scope tag, each note's
    values zipped against its own note type's field names.
    """
    names = {}
    out = {}
    for _nid, guid, mid, fields, _cards in _scoped_notes(scope_tag):
        if mid not in names:
            names[mid] = [f["name"] for f in mw.col.models.get(mid)["flds"]]
        out[guid] = dict(zip(names[mid], fields))
    return out


def _existing_guid_to_nid(scope_tag):
    """{note guid: note id} for every note under the scope tag."""
    return {guid: nid for nid, guid, *_rest in _scoped_notes(scope_tag)}


def _guid_to_nid():
    """{guid: note id} for the whole collection in one query. Anki keeps no index on
    guid, so asking per note scans the table each time. Lowest id wins on a repeat."""
    return dict(mw.col.db.all("select guid, id from notes order by id desc"))


def _note_mods(guids):
    """{guid: note mod} for the given guids, in one query."""
    wanted = set(guids)
    return {g: m for g, m in mw.col.db.all("select guid, mod from notes") if g in wanted}


def _stored_fields(guids):
    """{guid: {field name: value}} for the given guids as the collection holds them
    now, in one query. Read right after an import, this is the text Anki actually
    stored, which can differ from the package's (it renames a re-shipped image whose
    bytes changed and rewrites the field to match)."""
    wanted = set(guids)
    names, out = {}, {}
    for guid, mid, flds in mw.col.db.all("select guid, mid, flds from notes"):
        if guid in wanted:
            if mid not in names:
                names[mid] = [f["name"] for f in mw.col.models.get(mid)["flds"]]
            out[guid] = dict(zip(names[mid], flds.split("\x1f")))
    return out


def _home_did(card):
    """The deck a card belongs to: `odid` while it sits in a filtered deck, else `did`."""
    return getattr(card, "odid", 0) or card.did


def _deck_names():
    """{deck id: name} in one call; a card's deck id missing from it (a damaged
    collection) still gets whatever decks.name says."""
    names = {d.id: d.name for d in mw.col.decks.all_names_and_ids()}
    return lambda did: names.get(did) or mw.col.decks.name(did)


def _existing_guid_to_deck(scope_tag):
    """{note guid: current deck name} for every note under the scope tag, keyed off its
    first card's deck (all cards of a note normally share one deck).
    """
    deck_name = _deck_names()
    return {guid: deck_name(cards[0][0])
            for _nid, guid, _mid, _fields, cards in _scoped_notes(scope_tag) if cards}


def _existing_notes_summary(scope_tag, exclude_tag=None):
    """{guid, nid, model, front, label, reps, deck} for every note under the scope tag,
    the raw material find_duplicate_groups groups into duplicate candidates. `front` is
    the raw first field (the grouping key); `label` is a readable version for dialogs.

    `exclude_tag`, if given, is added as a search exclusion, so a note an earlier
    duplicate cleanup archived is not considered again.
    """
    deck_name = _deck_names()
    models = {}
    out = []
    for nid, guid, mid, fields, cards in _scoped_notes(scope_tag, exclude_tag):
        if not cards:
            continue
        if mid not in models:
            models[mid] = mw.col.models.get(mid)["name"]
        out.append({
            "guid": guid,
            "nid": nid,
            "model": models[mid],
            "front": fields[0],
            "label": note_display_label(fields),
            "reps": sum(reps for _did, reps in cards),
            "deck": deck_name(cards[0][0]),
        })
    return out


def installed_matching_collection(installed, scope_tag):
    """Reconcile installed.json against what's actually in the collection.

    installed.json lives in user_files/, outside the collection, so restoring a
    collection backup can leave it claiming a deck is synced when its cards are gone. A
    deck stays installed only if at least one note under scope_tag sits in the
    manifest's deck name or any subdeck under it; otherwise it is dropped, so a normal
    sync re-applies it.

    This can call a present deck missing in two cases: a reorg where Reconcile my decks
    has not yet moved the cards to the new deck name, and a card the learner filed
    outside the spec's hierarchy. Both cost one redundant re-sync of that deck and no
    data.
    """
    present = set(_existing_guid_to_deck(scope_tag).values())

    def _has_cards(name):
        return any(d == name or d.startswith(name + "::") for d in present)

    return {name: version for name, version in installed.items() if _has_cards(name)}


def decks_holding(guids, existing_guid_to_nid):
    """The decks the given notes' cards are sitting in right now, which is what a
    pre-run backup has to cover: the learner may have moved a card out of the deck a
    ledger recorded. Unknown guids are absent from the result.
    """
    out = []
    for guid in guids:
        nid = existing_guid_to_nid.get(guid)
        if nid is None:
            continue
        for cid in mw.col.get_note(nid).card_ids():
            name = mw.col.decks.name(_home_did(mw.col.get_card(cid)))
            if name and name not in out:
                out.append(name)
    return out


def apply_deck_moves(moves, existing_guid_to_nid):
    """Relocate a learner's cards to match a pure deck reorg without touching content or
    scheduling.

    `moves` is the filtered list from find_deck_moves_needed: every entry is a card
    still sitting where the source last put it. Schema-neutral (set_deck only), so it
    never forces an AnkiWeb full sync. Returns the number of notes moved.
    """
    n = 0
    for m in moves:
        nid = existing_guid_to_nid.get(m["guid"])
        if nid is None:
            continue
        cids = mw.col.get_note(nid).card_ids()
        if cids:
            mw.col.set_deck(cids, mw.col.decks.id(m["to"]))
            n += 1
    return n


def carry_over_protected_fields(retired, existing_guid_to_nid, protected_fields, per_note=None):
    """Before a retired note is archived, copy the learner's protected-field text (e.g.
    Notes) onto its replacement(s).

    `retired` is the fresh (not-yet-archived) entries from find_retired_in_collection,
    each with `guid` and `superseded_by`, and, when the caller has one, `source_guid`.
    Only fills a replacement's field if it's currently blank (fields_to_carry_over), and
    copies to every replacement the learner already has. Returns the number of
    replacement notes updated.

    `per_note` is keyed by the guid the deck source assigns and is looked up for the
    predecessor and for each replacement, so a field declared protected only on the
    successor is still carried. The predecessor's lookup uses `source_guid` when the
    entry carries one, else `guid`.
    """
    written = {}
    for r in retired:
        old_nid = existing_guid_to_nid.get(r["guid"])
        if old_nid is None:
            continue
        old_note = mw.col.get_note(old_nid)
        predecessor_fields = protected_for(
            r.get("source_guid", r["guid"]), protected_fields, per_note)
        saved = {}
        for name in predecessor_fields:
            f = _note_field(old_note, name)
            if f and old_note[f].strip():
                saved[f] = old_note[f]
        if not saved:
            continue
        for target_guid in r["superseded_by"]:
            target_nid = existing_guid_to_nid.get(target_guid)
            if target_nid is None:
                continue
            target_note = written.get(target_nid) or mw.col.get_note(target_nid)
            replacement_fields = protected_for(target_guid, protected_fields, per_note)
            current = {}
            for name in replacement_fields:
                f = _note_field(target_note, name)
                if f:
                    current[f] = target_note[f]
            to_write = fields_to_carry_over(saved, current)
            if not to_write:
                continue
            for f, v in to_write.items():
                # The replacement's note type can spell the field differently (casing),
                # so resolve the actual write target.
                tf = _note_field(target_note, f)
                if tf is None:
                    continue
                target_note[tf] = v
            written[target_nid] = target_note
    if written:
        mw.col.update_notes(list(written.values()))
    return len(written)


def archive_notes(nids, retired_deck, tag):
    """Get retired cards out of the review rotation without deleting anything.

    Moves every card of each note to `retired_deck` (created if absent), suspends those
    cards, and tags the notes with `tag` so a later reconcile run recognizes them as
    handled. None of these bump the schema, so this never forces a full AnkiWeb sync.
    Returns the number of notes archived.
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


# The scheduling a card carries. Not the deck (`did`) or the note's fields, so carrying
# it forward moves progress without moving organization.
_SCHED_FIELDS = ("type", "queue", "due", "ivl", "factor", "reps", "lapses")

# FSRS schedules from the card's memory state, not ivl/factor, so these travel with the
# interval.
_FSRS_FIELDS = ("desired_retention", "decay", "last_review_time")


def _home_due(card):
    """A card's real due: one in a filtered deck holds a placeholder in `due`."""
    if getattr(card, "odid", 0):
        return getattr(card, "odue", 0) or getattr(card, "due", 0)
    return getattr(card, "due", 0)


def _copy_memory_state(parent, card, scale):
    """Copy the parent's FSRS memory state onto `card`, its stability times `scale`.
    Stability is the FSRS counterpart of the interval and scales with it; difficulty
    carries over unchanged.

    A no-op on a collection not using FSRS, where memory_state is absent.
    """
    state = getattr(parent, "memory_state", None)
    if state is None:
        return
    try:
        copy = type(state)()
        copy.CopyFrom(state)
        if scale != 1:
            copy.stability = max(0.5, state.stability * scale)
        card.memory_state = copy
    except Exception:
        # A scheduler detail must not break the conversion; the card still carries the
        # seeded interval.
        pass


def carry_scheduling_forward(pairs, existing_guid_to_nid):
    """Move a stranded predecessor's review history onto its live successor.

    `pairs` is find_stranded_pairs' output. The predecessor's cards are copied onto the
    successor's by ordinal, so a renumbered cloze only moves the ordinals that still
    line up.

    A successor card is written only when it has fewer reps than the predecessor, so a
    studied card is never rolled back and a re-run is a no-op. Nothing is cleared from
    the predecessor.

    Every write is an ordinary card update with no schema bump. Returns the number of
    successor cards updated.
    """
    written = {}
    for p in pairs:
        old_nid = existing_guid_to_nid.get(p["guid"])
        new_nid = existing_guid_to_nid.get(p["successor_guid"])
        if old_nid is None or new_nid is None:
            continue
        src = {}
        for cid in mw.col.get_note(old_nid).card_ids():
            card = mw.col.get_card(cid)
            src[getattr(card, "ord", 0)] = card
        for cid in mw.col.get_note(new_nid).card_ids():
            dst = written.get(cid) or mw.col.get_card(cid)
            card = src.get(getattr(dst, "ord", 0))
            if card is None or getattr(dst, "reps", 0) >= getattr(card, "reps", 0):
                continue
            for f in _SCHED_FIELDS + _FSRS_FIELDS:
                if hasattr(card, f):
                    setattr(dst, f, getattr(card, f))
            dst.due = _home_due(card)
            # The same card's history, so the memory state travels whole.
            _copy_memory_state(card, dst, 1)
            written[cid] = dst
    if written:
        mw.col.update_cards(list(written.values()))
    return len(written)


def _collection_read_key():
    """A read cache belongs to one open database and every write invalidates it."""
    try:
        col = mw.col
        mod = col.mod
        changes = col.db.scalar("select total_changes()")
        if not isinstance(mod, int) or not isinstance(changes, int):
            return None
        # mod can repeat within a millisecond and undo can restore an older value.
        return weakref.ref(col), weakref.ref(col.db), mod, changes
    except Exception:
        return None


class _NoteTypeCache:
    def __init__(self, scope_tag):
        self.scope_tag = scope_tag
        self.key = None
        self.types = None

    def read(self):
        key = _collection_read_key()
        if key is None or key != self.key or self.types is None:
            self.types = _existing_note_types(self.scope_tag)
            self.key = key
        return self.types


def _existing_note_types(scope_tag):
    """{note guid: notetype name} for every note under the scope tag."""
    search = scope_search(scope_tag)
    out = {}
    for nid in mw.col.find_notes(search):
        note = mw.col.get_note(nid)
        out[note.guid] = note.note_type()["name"]
    return out


def _field_map(old_model, new_model):
    """new_fields for change_notetype_of_notes: one entry per field of the NEW note
    type, holding that field's index in the OLD one, or -1 to discard.

    Matched by name, not position: Anki's positional default would shift fields between
    Basic and Cloze. Front maps to Text explicitly; Back and Tag have nowhere to go.
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

    Anki carries the original's scheduling onto the first card and creates the rest as
    new. A sibling instead inherits the parent's ease, counters and standing at half its
    interval (floored at a day): the learner already knows the facts, but recalling one
    blank cold is harder than the parent's test.

    Only cards with no reviews of their own are touched, so re-running is a no-op.
    Returns the number of cards seeded.
    """
    seeded = []
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
            card.due = _home_due(parent)
            _copy_memory_state(parent, card, 0.5)
            seeded.append(card)
    if seeded:
        mw.col.update_cards(seeded)
    return len(seeded)


def change_note_types(changes):
    """Move the learner's notes onto the note type this update ships for them.

    Runs before the import: once a note is on the right type, the importer matches it by
    GUID and updates it in place, keeping the card and its review history.

    This bumps the collection's schema, so the caller must have consented to the
    one-time full AnkiWeb sync first.

    Returns the note ids converted, for the caller to hand to seed_converted_siblings
    after the import: the extra cloze cards exist only once the import writes the cloze
    markup.
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
            # Backstop: a missing target is the caller's to notice before consenting
            # (see missing_notetype_targets).
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


def _check_protected_conversion(changes, protected, per_note=None):
    """Refuse a conversion that would discard a nonempty protected field."""
    for change in changes:
        old = mw.col.models.by_name(change["old"])
        new = mw.col.models.by_name(change["new"])
        if not old or not new:
            continue
        kept = set(_field_map(old, new))
        nid = mw.col.db.scalar("select id from notes where guid = ?", change["guid"])
        if not nid:
            continue
        note = mw.col.get_note(nid)
        guid_protected = {str(name).casefold()
                          for name in protected_for(change["guid"], protected, per_note)}
        for i, field in enumerate(old["flds"]):
            if (i not in kept and field["name"].casefold() in guid_protected
                    and note[field["name"]].strip()):
                raise ValueError(f"Cannot convert this deck: protected field {field['name']} "
                                 "has content but no destination in the new note type.")


def fork_note_identities(changes):
    """Make explicitly kept old-format notes local and free their source GUIDs.

    Only the GUID changes. The note id, fields, cards, and scheduling remain attached
    to the learner's existing note. The generated identity is deliberately outside
    source matching, so the incoming note can keep its canonical GUID and receive
    future source updates without repeatedly duplicating it.

    Returns the old GUIDs that were replaced. The caller removes those identities from
    its preservation snapshot: after the fork, that GUID belongs to the new source
    note and must not inherit fields saved from the learner's local copy.
    """
    forked, notes, taken = set(), [], set()
    for change in changes:
        old_guid = change["guid"]
        if old_guid in forked:
            continue
        nid = mw.col.db.scalar("select id from notes where guid = ?", old_guid)
        if not nid:
            continue
        note = mw.col.get_note(nid)
        while True:
            local_guid = ai_logic.generated_guid()
            if (local_guid not in taken and not mw.col.db.scalar(
                    "select id from notes where guid = ?", local_guid)):
                break
        note.guid = local_guid
        taken.add(local_guid)
        notes.append(note)
        forked.add(old_guid)
    if notes:
        mw.col.update_notes(notes)
    return forked


def missing_notetype_targets(changes):
    """The note types `changes` would convert onto that this collection doesn't have.

    change_note_types can only move a note onto an existing note type, and the import
    that would create one runs after it. A caller that finds anything here holds that
    deck's version back and says so; the import adds the missing note type, so the next
    run converts.
    """
    return sorted({c["new"] for c in changes if not mw.col.models.by_name(c["new"])})


def notetype_changes(src, existing_fronts, aliases, scope_tag, declined,
                     existing_types=None):
    """Note-type changes this .apkg would need on the learner's own notes.

    Resolves the .apkg's guids through the same matching ladder the import uses, then
    compares types. Returns plan_notetype_changes' list, each change carrying `drops`
    (cards the conversion would delete); empty when nothing needs converting.

    `declined` is logic.declined_guids' set; a match is left out of the plan, since a
    declined note is dropped from the import (logic.declined_drop) and would be
    converted without receiving the new content. Both the package's guid and the guid it
    would be remapped to are tested. Required, so every caller decides about it.
    """
    remap, _in_place, _as_new, _new, _matched = remap_cards(src, existing_fronts, aliases)
    incoming = apkg_note_types(src)
    by_existing_guid = {}
    for rid, _fields, guid in apkg_notes(src):
        existing_guid = remap.get(rid, guid)
        if existing_guid in declined or guid in declined:
            continue
        if guid in incoming:
            by_existing_guid[existing_guid] = incoming[guid]
    if existing_types is None:
        existing_types = _existing_note_types(scope_tag)
    changes = plan_notetype_changes(by_existing_guid, existing_types, TARGET_FIELDS)
    for change in changes:
        change["drops"] = _cards_a_conversion_drops(change)
    return changes


def _cards_a_conversion_drops(change):
    """How many of the learner's cards converting this note would delete; 0 when the
    target note type is not in the collection yet (nothing can convert then)."""
    old = mw.col.models.by_name(change["old"])
    new = mw.col.models.by_name(change["new"])
    nid = mw.col.db.scalar("select id from notes where guid = ?", change["guid"])
    if not old or not new or not nid:
        return 0
    info = mw.col.models.change_notetype_info(old_notetype_id=old["id"],
                                              new_notetype_id=new["id"])
    ords = [mw.col.get_card(cid).ord for cid in mw.col.get_note(nid).card_ids()]
    return cards_lost_in_conversion(ords, new.get("type") == 1,
                                    list(info.input.new_templates), len(new["tmpls"]))


class NoteTypeFieldsRequired(RuntimeError):
    """An unattended import needs a one-time field addition."""


def _prepare_import_notetypes(con, allow_field_additions=True):
    """Align managed package notes with their existing model IDs and field names.

    Only the disposable package is rewritten. Missing fields are appended to the
    collection's model, never removed or reordered; templates stay local. A GUID
    match can belong to any legacy '+' variant, not just the canonical type.
    """
    row = con.execute("select models from col").fetchone()
    incoming = json.loads(row[0])
    mm = mw.col.models
    existing = {m['id']: copy.deepcopy(m) for m in mm.all()}
    by_name = {m['name']: m for m in existing.values()}
    plans = []
    changed = set()
    nids = _guid_to_nid()
    for rid, guid, mid, fields in con.execute("select id, guid, mid, flds from notes"):
        source = incoming[str(mid)]
        family = base_notetype_name(source['name'])
        if family not in TARGET_FIELDS:
            continue
        nid = nids.get(guid)
        local = mw.col.get_note(nid) if nid else None
        if local is not None and getattr(local, 'mod', None) is not None:
            # Anki skips equal modification timestamps even with ALWAYS. Lowering this
            # scratch timestamp lets same-second source edits update, and keeps matched
            # notes out of log.duplicate, which _apply_deck leaves out of `touched`.
            # Pinned by run_identical_first_sync_baseline_contract in
            # tests/real_anki_audit_contracts.py.
            con.execute("update notes set mod=mod-1 where id=? and mod=?",
                        (rid, local.mod))
        target = local.note_type() if local else existing.get(mid, by_name.get(family))
        if target is None:
            continue
        if base_notetype_name(target['name']) != family:
            # Different-family conversions require the existing consent flow.
            continue
        if (source.get('type', 0) != target.get('type', 0)
                or len(source['tmpls']) != len(target['tmpls'])):
            raise ValueError("The existing note type has incompatible card templates; "
                             "this deck was not installed.")
        names = [f['name'] for f in source['flds']]
        values = fields.split('\x1f')
        if len(names) != len(values) or len(set(names)) != len(names):
            raise ValueError("The package has inconsistent note fields.")
        plans.append((rid, target['id'], dict(zip(names, values)),
                      {name: local[name] for name in local.keys()} if local else {}))
        target = existing[target['id']]
        for name in names:
            if name not in {f['name'] for f in target['flds']}:
                target['flds'].append(mm.new_field(name))
                changed.add(target['id'])
    if changed and not allow_field_additions:
        raise NoteTypeFieldsRequired(
            "New note-type fields are needed; run Update my decks manually.")
    targets = {mid for _, mid, _, _ in plans}
    planned_rows = {rid for rid, _, _, _ in plans}
    untouched = list(con.execute("select id, mid from notes"))
    relocated = {}
    next_mid = max(set(existing) | {int(mid) for mid in incoming}, default=0) + 1
    for rid, mid in untouched:
        if rid in planned_rows or mid not in targets:
            continue
        # A different-family row awaiting consent still needs its original
        # schema, even when another row reuses that model ID for a local variant.
        if mid not in relocated:
            relocated[mid] = next_mid
            original = copy.deepcopy(incoming[str(mid)])
            original['id'] = next_mid
            incoming[str(next_mid)] = original
            next_mid += 1
        con.execute("update notes set mid=? where id=?", (relocated[mid], rid))
    for mid in targets:
        target = existing[mid]
        if mid in changed:
            (mm.update_dict if hasattr(mm, 'update_dict') else mm.save)(target)
            target = existing[mid] = mm.by_name(target['name'])
        incoming[str(mid)] = target
    for rid, mid, values, personal in plans:
        fields = [values.get(f['name'], personal.get(f['name'], ''))
                  for f in existing[mid]['flds']]
        con.execute("update notes set mid=?, flds=? where id=?",
                    (mid, '\x1f'.join(fields), rid))
    used = {str(mid) for (mid,) in con.execute("select distinct mid from notes")}
    con.execute("update col set models=?",
                (json.dumps({mid: model for mid, model in incoming.items() if mid in used}),))


def _apply_deck(src, aliases, existing_fronts, declined=frozenset(),
                expected_conflicting_rids=frozenset(), allow_field_additions=True,
                matched_out=None):
    """Import one deck, returning (in_place, as_new, touched) where `touched` is the
    guids this import wrote in the learner's collection: the remapped guid where a note
    matched one of theirs, the .apkg's own otherwise. _capture_shipped needs exactly
    that set.

    `declined` is the decline registry's guids; matching notes are dropped from the
    scratch package before Anki sees it, so a skipped or never-imported card never
    lands and a kept-back card's collection copy is never overwritten. Dropped notes
    are excluded from the counts and from `touched`. `matched_out`, when given, is
    extended with remap_cards' matched (rid, package guid, learner guid) triples."""
    remap, in_place, as_new, _, matched = remap_cards(src, existing_fronts, aliases)
    if matched_out is not None:
        matched_out.extend(matched)
    # Anki matches GUIDs globally, not just within our scope. Fail this deck before
    # import rather than silently writing an excluded note that was not backed up.
    scoped = set(getattr(existing_fronts, "guids", existing_fronts.values()))
    nids = _guid_to_nid()
    for _rid, _fields, guid in apkg_notes(src):
        if guid not in scoped and guid in nids:
            raise ValueError("This package matches a note outside the managed scope; "
                             "no cards from this deck were imported.")
    drop, touched, in_place, as_new = declined_drop(src, remap, existing_fronts, declined,
                                                     in_place, as_new)
    # A unique name in the system temp directory: a fixed path derived from `src` could
    # collide between runs or be pre-created as a symlink, and `src`'s own folder may be
    # read-only and is the learner's.
    out = platform().allocate_temporary_file(
        platform_owner_id(mw), "sync-import", ".sync.apkg")
    try:
        write_personalized(src, remap, out, drop=drop,
                           prepare_notetypes=lambda con: _prepare_import_notetypes(
                               con, allow_field_additions=allow_field_additions))
        result = _import_apkg(out)
        log = getattr(result, "log", None)
        if log is not None:
            conflicts = list(getattr(log, "conflicting", ()))
            conflict_rids = {
                row.id.nid for row in conflicts
                if getattr(getattr(row, "id", None), "nid", None) is not None
            }
            expected_conflicting_rids = set(expected_conflicting_rids)
            expected_conflicts = (len(conflict_rids) == len(conflicts)
                                  and conflict_rids <= expected_conflicting_rids)
            rejected = sum(len(getattr(log, key, ())) for key in
                           ("missing_notetype", "missing_deck", "empty_first_field"))
            if conflicts and not expected_conflicts:
                rejected += len(conflicts)
            if rejected:
                reasons = [("note-type conflicts", len(conflicts)
                            if not expected_conflicts else 0)]
                reasons.extend((label, len(getattr(log, key, ()))) for key, label in
                               (("missing_notetype", "missing note types"),
                                ("missing_deck", "missing decks"),
                                ("empty_first_field", "empty first fields")))
                detail = ", ".join(f"{count} {label}" for label, count in reasons if count)
                raise RuntimeError(f"Anki rejected {rejected} notes ({detail}); "
                                   "this deck was not installed.")
            # Use backend outcomes, not the preview's predictions, for preservation
            # baselines. Unchanged duplicates must not capture personal text as shipped.
            touched = set()
            for row in list(log.new) + list(log.updated):
                touched.add(mw.col.get_note(row.id.nid).guid)
            as_new = len(log.new)
            in_place = len(log.updated) + len(log.duplicate) + len(conflicts)
    finally:
        try:
            os.remove(out)
        except OSError:
            pass
    return in_place, as_new, touched


def invalidate_installed(names=None):
    """Drop the add-on's record of which deck versions are applied.

    installed.json lives in user_files/, outside the collection, so a restore rolls the
    cards back while it still claims the newest version. `names` limits the drop to
    those decks; None clears everything. Re-imports match by GUID, so re-offering an
    unchanged deck costs bandwidth, never history.
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
    """Revert the whole collection to a backup.

    This is Anki's own restore: it replaces every deck and note in the profile, asks for
    confirmation, and reloads the profile itself.

    The interleave guard covers this dialog and the installed.json clear, so an
    auto-sync tick can't land between the two. Anki's restore runs after this returns;
    the poll's `mw.col is None` check holds during the profile reload. installed.json is
    cleared only if the restore goes ahead.
    """
    if not _ask(
        "This opens Anki's own backup picker so you can revert your whole collection "
        "(every deck, not just Intern Pearls ones) to an earlier point. Anki will ask "
        "you to confirm the specific backup before doing anything. Continue?",
        yes_label="Choose a backup", no_label="Cancel"
    ):
        return
    # Anki closes the profile only once the learner accepts its confirmation, so that is
    # the signal a restore is happening; cancelling the picker never fires it.
    gui_hooks.profile_will_close.append(invalidate_installed)
    try:
        mw.onOpenBackup()
    finally:
        gui_hooks.profile_will_close.remove(invalidate_installed)


@_safe
def export_deck():
    """Export cfg["export_deck"] as a self-contained .apkg with scheduling, deck options
    and media, to a location the user picks.
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
        _warn(f"Export failed: {html.escape(str(e), quote=False)}")
        return
    _info(f"Exported <b>{plural(note_count, 'note')}</b> from "
          f"{html.escape(str(deck_name), quote=False)} to:"
          f"<br><code>{html.escape(str(path), quote=False)}</code><br><br>"
          "Review history, deck options, and media are all included, this is a "
          "complete, standalone copy of just this deck.")


@_safe
@_manual_flow
def import_deck():
    """Bring an exported or backed-up Intern Pearls .apkg back into this collection. The
    file carries this collection's own note GUIDs, so the importer matches by GUID with
    no front-text matching.
    """
    from .sync import RESTORE_UNDO_NAME, _restore_undo_steps, _undo_key
    src = getFile(mw, "Choose an Intern Pearls .apkg", cb=None,
                 filter="*.apkg", dir=_deck_backup_folder())
    if not src:
        return
    if isinstance(src, (list, tuple)):
        src = src[0]
    if not _ask(f"Import {html.escape(str(os.path.basename(src)), quote=False)}? "
                "Matching cards are updated in place, keeping their scheduling; "
                "anything not already here is added "
                "as new. A backup is taken automatically first.",
                yes_label="Import", no_label="Cancel"):
        return
    cfg = _cfg()
    # Read before anything is written, so unreadable state refuses the restore up front.
    shipped = _load_json(SHIPPED, {})
    shipped = shipped if isinstance(shipped, dict) else {}
    # Anki's importer reads each member's stream and ignores the size the file declares
    # for it, so it imports a copy made through zipfile, which holds to those sizes.
    # Made first, so a file refused here costs no backup.
    checked = platform().allocate_temporary_file(
        platform_owner_id(mw), "deck-import", ".apkg")
    try:
        try:
            copy_apkg_checked(src, checked)
        except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError):
            _warn("This file isn't an Anki deck package (.apkg), so nothing was "
                  "imported.")
            return
        except Exception as e:
            _warn(f"Import failed: {html.escape(str(e), quote=False)}")
            return
        if not _pre_sync_backup_or_confirm_skip(cfg["export_deck"],
                                                _restore_decks(src),
                                                cfg["scope_tag"], keep=src)[0]:
            return
        try:
            undo = mw.col.add_custom_undo_entry(RESTORE_UNDO_NAME)
            try:
                result = _import_apkg(checked, with_scheduling=True)
            except Exception:
                try:
                    mw.col.merge_undo_entries(undo)
                except Exception:
                    pass
                raise
            else:
                mw.col.merge_undo_entries(undo)
        except Exception as e:
            _warn(f"Import failed: {html.escape(str(e), quote=False)}")
            return
    finally:
        try:
            os.remove(checked)
        except OSError:
            pass
    # The imported file holds older cards than the source, so what it restored has to be
    # re-offered: drop the installed record for the decks in the file, or for all decks
    # if the file cannot be read or none of its deck names match (they may have been
    # renamed since). Done before the baseline reset, so a failure there can't leave
    # restored cards reading as current.
    installed = _load_json(INSTALLED, {})
    installed = installed if isinstance(installed, dict) else {}
    deck_names = []
    try:
        deck_names = apkg_deck_names(src)
        names = manifest_decks_for(deck_names, list(installed))
    except Exception:
        names = None
    log = getattr(result, "log", None)
    guids = _restored_baseline_guids(src, log)
    if guids:
        prior_names = names or set(installed) | set(deck_names)
        key = _undo_key(undo)
        _restore_undo_steps[key] = (
            {name: (name in installed, installed.get(name)) for name in prior_names},
            {guid: (guid in shipped, shipped.get(guid)) for guid in guids})
    try:
        invalidate_installed(names or None)
    except Exception:
        if guids:
            _restore_undo_steps[key] = (
                {name: (name in installed, installed.get(name))
                 for name in set(installed) | set(deck_names)},
                _restore_undo_steps[key][1])
        invalidate_installed()
    _reset_baseline(src, log, shipped)
    mw.reset()
    name = html.escape(str(os.path.basename(src)), quote=False)
    skipped = len(getattr(log, "conflicting", None) or ())
    if skipped:
        _warn(f"Imported <code>{name}</code>, but Anki left {plural(skipped, 'card')} "
              "as they are now: their note type changed after this backup was made.")
        return
    _info(f"Imported <code>{name}</code>.")


def _restore_decks(src):
    """The decks a restore of `src` rewrites: wherever its notes sit now, scope tag or
    not, since the importer matches them by GUID across the whole collection. None when
    the file can't be read."""
    try:
        guids = {guid for _rid, _fields, guid in apkg_notes(src)}
    except Exception:
        return None
    found = {guid: mw.col.db.scalar("select id from notes where guid = ?", guid)
             for guid in guids}
    return decks_holding(guids, {g: nid for g, nid in found.items() if nid})


def _restored_baseline_guids(src, log=None):
    """The GUIDs whose baselines a restore will replace or drop."""
    try:
        if log is not None:
            return {mw.col.get_note(row.id.nid).guid
                    for row in list(log.new) + list(log.updated)}
        return {guid for _rid, _fields, guid in apkg_notes(src)}
    except Exception:
        return set()


def _reset_baseline(src, log=None, shipped=None):
    """Give each restored note the shipped-field baseline its backup was taken with, so
    the next update reads restored source text as the source's rather than as the
    learner's edit. A file with no saved baseline drops those notes' baselines, which
    keeps every non-blank restored value, annotations included.

    Limited to the notes Anki's import `log` says it added or changed: a note it matched
    unchanged still holds what its current baseline describes. Without a log, every
    note in the file counts."""
    guids = _restored_baseline_guids(src, log)
    if not guids:
        return
    own = os.path.dirname(os.path.realpath(src)) == os.path.realpath(_deck_backup_folder())
    saved = _load_json(_baseline_path(src), {}) if own and src.endswith(".apkg") else {}
    saved = saved if isinstance(saved, dict) else {}
    if shipped is None:
        shipped = _load_json(SHIPPED, {})
        shipped = shipped if isinstance(shipped, dict) else {}
    for guid in guids:
        if isinstance(saved.get(guid), dict):
            shipped[guid] = saved[guid]
        else:
            shipped.pop(guid, None)
    _save_json(SHIPPED, shipped)


@_safe
def backup_deck_now():
    """Manual version of the deck-scoped backup Sync/Import take automatically."""
    deck_name = _cfg()["export_deck"]
    path = _backup_deck(deck_name)
    if not path:
        _warn(f"Couldn't back up the <b>{html.escape(str(deck_name), quote=False)}</b> "
              "deck. It may not exist in this collection yet.")
        return
    _info("Backed up the Intern Pearls deck to:<br>"
          f"<code>{html.escape(str(path), quote=False)}</code>")


@_safe
def backup_collection_now():
    """Manual whole-collection backup."""
    folder, written = _backup_collection()
    if not folder:
        _warn("Couldn't create a collection backup.")
        return
    if not written:
        _info("Nothing has changed since your last collection backup, so it's already "
              f"up to date in:<br><code>{html.escape(str(folder), quote=False)}</code>")
        return
    _info("Backed up your whole collection (every deck) to:<br>"
          f"<code>{html.escape(str(folder), quote=False)}</code>")


@_safe
@_manual_flow
def update_notetypes():
    """Add any missing managed field to the collection's note types. Guarded like the
    other collection-writing menu actions: adding a field bumps the schema, and an
    auto-sync tick must not interleave with it.
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

    Uses Anki's own empty-cards report, then select_empty_cards keeps only notes under
    the scope tag and refuses any note whose every card is empty.

    `ords` are 1-based to match what the learner reads on the dead card ("No cloze 3
    found on card" is ord 2 internally).
    """
    search = scope_search(scope_tag)
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
    The one place this add-on deletes anything.

    Three guards, in order: the note must be the learner's own (scope tag), the note
    must keep at least one real card (select_empty_cards refuses the rest), and the
    learner sees and confirms every card first. The usual automatic backup runs before
    anything is touched.
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
    # One kind of row throughout, so the caret and chip columns are declined (see
    # widgets.simple_row).
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
    # Backup scoped from where the affected cards sit: the report is narrowed by tag,
    # not deck, so a card may be outside export_deck.
    affected = {mw.col.get_note(r["nid"]).guid: r["nid"] for r in rows}
    proceed, backed_up = _pre_sync_backup_or_confirm_skip(
        cfg["export_deck"], decks_holding(list(affected), affected), cfg["scope_tag"])
    if not proceed:
        return
    # Asked again after the confirmation: an import or undo during the modal dialog can
    # give a card content back. Only cards on both lists are removed, so nothing is
    # deleted that was not shown.
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
# exist in this collection: a learner's own note type is not this feature's to write.
_GENERATED_ALLOWED_TYPES = frozenset(TARGET_FIELDS) | {"Basic", "Cloze"}


class PartialImport(Exception):
    """add_generated_notes failed after some notes landed. `written` holds the
    positions in the input list of the cards that were added; the original error
    is the __cause__."""

    def __init__(self, written, total):
        super().__init__(f"{len(written)} of {total} generated cards were added")
        self.written = written
        self.total = total


def add_generated_notes(cards, media, deck_name, scope_tag):
    """Write accepted AI-generated cards into `deck_name` as one undoable operation.

    Media contract: the calling review dialog has already resolved every image a card
    references to bytes. card["_media_files"] lists that card's filenames in render
    order, and `media` is {filename: bytes} for the whole batch. This writes those bytes
    into the collection's media folder and appends an `<img src="...">` per file to the
    card's Image field (or its primary field, for a note type with none), each followed
    by its credit line from card["_media_credits"] (parallel to _media_files). A card's
    card["_sources"] become a "Sources:" line appended to its Why field, else Back, else
    Back Extra. It never resolves or fetches an image itself.

    "Basic" and "Cloze" resolve through ai_logic.find_core_notetype, so stock types with
    translated names work too.

    Every note gets a fresh iplocal- GUID (ai_logic.generated_guid()), so it never
    matches a synced card. Existing notes are not read or modified.

    Raises RuntimeError before writing anything if a card names a note type outside
    _GENERATED_ALLOWED_TYPES or one absent from this collection, or has an empty primary
    field and no picture. Returns the number of notes added; 0 for an empty `cards`.
    Each written card gets `_imported_note` with the note's id and guid.

    A failure during the writes is not rolled back, but whatever landed, media and notes
    alike, is one undo step. A failure after at least one note landed raises
    PartialImport (the original error as its __cause__) naming which cards were written;
    a failure before that propagates unchanged.
    """
    cards = list(cards or [])
    if not cards:
        return 0
    col = mw.col
    _ensure_notetypes()   # a one-time, separate step; not part of this import's undo

    models, field_maps, unknown = {}, {}, set()
    for card in cards:
        ntype = card["note_type"]
        if ntype in models or ntype in unknown:
            continue
        if ntype in ai_logic.CORE_NOTE_TYPES:
            model, field_maps[ntype] = ai_logic.find_core_notetype(col.models.all(), ntype)
        else:
            model = col.models.by_name(ntype) if ntype in _GENERATED_ALLOWED_TYPES else None
        if model:
            models[ntype] = model
        else:
            unknown.add(ntype)
    if unknown:
        raise RuntimeError(
            "Can't import generated cards: unknown or missing note type(s) "
            + ", ".join(sorted(unknown)))
    blank = [i for i, card in enumerate(cards, 1) if ai_logic.primary_is_blank(card)]
    if blank:
        raise RuntimeError(
            "Can't import generated cards: the front is empty on card "
            + ", ".join(map(str, blank)))

    undo_target = col.add_custom_undo_entry(f"Import {plural(len(cards), 'generated card')}")
    count = 0
    landed = []
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
        for pos, card in enumerate(cards):
            note = col.new_note(models[card["note_type"]])
            fmap = field_maps.get(card["note_type"], {})
            for name, value in card["fields"].items():
                if fmap.get(name, name) in note:
                    note[fmap.get(name, name)] = value
            files = card.get("_media_files", [])
            credits = card.get("_media_credits") or []
            credit_lines = [ai_logic.image_credit_html(credits[k] if k < len(credits) else "")
                            for k in range(len(files))]
            # A credit follows its picture only in an Image field; beside a picture in
            # the prompt field it would show on the question and join its front text.
            in_image_field = "Image" in note
            imgs = "".join(f'<img src="{written.get(f, f)}">'
                           + (credit_lines[k] if in_image_field else "")
                           for k, f in enumerate(files))
            if imgs:
                if in_image_field:
                    target = "Image"
                else:
                    primary = ai_logic.PRIMARY_FIELD.get(
                        card["note_type"], next(iter(card["fields"])))
                    target = fmap.get(primary, primary)
                note[target] = (note[target] + imgs) if note[target] else imgs
            extra = ("" if in_image_field else "".join(credit_lines)) + ai_logic.sources_html(
                card.get("_sources"))
            explain = next((fmap.get(n, n) for n in ("Why", "Back", "Back Extra")
                            if fmap.get(n, n) in note), None)
            if extra and explain:
                note[explain] = note[explain] + extra
            note.guid = ai_logic.generated_guid()
            note.tags = list(card.get("tags", [])) + [tag]
            col.add_note(note, did)
            # Anki keeps 30 undo steps; fold each add in before the target is evicted.
            col.merge_undo_entries(undo_target)
            card["_imported_note"] = {"id": note.id, "guid": note.guid}
            count += 1
            landed.append(pos)
    except Exception as e:
        if landed:
            raise PartialImport(landed, len(cards)) from e
        raise
    finally:
        # Whatever landed before a mid-loop failure is still exactly one undo step.
        col.merge_undo_entries(undo_target)
    return count


def deck_search(deck_name):
    """`deck:"..."` for one deck name, with the characters Anki's search parser reads as
    syntax escaped: an underscore matches any single character, an asterisk any run, and
    a quote would end the term early.
    """
    escaped = deck_name
    for ch in ("\\", '"', "*", "_"):
        escaped = escaped.replace(ch, "\\" + ch)
    return f'deck:"{escaped}"'


def note_rows(col, scope_tag=None, deck_name=None):
    """`[(note_id, text, deck_name, note_type)]` for the notes selected by `scope_tag`
    or by `deck_name` (mutually exclusive; neither given returns every note in the
    collection). `text` is the note's first two fields, joined by a space, since that's
    what the duplicate scan compares. `deck_name` in each row is the note's home deck,
    resolved through `odid` for a card currently sitting in a filtered deck (see
    `_home_did`), which is also how a deck-scoped selection is filtered: a plain
    `deck:"X"` search already resolves a filtered deck's cards to their original deck.
    """
    if scope_tag:
        search = scope_search(scope_tag)
    elif deck_name:
        search = deck_search(deck_name)
    else:
        search = ""
    nids = None if search == "" else set(col.find_notes(search))
    return _note_rows_sql(col, nids)


def _note_rows_sql(col, nids):
    """`note_rows` in one query: a join over notes and cards, with `group by` keeping
    one card per note."""
    deck_names = {d.id: d.name for d in col.decks.all_names_and_ids()}
    model_names = {}
    rows = []
    for nid, flds, mid, did, odid in col.db.all(
            "select n.id, n.flds, n.mid, c.did, c.odid from notes n "
            "join cards c on c.nid = n.id group by n.id order by n.id"):
        if nids is not None and nid not in nids:
            continue
        if mid not in model_names:
            model_names[mid] = _model_name(col, nid, mid)
        text = " ".join(flds.split("\x1f")[:2])
        rows.append((nid, text, deck_names.get(odid or did) or "", model_names[mid]))
    return rows


def _model_name(col, nid, mid):
    try:
        model = col.models.get(mid)
        if model:
            return model["name"]
    except Exception:
        pass
    return col.get_note(nid).note_type()["name"]


def suspend_notes(col, note_ids):
    """Suspend every card of every note in `note_ids` through the scheduler API, never
    direct SQL, so Anki can undo it.
    """
    cids = [cid for nid in note_ids for cid in col.get_note(nid).card_ids()]
    if cids:
        col.sched.suspend_cards(cids)


def unsuspend_notes(col, note_ids):
    """The other half of `suspend_notes`."""
    cids = [cid for nid in note_ids for cid in col.get_note(nid).card_ids()]
    if cids:
        col.sched.unsuspend_cards(cids)
