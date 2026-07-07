"""
Intern Pearls Deck Tools  —  Anki add-on for history-safe deck sync.

"Sync decks" is the only button most people need: it pulls any changed decks (from the
private GitHub repo, or a local folder) and applies each one so your REVIEW HISTORY and
personal NOTES are preserved. Everything else (fix note types, restore notes, single-file
import) is an automatic part of Sync, exposed under "Advanced" only as a fallback.

Nothing changes your collection except Anki's own import plus safe note-type field
additions — a pre-import backup makes it fully reversible.
"""
import json
import os
import re
import sqlite3
import tempfile
import urllib.request
import zipfile

from aqt import mw, gui_hooks
from aqt.qt import QAction, QMenu, QMessageBox, Qt
from aqt.utils import (askUser, getFile, getText, openLink, showInfo,
                       showWarning, tooltip)

ADDON_VERSION = "0.5.0"   # MAJOR.MINOR.PATCH — see CLAUDE.md "Versioning"
ANKI_REPO = "LTimothy/internpearls-anki"   # public add-on repo (used for self-update)
FS = "\x1f"
_DIR = os.path.dirname(__file__)
SNAPSHOT = os.path.join(_DIR, "notes_snapshot.json")
INSTALLED = os.path.join(_DIR, "installed.json")

TARGET_FIELDS = {
    "Study Deck - Basic":    ["Front", "Back", "Why", "Image", "Tag", "Dosing", "Notes"],
    "Study Deck - Cloze":    ["Text", "Why", "Image", "Dosing", "Notes"],
    "Study Deck - Image ID": ["Image", "Prompt", "Answer", "Why", "Notes"],
}


# --------------------------------------------------------------------------- config
def _cfg():
    c = mw.addonManager.getConfig(__name__) or {}
    return {
        "protected": c.get("protected_fields", ["Notes"]),
        "scope_tag": c.get("scope_tag", "InternPearls"),
        "decks_dir": c.get("decks_dir", ""),
        "gh_repo":   c.get("github_decks_repo", ""),
        "gh_ref":    c.get("github_ref", "main"),
        "gh_token":  c.get("github_token", ""),
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


def _import_apkg(path):
    from anki.collection import ImportAnkiPackageRequest, ImportAnkiPackageOptions
    opts = ImportAnkiPackageOptions()
    for attr, val in (("with_scheduling", False), ("merge_notetypes", True)):
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
        showWarning(f"Couldn't reach the deck source: {e}\n\n"
                    "Check your GitHub token / decks_dir in Config.")
        return
    if not manifest:
        showWarning("No deck source configured yet.\n\n"
                    "Run Intern Pearls → 'Configure deck source…' first.")
        return

    installed = _load_json(INSTALLED, {})
    todo = [d for d in manifest["decks"] if installed.get(d["name"]) != d["version"]]
    if not todo:
        showInfo(f"All decks are up to date (source: {source}).")
        return
    if not askUser("Update these decks?\n\n  " +
                   "\n  ".join(d["name"].split("::")[-1] for d in todo) +
                   "\n\nBack up first (File → Export → Collection Package). Proceed?"):
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
            results.append(f"✓ {short}: {in_place} kept history, {as_new} new")
        except Exception as e:
            results.append(f"✗ {short}: {e}")
    _save_json(INSTALLED, installed)
    restored = _restore(snap)
    mw.reset()
    showInfo(f"Sync complete (source: {source}):\n\n  " + "\n  ".join(results) +
             f"\n\nNotes restored on {restored} card(s).")


def check_updates():
    """Compare our version to version.json in the public add-on repo; offer to update."""
    url = f"https://raw.githubusercontent.com/{ANKI_REPO}/main/version.json"
    try:
        latest = json.loads(_http_get(url))
    except Exception as e:
        showWarning(f"Couldn't check for updates: {e}")
        return

    def nums(v):
        return tuple(int(x) for x in re.findall(r"\d+", str(v)))

    latest_n, cur_n = nums(latest.get("version", "0")), nums(ADDON_VERSION)
    width = max(len(latest_n), len(cur_n))          # zero-pad so 0.5 == 0.5.0
    latest_n += (0,) * (width - len(latest_n))
    cur_n += (0,) * (width - len(cur_n))
    if latest_n <= cur_n:
        showInfo(f"Intern Pearls Deck Tools is up to date (v{ADDON_VERSION}).")
        return
    if not askUser(f"Update available: v{latest['version']} "
                   f"(you have v{ADDON_VERSION}). Download and install now?"):
        return
    try:
        data = _http_get(latest["download"])
        tmp = os.path.join(tempfile.gettempdir(), "internpearls.ankiaddon")
        with open(tmp, "wb") as fh:
            fh.write(data)
        mw.addonManager.install(tmp)
        showInfo("Updated — please restart Anki.")
    except Exception as e:
        showWarning(f"Auto-install failed ({e}).\nOpening the download page instead.")
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
    except Exception:
        pass
    _ensure_notetypes()
    her = _her_front_to_guid(cfg["scope_tag"])
    remap, in_place, as_new = _remap(src, her, aliases)
    if not askUser(f"{in_place} cards keep history, {as_new} import as new. "
                   "Back up first. Write personalized .apkg + snapshot Notes?"):
        return
    _save_json(SNAPSHOT, _snapshot(cfg["protected"], cfg["scope_tag"]))
    out = os.path.splitext(src)[0] + ".forreview.apkg"
    _write_personalized(src, remap, out)
    showInfo(f"Wrote:\n  {out}\n\nDouble-click it, then Advanced → Restore my notes.")


def restore_notes():
    snap = _load_json(SNAPSHOT, None)
    if not snap:
        showWarning("No Notes snapshot found.")
        return
    restored = _restore(snap)
    mw.reset()
    tooltip(f"Restored notes on {restored} card(s).")


def update_notetypes():
    added = _ensure_notetypes()
    showInfo(("Updated note types (cards & scheduling untouched):\n\n  " +
              "\n  ".join(added)) if added else
             "Note types already up to date — no changes needed.")


def configure_source():
    """Set where decks come from: a GitHub repo + read-only token, or a local folder."""
    conf = mw.addonManager.getConfig(__name__) or {}
    if askUser("Sync decks from GitHub?\n\n"
               "Yes  = a GitHub repo + a read-only access token.\n"
               "No   = a local folder on this computer."):
        repo, ok = getText("Decks repo (owner/name):",
                           default=conf.get("github_decks_repo", ""))
        if not ok:
            return
        token, ok = getText("Read-only access token:",
                            default=conf.get("github_token", ""))
        if not ok:
            return
        conf["github_decks_repo"] = repo.strip()
        conf["github_token"] = token.strip()
        conf["decks_dir"] = ""
    else:
        path, ok = getText("Folder with manifest.json + .apkg files:",
                          default=conf.get("decks_dir", ""))
        if not ok:
            return
        conf["decks_dir"] = path.strip()
        conf["github_token"] = ""
    mw.addonManager.writeConfig(__name__, conf)
    showInfo("Saved. Run Intern Pearls → Sync decks.")


def about():
    box = QMessageBox(mw)
    box.setWindowTitle("Intern Pearls Deck Tools")
    box.setTextFormat(Qt.TextFormat.RichText)
    box.setText(
        f"<b>Intern Pearls Deck Tools</b> &nbsp; v{ADDON_VERSION}<br><br>"
        "One-click, history-safe deck updates. <b>Sync decks</b> pulls the latest "
        "decks and applies them while keeping your review history and personal Notes."
        "<br><br>Set your deck source under <i>Configure deck source…</i>."
        "<br><br>"
        f'<a href="https://github.com/{ANKI_REPO}">github.com/{ANKI_REPO}</a>')
    box.setStandardButtons(QMessageBox.StandardButton.Ok)
    box.exec()


# ---------------------------------------------------------------------------- menu
def _menu():
    menu = QMenu("&Intern Pearls", mw)

    def add(target, label, fn):
        act = QAction(label, mw)
        act.triggered.connect(fn)
        target.addAction(act)

    add(menu, "Sync decks", sync_decks)
    add(menu, "Configure deck source…", configure_source)
    add(menu, "Check for add-on updates", check_updates)
    menu.addSeparator()
    adv = menu.addMenu("Advanced")
    add(adv, "Import single deck (manual)…", import_single)
    add(adv, "Fix note types", update_notetypes)
    add(adv, "Restore my notes", restore_notes)
    menu.addSeparator()
    add(menu, "About…", about)

    try:
        mw.form.menubar.insertMenu(mw.form.menuHelp.menuAction(), menu)
    except Exception:
        mw.form.menuTools.addMenu(menu)


gui_hooks.main_window_did_init.append(_menu)
