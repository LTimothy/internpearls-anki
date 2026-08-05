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

from .config import (DECK_BACKUPS_KEEP, INSTALLED, TARGET_FIELDS, _USER_FILES, _cfg,
                     _load_json, _save_json)
from .logic import (apkg_deck_names, apkg_models, apkg_note_types, apkg_notes,
                    bullets, changed_templates, empty_cards_dialog_html,
                    fields_to_carry_over, manifest_decks_for, model_shape,
                    note_display_label, plan_notetype_changes, remap_cards,
                    select_empty_cards, write_personalized)
from .palette import colors
from .ui import _ask, _ask_scrollable, _info, _safe, _warn


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
            if f in was_shipped and note[f] != was_shipped[f]:
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
    what will sync, and the Manage decks status pills would all wrongly read "up to
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


def notetype_changes(src, her, aliases, scope_tag):
    """Note-type changes this .apkg would need on the learner's own notes.

    Resolves the .apkg's guids through the same matching ladder the import uses, so a
    note matched by front counts, then compares types. Returns plan_notetype_changes'
    list; empty when nothing needs converting, which is the normal case.
    """
    remap, _in_place, _as_new, _new, _matched = remap_cards(src, her, aliases)
    incoming = apkg_note_types(src)
    by_her = {}
    for rid, _fields, guid in apkg_notes(src):
        her_guid = remap.get(rid, guid)
        if guid in incoming:
            by_her[her_guid] = incoming[guid]
    return plan_notetype_changes(by_her, _her_note_types(scope_tag), TARGET_FIELDS)


def _apply_deck(src, aliases, her):
    """Import one deck, returning (in_place, as_new, touched) where `touched` is the
    guids this import wrote in her collection: the remapped guid where a note matched
    one of hers, the .apkg's own otherwise. _capture_shipped needs exactly that set."""
    remap, in_place, as_new, _, _matched = remap_cards(src, her, aliases)
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
def update_notetypes():
    added = _ensure_notetypes()
    _info(("<b>Updated note types</b> (cards and scheduling untouched):" +
           bullets(added)) if added else
          "Note types are already up to date, no changes needed.")


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
            f"<br><br>({skipped} note(s) have no content on any card at all and were "
            "left alone, since removing those cards would delete the note itself.)"
            if skipped else ""))
        return
    n_cards = sum(len(r["card_ids"]) for r in rows)
    safety = ("<br><br>Only the empty cards are removed; the notes themselves, and "
              "every card that still shows something, are left exactly as they are. A "
              "backup is taken automatically before anything changes.")
    if not _ask_scrollable(
            empty_cards_dialog_html(rows, colors()["muted"], skipped) + safety,
            yes_label=f"Remove {n_cards} card(s)"):
        return
    proceed, backed_up = _pre_sync_backup_or_confirm_skip(cfg["export_deck"])
    if not proceed:
        return
    cids = [cid for r in rows for cid in r["card_ids"]]
    mw.col.remove_cards_and_orphaned_notes(cids)
    mw.reset()
    backup_line = ("" if backed_up else
                   "<br><br>(No backup was taken this time: nothing to back up yet, or "
                   "it failed and you chose to continue.)")
    _info(f"Removed <b>{len(cids)}</b> empty card(s) from "
          f"<b>{len(rows)}</b> note(s). Nothing else changed." + backup_line)
