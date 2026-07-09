"""Browser-side glue for the live demo: boots the REAL add-on under Pyodide.

Nothing in here re-implements add-on behavior. It installs the same mock-Anki
harness pytest uses (mock_anki.py, byte-identical to tests/mock_anki.py),
imports the real internpearls package — which builds the real menu — and
exposes a thin JSON bridge the page's driver calls:

  boot()               seed the collection by really syncing the example decks
  menu()               the menu tree recorded from the real _menu()
  start(wid)/feed(...) run a real menu action via mock_anki.Runner, pausing at
                       each real dialog for the user
  collection_state()   current collection state for rendering
  maintainer(op)       edit the real .apkg/manifest files in the source folder

Everything the user sees — dialog layout, wording, counts, versions, behavior —
comes out of the add-on's own code at runtime.
"""
import json
import os
import sqlite3
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mock_anki

MOCK = mock_anki.install()

import internpearls                     # noqa: E402  (real __init__: builds the menu)
from internpearls import background, collection, config, net, sync  # noqa: E402

SOURCE = os.environ.get("DEMO_SOURCE", "/source")   # env override for local smoke tests
INTERVALS = ["2.3 mo", "11 d", "27 d", "6 d", "3.1 mo", "16 d", "9 d", "1.2 mo"]
SEED_NOTES = {0: "mnemonic: A for A", 4: "ask Dr. P about IV dosing"}

RUNNER = mock_anki.Runner(MOCK, paths=[config.INSTALLED, config.STATE,
                                       collection._USER_FILES])


def _install_browser_net():
    """Route the add-on's real HTTP helper through a synchronous XHR so the
    GitHub source option and the add-on update check genuinely work in the
    browser. charset=x-user-defined is the standard trick for binary-safe
    text responses."""
    try:
        from js import XMLHttpRequest
    except ImportError:
        return   # not running under Pyodide (e.g. a local smoke test)

    def _http_get(url, token=None, accept=None, timeout=None):
        req = XMLHttpRequest.new()
        req.open("GET", url, False)
        req.overrideMimeType("text/plain; charset=x-user-defined")
        if accept:
            req.setRequestHeader("Accept", accept)
        if token:
            req.setRequestHeader("Authorization", f"Bearer {token}")
        try:
            req.send(None)
        except Exception:
            raise RuntimeError(f"Couldn't reach {url.split('/')[2]} "
                               "(offline, or blocked by the browser).")
        if req.status >= 400:
            raise RuntimeError(f"The server answered HTTP {req.status} for {url}.")
        return bytes(ord(c) & 0xFF for c in req.responseText)

    net._http_get = _http_get


def boot():
    """Configure the add-on the way 'Try the example deck' does, then really
    sync the example decks into the empty collection and decorate it with a
    reviewer's state (intervals, a couple of personal annotations)."""
    _install_browser_net()
    for hook in sys.modules["aqt"].gui_hooks.main_window_did_init:
        hook()

    MOCK.mw._config = {"decks_dir": SOURCE,
                       "scope_tag": config.EXAMPLE_SCOPE_TAG,
                       "export_deck": "Example Decks"}
    MOCK.col.models._models.clear()   # real note types arrive with the import

    cfg = config._cfg()
    manifest = json.load(open(os.path.join(SOURCE, "manifest.json"),
                              encoding="utf8"))
    sync._run_sync(cfg, manifest,
                   lambda d: os.path.join(SOURCE, d["apkg"]),
                   manifest["decks"], {})
    MOCK.col.decks.names.setdefault("Example Decks", 999)

    for i, note in enumerate(sorted(MOCK.col._notes.values(),
                                    key=lambda n: (n.deck or "", n.id))):
        note.interval = INTERVALS[i % len(INTERVALS)]
        if i in SEED_NOTES and "Notes" in note:
            note["Notes"] = SEED_NOTES[i]
    for lst in (MOCK.gui.infos, MOCK.gui.tooltips):
        lst.clear()
    return collection_state()


# ------------------------------------------------------------------ rendering
def _note_view(n):
    is_cloze = "Cloze" in n.model["name"]
    return {
        "guid": n.guid,
        "front": n.fields[0],
        "back": (n["Why"] if is_cloze and "Why" in n else
                 (n["Back"] if "Back" in n else "")),
        "cloze": is_cloze,
        "notes": n["Notes"] if "Notes" in n else "",
        "interval": getattr(n, "interval", None),
    }


def collection_state():
    groups = {}
    for n in sorted(MOCK.col._notes.values(), key=lambda n: (n.deck or "", n.id)):
        top = "::".join((n.deck or "Default").split("::")[:2])
        groups.setdefault(top, []).append(_note_view(n))
    return json.dumps({
        "decks": [{"name": k, "cards": v} for k, v in sorted(groups.items())],
        "version": config.ADDON_VERSION,
    })


def set_note(guid, text):
    n = MOCK.col.note_by_guid(guid)
    if "Notes" in n:
        n["Notes"] = text
    return collection_state()


def menu():
    return json.dumps(MOCK.mw._menus[0].tree())


def get_config():
    c = config._cfg()
    return json.dumps({"auto_sync": c["auto_sync_decks"],
                       "interval": c["auto_sync_interval_minutes"]})


def list_files(folder):
    out = []
    if folder and os.path.isdir(folder):
        for root, _, files in os.walk(folder):
            out += [os.path.join(root, f) for f in files]
    return json.dumps(sorted(out, reverse=True))


# ------------------------------------------------------------ flows / dialogs
def start(wid):
    return json.dumps(RUNNER.start(lambda: mock_anki.trigger_action(wid)))


def feed(response_json):
    return json.dumps(RUNNER.feed(json.loads(response_json)))


def flow_tooltips():
    return json.dumps(list(MOCK.gui.tooltips))


def auto_sync_tick():
    MOCK.gui.tooltips.clear()
    mock_anki.reset_run()
    background._auto_sync_check()
    return json.dumps(list(MOCK.gui.tooltips))


# --------------------------------------------------------------- maintainer
def _edit_apkg(path, edit):
    with tempfile.TemporaryDirectory() as d:
        with zipfile.ZipFile(path) as z:
            z.extractall(d)
        con = sqlite3.connect(os.path.join(d, "collection.anki2"))
        edit(con)
        con.commit()
        con.close()
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            for root, _, files in os.walk(d):
                for f in files:
                    full = os.path.join(root, f)
                    z.write(full, os.path.relpath(full, d))


def _bump(deck_name):
    mpath = os.path.join(SOURCE, "manifest.json")
    manifest = json.load(open(mpath, encoding="utf8"))
    for d in manifest["decks"]:
        if d["name"] == deck_name:
            base = d["version"].split("+")[0]
            n = int(d["version"].split("+")[1]) + 1 if "+" in d["version"] else 1
            d["version"] = f"{base}+{n}"
    with open(mpath, "w", encoding="utf8") as fh:
        json.dump(manifest, fh)


def _decks():
    manifest = json.load(open(os.path.join(SOURCE, "manifest.json"),
                              encoding="utf8"))
    return manifest["decks"]


def _first_basic_note(con):
    fs = mock_anki.FS
    for nid, flds in con.execute("select id, flds from notes order by id"):
        if "{{c" not in flds.split(fs)[0]:
            return nid, flds
    raise RuntimeError("no basic note found")


def maintainer(op):
    """Edit the real source .apkg + manifest, exactly like a maintainer pushing
    to the deck repo. GUIDs are never touched — which is precisely why the
    reworded card keeps its history when the add-on syncs it."""
    fs = mock_anki.FS
    decks = _decks()
    deck = decks[min(1, len(decks) - 1)]
    path = os.path.join(SOURCE, deck["apkg"])

    if op == "fix":
        deck = decks[0]
        path = os.path.join(SOURCE, deck["apkg"])

        def edit(con):
            nid, flds = _first_basic_note(con)
            parts = flds.split(fs)
            suffix = " (Clarified in today's update.)"
            if not parts[1].endswith(suffix):
                parts[1] += suffix
            con.execute("update notes set flds=? where id=?", (fs.join(parts), nid))
        label = "clarified an answer in " + deck["name"].split("::")[-1]

    elif op == "reword":
        def edit(con):
            nid, flds = _first_basic_note(con)
            parts = flds.split(fs)
            if not parts[0].rstrip().endswith("— and why?"):
                parts[0] = parts[0].rstrip().rstrip("?") + " — and why?"
            con.execute("update notes set flds=? where id=?", (fs.join(parts), nid))
        label = ("reworded a question in " + deck["name"].split("::")[-1] +
                 " (GUID kept stable)")

    elif op == "add":
        def edit(con):
            if con.execute("select 1 from notes where guid='demo-extra'").fetchone():
                return
            cols = [c[1] for c in con.execute("pragma table_info(notes)")]
            row = list(con.execute("select * from notes order by id limit 1").fetchone())
            fields = dict(zip(cols, row))
            note_fields = fields["flds"].split(fs)
            note_fields[0] = "Which route gets epinephrine working fastest in anaphylaxis?"
            note_fields[1] = "Intramuscular, into the outer thigh."
            for i in range(2, len(note_fields)):
                note_fields[i] = ""
            fields.update(id=fields["id"] + 100000, guid="demo-extra",
                          flds=fs.join(note_fields), sfld=note_fields[0])
            con.execute("insert into notes values (%s)" % ",".join("?" * len(cols)),
                        [fields[c] for c in cols])
            ccols = [c[1] for c in con.execute("pragma table_info(cards)")]
            crow = list(con.execute("select * from cards order by id limit 1").fetchone())
            cfields = dict(zip(ccols, crow))
            cfields.update(id=cfields["id"] + 100000, nid=fields["id"])
            con.execute("insert into cards values (%s)" % ",".join("?" * len(ccols)),
                        [cfields[c] for c in ccols])
        label = "added a new card to " + deck["name"].split("::")[-1]

    elif op == "restyle":
        def edit(con):
            models = json.loads(con.execute("select models from col").fetchone()[0])
            for m in models.values():
                m["css"] = (m.get("css", "") +
                            "\n/* v2 look: bigger font, night-mode colors */")
            con.execute("update col set models=?", (json.dumps(models),))
        label = "restyled the card template (bigger font, night-mode colors)"

    else:
        raise ValueError(op)

    _edit_apkg(path, edit)
    _bump(deck["name"])
    return json.dumps({"label": label, "deck": deck["name"].split("::")[-1]})
