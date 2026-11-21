"""The deck-sync and reconcile flows: source resolution, Sync decks, Reconcile my decks,
the unified Update my decks front door, and Import single deck.

_run_sync is the one implementation of the history-preserving sequence (fix note types,
snapshot protected fields, remap and import, restore, persist versions), shared by Sync
decks, update_decks() and the auto-sync poll in background.py. _reconcile_pending is the
one source of what Reconcile my decks would find pending, shared by reconcile_decks()
and update_decks().
"""
import datetime
import hashlib
import html
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import time
import weakref

from aqt import mw
from aqt.utils import getFile

from .ai_logic import is_generated_guid
from .collection import (NoteTypeFieldsRequired, _NoteTypeCache, _apply_deck,
                         _archived_guids,
                         _apply_template_changes, _capture_shipped,
                         _check_protected_conversion,
                         _ensure_notetypes, change_note_types, fork_note_identities,
                         missing_notetype_targets, notetype_changes,
                         seed_converted_siblings,
                         _existing_front_to_guid, _existing_guid_to_deck,
                         _existing_guid_to_fields, _existing_guid_to_nid,
                         _existing_notes_summary, _note_mods, _import_apkg,
                         _pre_sync_backup_or_confirm_skip, _restore, _restore_tags,
                         _snapshot, _snapshot_fields, _snapshot_tags, _stored_fields, _template_changes,
                         apply_deck_moves,
                         archive_notes, carry_over_protected_fields,
                         carry_scheduling_forward, decks_holding,
                         installed_matching_collection, invalidate_installed)
from .config import (ADDON_VERSION, DECK_CACHE, DUPLICATE_TAG_LEAF, INSTALLED,
                     MEDIA_CACHE, MEDIA_FILE_MAX_BYTES,
                     RETIRED_DECK_LEAF,
                     RECORDED_TEXT, RETIRED_TAG_LEAF, SHIPPED, SUPPORTED_MANIFEST_SCHEMA, _cfg,
                     _load_json, _save_json, collection_key, load_declined, load_deck_skill,
                     replace_file, save_declined, save_deck_skill, save_later_seen)
from .logic import (APKG_MAX_BYTES, HASH_KEY, MOD_KEY, apkg_deck_names, apkg_note_details, apkg_notes,
                    card_progress,
                    change_notes_for, changed_since, inject_apkg_media,
                    safe_source_path, validate_media_entries,
                    source_label_for, group_change_notes, sort_source_groups,
                    declined_drop, declined_guids,
                    decks_to_update, feedback_entries, merge_saved_feedback,
                    duplicate_dialog_rows, find_changed_notes, find_deck_moves_needed,
                    find_kept_edits, KeptEdit,
                    find_duplicate_groups, find_retired_in_collection,
                    find_stranded_pairs, held_deck_names, held_entries,
                    held_outside_manifest, holdable_guids, later_count, later_status,
                    settle_held_outside_manifest,
                    manifest_needs_newer_addon,
                    note_display_label, note_fields_hash, per_note_for_package,
                    with_edits_protected,
                    skill_version, unopened_line,
                    plain_text, plural, prune_declined, released_held_guids, remap_cards,
                    reveal_hidden, split_notetype_changes, strip_hidden,
                    find_learner_edits, hashes_unless_renamed, shipped_field_hashes,
                    stored_field_hashes,
                    truncate, with_field_hashes,
                    write_personalized)
from .net import (_CONNECT_TIMEOUT, _DOWNLOAD_TIMEOUT, DownloadCancelled,
                  TransportError, _gh_raw)
from .palette import colors
from .review import (_CONFIRM_HEIGHT, NOTHING_CHANGED, append_rows, build_list_body,
                     build_update_body, clear_saved_feedback, folded_guids, hold_control,
                     load_saved_feedback, show_result, show_result_with_feedback)
from .ui import (_ask, _ask_scrollable, _ask_with_widget, _info, _manual_flow, _safe,
                 _warn, cancellable_progress, ProgressCancelled, run_with_progress)

# The "Reconcile my decks" QAction, set once by __init__.py. Its label carries the
# pending count, refreshed from here and from background.py's auto-sync poll, since
# auto-sync never archives or relocates on its own.
_reconcile_action = None


def register_reconcile_action(action):
    """Called once by __init__.py right after building the menu."""
    global _reconcile_action
    _reconcile_action = action


def _refresh_reconcile_action_label(pending):
    """Show a pending count on the menu item, or reset to the plain label when nothing
    is left. No-op before the menu exists.
    """
    if _reconcile_action is None:
        return
    if pending:
        _reconcile_action.setText(f"Reconcile my decks ({pending} pending)")
    else:
        _reconcile_action.setText("Reconcile my decks")


_scratch_dir = None
_scratch_lock = threading.Lock()


def _scratch():
    """A private, per-session directory for downloaded decks and the personalized copies
    written beside them. mkdtemp is mode 0700 with an unguessable name, so the path
    cannot be pre-created as a symlink. One directory per session, so `_cached_fetch`
    can hand a preview download to the apply step by path.
    """
    global _scratch_dir
    # The background poll and a preview can both get here first.
    with _scratch_lock:
        if _scratch_dir is None or not os.path.isdir(_scratch_dir):
            os.makedirs(DECK_CACHE, exist_ok=True)
            _scratch_dir = tempfile.mkdtemp(prefix="internpearls-", dir=DECK_CACHE)
            # user_files outlives sessions, so earlier sessions' dirs would pile up.
            for name in os.listdir(DECK_CACHE):
                path = os.path.join(DECK_CACHE, name)
                if name.startswith("internpearls-") and path != _scratch_dir:
                    shutil.rmtree(path, ignore_errors=True)
        return _scratch_dir


def _scratch_path(apkg_path, version=None):
    """Where one manifest .apkg path downloads to inside the session's scratch dir.

    Keyed by the whole manifest path, not its basename, so decks/basics/Foo.apkg and
    decks/advanced/Foo.apkg stay apart: a short hash of the path plus a sanitized
    filename.

    The version (a content hash) is part of the key too, so a newly fetched version
    never lands on a file an open confirmation has already read.
    """
    key = f"{apkg_path}\n{version}" if version else apkg_path
    digest = hashlib.sha1(key.encode("utf8")).hexdigest()[:8]
    name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(apkg_path)) or "deck.apkg"
    return os.path.join(_scratch(), f"{digest}-{name}")


def _write_scratch(apkg_path, data, version=None):
    """Land a downloaded deck at its keyed scratch path, atomically. The background
    poll's worker thread and an interactive preview can write the same path, so the data
    goes to a temp file in the same directory and is renamed into place with os.replace.
    """
    fd, tmp = tempfile.mkstemp(dir=_scratch(), suffix=".part")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        path = _scratch_path(apkg_path, version)
        replace_file(tmp, path)
        return path
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# A manifest is a list of decks and a few ledgers: a large source's is about 500 KB.
_MANIFEST_MAX_BYTES = 32 * 1024 * 1024
# A deck skill is a page of instructions for the AI, sent with every run.
_SKILL_MAX_BYTES = 1024 * 1024


def _local_source_file(folder, path):
    """`path` from the manifest, as a file inside the local source `folder`.

    safe_source_path refuses an absolute path or '..'; this also follows symlinks, so a
    link inside the folder that points out of it is refused too, and requires a regular
    file, since a FIFO or a device would block the main thread on open.
    """
    full = os.path.join(folder, safe_source_path(path))
    root = os.path.realpath(folder)
    try:
        inside = os.path.commonpath([os.path.realpath(full), root]) == root
    except ValueError:      # another drive
        inside = False
    if not inside:
        raise RuntimeError(f'"{path}" resolves outside the deck source, so it wasn\'t '
                           "read")
    if not stat.S_ISREG(os.stat(full).st_mode):
        raise RuntimeError(f'"{path}" isn\'t a regular file, so it wasn\'t read')
    return full


def _text(value):
    """`value` as text for a rich-text label: deck names, card labels and messages carry
    the source's own characters, and a bare "<" would swallow the rest of the line."""
    return html.escape(str(value), quote=False)


def _card_html(fields, max_len=90):
    """A card's display label (note_display_label), escaped for a rich-text row."""
    return _text(note_display_label(fields, max_len=max_len))


def _require_manifest_object(manifest, where):
    """Refuse a manifest that parsed as valid JSON but isn't an object. Every reader
    downstream calls .get on it, so this is checked here at the source boundary.
    """
    if not isinstance(manifest, dict):
        raise RuntimeError(f"the manifest.json in {where} isn't valid: it holds a "
                           f"{type(manifest).__name__} where a set of decks was "
                           "expected")


def _source_warning(e):
    """Warn about a manifest fetch that failed, saying which kind it was.

    A source never reached (no DNS, refused, timed out) raises net.TransportError;
    everything else is a source found and unusable (no manifest.json, invalid JSON, an
    HTTP status about the repo, branch or token). The two need opposite next steps, so
    the advice follows the same split.
    """
    if isinstance(e, TransportError):
        _warn(f"Couldn't reach the deck source: {_text(e)}<br><br>"
              "Check your internet connection and try again. If the source itself has "
              "moved, open <b>Intern Pearls → Manage decks</b> and use Change source.")
        return
    _warn(f"The deck source couldn't be used: {_text(e)}.<br><br>"
          "Open <b>Intern Pearls → Manage decks</b> and use Change source to check "
          "your GitHub token or local folder.")


def _cached_media(sha, path=None):
    """`path` (default: the cache file for `sha`) if it holds exactly `sha`, else None."""
    path = path or os.path.join(MEDIA_CACHE, sha)
    try:
        if os.path.getsize(path) > MEDIA_FILE_MAX_BYTES:
            return None
        with open(path, "rb") as fh:
            if hashlib.sha256(fh.read()).hexdigest() == sha:
                return path
    except OSError:
        pass
    return None


def _ensure_media(entries, read, on_file=None, media_dir=None):
    """{filename: local path} for every image in `entries`, downloading only misses.

    A picture already in Anki's media folder (`media_dir`) with the same bytes is used
    where it is, then the cache, then the source. `read(path)` returns one image's
    bytes from the source; `on_file(k, n)` ticks once per download. A file that
    doesn't match its checksum is refused and never cached, so the deck waits for the
    next run.
    """
    out, missing = {}, []
    for name, entry in entries.items():
        sha = entry["sha256"]
        hit = (media_dir and _cached_media(sha, os.path.join(media_dir, name))
               or _cached_media(sha))
        if hit:
            out[name] = hit
        else:
            missing.append((name, entry))
    for k, (name, entry) in enumerate(missing, 1):
        sha = entry["sha256"]
        data = read(entry["path"])
        if hashlib.sha256(data).hexdigest() != sha:
            raise RuntimeError("an image for this deck didn't match its "
                               f"checksum: {name}")
        os.makedirs(MEDIA_CACHE, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=MEDIA_CACHE, suffix=".part")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            out[name] = os.path.join(MEDIA_CACHE, sha)
            replace_file(tmp, out[name])
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        if on_file is not None and not on_file(k, len(missing)):
            raise DownloadCancelled()
    return out


def _prune_media_cache(manifest):
    """Drop cached images no deck in `manifest` lists any more."""
    if manifest_needs_newer_addon(manifest, SUPPORTED_MANIFEST_SCHEMA):
        return
    keep = set()
    for d in manifest.get("decks") or ():
        media = d.get("media") if isinstance(d, dict) else None
        if isinstance(media, dict):
            keep.update(e["sha256"] for e in media.values()
                        if isinstance(e, dict) and isinstance(e.get("sha256"), str))
    try:
        names = os.listdir(MEDIA_CACHE)
    except OSError:
        return
    for name in names:
        # Only finished files: a .part may be another fetch's download in progress.
        if name not in keep and re.fullmatch(r"[0-9a-f]{64}", name):
            try:
                os.remove(os.path.join(MEDIA_CACHE, name))
            except OSError:
                pass


def _with_media(d, apkg, scratch_key, read, part=None, media_dir=None):
    """`apkg` with the deck's listed images put back in, as a scratch file; `apkg`
    itself when the deck lists none."""
    entries = validate_media_entries(d.get("media"))
    if not entries:
        return apkg
    on_file = part and (lambda n, of: part(n, of, f"Downloading images: {n} of {of}"))
    files = _ensure_media(entries, read, on_file, media_dir)
    fd, tmp = tempfile.mkstemp(dir=_scratch(), suffix=".part")
    os.close(fd)
    try:
        inject_apkg_media(apkg, tmp, files)
        path = _scratch_path(scratch_key + "+media", d.get("version"))
        replace_file(tmp, path)
        return path
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _fetch_manifest(cfg, timeout=_CONNECT_TIMEOUT, download_timeout=_DOWNLOAD_TIMEOUT):
    """Return (manifest, fetch_apkg, source_label) where fetch_apkg(deck, on_chunk=None)
    returns a local .apkg path.

    A GitHub source needs only the repo; the token is optional. `timeout` bounds the
    manifest fetch; `download_timeout` bounds each deck download. The unattended poll
    overrides the latter when it runs downloads inline on the main thread (see
    background._auto_sync_check).

    `on_chunk` is net._http_get's, passed through, so an interactive caller can pass
    `cancellable_progress`'s `step.pump` and have Cancel work during a download. The
    local-folder source ignores it.

    (None, None, None) means nothing is configured, and only that. A configured source
    that can't be loaded raises with a message naming the problem.
    """
    if cfg["gh_repo"]:
        raw = _gh_raw(cfg["gh_repo"], "manifest.json", cfg["gh_token"], cfg["gh_ref"],
                      timeout=timeout, max_bytes=_MANIFEST_MAX_BYTES)
        try:
            manifest = json.loads(raw)
        except Exception as e:
            # Reachable but not JSON: say so in the words the local-folder branch uses.
            raise RuntimeError(f"the manifest.json in {cfg['gh_repo']} isn't valid "
                               f"JSON ({e})") from e
        if not manifest:
            raise RuntimeError(f"the manifest.json in {cfg['gh_repo']} is empty")
        _require_manifest_object(manifest, cfg["gh_repo"])

        def fetch(d, on_chunk=None, max_bytes=APKG_MAX_BYTES, with_media=True, apkg=None):
            validate_media_entries(d.get("media"))
            path = safe_source_path(d["apkg"])
            key = f"{cfg['gh_repo']}@{cfg['gh_ref']}/{path}"
            if apkg is None:
                data = _gh_raw(cfg["gh_repo"], path, cfg["gh_token"], cfg["gh_ref"],
                               timeout=download_timeout, on_chunk=on_chunk,
                               max_bytes=max_bytes)
                apkg = _write_scratch(key, data, d.get("version"))

            def read(p):
                return _gh_raw(cfg["gh_repo"], safe_source_path(p), cfg["gh_token"],
                               cfg["gh_ref"], timeout=download_timeout,
                               on_chunk=on_chunk, max_bytes=MEDIA_FILE_MAX_BYTES)

            if not with_media:
                return apkg
            return _with_media(d, apkg, key, read, getattr(on_chunk, "part", None),
                               getattr(fetch, "media_dir", None))

        fetch.source_key = ("github", cfg["gh_repo"], cfg["gh_ref"])
        _prune_media_cache(manifest)
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
        path = _local_source_file(folder, "manifest.json")
        if os.path.getsize(path) > _MANIFEST_MAX_BYTES:
            raise RuntimeError(f"the manifest.json in {folder} is larger than "
                               f"{_MANIFEST_MAX_BYTES // (1024 * 1024)} MB, so it "
                               "wasn't read")
        try:
            manifest = _load_json(path, None, strict=True)
        except Exception as e:
            raise RuntimeError(f"the manifest.json in {folder} isn't valid JSON "
                               f"({e})") from e
        if not manifest:
            raise RuntimeError(f"the manifest.json in {folder} is empty")
        _require_manifest_object(manifest, folder)

        def fetch(d, on_chunk=None, max_bytes=None, with_media=True, apkg=None):
            validate_media_entries(d.get("media"))
            apkg = apkg or _local_source_file(folder, d["apkg"])

            def read(p):
                local = _local_source_file(folder, p)
                if os.path.getsize(local) > MEDIA_FILE_MAX_BYTES:
                    raise RuntimeError(f"the image {p} in {folder} is too large")
                with open(local, "rb") as fh:
                    return fh.read()

            if not with_media:
                return apkg
            return _with_media(d, apkg, f"{os.path.realpath(folder)}/{d['apkg']}", read,
                               getattr(on_chunk, "part", None),
                               getattr(fetch, "media_dir", None))

        fetch.source_key = ("folder", os.path.realpath(folder))
        _prune_media_cache(manifest)
        return manifest, fetch, "local folder"

    return None, None, None


def local_manifest(cfg):
    """The manifest of a local-folder source, read without fetching anything, or None
    for a GitHub source (it keeps no copy on disk) or a folder that can't be read."""
    if cfg["gh_repo"] or not cfg["decks_dir"]:
        return None
    try:
        return _fetch_manifest(cfg)[0]
    except Exception:
        return None


def _fetch_manifest_gated(cfg):
    """_fetch_manifest plus the "you need a newer add-on" schema gate and the
    unreachable/unconfigured warnings. Returns (manifest, fetch, source), or None after
    showing the user a warning; the caller should just return.
    """
    try:
        manifest, fetch, source = run_with_progress(
            "Checking the deck source", lambda: _fetch_manifest(cfg))
    except ProgressCancelled:
        return
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
            "Update the add-on first (<b>Intern Pearls → Advanced → Check for add-on "
            "updates</b>), then try again. Syncing against a manifest format this "
            "version doesn't understand is refused rather than attempted, so nothing "
            "here has been touched."
        )
        return None
    # _fetch_manifest ran off the main thread, so the collection is read here.
    fetch.media_dir = _collection_media_dir()
    return manifest, fetch, source


def _collection_media_dir():
    try:
        return mw.col.media.dir()
    except Exception:
        return None


def _check_deck_skill(cfg, manifest, fetch):
    """Fetch a deck source's card-authoring skill (manifest["skill"] = {"path",
    "version"}) and gate it behind explicit consent: show the full text and ask, since
    it goes on to drive an agent CLI with web access.

    No "skill" entry is a no-op. `fetch` is the per-path callable used for deck .apkg
    files, called with a fake {"apkg": path}. A fetch failure never blocks a deck sync,
    so it is swallowed.

    Consent is keyed by content hash: an unchanged hash is silent, any other re-asks
    with the full new text. load_deck_skill reads a stored consent clean of hidden
    characters and with its version tamed. Declining leaves whatever was previously
    consented to as it was.
    """
    entry = manifest.get("skill") if isinstance(manifest, dict) else None
    if not isinstance(entry, dict) or not entry.get("path"):
        return
    try:
        local = fetch({"apkg": entry["path"], "version": entry.get("version")},
                      max_bytes=_SKILL_MAX_BYTES)
        if os.path.getsize(local) > _SKILL_MAX_BYTES:
            return
        with open(local, "rb") as fh:
            raw = fh.read()
    except Exception:
        return
    digest = hashlib.sha256(raw).hexdigest()
    stored = load_deck_skill()
    if stored and stored.get("hash") == digest:
        return
    text = raw.decode("utf-8-sig", "replace")
    version = skill_version(entry.get("version"))
    if _ask_scrollable(_skill_consent_html(text, version, bool(stored)),
                       yes_label="Use this skill", no_label="Not now"):
        save_deck_skill({"text": strip_hidden(text), "version": version,
                         "hash": digest,
                         "consented_on": datetime.date.today().isoformat(),
                         "enabled": True})


def _skill_consent_html(text, version, updated):
    """The deck-skill consent body: what the skill is for, what the AI can reach while
    following it, and the full text, every part from the source escaped. Hidden
    characters are flagged and written out, and they are removed before the skill is
    stored, so the AI only ever reads what was shown here."""
    verb = "updated its" if updated else "added a"
    version_note = f" (version {html.escape(version)})" if version else ""
    shown, hidden = reveal_hidden(text)
    warning = ""
    if hidden:
        warning = (f"<b>It contains {plural(hidden, 'hidden character')}</b>, which "
                   "can carry instructions you can't see. Each is written out below "
                   "as [U+...] and is removed before the AI reads the skill.<br><br>")
    return (f"Your deck source has {verb} card-authoring skill{version_note}. It adds "
            "instructions the AI follows when it drafts and checks cards for these "
            "decks. The AI can search the web while it works: Quick drafts look for "
            "card images, and Thorough drafts and Check facts look things up to "
            "verify them.<br><br>" + warning +
            "Read the full text before allowing it:<br><br>"
            + html.escape(shown).replace("\n", "<br>"))


@_safe
@_manual_flow
def sync_decks():
    cfg = _cfg()
    fetched = _fetch_manifest_gated(cfg)
    if not fetched:
        return
    manifest, fetch, source = fetched
    _check_deck_skill(cfg, manifest, fetch)

    installed = installed_matching_collection(_load_json(INSTALLED, {}), cfg["scope_tag"])
    todo = decks_to_update(manifest, installed, cfg["excluded"])
    if not todo:
        _info(f"All selected decks are up to date (source: {source}).")
        return

    def _deck_row(d):
        """One deck's row: the deck as the primary text, its size as the trailing
        column, and a chip: NEW for a deck the learner has none of, UPDATED for one
        already in the collection.

        The count is the manifest's total for the deck, worded as a deck total ("6 cards
        in deck"), since Sync decks downloads nothing before this confirmation.
        """
        short = d["name"].split("::")[-1]
        cards = d.get("cards")
        kind = "changed" if d["name"] in installed else "new"
        return ("row", kind, _text(short),
                f"{plural(cards, 'card')} in deck" if cards is not None else "")

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

    # A cancellable, determinate progress window: the fetches run on the main thread
    # here. step.pump goes to the fetch so Cancel works during a download too.
    starts, total = card_progress(todo)
    with cancellable_progress("Syncing decks", total) as step:
        results, restored, tpl_changes, _, cancelled, collisions, converted = _run_sync(
            cfg, manifest, lambda d: fetch(d, on_chunk=step.pump), todo,
            on_progress=lambda i, n, name: step(starts[i - 1] + 1,
                                                f"Syncing {name} ({i} of {n})",
                                                span=_share(starts, total, i)))
    _offer_template_changes(tpl_changes)
    backup_line = (
        "A pre-sync backup of the Intern Pearls deck was saved; use "
        "<i>Advanced → Restore intern pearls deck</i> to revert to it if needed."
        if backed_up else
        "No pre-sync backup was taken this time (nothing to back up yet, or it "
        "failed and you chose to continue).")
    title = "Sync stopped early" if cancelled else "Sync complete"
    # Each deck's outcome is a row; run-wide notes are paragraphs between them.
    items = []
    append_rows(items, [("row", None, line, "") for line in results])
    if restored:
        items.append(("note", f"Kept your own edits on {plural(restored, 'card')}."))
    items += _converted_items(converted)
    items += _collision_items(collisions)
    items.append(("note", backup_line))
    if cancelled:
        items.append(("note", "Nothing else was touched; run <b>Sync decks</b> again "
                              "anytime to pick up where this left off."))
    show_result(f"{title} (source: {source})", items)


def _converted_items(converted):
    """What a consented note-type conversion did, for the end-of-run summary, named as
    moved cards because that is what the question asked about.
    """
    if not converted:
        return []
    return [("note", f"Moved {plural(converted, 'card')} to the new format, review "
                     "history kept. Your next AnkiWeb sync is the one-time full sync "
                     "this was asked about: choose \"Upload to AnkiWeb\".")]


def _hand_edits(cfg, per_note, existing_fields, shipped):
    """find_learner_edits over the cards not archived, with the rollback guard."""
    archived = _archived_guids(cfg["scope_tag"])
    live = {g: f for g, f in existing_fields.items() if g not in archived}
    return find_learner_edits(shipped, live, cfg["protected"], per_note, _note_mods(live))


def _learner_edits(cfg, manifest, existing_fields):
    """The learner's hand edits as digest rows, read before anything imports, so an edit
    the update then overwrites still reaches the digest."""
    shipped = _load_shipped()
    found = _hand_edits(cfg, manifest.get("note_protected_fields", {}), existing_fields,
                        shipped)
    if not found:
        return []
    decks = _existing_guid_to_deck(cfg["scope_tag"])
    return [{"deck": decks.get(g, ""), "guid": g, "source": shipped[g].get(HASH_KEY, g),
             "field": f, "shipped": h, "value": v,
             "front": note_display_label(list(existing_fields[g].values()))}
            for g, f, h, v in found]


def _record_current_hashes(cfg, manifest, fetch, installed, todo, reg):
    """Record the text each up-to-date deck ships for cards with no record yet, so a hand
    edit made before hashes were kept is found by the next update. Nothing is imported,
    and no mod is stamped: the next real import does that, and a stamp now would read
    every earlier edit as a rolled-back note."""
    scope, declined = cfg["scope_tag"], declined_guids(reg)
    shipped, archived = _load_shipped(), _archived_guids(scope)
    candidates = {g: deck for g, deck in _existing_guid_to_deck(scope).items()
                  if g not in archived and g not in declined
                  and not any(k.startswith(HASH_KEY) for k in shipped.get(g, {}))}
    pending = {t["name"] for t in todo}
    done = _load_json(RECORDED_TEXT, {})
    done = done if isinstance(done, dict) else {}
    decks = []
    for d in manifest.get("decks", []):
        if not isinstance(d, dict):
            continue
        name = d.get("name")
        mine = {g for g, deck in candidates.items() if deck and name
                and (deck == name or deck.startswith(f"{name}::"))}
        if (mine and name not in pending and installed.get(name) == d.get("version")
                and done.get(name) != d.get("version")
                and not _deck_opted_out(name, cfg["excluded"])):
            decks.append((d, mine))
    if not decks:
        return
    updated, cancelled = {}, False
    fronts = _existing_front_to_guid(scope)
    starts, total = card_progress([d for d, _ in decks])
    with cancellable_progress("Recording your decks' current text", total) as step:
        for i, (d, mine) in enumerate(decks, 1):
            if not step(starts[i - 1] + 1,
                        f"Recording {d['name'].split('::')[-1]} ({i} of {len(decks)})"):
                cancelled = True
                break
            try:
                src = _cached_fetch(fetch, d, on_chunk=step.pump, with_media=False)
                *_, matched = remap_cards(src, fronts, manifest.get("front_aliases", {}))
                hashes = shipped_field_hashes(apkg_note_details(src), matched,
                                              skip=declined)
            except DownloadCancelled:
                cancelled = True
                break
            except Exception:
                continue    # this deck is recorded on a later run
            hashes = hashes_unless_renamed(
                {g: h for g, h in hashes.items() if g in mine}, _stored_fields(mine))
            updated.update({g: {**shipped.get(g, {}), **h} for g, h in hashes.items()})
            done[d["name"]] = d.get("version")
    if updated:
        _save_json(SHIPPED, {**_load_shipped(), **updated})
    _save_json(RECORDED_TEXT, done)
    return cancelled


def _adopted_items(edits):
    """Summary rows for edits this update shipped back as the deck's own text, capped
    like _collision_items. Drops them from `edits` in place: nothing is left to send."""
    shipped = _load_shipped()
    adopted = [e for e in edits if shipped.get(e["guid"], {}).get(HASH_KEY + e["field"])
               == note_fields_hash([e["value"]])]
    if not adopted:
        return []
    edits[:] = [e for e in edits if e not in adopted]
    cards = {}
    for e in adopted:
        cards.setdefault((e["guid"], e["front"]), []).append(e["field"])
    rows = [f"{_text(truncate(front, 70))} ({_text(', '.join(fields))})"
            for (_g, front), fields in list(cards.items())[:10]]
    if len(cards) > 10:
        rows.append(f"<i>and {len(cards) - 10} more</i>")
    items = [("note", "The deck now uses your wording on "
                      f"<b>{plural(len(cards), 'card')}</b> you had edited:")]
    append_rows(items, [("row", None, r, "") for r in rows])
    return items


def _collision_items(collisions):
    """The cards where the learner's edit and a source update landed on the same field:
    the explanation, then one row per card. The learner's version is kept.

    Capped at ten, with the remainder counted on a row of its own: naming a card means
    reading its note out of the collection.

    Each card is named through note_display_label, since a raw first field is HTML.
    """
    if not collisions:
        return []
    fronts = []
    for guid, field in collisions[:10]:
        nid = mw.col.db.scalar("select id from notes where guid = ?", guid)
        if nid:
            label = _card_html(mw.col.get_note(nid).fields, max_len=70)
            fronts.append(f"{label} ({_text(field)})")
    if len(collisions) > len(fronts):
        fronts.append(f"<i>and {len(collisions) - len(fronts)} more</i>")
    items = [("note",
              f"On <b>{plural(len(collisions), 'card')}</b>, this update changed a "
              "field you had also written in yourself. <b>Your version was kept</b> "
              "and the deck's change to that field was skipped. If the field is one "
              "you edited by hand, the next update of that deck shows the deck's "
              "version on the card's row and you can take it there. A preserved "
              "field always keeps yours.")]
    append_rows(items, [("row", None, front, "") for front in fronts])
    return items


_FORMAT_CHANGE = ("(a question and answer became a fill-in-the-blank, or the other way "
                  "round)")


def _kept_beside_text(beside, of_them):
    """What happens to notes whose conversion would delete cards: nothing asked, so
    this is said wherever the conversion question is."""
    one = len(beside) == 1
    who = (f"<b>{plural(len(beside), 'card')}</b> of them" if of_them
           else "This card" if one else "These cards")
    return (f"{who} would lose cards if moved (a fill-in-the-blank with several blanks "
            "becomes a single card), and their review history with them. So "
            f"{'it stays as it is' if one else 'they stay as they are'}, as a local copy "
            "that no longer receives updates, and the new version is added beside "
            f"{'it' if one else 'them'}.")


def _conversion_question(convert, beside, decline_text):
    """The consent question for `convert`, naming `beside` too when there are any."""
    total = len(convert) + len(beside)
    kept = f"{_kept_beside_text(beside, True)}<br><br>" if beside else ""
    which = ("your existing cards" if not beside
             else "the other one" if len(convert) == 1 else f"the other {len(convert)}")
    return (f"<b>{plural(total, 'card')}</b> in this update changed format "
            f"{_FORMAT_CHANGE}.<br><br>{kept}Move {which} to the new format? They keep "
            "all their cards and review history. Anki treats this as a schema change, so "
            "your next AnkiWeb sync will be a one-time full sync, choose \"Upload to "
            f"AnkiWeb\" when asked.<br><br>{decline_text}")


def _kept_beside_notice(beside):
    return (f"<b>{plural(len(beside), 'card')}</b> in this update changed format "
            f"{_FORMAT_CHANGE}.<br><br>{_kept_beside_text(beside, False)}")


def _offer_notetype_changes(changes, protected=None, on_import_as_new=None, per_note=None):
    """Ask before converting the learner's notes to the note type an update ships.

    Accepting keeps every card with its history at the cost of a one-time full AnkiWeb
    sync. Declining imports the cards as new notes beside the existing ones. A note
    whose conversion would delete a card is never converted: it goes to
    `on_import_as_new` whatever the answer.
    """
    if not changes:
        return []
    convert, beside = split_notetype_changes(changes)
    if not convert:
        _info(_kept_beside_notice(beside))
        accepted = False
    else:
        accepted = _ask(
            _conversion_question(
                convert, beside,
                "Choosing to import them as new makes your existing cards local copies: "
                "they keep their current format, fields, note IDs, and review history, "
                "but stop receiving updates from this source. The new-format cards "
                "import separately and receive future source updates."),
            yes_label="Move my cards across", no_label="Import them as new")
    if not accepted:
        if on_import_as_new is not None:
            on_import_as_new(changes)
        return []
    if protected is not None:
        _check_protected_conversion(convert, protected, per_note)
    if beside and on_import_as_new is not None:
        on_import_as_new(beside)
    return change_note_types(convert)


def _planned_notetype_row_ids(src, existing_fronts, aliases, changes):
    """Package row ids whose matched local notes are in a conversion plan."""
    planned_guids = {change["guid"] for change in changes}
    remap, _kept, _new, _new_notes, _matched = remap_cards(src, existing_fronts, aliases)
    return {rid for rid, _fields, guid in apkg_notes(src)
            if remap.get(rid, guid) in planned_guids}


def _fork_import_as_new(changes, scope_tag):
    """Fork local identities; the caller commits snapshot removal after import."""
    forked_guids = fork_note_identities(changes)
    return _existing_front_to_guid(scope_tag), forked_guids


def _load_shipped():
    """shipped_fields.json as {guid: {field: text}}. A malformed entry reads as no
    baseline, which keeps the learner's value; an unparseable file raises
    CorruptStateError."""
    raw = _load_json(SHIPPED, {})
    if not isinstance(raw, dict):
        return {}
    return {guid: {f: v for f, v in entry.items() if isinstance(f, str) and isinstance(v, str)}
            for guid, entry in raw.items() if isinstance(entry, dict)}


UPDATE_UNDO_NAME = "Intern Pearls deck update"
RESTORE_UNDO_NAME = "Intern Pearls deck restore"
# (collection ref, database ref, counter) -> (deck name, prior installed version,
# {guid: prior baseline}, applied version).
_update_undo_steps = {}
# Same key -> ({deck: (present, prior version)}, {guid: (present, prior baseline)}).
_restore_undo_steps = {}
# (collection key, deck, version) updates the learner undid this session: auto-sync
# leaves them for a manual Update my decks rather than applying them again unasked.
# Keyed per collection so an undo in one profile never holds back another.
undone_updates = set()


def clear_undo_steps():
    _update_undo_steps.clear()
    _restore_undo_steps.clear()


def _undo_key(counter):
    col = mw.col
    db = getattr(col, "db", None)
    if col is None or db is None:
        clear_undo_steps()
        return None
    # Reopening the same Collection replaces its database and restarts its counters.
    for steps in (_update_undo_steps, _restore_undo_steps):
        for key in list(steps):
            if key[0]() is not col or key[1]() is not db:
                steps.pop(key)
    return weakref.ref(col), weakref.ref(db), counter


IMPORT_UNDO_NAME = "Intern Pearls deck import"


def restore_pending_after_undo(changes):
    """Put installed versions and shipped baselines back after an update or restore."""
    try:
        key = _undo_key(getattr(changes, "counter", None))
        if getattr(changes, "operation", None) in (RESTORE_UNDO_NAME, IMPORT_UNDO_NAME):
            step = _restore_undo_steps.pop(key, None)
            if step is None:
                return
            for path, prior in zip((INSTALLED, SHIPPED), step):
                current = _load_json(path, {})
                current = current if isinstance(current, dict) else {}
                for key, (present, value) in prior.items():
                    if present:
                        current[key] = value
                    else:
                        current.pop(key, None)
                _save_json(path, current)
            return
        if getattr(changes, "operation", None) != UPDATE_UNDO_NAME:
            return
        step = _update_undo_steps.pop(key, None)
        if step is None:
            return
        name, prior_version, prior_shipped, undone_version = step
        undone_updates.add((collection_key(), name, undone_version))
        installed = _load_json(INSTALLED, {})
        installed = installed if isinstance(installed, dict) else {}
        if prior_version is None:
            installed.pop(name, None)
        else:
            installed[name] = prior_version
        _save_json(INSTALLED, installed)
        shipped = _load_shipped()
        for guid, entry in prior_shipped.items():
            if entry is None:
                shipped.pop(guid, None)
            else:
                shipped[guid] = entry
        _save_json(SHIPPED, shipped)
    except Exception:
        # Anki drops a hook that raises; the cost of a miss is a deck not re-offered.
        pass


def _package_hashes(src, declined, expected_conflicts, package_matched, wrote):
    """Per-field shipped hashes for one package's notes. A note the import wrote is
    hashed as the collection stored it, so call this right after `_apply_deck`."""
    details = apkg_note_details(src)
    # Declined notes and held-back note-type conflicts never reached the
    # collection, so they keep the hashes of what the learner actually holds.
    unwritten = set(declined) | {d["guid"] for d in details
                                 if d["rid"] in expected_conflicts}
    hashes = shipped_field_hashes(details, package_matched, unwritten)
    return stored_field_hashes(hashes, _stored_fields(wrote) if wrote else {}, wrote)


def _run_sync(cfg, manifest, fetch, todo, on_progress=None,
              defer_template_changes=False, convert_notetypes=None,
              undisclosed_conversions=(), take_deck=frozenset()):
    """Apply every deck in `todo`: fix note types, snapshot protected fields and tags,
    remap and import each deck (keeping the learner's scheduling) and restore the
    snapshot onto it in the same undo step, then persist the new installed versions.

    The caller must already have confirmed (if interactive) and taken a backup.

    Returns (results, restored, tpl_changes, deferred, cancelled, collisions,
    converted): per-deck outcome lines, the note-restore count, template/CSS changes
    detected in the imported decks (imports never propagate them, see _import_apkg), the
    names of decks skipped because of such a change, whether `on_progress` asked to
    stop, the fields where the learner's edit met an update (see _restore), and how many
    notes were converted to a new note type.

    `on_progress(i, total, deck_short_name)`, if given, fires before each deck is
    fetched and applied and must return a truthy value to continue. A falsy return stops
    before that deck's fetch/import, never partway through one, so the decks already
    completed are fully applied and their versions persisted.

    `defer_template_changes` is the unattended-caller policy: applying a template bumps
    the collection schema (a one-time full AnkiWeb sync), which needs consent. With
    True, a deck whose update includes a template change is left un-imported and not
    marked installed, so it stays pending for the next interactive sync.

    `convert_notetypes` is who owns the note-type-conversion decision. None (Sync decks)
    means ask per deck, here, via _offer_notetype_changes. True or False means the
    caller already asked once for the whole run and this loop must not ask again. False
    converts nothing.

    `undisclosed_conversions` names the decks whose conversions that one question could
    not have covered, because their package could not be read when it was asked. Such a
    deck is held back rather than imported with its conversion declined: it stays
    pending for the next run's preview.
    """
    aliases = manifest.get("front_aliases", {})   # from the (private) manifest, not config
    per_note = manifest.get("note_protected_fields", {})
    _ensure_notetypes()
    baseline = _load_shipped()
    # Hand edits are kept like preserved fields for this import only; the declared
    # per_note alone still feeds _capture_shipped, so they never gain a text baseline.
    before = _existing_guid_to_fields(cfg["scope_tag"])
    edited = [e for e in _hand_edits(cfg, per_note, before, baseline)
              if (e[0], e[1]) not in take_deck]
    snap = _snapshot(cfg["protected"], cfg["scope_tag"],
                     with_edits_protected(per_note, ((g, f) for g, f, _h, _v in edited)))
    tag_snap = _snapshot_tags(cfg["scope_tag"])
    prior_installed = _load_json(INSTALLED, {})
    prior_installed = prior_installed if isinstance(prior_installed, dict) else {}
    existing_fronts = _existing_front_to_guid(cfg["scope_tag"])
    reg = load_declined()
    declined = declined_guids(reg)
    seen = {}
    results, tpl_changes, deferred = [], {}, []
    shipped, restored, collisions = {}, 0, []
    # This run's per-deck versions, the only thing written back: the save below merges
    # them into whatever installed.json holds at that moment.
    applied = {}
    converted = 0
    cancelled = False
    note_types = _NoteTypeCache(cfg["scope_tag"])
    for i, d in enumerate(todo, 1):
        conversion_undo = None
        forked_guids = set()
        short = d["name"].split("::")[-1]
        name = _text(short)
        try:
            if on_progress and not on_progress(i, len(todo), short):
                cancelled = True
                break
            src = fetch(d)
            # note_protected_fields is keyed by the guid the deck source assigns, but a
            # note matched by front keeps the learner's own guid. Re-key this deck's
            # declarations onto that guid before anything below reads per_note. Skipped
            # when nothing is declared, to save an apkg read.
            if per_note:
                _remap, _in_place, _as_new, _new_notes, matched = remap_cards(
                    src, existing_fronts, aliases)
                deck_resolved = per_note_for_package(per_note, matched)
                per_note = {**per_note, **deck_resolved}
                for guid, fields in deck_resolved.items():
                    top_up = _snapshot_fields(guid, fields)
                    if top_up:
                        # Merge, not replace: this guid may already hold a snapshot
                        # value from the global protected_fields list.
                        snap.setdefault(guid, {}).update(top_up)
            tpl = _template_changes(src)
            # A note-type conversion bumps the schema like a template change, so it
            # needs consent and is deferred when unattended. `declined` keeps a card the
            # learner turned away out of the plan: its content is dropped from the
            # import below.
            nt = notetype_changes(src, existing_fronts, aliases, cfg["scope_tag"],
                                  declined, note_types.read())
            if (tpl or nt) and defer_template_changes:
                deferred.append(d["name"])
                # Name what is held back: a template change, a note-type conversion, or
                # both.
                what = " and ".join(x for x in ("card-template" if tpl else None,
                                                "note-type format" if nt else None) if x)
                results.append(f"• <b>{name}</b>: includes a {what} update, "
                               "waiting for a manual Sync decks")
                continue
            if nt and d["name"] in undisclosed_conversions:
                # The run's one conversion question was asked before this deck could be
                # read. Hold the deck back rather than decline for the learner; the next
                # run asks up front.
                deferred.append(d["name"])
                results.append(f"• <b>{name}</b>: includes a note-type format update "
                               "this run couldn't check in time, waiting for the next "
                               "update")
                continue
            tpl_changes.update(tpl)
            # A target note type this collection doesn't hold yet can't be converted
            # onto, and the import that creates it runs after this. The deck imports but
            # its version is not recorded below, so the next run finds it pending and
            # converts.
            missing = ([] if convert_notetypes is False
                       else missing_notetype_targets(nt))
            conversion_undo = mw.col.add_custom_undo_entry(UPDATE_UNDO_NAME)
            # Before the import: once the note is on the right type, the import matches
            # it by GUID and updates it in place.
            forked = []
            if missing:
                changed_nids = []
            elif convert_notetypes is None:
                changed_nids = _offer_notetype_changes(
                    nt, cfg["protected"],
                    on_import_as_new=lambda changes: forked.extend(changes),
                    per_note=per_note)
            else:
                # A conversion that would delete a card is kept beside even when accepted.
                to_convert, forked = (split_notetype_changes(nt) if convert_notetypes
                                      else ([], nt))
                if to_convert:
                    _check_protected_conversion(to_convert, cfg["protected"], per_note)
                changed_nids = change_note_types(to_convert)
            if forked:
                # Only the learner's note's guid changes: its fields and tags stay as
                # they are, already restored if an earlier deck in this run imported it.
                existing_fronts, forked_guids = _fork_import_as_new(
                    forked, cfg["scope_tag"])
            package_matched = []
            expected_conflicts = (
                _planned_notetype_row_ids(src, existing_fronts, aliases, nt)
                if missing else frozenset())
            started = int(time.time())
            in_place, as_new, wrote = _apply_deck(
                src, aliases, existing_fronts, declined,
                expected_conflicting_rids=expected_conflicts,
                allow_field_additions=not defer_template_changes,
                matched_out=package_matched)
            for guid in forked_guids:
                snap.pop(guid, None)
                tag_snap.pop(guid, None)
            # Inside this deck's undo step, before the next deck is fetched, so a
            # failure here rolls the import back. The shipped values are read before the
            # restore puts the learner's own back over them.
            hashes = _package_hashes(src, declined, expected_conflicts, package_matched,
                                    wrote)
            deck_shipped = with_field_hashes(
                _capture_shipped(cfg["protected"], cfg["scope_tag"], wrote, per_note),
                baseline, hashes, wrote)
            deck_restored, deck_collisions = _restore(snap, baseline, wrote)
            # A clash keeps the hash the learner last took, so later updates still
            # see the deck's change and offer it (find_kept_edits).
            for guid, f in deck_collisions:
                prior = baseline.get(guid, {}).get(HASH_KEY + f)
                if prior is not None and guid in deck_shipped:
                    deck_shipped[guid][HASH_KEY + f] = prior
            _restore_tags(tag_snap, cfg["scope_tag"], wrote)
            # After the import: the extra cloze cards exist only once the cloze markup
            # has landed.
            seed_converted_siblings(changed_nids)
            # Anki keeps the package's own mod on an updated note, so anchor a note
            # whose text this import changed on its start. One it rewrote identically
            # keeps its mod, or an edit synced in later would read as a rollback.
            changed = changed_since(hashes, before)
            for guid, mod in _note_mods(hashes).items():
                deck_shipped[guid][MOD_KEY] = str(max(mod, started) if guid in changed
                                                  else mod)
            mw.col.merge_undo_entries(conversion_undo)
            _update_undo_steps[_undo_key(conversion_undo)] = (
                d["name"], prior_installed.get(d["name"]),
                {guid: baseline.get(guid) for guid in deck_shipped}, d.get("version"))
            conversion_undo = None
            shipped.update(deck_shipped)
            # Saved per deck, merged into what is on disk: a crash before the next deck
            # must not leave this one's text looking like the learner's own edits.
            _save_json(SHIPPED, {**_load_shipped(), **deck_shipped})
            restored += deck_restored
            collisions.extend(deck_collisions)
            converted += len(changed_nids)
            existing_fronts = _existing_front_to_guid(cfg["scope_tag"])
            seen[d["name"]] = ({g for _, _f, g in apkg_notes(src)}
                               | {existing for _r, _g, existing in package_matched})
            if missing:
                # Worded as a routine second pass, not a fault.
                results.append(
                    f"✓ <b>{name}</b>: {in_place} kept history, {as_new} new. "
                    "One more pass needed: your collection had no "
                    f"{_text(', '.join(missing))} note type until this import added "
                    "it, so "
                    "this deck stays pending. Run <b>Update my decks</b> again to "
                    "finish moving your existing cards to the new format.")
                continue
            applied[d["name"]] = d["version"]
            _save_json(INSTALLED, {**_load_json(INSTALLED, {}), d["name"]: d["version"]})
            results.append(f"✓ <b>{name}</b>: {in_place} kept history, {as_new} new")
        except DownloadCancelled:
            # Cancel during this deck's download: nothing of it was imported. Treated
            # like a Cancel between decks, not as a failed deck.
            cancelled = True
            break
        except Exception as e:
            if conversion_undo is not None:
                # Conversion and package import are undoable backend operations.
                # Roll back this deck's group, never an unrelated prior action.
                mw.col.merge_undo_entries(conversion_undo)
                mw.col.undo()
                existing_fronts = _existing_front_to_guid(cfg["scope_tag"])
            if isinstance(e, NoteTypeFieldsRequired):
                deferred.append(d["name"])
                results.append(f"• <b>{name}</b>: {_text(e)}")
            else:
                results.append(f"✗ <b>{name}</b>: {_text(e)}")
    # Merged into what is on disk, so a version another sync recorded meanwhile stays.
    _save_json(INSTALLED, {**_load_json(INSTALLED, {}), **applied})
    # Registry housekeeping runs last, inside its own guard, so a hand-edited registry
    # the per-entry hardening can't cover never fails a run whose decks already applied.
    try:
        retired_guids = {g for per_deck in (manifest.get("retired") or {}).values()
                         for g in per_deck}
        if prune_declined(reg, retired_guids, seen,
                          {d["name"] for d in manifest.get("decks", [])}):
            save_declined(reg)
    except Exception:
        pass
    mw.reset()
    return results, restored, tpl_changes, deferred, cancelled, collisions, converted


def _offer_template_changes(tpl_changes):
    """Interactive follow-up to a sync that found template/CSS changes: explain the
    one-time full-sync consequence and apply only on yes. Declining keeps the current
    card look; the content already imported, and the next template change offers again.
    Returns the note types changed.
    """
    if not tpl_changes:
        return []
    names = ", ".join(f"<b>{_text(n)}</b>" for n in sorted(tpl_changes))
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


def _retry_failed_downloads(fetch, todo, downloaded):
    """Download the decks whose preview download failed, before this run asks anything.

    `downloaded` is updated in place, so the apply step reuses these files. Returns
    whether the reader clicked Cancel; nothing has been backed up or imported at this
    point.

    It runs here so a note-type conversion in such a deck is planned and asked about
    before the apply loop, which has nowhere to ask. A look change in one is handled
    after the run (see _apply_consented_look).

    A deck that fails again is left as it was: the apply loop fetches it once more and
    reports a second failure per deck. If that attempt succeeds and carries a
    conversion, _run_sync's `undisclosed_conversions` holds the deck back.
    """
    missing = [d for d in todo if not _is_local(downloaded.get(d["name"]))]
    if not missing:
        return False
    starts, total = card_progress(missing)
    with cancellable_progress("Downloading decks", total) as step:
        for i, d in enumerate(missing, 1):
            short = d["name"].split("::")[-1]
            if not step(starts[i - 1] + 1, f"Downloading {short} ({i} of {len(missing)})",
                        span=_share(starts, total, i)):
                return True
            try:
                downloaded[d["name"]] = _cached_fetch(fetch, d, on_chunk=step.pump)
            except DownloadCancelled:
                return True
            except Exception:
                pass
    return False


def _final_conversion_plan(todo, downloaded, existing_fronts, aliases, scope_tag,
                           declined):
    """{deck name: the note-type conversions in it} for every deck of `todo` that is on
    disk, planned against the declines as they stand once the learner has decided.
    A deck that can't be read is left out."""
    plan = {}
    note_types = _NoteTypeCache(scope_tag)
    for d in todo:
        src = downloaded.get(d["name"])
        if not _is_local(src):
            continue
        try:
            plan[d["name"]] = notetype_changes(src, existing_fronts, aliases, scope_tag,
                                               declined, note_types.read())
        except Exception:
            pass
    return plan


def _apply_consented_look(tpl_changes, tpl_choice, disclosed):
    """Apply the look change the reader agreed to, and ask about any they never saw.

    `tpl_changes` is what the import found, `disclosed` is what the confirmation named
    beside its checkbox, and the intersection is what the tick covers. A note type
    outside it came from a deck whose preview download failed; it is asked about here
    through _offer_template_changes, since the deck is now recorded as installed and
    would not offer it again. Returns the note types changed.
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
    Matched by prefix: an excluded name is the manifest's, and the card is often in a
    subdeck of it.
    """
    return bool(deck) and any(deck == x or deck.startswith(x + "::") for x in excluded)


def _live_cards_loader(manifest, fetch=None, local=None):
    """A memoized zero-argument callable returning (guids, fronts) of every card in the
    manifest's packages, read only when something is about to be archived.

    `local` is {deck name: path} for packages already on disk; anything else is fetched
    through `fetch` (through the session cache) under a cancellable progress window, or
    skipped when there is none, so the unattended poll never downloads. A package that can't be read
    contributes nothing.
    """
    cache = []

    def load():
        if not cache:
            guids, fronts = set(), set()

            def read(src):
                for _rid, fields, guid in apkg_notes(src):
                    guids.add(guid)
                    fronts.add(fields[0] if fields else "")

            to_fetch = []
            for d in manifest.get("decks", []):
                src = (local or {}).get(d["name"])
                if _is_local(src):
                    try:
                        read(src)
                    except Exception:
                        pass
                elif fetch is not None:
                    to_fetch.append(d)
            if to_fetch:
                starts, total = card_progress(to_fetch)
                with cancellable_progress("Checking your current decks", total) as step:
                    for i, d in enumerate(to_fetch, 1):
                        short = d["name"].split("::")[-1]
                        # Cancel ends the run: a partial read would let a still-shipped
                        # card look retired.
                        if not step(starts[i - 1] + 1, f"Reading {short} ({i} of {len(to_fetch)})"):
                            raise ProgressCancelled()
                        try:
                            read(_cached_fetch(fetch, d, on_chunk=step.pump,
                                               with_media=False))
                        except DownloadCancelled:
                            raise ProgressCancelled() from None
                        except Exception:
                            pass
            cache.append((guids, fronts))
        return cache[0]
    return load


def _reconcile_pending(manifest, cfg, live_cards=None):
    """Everything Reconcile my decks would find pending: retired cards still in the
    collection (split into fresh vs. already-archived) and cards sitting in a
    since-reorganized deck.

    Returns (existing_nids, fresh, already, moves, retired_deck, tag, stranded).
    `existing_nids` is {guid: nid} for every note under scope_tag. `stranded` is the
    reworded-card pairs the learner holds both halves of (see find_stranded_pairs); its
    predecessors are archived like `fresh`, but only after their scheduling has been
    carried forward.

    Three things are filtered out here: a card sitting in a deck the learner has
    unchecked in Manage decks, a relocation of a card this pass is about to archive, and
    a retirement of a card that is also half of a reworded pair, which the merge covers.

    `live_cards` is a _live_cards_loader: with it, a learner note that matches (by guid
    or front) a card the current packages still carry is never offered for archiving.
    """
    existing_nids = _existing_guid_to_nid(cfg["scope_tag"])
    # existing_fronts lets both ledgers act on a card whose GUID no longer matches them,
    # by its front, the way remap_cards matches content.
    existing_fronts = _existing_front_to_guid(cfg["scope_tag"])
    # A generated card's front can coincidentally match a ledger entry; never archive
    # or relocate it, same guard as remap_cards.
    found = [r for r in find_retired_in_collection(
                 manifest.get("retired", {}), set(existing_nids), existing_fronts)
             if not is_generated_guid(r["guid"])]
    existing_decks = _existing_guid_to_deck(cfg["scope_tag"])
    moves = [m for m in find_deck_moves_needed(manifest.get("deck_moves", {}),
                                               existing_decks, existing_fronts)
             if m["guid"] in existing_nids and not is_generated_guid(m["guid"])]

    tag = f'{cfg["scope_tag"]}::{RETIRED_TAG_LEAF}'
    retired_deck = f'{cfg["export_deck"]}::{RETIRED_DECK_LEAF}'
    # A previous run tags what it archives; skip those so re-running is a no-op on them.
    fresh, already = [], 0
    for r in found:
        if tag in mw.col.get_note(existing_nids[r["guid"]]).tags:
            already += 1
        else:
            fresh.append(r)
    stranded = [p for p in find_stranded_pairs(manifest.get("superseded_fronts", {}),
                                               existing_fronts)
                if p["guid"] in existing_nids and p["successor_guid"] in existing_nids
                and not is_generated_guid(p["guid"])
                and not is_generated_guid(p["successor_guid"])
                and tag not in mw.col.get_note(existing_nids[p["guid"]]).tags]
    if live_cards is not None and (fresh or stranded):
        live_guids, live_fronts = live_cards()
        kept = {r["guid"] for r in find_retired_in_collection(
            manifest.get("retired", {}), set(existing_nids), existing_fronts,
            live_guids, live_fronts)}
        fresh = [r for r in fresh if r["guid"] in kept]
        kept_pairs = {(p["guid"], p["successor_guid"]) for p in find_stranded_pairs(
            manifest.get("superseded_fronts", {}), existing_fronts, live_guids, live_fronts)}
        stranded = [p for p in stranded if (p["guid"], p["successor_guid"]) in kept_pairs]

    def _opted_out(guid):
        """Read against where the learner's copy lives, not the ledger's deck."""
        return _deck_opted_out(existing_decks.get(guid), cfg["excluded"])

    fresh = [r for r in fresh if not _opted_out(r["guid"])]
    stranded = [p for p in stranded
                if not (_opted_out(p["guid"]) or _opted_out(p["successor_guid"]))]
    # A card the retirement ledger names that the learner also holds both wordings of is
    # handled once, as the merge, which carries its scheduling and annotations forward
    # and then archives it.
    merged = {p["guid"] for p in stranded}
    fresh = [r for r in fresh if r["guid"] not in merged]
    archived = {r["guid"] for r in fresh} | merged
    # A card in both ledgers is retired, not relocated: a relocation after archiving
    # would pull it back out of the Retired deck.
    moves = [m for m in moves
             if m["guid"] not in archived and not _opted_out(m["guid"])]
    return existing_nids, fresh, already, moves, retired_deck, tag, stranded


def _stranded_lead(stranded):
    """What the reworded-pair section says before naming any of them, worded around what
    the learner sees (two versions of one card), not GUIDs.
    """
    return (f"<b>{plural(len(stranded), 'card')}</b> "
            f"{'is' if len(stranded) == 1 else 'are'} in your collection twice, in an "
            "older and a newer wording of the same question, because the wording "
            "changed after your first import. Your progress on the older copy moves to "
            "the newer one, then the older copy is archived.")


def _stranded_lines(stranded):
    """One line per reworded pair: the wording the learner holds, then the one it
    becomes. Both halves are raw note fields, so each goes through note_display_label.
    """
    muted = colors()["muted"]
    return [f"{_card_html([p['front']])} <span style='color:{muted};'>→ "
            f"{_card_html([p['successor_front']])}</span>"
            for p in stranded]


def _stranded_items(stranded):
    """The reworded-pair group for Update my decks' list, in build_update_body's row
    vocabulary: the sentence that explains it, then one RETIRED row per pair.
    """
    if not stranded:
        return []
    items = [("note", _stranded_lead(stranded))]
    append_rows(items, [("retired", line) for line in _stranded_lines(stranded)])
    return items


def _reconcile_backup_decks(fresh, moves, stranded, existing_nids):
    """Every deck a reconcile pass writes in, for the pre-run backup to cover.

    A retired card's live deck wins over its ledger deck, which is the fallback for a
    card the collection can't place. A stranded pair is in neither ledger: both of its
    cards' current decks are covered.
    """
    decks = []
    for r in fresh:
        decks += decks_holding([r["guid"]], existing_nids) or [r["deck"]]
    decks += [m["from"] for m in moves]
    for p in stranded:
        decks += decks_holding([p["guid"], p["successor_guid"]], existing_nids)
    return decks


def _content_backup_decks(srcs, aliases, scope_tag):
    """Decks holding the learner's cards that an import of `srcs` would rewrite.

    An import matches by GUID (then front, then alias) wherever the note sits, which may
    not be the manifest's deck. Asked through remap_cards, so this is the match the
    import will make.

    Only for a caller that already has the packages on disk. A deck that can't be read
    is skipped.
    """
    existing_fronts = _existing_front_to_guid(scope_tag)
    guids = set()
    for src in srcs:
        try:
            guids |= {g for _rid, _apkg_guid, g
                     in remap_cards(src, existing_fronts, aliases)[4]}
        except Exception:
            pass
    return decks_holding(guids, _existing_guid_to_nid(scope_tag)) if guids else []


def _reworded_backup_decks(superseded, scope_tag, skipped=(), skipped_retired_guids=()):
    """Decks holding either wording of any pair the source has ever reworded.

    Wider than the pairs found before a run: update_decks recomputes stranded pairs
    after its import, which can itself create one, and merges it in the same run. Its
    predecessor is already in the collection, so it is covered here before the backup is
    taken.
    """
    if not superseded:
        return []
    wanted = set(superseded) | set(superseded.values())
    existing_decks = _existing_guid_to_deck(scope_tag)
    existing_fronts = _existing_front_to_guid(scope_tag)
    if skipped or skipped_retired_guids:
        for p in find_stranded_pairs(superseded, existing_fronts):
            if (p["guid"] in skipped_retired_guids
                    or p["successor_guid"] in skipped_retired_guids
                    or _deck_opted_out(existing_decks.get(p["successor_guid"]), skipped)):
                wanted.discard(p["front"])
                wanted.discard(p["successor_front"])
    decks = []
    for front, guid in existing_fronts.items():
        deck = (existing_decks.get(guid)
                if front in wanted and guid not in skipped_retired_guids else None)
        if deck:
            decks.append(deck)
    return decks


def _merge_stranded(stranded, existing_nids, protected, retired_deck, tag, per_note=None):
    """Carry each stranded predecessor's scheduling and personal notes onto its live
    successor, then archive the predecessor. Returns the number of pairs merged.

    Scheduling first, archiving second, so a predecessor that failed to hand its history
    over stays in the review queue. Each pair goes through carry_over_protected_fields
    as one predecessor and one replacement.
    """
    if not stranded:
        return 0
    carry_scheduling_forward(stranded, existing_nids)
    carry_over_protected_fields(
        [{"guid": p["guid"], "superseded_by": [p["successor_guid"]]} for p in stranded],
        existing_nids, protected, per_note)
    archive_notes([existing_nids[p["guid"]] for p in stranded], retired_deck, tag)
    return len(stranded)


@_safe
@_manual_flow
def reconcile_decks():
    """Find retired cards still in the learner's collection and archive them, and
    relocate any cards a pure deck reorg has moved to a new deck.

    A sync only adds a split, merged or reworded card's replacements; the old card
    stays. This reads the retirement ledger in the manifest, finds the retired cards the
    learner still has, carries personal notes onto their replacement(s), and archives
    them: moved to a Retired subdeck, suspended, tagged. Nothing is deleted.

    Anki's importer assigns a deck only to a new note, so a card the source moved to
    another deck is not relocated by a sync. This reads the deck-moves ledger and
    relocates any card still sitting where the source last filed it
    (find_deck_moves_needed skips anything the learner has moved).

    An Advanced-menu action for running this half on its own; update_decks() runs it
    right after a sync.
    """
    cfg = _cfg()
    try:
        manifest, fetch, source = run_with_progress(
            "Checking the deck source", lambda: _fetch_manifest(cfg))
    except ProgressCancelled:
        return
    except Exception as e:
        _source_warning(e)
        return
    if not manifest:
        _warn("No deck source configured yet.<br><br>"
              "Open <b>Intern Pearls → Manage decks</b> and use Configure source.")
        return

    live_cards = _live_cards_loader(manifest, fetch)
    try:
        existing_nids, fresh, already, moves, retired_deck, tag, stranded = (
            _reconcile_pending(manifest, cfg, live_cards))
    except ProgressCancelled:
        return
    if not fresh and not moves and not stranded:
        _refresh_reconcile_action_label(0)
        if already:
            _info(f"{plural(already, 'retired card')} in your collection "
                  f"{'is' if already == 1 else 'are'} already archived (suspended and "
                  f"moved to <b>{RETIRED_DECK_LEAF}</b>). Nothing more to do.")
        else:
            _info("No retired cards or reorganized decks found in your collection, so "
                  f"nothing to tidy up. (Source: {source}.)")
        return

    # A big first run reads as alarming without context: say up front that it is a
    # one-time catch-up.
    catch_up_note = (
        "<i>This looks like a one-time catch-up, likely your first Reconcile since a "
        "larger update. Future runs should be much shorter.</i>"
        if len(fresh) + len(moves) + len(stranded) > 20 else "")

    # Every card is a row, chipped as on Update my decks' list (RETIRED, MOVED). Each
    # group's explanation sits directly above its rows. The lists are uncapped: the rows
    # stream and the buttons sit outside the scroll area.
    items = []
    missing = sum(1 for r in fresh
                  if r["superseded_by"] and r["replacements_present"] == 0)
    already_note = (f" ({already} more {'was' if already == 1 else 'were'} already "
                    "archived earlier.)" if already else "")
    if fresh:
        items.append(("note",
                      f"<b>{plural(len(fresh), 'retired card')}</b> "
                      f"{'is' if len(fresh) == 1 else 'are'} still in your collection. "
                      f"{'It was' if len(fresh) == 1 else 'They were'} split or reworded "
                      "since, with the replacements already added "
                      "separately, so "
                      f"{'it just duplicates' if len(fresh) == 1 else 'these just duplicate'} "
                      f"your reviews now.{already_note}"))
        append_rows(items, [("row", "retired", _card_html([r["identity"]]),
                             _text(r["deck"].split("::")[-1])) for r in fresh])
        if missing:
            items.append(("note",
                          f"<b>Note:</b> {missing} of these don't have their "
                          "replacement cards in your collection yet. Run <b>Sync "
                          "decks</b> first if you want the new versions before "
                          "archiving the old ones."))
    if stranded:
        items.append(("note", _stranded_lead(stranded)))
        # RETIRED: the older wording is archived once its progress has moved across.
        append_rows(items, [("row", "retired", line, "")
                            for line in _stranded_lines(stranded)])
    if moves:
        items.append(("note",
                      f"<b>{plural(len(moves), 'card')}</b> "
                      f"{'belongs' if len(moves) == 1 else 'belong'} to a deck that's "
                      "since been reorganized."))
        # Named through note_display_label: a raw first field is HTML.
        append_rows(items, [
            ("row", "moved",
             _card_html(mw.col.get_note(existing_nids[m["guid"]]).fields),
             f"→ {_text(m['to'].split('::')[-1])}") for m in moves])

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
        _reconcile_backup_decks(fresh, moves, stranded, existing_nids), cfg["scope_tag"])
    if not proceed:
        return
    per_note = manifest.get("note_protected_fields", {})
    carried = carry_over_protected_fields(fresh, existing_nids, cfg["protected"], per_note)
    n_merged = _merge_stranded(stranded, existing_nids, cfg["protected"], retired_deck, tag,
                               per_note)
    n_archived = archive_notes([existing_nids[r["guid"]] for r in fresh], retired_deck, tag)
    n_moved = apply_deck_moves(moves, existing_nids)
    mw.reset()
    _refresh_reconcile_action_label(0)   # this run just handled everything found
    backup_line = ("" if backed_up else
                   "<br><br>(No backup was taken this time: there was nothing to back up yet, or "
                   "it failed and you chose to continue.)")
    result_lines = []
    if n_archived:
        result_lines.append(
            f"Archived <b>{plural(n_archived, 'retired card')}</b> to "
            f"<b>{_text(retired_deck)}</b>: suspended and tagged "
            f"<code>{_text(tag)}</code>, review "
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
            f"{'its' if n_moved == 1 else 'their'} reorganized deck. Content and "
            "scheduling are untouched.")
    _info("<br><br>".join(result_lines) + backup_line)


@_safe
@_manual_flow
def clean_up_duplicates():
    """Find sync duplicates (two notes, same type and front text, different GUIDs) and
    archive the losing copy of each with the retire machinery Reconcile my decks uses:
    carry over personal notes, then suspend, move to the Retired deck, and tag so a
    later run skips it.

    See logic.find_duplicate_groups for the ranking rule: most reviews wins, ties prefer
    the copy under the deck source's current canonical deck path.
    """
    cfg = _cfg()
    try:
        manifest, _, source = run_with_progress(
            "Checking the deck source", lambda: _fetch_manifest(cfg))
    except ProgressCancelled:
        return
    except Exception as e:
        _source_warning(e)
        return
    if not manifest:
        _warn("No deck source configured yet.<br><br>"
              "Open <b>Intern Pearls → Manage decks</b> and use Configure source.")
        return

    tag = f'{cfg["scope_tag"]}::{DUPLICATE_TAG_LEAF}'
    canonical_deck_names = [d["name"] for d in manifest.get("decks", [])]
    existing_notes = _existing_notes_summary(cfg["scope_tag"], exclude_tag=tag)
    groups = find_duplicate_groups(existing_notes, canonical_deck_names)
    if not groups:
        _info(f"No duplicate cards found. (Source: {source}.)")
        return

    # One kind of row throughout, so no chip, and the caret and chip columns are
    # declined (see widgets.simple_row).
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

    # Backup scoped from where each group's notes sit, not from export_deck: duplicates
    # are found by tag across the whole collection, and both halves of a group are
    # written.
    proceed, backed_up = _pre_sync_backup_or_confirm_skip(
        cfg["export_deck"],
        [n["deck"] for g in groups for n in [g["keep"], *g["archive"]]],
        cfg["scope_tag"])
    if not proceed:
        return
    retired_deck = f'{cfg["export_deck"]}::{RETIRED_DECK_LEAF}'
    retired = [{"guid": a["guid"], "superseded_by": [g["keep"]["guid"]]}
               for g in groups for a in g["archive"]]
    existing_guid_to_nid = {n["guid"]: n["nid"] for g in groups
                            for n in [g["keep"], *g["archive"]]}
    carried = carry_over_protected_fields(retired, existing_guid_to_nid, cfg["protected"],
                                          manifest.get("note_protected_fields", {}))
    n_archived = archive_notes([a["nid"] for g in groups for a in g["archive"]],
                               retired_deck, tag)
    # Free each archived copy's GUID, or the next update matches it instead of the
    # kept card.
    fork_note_identities([{"guid": a["guid"]} for g in groups for a in g["archive"]])
    mw.reset()
    backup_line = ("" if backed_up else
                   "<br><br>(No backup was taken this time: nothing to back up yet, or "
                   "it failed and you chose to continue.)")
    _info(f"Archived <b>{plural(n_archived, 'duplicate card')}</b> to "
          f"<b>{_text(retired_deck)}</b>: suspended and tagged "
          f"<code>{_text(tag)}</code>, review "
          "history kept"
          + (f" ({plural(carried, 'personal note')} carried over to the kept copy)"
             if carried else "") + ". Bring any back by unsuspending it or moving "
          "it out of the Retired deck." + backup_line)


# A session-lived cache keyed by source, deck, package path and version, so reopening
# Update my decks does not re-download an unchanged deck. In memory only; a cached file
# swept from the temp directory is fetched again.
_apkg_cache = {}


def _share(starts, total, i):
    """How much of the bar the i-th (1-based) deck owns, for its images to fill."""
    return (starts + [total])[i] - starts[i - 1]


def _cached_fetch(fetch, d, on_chunk=None, with_media=True):
    """`with_media=False` is for callers that only read notes, so they never wait on
    image downloads; a full fetch already made serves them too."""
    key = (getattr(fetch, "source_key", fetch), d["name"], d.get("apkg"), d.get("version"))
    for k in ((key,) if with_media else (key, key + ("bare",))):
        hit = _apkg_cache.get(k)
        if hit and os.path.exists(hit):
            return hit
    if not with_media:
        key += ("bare",)
        path = fetch(d, on_chunk=on_chunk, with_media=False)
        _apkg_cache[key] = path
        return path
    bare = _apkg_cache.get(key + ("bare",))
    if bare and os.path.exists(bare):
        path = fetch(d, on_chunk=on_chunk, apkg=bare)
    else:
        path = fetch(d, on_chunk=on_chunk)
    _apkg_cache[key] = path
    return path


def _is_local(entry):
    """Whether a {deck: path or Exception} entry is a file that is actually there. The
    scratch directory is a temp directory and can be swept during a long session.
    """
    return (entry is not None and not isinstance(entry, Exception)
            and os.path.exists(entry))


# Why each deck's last preview failed, so the retry prompt can say it.
_preview_errors = {}


def _preview_content_changes(fetch, todo, existing_fronts, aliases, existing_fields=None,
                             per_note=None, edited=None):
    """Download every pending deck and match it against the collection, so the
    confirmation can show real kept/new counts. Runs under a cancellable progress
    window.

    Returns ({deck_name: (kept, new, new_notes, changed) | None}, downloaded,
    cancelled). `new_notes` is remap_cards' list of the notes that will import as new.
    `changed` is {note id: {field: the learner's current value}} for the cards this deck
    would rewrite. `downloaded` is {deck_name: local_path_or_Exception}, the shape
    _run_sync takes, so no deck is downloaded twice. A per-deck failure is recorded, not
    raised; a deck whose file downloaded but failed to parse keeps the file. `cancelled`
    means the learner clicked Cancel; nothing has touched the collection.

    Downloads go through _cached_fetch.
    """
    preview, downloaded = {}, {}
    _preview_errors.clear()
    baseline = _load_shipped()
    starts, total = card_progress(todo)
    with cancellable_progress("Checking for updates", total) as step:
        for i, d in enumerate(todo, 1):
            short = d["name"].split("::")[-1]
            if not step(starts[i - 1] + 1, f"Checking {short} ({i} of {len(todo)})",
                        span=_share(starts, total, i)):
                return preview, downloaded, True
            try:
                # step.pump keeps Cancel live during the download, a single blocking
                # read on the main thread.
                src = _cached_fetch(fetch, d, on_chunk=step.pump)
                downloaded[d["name"]] = src
                _, kept, new, new_notes, matched = remap_cards(src, existing_fronts, aliases)
                changed = {}
                if existing_fields and matched:
                    # Resolved as _run_sync resolves it, so a note matched by front
                    # reads as protected here too.
                    resolved_per_note = {**(per_note or {}),
                                        **per_note_for_package(per_note, matched)}
                    details = apkg_note_details(src)
                    changed = find_changed_notes(
                        matched, details, existing_fields,
                        protected=_cfg()["protected"], per_note=resolved_per_note,
                        baseline=baseline)
                    for rid, fields in find_kept_edits(matched, details, existing_fields,
                                                       edited or {}, baseline).items():
                        changed.setdefault(rid, {}).update(fields)
                preview[d["name"]] = (kept, new, new_notes, changed)
            except DownloadCancelled:
                # Cancel clicked mid-download: report it as a cancel, not a deck that
                # failed to preview.
                return preview, downloaded, True
            except Exception as e:
                # setdefault, not assignment: the fetch may have succeeded and only the
                # read failed, and the apply step can still use the file.
                downloaded.setdefault(d["name"], e)
                preview[d["name"]] = None
                _preview_errors[d["name"]] = str(e) or type(e).__name__
    return preview, downloaded, False


def _gather_pending_items(todo, preview, downloaded, extra=None, registry=None,
                          change_notes=None, installed=None, note_sources=None):
    """Every card this update would touch, grouped under its deck, for the inline card
    list on the confirmation.

    `extra` is `_retired_moved_items`' {deck: [row, ...]}: rows read from the ledgers
    rather than a downloaded .apkg. They are folded in under that deck's heading through
    the same grouping pass as its cards (see `_grouped_rows`). A deck whose only pending
    work is a retirement or relocation gets a heading of its own at the end.

    `registry` is config.load_declined()'s {guid: entry}. A card declined "never" is
    dropped from the list and counted in `hidden`. An unchanged revision declined "keep"
    is omitted without being counted; it remains available from Manage decks. Other
    "keep"/"held" rows are kept and tagged `detail["declined_state"]` (see
    review._card_row). A "keep" row whose incoming fields hash differs from what was
    declined gets `detail["changed_since_decline"]` too; a "held" (Later) row gets
    logic.later_status' keys instead.

    Returns (items, failed, sources, hidden). `items` is a mix of ("header",
    deck_short_name), ("sep",), and ("card", deck_name, detail) entries plus whatever
    `extra` supplied. Each detail is tagged "kind" ("new" or "changed") and, for a
    changed one, "was" (what the learner's copy currently says). A detail can also carry
    "change_notes", the deck source's account of why the card changed, attached for
    changed cards always and for new cards only in a deck already installed.

    A detail carries "card_source" when the deck source ships a short label for where
    that card came from (see logic.source_label_for); it is shown on every row that has
    one.

    `failed` names decks whose pending cards could not be read; the update is
    unaffected. `sources` is {deck_name: .apkg path}, for the row pictures' resolvers.
    """
    items, failed, sources, hidden = [], [], {}, 0
    extra = dict(extra or {})

    def _extra_for(deck_name):
        """This deck's ledger rows, including any filed under a subdeck of it: cards
        often land in `deck_name::<subdeck>`, so rows match by prefix as well as
        exactly.
        """
        keys = [k for k in extra if k == deck_name or k.startswith(deck_name + "::")]
        return [row for k in keys for row in extra.pop(k)]

    def _section(heading, rows):
        """`rows` is `_grouped_rows`' (row, grouped) pairs. `grouped` marks a row under
        a group_note header; the sep ahead of it is tagged the same way, so a folded
        group can tell its own hairlines from top-level ones.
        """
        items.append(("header", heading))
        for i, (row, grouped) in enumerate(rows):
            if i:
                items.append(("sep", "grouped") if grouped else ("sep",))
            items.append(row)

    def _grouped_rows(details, extra_rows, deck_name):
        """This deck's card details and its raw `_retired_moved_items` rows, folded
        through group_change_notes into the deck section's (row, grouped) list,
        `grouped` marking a row that sits under a group_note header (see `_section`).

        A group of more than one member with a shared note gets one ("group_note", note,
        card_count) header (see review._change_note_row); that entry is then dropped
        from each member card's `change_notes`, while any other entry still renders on
        the card. `card_count` counts the group's ("card", ...) members only: a retired
        member renders as ("retired", ...) and keeps its own reason. A moved row never
        groups and always trails.
        """
        retired_raw = [r for r in extra_rows if r.get("row_kind") == "retired"]
        moved_raw = [r for r in extra_rows if r.get("row_kind") == "moved"]
        retired_ids = {id(r) for r in retired_raw}
        rows = []
        for group in sort_source_groups(group_change_notes(details, retired_raw)):
            members, note = group["members"], group["note"]
            header = len(members) > 1 and note is not None
            # A retired member renders as ("retired", ...), not a card, so it does not
            # belong in a header that reads "N cards".
            card_members = sum(1 for m in members if id(m) not in retired_ids)
            if header:
                rows.append((("group_note", note, card_members), False))
            key = (note.get("kind"), note.get("note"), note.get("on")) if note else None
            for m in members:
                if id(m) in retired_ids:
                    rows.append((("retired", _card_html([m["identity"]]),
                                  m.get("reason", "")), header))
                    continue
                if header:
                    cn = m.get("change_notes") or []
                    kept = [e for e in cn if not (isinstance(e, dict) and
                            (e.get("kind"), e.get("note"), e.get("on")) == key)]
                    if len(kept) != len(cn):
                        m = dict(m, change_notes=kept)
                rows.append((("card", deck_name, m), header))
        for m in moved_raw:
            rows.append((("moved", _text(m["front"]), _text(m["to"])), False))
        return rows

    for d in todo:
        details = []
        pc = preview.get(d["name"])
        src = downloaded.get(d["name"])
        if pc and src and not isinstance(src, Exception) and (pc[2] or pc[3]):
            try:
                rids = [r for r, _, _ in pc[2]] + list(pc[3])
                fetched = apkg_note_details(src, rids)
            except Exception as e:
                failed.append(_text(f"{d['name'].split('::')[-1]} ({e})"))
                fetched = []
            new_rids = {r for r, _, _ in pc[2]}
            for detail in fetched:
                detail["kind"] = "new" if detail["rid"] in new_rids else "changed"
                detail["was"] = pc[3].get(detail["rid"], {})
                entry = (registry or {}).get(detail["guid"])
                state = entry.get("state") if isinstance(entry, dict) else None
                if state in ("never", "frozen"):
                    hidden += 1
                    continue
                # Keep applies to the revision already reviewed, not every unrelated
                # update to its deck. Preserve import protection, but don't ask the
                # same question again until this note's source fields change.
                if (state == "keep" and entry.get("hash")
                        and entry["hash"] == note_fields_hash(
                            [v for _, v in detail["fields"]])):
                    continue
                if state:
                    detail["declined_state"] = state
                    incoming = note_fields_hash([v for _, v in detail["fields"]])
                    # A held card says what it needs through later_status instead:
                    # it was set aside, not turned down, so "changed since" is not
                    # its cue.
                    if state == "held":
                        detail.update(later_status(entry, incoming))
                    elif entry.get("hash") and entry["hash"] != incoming:
                        detail["changed_since_decline"] = True
                label = source_label_for(note_sources, detail["guid"])
                if label:
                    detail["card_source"] = label
                if change_notes and (detail["kind"] == "changed"
                                     or d["name"] in (installed or {})):
                    incoming = [v for _, v in detail["fields"]]
                    notes = change_notes_for(change_notes, detail["guid"], incoming)
                    if notes:
                        detail["change_notes"] = notes
                details.append(detail)
            if fetched:
                sources[d["name"]] = src
        rows = _grouped_rows(details, _extra_for(d["name"]), d["name"])
        if rows:
            _section(d["name"].split("::")[-1], rows)

    for deck, extra_rows in extra.items():
        _section(deck.split("::")[-1], _grouped_rows([], extra_rows, deck))
    return items, failed, sources, hidden


def _retired_moved_items(fresh, moves, existing_nids):
    """Retired and relocated cards as rows for the inline list `_gather_pending_items`
    builds.

    Keyed by the deck each card belongs to: a retired card by the retirement ledger's
    deck, a relocated card by `from`, where it sits today. A moved row names its
    destination; a retired row does not repeat its deck.

    Returns {deck: [row, ...]}, empty when nothing is pending. A retired row is a dict
    carrying `guid`, `identity`, `reason`, and `superseded_by` (raw, not yet rendered)
    for group_change_notes; a moved row is `{"row_kind": "moved", "front", "to"}`.
    `existing_nids` is `_reconcile_pending`'s {guid: nid} map, needed to read a moved
    card's current front.
    """
    by_deck = {}
    for r in fresh:
        by_deck.setdefault(r["deck"], []).append({
            "row_kind": "retired", "guid": r["guid"], "identity": r["identity"],
            "reason": r.get("reason", ""),
            "superseded_by": list(r.get("superseded_by") or [])})
    for m in moves:
        # note_display_label, not the raw first field, which is HTML.
        front = note_display_label(mw.col.get_note(existing_nids[m["guid"]]).fields)
        by_deck.setdefault(m["from"], []).append(
            {"row_kind": "moved", "front": front, "to": m["to"].split("::")[-1]})
    return by_deck


# The fixed reassurance below the update confirmation's list. Module-level so the
# real-Qt tests render this exact string.
_UPDATE_SAFETY_NOTE = (
    "This is a preview: nothing is applied until you confirm below, and a backup "
    "is taken automatically first. Your review history and your own notes on cards "
    "are always kept, and nothing here is ever deleted. Anything you leave for later "
    "or turn away can be offered again under Manage decks \u2192 Later and declined cards.")


@_safe
@_manual_flow
def update_decks():
    """The one-click front door: computes everything pending (deck content updates,
    retired cards still in the collection, and cards a deck reorg needs to relocate) in
    a single pass, shows one confirmation covering all of it, then applies content
    updates before archiving/relocating, so a retired card's replacement is in place
    before the old card is archived.

    Composes _run_sync, _reconcile_pending, carry_over_protected_fields, archive_notes
    and apply_deck_moves; this adds the combined preview/confirm/summary layer.
    """
    cfg = _cfg()
    reg = load_declined()
    fetched = _fetch_manifest_gated(cfg)
    if not fetched:
        return
    manifest, fetch, source = fetched
    _check_deck_skill(cfg, manifest, fetch)

    installed = installed_matching_collection(_load_json(INSTALLED, {}), cfg["scope_tag"])
    todo = decks_to_update(manifest, installed, cfg["excluded"],
                           held=held_deck_names(reg))
    if _record_current_hashes(cfg, manifest, fetch, installed, todo, reg):
        _info(NOTHING_CHANGED)
        return
    live_cards = _live_cards_loader(manifest, fetch)
    try:
        existing_nids, fresh, _already, moves, retired_deck, tag, stranded = (
            _reconcile_pending(manifest, cfg, live_cards))
    except ProgressCancelled:
        _info(NOTHING_CHANGED)
        return

    if not todo and not fresh and not moves and not stranded:
        _refresh_reconcile_action_label(0)
        _info(f"You're all up to date (source: {source}).")
        return

    existing_fields = _existing_guid_to_fields(cfg["scope_tag"])
    edits = _learner_edits(cfg, manifest, existing_fields)
    edited = {}
    for e in edits:
        edited.setdefault(e["guid"], set()).add(e["field"])
    take_deck = set()
    preview, downloaded, collisions = {}, {}, []
    skipped = set()
    skipped_retired_guids = set()
    if todo:
        preview_args = (_existing_front_to_guid(cfg["scope_tag"]),
                        manifest.get("front_aliases", {}),
                        existing_fields,
                        with_edits_protected(manifest.get("note_protected_fields", {}),
                                             ((e["guid"], e["field"]) for e in edits)),
                        edited)
        preview, downloaded, cancelled = _preview_content_changes(fetch, todo, *preview_args)
        if cancelled:
            _info(NOTHING_CHANGED)
            return
        while True:
            failed = [d for d in todo if preview[d["name"]] is None]
            if not failed:
                break
            names = "".join(
                f"<p>{_text(d['name'].split('::')[-1])}: "
                f"{_text(_preview_errors.get(d['name'], 'unknown error'))}</p>"
                for d in failed)
            one = len(failed) == 1
            retry = _ask(
                ("This deck could not be previewed:" if one
                 else "These decks could not be previewed:") + names
                + ("Its cards" if one else "Their cards")
                + " cannot be shown for review. Retry the preview, or skip "
                  + ("it" if one else "them") + " for now. Skipping leaves "
                  + ("it" if one else "them") + " for the next Update my decks.",
                yes_label="Retry preview", no_label="Skip for now", default_yes=True)
            if not retry:
                failed_names = {d["name"] for d in failed}
                skipped.update(failed_names)
                skipped_retired_guids = {
                    guid for deck, entries in manifest.get("retired", {}).items()
                    if _deck_opted_out(deck, skipped) for guid in entries}
                todo = [d for d in todo if d["name"] not in failed_names]
                for name in failed_names:
                    preview.pop(name, None)
                    downloaded.pop(name, None)
                fresh = [r for r in fresh if r["guid"] not in skipped_retired_guids
                         and not _deck_opted_out(r["deck"], skipped)]
                moves = [m for m in moves if m["guid"] not in skipped_retired_guids
                         and not _deck_opted_out(m["to"], skipped)]
                existing_decks = _existing_guid_to_deck(cfg["scope_tag"])
                stranded = [p for p in stranded
                            if p["guid"] not in skipped_retired_guids
                            and p["successor_guid"] not in skipped_retired_guids
                            and not _deck_opted_out(
                                existing_decks.get(p["successor_guid"]), skipped)]
                if not todo and not fresh and not moves and not stranded:
                    _refresh_reconcile_action_label(0)
                    _info("Nothing was updated. "
                          + ("This deck is" if one else "These decks are")
                          + " still waiting and will be offered again next time:" + names)
                    return
                break
            retried, fetched_again, cancelled = _preview_content_changes(
                fetch, failed, *preview_args)
            if cancelled:
                _info(NOTHING_CHANGED)
                return
            preview.update(retried)
            downloaded.update(fetched_again)

    # Every card this update would add, in deck order then .apkg order. `new_index` maps
    # each GUID to its deck and card, so a flag the learner writes can say what it was
    # about. `incoming_hashes` is what a Keep, Never or Later this run records into the
    # declined registry.
    new_cards, incoming_hashes = [], {}
    for d in todo:
        pc = preview.get(d["name"])
        for _, fields, guid in pc[2]:
            new_cards.append((d["name"], note_display_label(fields), guid))
            incoming_hashes[guid] = note_fields_hash(fields)
    # The cards this update would rewrite, gathered the same way. A deck whose details
    # can't be read is skipped here; the update still applies.
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
    # Template changes and note-type conversions are detected here, from the packages
    # the preview already downloaded, so one confirmation and one question cover the
    # whole run before the apply loop starts. Sync decks still asks per deck (see
    # _run_sync's `convert_notetypes`). Kept per deck, so a deck read twice is replaced
    # rather than added to, and so the apply step can tell which decks the question
    # covered.
    pending_templates, conversions_by_deck = {}, {}
    # Declined cards are left out of the plan: their content is dropped from the import
    # (logic.declined_drop).
    declined = declined_guids(reg)
    # Held cards default to Import on this screen, so they count as pending, except
    # one that returns waiting at Later (a noted card not yet updated, or an old Skip).
    waiting = {g for g, e in held_entries(reg).items()
               if later_status(e, incoming_hashes.get(g)).get("later_wait")}
    counted_out = declined - (set(held_entries(reg)) - waiting)
    existing_fronts = _existing_front_to_guid(cfg["scope_tag"])
    aliases = manifest.get("front_aliases", {})
    note_types = _NoteTypeCache(cfg["scope_tag"])
    for d in todo:
        src = downloaded.get(d["name"])
        if not src or isinstance(src, Exception):
            continue
        try:
            pending_templates.update(_template_changes(src))
        except Exception:
            pass    # a deck we can't read here still imports; it just can't offer this
        try:
            conversions_by_deck[d["name"]] = notetype_changes(
                src, existing_fronts, aliases, cfg["scope_tag"], counted_out,
                note_types.read())
        except Exception:
            pass    # same: unreadable here, still imported, just not offered up front
    pending_conversions = [c for cs in conversions_by_deck.values() for c in cs]

    new_index = {guid: (deck, label) for deck, label, guid in new_cards + changed_cards}
    flags = {}
    # Notes from a run that never reached its digest come back here, in this run's
    # flags. merge_saved_feedback carries each one's deck and front, since the card may
    # not be in this run's index.
    recovered = merge_saved_feedback(load_saved_feedback(), flags, new_index)

    # A standing decline drops the card from the import whatever its state (see
    # logic.declined_guids), so the per-deck counts must not count any of them as
    # pending. A new card's guid reads off its deck's preview; a changed card's guid
    # exists only in `changed_cards`, so those are tallied per deck here. A waiting
    # Later row is still on the list, so it is counted, and named.
    suppressed_changed, waiting_changed = {}, {}
    for deck_name, _label, g in changed_cards:
        if g in waiting:
            waiting_changed[deck_name] = waiting_changed.get(deck_name, 0) + 1
        elif g in counted_out:
            suppressed_changed[deck_name] = suppressed_changed.get(deck_name, 0) + 1

    def _deck_summary_row(d, folded=0):
        """One deck's line in the summary that opens the list: the deck as the primary
        text, its counts as the trailing muted column. The changed count is a subset of
        kept, so it is parenthesized onto "kept" rather than joined with "·".
        """
        short = d["name"].split("::")[-1]
        pc = preview[d["name"]]
        changing = len(pc[3]) - suppressed_changed.get(d["name"], 0)
        held_back = waiting_changed.get(d["name"], 0)
        waits = f", {held_back} waiting at Later" if held_back else ""
        kept = f"{pc[0]} kept" + (f" ({changing} changing{waits})" if changing else "")
        new_count = sum(1 for _rid, _fields, g in pc[2]
                        if g not in counted_out or g in waiting)
        new_waiting = sum(1 for _rid, _fields, g in pc[2] if g in waiting)
        new = f"{new_count} new" + (f" ({new_waiting} waiting at Later)"
                                    if new_waiting else "")
        tail = f" · {folded} in folded groups" if folded else ""
        return ("deck", _text(short), f"{kept} · {new}{tail}")

    muted = colors()["muted"]
    sections = []
    # The deck summary is the list's first section (see the `items` assembly below).
    # New, changed, retired and relocated cards get no section in this fixed text: the
    # inline list is built from them (see _gather_pending_items and
    # _retired_moved_items). new_cards/changed_cards are still needed above, to build
    # new_index.

    # A big first run reads as alarming without context: say up front it's a one-time
    # catch-up.
    catch_up_note = (
        "<i>This looks like a one-time catch-up, likely your first update in a "
        "while. Future updates should be much shorter.</i>"
        if len(fresh) + len(moves) + len(stranded) > 20 else "")
    # Counts read off `decisions` below, in the row controls' own words. A fixed word
    # order, not click order.
    _TALLY_WORDS = (("held", "left for later"), ("keep", "kept yours for now"),
                    ("never", "never"), ("frozen", "no more updates"))

    def _decision_tally():
        counts = {}
        for state in decisions.values():
            counts[state] = counts.get(state, 0) + 1
        parts = [f"{counts[s]} {word}" for s, word in _TALLY_WORDS if s in counts]
        return ", ".join(parts) + "." if parts else ""

    def _status_line():
        parts = []
        # What Update would apply without the reader having looked at it, said beside
        # the buttons that do it. Informative only: the hold button is the way out.
        unopened = unopened_line(row_kind, touched | opened, folded, reg, decisions)
        if unopened:
            parts.append(unopened)
        if flags:
            if not recovered:
                carried_txt = ""
            elif len(flags) == 1:
                # Avoids "1 card flagged. 1 of them carried over".
                carried_txt = " It carried over from an earlier session."
            else:
                carried_txt = f" {recovered} of them carried over from an earlier session."
            parts.append(f"<b>{plural(len(flags), 'card')} flagged.</b>{carried_txt} "
                         "You'll get a summary to send back when this finishes.")
        tally = _decision_tally()
        if tally:
            parts.append(tally)
        if hidden:
            parts.append(f"{plural(hidden, 'card')} hidden (Never). Restore "
                         f"{'it' if hidden == 1 else 'them'} under Manage decks \u2192 "
                         "Later and declined cards.")
        return ("<br><br>".join(parts) + "<br><br>") if parts else ""

    def _finish(title=None, items=(), run_decisions=None, nothing_note=""):
        """End the run: the summary and the learner's notes as one dialog, then drop the
        saved copy of those notes if the digest was archived.

        Called on every exit path including Cancel, so notes written before backing out
        still reach the digest.

        `title` is the run's headline and `items` is everything below it in
        build_list_body's vocabulary, passed to show_result_with_feedback. Both are left
        at their defaults when there is no completed run to summarize. `run_decisions`
        is the reader-facing {guid: "kept yours"/"never"/...} for what this run decided.
        Only declining the confirmation itself, the one exit before the registry write,
        leaves it at None.

        The saved copy is cleared only once the digest has been archived and shown. An
        exit with nothing flagged leaves the file alone, so a recovered note is not
        lost.
        """
        entries = feedback_entries(flags, new_index, run_decisions)
        # Read back from disk rather than `reg`: the import prunes entries for
        # retired cards, and the snapshot should match what the registry now holds.
        standing = load_declined()
        if run_decisions is not None:
            save_later_seen(later_count(standing, cfg["excluded"]))
        archived = show_result_with_feedback(title, items, entries, nothing_note=nothing_note,
                                             standing_declines=standing, excluded=cfg["excluded"],
                                             edits=edits)
        if entries:
            if archived:
                clear_saved_feedback()
            else:
                _warn("A copy of this card feedback couldn't be saved, so your notes are kept "
                      "and will come back on your next update.", textFormat="plain")

    # Unticked by default: applying it forces a one-time full AnkiWeb sync. Declining
    # still imports the content, and the next update carrying a look change offers it
    # again.
    tpl_choice = {"label": "Also apply the new card look (forces a one-time full "
                           "AnkiWeb sync)", "checked": False} if pending_templates else None
    # Disclosed here so the question asked later isn't the first the reader hears of it.
    # It can't be a second checkbox: _ask_with_widget carries one.
    if pending_conversions:
        convertible, beside = split_notetype_changes(pending_conversions)
        sections.append(
            f"<b>{plural(len(pending_conversions), 'card')}</b> in this update changed "
            f"format {_FORMAT_CHANGE}."
            + (f" {_kept_beside_text(beside, bool(convertible))}" if beside else "")
            + (" You'll be asked once, before anything imports, whether to move "
               f"{'the others' if beside else 'your existing cards'} across."
               if convertible else ""))

    items, unreadable, sources, hidden = _gather_pending_items(
        todo, preview, downloaded, _retired_moved_items(fresh, moves, existing_nids),
        registry=reg, change_notes=manifest.get("change_notes"), installed=installed,
        note_sources=manifest.get("note_sources"))
    folded = folded_guids(items)
    folded_by_deck = {}
    for deck_name in folded.values():
        folded_by_deck[deck_name] = folded_by_deck.get(deck_name, 0) + 1
    if todo:
        summary = [("header", f"{plural(len(todo), 'deck')} "
                              f"{'has' if len(todo) == 1 else 'have'} updates:")]
        for i, d in enumerate(todo):
            if i:
                summary.append(("sep",))
            summary.append(_deck_summary_row(d, folded_by_deck.get(d["name"], 0)))
        items = summary + items
    items += _stranded_items(stranded)
    if unreadable:
        sections.append(
            f"<span style='color:{muted};'>Couldn't read the pending cards from "
            + ", ".join(unreadable) + ". The update itself is unaffected; those decks "
            "just aren't shown here.</span>")
    # Last of the blocks above the list: ui._place_checkbox puts the look-change box
    # directly under this text, so the last paragraph is the one it reads as answering.
    if tpl_choice:
        sections.append(
            "This update also changes how some cards look (template or styling) for: "
            + ", ".join(f"<b>{_text(n)}</b>" for n in sorted(pending_templates))
            + ". Your review history and card content are unaffected either way.")

    # Joined rather than each block carrying its own trailing break, since every block
    # is optional.
    top_html = "<br><br>".join(b for b in [catch_up_note] + sections if b)
    # Seeded by build_update_body itself from `items`' own predeclined details before
    # any row is built; read back here once the dialog closes to know what the learner
    # decided.
    decisions = {}
    touched, opened = set(), set()
    row_kind = {item[2]["guid"]: item[2].get("kind")
               for item in items if item[0] == "card"}
    # A held card filed under a deck the source no longer lists follows its card to
    # the deck it ships in now, read from this run's packages and rows.
    if held_outside_manifest(reg, manifest):
        seen = {}
        for item in items:
            if item[0] == "card":
                seen.setdefault(item[1], set()).add(item[2]["guid"])
        for d in todo:
            src = downloaded.get(d["name"])
            if _is_local(src):
                try:
                    seen.setdefault(d["name"], set()).update(
                        g for _r, _f, g in apkg_notes(src))
                except Exception:
                    seen.pop(d["name"], None)   # unread, so nothing is judged gone
        if settle_held_outside_manifest(reg, manifest, seen):
            save_declined(reg)
    # The hold control is built after the body, which seeds `decisions`, so its count
    # leaves out rows that start away from their default.
    hold_refresh = []

    def _on_review():
        for refresh in hold_refresh:
            refresh()

    body, _boxes, flush = build_update_body(
        items, sources, flags, new_index, decisions, top_html,
        _status_line, _UPDATE_SAFETY_NOTE, touched, opened=opened,
        on_review=_on_review, take_deck=take_deck)
    hold, refresh_hold = hold_control(reg, row_kind, lambda: touched | opened,
                                      decisions)
    hold_refresh.append(refresh_hold)
    # Decks whose pending cards were all listed, so a held card missing from the list
    # has nothing left to offer rather than just failing to read.
    readable = {d["name"] for d in todo
                if d["name"] in sources
                or (preview.get(d["name"]) and not (preview[d["name"]][2]
                                                    or preview[d["name"]][3]))}

    # "Update" only when there is content to update. With only retired and relocated
    # cards pending, the button says what Reconcile my decks says.
    yes_label = "Update" if todo else (" and ".join(
        x for x in ("Archive" if fresh or stranded else None,
                    "relocate" if moves else None) if x) or "Apply")

    # flush runs through on_close, not after this call: it reads the note boxes and
    # stops their save timer, and Qt has freed those widgets by the time this returns.
    #
    # min_width is above _ask_with_widget's 560px default so a row keeps room for the
    # card's text beside its decision control. open_size opens well above both minimums,
    # clamped to the screen.
    accepted = _ask_with_widget(body, yes_label=yes_label, checkbox=tpl_choice,
                                on_close=flush, min_width=660, open_size=(880, 800),
                                extra=hold)

    if not accepted:
        _finish()
        return

    # Folded into the declined registry now, so a Keep/Never/Later survives even if the
    # apply loop below is cancelled. `row_kind` (built above) is every card row's kind.
    prior = dict(reg)
    hold_now = (holdable_guids(prior, row_kind, touched | opened, decisions)
               if hold and hold.get("clicked") else [])
    today = datetime.date.today().isoformat()

    def _prior_entry(g):
        # A hand-edited registry can hold a non-dict value for a guid; read it as no
        # prior decline.
        e = prior.get(g)
        return e if isinstance(e, dict) else {}

    def _registry_entry(guid, state):
        # Every guid written below was read into incoming_hashes by the preview, so the
        # empty-hash default should be unreachable.
        deck, front = new_index.get(guid, ("", guid))
        return {"state": state, "front": plain_text(front), "deck": deck,
                "decided": today, "hash": incoming_hashes.get(guid, "")}

    # "Decided this run" means a guid whose state changed from what the registry held
    # when the dialog opened; the write below and the digest both key off it. A row left
    # as seeded is neither rewritten nor reported, so its prior hash and any pending
    # "changed since decline" cue stay. Later ("held") is a deferral, so it stays out of
    # `run_decisions` and the digest; its own write follows below.
    run_decisions = {g: {"keep": "kept yours", "never": "never",
                         "frozen": "kept yours, no more updates"}[s]
                     for g, s in decisions.items()
                     if s != "held" and _prior_entry(g).get("state") != s}
    for guid in run_decisions:
        reg[guid] = _registry_entry(guid, decisions[guid])
    # `touched` also catches a click that lands on the state the registry already held
    # (a re-review of a stale-hash card). Its stored hash/front refresh, but it is not a
    # new decision, so it stays out of `run_decisions` and the digest.
    for guid, s in decisions.items():
        if s != "held" and guid in touched and _prior_entry(guid).get("state") == s:
            reg[guid] = _registry_entry(guid, s)
    # Later set or re-chosen this run, or given a new note, is written fresh with the
    # row's note, so a noted card waits for its content to change. Re-choosing Later
    # on a card still waiting keeps its earlier note. A row only seeded at Later and
    # left alone keeps its entry, note and hash as they were.
    later = [g for g, s in decisions.items() if s == "held"]
    for guid in later:
        was = _prior_entry(guid)
        note = (flags.get(guid) or "").strip()
        if not (guid in touched or was.get("state") != "held"
                or (note and note != was.get("note"))):
            continue
        if not note and later_status(was, incoming_hashes.get(guid)).get(
                "later_note_status") == "not_updated":
            note = was["note"]
        reg[guid] = _registry_entry(guid, "held")
        if note:
            reg[guid]["note"] = note
    # A guid drops out of `decisions` only when the learner's click set its control back
    # to the row's default. But a visible row can only have been seeded with the state
    # `_EXPRESSIBLE_DECLINE` names for its current kind, so a prior entry in any other
    # state (a "keep" on a row that is new again) was never seeded, and its absence from
    # `decisions` says nothing. `touched` tells an active click on such a row apart.
    _EXPRESSIBLE_DECLINE = {"changed": "keep"}
    for guid in [g for g in reg
                 if g not in decisions and g in row_kind
                 and _prior_entry(g).get("state") != "held"
                 and (_prior_entry(g).get("state") == _EXPRESSIBLE_DECLINE.get(row_kind.get(g))
                      or (g in touched and _prior_entry(g).get("state") is not None))]:
        del reg[guid]        # the learner flipped a standing decline back to the default
        run_decisions[guid] = "imported after all"
    released = released_held_guids(prior, row_kind, decisions, hold_now, readable)
    for guid in released:
        reg.pop(guid, None)
        # A migrated entry was a Skip, whose flip back to Import was always reported.
        if (guid in row_kind
                and (_prior_entry(guid).get("migrated") or _prior_entry(guid).get("wait"))
                and (row_kind[guid] == "new" or guid in touched)):
            run_decisions[guid] = "imported after all"
    # A released hold may have been the only reason its deck was still pending; drop
    # it from installed.json too, so a run that stops before that deck actually
    # imports still offers the card again next time instead of losing it.
    released_decks = {prior[g]["deck"] for g in released if prior.get(g, {}).get("deck")}
    if released_decks:
        invalidate_installed(released_decks)
    for guid in hold_now:
        reg[guid] = _registry_entry(guid, "held")
    # The learner has now seen the "Skip is now Later" line for these rows. An old
    # Skip left alone keeps waiting at Later rather than returning at Import.
    for guid in row_kind:
        entry = reg.get(guid)
        if (isinstance(entry, dict) and entry.get("state") == "held"
                and entry.pop("migrated", None)):
            entry["wait"] = True
    save_declined(reg)
    n_later = len(set(later) | set(hold_now))
    held_note = (f"{plural(n_later, 'card')} left for later. "
                f"{'It comes' if n_later == 1 else 'They come'} back the next "
                "time you run Update my decks.") if n_later else ""
    held_items = [("note", held_note)] if held_note else []
    # The registry write already happened above, so an early exit that involved a hold
    # says so.
    early_exit_note = (f"Update cancelled before anything was imported. {held_note}"
                       if n_later else NOTHING_CHANGED)

    undisclosed = set()
    if todo:
        cancelled = _retry_failed_downloads(fetch, todo, downloaded)
        if cancelled:
            # Nothing has been backed up or imported yet, but the registry write already
            # happened, so what the learner decided is reported.
            _finish(run_decisions=run_decisions, nothing_note=early_exit_note)
            return
        # Planned from the registry as it now stands: a card the learner declined or
        # kept held is out, one they released or set back to default is in.
        conversions_by_deck = _final_conversion_plan(
            todo, downloaded, existing_fronts, aliases, cfg["scope_tag"],
            declined_guids(reg))
        pending_conversions = [c for cs in conversions_by_deck.values() for c in cs]
        # A deck the question below cannot cover: not on disk after two attempts, or
        # planned with no conversion. A conversion the apply loop finds in either is
        # deferred (see _run_sync's undisclosed_conversions).
        undisclosed = {d["name"] for d in todo
                       if not _is_local(downloaded.get(d["name"]))
                       or not conversions_by_deck.get(d["name"])}

    # The decks this run will write in: the ones it imports into, the ones whose cards
    # its imports rewrite (see _content_backup_decks), the ones its archived and
    # relocated cards sit in, and the ones holding either wording of a reworded pair
    # (see _reworded_backup_decks).
    proceed, backed_up = _pre_sync_backup_or_confirm_skip(
        cfg["export_deck"],
        [d["name"] for d in todo]
        + _content_backup_decks([v for v in downloaded.values() if _is_local(v)],
                                aliases, cfg["scope_tag"])
        + _reconcile_backup_decks(fresh, moves, stranded, existing_nids)
        + _reworded_backup_decks(manifest.get("superseded_fronts", {}), cfg["scope_tag"],
                                 skipped, skipped_retired_guids),
        cfg["scope_tag"])
    if not proceed:
        # As with the retry cancel above: report what the learner decided.
        _finish(run_decisions=run_decisions, nothing_note=early_exit_note)
        return

    # Asked once for the whole run, after the backup and before the first import, so
    # nothing interrupts the apply loop.
    convertible, beside = split_notetype_changes(pending_conversions)
    convert = bool(convertible) and _ask(
        _conversion_question(
            convertible, beside,
            "Choosing to import them as new still imports them, as separate new cards "
            "beside the ones you have, leaving your progress on the old versions."),
        yes_label="Move my cards across", no_label="Import them as new")
    if beside and not convertible:
        _info(_kept_beside_notice(beside))

    results, restored, tpl_changes, converted = [], 0, {}, 0
    if todo:
        # A cancellable progress window while each deck imports; the preview already
        # covered the downloads.
        starts, total = card_progress(todo)
        with cancellable_progress("Updating decks", total,
                                  cancel_text="Stop after this deck") as step:
            def _already_fetched(d):
                # Reuses the preview's download. A deck the preview couldn't fetch is
                # fetched again here, with Cancel live through step.pump; a second
                # failure raises into _run_sync's per-deck try/except. A file swept from
                # the temp directory counts as not fetched (_is_local).
                v = downloaded.get(d["name"])
                return v if _is_local(v) else _cached_fetch(fetch, d,
                                                            on_chunk=_finish_deck)

            # "Stop after this deck": a retry finishes its download once started.
            def _finish_deck(_n=None):
                step.pump()
                return True
            _finish_deck.part = lambda done, of, text=None: step.pump.part(
                done, of, text) or True

            # Taking the deck's version only means something on a card that applies.
            take_deck -= {(v.guid, n) for item in items if item[0] == "card"
                          and decisions.get(item[2]["guid"])
                          for n, v in (item[2].get("was") or {}).items()
                          if isinstance(v, KeptEdit)}
            (results, restored, tpl_changes, _deferred, cancelled, collisions,
             converted) = _run_sync(
                cfg, manifest, _already_fetched, todo,
                # step() shows one less than it is given.
                on_progress=lambda i, n, name: step(starts[i - 1] + 1,
                                                    f"Applying {name} ({i} of {n})",
                                                    span=_share(starts, total, i)),
                convert_notetypes=convert, undisclosed_conversions=undisclosed,
                take_deck=take_deck)
            # An edit the learner just gave up is not worth sending.
            edits[:] = [e for e in edits if (e["guid"], e["field"]) not in take_deck]

        if cancelled:
            # Stop here rather than falling through to archive/relocate, which assumes
            # every content update landed. The decks _run_sync finished are fully
            # applied and persisted.
            #
            # A ticked look-change checkbox is honored for those finished decks here:
            # they are recorded as installed, so no later run would offer it.
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
                    ("note", f"Kept your own edits on {plural(restored, 'card')}."))
            if looks:
                items.append(("note", "The new card look was applied for the decks "
                                      "that finished before you stopped."))
            items += _converted_items(converted)
            # Decks applied before the cancel can have collided with the learner's
            # edits, so a stopped run reports those too.
            items += _collision_items(collisions)
            items += _adopted_items(edits)
            items += held_items
            items.append(("note", backup_line))
            _finish(f"Update stopped early (source: {source})", items, run_decisions)
            return
        # Consented to on the confirmation, so nothing is normally asked here; a change
        # the confirmation never named is the exception (see _apply_consented_look).
        _apply_consented_look(tpl_changes, tpl_choice, pending_templates)

    n_archived = n_moved = carried = n_merged = 0
    if fresh or moves or stranded:
        # Refetched after the sync: it may have imported a retired card's replacement
        # for the first time, and carry_over_protected_fields needs that note's nid.
        existing_nids = _existing_guid_to_nid(cfg["scope_tag"])
        # Recomputed against the post-sync collection too: the sync can create a
        # stranding by importing a reworded front as a second note, and it is merged in
        # the same run. Filtered against the decks the learner has unchecked, as
        # _reconcile_pending filters its own.
        existing_decks = _existing_guid_to_deck(cfg["scope_tag"])
        # Same generated-card guard as _reconcile_pending, on both halves of a
        # superseded_fronts pair.
        superseded = manifest.get("superseded_fronts", {})
        fronts_now = _existing_front_to_guid(cfg["scope_tag"])
        pairs = find_stranded_pairs(superseded, fronts_now)
        if pairs:
            try:
                pairs = find_stranded_pairs(superseded, fronts_now, *live_cards())
            except ProgressCancelled:
                pairs = []      # merged on a later run, once every deck has been read
        stranded = [p for p in pairs
            if p["guid"] in existing_nids and p["successor_guid"] in existing_nids
            and p["guid"] not in skipped_retired_guids
            and p["successor_guid"] not in skipped_retired_guids
            and not is_generated_guid(p["guid"])
            and not is_generated_guid(p["successor_guid"])
            and tag not in mw.col.get_note(existing_nids[p["guid"]]).tags
            and not _deck_opted_out(existing_decks.get(p["guid"]), cfg["excluded"])
            and not _deck_opted_out(existing_decks.get(p["successor_guid"]), cfg["excluded"])
            and not _deck_opted_out(existing_decks.get(p["successor_guid"]), skipped)]
        # The one-card-one-outcome rule again: a card this recompute pairs may already
        # be in `fresh`, and the merge below archives it.
        merged_guids = {p["guid"] for p in stranded}
        waiting = [r for r in fresh if r["guid"] in existing_nids
                   and not all(g in existing_nids for g in r.get("superseded_by", []))]
        fresh = [r for r in fresh if r["guid"] not in merged_guids
                 and r["guid"] in existing_nids
                 and all(g in existing_nids for g in r.get("superseded_by", []))]
        per_note = manifest.get("note_protected_fields", {})
        carried = carry_over_protected_fields(fresh, existing_nids, cfg["protected"], per_note)
        n_merged = _merge_stranded(stranded, existing_nids, cfg["protected"], retired_deck, tag,
                                   per_note)
        n_archived = archive_notes([existing_nids[r["guid"]] for r in fresh], retired_deck, tag)
        n_moved = apply_deck_moves(moves, existing_nids)
        mw.reset()
        _refresh_reconcile_action_label(len(waiting))
        if waiting:
            results.append(f"• Kept {plural(len(waiting), 'older card')} in review "
                           "because their replacements have not been imported.")

    result_lines = list(results)
    if n_archived:
        result_lines.append(
            f"✓ Archived <b>{plural(n_archived, 'retired card')}</b> to "
            f"<b>{_text(retired_deck)}</b>"
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
        items.append(("note", f"Kept your own edits on {plural(restored, 'card')}."))
    items += _converted_items(converted)
    items += _collision_items(collisions)
    items += _adopted_items(edits)
    items += held_items
    items.append(("note", backup_line))
    _finish(f"Update complete (source: {source})", items, run_decisions)


@_safe
@_manual_flow
def import_single():
    """Import one hand-picked, spec-authored .apkg outside the configured source.

    Does the same personalization, backup and note-restore as Sync, in _run_sync's
    order: confirm, back up, fix note types, then offer any note-type conversion.
    _ensure_notetypes and a conversion both bump the collection schema, so neither may
    run before the user confirms the import.
    """
    cfg = _cfg()
    src = getFile(mw, "Choose an Intern Pearls .apkg", cb=None,
                  filter="*.apkg", key="internpearls")
    if not src:
        return
    if isinstance(src, (list, tuple)):
        src = src[0]
    aliases = {}
    per_note = {}
    try:
        manifest, _, _ = _fetch_manifest(cfg)
        if manifest:
            aliases = manifest.get("front_aliases", {})
            per_note = manifest.get("note_protected_fields", {})
    except Exception as e:
        if not _ask(f"Couldn't fetch the reworded-front list from your deck source "
                    f"({_text(e)}).<br><br>Without it, any card whose front text "
                    "changed there "
                    "will be treated as new instead of matching your existing card, "
                    "so its history won't carry over, and per-card field protection "
                    "will be off for this import. Continue anyway?",
                    yes_label="Import without it", no_label="Cancel"):
            return
    existing_fronts = _existing_front_to_guid(cfg["scope_tag"])
    remap, in_place, as_new, _, matched = remap_cards(src, existing_fronts, aliases)
    # note_protected_fields is keyed by the guid the deck source assigns, but a note
    # matched by front keeps its own guid. Re-key onto that guid so _snapshot below
    # covers it, as _run_sync does.
    per_note = {**per_note, **per_note_for_package(per_note, matched)}
    # Filtered as _apply_deck filters a sync's import: a declined note must not land
    # through this path either.
    declined = declined_guids(load_declined())
    drop, touched, in_place, as_new = declined_drop(src, remap, existing_fronts, declined,
                                                     in_place, as_new)
    # A hand-picked file can carry a note-type conversion like a synced deck: same plan,
    # same consent wording, same missing-target guard as the sync path.
    nt = notetype_changes(src, existing_fronts, aliases, cfg["scope_tag"], declined)
    missing = missing_notetype_targets(nt)
    if nt and not missing:
        convertible, beside = split_notetype_changes(nt)
        format_line = (f" {plural(len(nt), 'card')} changed format {_FORMAT_CHANGE}."
                       + (f" {_kept_beside_text(beside, bool(convertible))}"
                          if beside else "")
                       + (" You'll be asked whether to move "
                          f"{'the others' if beside else 'your existing cards'} across, "
                          "which is what keeps the history on those."
                          if convertible else ""))
    elif nt:
        format_line = (f" {plural(len(nt), 'card')} changed format {_FORMAT_CHANGE}, "
                       "and your collection has no "
                       f"{_text(', '.join(missing))} note type yet, so the history on "
                       f"{'that one' if len(nt) == 1 else 'those'} can't carry over "
                       "this time.")
    else:
        format_line = ""
    if not _ask(f"{plural(in_place, 'card')} will keep "
                f"{'its' if in_place == 1 else 'their'} history, {as_new} will be added "
                f"as new.{format_line} A backup is taken automatically first. "
                "Import now?",
                yes_label="Import", no_label="Cancel"):
        return
    # Scoped from the file's own deck names, so cards filed outside export_deck are
    # backed up where they will land. A package this build can't read (a modern,
    # zstd-compressed export) falls back to the configured deck.
    try:
        file_decks = apkg_deck_names(src)
    except Exception:
        file_decks = None
    if not _pre_sync_backup_or_confirm_skip(cfg["export_deck"], file_decks,
                                            cfg["scope_tag"])[0]:
        return
    _ensure_notetypes()
    tpl = _template_changes(src)
    baseline = _load_shipped()
    before = _existing_guid_to_fields(cfg["scope_tag"])
    edited = _hand_edits(cfg, per_note, before, baseline)
    snap = _snapshot(cfg["protected"], cfg["scope_tag"],
                     with_edits_protected(per_note, ((g, f) for g, f, _h, _v in edited)))
    tag_snap = _snapshot_tags(cfg["scope_tag"])
    # After the snapshot and before the import, as in _run_sync: on the right note type
    # the import matches by GUID and updates in place, and the snapshot survives the
    # conversion's field map. Nothing is asked when the target type is absent; the
    # import creates it.
    undo = mw.col.add_custom_undo_entry(IMPORT_UNDO_NAME)
    try:
        forked = []
        changed_nids = ([] if missing else _offer_notetype_changes(
            nt, cfg["protected"],
            on_import_as_new=lambda changes: forked.extend(changes),
            per_note=per_note))
        forked_guids = set()
        if forked:
            existing_fronts, forked_guids = _fork_import_as_new(forked, cfg["scope_tag"])
        expected_conflicts = (_planned_notetype_row_ids(src, existing_fronts, aliases, nt)
                              if missing else frozenset())
        package_matched = []
        started = int(time.time())
        in_place, as_new, touched = _apply_deck(
            src, aliases, existing_fronts, declined,
            expected_conflicting_rids=expected_conflicts, matched_out=package_matched)
        for guid in forked_guids:
            snap.pop(guid, None)
            tag_snap.pop(guid, None)
        # Restore before any later bookkeeping or presentation can fail.
        try:
            hashes = _package_hashes(src, declined, expected_conflicts, package_matched,
                                    touched)
            shipped = with_field_hashes(
                _capture_shipped(cfg["protected"], cfg["scope_tag"], touched, per_note),
                baseline, hashes, touched)
        finally:
            restored, collisions = _restore(snap, baseline, touched)
            _restore_tags(tag_snap, cfg["scope_tag"], touched)
        for guid, f in collisions:
            prior = baseline.get(guid, {}).get(HASH_KEY + f)
            if prior is not None and guid in shipped:
                shipped[guid][HASH_KEY + f] = prior
        seed_converted_siblings(changed_nids)
        changed = changed_since(hashes, before)
        for guid, mod in _note_mods(hashes).items():
            shipped[guid][MOD_KEY] = str(max(mod, started) if guid in changed else mod)
        mw.col.merge_undo_entries(undo)
    except Exception:
        mw.col.merge_undo_entries(undo)
        mw.col.undo()
        raise
    if shipped:
        prior = _load_shipped()
        _restore_undo_steps[_undo_key(undo)] = (
            {}, {guid: (guid in prior, prior.get(guid)) for guid in shipped})
        _save_json(SHIPPED, {**prior, **shipped})
    mw.reset()
    _offer_template_changes(tpl)
    fields_line = (f" Kept your own edits on {plural(restored, 'card')}."
                   if restored else "")
    moved_line = (f" Moved {plural(len(changed_nids), 'card')} to the new format."
                  if changed_nids else "")
    _info(f"Imported {_text(os.path.basename(src))}: {in_place} kept history, {as_new} new."
          f"{moved_line}{fields_line}")
