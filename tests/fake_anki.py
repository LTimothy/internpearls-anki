"""A fake Anki, just deep enough to drive the add-on's real flows under pytest.

`install()` registers stub `aqt` / `anki` modules in sys.modules and returns a
FakeAnki handle, so `internpearls.sync`, `internpearls.collection`, and
`internpearls.background` import and run without an Anki install. The point is to
test the flows those modules compose (sync end to end, snapshot/restore, template
deferral), not to reimplement Anki — every behavior here is the minimum the add-on
actually relies on:

- notes are matched by GUID on import, and an import OVERWRITES every field of a
  matched note (that's why the protected-fields snapshot/restore exists at all);
- note types are plain dicts with flds/tmpls/css, mutated in place;
- dialogs are recorded, and `_ask` answers come from a scripted queue;
- QueryOp is absent on purpose, so background work runs inline and assertable.

Import this before any `internpearls` module. The real package __init__ wires
menus and imports Qt-heavy dialogs, so conftest registers the package with its
__path__ but never executes __init__.py — submodules import cleanly on their own.
"""
import json
import os
import sqlite3
import sys
import tempfile
import types
import zipfile

FS = "\x1f"

BASIC_FIELDS = ["Front", "Back", "Why", "Image", "Tag", "Dosing", "Notes"]


def make_model(name="Study Deck - Basic", fields=None, css=".card { color: black; }",
               qfmt="{{Front}}", afmt="{{Back}}"):
    return {
        "name": name,
        "flds": [{"name": f} for f in (fields or list(BASIC_FIELDS))],
        "tmpls": [{"name": "c", "qfmt": qfmt, "afmt": afmt, "ord": 0}],
        "css": css,
        "id": abs(hash(name)) % 10**9,
    }


def make_apkg(path, notes, model=None):
    """Write a fake .apkg the add-on can fully process.

    `notes`: list of (guid, fields_list, tags_string). Includes the col.models JSON
    (so _template_changes has something to read) and a tags column (so imported
    notes land under the add-on's scope tag).
    """
    model = model or make_model()
    db = path + ".tmp.db"
    if os.path.exists(db):
        os.remove(db)
    con = sqlite3.connect(db)
    con.execute("create table notes "
                "(id integer primary key, guid text, flds text, tags text)")
    for i, (guid, fields, tags) in enumerate(notes, 1):
        con.execute("insert into notes (id, guid, flds, tags) values (?, ?, ?, ?)",
                    (i, guid, FS.join(fields), tags))
    con.execute("create table col (models text)")
    con.execute("insert into col (models) values (?)",
                (json.dumps({str(model["id"]): model}),))
    con.commit()
    con.close()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(db, "collection.anki2")
    os.remove(db)


class FakeNote:
    def __init__(self, nid, guid, model, values, tags):
        self.id, self.guid, self.model, self.tags = nid, guid, model, list(tags)
        self._names = [f["name"] for f in model["flds"]]
        self.fields = list(values) + [""] * (len(self._names) - len(values))

    def __contains__(self, name):
        return name in self._names

    def __getitem__(self, name):
        return self.fields[self._names.index(name)]

    def __setitem__(self, name, value):
        self.fields[self._names.index(name)] = value


class _Models:
    def __init__(self, models):
        self._models = models

    def all(self):
        return self._models

    def new_field(self, name):
        return {"name": name}

    def add_field(self, model, field):
        model["flds"].append(field)

    def update_dict(self, model):
        pass   # dicts are mutated in place


class _Decks:
    def __init__(self):
        self.names = {}

    def id_for_name(self, name):
        return self.names.get(name)


class _Db:
    def __init__(self, col):
        self._col = col

    def scalar(self, _query, guid):
        for n in self._col._notes.values():
            if n.guid == guid:
                return n.id
        return None


class FakeCollection:
    def __init__(self):
        self._notes = {}
        self._next_id = 1
        self.models = _Models([make_model()])
        self.decks = _Decks()
        self.db = _Db(self)
        self.imports = []   # paths passed to import_anki_package, for assertions

    # -- helpers for tests -------------------------------------------------
    def add_note(self, guid, values, tags):
        model = self.models.all()[0]
        note = FakeNote(self._next_id, guid, model, values, tags)
        self._notes[self._next_id] = note
        self._next_id += 1
        return note

    def note_by_guid(self, guid):
        return next(n for n in self._notes.values() if n.guid == guid)

    # -- surface the add-on calls -------------------------------------------
    def find_notes(self, search):
        # The add-on only ever searches '"tag:X" OR "tag:X::*"'; match that scope.
        tag = search.split('"tag:', 1)[1].split('"', 1)[0] if "tag:" in search else ""
        out = []
        for nid, n in self._notes.items():
            if any(t == tag or t.startswith(tag + "::") for t in n.tags):
                out.append(nid)
        return out

    def get_note(self, nid):
        return self._notes[nid]

    def update_note(self, note):
        pass   # FakeNote is mutated in place

    def import_anki_package(self, request):
        """Anki's importer, reduced to what the add-on depends on: match by GUID;
        a matched note gets EVERY field overwritten (scheduling is out of scope
        here); an unmatched note is added as new, tags included."""
        self.imports.append(request.package_path)
        model = self.models.all()[0]
        by_guid = {n.guid: n for n in self._notes.values()}
        with zipfile.ZipFile(request.package_path) as z:
            with tempfile.TemporaryDirectory() as d:
                z.extract("collection.anki2", d)
                con = sqlite3.connect(os.path.join(d, "collection.anki2"))
                rows = con.execute("select guid, flds, tags from notes").fetchall()
                con.close()
        for guid, flds, tags in rows:
            values = flds.split(FS)
            existing = by_guid.get(guid)
            if existing:
                existing.fields = list(values) + \
                    [""] * (len(existing._names) - len(values))
            else:
                self.add_note(guid, values, tags.split())

    def export_anki_package(self, out_path, options, limit):
        with open(out_path, "wb") as fh:
            fh.write(b"fake backup")
        return len(self._notes)


class Gui:
    """Recorded dialog traffic. `answers` is a FIFO for _ask; empty means True."""

    def __init__(self):
        self.infos, self.warnings, self.tooltips, self.asks = [], [], [], []
        self.answers = []

    def ask(self, text, **kw):
        self.asks.append(text)
        return self.answers.pop(0) if self.answers else True


class FakeMW:
    def __init__(self, gui):
        self.col = FakeCollection()
        self._config = {}
        self.addonManager = types.SimpleNamespace(
            getConfig=lambda pkg: dict(self._config),
            writeConfig=lambda pkg, cfg: self._config.update(cfg))
        self.progress = types.SimpleNamespace(
            start=lambda **kw: None, update=lambda **kw: None,
            finish=lambda: None)
        self.pm = types.SimpleNamespace(backupFolder=lambda: tempfile.gettempdir())
        self.reset_count = 0
        self._gui = gui

    def reset(self):
        self.reset_count += 1

    def onOpenBackup(self):
        pass


class FakeAnki:
    """Handle returned by install(): .mw, .col, .gui for driving and asserting."""

    def __init__(self):
        self.gui = Gui()
        self.mw = FakeMW(self.gui)

    @property
    def col(self):
        return self.mw.col


def install():
    fake = FakeAnki()
    gui, mw = fake.gui, fake.mw

    aqt = types.ModuleType("aqt")
    aqt.mw = mw
    aqt.gui_hooks = types.SimpleNamespace()

    aqt_qt = types.ModuleType("aqt.qt")

    class _Qt:
        class CursorShape:
            WaitCursor = 0

    class _QApplication:
        @staticmethod
        def setOverrideCursor(cursor):
            pass

        @staticmethod
        def restoreOverrideCursor():
            pass

    class _QTimer:
        def __init__(self, parent=None):
            self.started = None

        def start(self, ms):
            self.started = ms

        def stop(self):
            pass

        def connect(self, fn):
            pass

        timeout = property(lambda self: types.SimpleNamespace(connect=lambda fn: None))

        @staticmethod
        def singleShot(ms, fn):
            pass   # tests call background checks directly, never via timers

    for name, obj in (("Qt", _Qt), ("QApplication", _QApplication),
                      ("QTimer", _QTimer), ("QLabel", object),
                      ("QPushButton", object), ("QAction", object),
                      ("QMenu", object)):
        setattr(aqt_qt, name, obj)

    aqt_utils = types.ModuleType("aqt.utils")
    aqt_utils.showInfo = lambda text, **kw: gui.infos.append(text)
    aqt_utils.showWarning = lambda text, **kw: gui.warnings.append(text)
    aqt_utils.askUser = gui.ask
    aqt_utils.getText = lambda text, **kw: ("", False)
    aqt_utils.tooltip = lambda text, **kw: gui.tooltips.append(text)
    aqt_utils.getFile = lambda *a, **kw: None
    aqt_utils.getSaveFile = lambda *a, **kw: None
    aqt_utils.openLink = lambda url: None

    # No aqt.operations on purpose: background._run_in_background falls back to
    # running work() inline, which is exactly what a deterministic test wants.

    anki = types.ModuleType("anki")
    anki_collection = types.ModuleType("anki.collection")

    class ImportAnkiPackageOptions:
        pass

    class ImportAnkiPackageRequest:
        def __init__(self, package_path=None, options=None):
            self.package_path, self.options = package_path, options

    class ExportAnkiPackageOptions:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class DeckIdLimit:
        def __init__(self, deck_id=None):
            self.deck_id = deck_id

    for name, obj in (("ImportAnkiPackageOptions", ImportAnkiPackageOptions),
                      ("ImportAnkiPackageRequest", ImportAnkiPackageRequest),
                      ("ExportAnkiPackageOptions", ExportAnkiPackageOptions),
                      ("DeckIdLimit", DeckIdLimit)):
        setattr(anki_collection, name, obj)
    anki.collection = anki_collection

    sys.modules["aqt"] = aqt
    sys.modules["aqt.qt"] = aqt_qt
    sys.modules["aqt.utils"] = aqt_utils
    sys.modules.pop("aqt.operations", None)
    sys.modules["anki"] = anki
    sys.modules["anki.collection"] = anki_collection
    return fake
