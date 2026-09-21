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
import math
import os
import sqlite3
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mock_anki

MOCK = mock_anki.install()

import internpearls                     # noqa: E402  (real __init__: builds the menu)
from internpearls import background, collection, config, net, palette, sync  # noqa: E402
from internpearls.platform import new_work_request, platform, work_checkpoint  # noqa: E402

SOURCE = os.environ.get("DEMO_SOURCE", "/source")   # env override for local smoke tests
INTERVALS = ["2.3 mo", "11 d", "27 d", "6 d", "3.1 mo", "16 d", "9 d", "1.2 mo"]
SEED_NOTES = {0: "mnemonic: A for A", 4: "review the dosing example"}

RUNNER = mock_anki.Runner(MOCK, paths=[config.INSTALLED, config.STATE,
                                       collection._USER_FILES, SOURCE])

WORKER_MESSAGE_TYPES = {
    "boot", "menu", "start", "feed", "state", "maintainer",
    "set-theme", "reset",
}


def _invalid_message():
    raise ValueError("invalid-message")


def _exact_fields(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields):
        _invalid_message()


def _nonnegative_integer(value):
    return (not isinstance(value, bool) and isinstance(value, int)
            and 0 <= value <= 2147483647)


def _validate_json_value(value, depth=0, ancestors=None):
    if depth > 64:
        _invalid_message()
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            _invalid_message()
        return
    if not isinstance(value, (list, dict)):
        _invalid_message()
    if len(value) > 10000:
        _invalid_message()
    ancestors = set() if ancestors is None else ancestors
    marker = id(value)
    if marker in ancestors:
        _invalid_message()
    ancestors.add(marker)
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item, depth + 1, ancestors)
    else:
        for key, item in value.items():
            if not isinstance(key, str):
                _invalid_message()
            _validate_json_value(item, depth + 1, ancestors)
    ancestors.remove(marker)


def _validate_runner_request(envelope):
    _exact_fields(envelope, {
        "protocol", "epoch", "sequence", "render_revision", "actions",
    })
    if envelope["protocol"] != 1:
        _invalid_message()
    if not all(_nonnegative_integer(envelope[name]) for name in (
            "epoch", "sequence", "render_revision")):
        _invalid_message()
    if not isinstance(envelope["actions"], list):
        _invalid_message()
    _validate_json_value(envelope["actions"])


def _validate_worker_payload(message_type, payload):
    if not isinstance(payload, dict):
        _invalid_message()
    if message_type == "boot":
        if set(payload) not in (set(), {"dark"}):
            _invalid_message()
        if "dark" in payload and not isinstance(payload["dark"], bool):
            _invalid_message()
    elif message_type == "menu":
        _exact_fields(payload, set())
    elif message_type == "start":
        _exact_fields(payload, {"menu_id", "epoch"})
        if (not isinstance(payload["menu_id"], str) or not payload["menu_id"]
                or isinstance(payload["epoch"], bool)
                or not isinstance(payload["epoch"], int)
                or not 0 <= payload["epoch"] <= 2147483647):
            _invalid_message()
    elif message_type == "feed":
        _exact_fields(payload, {"envelope"})
        _validate_runner_request(payload["envelope"])
    elif message_type == "state":
        action = payload.get("action")
        if action in {"read", "auto-sync"}:
            _exact_fields(payload, {"action"})
        elif action == "set-note":
            _exact_fields(payload, {"action", "guid", "text"})
            if (not isinstance(payload["guid"], str) or not payload["guid"]
                    or not isinstance(payload["text"], str)):
                _invalid_message()
        elif action == "list-files":
            _exact_fields(payload, {"action", "folder"})
            if not isinstance(payload["folder"], str):
                _invalid_message()
        else:
            _invalid_message()
    elif message_type == "maintainer":
        _exact_fields(payload, {"operation"})
        if payload["operation"] not in {"fix", "reword", "add", "restyle"}:
            _invalid_message()
    elif message_type == "set-theme":
        _exact_fields(payload, {"dark"})
        if not isinstance(payload["dark"], bool):
            _invalid_message()
    elif message_type == "reset":
        _exact_fields(payload, {"recovery", "cancel"})
        if not isinstance(payload["recovery"], list):
            _invalid_message()
        for entry in payload["recovery"]:
            if not isinstance(entry, dict) or entry.get("type") in {"boot", "reset"}:
                _invalid_message()
            validate_worker_message(entry)
        _validate_worker_payload("feed", payload["cancel"])
    else:
        _invalid_message()
    _validate_json_value(payload)


def validate_worker_message(message):
    _exact_fields(message, {"type", "payload"})
    message_type = message["type"]
    if not isinstance(message_type, str) or message_type not in WORKER_MESSAGE_TYPES:
        _invalid_message()
    _validate_worker_payload(message_type, message["payload"])
    return message


def _install_demo_net():
    """The demo's network. Two layers:

    - The example deck repo's contents-API URLs are served from the in-page copy
      of the repo (/source) — the very files the maintainer buttons edit — so the
      add-on's real GitHub fetch path runs, and a "pushed" update is exactly what
      it downloads. (The page already fetched that copy from the real repo at load.)
    - Everything else (the add-on's own update check) goes through a synchronous
      XHR when running under Pyodide. charset=x-user-defined is the standard
      trick for binary-safe text responses.
    """
    example = f"https://api.github.com/repos/{config.EXAMPLE_REPO}/contents/"
    real_get = net._http_get

    def _read(path):
        with open(os.path.join(SOURCE, path), "rb") as fh:
            return fh.read()

    def _from_source(url):
        path = url[len(example):].split("?")[0]
        if work_checkpoint("fixture-fetch:start"):
            data = _read(path)
            work_checkpoint("fixture-fetch:complete")
            return data
        if not getattr(platform(), "reconstructing", False):
            return _read(path)

        result, failure = [], []

        def work(context):
            context.checkpoint("fixture-fetch:start")
            data = _read(path)
            context.checkpoint("fixture-fetch:complete")
            return data

        request = new_work_request(MOCK.mw, "background-fetch", "background.fetch")
        handle = platform().start_work(
            request, work, result.append, failure.append)
        handle.start()
        while handle.is_alive():
            response = MOCK.gui.next_interaction({"kind": "work"})
            if "actions" in response:
                mock_anki.apply_actions(response, allowed_ids=set())
        if failure:
            raise failure[0]
        if not result:
            raise RuntimeError("fixture read did not complete")
        return result[0]

    try:
        from js import XMLHttpRequest
    except ImportError:
        # Not under Pyodide (a local smoke test): still serve the example repo
        # from SOURCE; anything else keeps the real urllib path.
        def _local_get(url, token=None, accept=None, timeout=None, on_chunk=None):
            if url.startswith(example):
                return _from_source(url)
            return real_get(url, token=token, accept=accept, on_chunk=on_chunk)

        net._http_get = _local_get
        return

    def _http_get(url, token=None, accept=None, timeout=None, on_chunk=None):
        if url.startswith(example):
            return _from_source(url)
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
    """Configure the add-on the way 'Try the example deck' does (the GitHub
    example repo as the source), then really sync the example decks into the
    empty collection and decorate it with a reviewer's state (intervals, a
    couple of personal annotations)."""
    _install_demo_net()
    for hook in sys.modules["aqt"].gui_hooks.main_window_did_init:
        hook()

    MOCK.mw._config = {"github_decks_repo": config.EXAMPLE_REPO,
                       "scope_tag": config.EXAMPLE_SCOPE_TAG,
                       "export_deck": config.EXAMPLE_DECK_NAME}
    MOCK.col.models._models.clear()   # real note types arrive with the import

    cfg = config._cfg()
    manifest, fetch, _ = sync._fetch_manifest(cfg)
    sync._run_sync(cfg, manifest, fetch, manifest["decks"])
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


def set_dark(flag):
    """Point the palette at the page's own colour scheme.

    palette.is_dark() asks Anki's theme manager, which the mock has none of, so without
    this every dialog the demo builds carries the light set, painted onto the page's dark
    panels when the browser is in dark mode. Read live at build time like the real thing,
    so a flip lands on the next dialog opened and leaves an open one alone, exactly as
    Anki's own theme switch does. Real Anki never reaches the fallback: aqt.theme is
    there and wins.
    """
    palette.FALLBACK_DARK = bool(flag)


def accents():
    """Both sets' accent colour, so the page can spot a link-style button by the colour
    ui.link_button() actually paints rather than by a hex of the page's own that goes
    stale the moment the palette is retuned."""
    return json.dumps([palette.LIGHT["accent"], palette.DARK["accent"]])


def menu():
    return json.dumps(MOCK.mw._menus[0].tree())


def get_config():
    c = config._cfg()
    return json.dumps({"auto_sync": c["auto_sync_decks"],
                       "interval": c["auto_sync_interval_minutes"]})


def _row_count(items):
    """How many of `items` are an actual row rather than list scaffolding.

    build_list_body and build_update_body both interleave real rows (a deck, card,
    retired, moved, or "row" entry) with ("header", ...), ("note", ...) and one
    ("sep",) between every pair of rows in a group, per review.py's own docstrings.
    Counting raw items double-counts the backlog: a 35-card list carries about 34
    seps plus a header, so "items remaining" comes out near double "cards remaining".
    """
    return sum(1 for item in items if item[0] not in ("header", "note", "sep"))


def list_progress(wid):
    """Real rows shown/total for the widgets.StreamingList a "scroll" node's wid names.

    The mock never puts this in the serialized tree (widgets.py isn't part of this
    demo's own files), so it's read straight off the live widget object mock_anki
    already tracks in its wid registry for the dialog currently on screen. None for
    a plain QScrollArea (dialogs.py/review.py/ui.py's static ones), which never
    truncates and has no such methods.

    Counts real rows, not raw list entries: `shown`/`total` off the widget itself
    count every item including the header/note/sep scaffolding between rows, so the
    page's "N more not shown" would otherwise report roughly double the real
    remaining cards. See _row_count.
    """
    w = mock_anki._widgets.get(wid)
    if not hasattr(w, "shown"):
        return json.dumps(None)
    items = w._items
    return json.dumps({"shown": _row_count(items[:w.shown()]),
                       "total": _row_count(items)})


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


def start_protocol(wid, epoch):
    return json.dumps(RUNNER.start_protocol(
        lambda: mock_anki.trigger_action(wid), int(epoch)))


def feed_protocol(envelope_json):
    return json.dumps(RUNNER.feed_protocol(json.loads(envelope_json)))


def flow_tooltips():
    return json.dumps(list(MOCK.gui.tooltips))


def auto_sync_tick():
    MOCK.gui.tooltips.clear()
    mock_anki.reset_run()
    background._auto_sync_check()
    return json.dumps(list(MOCK.gui.tooltips))


def _worker_state(tooltips=None):
    return {
        "state": json.loads(collection_state()),
        "config": json.loads(get_config()),
        "tooltips": (list(MOCK.gui.tooltips) if tooltips is None
                     else list(tooltips)),
    }


def handle_worker_message(message_json):
    """Validate and dispatch one JSON-only request from the browser Worker."""
    if not isinstance(message_json, str):
        _invalid_message()
    try:
        message = json.loads(message_json)
    except (TypeError, ValueError):
        _invalid_message()
    validate_worker_message(message)
    message_type = message["type"]
    payload = message["payload"]

    if message_type == "menu":
        result = {"menu": json.loads(menu())}
    elif message_type == "start":
        response = json.loads(start_protocol(payload["menu_id"], payload["epoch"]))
        result = _worker_state([])
        result["response"] = response
    elif message_type == "feed":
        response = json.loads(feed_protocol(
            json.dumps(payload["envelope"], allow_nan=False)))
        result = _worker_state([])
        result["response"] = response
    elif message_type == "state":
        action = payload["action"]
        if action == "read":
            result = _worker_state([])
        elif action == "set-note":
            set_note(payload["guid"], payload["text"])
            result = _worker_state([])
        elif action == "auto-sync":
            tips = json.loads(auto_sync_tick())
            result = _worker_state(tips)
        else:
            result = {"files": [os.path.basename(path) for path in
                                json.loads(list_files(payload["folder"]))]}
    elif message_type == "maintainer":
        result = json.loads(maintainer(payload["operation"]))
    elif message_type == "set-theme":
        set_dark(payload["dark"])
        result = {}
    else:
        _invalid_message()

    _validate_json_value(result)
    return json.dumps(result, allow_nan=False, separators=(",", ":"))


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
    if RUNNER.protocol_active:
        raise mock_anki.ProtocolError("protocol flow active")
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

    def apply():
        _edit_apkg(path, edit)
        _bump(deck["name"])

    RUNNER.maintain_fixture(apply)
    return json.dumps({"label": label, "deck": deck["name"].split("::")[-1]})
