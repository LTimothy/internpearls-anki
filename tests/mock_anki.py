"""A mock Anki, deep enough to run the add-on's real code without an Anki install.

`install()` registers stub `aqt` / `anki` modules in sys.modules and returns a
MockAnki handle. Two consumers share it, which is the whole point — one mock,
no parallel implementations:

- pytest (tests/conftest.py) drives the real sync/collection/background flows
  and, via the widget layer below, the real Qt dialog code in dialogs.py and
  the real menu construction in __init__.py;
- the GitHub Pages live demo runs this same file under Pyodide, so the demo's
  behavior IS the add-on's code, not a re-implementation.

Every behavior here is the minimum the add-on actually relies on:

- notes match by GUID on import, and an import OVERWRITES every field of a
  matched note (that's why the protected-fields snapshot/restore exists);
- note types are dicts (multiple, keyed by the .apkg's own models when it has
  a `col` table) mutated in place;
- dialogs pause the flow and replay: every dialog call goes through
  Gui.next_interaction(), which either pops a scripted response (pytest, or a
  response the demo driver recorded from a real click) or raises
  NeedInteraction so the driver can show the dialog and re-run the flow from a
  snapshot with the response appended. Flows are deterministic, so replay is
  exact. In non-interactive mode (pytest default), info/warn just record and
  askUser answers from the `answers` queue (True when empty).
- QueryOp is absent on purpose, so background work runs inline and assertable.

The real package __init__ wires menus and imports the Qt-heavy dialogs module;
conftest registers the package with just its __path__ so submodules import
without executing __init__.py. To exercise the real menu, call
load_addon_init(), which imports __init__.py under these stubs and captures
what it builds.
"""
import copy
import importlib
import json
import os
import sqlite3
import sys
import tempfile
import types
import zipfile

FS = "\x1f"

BASIC_FIELDS = ["Front", "Back", "Why", "Image", "Tag", "Dosing", "Notes"]

_gui = None            # set by install(); the dialog/replay hub
_widgets = {}          # wid -> widget, rebuilt on every flow (re)run
_widget_seq = [0]
# Menu actions live as long as the app, not one flow — they get their own id
# namespace so reset_run() (which resets per-flow widget ids for deterministic
# replay) can't orphan them.
_persistent = {}
_persistent_seq = [0]


def reset_run():
    """Start a fresh (re)run of a flow: widget ids must be deterministic across
    replays, and per-run dialog records start empty (interactions persist —
    they're the script being replayed)."""
    _widgets.clear()
    _widget_seq[0] = 0
    if _gui:
        _gui.cursor = 0
        for lst in (_gui.infos, _gui.warnings, _gui.tooltips, _gui.asks,
                    _gui.payloads):
            lst.clear()


def _new_wid(w):
    _widget_seq[0] += 1
    wid = "w%d" % _widget_seq[0]
    _widgets[wid] = w
    return wid


def _new_persistent_wid(w):
    _persistent_seq[0] += 1
    wid = "m%d" % _persistent_seq[0]
    _persistent[wid] = w
    return wid


class NeedInteraction(BaseException):
    """The flow needs the user. BaseException so the add-on's _safe/_bg_safe
    decorators (which catch Exception) let it propagate to the driver."""

    def __init__(self, payload):
        super().__init__("NeedInteraction")
        self.payload = payload


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
    """Write a mock .apkg the add-on can fully process.

    `notes`: list of (guid, fields_list, tags_string). Includes the col.models
    JSON (so _template_changes has something to read) and a tags column (so
    imported notes land under the add-on's scope tag).
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


# ============================== collection ==============================
class MockNote:
    def __init__(self, nid, guid, model, values, tags, deck=None):
        self.id, self.guid, self.model, self.tags = nid, guid, model, list(tags)
        self.deck = deck
        self._card_ids = []   # populated by MockCollection.add_note
        self._resize(values)

    def card_ids(self):
        return list(self._card_ids)

    def _resize(self, values):
        names = [f["name"] for f in self.model["flds"]]
        self._names = names
        self.fields = list(values)[:len(names)] + \
            [""] * max(0, len(names) - len(values))

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

    def by_name(self, name):
        return next((m for m in self._models if m["name"] == name), None)

    def new_field(self, name):
        return {"name": name}

    def add_field(self, model, field):
        model["flds"].append(field)
        col = getattr(self, "_col", None)
        if col is not None:
            col.scm += 1   # adding a field is a real schema change (bumps schema mod)
            for n in col._notes.values():
                if n.model is model:
                    n._resize(n.fields)

    def update_dict(self, model):
        pass   # dicts are mutated in place


class _Decks:
    def __init__(self):
        self.names = {}

    def id_for_name(self, name):
        return self.names.get(name)

    def id(self, name, create=True):
        """Anki's decks.id(): return the deck id, creating the deck if absent."""
        if name not in self.names:
            if not create:
                return None
            self.names[name] = len(self.names) + 1
        return self.names[name]

    def name(self, did):
        """Anki's decks.name(): reverse lookup, deck id -> deck name."""
        for n, i in self.names.items():
            if i == did:
                return n
        return None


class _Db:
    def __init__(self, col):
        self._col = col

    def scalar(self, _query, guid):
        for n in self._col._notes.values():
            if n.guid == guid:
                return n.id
        return None


def _read_apkg(path):
    """(notes, models_by_mid, deck_by_nid) from a real or mock .apkg. Every
    table beyond `notes` is optional so minimal test fixtures keep working."""
    with zipfile.ZipFile(path) as z:
        with tempfile.TemporaryDirectory() as d:
            z.extract("collection.anki2", d)
            con = sqlite3.connect(os.path.join(d, "collection.anki2"))
            try:
                rows = con.execute(
                    "select id, guid, flds, tags, mid from notes").fetchall()
            except sqlite3.OperationalError:
                rows = [(nid, g, f, t, None) for nid, g, f, t in con.execute(
                    "select id, guid, flds, coalesce(tags, '') from notes")]
            models, deck_by_nid = {}, {}
            try:
                models_json, decks_json = con.execute(
                    "select models, decks from col").fetchone()
                models = {int(k): v for k, v in json.loads(models_json).items()}
                decks = {int(k): v.get("name", "") for k, v in
                         json.loads(decks_json).items()}
                for nid, did in con.execute("select nid, did from cards"):
                    deck_by_nid.setdefault(nid, decks.get(did, ""))
            except sqlite3.OperationalError:
                try:
                    models_json = con.execute("select models from col").fetchone()[0]
                    models = {int(k): v for k, v in json.loads(models_json).items()}
                except sqlite3.OperationalError:
                    pass
            con.close()
    return rows, models, deck_by_nid


class MockCollection:
    def __init__(self):
        self._notes = {}
        self._cards = {}    # cid -> SimpleNamespace(nid, did, queue); one card per note
        self._next_id = 1
        self._next_cid = 1
        self.scm = 0        # schema modification counter; only real schema changes bump it
        self.models = _Models([make_model()])
        self.models._col = self
        self.decks = _Decks()
        self.db = _Db(self)
        self.imports = []   # paths passed to import_anki_package, for assertions
        # Anki exposes suspend via col.sched and tag edits via col.tags; the add-on's
        # archive path (Reconcile) uses set_deck + these two. All are incremental (no
        # schema bump), which is exactly what the reconcile feature relies on.
        self.sched = types.SimpleNamespace(suspend_cards=self._suspend_cards)
        self.tags = types.SimpleNamespace(bulk_add=self._tags_bulk_add)

    # -- helpers for tests and the demo --------------------------------------
    def add_note(self, guid, values, tags, model=None, deck=None):
        model = model or self.models.all()[0]
        note = MockNote(self._next_id, guid, model, values, tags, deck)
        self._notes[self._next_id] = note
        self._next_id += 1
        cid = self._next_cid
        self._next_cid += 1
        did = self.decks.id(deck) if deck else None
        self._cards[cid] = types.SimpleNamespace(nid=note.id, did=did, queue=0)
        note._card_ids.append(cid)
        return note

    def note_by_guid(self, guid):
        return next(n for n in self._notes.values() if n.guid == guid)

    # -- card-level surfaces the archive path uses ----------------------------
    def set_deck(self, cids, did):
        for cid in cids:
            self._cards[cid].did = did

    def _suspend_cards(self, cids):
        for cid in cids:
            self._cards[cid].queue = -1   # -1 is Anki's suspended queue

    def _tags_bulk_add(self, nids, tag):
        for nid in nids:
            note = self._notes[nid]
            for t in tag.split():
                if t not in note.tags:
                    note.tags.append(t)

    # -- surface the add-on calls ---------------------------------------------
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

    def get_card(self, cid):
        return self._cards[cid]

    def update_note(self, note):
        pass   # MockNote is mutated in place

    def _register_models(self, models_by_mid):
        for m in models_by_mid.values():
            if not self.models.by_name(m["name"]):
                self.models._models.append(json.loads(json.dumps(m)))

    def import_anki_package(self, request):
        """Anki's importer, reduced to what the add-on depends on: match by GUID;
        a matched note gets EVERY field overwritten (scheduling is out of scope
        here); an unmatched note is added as new, tags and deck included."""
        self.imports.append(request.package_path)
        rows, models_by_mid, deck_by_nid = _read_apkg(request.package_path)
        self._register_models(models_by_mid)
        by_guid = {n.guid: n for n in self._notes.values()}
        for nid, guid, flds, tags, mid in rows:
            values = flds.split(FS)
            model = (self.models.by_name(models_by_mid[mid]["name"])
                     if mid in models_by_mid else self.models.all()[0])
            deck = deck_by_nid.get(nid)
            existing = by_guid.get(guid)
            if existing:
                existing.fields = list(values)[:len(existing._names)] + \
                    [""] * max(0, len(existing._names) - len(values))
                if deck and not existing.deck:
                    existing.deck = deck
            else:
                note = self.add_note(guid, values, tags.split(), model, deck)
                if deck:
                    top = "::".join(deck.split("::")[:2]) or deck
                    self.decks.names.setdefault(top, len(self.decks.names) + 1)
                    self.decks.names.setdefault(deck, len(self.decks.names) + 1)

    def export_anki_package(self, out_path, options, limit):
        """A real (minimal) .apkg of the whole mock collection, so a backup made
        here can actually be re-imported through import_anki_package."""
        db = out_path + ".tmp.db"
        if os.path.exists(db):
            os.remove(db)
        con = sqlite3.connect(db)
        con.execute("create table notes (id integer primary key, guid text, "
                    "flds text, tags text, mid integer)")
        con.execute("create table cards (id integer primary key, nid integer, "
                    "did integer)")
        models = {str(m["id"]): m for m in self.models.all()}
        deck_ids, decks = {}, {}
        for i, (nid, n) in enumerate(sorted(self._notes.items()), 1):
            did = deck_ids.setdefault(n.deck or "Default", len(deck_ids) + 1)
            decks[str(did)] = {"name": n.deck or "Default"}
            con.execute("insert into notes values (?, ?, ?, ?, ?)",
                        (nid, n.guid, FS.join(n.fields), " ".join(n.tags),
                         n.model["id"]))
            con.execute("insert into cards values (?, ?, ?)", (i, nid, did))
        con.execute("create table col (models text, decks text)")
        con.execute("insert into col values (?, ?)",
                    (json.dumps(models), json.dumps(decks)))
        con.commit()
        con.close()
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(db, "collection.anki2")
        os.remove(db)
        return len(self._notes)


# ============================== gui / replay ==============================
class Gui:
    """The dialog hub. Non-interactive (pytest default): info/warn record,
    askUser pops `answers` (True when empty). Interactive (the demo driver, or
    dialog tests): EVERY dialog goes through next_interaction — scripted
    responses replay, and running past the script raises NeedInteraction."""

    def __init__(self):
        self.infos, self.warnings, self.tooltips, self.asks = [], [], [], []
        self.answers = []        # non-interactive askUser script
        self.interactive = False
        self.interactions = []   # interactive-mode response script (the replay)
        self.cursor = 0
        self.payloads = []       # every payload surfaced this run, for asserting

    def next_interaction(self, payload):
        self.payloads.append(payload)
        if self.cursor < len(self.interactions):
            resp = self.interactions[self.cursor]
            self.cursor += 1
            return resp
        raise NeedInteraction(payload)

    def ask(self, text, **kw):
        self.asks.append(text)
        if not self.interactive:
            return self.answers.pop(0) if self.answers else True
        resp = self.next_interaction({"kind": "ask", "text": text})
        return bool(resp.get("answer"))

    def info(self, text, **kw):
        self.infos.append(text)
        if self.interactive:
            self.next_interaction({"kind": "info", "text": text})

    def warn(self, text, **kw):
        self.warnings.append(text)
        if self.interactive:
            self.next_interaction({"kind": "warn", "text": text})

    def prompt(self, text, **kw):
        if not self.interactive:
            return ("", False)
        resp = self.next_interaction({"kind": "prompt", "text": text,
                                      "default": kw.get("default", "")})
        return (resp.get("text", ""), bool(resp.get("ok")))

    def pick_file(self, payload):
        if not self.interactive:
            return None
        resp = self.next_interaction(payload)
        return resp.get("path")


# ============================== Qt widget layer ==============================
class Signal:
    def __init__(self):
        self._slots = []

    def connect(self, fn):
        self._slots.append(fn)

    def emit(self, *a):
        for fn in list(self._slots):
            fn()


class QWidget:
    def __init__(self, *a, **k):
        self.wid = _new_wid(self)
        self._style = ""
        self._layout = None
        self._tooltip = ""
        self._enabled = True

    def setStyleSheet(self, s):
        self._style = s

    def setObjectName(self, n):
        pass

    def setToolTip(self, t):
        self._tooltip = t

    def setEnabled(self, v):
        self._enabled = v

    def setWordWrap(self, v):
        pass

    def setMinimumWidth(self, v):
        pass

    def setMinimumHeight(self, v):
        pass

    def setFrameShape(self, s):
        pass   # QScrollArea/QFrame are QFrame subclasses in real Qt

    def node(self):
        return {"t": "box", "id": self.wid, "style": self._style,
                "children": [self._layout.node()] if self._layout else []}


class QLabel(QWidget):
    def __init__(self, text="", *a, **k):
        super().__init__()
        self._text = text

    def text(self):
        return self._text

    def setText(self, t):
        self._text = t

    def node(self):
        return {"t": "label", "id": self.wid, "text": self._text,
                "style": self._style}


class QPushButton(QWidget):
    def __init__(self, label="", *a, **k):
        super().__init__()
        self._label = label
        self.clicked = Signal()

    def setFlat(self, v):
        pass

    def setText(self, t):
        self._label = t

    def text(self):
        return self._label

    def node(self):
        return {"t": "button", "id": self.wid, "label": self._label,
                "style": self._style, "enabled": self._enabled,
                "tooltip": self._tooltip}


class QCheckBox(QWidget):
    def __init__(self, label="", *a, **k):
        super().__init__()
        self._label = label
        self._checked = False

    def setChecked(self, v):
        self._checked = bool(v)

    def isChecked(self):
        return self._checked

    def node(self):
        return {"t": "check", "id": self.wid, "label": self._label,
                "checked": self._checked, "style": self._style}


class QLineEdit(QWidget):
    class EchoMode:
        Normal, Password = 0, 2

    def __init__(self, text="", *a, **k):
        super().__init__()
        self._text = text
        self._placeholder = ""
        self._password = False

    def setText(self, t):
        self._text = t

    def text(self):
        return self._text

    def setPlaceholderText(self, t):
        self._placeholder = t

    def setEchoMode(self, m):
        self._password = (m == QLineEdit.EchoMode.Password)

    def node(self):
        return {"t": "line", "id": self.wid, "value": self._text,
                "placeholder": self._placeholder, "password": self._password}


class QSpinBox(QWidget):
    def __init__(self, *a, **k):
        super().__init__()
        self._value, self._min, self._max, self._suffix = 0, 0, 99, ""

    def setRange(self, lo, hi):
        self._min, self._max = lo, hi

    def setValue(self, v):
        self._value = int(v)

    def value(self):
        return self._value

    def setSuffix(self, s):
        self._suffix = s

    def node(self):
        return {"t": "spin", "id": self.wid, "value": self._value,
                "min": self._min, "max": self._max, "suffix": self._suffix}


class _Layout:
    kind = "col"

    def __init__(self, parent=None):
        self.wid = _new_wid(self)
        self._children = []
        if parent is not None and isinstance(parent, QWidget):
            parent._layout = self

    def addWidget(self, w, *a):
        self._children.append(w)

    def addLayout(self, l):
        self._children.append(l)

    def addStretch(self, *a):
        pass

    def setSpacing(self, v):
        pass

    def setContentsMargins(self, *a):
        pass

    def node(self):
        return {"t": self.kind, "id": self.wid,
                "children": [c.node() for c in self._children]}


class QVBoxLayout(_Layout):
    kind = "col"


class QHBoxLayout(_Layout):
    kind = "row"


class QFrame(QWidget):
    class Shape:
        NoFrame = 0

    def setFrameShape(self, s):
        pass

    def node(self):
        return {"t": "frame", "id": self.wid, "style": self._style,
                "children": [self._layout.node()] if self._layout else []}


class QScrollArea(QWidget):
    def __init__(self, *a, **k):
        super().__init__()
        self._widget = None

    def setWidgetResizable(self, v):
        pass

    def setWidget(self, w):
        self._widget = w

    def node(self):
        return {"t": "scroll", "id": self.wid,
                "children": [self._widget.node()] if self._widget else []}


class QDialogButtonBox(QWidget):
    class ButtonRole:
        AcceptRole, ApplyRole, RejectRole = 0, 1, 2

    class StandardButton:
        # Qt's real flag values, so `Ok | Cancel` works exactly as in aqt.qt.
        Ok, Cancel = 0x400, 0x400000

    def __init__(self, standard=None, *a, **k):
        super().__init__()
        self._buttons = []
        self.accepted = Signal()
        self.rejected = Signal()
        if isinstance(standard, int):
            if standard & QDialogButtonBox.StandardButton.Ok:
                self.addButton(QDialogButtonBox.StandardButton.Ok)
            if standard & QDialogButtonBox.StandardButton.Cancel:
                self.addButton(QDialogButtonBox.StandardButton.Cancel)

    def addButton(self, arg, role=None):
        if arg == QDialogButtonBox.StandardButton.Ok:
            btn = QPushButton("OK")
            btn.clicked.connect(self.accepted.emit)
        elif arg == QDialogButtonBox.StandardButton.Cancel:
            btn = QPushButton("Cancel")
            btn.clicked.connect(self.rejected.emit)
        else:
            btn = QPushButton(str(arg))
        self._buttons.append(btn)
        return btn

    def node(self):
        return {"t": "buttons", "id": self.wid,
                "children": [b.node() for b in self._buttons]}


def _apply_events(events):
    """Replay one recorded user interaction into the live widget tree."""
    for ev in events.get("events", []):
        w = _widgets.get(ev.get("id"))
        if w is None:
            continue
        if "value" in ev:
            if isinstance(w, QCheckBox):
                w.setChecked(ev["value"])
            elif isinstance(w, QLineEdit):
                w.setText(str(ev["value"]))
            elif isinstance(w, QSpinBox):
                w.setValue(ev["value"])
        if ev.get("click"):
            w.clicked.emit()


class QDialog(QWidget):
    def __init__(self, parent=None, *a, **k):
        super().__init__()
        self._title = ""
        self._result = None

    def setWindowTitle(self, t):
        self._title = t

    def accept(self):
        self._result = 1

    def reject(self):
        self._result = 0

    def exec(self):
        while self._result is None:
            resp = _gui.next_interaction({"kind": "dialog", "id": self.wid,
                                          "title": self._title,
                                          "tree": self.node()})
            _apply_events(resp)
        return self._result


class QMessageBox(QWidget):
    class Icon:
        Question, Information, Warning = 0, 1, 2

    class ButtonRole:
        AcceptRole = 0

    class StandardButton:
        Ok, Cancel = 0x400, 0x400000

    def __init__(self, parent=None):
        super().__init__()
        self._title, self._text = "", ""
        self._buttons = []
        self._clicked = None

    def setWindowTitle(self, t):
        self._title = t

    def setIcon(self, i):
        pass

    def setTextFormat(self, f):
        pass

    def setText(self, t):
        self._text = t

    def setStandardButtons(self, b):
        self.addButton(b)

    def addButton(self, arg, role=None):
        if arg == QMessageBox.StandardButton.Ok:
            label = "OK"
        elif arg == QMessageBox.StandardButton.Cancel:
            label = "Cancel"
        else:
            label = str(arg)
        btn = QPushButton(label)
        self._buttons.append(btn)
        return btn

    def clickedButton(self):
        return self._clicked

    def exec(self):
        resp = _gui.next_interaction({
            "kind": "msgbox", "id": self.wid, "title": self._title,
            "text": self._text,
            "buttons": [{"id": b.wid, "label": b._label} for b in self._buttons]})
        for ev in resp.get("events", []):
            if ev.get("click") and ev["id"] in _widgets:
                self._clicked = _widgets[ev["id"]]
        return 0


# ============================== menu recording ==============================
class QAction:
    class MenuRole:
        NoRole = 0

    def __init__(self, label="", parent=None):
        self.wid = _new_persistent_wid(self)
        self._label = label
        self.triggered = Signal()

    def setMenuRole(self, r):
        pass


class QMenu:
    def __init__(self, label="", parent=None):
        self.wid = _new_persistent_wid(self)
        self._label = label
        self._items = []   # ("action", QAction) | ("sep", None) | ("menu", QMenu)

    def addAction(self, act):
        self._items.append(("action", act))

    def addSeparator(self):
        self._items.append(("sep", None))

    def addMenu(self, label):
        m = QMenu(label)
        self._items.append(("menu", m))
        return m

    def menuAction(self):
        return self

    def tree(self):
        out = []
        for kind, item in self._items:
            if kind == "sep":
                out.append({"t": "sep"})
            elif kind == "action":
                out.append({"t": "item", "id": item.wid,
                            "label": item._label.replace("&", "")})
            else:
                out.append({"t": "menu", "label": item._label.replace("&", ""),
                            "items": item.tree()})
        return out


def trigger_action(wid):
    """Fire a recorded menu action by widget id — the real function runs."""
    _persistent[wid].triggered.emit()


# ============================== mw / modules ==============================
class MockMW:
    def __init__(self, gui):
        self.col = MockCollection()
        self._config = {}
        self.addonManager = types.SimpleNamespace(
            getConfig=lambda pkg: dict(self._config),
            writeConfig=lambda pkg, cfg: (self._config.clear(),
                                          self._config.update(cfg)))
        self.progress = types.SimpleNamespace(
            start=lambda **kw: None, update=lambda **kw: None,
            finish=lambda: None)
        self.pm = types.SimpleNamespace(backupFolder=lambda: tempfile.gettempdir())
        self.reset_count = 0
        self._gui = gui
        self._menus = []
        menubar = types.SimpleNamespace(
            insertMenu=lambda before, menu: self._menus.append(menu))
        self.form = types.SimpleNamespace(
            menubar=menubar,
            menuHelp=types.SimpleNamespace(menuAction=lambda: None),
            menuTools=types.SimpleNamespace(addMenu=self._menus.append))

    def reset(self):
        self.reset_count += 1

    def onOpenBackup(self):
        self._gui.tooltips.append("(Anki's own backup picker would open here)")


class MockAnki:
    """Handle returned by install(): .mw, .col, .gui for driving and asserting."""

    def __init__(self):
        self.gui = Gui()
        self.mw = MockMW(self.gui)

    @property
    def col(self):
        return self.mw.col


class Runner:
    """Deterministic replay driver, shared by dialog tests and the browser demo.

    A flow that needs the user raises NeedInteraction mid-run. Since a run may
    already have mutated the collection, config, or on-disk state before the
    pause, every re-run starts from a snapshot taken at start(): the flow
    replays deterministically through the recorded responses and continues past
    them. `paths` lists files/directories of persistent add-on state to include
    in the snapshot (installed.json, the user_files backups dir).

    start(fn) / feed(response) each return {"status": "done"} or
    {"status": "need", "payload": <dialog description>} — a shape a JS driver
    can act on directly; drive() is the synchronous convenience loop for tests.
    """

    def __init__(self, mock, paths=()):
        self.mock = mock
        self.paths = list(paths)
        self._fn = None
        self._snap = None

    def _files(self):
        out = {}
        for p in self.paths:
            if os.path.isdir(p):
                for root, _, files in os.walk(p):
                    for f in files:
                        full = os.path.join(root, f)
                        out[full] = open(full, "rb").read()
            elif os.path.exists(p):
                out[p] = open(p, "rb").read()
        return out

    def _take(self):
        return {"col": copy.deepcopy(self.mock.mw.col),
                "config": dict(self.mock.mw._config),
                "files": self._files()}

    def _restore(self):
        self.mock.mw.col = copy.deepcopy(self._snap["col"])
        self.mock.mw._config = dict(self._snap["config"])
        for p in list(self._files()):
            if p not in self._snap["files"]:
                os.remove(p)
        for p, data in self._snap["files"].items():
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as fh:
                fh.write(data)

    def start(self, fn):
        self._fn = fn
        self._snap = self._take()
        self.mock.gui.interactions = []
        self.mock.gui.interactive = True
        return self._go()

    def feed(self, response):
        self.mock.gui.interactions.append(response)
        return self._go()

    def _go(self):
        self._restore()
        reset_run()
        try:
            self._fn()
            return {"status": "done"}
        except NeedInteraction as e:
            return {"status": "need", "payload": e.payload}

    def drive(self, fn, respond, max_rounds=30):
        r = self.start(fn)
        for _ in range(max_rounds):
            if r["status"] == "done":
                return
            r = self.feed(respond(r["payload"]))
        raise AssertionError("dialog flow did not converge")


def load_addon_init():
    """Import the real internpearls/__init__.py under these stubs, fire the
    main-window hook it registers, and return the recorded top-level QMenu."""
    aqt = sys.modules["aqt"]
    importlib.import_module("internpearls.__init__")
    for hook in aqt.gui_hooks.main_window_did_init:
        hook()
    return aqt.mw._menus[0]


def install():
    global _gui
    mock = MockAnki()
    _gui = mock.gui
    gui, mw = mock.gui, mock.mw

    aqt = types.ModuleType("aqt")
    aqt.mw = mw
    aqt.gui_hooks = types.SimpleNamespace(main_window_did_init=[])

    aqt_qt = types.ModuleType("aqt.qt")

    class _Qt:
        class CursorShape:
            WaitCursor = 0

        class TextFormat:
            RichText = 1

    class _QApplication:
        @staticmethod
        def setOverrideCursor(cursor):
            pass

        @staticmethod
        def restoreOverrideCursor():
            pass

        @staticmethod
        def processEvents():
            pass

    class _QTimer:
        def __init__(self, parent=None):
            self.started = None

        def start(self, ms):
            self.started = ms

        def stop(self):
            pass

        timeout = property(lambda self: types.SimpleNamespace(connect=lambda fn: None))

        @staticmethod
        def singleShot(ms, fn):
            pass   # tests and the demo call background checks directly

    for name, obj in (("Qt", _Qt), ("QApplication", _QApplication),
                      ("QTimer", _QTimer), ("QLabel", QLabel),
                      ("QPushButton", QPushButton), ("QAction", QAction),
                      ("QMenu", QMenu), ("QCheckBox", QCheckBox),
                      ("QDialog", QDialog), ("QDialogButtonBox", QDialogButtonBox),
                      ("QFrame", QFrame), ("QHBoxLayout", QHBoxLayout),
                      ("QLineEdit", QLineEdit), ("QMessageBox", QMessageBox),
                      ("QScrollArea", QScrollArea), ("QSpinBox", QSpinBox),
                      ("QVBoxLayout", QVBoxLayout), ("QWidget", QWidget)):
        setattr(aqt_qt, name, obj)

    aqt_utils = types.ModuleType("aqt.utils")
    aqt_utils.showInfo = gui.info
    aqt_utils.showWarning = gui.warn
    aqt_utils.askUser = gui.ask
    aqt_utils.getText = gui.prompt
    aqt_utils.tooltip = lambda text, **kw: gui.tooltips.append(text)
    aqt_utils.getFile = lambda parent, title, cb=None, filter="", dir=None, key=None: \
        gui.pick_file({"kind": "file", "title": title, "dir": dir or ""})
    aqt_utils.getSaveFile = lambda parent, title, key, name, ext, fname="": \
        gui.pick_file({"kind": "savefile", "title": title, "fname": fname})
    aqt_utils.openLink = lambda url: None

    # No aqt.operations on purpose: background._run_in_background falls back to
    # running work() inline, which is exactly what a deterministic run wants.

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
    return mock
