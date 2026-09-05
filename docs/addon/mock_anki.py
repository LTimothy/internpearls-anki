"""A mock Anki, deep enough to run the add-on's real code without an Anki install.

`install()` registers stub `aqt` / `anki` modules in sys.modules and returns a
MockAnki handle. Two consumers share it, which is the whole point — one mock,
no parallel implementations:

- pytest (tests/conftest.py) drives the real sync/collection/background flows
  and, via the widget layer below, the real Qt dialog code in dialogs.py and
  the real menu construction in __init__.py;
- the GitHub Pages live demo runs this same file under Pyodide, so the demo's
  behavior IS the add-on's code, not a re-implementation.
- tools/render_dialog.py installs this world and then replaces the Qt layer below
  with real PyQt6, to render a dialog to a PNG. The two are opposites, not
  duplicates: the widgets here answer "what is the structure", cheaply and
  everywhere; real Qt answers "did it paint", which nothing here can. Keep this
  layer's setter surface honest (a no-op setFixedWidth is fine, a missing one is
  an AttributeError) so that swap keeps working.

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
import re
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
    _reset_chip_measurements()
    if _gui:
        _gui.cursor = 0
        for lst in (_gui.infos, _gui.warnings, _gui.tooltips, _gui.asks,
                    _gui.ask_buttons, _gui.ask_defaults, _gui.payloads):
            lst.clear()


def _reset_chip_measurements():
    """Drop widgets.py's cached chip-column widths, if that module is loaded yet.

    It measures a screen's chip set by building probe labels the first time that set
    is asked for, and every widget built here takes the next widget id. Cached across
    a replay, those probes are built on the first run of a flow and on no later one,
    which shifts every id after them on that run alone and makes a recorded click
    answer a different widget on the replay. Measuring afresh on every run is what
    keeps the ids identical, which is the driver's whole contract.
    """
    widgets = sys.modules.get("internpearls.widgets")
    if widgets is not None:
        widgets._CHIP_W.clear()


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
        "flds": [{"name": f, "ord": i}
                 for i, f in enumerate(fields or list(BASIC_FIELDS))],
        "tmpls": [{"name": "c", "qfmt": qfmt, "afmt": afmt, "ord": 0}],
        "css": css,
        "id": abs(hash(name)) % 10**9,
    }


def make_apkg(path, notes, model=None, deck=None, media=None):
    """Write a mock .apkg the add-on can fully process.

    `notes`: list of (guid, fields_list, tags_string). Includes the col.models
    JSON (so _template_changes has something to read) and a tags column (so
    imported notes land under the add-on's scope tag). `deck`, if given, also writes
    a `cards`/`decks` table (mirroring a real Anki export) so a fresh import lands
    each note in a real Anki deck of that name — needed for anything that checks
    where in the collection a synced note actually sits, not just that it exists.
    `media`, if given, is {filename: bytes}, written as the numbered blobs and JSON
    index a real export carries.
    """
    model = model or make_model()
    db = path + ".tmp.db"
    if os.path.exists(db):
        os.remove(db)
    con = sqlite3.connect(db)
    # `mid` mirrors a real export: Anki's notes table always carries the note type id,
    # and apkg_note_details joins on it to label each field (Front/Back/Why, which are
    # not the same names across our note types). Omitting it here would let the mock
    # pass with generic "Field N" labels that production never produces.
    con.execute("create table notes "
                "(id integer primary key, guid text, mid integer, flds text, tags text)")
    for i, (guid, fields, tags) in enumerate(notes, 1):
        con.execute("insert into notes (id, guid, mid, flds, tags) values (?, ?, ?, ?, ?)",
                    (i, guid, model["id"], FS.join(fields), tags))
    if deck:
        con.execute("create table cards "
                    "(id integer primary key, nid integer, did integer)")
        for i in range(1, len(notes) + 1):
            con.execute("insert into cards (id, nid, did) values (?, ?, 1)", (i, i))
        con.execute("create table col (models text, decks text)")
        con.execute("insert into col (models, decks) values (?, ?)",
                    (json.dumps({str(model["id"]): model}),
                     json.dumps({"1": {"name": deck}})))
    else:
        con.execute("create table col (models text)")
        con.execute("insert into col (models) values (?)",
                    (json.dumps({str(model["id"]): model}),))
    con.commit()
    con.close()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(db, "collection.anki2")
        if media:
            index = {}
            for i, (name, blob) in enumerate(media.items()):
                z.writestr(str(i), blob)
                index[str(i)] = name
            z.writestr("media", json.dumps(index))
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

    def note_type(self):
        return self.model

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

    def keys(self):
        return list(self._names)


class FsrsMemoryState:
    """Stands in for Anki's protobuf memory state: stability and difficulty, with the
    CopyFrom the add-on uses to clone one before halving its stability."""
    def __init__(self, stability=0.0, difficulty=0.0):
        self.stability, self.difficulty = stability, difficulty

    def CopyFrom(self, other):
        self.stability, self.difficulty = other.stability, other.difficulty


class ChangeNotetypeRequest:
    """Stands in for Anki's protobuf message, with its real field set.

    Deliberately strict about unknown fields. A protobuf raises when you assign one it
    does not define, and an earlier version of this stub was a plain namespace that
    happily accepted anything, so add-on code setting a field the real message lacks
    passed every test here and then failed on a real collection with "Protocol message
    ChangeNotetypeRequest has no ... field". A stub looser than the thing it stands in
    for turns a runtime error into a green test suite.

    Field names mirror anki.notetypes_pb2.ChangeNotetypeRequest (Anki 25.7).
    """
    _FIELDS = ("note_ids", "new_fields", "new_templates", "old_notetype_id",
               "new_notetype_id", "current_schema", "old_notetype_name", "is_cloze")

    def __init__(self):
        object.__setattr__(self, "note_ids", [])
        object.__setattr__(self, "new_fields", [])
        object.__setattr__(self, "new_templates", [])
        for name in ("old_notetype_id", "new_notetype_id", "current_schema"):
            object.__setattr__(self, name, 0)
        object.__setattr__(self, "old_notetype_name", "")
        object.__setattr__(self, "is_cloze", False)

    def __setattr__(self, name, value):
        if name not in self._FIELDS:
            raise AttributeError(
                f'Protocol message ChangeNotetypeRequest has no "{name}" field.')
        object.__setattr__(self, name, value)

    def CopyFrom(self, other):
        for name in self._FIELDS:
            value = getattr(other, name)
            object.__setattr__(self, name,
                               list(value) if isinstance(value, list) else value)


class _Models:
    def __init__(self, models):
        self._models = models

    def all(self):
        return self._models

    def by_name(self, name):
        return next((m for m in self._models if m["name"] == name), None)

    def new_field(self, name):
        return {"name": name}

    # === change-notetype surface (see collection.change_note_types) ===
    def change_notetype_info(self, *, old_notetype_id, new_notetype_id):
        req = ChangeNotetypeRequest()
        req.old_notetype_id, req.new_notetype_id = old_notetype_id, new_notetype_id
        return types.SimpleNamespace(input=req)

    def change_notetype_of_notes(self, req):
        """Reassign the notes' model and remap their field values by the caller's map,
        the way Anki's backend does. The mock keeps the note's cards as they are, which
        is the behaviour under test: converting must not discard review history."""
        new_model = next((m for m in self._models if m["id"] == req.new_notetype_id), None)
        for nid in req.note_ids:
            note = self._col._notes[nid]
            old = list(note.fields)
            note.model = new_model
            note._resize([old[i] if 0 <= i < len(old) else "" for i in req.new_fields])
            self._col._generate_cloze_cards(note)
        self._col.notetype_changes.append(list(req.note_ids))

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
        """Anki's decks.id(): return the deck id, creating the deck if absent.

        Every missing ancestor is created too, exactly as real Anki does: filing a card
        into "A::B::C" makes A and A::B real decks, which is what lets a deck-scoped
        export be limited to a parent path that no card sits in directly.
        """
        if name not in self.names:
            if not create:
                return None
            parts = name.split("::")
            for i in range(1, len(parts) + 1):
                self.names.setdefault("::".join(parts[:i]), len(self.names) + 1)
        return self.names[name]

    def name(self, did):
        """Anki's decks.name(): reverse lookup, deck id -> deck name."""
        for n, i in self.names.items():
            if i == did:
                return n
        return None

    def all_names_and_ids(self):
        """Anki's decks.all_names_and_ids(): every deck, name and id, for a picker
        that lists real decks (dupes_dialog's scope combos)."""
        return [types.SimpleNamespace(id=i, name=n)
               for n, i in sorted(self.names.items())]


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


class MockMedia:
    """Stands in for Anki's col.media: writes image bytes into the media folder.

    Mirrors write_data()'s real contract: a name collision with DIFFERENT bytes
    gets renamed (Anki dedupes by content hash), so a caller must use the returned
    filename, never the one it asked for. Files land in an in-memory dict rather
    than on disk; nothing here reads the filesystem.
    """
    def __init__(self):
        self._files = {}   # written filename -> bytes
        self._fail_after = None   # [n, exc], armed by fail_after(); None = never fails

    def fail_after(self, n, exc=None):
        """Test hook: make the Nth call (1-indexed) to write_data() raise `exc`
        (RuntimeError by default) instead of writing, to exercise a caller's
        exception-safety around a real backend/disk failure. Never armed by default:
        production and every other test are unaffected unless this is called."""
        self._fail_after = [n, exc or RuntimeError("mock media write failure")]

    def write_data(self, desired_fname, data):
        if self._fail_after:
            self._fail_after[0] -= 1
            if self._fail_after[0] == 0:
                raise self._fail_after[1]
        fname = desired_fname
        if fname in self._files and self._files[fname] != data:
            base, ext = os.path.splitext(desired_fname)
            i = 1
            while f"{base}-{i}{ext}" in self._files:
                i += 1
            fname = f"{base}-{i}{ext}"
        self._files[fname] = data
        return fname

    def add_file(self, path):
        with open(path, "rb") as fh:
            return self.write_data(os.path.basename(path), fh.read())


class MockCollection:
    def __init__(self):
        self._notes = {}
        self._cards = {}    # cid -> SimpleNamespace(nid, did, queue); one card per note
        self._next_id = 1
        self._next_cid = 1
        self._next_guid = 1
        self.scm = 0        # schema modification counter; only real schema changes bump it
        self.models = _Models([make_model()])
        self.models._col = self
        self.decks = _Decks()
        self.db = _Db(self)
        self.media = MockMedia()
        self.imports = []   # paths passed to import_anki_package, for assertions
        self.exports = []   # (path, options, limit) passed to export_anki_package
        self.updated_cards = []   # nids passed to update_card, for assertions
        self.notetype_changes = []   # note-id batches converted, for assertions
        # Undo entries the AI-import path relies on: each is {name, notes, cards}.
        # Every note-adding op below pushes its OWN entry (real Anki does this per
        # backend op too) unless _undo_merge_open points at one still being collected
        # (add_custom_undo_entry opens it, merge_undo_entries closes it), in which
        # case the op folds into that one instead. That's what lets a caller squash a
        # whole run of adds into a single undo() later. Scoped to notes/cards only;
        # nothing else here needs undo modeling.
        self._undo_entries = []
        self._undo_merge_open = None
        self._add_note_fail_after = None   # [n, exc], armed by fail_add_note_after()
        # Anki exposes suspend via col.sched and tag edits via col.tags; the add-on's
        # archive path (Reconcile) uses set_deck + these two. All are incremental (no
        # schema bump), which is exactly what the reconcile feature relies on.
        self.sched = types.SimpleNamespace(suspend_cards=self._suspend_cards,
                                          unsuspend_cards=self._unsuspend_cards)
        self.tags = types.SimpleNamespace(bulk_add=self._tags_bulk_add)

    # === undo: add_custom_undo_entry / merge_undo_entries / undo ===
    def add_custom_undo_entry(self, name):
        self._undo_entries.append({"name": name, "notes": [], "cards": []})
        target = len(self._undo_entries) - 1
        self._undo_merge_open = target
        return target

    def merge_undo_entries(self, target):
        entry = self._undo_entries[target]
        for e in self._undo_entries[target + 1:]:
            entry["notes"].extend(e["notes"])
            entry["cards"].extend(e["cards"])
        del self._undo_entries[target + 1:]
        self._undo_merge_open = None

    def undo(self):
        if not self._undo_entries:
            raise Exception("nothing to undo")
        entry = self._undo_entries.pop()
        for nid in entry["notes"]:
            note = self._notes.pop(nid, None)
            if note:
                for cid in note._card_ids:
                    self._cards.pop(cid, None)
        return types.SimpleNamespace(operation=entry["name"])

    # === new_note / add_note: real Anki's two-call add path ===
    def new_note(self, model):
        """Anki's col.new_note(): an unsaved note for `model`, with a placeholder
        guid a caller may overwrite before add_note() (real Anki assigns a random
        backend guid here; the AI-import path always replaces it with
        ai_logic.generated_guid())."""
        self._next_guid += 1
        return MockNote(0, f"mockguid-{self._next_guid}", model,
                        [""] * len(model["flds"]), [])

    def add_note(self, note_or_guid, values_or_did, tags=None, model=None, deck=None):
        """Dispatches on the first argument's type. Real Anki's add_note(note,
        deck_id) and this suite's long-standing test helper
        add_note(guid, values, tags, model=None, deck=None) share this name; a
        MockNote first argument means the real signature is the one being called.
        """
        if isinstance(note_or_guid, MockNote):
            return self._add_prepared_note(note_or_guid, values_or_did)
        return self._add_legacy_note(note_or_guid, values_or_did, tags, model, deck)

    def fail_add_note_after(self, n, exc=None):
        """Test hook: make the Nth call (1-indexed) to the real add_note(note, did)
        path raise `exc` (RuntimeError by default) instead of adding, so a test can
        exercise a caller's exception-safety around a multi-note write the way a real
        Anki backend failure would. Never armed by default: production and every
        other test are unaffected unless this is called."""
        self._add_note_fail_after = [n, exc or RuntimeError("mock add_note failure")]

    def _add_prepared_note(self, note, did):
        """Real add_note(note, deck_id): assigns note.id, files one card into `did`,
        and, like a fresh cloze note in real Anki, adds one card per further
        cloze deletion the note's own text carries. Records into the open undo
        entry of its own, exactly as real Anki does."""
        if self._add_note_fail_after:
            self._add_note_fail_after[0] -= 1
            if self._add_note_fail_after[0] == 0:
                raise self._add_note_fail_after[1]
        note.id = self._next_id
        self._next_id += 1
        note.deck = self.decks.name(did)
        self._notes[note.id] = note
        cid = self._next_cid
        self._next_cid += 1
        self._cards[cid] = types.SimpleNamespace(
            id=cid, nid=note.id, did=did, queue=0, reps=0, ord=0,
            type=0, due=0, ivl=0, factor=0, lapses=0,
            memory_state=None, desired_retention=None, decay=None,
            last_review_time=None, odid=0)
        note._card_ids.append(cid)
        self._generate_cloze_cards(note)
        # Real Anki gives every add its own undo entry; merge_undo_entries is what
        # collapses a run of them into one. Modelling that faithfully is what makes a
        # missing merge observable in a test rather than silently equivalent.
        self._undo_entries.append(
            {"name": "Add Note", "notes": [note.id], "cards": list(note._card_ids)})
        return types.SimpleNamespace(note_id=note.id)

    # === helpers for tests and the demo ===
    def _add_legacy_note(self, guid, values, tags, model=None, deck=None):
        model = model or self.models.all()[0]
        note = MockNote(self._next_id, guid, model, values, tags, deck)
        self._notes[self._next_id] = note
        self._next_id += 1
        cid = self._next_cid
        self._next_cid += 1
        did = self.decks.id(deck) if deck else None
        # Scheduling fields mirror Anki's card columns, so carry_scheduling_forward can
        # be exercised for real: it copies these across and the tests read them back.
        self._cards[cid] = types.SimpleNamespace(
            id=cid, nid=note.id, did=did, queue=0, reps=0, ord=0,
            type=0, due=0, ivl=0, factor=0, lapses=0,
            memory_state=None, desired_retention=None, decay=None,
            last_review_time=None, odid=0)
        note._card_ids.append(cid)
        return note

    def note_by_guid(self, guid):
        return next(n for n in self._notes.values() if n.guid == guid)

    # === card-level surfaces the archive path uses ===
    def set_deck(self, cids, did):
        for cid in cids:
            self._cards[cid].did = did

    def _suspend_cards(self, cids):
        for cid in cids:
            self._cards[cid].queue = -1   # -1 is Anki's suspended queue

    def _unsuspend_cards(self, cids):
        for cid in cids:
            self._cards[cid].queue = 0

    def file_in_filtered_deck(self, nid, filtered_deck, home_deck):
        """Test helper: put a note's card physically in `filtered_deck` with its
        home deck recorded via `odid`, the way a real filtered deck does."""
        did = self.decks.id(filtered_deck)
        odid = self.decks.id(home_deck)
        for cid in self._notes[nid]._card_ids:
            self._cards[cid].did = did
            self._cards[cid].odid = odid

    def _tags_bulk_add(self, nids, tag):
        for nid in nids:
            note = self._notes[nid]
            for t in tag.split():
                if t not in note.tags:
                    note.tags.append(t)

    # === surface the add-on calls ===
    def find_notes(self, search):
        # The add-on searches '"tag:X" OR "tag:X::*"', optionally with a trailing
        # ' -"tag:Y"' exclusion (see collection._her_notes_summary); a bare
        # 'deck:"X"' (collection.note_rows, by-deck scope); or "" for every note
        # (collection.note_rows with neither tag nor deck given).
        if search == "":
            return list(self._notes.keys())
        if 'deck:"' in search:
            deck = search.split('deck:"', 1)[1].split('"', 1)[0]
            out = []
            for nid, n in self._notes.items():
                if not n._card_ids:
                    continue
                card = self._cards[n._card_ids[0]]
                did = getattr(card, "odid", 0) or card.did
                name = self.decks.name(did) or ""
                if name == deck or name.startswith(deck + "::"):
                    out.append(nid)
            return out
        exclude = None
        if ' -"tag:' in search:
            search, excl_part = search.split(' -"tag:', 1)
            exclude = excl_part.rstrip('"')
        tag = search.split('"tag:', 1)[1].split('"', 1)[0] if "tag:" in search else ""
        out = []
        for nid, n in self._notes.items():
            if not any(t == tag or t.startswith(tag + "::") for t in n.tags):
                continue
            if exclude and any(t == exclude or t.startswith(exclude + "::")
                               for t in n.tags):
                continue
            out.append(nid)
        return out

    def get_note(self, nid):
        return self._notes[nid]

    def get_card(self, cid):
        return self._cards[cid]

    def get_empty_cards(self):
        """Anki's own empty-cards report, reduced to what the add-on reads.

        Real Anki renders every card and reports the ones that come out blank. For a
        cloze note that means a card whose ordinal has no matching {{cN::}} left in the
        text, which is exactly the leftover a deck source creates when it regroups a
        live cloze into fewer deletions. Computed from the note text here rather than
        stubbed, so a test can't pass against a report that disagrees with the note.

        Scans the WHOLE collection, like the real one: scoping to the learner's own
        notes is the add-on's job, and a mock that pre-filtered would hide a bug there.
        """
        notes = []
        for nid, note in self._notes.items():
            if "Cloze" not in note.model["name"]:
                continue
            live = {int(m) - 1 for m in re.findall(r"\{\{c(\d+)::", note.fields[0])}
            empty = [cid for cid in note._card_ids
                     if self._cards[cid].ord not in live]
            if empty:
                notes.append(types.SimpleNamespace(
                    note_id=nid, card_ids=empty,
                    will_delete_note=len(empty) == len(note._card_ids)))
        return types.SimpleNamespace(notes=notes, report="")

    def remove_cards_and_orphaned_notes(self, cids):
        """Remove cards, and any note left with none. The add-on is expected never to
        orphan a note this way; the mock still implements it so a regression that does
        shows up as a missing note rather than silently passing."""
        self.removed_cards = getattr(self, "removed_cards", []) + list(cids)
        for cid in cids:
            card = self._cards.pop(cid, None)
            if card is None:
                continue
            note = self._notes.get(card.nid)
            if note and cid in note._card_ids:
                note._card_ids.remove(cid)
        for nid, note in list(self._notes.items()):
            if not note._card_ids:
                del self._notes[nid]

    def _generate_cloze_cards(self, note):
        """Add a card per extra cloze deletion, the way Anki does after a conversion:
        the original card stays as ordinal 0, the rest are created new."""
        if "Cloze" not in note.model["name"]:
            return
        ords = sorted({int(m) for m in re.findall(r"\{\{c(\d+)::", note.fields[0])})
        for i, _o in enumerate(ords):
            if i < len(note._card_ids):
                continue
            cid = self._next_cid
            self._next_cid += 1
            first = self._cards[note._card_ids[0]]
            self._cards[cid] = types.SimpleNamespace(
                id=cid, nid=note.id, did=first.did, queue=0, reps=0, ord=i,
                type=0, due=0, ivl=0, factor=0, lapses=0,
                memory_state=None, desired_retention=None, decay=None,
                last_review_time=None, odid=getattr(first, "odid", 0))
            note._card_ids.append(cid)

    def update_card(self, card):
        # The namespace is already mutated in place, so this records the call rather
        # than applying it: in real Anki this is what persists the change, and a test
        # that only reads the namespace back would pass even if it were never called.
        self.updated_cards.append(card.nid)

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
                self._generate_cloze_cards(existing)
                if deck and not existing.deck:
                    existing.deck = deck
            else:
                # add_note files it through decks.id(), which registers the deck and
                # every ancestor of it, same as real Anki.
                self.add_note(guid, values, tags.split(), model, deck)

    def export_anki_package(self, out_path, options, limit):
        """A real (minimal) .apkg of the whole mock collection, so a backup made
        here can actually be re-imported through import_anki_package.

        Always written in the legacy (plain collection.anki2) shape, since the mock has
        no zstd either; `options` and `limit` are recorded in `self.exports` so a test
        can still assert which format the add-on ASKED Anki for (the part that decides
        whether a real backup is readable by this add-on's own readers) and which deck
        it asked for.
        """
        self.exports.append((out_path, options, limit))
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
    askUser pops `answers` (True when empty), getFile/getSaveFile pop
    `file_picks` (None when empty). Interactive (the demo driver, or
    dialog tests): EVERY dialog goes through next_interaction — scripted
    responses replay, and running past the script raises NeedInteraction."""

    def __init__(self):
        self.infos, self.warnings, self.tooltips, self.asks = [], [], [], []
        # The button labels of every named-button question, in (yes, no) order, and the
        # label ui._ask made the default on each. Recorded beside `asks` (which still
        # holds the text) so a test can assert the buttons name the action rather than
        # reading Yes/No, and that the default is the answer that costs nothing.
        self.ask_buttons = []
        self.ask_defaults = []
        # When True, every named-button question is answered by pressing Escape rather
        # than by clicking: the escape button is the declining one, so this is how a
        # test asserts that dismissing a question means the safe answer.
        self.escape_asks = False
        self.clipboard = []      # every text copy_to_clipboard() put on the clipboard
        self.answers = []        # non-interactive askUser script
        self.file_picks = []     # non-interactive getFile/getSaveFile script
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

    def ask(self, text, buttons=None, **kw):
        """One yes/no question, however it was rendered.

        `buttons` is (yes label, no label) when ui._ask built a named-button message box
        rather than a plain askUser, and it only changes what is recorded and what the
        interactive payload carries: the answer still comes back as a bool from the same
        `answers` queue or the same replayed response, so a question that gains named
        buttons doesn't change which mechanism a test answers it with.
        """
        self.asks.append(text)
        payload = {"kind": "ask", "text": text}
        if buttons:
            self.ask_buttons.append(tuple(buttons))
            payload["buttons"] = list(buttons)
        if buttons and self.escape_asks:
            # Escape and the close box activate the escape button, which ui._ask makes
            # the declining one. Answered here rather than from the script, in both
            # modes, since this is a keypress rather than a choice a test is scripting.
            self.payloads.append(payload)
            return False
        if not self.interactive:
            return self.answers.pop(0) if self.answers else True
        return bool(self.next_interaction(payload).get("answer"))

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
        if self.file_picks:
            return self.file_picks.pop(0)
        if not self.interactive:
            return None
        resp = self.next_interaction(payload)
        return resp.get("path")


# ============================== Qt widget layer ==============================
class QFont:
    """Enough of QFont for a label to carry a strikeout flag, the one property
    review.py's "never" decision reads back after setting it."""

    def __init__(self):
        self._strike_out = False

    def setStrikeOut(self, v):
        self._strike_out = bool(v)

    def strikeOut(self):
        return self._strike_out


class QFontMetrics:
    """Enough of QFontMetrics for a label-column width computed from text, the
    way ai_dialog._build_advanced sizes its own label column: a fixed width
    per character is not real font metrics, but it preserves the one thing
    that computation cares about, that a longer string measures wider than a
    shorter one."""

    def __init__(self, font=None):
        self._font = font

    def horizontalAdvance(self, text):
        return len(text) * 7


class Signal:
    def __init__(self):
        self._slots = []

    def connect(self, fn):
        self._slots.append(fn)

    def emit(self, *a):
        # Passed through rather than dropped: real Qt hands a slot the signal's own
        # arguments, and a slot that takes one (QCheckBox.toggled -> setEnabled) needs it.
        for fn in list(self._slots):
            fn(*a)


class pyqtSignal:
    """Enough of PyQt's class-level `foo = pyqtSignal()` declaration to support a
    widget that declares its own signal (ai_setup.ModelEffortControls' `changed`):
    a descriptor that hands each instance its own lazily-created Signal, mirroring
    real PyQt's per-instance bound-signal semantics rather than one Signal shared
    across every instance of the class."""

    def __init__(self, *a, **k):
        self._name = None

    def __set_name__(self, owner, name):
        self._name = name

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        key = f"_signal_{self._name}"
        sig = obj.__dict__.get(key)
        if sig is None:
            sig = Signal()
            obj.__dict__[key] = sig
        return sig


class QWidget:
    def __init__(self, *a, **k):
        self.wid = _new_wid(self)
        self._style = ""
        self._layout = None
        self._tooltip = ""
        self._accessible = ""
        self._enabled = True
        self._visible = True
        self.deleted = False

    def setStyleSheet(self, s):
        self._style = s

    def styleSheet(self):
        return self._style

    def layout(self):
        """The layout set on this widget, the way real Qt hands one back. ui.py reads it
        to place a checkbox inside a caller-built body rather than under it."""
        return self._layout

    def setObjectName(self, n):
        pass

    def setParent(self, parent):
        """Qt's reparenting, used to detach a widget the moment it is replaced
        rather than when deleteLater eventually runs. There is no parent chain
        here (see node()), and a widget already taken out of its layout is out of
        everything this mock walks, so this only has to exist."""
        pass

    def setToolTip(self, t):
        self._tooltip = t

    def setAccessibleName(self, name):
        self._accessible = name

    def deleteLater(self):
        """Qt's deferred delete. Nothing here owns C++ memory to free, so this records
        the call: a dialog parented to mw leaks in real Anki unless something asks for
        it, and this is what lets a flow test assert that something did."""
        self.deleted = True

    def setEnabled(self, v):
        self._enabled = v

    def isEnabled(self):
        return self._enabled

    def setVisible(self, v):
        self._visible = v

    def hide(self):
        self.setVisible(False)

    def isVisible(self):
        return self._visible

    def isVisibleTo(self, ancestor):
        # No parent chain to walk here (see node()): this widget's own visibility
        # is the only thing the mock has an opinion about, same simplification the
        # rest of this class already makes for geometry.
        return self._visible

    def setWordWrap(self, v):
        pass

    def setMinimumWidth(self, v):
        pass

    def setMinimumHeight(self, v):
        pass

    def setMaximumWidth(self, v):
        pass

    def setMaximumHeight(self, v):
        pass

    def setFixedHeight(self, v):
        pass

    def setFixedWidth(self, v):
        pass

    def resize(self, w, h):
        pass

    def setCursor(self, c):
        pass

    def setFont(self, font):
        self._font = font

    def font(self):
        if getattr(self, "_font", None) is None:
            self._font = QFont()
        return self._font

    def setFrameShape(self, s):
        pass   # QScrollArea/QFrame are QFrame subclasses in real Qt

    def setSizePolicy(self, *a):
        pass

    def height(self):
        # No real layout engine here, so no real geometry either: always 0, same as a
        # freshly constructed, never-shown real widget. qt_tests/ (real Qt, offscreen)
        # is what exercises actual scroll geometry.
        return 0

    def ensurePolished(self):
        pass   # no style engine here, so nothing a stylesheet could resolve into

    def update(self):
        pass   # Qt's repaint request; no paint engine here to schedule one on

    def sizeHint(self):
        # Same reasoning as height(): no font and no layout, so nothing to measure.
        # widgets.chip_column_width() therefore reads 0 under the mock, which is
        # correct for a suite that asserts structure; qt_tests/ measures the real one.
        return types.SimpleNamespace(width=lambda: 0, height=lambda: 0)

    def node(self):
        return {"t": "box", "id": self.wid, "style": self._style,
                "visible": self._visible,
                "children": [self._layout.node()] if self._layout else []}


class QImage:
    """Enough of QImage for the width cap review.py applies to an extracted picture.

    The mock suite never has real image data, so an existing file reports a fixed
    natural width and anything else reads as null, which is the branch that falls back
    to naming the image. qt_tests/ exercises the real one.
    """

    def __init__(self, path=""):
        self._ok = bool(path) and os.path.exists(path)

    def isNull(self):
        return not self._ok

    def width(self):
        return 800 if self._ok else 0


class QLabel(QWidget):
    def __init__(self, text="", *a, **k):
        super().__init__()
        self._text = text
        self.linkActivated = Signal()

    def text(self):
        return self._text

    def setText(self, t):
        self._text = t

    def setTextFormat(self, f):
        pass

    def setAlignment(self, a):
        pass

    def setOpenExternalLinks(self, v):
        pass

    def node(self):
        return {"t": "label", "id": self.wid, "text": self._text,
                "style": self._style, "strike": self.font().strikeOut()}


class QPushButton(QWidget):
    def __init__(self, label="", *a, **k):
        super().__init__()
        self._label = label
        self.clicked = Signal()
        self._checkable = False
        self._checked = False

    def setFlat(self, v):
        pass

    def setDefault(self, v):
        # Real QPushButton's "activated by a bare Return": nothing here reads it
        # back, since the mock has no keyboard-event loop to honour it, but a
        # button (e.g. a QDialogButtonBox's accept button) must be able to call
        # it without raising.
        self._default = bool(v)

    def setText(self, t):
        self._label = t

    def text(self):
        return self._label

    def setCheckable(self, v):
        self._checkable = bool(v)

    def setChecked(self, v):
        self._checked = bool(v)

    def isChecked(self):
        return self._checked

    def click(self):
        # Real QPushButton.click(): toggles checked state first (if checkable), then
        # emits clicked. decision_cell's own handler drives the real state change via
        # set_state, so the value emitted here is not load-bearing for that flow.
        if self._checkable:
            self.setChecked(not self._checked)
        self.clicked.emit(self._checked)

    def node(self):
        # `visible` like every other node: a button hidden once its job is done (Manage
        # decks' Clear link) is gone from the screen, and a tree that never says so
        # cannot tell a driver the difference between hidden and still offered.
        return {"t": "button", "id": self.wid, "label": self._label,
                "style": self._style, "enabled": self._enabled,
                "visible": self._visible,
                "tooltip": self._tooltip, "accessible": self._accessible,
                "checked": self._checked}


class QCheckBox(QWidget):
    def __init__(self, label="", *a, **k):
        super().__init__()
        self._label = label
        self._checked = False
        # Real QCheckBox emits this only when the state actually changes, which is what
        # a control wired to follow a checkbox (Settings' interval spinbox) rides on.
        self.toggled = Signal()

    def setChecked(self, v):
        v = bool(v)
        if v != self._checked:
            self._checked = v
            self.toggled.emit(v)

    def isChecked(self):
        return self._checked

    def node(self):
        return {"t": "check", "id": self.wid, "label": self._label,
                "checked": self._checked, "style": self._style,
                "tooltip": self._tooltip}


class QRadioButton(QWidget):
    """No auto-exclusive grouping: nothing here drives one, and no test yet needs
    clicking one radio to uncheck its sibling."""

    def __init__(self, label="", *a, **k):
        super().__init__()
        self._label = label
        self._checked = False
        self.toggled = Signal()

    def setChecked(self, v):
        v = bool(v)
        if v != self._checked:
            self._checked = v
            self.toggled.emit(v)

    def isChecked(self):
        return self._checked

    def setText(self, t):
        self._label = t

    def text(self):
        return self._label

    def node(self):
        return {"t": "radio", "id": self.wid, "label": self._label,
                "checked": self._checked}


class QButtonGroup(QWidget):
    """Just enough to make a set of radio buttons mutually exclusive and fire
    buttonToggled the way real QButtonGroup does when a member's checked state
    changes."""

    def __init__(self, *a, **k):
        super().__init__()
        self._buttons = []
        self.buttonToggled = Signal()

    def addButton(self, button):
        self._buttons.append(button)
        button.toggled.connect(lambda checked, b=button: self._on_toggled(b, checked))

    def _on_toggled(self, button, checked):
        if checked:
            for other in self._buttons:
                if other is not button and other.isChecked():
                    other.setChecked(False)
        self.buttonToggled.emit(button, checked)


class QComboBox(QWidget):
    def __init__(self, *a, **k):
        super().__init__()
        self._items = []       # display text per row
        self._data = []        # itemData per row, parallel to _items
        self._editable = False
        self._text = ""
        self._index = -1
        # Real QComboBox emits both on any change to the current selection or
        # (when editable) typed text; the AI wizard's model/effort controls
        # wire both, so the mock needs to actually fire them.
        self.currentTextChanged = Signal()
        self.currentIndexChanged = Signal()

    def setEditable(self, v):
        self._editable = bool(v)

    def addItem(self, text, data=None):
        self._items.append(text)
        self._data.append(data)
        if len(self._items) == 1:
            self._text, self._index = text, 0

    def addItems(self, texts):
        for t in texts:
            self.addItem(t)

    def clear(self):
        self._items, self._data = [], []
        self._text, self._index = "", -1

    def currentText(self):
        return self._text

    def currentData(self):
        return self._data[self._index] if 0 <= self._index < len(self._data) else None

    def currentIndex(self):
        return self._index

    def findData(self, data):
        return self._data.index(data) if data in self._data else -1

    def setCurrentIndex(self, i):
        if 0 <= i < len(self._items):
            self._index, self._text = i, self._items[i]
            self.currentIndexChanged.emit(i)
            self.currentTextChanged.emit(self._text)

    def setCurrentText(self, t):
        self._text = t
        if t not in self._items:
            self._items.append(t)
            self._data.append(None)
        self._index = self._items.index(t)
        self.currentTextChanged.emit(t)

    def setEditText(self, t):
        self.setCurrentText(t)

    def node(self):
        return {"t": "combo", "id": self.wid, "items": list(self._items),
                "value": self._text, "editable": self._editable}


class QStackedWidget(QWidget):
    def __init__(self, *a, **k):
        super().__init__()
        self._pages = []
        self._current = None

    def addWidget(self, w):
        self._pages.append(w)
        if self._current is None:
            self._current = w
        return len(self._pages) - 1

    def setCurrentWidget(self, w):
        self._current = w

    def currentWidget(self):
        return self._current

    def count(self):
        return len(self._pages)

    def node(self):
        return {"t": "stack", "id": self.wid,
                "children": [self._current.node()] if self._current else []}


class QLineEdit(QWidget):
    class EchoMode:
        Normal, Password = 0, 2

    def __init__(self, text="", *a, **k):
        super().__init__()
        self._text = text
        self._placeholder = ""
        self._password = False
        # Real QLineEdit emits this on a programmatic setText as well as on typing,
        # which is what a validation message wired to clear itself as the field is
        # edited rides on.
        self.textChanged = Signal()
        # Real QLineEdit emits this on Enter or on losing focus, not per keystroke;
        # the mock has no focus model, so tests trigger it directly.
        self.editingFinished = Signal()
        # Real QLineEdit emits this only for a user's own keystrokes, never for a
        # programmatic setText; the mock has no keyboard model either, so a test
        # triggers it directly, same as editingFinished above.
        self.textEdited = Signal()

    def setText(self, t):
        self._text = t
        self.textChanged.emit(t)

    def text(self):
        return self._text

    def setPlaceholderText(self, t):
        self._placeholder = t

    def setEchoMode(self, m):
        self._password = (m == QLineEdit.EchoMode.Password)

    def node(self):
        return {"t": "line", "id": self.wid, "value": self._text,
                "placeholder": self._placeholder, "password": self._password}


class QPlainTextEdit(QWidget):
    """The review dialog's per-card feedback box, and its read-only digest view."""

    def __init__(self, text="", *a, **k):
        super().__init__()
        self._text = text
        self._placeholder = ""
        self._readonly = False
        self._max_block_count = 0   # 0: unlimited, matches real QPlainTextEdit's default
        # Real QPlainTextEdit emits this on every edit; the review dialog connects it
        # to the debounced save, so a mock without it hides whether that wiring exists.
        self._changed = []
        self.textChanged = types.SimpleNamespace(connect=self._changed.append)

    def setPlainText(self, t):
        self._text = t
        for fn in self._changed:
            fn()

    def toPlainText(self):
        return self._text

    def clear(self):
        self.setPlainText("")

    def appendPlainText(self, line):
        self._text = line if not self._text else self._text + "\n" + line
        if self._max_block_count > 0:
            lines = self._text.split("\n")
            if len(lines) > self._max_block_count:
                self._text = "\n".join(lines[-self._max_block_count:])

    def setMaximumBlockCount(self, n):
        self._max_block_count = n

    def verticalScrollBar(self):
        # A no-op scrollbar stand-in: the activity feed calls setValue(maximum())
        # to auto-scroll, which is meaningless without real layout, but must not
        # raise in the mock harness.
        return types.SimpleNamespace(maximum=lambda: 0, setValue=lambda v: None)

    def setPlaceholderText(self, t):
        self._placeholder = t

    def setReadOnly(self, v):
        self._readonly = v

    def fontMetrics(self):
        # No real font here, so no real line height either; enough for review.py's
        # content-height arithmetic to run without crashing, not to be accurate.
        return types.SimpleNamespace(lineSpacing=lambda: 14)

    def document(self):
        return types.SimpleNamespace(documentMargin=lambda: 4)

    def frameWidth(self):
        return 1

    def node(self):
        return {"t": "textarea", "id": self.wid, "value": self._text,
                "placeholder": self._placeholder, "readonly": self._readonly}


class QSpinBox(QWidget):
    def __init__(self, *a, **k):
        super().__init__()
        self._value, self._min, self._max, self._suffix = 0, 0, 99, ""
        self._special = ""
        self.valueChanged = Signal()

    def setRange(self, lo, hi):
        self._min, self._max = lo, hi

    def setSpecialValueText(self, text):
        """What a real QSpinBox shows in place of its minimum value, which is how
        the AI wizard's Advanced panel says "auto" rather than "0"."""
        self._special = text

    def setValue(self, v):
        self._value = int(v)
        self.valueChanged.emit(self._value)

    def value(self):
        return self._value

    def setSuffix(self, s):
        self._suffix = s

    def node(self):
        return {"t": "spin", "id": self.wid, "value": self._value,
                "min": self._min, "max": self._max, "suffix": self._suffix,
                "special": self._special, "enabled": self._enabled}


class _LayoutItem:
    """What QLayout.itemAt returns: a handle whose widget() is the widget it holds, or
    None for an item that is a nested layout or a spacer."""

    def __init__(self, widget):
        self._widget = widget

    def widget(self):
        return self._widget


class _Layout:
    kind = "col"

    def __init__(self, parent=None):
        self.wid = _new_wid(self)
        self._children = []
        # (left, top, right, bottom), recorded rather than dropped: a row's own indent
        # is a layout margin, and a suite with no geometry has nothing else to read it
        # from (review._card_row indents an expanded body by exactly this).
        self._margins = (0, 0, 0, 0)
        if parent is not None and isinstance(parent, QWidget):
            parent._layout = self

    def addWidget(self, w, *a):
        self._children.append(w)

    def insertWidget(self, i, w, *a):
        self._children.insert(i, w)

    def count(self):
        return len(self._children)

    def itemAt(self, i):
        """Qt hands back a layout ITEM, not the widget: an item wrapping a nested layout
        answers widget() with None. Modelled rather than flattened, since that None is
        exactly what a caller walking a layout has to handle."""
        if not 0 <= i < len(self._children):
            return None
        child = self._children[i]
        return _LayoutItem(child if isinstance(child, QWidget) else None)

    def takeAt(self, i):
        """Like itemAt, but also removes the child: dialogs._DeclinedDialog._rebuild()
        clears and repopulates its list this way, same as real Qt."""
        if not 0 <= i < len(self._children):
            return None
        child = self._children.pop(i)
        return _LayoutItem(child if isinstance(child, QWidget) else None)

    def addLayout(self, l):
        self._children.append(l)

    def addStretch(self, *a):
        pass

    def addSpacing(self, v):
        pass

    def setSpacing(self, v):
        pass

    def setContentsMargins(self, *a):
        self._margins = tuple(a) if len(a) == 4 else self._margins

    def setSizeConstraint(self, c):
        pass

    def activate(self):
        pass

    def node(self):
        return {"t": self.kind, "id": self.wid,
                "children": [c.node() for c in self._children]}


class QLayout:
    """Only used here for its SizeConstraint enum: ai_setup._AIBackendsDialog and
    ai_dialog._GenerateDialog both read QLayout.SizeConstraint.SetMinimumSize at
    construction time, and _Layout above (what setSizeConstraint is actually
    called on) has no real layout engine to constrain."""
    class SizeConstraint:
        SetDefaultConstraint = 0
        SetMinimumSize = 2


class QVBoxLayout(_Layout):
    kind = "col"


class QHBoxLayout(_Layout):
    kind = "row"


class QFormLayout(_Layout):
    kind = "form"

    def addRow(self, label, field):
        """Real QFormLayout takes a label (str or QLabel) and a field widget per
        row, in two aligned columns. The mock doesn't model columns, just the
        flat child list every _Layout already exposes to node()/count()/itemAt."""
        if isinstance(label, str):
            label = QLabel(label)
        self._children.append(label)
        self._children.append(field)


class QGridLayout(_Layout):
    """The AI Backends window's settings panel: a real grid, so every label sits in
    one fixed-width column and every field starts at the same x after it. The mock
    models no columns (see QFormLayout above), just the flat child list; the real
    alignment is qt_tests/'s to measure."""
    kind = "grid"

    def setHorizontalSpacing(self, v):
        pass

    def setVerticalSpacing(self, v):
        pass

    def setColumnMinimumWidth(self, col, width):
        pass

    def setColumnStretch(self, col, stretch):
        pass


class QFrame(QWidget):
    class Shape:
        NoFrame, HLine, StyledPanel = 0, 4, 6   # Qt's own values

    class Shadow:
        Plain = 0x10                            # Qt's own value

    def __init__(self, *a, **k):
        super().__init__()
        self._shape = QFrame.Shape.NoFrame

    def setFrameShape(self, s):
        self._shape = s

    def setFrameShadow(self, s):
        pass

    def node(self):
        # A shaped frame carries no content: reported as its own kind so a test can
        # tell a separator apart from a container box.
        kind = "hline" if self._shape == QFrame.Shape.HLine else "frame"
        return {"t": kind, "id": self.wid, "style": self._style,
                "children": [self._layout.node()] if self._layout else []}


class QScrollBar:
    """Minimal stand-in for the object a real QScrollArea.verticalScrollBar() returns.

    Added for widgets.StreamingList, which connects to its valueChanged signal to
    extend the list as the reader scrolls near the bottom. The mock has no real layout
    engine, so maximum() stays whatever a caller sets it to (0 unless told otherwise);
    the geometry-driven behaviour is qt_tests/'s job, on the real widget. This stub
    exists so the production wiring (connecting to the real signal) has something to
    connect to here too, rather than the widget having to guard against a scrollbar
    that might not exist.
    """

    def __init__(self):
        self._value = 0
        self._maximum = 0
        self.valueChanged = Signal()

    def value(self):
        return self._value

    def setValue(self, v):
        self._value = v
        self.valueChanged.emit(v)

    def maximum(self):
        return self._maximum

    def setMaximum(self, v):
        self._maximum = v


class QScrollArea(QWidget):
    def __init__(self, *a, **k):
        super().__init__()
        self._widget = None
        self._vbar = QScrollBar()
        self._viewport = QWidget()

    def setWidgetResizable(self, v):
        pass

    def setWidget(self, w):
        self._widget = w

    def verticalScrollBar(self):
        return self._vbar

    def viewport(self):
        return self._viewport

    def node(self):
        return {"t": "scroll", "id": self.wid,
                "children": [self._widget.node()] if self._widget else []}


class QDialogButtonBox(QWidget):
    class ButtonRole:
        # ActionRole is Qt's own value for a button that does something without
        # answering the dialog: it's deliberately NOT wired to accepted/rejected in
        # addButton below, which is what lets _ask_scrollable's "Review new cards"
        # button run and leave the confirmation standing.
        AcceptRole, ApplyRole, RejectRole, ActionRole = 0, 1, 2, 3

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
            # Real QDialogButtonBox auto-wires a custom-labeled button's role to the
            # box's accepted/rejected signal too, same as the standard buttons above —
            # a caller with its own action label (e.g. "Archive & relocate") still gets
            # bb.rejected/accepted for free, matching aqt.qt behavior.
            if role == QDialogButtonBox.ButtonRole.AcceptRole:
                btn.clicked.connect(self.accepted.emit)
            elif role == QDialogButtonBox.ButtonRole.RejectRole:
                btn.clicked.connect(self.rejected.emit)
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
            if isinstance(w, (QCheckBox, QRadioButton)):
                w.setChecked(ev["value"])
            elif isinstance(w, QLineEdit):
                w.setText(str(ev["value"]))
            elif isinstance(w, QPlainTextEdit):
                w.setPlainText(str(ev["value"]))
            elif isinstance(w, QSpinBox):
                w.setValue(ev["value"])
        if ev.get("click"):
            w.clicked.emit()


class QDialog(QWidget):
    class DialogCode:
        # Qt's own values: exec()/accept()/reject() below already agree with these.
        Rejected, Accepted = 0, 1

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
    """Enough of Qt's message box to stand in for the one ui._ask builds.

    The roles matter here, not just the labels: the declining button carries RejectRole
    and is made both the default and the escape button, which is what makes Return and
    Escape mean the safe answer rather than consenting to a schema change. exec()
    answers a question built that way through Gui.ask, so every existing test that
    scripts or replays an answer answers this the same way it always has.
    """

    class Icon:
        Question, Information, Warning = 0, 1, 2

    class ButtonRole:
        AcceptRole, RejectRole = 0, 1

    class StandardButton:
        Ok, Cancel = 0x400, 0x400000

    def __init__(self, parent=None):
        super().__init__()
        self._title, self._text = "", ""
        self._buttons = []
        self._roles = {}
        self._default = None
        self._escape = None
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
        self._roles[btn.wid] = role
        return btn

    def setDefaultButton(self, btn):
        self._default = btn

    def setEscapeButton(self, btn):
        self._escape = btn

    def clickedButton(self):
        return self._clicked

    def exec(self):
        if self._escape is not None:
            return self._exec_named()
        resp = _gui.next_interaction({
            "kind": "msgbox", "id": self.wid, "title": self._title,
            "text": self._text,
            "buttons": [{"id": b.wid, "label": b._label} for b in self._buttons]})
        for ev in resp.get("events", []):
            if ev.get("click") and ev["id"] in _widgets:
                self._clicked = _widgets[ev["id"]]
        return 0

    def _exec_named(self):
        """A ui._ask question: answered through Gui.ask, recorded with its buttons."""
        yes = next(b for b in self._buttons
                   if self._roles.get(b.wid) == QMessageBox.ButtonRole.AcceptRole)
        no = self._escape
        _gui.ask_defaults.append(self._default._label if self._default else None)
        self._clicked = yes if _gui.ask(
            self._text, buttons=(yes._label, no._label)) else no
        return 0


# ============================== menu recording ==============================
class QAction:
    class MenuRole:
        NoRole = 0

    def __init__(self, label="", parent=None):
        self.wid = _new_persistent_wid(self)
        self._label = label
        self._enabled = True
        self.triggered = Signal()

    def setMenuRole(self, r):
        pass

    def setText(self, t):
        self._label = t

    def text(self):
        return self._label

    def setEnabled(self, v):
        self._enabled = bool(v)

    def isEnabled(self):
        return self._enabled


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
            menuTools=types.SimpleNamespace(addMenu=self._menus.append),
            actionUndo=QAction())
        self.update_undo_actions()

    def reset(self):
        self.reset_count += 1
        self.update_undo_actions()

    def update_undo_actions(self):
        """Mirrors real Anki's mw.update_undo_actions(): Edit > Undo is enabled
        exactly when the collection has something to undo. col._undo_entries is
        the mock's stand-in for real Anki's col.undo_status().can_undo: good
        enough to catch a caller that writes the collection but never tells the
        UI a new undo entry exists, which is the bug this exists to catch."""
        self.form.actionUndo.setEnabled(bool(self.col._undo_entries))

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

    @property
    def qt_timers(self):
        """Every QTimer the add-on has built. There is no event loop here, so a test
        that cares what a timer does fires it itself; see _QTimer."""
        import sys
        return list(sys.modules["aqt.qt"].QTimer.registry)


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
    aqt.gui_hooks = types.SimpleNamespace(main_window_did_init=[], card_will_show=[],
                                          webview_will_set_content=[])

    aqt_qt = types.ModuleType("aqt.qt")

    class _Qt:
        class CursorShape:
            WaitCursor = 0
            PointingHandCursor = 13   # Qt's own value

        class AlignmentFlag:
            AlignTop = 0x20           # Qt's own values
            AlignLeft = 0x01
            AlignVCenter = 0x80
            AlignCenter = 0x84

        class TextFormat:
            RichText = 1

        class WindowModality:
            WindowModal = 1

    class _QFileDialog:
        """The native directory picker configure_source opens for a local folder.

        There is no native dialog to open here, so this answers through the same
        `prompt` payload the rest of this file uses for a typed value: pytest scripts
        it as a prompt response, and the live demo (which has no filesystem picker of
        its own either) keeps drawing the text field it already draws.
        """

        @staticmethod
        def getExistingDirectory(parent=None, caption="", directory="", *a, **k):
            text, ok = gui.prompt(caption, default=directory)
            return text if ok else ""

        @staticmethod
        def getOpenFileName(parent=None, caption="", directory="", filter="", *a, **k):
            """The AI Backends window's Browse button. Same prompt-based stand-in
            as getExistingDirectory above: a test that wants a chosen path
            monkeypatches this directly rather than scripting a real file picker."""
            text, ok = gui.prompt(caption, default=directory)
            return (text, filter) if ok else ("", "")

    class _QUrl:
        """The address a link carries. Qt wraps a string in one of these before
        handing it to the desktop; nothing here needs more than the string back."""

        def __init__(self, url=""):
            self._url = url

        def toString(self):
            return self._url

    class _QDesktopServices:
        """Hands a URL to the platform browser. Nothing opens here, so the calls are
        recorded instead: that is what lets a test assert an install-guide link sends
        the reader to the right documentation without launching anything."""

        opened = []

        @staticmethod
        def openUrl(url):
            _QDesktopServices.opened.append(
                url.toString() if hasattr(url, "toString") else str(url))
            return True

    class _QFontDatabase:
        class SystemFont:
            FixedFont = 0

        @staticmethod
        def systemFont(kind):
            return None

    class _QSizePolicy:
        class Policy:
            Fixed, Preferred, Expanding = 0, 5, 7   # Qt's own values

        def __init__(self, *a, **k):
            pass

    class _QKeySequence:
        """Stands in for real Qt's QKeySequence just enough to test that
        ai_dialog asks Qt for the platform's native Undo accelerator rather
        than hardcoding one: real Qt renders StandardKey.Undo as "Ctrl+Z" on
        Windows/Linux and "⌘Z" on macOS; this mirrors that one distinction
        rather than Qt's full native-text rendering, which qt_tests/ (real
        PyQt6) exercises directly.
        """
        class StandardKey:
            Undo = "undo"

        class SequenceFormat:
            # Real Qt: NativeText renders the OS's own glyphs (e.g. "⌘Z" on
            # macOS); PortableText is the plain ASCII form ("Ctrl+Z") used to
            # serialize a shortcut, the same on every platform. toString()'s
            # default format is PortableText, so this mock only renders the
            # platform-specific glyph when NativeText is asked for explicitly:
            # a caller that dropped the format argument (or passed the wrong
            # one) gets the same "Ctrl+Z" on every platform, exactly the bug
            # this exists to catch.
            NativeText = "native"
            PortableText = "portable"

        def __init__(self, key):
            self._key = key

        def toString(self, fmt=None):
            if self._key != _QKeySequence.StandardKey.Undo:
                return ""
            if fmt == _QKeySequence.SequenceFormat.NativeText and sys.platform == "darwin":
                return "⌘Z"
            return "Ctrl+Z"

    class _Clipboard:
        @staticmethod
        def setText(text):
            gui.clipboard.append(text)

    class _QApplication:
        # Every test here runs "inside Anki", where a QApplication always
        # exists: so instance() is live by default. A test that wants to
        # exercise the no-app fallback path (ai_dialog._undo_shortcut) can
        # monkeypatch this to None for the duration of the call.
        _instance = object()

        @staticmethod
        def instance():
            return _QApplication._instance

        @staticmethod
        def setOverrideCursor(cursor):
            pass

        @staticmethod
        def restoreOverrideCursor():
            pass

        @staticmethod
        def processEvents():
            pass

        @staticmethod
        def clipboard():
            return _Clipboard()

    class _QTimer:
        """Real QTimer's shape, minus an event loop to fire it.

        `timeout.connect` records the callback and `fire()` runs it, so a test can
        assert what a timer actually does rather than only that one was created. It is
        deliberately NOT fired by start(): background.py schedules its auto-sync poll on
        one of these, and a timer that ran its callback the moment it was started would
        turn that poll into unbounded recursion the first time a test touched it.
        """
        registry = []    # every timer built this process; a test fires them by hand

        def __init__(self, parent=None):
            self.started = None
            self.interval = None
            self.single_shot = False
            self._callbacks = []
            _QTimer.registry.append(self)

        def setSingleShot(self, on):
            self.single_shot = bool(on)

        def setInterval(self, ms):
            self.interval = ms

        def start(self, ms=None):
            # Real QTimer.start() takes an optional interval and falls back to
            # setInterval's value; a signal connected straight to start passes none.
            self.started = self.interval if ms is None else ms

        def stop(self):
            self.started = None

        def fire(self):
            for fn in self._callbacks:
                fn()

        @property
        def timeout(self):
            return types.SimpleNamespace(connect=self._callbacks.append)

        @staticmethod
        def singleShot(ms, fn):
            pass   # tests and the demo call background checks directly

    class _QProgressDialog:
        """Stands in for cancellable_progress()'s real QProgressDialog. Never
        cancels on its own — nothing here drives a real Qt event loop to click a
        Cancel button — except when a test opts in via `cancel_after`, keyed by
        the dialog's initial label (its title, e.g. "Updating decks"): the dialog
        reports canceled once step() has run more than that many times, simulating
        "the learner clicked Cancel after N steps" for tests that need to verify a
        partial-apply is handled safely. Counts setLabelText() calls specifically
        (not setValue()) since cancellable_progress's step() is the only caller of
        setLabelText — the setup/teardown setValue(0)/setValue(total) calls around
        the loop must not themselves count as steps.
        """
        cancel_after = {}   # {label: n}; tests set this, one fresh dict per install()

        def __init__(self, label, cancel_text, minv, maxv, parent=None):
            self._label = label
            self._title = ""
            self._calls = 0
            self._canceled = False
            self.deleted = False

        def setWindowTitle(self, t):
            self._title = t

        def setWindowModality(self, m):
            pass

        def setMinimumDuration(self, ms):
            pass

        def setAutoClose(self, b):
            pass

        def setLabelText(self, text):
            self._calls += 1
            n = _QProgressDialog.cancel_after.get(self._label)
            if n is not None and self._calls > n:
                self._canceled = True

        def setValue(self, v):
            pass

        def wasCanceled(self):
            return self._canceled

        def close(self):
            pass

        def deleteLater(self):
            self.deleted = True

    for name, obj in (("Qt", _Qt), ("QApplication", _QApplication),
                      ("QTimer", _QTimer), ("QProgressDialog", _QProgressDialog),
                      ("QFileDialog", _QFileDialog), ("QKeySequence", _QKeySequence),
                      ("QFontDatabase", _QFontDatabase), ("QFontMetrics", QFontMetrics),
                      ("QLabel", QLabel),
                      ("QPushButton", QPushButton), ("QAction", QAction),
                      ("QMenu", QMenu), ("QCheckBox", QCheckBox),
                      ("QComboBox", QComboBox), ("QRadioButton", QRadioButton),
                      ("QButtonGroup", QButtonGroup),
                      ("pyqtSignal", pyqtSignal),
                      ("QStackedWidget", QStackedWidget),
                      ("QDialog", QDialog), ("QDialogButtonBox", QDialogButtonBox),
                      ("QFrame", QFrame), ("QHBoxLayout", QHBoxLayout),
                      ("QFormLayout", QFormLayout),
                      ("QGridLayout", QGridLayout), ("QLayout", QLayout),
                      ("QDesktopServices", _QDesktopServices), ("QUrl", _QUrl),
                      ("QImage", QImage),
                      ("QLineEdit", QLineEdit), ("QMessageBox", QMessageBox),
                      ("QPlainTextEdit", QPlainTextEdit),
                      ("QScrollArea", QScrollArea), ("QSizePolicy", _QSizePolicy),
                      ("QSpinBox", QSpinBox),
                      ("QVBoxLayout", QVBoxLayout), ("QWidget", QWidget)):
        setattr(aqt_qt, name, obj)

    aqt_utils = types.ModuleType("aqt.utils")
    aqt_utils.showInfo = gui.info
    aqt_utils.showWarning = gui.warn
    aqt_utils.askUser = gui.ask

    # No askUserDialog: ui._ask builds its own QMessageBox for a named-button question,
    # since Anki's helper gives every button AcceptRole, which leaves the dialog with no
    # escape and no default (see ui._ask).
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
    anki_models = types.ModuleType("anki.models")
    anki_models.ChangeNotetypeRequest = ChangeNotetypeRequest

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
    sys.modules["anki.models"] = anki_models
    return mock
