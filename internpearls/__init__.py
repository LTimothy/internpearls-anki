"""
Intern Pearls Deck Tools: an Anki add-on for history-safe deck sync.

"Sync decks" is the only button most people need. It pulls any changed decks (from the
private GitHub repo, or a local folder) and applies each one so your review history and
personal notes are preserved. Everything else (fix note types, restore notes, single-file
import) runs automatically as part of Sync and is exposed under "Advanced" only as a
fallback.

Before touching the collection, Sync takes its own timestamped backup, so nothing here
depends on the user remembering to export one first.
"""
import datetime
import json
import os
import re
import sqlite3
import tempfile
import urllib.request
import zipfile

from aqt import mw, gui_hooks
from aqt.qt import QAction, QLineEdit, QMenu, QMessageBox, Qt
from aqt.utils import (askUser, getFile, getSaveFile, getText, openLink,
                       showInfo, showWarning, tooltip)

ADDON_VERSION = "0.10.0"   # MAJOR.MINOR.PATCH, see CLAUDE.md "Versioning"
ANKI_REPO = "LTimothy/internpearls-anki"   # public add-on repo (used for self-update)
APP_NAME = "Intern Pearls"   # every dialog's title bar, so it never just says "Anki"
EXPORT_DECK = "Intern Pearls::Intern Custom"   # the deck Export Intern Pearls deck scopes to
DECK_BACKUPS_KEEP = 10   # how many automatic Intern Pearls deck backups to retain
FS = "\x1f"
_DIR = os.path.dirname(__file__)

# Anki's add-on manager wipes and re-extracts everything in this folder on every add-on
# update, except a "user_files" subfolder, which it explicitly backs up and restores
# around the reinstall. Our own sync state has to live there or every add-on update
# resets it, making Sync think every deck is new again.
_USER_FILES = os.path.join(_DIR, "user_files")
os.makedirs(_USER_FILES, exist_ok=True)
SNAPSHOT = os.path.join(_USER_FILES, "notes_snapshot.json")
INSTALLED = os.path.join(_USER_FILES, "installed.json")

# One-time migration: earlier versions wrote these next to __init__.py, so an add-on
# update would have already wiped them. Move them over if they're still there from a
# same-version reinstall.
for _name in ("notes_snapshot.json", "installed.json"):
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


def _bullets(items):
    """Render a list as clean HTML for use inside _info()/_warn() rich text."""
    return "<ul style='margin:4px 0 4px 0;'>" + "".join(
        f"<li>{item}</li>" for item in items) + "</ul>"


# ------------------------------------------------------------------------ http/github
def _http_get(url, token=None, accept=None):
    headers = {"User-Agent": "internpearls-addon"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def _gh_raw(repo, path, token, ref):
    """Raw bytes of a file in a (possibly private) repo via the contents API."""
    url = f"https://api.github.com/repos/{repo}/contents/{path}?ref={ref}"
    return _http_get(url, token=token, accept="application/vnd.github.raw")


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
                "intern pearls deck now, or Advanced → Full collection backup now.)")


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
def _apkg_notes(path):
    with zipfile.ZipFile(path) as z:
        if "collection.anki2" not in z.namelist():
            raise RuntimeError("Unexpected .apkg format (no collection.anki2).")
        with tempfile.TemporaryDirectory() as d:
            z.extract("collection.anki2", d)
            con = sqlite3.connect(os.path.join(d, "collection.anki2"))
            rows = [(rid, flds.split(FS)[0], guid) for rid, guid, flds in
                    con.execute("select id, guid, flds from notes")]
            con.close()
    return rows


def _write_personalized(src, remap, out):
    with tempfile.TemporaryDirectory() as d:
        with zipfile.ZipFile(src) as z:
            z.extractall(d)
        con = sqlite3.connect(os.path.join(d, "collection.anki2"))
        for rid, g in remap.items():
            con.execute("update notes set guid=? where id=?", (g, rid))
        con.commit()
        con.close()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for root, _, files in os.walk(d):
                for f in files:
                    full = os.path.join(root, f)
                    z.write(full, os.path.relpath(full, d))


def _import_apkg(path, with_scheduling=False):
    """with_scheduling=False for a spec-authored deck matched onto existing cards (the
    learner's own scheduling should win); True for reimporting our own previously
    exported/backed-up package, where the file's scheduling IS the thing being restored.
    """
    from anki.collection import ImportAnkiPackageRequest, ImportAnkiPackageOptions
    opts = ImportAnkiPackageOptions()
    for attr, val in (("with_scheduling", with_scheduling), ("merge_notetypes", True)):
        try:
            setattr(opts, attr, val)
        except Exception:
            pass
    return mw.col.import_anki_package(
        ImportAnkiPackageRequest(package_path=path, options=opts))


def _remap(src, her, aliases):
    remap, in_place, as_new = {}, 0, 0
    for rid, front, apkg_guid in _apkg_notes(src):
        her_guid = her.get(front)
        if her_guid is None and front in aliases:
            her_guid = her.get(aliases[front])
        if her_guid is None:
            as_new += 1
        else:
            in_place += 1
            if her_guid != apkg_guid:
                remap[rid] = her_guid
    return remap, in_place, as_new


def _her_front_to_guid(scope_tag):
    search = f'"tag:{scope_tag}" OR "tag:{scope_tag}::*"' if scope_tag else ""
    out = {}
    for nid in mw.col.find_notes(search):
        note = mw.col.get_note(nid)
        out.setdefault(note.fields[0], note.guid)
    return out


def _apply_deck(src, aliases, her):
    remap, in_place, as_new = _remap(src, her, aliases)
    out = src + ".sync.apkg"
    _write_personalized(src, remap, out)
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
            data = _gh_raw(cfg["gh_repo"], d["apkg"], cfg["gh_token"], cfg["gh_ref"])
            tmp = os.path.join(tempfile.gettempdir(), d["apkg"])
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
    todo = [d for d in manifest["decks"] if installed.get(d["name"]) != d["version"]]
    if not todo:
        _info(f"All decks are up to date (source: {source}).")
        return

    def _line(d):
        short = d["name"].split("::")[-1]
        cards = d.get("cards")
        return f"{short} ({cards} cards)" if cards is not None else short

    if not _ask(
        "Update these decks?\n\n  • " + "\n  • ".join(_line(d) for d in todo) +
        "\n\nYour review history and any personal notes on existing cards are kept "
        "(matched by card, not overwritten). A backup is taken automatically first, "
        "so this is safe to undo if anything looks wrong afterward."
    ):
        return
    if not _pre_sync_backup_or_confirm_skip(cfg["export_deck"]):
        return

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
    _info(f"<b>Sync complete</b> (source: {source})" + _bullets(results) +
          f"Notes restored on {restored} card(s).<br><br>"
          f"A pre-sync backup of the Intern Pearls deck was saved; use "
          f"<i>Advanced → Import intern pearls deck</i> to revert to it if needed.")


def check_updates():
    """Compare our version to version.json in the public add-on repo; offer to update."""
    url = f"https://raw.githubusercontent.com/{ANKI_REPO}/main/version.json"
    try:
        latest = json.loads(_http_get(url))
    except Exception as e:
        _warn(f"Couldn't check for updates: {e}")
        return

    def nums(v):
        return tuple(int(x) for x in re.findall(r"\d+", str(v)))

    latest_n, cur_n = nums(latest.get("version", "0")), nums(ADDON_VERSION)
    width = max(len(latest_n), len(cur_n))          # zero-pad so 0.5 == 0.5.0
    latest_n += (0,) * (width - len(latest_n))
    cur_n += (0,) * (width - len(cur_n))
    if latest_n <= cur_n:
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


def import_single():
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
    remap, in_place, as_new = _remap(src, her, aliases)
    if not _ask(f"{in_place} card(s) will keep their history, {as_new} will be added "
                "as new. A backup is taken automatically first. Write the "
                "personalized .apkg and snapshot your notes?"):
        return
    if not _pre_sync_backup_or_confirm_skip(cfg["export_deck"]):
        return
    _save_json(SNAPSHOT, _snapshot(cfg["protected"], cfg["scope_tag"]))
    out = os.path.splitext(src)[0] + ".forreview.apkg"
    _write_personalized(src, remap, out)
    _info(f"Wrote:<br><code>{out}</code><br><br>Double-click it to import, then run "
          "<i>Advanced → Restore my notes</i>.")


def restore_notes():
    snap = _load_json(SNAPSHOT, None)
    if not snap:
        _warn("No Notes snapshot found.")
        return
    restored = _restore(snap)
    mw.reset()
    tooltip(f"Restored notes on {restored} card(s).")


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


def import_deck():
    """Bring an exported/backed-up Intern Pearls .apkg back into this collection.

    The file already carries this collection's own note GUIDs (it was made by Export
    Intern Pearls deck or an automatic pre-sync backup), so Anki's importer matches
    everything by GUID directly: no front-text personalization needed the way Sync and
    Import single deck need it for a spec-authored deck from someone else's collection.
    """
    src = getFile(mw, "Choose an Intern Pearls .apkg", cb=None,
                 filter="*.apkg", key="internpearls", dir=_deck_backup_folder())
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


def update_notetypes():
    added = _ensure_notetypes()
    _info(("<b>Updated note types</b> (cards and scheduling untouched):" +
           _bullets(added)) if added else
          "Note types are already up to date, no changes needed.")


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


def about():
    box = QMessageBox(mw)
    box.setWindowTitle(f"{APP_NAME}: About")
    box.setIcon(QMessageBox.Icon.Information)
    box.setTextFormat(Qt.TextFormat.RichText)
    box.setText(
        f"<b>Intern Pearls Deck Tools</b> &nbsp;<span style='color:gray;'>v{ADDON_VERSION}"
        "</span><br><br>"
        "History-safe deck management for Anki: sync decks from a repo or folder "
        "without losing review history or personal notes, and back up, export, or "
        "import your decks without leaving Anki."
        "<br><br><b>Sync decks</b> pulls whatever changed at your configured source, "
        "matches every card to your existing one by ID so scheduling and personal "
        "notes carry over, and backs up first automatically."
        "<br><br><b>Configure deck source</b> points Sync at a GitHub repo or a local "
        "folder containing a <code>manifest.json</code> and the deck files."
        "<br><br><b>Advanced</b> holds the individual pieces Sync runs for you, plus "
        "standalone backup, export, and import tools:"
        + _bullets([
            "Import single deck / Fix note types / Restore my notes, the manual "
            "fallback for each step of Sync",
            "Backup / Import / Export intern pearls deck, a fast, self-contained "
            "copy of just this deck",
            "Full collection backup now / Restore from backup, the same "
            "whole-collection tools Anki uses on its own",
        ]) +
        "This started as a tool for one set of anesthesia study decks, but nothing "
        "about it is specific to that content. Anyone can point it at their own deck "
        "source, and more Anki enhancements may live under this same menu over time."
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
        act.triggered.connect(fn)                 # matches "Configure"/"About"/etc. into
        target.addAction(act)                     # the app menu unless told not to

    add(menu, "Sync decks", sync_decks)
    add(menu, "Configure deck source", configure_source)
    add(menu, "Check for add-on updates", check_updates)
    menu.addSeparator()
    adv = menu.addMenu("Advanced")
    add(adv, "Import single deck (manual)", import_single)
    add(adv, "Fix note types", update_notetypes)
    add(adv, "Restore my notes", restore_notes)
    adv.addSeparator()
    add(adv, "Backup intern pearls deck now", backup_deck_now)
    add(adv, "Import intern pearls deck", import_deck)
    add(adv, "Export intern pearls deck", export_deck)
    adv.addSeparator()
    add(adv, "Full collection backup now", backup_collection_now)
    add(adv, "Restore from backup", restore_from_backup)
    menu.addSeparator()
    add(menu, "About", about)

    try:
        mw.form.menubar.insertMenu(mw.form.menuHelp.menuAction(), menu)
    except Exception:
        mw.form.menuTools.addMenu(menu)


gui_hooks.main_window_did_init.append(_menu)
