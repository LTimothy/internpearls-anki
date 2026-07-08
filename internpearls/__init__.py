"""
Intern Pearls Deck Tools: an Anki add-on for history-safe deck sync.

"Sync decks" is the only button most people need. It pulls any changed decks (from the
private GitHub repo, or a local folder) and applies each one so your review history and
personal notes are preserved. Fixing note types and importing a single deck run
automatically as part of Sync and are exposed under "Advanced" only as a fallback.

Before touching the collection, Sync takes its own timestamped backup, so nothing here
depends on the user remembering to export one first.
"""
import datetime
import functools
import json
import os
import socket
import tempfile
import traceback
import urllib.error
import urllib.request

from aqt import mw, gui_hooks
from aqt.qt import (QAction, QApplication, QCheckBox, QDialog, QDialogButtonBox, QFrame,
                    QHBoxLayout, QLabel, QLineEdit, QMenu, QMessageBox, QPushButton,
                    QScrollArea, Qt, QTimer, QVBoxLayout, QWidget)
from aqt.utils import (askUser, getFile, getSaveFile, getText, openLink,
                       showInfo, showWarning, tooltip)

from .logic import (bullets, deck_status, decks_to_update, parse_fields,
                    remap_cards, should_notify_update, version_at_least,
                    write_personalized)

ADDON_VERSION = "0.15.0"   # MAJOR.MINOR.PATCH, see CLAUDE.md "Versioning"
ANKI_REPO = "LTimothy/internpearls-anki"   # public add-on repo (used for self-update)
APP_NAME = "Intern Pearls"   # every dialog's title bar, so it never just says "Anki"
EXPORT_DECK = "Intern Pearls::Intern Custom"   # the deck Export Intern Pearls deck scopes to
DECK_BACKUPS_KEEP = 10   # how many automatic Intern Pearls deck backups to retain
_DIR = os.path.dirname(__file__)

# Anki's add-on manager wipes and re-extracts everything in this folder on every add-on
# update, except a "user_files" subfolder, which it explicitly backs up and restores
# around the reinstall. Our own sync state has to live there or every add-on update
# resets it, making Sync think every deck is new again.
_USER_FILES = os.path.join(_DIR, "user_files")
os.makedirs(_USER_FILES, exist_ok=True)
INSTALLED = os.path.join(_USER_FILES, "installed.json")
# Small add-on state that isn't a user setting (so it doesn't belong in config.json) but
# must still survive an add-on update — currently just which add-on version we've already
# nagged about, so the startup notice fires once per release, not every launch.
STATE = os.path.join(_USER_FILES, "state.json")

AUTO_SYNC_INTERVAL_FLOOR_MIN = 15   # refuse to poll more often than this, however configured

# One-time migration: earlier versions wrote this next to __init__.py, so an add-on
# update would have already wiped it. Move it over if it's still there from a
# same-version reinstall.
for _name in ("installed.json",):
    _old, _new = os.path.join(_DIR, _name), os.path.join(_USER_FILES, _name)
    if os.path.exists(_old) and not os.path.exists(_new):
        try:
            os.rename(_old, _new)
        except OSError:
            pass

TARGET_FIELDS = {
    "Study Deck - Basic":    ["Front", "Back", "Why", "Image", "Tag", "Dosing", "Notes"],
    "Study Deck - Cloze":    ["Text", "Why", "Image", "Dosing", "Notes"],
    "Study Deck - Image ID": ["Image", "Prompt", "Answer", "Why", "Notes"],
}


# --------------------------------------------------------------------------- config
def _cfg():
    c = mw.addonManager.getConfig(__name__) or {}
    return {
        "protected":   c.get("protected_fields", ["Notes"]),
        "scope_tag":   c.get("scope_tag", "InternPearls"),
        "decks_dir":   c.get("decks_dir", ""),
        "gh_repo":     c.get("github_decks_repo", ""),
        "gh_ref":      c.get("github_ref", "main"),
        "gh_token":    c.get("github_token", ""),
        "export_deck": c.get("export_deck", EXPORT_DECK),
        "excluded":    c.get("excluded_decks", []),
        "notify_addon_updates": c.get("notify_addon_updates", True),
        "auto_sync_decks":      c.get("auto_sync_decks", False),
        "auto_sync_interval_minutes": c.get("auto_sync_interval_minutes", 60),
    }


def _load_json(path, default):
    try:
        with open(path, encoding="utf8") as fh:
            return json.load(fh)
    except Exception:
        return default


def _save_json(path, data):
    with open(path, "w", encoding="utf8") as fh:
        json.dump(data, fh, indent=2)


# ---------------------------------------------------------------------------- dialogs
# Thin wrappers so every dialog carries the "Intern Pearls" title (Anki's helpers
# default to the generic "Anki") and list-style messages get real HTML formatting
# instead of hand-indented text. Route any new dialog through these, not the raw
# aqt.utils calls, so a future addition here stays consistent automatically.
def _info(text, **kw):
    kw.setdefault("title", APP_NAME)
    kw.setdefault("textFormat", "rich")
    return showInfo(text, **kw)


def _warn(text, **kw):
    kw.setdefault("title", APP_NAME)
    kw.setdefault("textFormat", "rich")
    return showWarning(text, **kw)


def _ask(text, **kw):
    kw.setdefault("title", APP_NAME)
    return askUser(text, **kw)


def _prompt(text, **kw):
    kw.setdefault("title", APP_NAME)
    return getText(text, **kw)


def _safe(fn):
    """Wrap a menu action so a bug here shows a plain warning dialog instead of
    Anki's raw traceback box. The full traceback still goes to stdout (visible in
    Anki's debug console) for anyone actually trying to fix it; the dialog only needs
    enough for a user to describe what happened.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            print(traceback.format_exc())
            _warn(f"Something went wrong: {e}<br><br>"
                  "If a backup was taken before this ran, Advanced has tools to "
                  "revert to it: Import intern pearls deck or Restore full collection.")
    return wrapper


def _bg_safe(fn):
    """Like `_safe`, but for calls that fire on their own (a startup check, a poll timer)
    rather than from a menu click. A blocking warning dialog is fine for a menu action —
    the user just clicked something and is looking at the screen — but popping one up
    unprompted, possibly mid-review, is jarring. Background failures print to console
    (same as `_safe`) and surface as a transient tooltip instead of a modal.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            print(traceback.format_exc())
            try:
                tooltip(f"Intern Pearls: background check failed ({e})",
                       period=4000, parent=mw)
            except Exception:
                pass
    return wrapper


# ------------------------------------------------------------------------ http/github
# Network calls run on Anki's UI thread, so a slow/unreachable host freezes the app (the
# macOS beachball) for however long the socket takes to give up. First-contact calls
# (the manifest, the version check) use a short timeout so an offline machine or captive
# portal fails fast with a clear dialog instead of hanging. Only the large .apkg
# downloads — reached only after first contact already proved we're online — get a
# generous timeout so a big deck on a slow link isn't cut off mid-transfer.
_CONNECT_TIMEOUT = 6     # seconds; fail-fast bound for reaching the source at all
_DOWNLOAD_TIMEOUT = 60   # seconds; per-read bound for pulling a deck once we're online


def _http_get(url, token=None, accept=None, timeout=_CONNECT_TIMEOUT):
    """GET `url`, raising a plain RuntimeError with an actionable message on failure.

    Every network call in this add-on goes through here, so this is the one place that
    needs to turn urllib's exceptions into something a non-technical error dialog can
    show as-is, rather than a Python traceback repr.
    """
    headers = {"User-Agent": "internpearls-addon"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise RuntimeError(
                "access denied (check that your token is valid and can read this "
                "repo)") from e
        if e.code == 404:
            raise RuntimeError(
                "not found (check the repo name, branch, and file path)") from e
        raise RuntimeError(f"server returned HTTP {e.code}") from e
    except (TimeoutError, socket.timeout) as e:
        # Bare socket timeout (isn't always wrapped in URLError); surface it fast.
        raise RuntimeError(
            "the network isn't responding (timed out). Check your internet connection "
            "and try again.") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"couldn't reach the network ({e.reason})") from e


def _gh_raw(repo, path, token, ref, timeout=_CONNECT_TIMEOUT):
    """Raw bytes of a file in a (possibly private) repo via the contents API."""
    url = f"https://api.github.com/repos/{repo}/contents/{path}?ref={ref}"
    return _http_get(url, token=token, accept="application/vnd.github.raw",
                     timeout=timeout)


# ---------------------------------------------------------------------- note types
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
    """
    if mw.col.decks.id_for_name(deck_name) is None:
        return True   # nothing to back up yet, e.g. someone's very first sync
    if _backup_deck(deck_name):
        return True
    return _ask("Couldn't create an automatic backup.\n\n"
                "Proceed anyway? (You can back up manually first: Advanced → Backup "
                "intern pearls deck, or Advanced → Backup full collection.)")


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


# ------------------------------------------------------------------- deck source
def _fetch_manifest(cfg):
    """Return (manifest, fetch_apkg) where fetch_apkg(deck) -> local .apkg path."""
    if cfg["gh_token"] and cfg["gh_repo"]:
        manifest = json.loads(_gh_raw(cfg["gh_repo"], "manifest.json",
                                      cfg["gh_token"], cfg["gh_ref"]))

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


# ------------------------------------------------------------------------- actions
@_safe
def sync_decks():
    cfg = _cfg()
    try:
        manifest, fetch, source = _fetch_manifest(cfg)
    except Exception as e:
        _warn(f"Couldn't reach the deck source: {e}<br><br>"
              "Check your GitHub token or local folder under Configure deck source.")
        return
    if not manifest:
        _warn("No deck source configured yet.<br><br>"
              "Run <b>Intern Pearls → Configure deck source</b> first.")
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
    if not _pre_sync_backup_or_confirm_skip(cfg["export_deck"]):
        return

    results, restored = _run_sync(cfg, manifest, fetch, todo, installed)
    notes_line = f"Notes restored on {restored} card(s).<br><br>" if restored else ""
    _info(f"<b>Sync complete</b> (source: {source})" + bullets(results) +
          notes_line +
          f"A pre-sync backup of the Intern Pearls deck was saved; use "
          f"<i>Advanced → Import intern pearls deck</i> to revert to it if needed.")


def _run_sync(cfg, manifest, fetch, todo, installed):
    """Apply every deck in `todo`: fix note types, snapshot protected fields, remap and
    import each deck (keeping the learner's scheduling), restore the snapshotted fields,
    and persist the new installed versions.

    The caller must already have confirmed (if interactive) and taken a backup — this is
    the one place the actual history-preserving sequence lives, shared by the interactive
    Sync decks flow and the unattended auto-sync poll, so there's exactly one
    implementation of the part that matters for not losing anyone's review history.
    Returns (results, restored): per-deck outcome lines and the note-restore count.
    """
    aliases = manifest.get("front_aliases", {})   # from the (private) manifest, not config
    _ensure_notetypes()
    snap = _snapshot(cfg["protected"], cfg["scope_tag"])
    her = _her_front_to_guid(cfg["scope_tag"])
    results = []
    for d in todo:
        short = d["name"].split("::")[-1]
        try:
            src = fetch(d)
            in_place, as_new = _apply_deck(src, aliases, her)
            installed[d["name"]] = d["version"]
            results.append(f"✓ <b>{short}</b>: {in_place} kept history, {as_new} new")
        except Exception as e:
            results.append(f"✗ <b>{short}</b>: {e}")
    _save_json(INSTALLED, installed)
    restored = _restore(snap)
    mw.reset()
    return results, restored


def _fetch_addon_version_info():
    """Fetch and parse the public add-on repo's version.json. Raises on any failure.

    Single source of truth for this URL/parse so the manual "Check for add-on updates"
    action and the silent startup notifier can't drift apart.
    """
    url = f"https://raw.githubusercontent.com/{ANKI_REPO}/main/version.json"
    return json.loads(_http_get(url))


@_safe
def check_updates():
    """Compare our version to version.json in the public add-on repo; offer to update."""
    try:
        latest = _fetch_addon_version_info()
    except Exception as e:
        _warn(f"Couldn't check for updates: {e}")
        return

    if version_at_least(ADDON_VERSION, latest.get("version", "0")):
        _info(f"Intern Pearls Deck Tools is up to date (v{ADDON_VERSION}).")
        return
    if not _ask(f"Update available: v{latest['version']} "
                f"(you have v{ADDON_VERSION}). Download and install now?"):
        return
    try:
        data = _http_get(latest["download"])
        tmp = os.path.join(tempfile.gettempdir(), "internpearls.ankiaddon")
        with open(tmp, "wb") as fh:
            fh.write(data)
        mw.addonManager.install(tmp)
        _info("Updated. Please restart Anki.")
    except Exception as e:
        _warn(f"Auto-install failed ({e}).<br>Opening the download page instead.")
        openLink(f"https://github.com/{ANKI_REPO}")


# --------------------------------------------------------------- background checks
# Two things run on their own, without a menu click: a one-shot "is a newer add-on
# version out" notice shortly after startup, and (only if the user opted in via Manage
# decks) a repeating poll that auto-syncs decks. Both are deliberately synchronous on
# the main thread rather than on a worker thread — this add-on has no existing
# threading, and adding one just for a startup notice would trade a rare, small,
# bounded hitch (capped by the fail-fast timeouts) for real thread-safety surface
# against Qt/`mw.col`. The trade-off is documented here rather than hidden.

@_bg_safe
def _check_addon_update_silently():
    """Startup, non-blocking: tooltip if a newer add-on version exists and we haven't
    already notified about it. Never auto-installs the add-on itself — that stays the
    explicit, user-confirmed "Check for add-on updates" action; this only makes sure the
    user finds out a new version exists instead of silently running a stale build.
    """
    if not _cfg()["notify_addon_updates"]:
        return
    try:
        latest = _fetch_addon_version_info()
    except Exception:
        return   # offline / GitHub hiccup at startup — stay quiet, try again next launch
    latest_version = latest.get("version", "")
    state = _load_json(STATE, {})
    if not should_notify_update(ADDON_VERSION, latest_version,
                                state.get("last_notified_addon_version")):
        return
    state["last_notified_addon_version"] = latest_version
    _save_json(STATE, state)
    tooltip(
        f"Intern Pearls Deck Tools v{latest_version} is available (you have "
        f"v{ADDON_VERSION}). Intern Pearls → Check for add-on updates to install.",
        period=8000, parent=mw)


_auto_sync_in_progress = False


@_bg_safe
def _auto_sync_check():
    """Timer-triggered: if auto-sync is on and any deck changed, apply it without asking.

    A backup is still taken first — non-negotiable, same as the interactive path — and
    if the backup fails, this aborts rather than importing unprotected (there's no user
    present to ask). The outcome is a transient tooltip, never a blocking dialog, since
    this can fire while the user is mid-review; a dialog popping up uninvited over a
    review session would be worse than the sync just quietly happening.
    """
    global _auto_sync_in_progress
    cfg = _cfg()
    if not cfg["auto_sync_decks"] or _auto_sync_in_progress or mw.col is None:
        return
    try:
        manifest, fetch, source = _fetch_manifest(cfg)
    except Exception:
        return   # offline / misconfigured source — stay quiet, retry on the next poll
    if not manifest:
        return
    installed = _load_json(INSTALLED, {})
    todo = decks_to_update(manifest, installed, cfg["excluded"])
    if not todo:
        return
    if not _pre_sync_backup_or_skip_silently(cfg["export_deck"]):
        tooltip("Intern Pearls: auto-sync skipped — couldn't create a backup first.",
               period=6000, parent=mw)
        return

    _auto_sync_in_progress = True
    try:
        results, restored = _run_sync(cfg, manifest, fetch, todo, installed)
    finally:
        _auto_sync_in_progress = False

    ok = sum(1 for r in results if r.startswith("✓"))
    fail = len(results) - ok
    msg = f"Intern Pearls: auto-synced {ok} deck(s) (source: {source})"
    if fail:
        msg += f", {fail} failed — open Sync decks for details"
    if restored:
        msg += f", notes restored on {restored} card(s)"
    tooltip(msg, period=6000, parent=mw)


_auto_sync_timer = None


def _stop_auto_sync_timer():
    global _auto_sync_timer
    if _auto_sync_timer is not None:
        _auto_sync_timer.stop()
        _auto_sync_timer = None


def _restart_auto_sync_timer(minutes):
    """(Re)start the repeating poll at `minutes`, floored so it can't be configured into
    a busy-loop. GitHub load at this cadence is trivial: one manifest.json GET per
    interval (a few KB), well under even the unauthenticated 60-requests/hour limit at
    the floor, let alone the 5000/hour a token gets — polling every 15 minutes all day
    is ~96 requests, nothing GitHub notices.
    """
    _stop_auto_sync_timer()
    global _auto_sync_timer
    interval_ms = max(AUTO_SYNC_INTERVAL_FLOOR_MIN, int(minutes or 60)) * 60 * 1000
    _auto_sync_timer = QTimer(mw)
    _auto_sync_timer.timeout.connect(_auto_sync_check)
    _auto_sync_timer.start(interval_ms)


def _schedule_background_checks():
    """Run once, a couple seconds after Anki finishes starting up: a silent add-on-update
    notice, and — only if the user turned on auto-sync in Manage decks — an immediate
    deck check plus the repeating poll that keeps checking while Anki stays open.
    """
    QTimer.singleShot(2000, _check_addon_update_silently)
    cfg = _cfg()
    if cfg["auto_sync_decks"]:
        QTimer.singleShot(4000, _auto_sync_check)
        _restart_auto_sync_timer(cfg["auto_sync_interval_minutes"])


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
    if not _pre_sync_backup_or_confirm_skip(cfg["export_deck"]):
        return
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
    notes_line = f" Notes restored on {restored} card(s)." if restored else ""
    _info(f"Imported {os.path.basename(src)}: {in_place} kept history, {as_new} new."
          f"{notes_line}")


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
    if not _pre_sync_backup_or_confirm_skip(_cfg()["export_deck"]):
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


@_safe
def configure_source():
    """Set where decks come from: a GitHub repo + read-only token, or a local folder."""
    conf = mw.addonManager.getConfig(__name__) or {}

    box = QMessageBox(mw)
    box.setWindowTitle(f"{APP_NAME}: Configure deck source")
    box.setIcon(QMessageBox.Icon.Question)
    box.setText("Where should decks come from?")
    gh_btn = box.addButton("GitHub repo", QMessageBox.ButtonRole.AcceptRole)
    local_btn = box.addButton("Local folder", QMessageBox.ButtonRole.AcceptRole)
    box.addButton(QMessageBox.StandardButton.Cancel)
    box.exec()
    clicked = box.clickedButton()

    if clicked is gh_btn:
        repo, ok = _prompt("GitHub decks repo, as owner/name:",
                           default=conf.get("github_decks_repo", ""))
        if not ok or not repo.strip():
            return
        token_edit = QLineEdit()
        token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        token, ok = _prompt("Read-only access token (hidden as you type):",
                            edit=token_edit, default=conf.get("github_token", ""))
        if not ok:
            return
        conf["github_decks_repo"] = repo.strip()
        conf["github_token"] = token.strip()
        conf["decks_dir"] = ""
    elif clicked is local_btn:
        path, ok = _prompt("Folder with manifest.json + .apkg files:",
                          default=conf.get("decks_dir", ""))
        if not ok or not path.strip():
            return
        conf["decks_dir"] = path.strip()
        conf["github_token"] = ""
    else:
        return  # Cancel, or the dialog was closed

    mw.addonManager.writeConfig(__name__, conf)

    try:
        manifest, _, source = _fetch_manifest(_cfg())
    except Exception as e:
        _warn(f"Saved, but couldn't connect: {e}<br><br>"
              "Double-check the repo name and token (or folder path), then run "
              "<i>Intern Pearls → Configure deck source</i> again.")
        return
    if not manifest:
        _warn("Saved, but nothing was found at that source yet. Check the path "
              "or repo and try again.")
        return
    _info(f"Saved and connected to <b>{source}</b>, found {len(manifest['decks'])} "
          "deck(s).<br><br>Run <i>Intern Pearls → Sync decks</i> whenever you're ready.")


# -------------------------------------------------------------------- deck manager
# Colors for a deck's sync-state pill. Deliberately readable on both Anki themes: a
# saturated mid-tone reads fine on light and dark backgrounds alike, so we don't need to
# branch on night mode.
_STATE_STYLE = {
    "new":     ("New",              "#2563eb"),
    "update":  ("Update available", "#b45309"),
    "current": ("Up to date",       "#6b7280"),
}


class _DeckManagerDialog(QDialog):
    """Pick which decks sync and which fields are preserved, in one clean panel.

    Pure-ish view: it's handed already-computed rows (from logic.deck_status) and just
    renders checkboxes + status pills, then hands back the user's choices via
    excluded_decks()/protected_fields()/auto_sync_enabled(). No network or collection
    access lives here.
    """

    def __init__(self, parent, rows, protected, source, preview_fn, auto_sync):
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME}: Manage decks")
        self.setMinimumWidth(480)
        self.sync_requested = False
        self._rows = rows
        self._preview_fn = preview_fn   # callable() -> {deck_name: (kept, new) | None}
        self._checks = {}   # deck name -> QCheckBox
        self._pills = {}    # deck name -> QLabel (status pill, updated after a preview)

        outer = QVBoxLayout(self)
        outer.setSpacing(10)

        title = QLabel("Manage decks")
        title.setStyleSheet("font-size: 17px; font-weight: 600;")
        outer.addWidget(title)
        sub = QLabel(f"Source: {source}. Check the decks you want to keep synced. "
                     "Unchecking one stops future syncs for it; cards already imported "
                     "stay in your collection until you delete them in Anki.")
        sub.setWordWrap(True)
        sub.setStyleSheet("color: gray;")
        outer.addWidget(sub)

        bar = QHBoxLayout()
        for label, val in (("Select all", True), ("Select none", False)):
            b = QPushButton(label)
            b.setFlat(True)
            b.setStyleSheet("color: #2563eb; text-align: left;")
            b.clicked.connect(lambda _=False, v=val: self._set_all(v))
            bar.addWidget(b)
        bar.addStretch()
        self._preview_btn = QPushButton("Check what will sync")
        self._preview_btn.setFlat(True)
        self._preview_btn.setStyleSheet("color: #2563eb;")
        self._preview_btn.setToolTip(
            "Download the changed decks and show, per deck, how many cards would update "
            "in place (history kept) vs. be added as new. Nothing is imported.")
        self._preview_btn.clicked.connect(self._run_preview)
        # Nothing to preview if every deck already matches what's installed — reflect
        # that on open so the button isn't an inviting dead end when all rows say
        # "up to date".
        if not any(r["state"] != "current" for r in rows):
            self._preview_btn.setText("All decks up to date")
            self._preview_btn.setEnabled(False)
            self._preview_btn.setStyleSheet("color: gray;")
        bar.addWidget(self._preview_btn)
        outer.addLayout(bar)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setMinimumHeight(230)
        holder = QWidget()
        col = QVBoxLayout(holder)
        col.setSpacing(6)
        col.setContentsMargins(0, 0, 6, 0)
        if rows:
            for r in rows:
                col.addWidget(self._deck_row(r))
        else:
            empty = QLabel("No decks found at this source yet.")
            empty.setStyleSheet("color: gray;")
            col.addWidget(empty)
        col.addStretch()
        scroll.setWidget(holder)
        outer.addWidget(scroll, 1)

        pf = QLabel("Preserved fields")
        pf.setStyleSheet("font-weight: 600;")
        outer.addWidget(pf)
        self._pf_edit = QLineEdit(", ".join(protected))
        self._pf_edit.setPlaceholderText("Notes")
        outer.addWidget(self._pf_edit)
        pf_hint = QLabel("Comma-separated fields holding your own annotations. Sync "
                         "snapshots and restores them, so importing an updated deck "
                         "never overwrites what you've written.")
        pf_hint.setWordWrap(True)
        pf_hint.setStyleSheet("color: gray; font-size: 11px;")
        outer.addWidget(pf_hint)

        self._auto_cb = QCheckBox("Automatically sync when updates are available")
        self._auto_cb.setChecked(auto_sync)
        self._auto_cb.setStyleSheet("font-weight: 600; margin-top: 6px;")
        outer.addWidget(self._auto_cb)
        auto_hint = QLabel("Checks the source about once an hour while Anki is open and "
                           "applies deck updates without asking — a backup is still "
                           "taken first, same as a manual sync. Off by default; turn "
                           "this on once you trust the flow.")
        auto_hint.setWordWrap(True)
        auto_hint.setStyleSheet("color: gray; font-size: 11px;")
        outer.addWidget(auto_hint)

        apply_hint = QLabel("Save keeps these choices for your next sync. Save and sync "
                            "now also pulls the selected decks right away.")
        apply_hint.setWordWrap(True)
        apply_hint.setStyleSheet("color: gray; font-size: 11px; margin-top: 4px;")
        outer.addWidget(apply_hint)

        bb = QDialogButtonBox()
        save = bb.addButton("Save", QDialogButtonBox.ButtonRole.AcceptRole)
        sync = bb.addButton("Save and sync now", QDialogButtonBox.ButtonRole.ApplyRole)
        bb.addButton(QDialogButtonBox.StandardButton.Cancel)
        save.clicked.connect(self.accept)
        sync.clicked.connect(self._save_and_sync)
        bb.rejected.connect(self.reject)
        outer.addWidget(bb)

    def _deck_row(self, r):
        row = QFrame()
        row.setObjectName("deckRow")
        row.setStyleSheet(
            "#deckRow { border: 1px solid rgba(128,128,128,0.35); border-radius: 6px; }")
        h = QHBoxLayout(row)
        h.setContentsMargins(11, 8, 11, 8)
        cb = QCheckBox(r["short"])
        cb.setChecked(r["enabled"])
        cb.setStyleSheet("font-weight: 600;")
        self._checks[r["name"]] = cb
        h.addWidget(cb)
        h.addStretch()
        label, color = _STATE_STYLE[r["state"]]
        cards = r.get("cards")
        text = f'{cards} cards · {label}' if cards is not None else label
        pill = QLabel(text)
        pill.setStyleSheet(f"color: {color}; font-size: 12px;")
        self._pills[r["name"]] = pill
        h.addWidget(pill)
        return row

    def _run_preview(self):
        """Download the changed decks and fill in each row's kept/new breakdown.

        Read-only: it counts how incoming cards match the collection, but imports
        nothing. Runs on click (not on open) so opening the panel stays fast.
        """
        pending = [r for r in self._rows if r["state"] != "current"]
        if not pending:
            self._preview_btn.setText("All up to date")
            self._preview_btn.setEnabled(False)
            return
        self._preview_btn.setText("Checking…")
        self._preview_btn.setEnabled(False)
        QApplication.processEvents()   # repaint the button before the blocking fetch
        try:
            result = self._preview_fn()   # {deck_name: (kept, new) | None}
        except Exception as e:
            self._preview_btn.setText("Preview failed")
            self._preview_btn.setEnabled(True)
            self._preview_btn.setToolTip(str(e))
            return
        for r in pending:
            pill = self._pills.get(r["name"])
            if pill is None:
                continue
            rc = result.get(r["name"])
            if rc is None:
                pill.setText("couldn't preview")
                pill.setStyleSheet("color: #b45309; font-size: 12px;")
            else:
                kept, new = rc
                pill.setText(f"{kept} kept · {new} new")
                pill.setStyleSheet("color: #6b7280; font-size: 12px;")
        self._preview_btn.setText("Preview updated")

    def _set_all(self, val):
        for cb in self._checks.values():
            cb.setChecked(val)

    def _save_and_sync(self):
        self.sync_requested = True
        self.accept()

    def excluded_decks(self):
        return [name for name, cb in self._checks.items() if not cb.isChecked()]

    def protected_fields(self):
        return parse_fields(self._pf_edit.text())

    def auto_sync_enabled(self):
        return self._auto_cb.isChecked()


@_safe
def manage_decks():
    """Open the deck manager: choose which decks sync and which fields are preserved."""
    cfg = _cfg()
    try:
        manifest, fetch, source = _fetch_manifest(cfg)
    except Exception as e:
        _warn(f"Couldn't reach the deck source: {e}<br><br>"
              "Check your GitHub token or local folder under Configure deck source.")
        return
    if not manifest:
        _warn("No deck source configured yet.<br><br>"
              "Run <b>Intern Pearls → Configure deck source</b> first.")
        return

    installed = _load_json(INSTALLED, {})
    rows = deck_status(manifest, installed, cfg["excluded"])

    def _preview():
        """Download every changed deck and match it against the collection, returning
        {deck_name: (kept_in_place, added_new)}. Read-only — imports nothing. Runs when
        the user clicks "Check what will sync"; the download is why it's on demand."""
        pending = [d for d in manifest["decks"]
                   if installed.get(d["name"]) != d["version"]]
        her = _her_front_to_guid(cfg["scope_tag"])
        aliases = manifest.get("front_aliases", {})
        out = {}
        for d in pending:
            src = None
            try:
                src = fetch(d)
                _, kept, new = remap_cards(src, her, aliases)
                out[d["name"]] = (kept, new)
            except Exception:
                out[d["name"]] = None
            finally:
                if src and source == "GitHub":   # only delete our temp download
                    try:
                        os.remove(src)
                    except OSError:
                        pass
        return out

    dlg = _DeckManagerDialog(mw, rows, cfg["protected"], source, _preview,
                             cfg["auto_sync_decks"])
    if not dlg.exec():
        return   # cancelled

    conf = mw.addonManager.getConfig(__name__) or {}
    conf["excluded_decks"] = dlg.excluded_decks()
    conf["protected_fields"] = dlg.protected_fields()
    conf["auto_sync_decks"] = dlg.auto_sync_enabled()
    mw.addonManager.writeConfig(__name__, conf)

    # Apply the auto-sync toggle immediately rather than waiting for a restart, so
    # turning it on/off in the panel actually takes effect right away.
    if conf["auto_sync_decks"]:
        _restart_auto_sync_timer(_cfg()["auto_sync_interval_minutes"])
    else:
        _stop_auto_sync_timer()

    if dlg.sync_requested:
        sync_decks()
        return
    kept = sum(1 for r in rows if r["name"] not in conf["excluded_decks"])
    excluded_n = len(rows) - kept
    scope = (f"All {kept} deck(s) are set to sync" if not excluded_n
             else f"{kept} of {len(rows)} deck(s) are set to sync ({excluded_n} excluded)")
    auto_line = (" Auto-sync is on, so these will keep applying on their own."
                if conf["auto_sync_decks"] else
                " Nothing synced yet — run <b>Sync decks</b> when you're ready to pull "
                "them (or use <i>Save and sync now</i> next time to do both at once).")
    _info(f"Saved. {scope}, preserving {', '.join(conf['protected_fields'])}."
          f"<br><br>{auto_line}")


@_safe
def about():
    box = QMessageBox(mw)
    box.setWindowTitle(f"{APP_NAME}: About")
    box.setIcon(QMessageBox.Icon.Information)
    box.setTextFormat(Qt.TextFormat.RichText)
    box.setText(
        f"<b>Intern Pearls Deck Tools</b> &nbsp;<span style='color:gray;'>v{ADDON_VERSION}"
        "</span><br><br>"
        "Keeps Anki decks in sync with a source you control, without losing review "
        "history or personal notes: cards are matched by ID, personal note fields are "
        "snapshotted and restored around every import, and backups run automatically "
        "before anything changes."
        "<br><br>No deck content ships with the add-on itself. Set your source under "
        "<i>Configure deck source</i>, a GitHub repo or a local folder."
        "<br><br>"
        f'<a href="https://github.com/{ANKI_REPO}">github.com/{ANKI_REPO}</a>')
    box.setStandardButtons(QMessageBox.StandardButton.Ok)
    box.exec()


# ---------------------------------------------------------------------------- menu
def _menu():
    menu = QMenu("&Intern Pearls", mw)

    def add(target, label, fn):
        act = QAction(label, mw)
        act.setMenuRole(QAction.MenuRole.NoRole)  # macOS Qt auto-moves items whose label
        act.triggered.connect(lambda checked=False, fn=fn: fn())  # Qt's triggered signal passes a
                                                   # checked bool; discard it since these all take
                                                   # no args and Qt can't introspect through _safe's
                                                   # *args wrapper to know that.
        target.addAction(act)                     # the app menu unless told not to

    add(menu, "Sync decks", sync_decks)
    add(menu, "Manage decks", manage_decks)
    add(menu, "Configure deck source", configure_source)
    add(menu, "Check for add-on updates", check_updates)
    menu.addSeparator()
    adv = menu.addMenu("Advanced")
    add(adv, "Import single deck (manual)", import_single)
    add(adv, "Fix note types", update_notetypes)
    adv.addSeparator()
    add(adv, "Backup intern pearls deck", backup_deck_now)
    add(adv, "Import intern pearls deck", import_deck)
    add(adv, "Export intern pearls deck", export_deck)
    adv.addSeparator()
    add(adv, "Backup full collection", backup_collection_now)
    add(adv, "Restore full collection", restore_from_backup)
    menu.addSeparator()
    add(menu, "About", about)

    try:
        mw.form.menubar.insertMenu(mw.form.menuHelp.menuAction(), menu)
    except Exception:
        mw.form.menuTools.addMenu(menu)


gui_hooks.main_window_did_init.append(_menu)
gui_hooks.main_window_did_init.append(_schedule_background_checks)
