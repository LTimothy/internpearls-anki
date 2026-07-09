"""
Intern Pearls Deck Tools: an Anki add-on for history-safe deck sync.

"Sync decks" is the only button most people need. It pulls any changed decks (from the
private GitHub repo, or a local folder) and applies each one so your review history and
your own annotations in any preserved field are kept. Fixing note types and importing a
single deck run automatically as part of Sync and are exposed under "Advanced" only as
a fallback.

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
                    QScrollArea, QSpinBox, Qt, QTimer, QVBoxLayout, QWidget)
from aqt.utils import (askUser, getFile, getSaveFile, getText, openLink,
                       showInfo, showWarning, tooltip)

# QueryOp is the standard way modern Anki add-ons run work off the main thread. It has
# been part of aqt's public surface since 2.1.45 (2021), which is older than the
# collection APIs this add-on already depends on (ImportAnkiPackageRequest, the
# with_scheduling/wait_for_completion backend options), so it should be present on any
# Anki build that can run this add-on at all. The import is still guarded: if it's ever
# missing, background checks fall back to running inline rather than the whole add-on
# failing to load.
try:
    from aqt.operations import QueryOp
except Exception:
    QueryOp = None

from .logic import (bullets, clamp_interval_minutes, deck_status, decide_addon_update_action,
                    decks_to_update, parse_fields, remap_cards, version_at_least,
                    write_personalized)

ADDON_VERSION = "0.18.0"   # MAJOR.MINOR.PATCH, see README "Versioning"
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

AUTO_SYNC_INTERVAL_FLOOR_MIN = 1     # refuse to poll more often than this, however configured
AUTO_SYNC_INTERVAL_DEFAULT_MIN = 15  # used when the setting is missing or unreadable

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
        "auto_update_addon":    c.get("auto_update_addon", False),
        "auto_sync_decks":      c.get("auto_sync_decks", False),
        "auto_sync_interval_minutes": c.get("auto_sync_interval_minutes",
                                            AUTO_SYNC_INTERVAL_DEFAULT_MIN),
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
# A tighter bound for the two checks that run on their own, unprompted: the deck-sync
# poll and the add-on-update check. These can fire as often as once a minute, so a slow
# or dead host has to fail well before the interactive 6-second bound would. Background
# checks that use QueryOp (see _run_in_background) run this off the main thread anyway,
# so the timeout mostly matters for the fallback path on an Anki build without QueryOp.
_BG_TIMEOUT = 3          # seconds; fail-fast bound for unattended background checks


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


def _gh_public_raw(path, ref="main", timeout=_CONNECT_TIMEOUT):
    """Raw bytes of a file in the public add-on repo, via the Contents API rather than
    raw.githubusercontent.com.

    raw.githubusercontent.com is served through a CDN that can lag well behind a push.
    Confirmed directly: right after pushing a new version.json, the Contents API
    reflected it immediately, while the raw CDN link for the same file and branch still
    served the previous content more than two minutes later. That gap is exactly why
    "Check for add-on updates" once failed to see a version that had already been
    pushed. Anything this add-on fetches about itself now goes through the API instead.
    No token is needed since this repo is public; version.json still lists the raw CDN
    URL under "download" as a convenience for a person opening it by hand, where a
    brief delay is harmless.
    """
    url = f"https://api.github.com/repos/{ANKI_REPO}/contents/{path}?ref={ref}"
    return _http_get(url, accept="application/vnd.github.raw", timeout=timeout)


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


# ------------------------------------------------------------------------- actions
@_safe
def sync_decks():
    cfg = _cfg()
    try:
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

    results, restored = _run_sync(cfg, manifest, fetch, todo, installed)
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


def _fetch_addon_version_info(timeout=_CONNECT_TIMEOUT):
    """Fetch and parse the public add-on repo's version.json via the Contents API (see
    _gh_public_raw for why not the raw CDN link). Raises on any failure.

    Single source of truth for this fetch so the manual "Check for add-on updates"
    action and the background checks can't drift apart.
    """
    return json.loads(_gh_public_raw("version.json", timeout=timeout))


def _download_addon_package(timeout=_DOWNLOAD_TIMEOUT):
    """Download the current .ankiaddon package to a temp file and return its path."""
    data = _gh_public_raw("internpearls.ankiaddon", timeout=timeout)
    path = os.path.join(tempfile.gettempdir(), "internpearls.ankiaddon")
    with open(path, "wb") as fh:
        fh.write(data)
    return path


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
        mw.addonManager.install(_download_addon_package())
        _info("Updated. Please restart Anki.")
    except Exception as e:
        _warn(f"Auto-install failed ({e}).<br>Opening the download page instead.")
        openLink(f"https://github.com/{ANKI_REPO}")


# --------------------------------------------------------------- background checks
# Two things run on their own, without a menu click: an add-on-update check once per
# launch, and (only if the user turned it on in Settings) a repeating poll that auto-
# syncs decks. Both dispatch their network work through _run_in_background, which uses
# Anki's QueryOp to run off the main thread when it's available, so a slow or dead host
# never freezes Anki, however often the poll fires. The only work that touches mw.col
# (backing up and importing, once something actually needs to change) still runs on the
# main thread inside the completion callback, same as it does for a manual Sync decks
# click today; that part is unaffected by this and isn't the part that could hang.

def _run_in_background(work, on_done):
    """Run `work()` off the main thread when possible, then call `on_done(result, error)`
    back on the main thread either way (`error` is None on success, `result` is None on
    failure). `work` must not touch `mw.col` or any Qt widget, since it may run on a
    worker thread; it should be pure computation plus network/file I/O.

    Uses QueryOp when available (the normal case; see the import guard near the top of
    this file) so the caller genuinely never blocks Anki's UI, no matter how often it's
    invoked. Falls back to calling `work()` directly, bounded by whatever timeout `work`
    itself uses, on any Anki build old enough to lack QueryOp.
    """
    def _safe_on_done(result, error):
        try:
            on_done(result, error)
        except Exception:
            print(traceback.format_exc())

    if QueryOp is not None:
        QueryOp(
            parent=mw,
            op=lambda _col: work(),
            success=lambda result: _safe_on_done(result, None),
        ).failure(lambda exc: _safe_on_done(None, exc)).run_in_background()
    else:
        try:
            result = work()
        except Exception as e:
            _safe_on_done(None, e)
        else:
            _safe_on_done(result, None)


def _addon_update_work(auto_update):
    """Background-safe: fetch the public repo's version info, and if `auto_update` is on
    and a newer version exists, also download the package. No Qt or mw.col access, so
    it's safe to run off the main thread. Raises on a fetch failure; the caller decides
    what "stay quiet" means for that.
    """
    info = _fetch_addon_version_info(timeout=_BG_TIMEOUT)
    package_path = None
    latest = info.get("version", "")
    if auto_update and latest and not version_at_least(ADDON_VERSION, latest):
        package_path = _download_addon_package(timeout=_DOWNLOAD_TIMEOUT)
    return {"info": info, "package_path": package_path}


@_bg_safe
def _check_addon_updates_background():
    """Runs once, shortly after Anki starts: fetch the public repo's version info off the
    main thread, then act on it per the Settings toggles (notify only, or auto-install).
    Skips the network call entirely if both toggles are off.
    """
    cfg = _cfg()
    if not (cfg["notify_addon_updates"] or cfg["auto_update_addon"]):
        return

    def _finish(result, error):
        if error or not result:
            return   # offline / GitHub hiccup — stay quiet, try again next launch
        latest = result["info"].get("version", "")
        state = _load_json(STATE, {})
        action = decide_addon_update_action(
            ADDON_VERSION, latest, cfg["auto_update_addon"], cfg["notify_addon_updates"],
            state.get("last_notified_addon_version"))
        if action == "none":
            return
        state["last_notified_addon_version"] = latest
        _save_json(STATE, state)

        if action == "auto_update" and result["package_path"]:
            try:
                mw.addonManager.install(result["package_path"])
                tooltip(f"Intern Pearls Deck Tools updated itself to v{latest}. Restart "
                       "Anki to use it.", period=8000, parent=mw)
            except Exception as e:
                tooltip(f"Intern Pearls: couldn't install v{latest} automatically ({e}). "
                       "Try Advanced → Check for add-on updates.", period=8000, parent=mw)
        else:
            # Either a plain notify, or auto-update was requested but the package
            # didn't download — either way, tell the user a newer version exists rather
            # than doing nothing.
            tooltip(
                f"Intern Pearls Deck Tools v{latest} is available (you have "
                f"v{ADDON_VERSION}). Intern Pearls → Advanced → Check for add-on "
                "updates to install.", period=8000, parent=mw)

    _run_in_background(lambda: _addon_update_work(cfg["auto_update_addon"]), _finish)


_auto_sync_in_progress = False


@_bg_safe
def _auto_sync_check():
    """Timer-triggered: if auto-sync is on and any deck changed, apply it without asking.

    The manifest fetch (the part that runs on every poll, most of the time finding
    nothing new) happens off the main thread via _run_in_background. Backing up and
    importing (the part that only runs when there's actually something to apply) still
    happens on the main thread inside the completion callback, matching the cost a
    manual Sync decks click already pays; only the frequent, usually-empty check is what
    needed to stop blocking Anki. A backup is still taken first, and if it fails, this
    aborts rather than importing unprotected, since there's no user to ask. The outcome
    is always a transient tooltip, never a blocking dialog, since this can fire mid-
    review.
    """
    global _auto_sync_in_progress
    cfg = _cfg()
    if not cfg["auto_sync_decks"] or _auto_sync_in_progress or mw.col is None:
        return

    def _fetch_work():
        # _BG_TIMEOUT, not the interactive default: this fires unattended as often as
        # once a minute, so a dead host must fail well inside the poll interval.
        manifest, fetch, source = _fetch_manifest(cfg, timeout=_BG_TIMEOUT)
        if not manifest:
            return None
        installed = _load_json(INSTALLED, {})
        todo = decks_to_update(manifest, installed, cfg["excluded"])
        if not todo:
            return None
        return {"manifest": manifest, "fetch": fetch, "source": source,
                "todo": todo, "installed": installed}

    def _apply(result, error):
        global _auto_sync_in_progress
        if error or not result:
            return   # offline, misconfigured, or nothing pending — stay quiet
        _auto_sync_in_progress = True
        try:
            if not _pre_sync_backup_or_skip_silently(cfg["export_deck"]):
                tooltip("Intern Pearls: auto-sync skipped, couldn't create a backup "
                       "first.", period=6000, parent=mw)
                return
            results, restored = _run_sync(cfg, result["manifest"], result["fetch"],
                                          result["todo"], result["installed"])
            ok = sum(1 for r in results if r.startswith("✓"))
            fail = len(results) - ok
            msg = f"Intern Pearls: auto-synced {ok} deck(s) (source: {result['source']})"
            if fail:
                msg += f", {fail} failed, open Sync decks for details"
            if restored:
                msg += f", preserved fields restored on {restored} card(s)"
            tooltip(msg, period=6000, parent=mw)
        finally:
            _auto_sync_in_progress = False

    _run_in_background(_fetch_work, _apply)


_auto_sync_timer = None


def _stop_auto_sync_timer():
    global _auto_sync_timer
    if _auto_sync_timer is not None:
        _auto_sync_timer.stop()
        _auto_sync_timer = None


def _restart_auto_sync_timer(minutes):
    """(Re)start the repeating poll at `minutes`, floored so it can't be configured into
    a busy-loop. GitHub load at this cadence is trivial: one small manifest.json request
    per interval, well under even the unauthenticated 60-requests-per-hour limit at the
    one-minute floor, let alone the 5000-per-hour a token gets.
    """
    _stop_auto_sync_timer()
    global _auto_sync_timer
    interval_ms = clamp_interval_minutes(
        minutes, AUTO_SYNC_INTERVAL_FLOOR_MIN, AUTO_SYNC_INTERVAL_DEFAULT_MIN) * 60 * 1000
    _auto_sync_timer = QTimer(mw)
    _auto_sync_timer.timeout.connect(_auto_sync_check)
    _auto_sync_timer.start(interval_ms)


def _schedule_background_checks():
    """Run once, a couple seconds after Anki finishes starting up: the add-on-update
    check, and, only if auto-sync is on in Settings, an immediate deck check plus the
    repeating poll that keeps checking while Anki stays open.
    """
    QTimer.singleShot(2000, _check_addon_updates_background)
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
    if not _pre_sync_backup_or_confirm_skip(cfg["export_deck"])[0]:
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
    fields_line = f" Preserved fields restored on {restored} card(s)." if restored else ""
    _info(f"Imported {os.path.basename(src)}: {in_place} kept history, {as_new} new."
          f"{fields_line}")


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


EXAMPLE_REPO = "LTimothy/internpearls-example-deck"   # public demo deck source
EXAMPLE_SCOPE_TAG = "ExampleDeck"                     # the example deck's base_tag
EXAMPLE_DECK_NAME = "Example Decks::Pharmacology Basics"


@_safe
def configure_source():
    """Set where decks come from: a GitHub repo (token optional; only needed for a
    private one), a local folder, or the public example repo for anyone who just wants
    to see the add-on do something before pointing it at real decks."""
    conf = mw.addonManager.getConfig(__name__) or {}

    box = QMessageBox(mw)
    box.setWindowTitle(f"{APP_NAME}: Configure deck source")
    box.setIcon(QMessageBox.Icon.Question)
    box.setText("Where should decks come from?<br><br>"
                "<span style='color:gray;'>No decks of your own yet? Try the example "
                "deck: a small public demo repo you can sync right away, and swap out "
                "later.</span>")
    gh_btn = box.addButton("GitHub repo", QMessageBox.ButtonRole.AcceptRole)
    local_btn = box.addButton("Local folder", QMessageBox.ButtonRole.AcceptRole)
    example_btn = box.addButton("Try the example deck", QMessageBox.ButtonRole.AcceptRole)
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
        token, ok = _prompt("Access token (hidden as you type). Leave blank for a "
                            "public repo; a private one needs a read-only token:",
                            edit=token_edit, default=conf.get("github_token", ""))
        if not ok:
            return
        conf["github_decks_repo"] = repo.strip()
        conf["github_token"] = token.strip()
        conf["decks_dir"] = ""
    elif clicked is example_btn:
        conf["github_decks_repo"] = EXAMPLE_REPO
        conf["github_token"] = ""
        conf["decks_dir"] = ""
        # Point the scope tag and backup deck at the example deck's own values (but only
        # if they're still at their defaults — never clobber a deliberate custom value),
        # so the demo shows the real experience: preserved fields survive re-syncs and
        # the automatic pre-sync backup finds its deck. Switching to a GitHub/local
        # source later undoes exactly this (see below).
        if conf.get("scope_tag", "InternPearls") == "InternPearls":
            conf["scope_tag"] = EXAMPLE_SCOPE_TAG
        if conf.get("export_deck", EXPORT_DECK) == EXPORT_DECK:
            conf["export_deck"] = EXAMPLE_DECK_NAME
    elif clicked is local_btn:
        path, ok = _prompt("Folder with manifest.json + .apkg files:",
                          default=conf.get("decks_dir", ""))
        if not ok or not path.strip():
            return
        conf["decks_dir"] = path.strip()
        conf["github_token"] = ""
    else:
        return  # Cancel, or the dialog was closed

    # Undo the example-deck scope/backup override when moving on to a real source: those
    # two values were set by the example button above, not chosen by the user, so
    # leaving them behind would silently mis-scope every future sync. A custom value the
    # user set themselves is never touched (the example button doesn't overwrite one,
    # and this only resets the exact example values).
    if clicked is not example_btn:
        if conf.get("scope_tag") == EXAMPLE_SCOPE_TAG:
            conf["scope_tag"] = "InternPearls"
        if conf.get("export_deck") == EXAMPLE_DECK_NAME:
            conf["export_deck"] = EXPORT_DECK

    mw.addonManager.writeConfig(__name__, conf)

    try:
        manifest, _, source = _fetch_manifest(_cfg())
    except Exception as e:
        _warn(f"Saved, but couldn't connect: {e}<br><br>"
              "Double-check the repo name and token (or folder path), then use "
              "<i>Change source</i> in Manage decks again.")
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

    Deck-source configuration lives here too, behind a "Configure source" / "Change
    source" button, rather than its own top-level menu item: the source only matters in
    the context of what decks are available to manage, so it made the menu bar noisier
    without adding a use case of its own.

    Mostly a thin rendering layer over already-computed rows (from logic.deck_status):
    it renders checkboxes and status pills, then hands back the user's choices via
    excluded_decks()/protected_fields(). No network or collection access lives here,
    except indirectly through change_source_requested, which the caller acts on after
    this dialog closes. Sync automation and add-on update behavior live in a separate
    Settings dialog: this one answers "which decks, which fields, from where," not "how
    automatic" (a different kind of choice that doesn't belong in the same panel).
    """

    def __init__(self, parent, rows, protected, source, preview_fn, configured):
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME}: Manage decks")
        self.setMinimumWidth(480)
        self.sync_requested = False
        self.change_source_requested = False
        self._rows = rows
        self._preview_fn = preview_fn   # callable() -> {deck_name: (kept, new) | None}
        self._checks = {}   # deck name -> QCheckBox
        self._pills = {}    # deck name -> QLabel (status pill, updated after a preview)

        outer = QVBoxLayout(self)
        outer.setSpacing(10)

        title = QLabel("Manage decks")
        title.setStyleSheet("font-size: 17px; font-weight: 600;")
        outer.addWidget(title)

        source_row = QHBoxLayout()
        source_label = QLabel(f"Source: {source}")
        source_label.setStyleSheet("color: gray;")
        source_row.addWidget(source_label)
        change_src_btn = QPushButton("Change source" if configured else "Configure source")
        change_src_btn.setFlat(True)
        change_src_btn.setStyleSheet("color: #2563eb; font-size: 12px;")
        change_src_btn.clicked.connect(self._request_change_source)
        source_row.addWidget(change_src_btn)
        source_row.addStretch()
        outer.addLayout(source_row)

        sub = QLabel("Check the decks you want to keep synced. Unchecking one stops "
                     "future syncs for it; cards already imported stay in your "
                     "collection until you delete them in Anki.")
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
        # Reflect why there's nothing to check on open, rather than leaving an inviting
        # button that just reports "nothing happened" once clicked: no decks at all
        # (source unreachable or not configured) reads differently from every deck
        # already matching what's installed.
        if not rows:
            self._preview_btn.setText("No decks available")
            self._preview_btn.setEnabled(False)
            self._preview_btn.setStyleSheet("color: gray;")
        elif not any(r["state"] != "current" for r in rows):
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
            empty = QLabel("No decks available yet. Use the button above to set up or "
                           "fix your deck source.")
            empty.setWordWrap(True)
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
        self._preview_btn.setText("Check again")
        self._preview_btn.setEnabled(True)

    def _set_all(self, val):
        for cb in self._checks.values():
            cb.setChecked(val)

    def _save_and_sync(self):
        self.sync_requested = True
        self.accept()

    def _request_change_source(self):
        # Close without treating this as a save or a plain cancel; the caller checks
        # change_source_requested first and reopens this same dialog after the source
        # configuration flow runs, so any in-progress checkbox/field edits here are
        # simply discarded, same as a Cancel would do.
        self.change_source_requested = True
        self.reject()

    def excluded_decks(self):
        return [name for name, cb in self._checks.items() if not cb.isChecked()]

    def protected_fields(self):
        return parse_fields(self._pf_edit.text())


@_safe
def manage_decks():
    """Open the deck manager: choose which decks sync, which fields are preserved, and
    which source to pull from.

    Never dead-ends on a missing or unreachable source; the dialog always opens, with
    an empty deck list and a "Configure source" / "Change source" button front and
    center, since that button is now the only way to reach deck-source configuration.
    """
    cfg = _cfg()
    manifest, fetch, source, error = None, None, None, None
    if cfg["gh_repo"] or cfg["decks_dir"]:
        try:
            manifest, fetch, source = _fetch_manifest(cfg)
        except Exception as e:
            error = str(e)
    source_label = source if manifest else (f"error: {error}" if error else "not configured")

    installed = _load_json(INSTALLED, {})
    rows = deck_status(manifest, installed, cfg["excluded"]) if manifest else []

    def _preview():
        """Download every changed deck and match it against the collection, returning
        {deck_name: (kept_in_place, added_new)}. Read-only — imports nothing. Runs when
        the user clicks "Check what will sync"; the download is why it's on demand.
        Only ever called when manifest/fetch exist, since the button stays disabled
        otherwise."""
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

    dlg = _DeckManagerDialog(mw, rows, cfg["protected"], source_label, _preview,
                             configured=bool(manifest))
    result = dlg.exec()

    if dlg.change_source_requested:
        configure_source()
        manage_decks()   # reopen against whatever the source is now
        return
    if not result:
        return   # cancelled

    conf = mw.addonManager.getConfig(__name__) or {}
    conf["excluded_decks"] = dlg.excluded_decks()
    conf["protected_fields"] = dlg.protected_fields()
    mw.addonManager.writeConfig(__name__, conf)

    if dlg.sync_requested:
        sync_decks()
        return
    if not rows:
        _info("Saved. No decks are available from this source yet.")
        return
    kept = sum(1 for r in rows if r["name"] not in conf["excluded_decks"])
    excluded_n = len(rows) - kept
    scope = (f"All {kept} deck(s) are set to sync" if not excluded_n
             else f"{kept} of {len(rows)} deck(s) are set to sync ({excluded_n} excluded)")
    # Auto-sync is a separate, independent setting (Intern Pearls -> Settings), so this
    # dialog only reports whether it's currently on, not whether it changed here.
    next_step = (" Auto-sync is on, so these will keep applying on their own."
                if cfg["auto_sync_decks"] else
                " Nothing synced yet, run <b>Sync decks</b> when you're ready to pull "
                "them (or use <i>Save and sync now</i> next time to do both at once).")
    _info(f"Saved. {scope}, preserving {', '.join(conf['protected_fields'])}."
          f"<br><br>{next_step}")


class _SettingsDialog(QDialog):
    """Sync automation and add-on update behavior, kept apart from Manage decks.

    Manage decks answers "which decks, which fields" (what gets synced). This dialog
    answers "how automatic, how often" (whether it happens on its own). Keeping the two
    separate is what stops either one from turning into a catch-all as more toggles get
    added.
    """

    def __init__(self, parent, auto_sync, interval_minutes, notify_updates, auto_update):
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME}: Settings")
        self.setMinimumWidth(440)

        outer = QVBoxLayout(self)
        outer.setSpacing(10)

        title = QLabel("Settings")
        title.setStyleSheet("font-size: 17px; font-weight: 600;")
        outer.addWidget(title)

        sync_head = QLabel("Deck sync")
        sync_head.setStyleSheet("font-weight: 600; margin-top: 4px;")
        outer.addWidget(sync_head)

        self._auto_sync_cb = QCheckBox("Sync decks automatically when updates are available")
        self._auto_sync_cb.setChecked(auto_sync)
        outer.addWidget(self._auto_sync_cb)

        interval_row = QHBoxLayout()
        interval_row.addWidget(QLabel("Check every"))
        self._interval_spin = QSpinBox()
        self._interval_spin.setRange(AUTO_SYNC_INTERVAL_FLOOR_MIN, 1440)
        self._interval_spin.setValue(interval_minutes)
        self._interval_spin.setSuffix(" min")
        interval_row.addWidget(self._interval_spin)
        interval_row.addStretch()
        outer.addLayout(interval_row)

        sync_hint = QLabel(
            "Checks the source in the background and applies any changed decks without "
            "asking. A backup is still taken first, the same as a manual sync. The check "
            "itself is built not to freeze Anki even on a slow or dead connection: it "
            "fails fast and tries again at the next check.")
        sync_hint.setWordWrap(True)
        sync_hint.setStyleSheet("color: gray; font-size: 11px;")
        outer.addWidget(sync_hint)

        upd_head = QLabel("Add-on updates")
        upd_head.setStyleSheet("font-weight: 600; margin-top: 14px;")
        outer.addWidget(upd_head)

        self._notify_cb = QCheckBox("Notify me when a new add-on version is out")
        self._notify_cb.setChecked(notify_updates)
        outer.addWidget(self._notify_cb)

        self._auto_update_cb = QCheckBox("Install add-on updates automatically")
        self._auto_update_cb.setChecked(auto_update)
        outer.addWidget(self._auto_update_cb)

        upd_hint = QLabel(
            "Checked once per launch rather than on a repeating timer, since a new "
            "add-on release isn't as time-sensitive as a new deck. Either way, Anki "
            "needs a restart to load the new version.")
        upd_hint.setWordWrap(True)
        upd_hint.setStyleSheet("color: gray; font-size: 11px;")
        outer.addWidget(upd_hint)

        bb = QDialogButtonBox()
        save = bb.addButton("Save", QDialogButtonBox.ButtonRole.AcceptRole)
        bb.addButton(QDialogButtonBox.StandardButton.Cancel)
        save.clicked.connect(self.accept)
        bb.rejected.connect(self.reject)
        outer.addWidget(bb)

    def values(self):
        return {
            "auto_sync_decks": self._auto_sync_cb.isChecked(),
            "auto_sync_interval_minutes": self._interval_spin.value(),
            "notify_addon_updates": self._notify_cb.isChecked(),
            "auto_update_addon": self._auto_update_cb.isChecked(),
        }


@_safe
def open_settings():
    """Open Settings: sync automation and add-on update behavior."""
    cfg = _cfg()
    dlg = _SettingsDialog(mw, cfg["auto_sync_decks"], cfg["auto_sync_interval_minutes"],
                          cfg["notify_addon_updates"], cfg["auto_update_addon"])
    if not dlg.exec():
        return

    values = dlg.values()
    conf = mw.addonManager.getConfig(__name__) or {}
    conf.update(values)
    mw.addonManager.writeConfig(__name__, conf)

    # Apply immediately rather than waiting for a restart.
    if values["auto_sync_decks"]:
        _restart_auto_sync_timer(values["auto_sync_interval_minutes"])
    else:
        _stop_auto_sync_timer()

    sync_line = (
        f"Deck sync checks every {values['auto_sync_interval_minutes']} minute(s) and "
        "applies updates on its own." if values["auto_sync_decks"] else
        "Deck sync stays manual, use Sync decks when you're ready.")
    if values["auto_update_addon"]:
        update_line = "Add-on updates install automatically."
    elif values["notify_addon_updates"]:
        update_line = "You'll get a notice when a new add-on version is out."
    else:
        update_line = "Add-on update checks are off."
    _info(f"Settings saved.<br><br>{sync_line}<br>{update_line}")


@_safe
def about():
    cfg = _cfg()
    sync_status = (
        f"on, checking every {cfg['auto_sync_interval_minutes']} minute(s)"
        if cfg["auto_sync_decks"] else "off")
    if cfg["auto_update_addon"]:
        update_status = "installs automatically"
    elif cfg["notify_addon_updates"]:
        update_status = "notifies you, you install it"
    else:
        update_status = "off"

    box = QMessageBox(mw)
    box.setWindowTitle(f"{APP_NAME}: About")
    box.setIcon(QMessageBox.Icon.Information)
    box.setTextFormat(Qt.TextFormat.RichText)
    box.setText(
        f"<b>Intern Pearls Deck Tools</b> &nbsp;<span style='color:gray;'>v{ADDON_VERSION}"
        "</span><br><br>"
        "Keeps a set of Anki decks in sync with a source you control, without losing "
        "review history or the annotations you keep in any preserved field. Cards are "
        "matched by ID, preserved fields are snapshotted and restored around every "
        "import, and a backup runs automatically before anything changes."
        "<br><br><b>Current settings</b>" +
        bullets([
            f"Auto-sync: {sync_status}",
            f"Add-on updates: {update_status}",
            f"Preserved fields: {', '.join(cfg['protected']) or 'none set'}",
        ]) +
        "Change these under <i>Manage decks</i> (which decks, which fields, and where "
        "from) or <i>Settings</i> (how automatic)."
        "<br><br>No deck content ships with the add-on itself. Set your source, a "
        "GitHub repo or a local folder, from <i>Manage decks</i>."
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

    # Two primary actions up top, everything occasional tucked under Advanced (including
    # the manual add-on-update check, which most people never need since the background
    # notice already covers it), and a small Settings/About pair at the bottom. Deck
    # source configuration lives inside Manage decks itself now, not as its own item,
    # since it only matters in the context of what decks are available to manage.
    add(menu, "Sync decks", sync_decks)
    add(menu, "Manage decks", manage_decks)
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
    adv.addSeparator()
    add(adv, "Check for add-on updates", check_updates)
    menu.addSeparator()
    add(menu, "Settings", open_settings)
    add(menu, "About", about)

    try:
        mw.form.menubar.insertMenu(mw.form.menuHelp.menuAction(), menu)
    except Exception:
        mw.form.menuTools.addMenu(menu)


gui_hooks.main_window_did_init.append(_menu)
gui_hooks.main_window_did_init.append(_schedule_background_checks)
