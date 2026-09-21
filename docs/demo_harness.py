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
import re
import sqlite3
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mock_anki

MOCK = mock_anki.install()

import internpearls                     # noqa: E402  (real __init__: builds the menu)
from internpearls import background, collection, config, logic, net, palette, sync  # noqa: E402
from demo.replay import ReplayPlatform  # noqa: E402
from internpearls.platform import (new_work_request, platform, use_platform,
                                  work_checkpoint)  # noqa: E402

SOURCE = os.environ.get("DEMO_SOURCE", "/source")   # env override for local smoke tests
INTERVALS = ["2.3 mo", "11 d", "27 d", "6 d", "3.1 mo", "16 d", "9 d", "1.2 mo"]
SEED_NOTES = {0: "mnemonic: A for A", 4: "review the dosing example"}

RUNNER = mock_anki.Runner(MOCK, paths=[config.INSTALLED, config.STATE,
                                       collection._USER_FILES, SOURCE])

WORKER_MESSAGE_TYPES = {
    "boot", "menu", "start", "feed", "state", "maintainer",
    "set-theme", "reset",
}
ACTION_FIELDS = {
    "activate": {"type", "id"},
    "toggle": {"type", "id", "checked"},
    "select-option": {"type", "id", "option_id"},
    "edit-text": {"type", "id", "value", "selection_start", "selection_end", "composing"},
    "finish-edit": {"type", "id"},
    "activate-link": {"type", "id", "action_id"},
    "key": {"type", "id", "key", "modifiers"},
    "scroll": {"type", "id", "offset"},
    "select-files": {"type", "id", "accept", "files"},
    "advance": {"type", "elapsed_ms", "checkpoint_credits"},
    "close": {"type", "id"},
}
RUNNER_ERROR_CODES = {
    "unsupported-protocol", "unknown-node-kind", "missing-required-field",
    "unknown-enum-value", "unknown-widget-id", "hidden-target", "disabled-target",
    "invalid-option-id", "invalid-link-id", "stale-epoch", "stale-sequence",
    "stale-render-revision", "invalid-envelope", "invalid-action",
    "action-not-allowed", "scheduler-limit", "journal-limit",
}
RUNTIME_PATH = re.compile(r"/(?:app|source)/[^<>'\"\r\n]+")


def _invalid_message():
    raise ValueError("invalid-message")


def _redact_runtime_paths(value):
    if isinstance(value, str):
        return RUNTIME_PATH.sub("the demo workspace", value)
    if isinstance(value, list):
        return [_redact_runtime_paths(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_runtime_paths(item) for key, item in value.items()}
    return value


def _exact_fields(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields):
        _invalid_message()


def _nonnegative_integer(value):
    return (not isinstance(value, bool) and isinstance(value, int)
            and 0 <= value <= 2147483647)


def _nonempty_string(value, maximum=10000):
    return isinstance(value, str) and 0 < len(value) <= maximum


def _validate_action(action):
    if (not isinstance(action, dict) or not isinstance(action.get("type"), str)
            or action["type"] not in ACTION_FIELDS):
        _invalid_message()
    _exact_fields(action, ACTION_FIELDS[action["type"]])
    for name in ("id", "option_id", "action_id"):
        if name in action and not _nonempty_string(action[name], 255):
            _invalid_message()
    if "checked" in action and not isinstance(action["checked"], bool):
        _invalid_message()
    if action["type"] == "edit-text":
        if (not isinstance(action["value"], str)
                or not _nonnegative_integer(action["selection_start"])
                or not _nonnegative_integer(action["selection_end"])
                or not isinstance(action["composing"], bool)):
            _invalid_message()
    for name in ("offset", "elapsed_ms", "checkpoint_credits"):
        if name in action and not _nonnegative_integer(action[name]):
            _invalid_message()
    if action["type"] == "key":
        modifiers = action["modifiers"]
        if (not _nonempty_string(action["key"], 64) or not isinstance(modifiers, list)
                or any(not isinstance(item, str)
                       or item not in {"alt", "control", "meta", "shift"}
                       for item in modifiers)
                or len(modifiers) != len(set(modifiers))):
            _invalid_message()
    if action["type"] == "select-files":
        if (not isinstance(action["accept"], list)
                or any(not isinstance(item, str) for item in action["accept"])
                or not isinstance(action["files"], list)):
            _invalid_message()
        for item in action["files"]:
            _exact_fields(item, {"name", "size", "type"})
            if (not _nonempty_string(item["name"], 255)
                    or not _nonnegative_integer(item["size"])
                    or not isinstance(item["type"], str)):
                _invalid_message()


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
    if isinstance(envelope["protocol"], bool) or envelope["protocol"] != 1:
        _invalid_message()
    if not all(_nonnegative_integer(envelope[name]) for name in (
            "epoch", "sequence", "render_revision")):
        _invalid_message()
    if not isinstance(envelope["actions"], list):
        _invalid_message()
    for action in envelope["actions"]:
        _validate_action(action)


def _validate_runner_response(response):
    _exact_fields(response, {
        "protocol", "epoch", "sequence", "render_revision", "status",
        "payload", "pending", "safe_status",
    })
    if (isinstance(response["protocol"], bool) or response["protocol"] != 1
            or not all(_nonnegative_integer(response[name])
            for name in ("epoch", "sequence", "render_revision"))):
        _invalid_message()
    if (not isinstance(response["status"], str)
            or response["status"] not in {
                "need", "done", "error", "stale", "contract-error",
            }):
        _invalid_message()
    pending = response["pending"]
    if not isinstance(pending, list):
        _invalid_message()
    for item in pending:
        _exact_fields(item, {"task_id", "kind", "stage"})
        if (not isinstance(item["task_id"], list) or len(item["task_id"]) != 4
                or not all(_nonnegative_integer(part) for part in item["task_id"])
                or not _nonempty_string(item["kind"], 255)
                or not _nonempty_string(item["stage"])):
            _invalid_message()
    _exact_fields(response["safe_status"], {"code"})
    if not _nonempty_string(response["safe_status"]["code"], 255):
        _invalid_message()
    if response["status"] in {"error", "stale", "contract-error"}:
        error = response["payload"]
        if (not isinstance(error, dict) or not isinstance(error.get("code"), str)
                or error["code"] not in RUNNER_ERROR_CODES
                or not set(error).issubset({"code", "kind", "id"})):
            _invalid_message()
    elif not isinstance(response["payload"], dict):
        _invalid_message()


def _validate_state(state):
    _exact_fields(state, {"decks", "version"})
    if not isinstance(state["decks"], list) or not isinstance(state["version"], str):
        _invalid_message()
    for deck in state["decks"]:
        _exact_fields(deck, {"name", "cards"})
        if not isinstance(deck["name"], str) or not isinstance(deck["cards"], list):
            _invalid_message()
        for card in deck["cards"]:
            _exact_fields(card, {"guid", "front", "back", "notes", "cloze", "interval"})
            if (any(not isinstance(card[name], str)
                    for name in ("guid", "front", "back", "notes"))
                    or not isinstance(card["cloze"], bool)
                    or (card["interval"] is not None
                        and not isinstance(card["interval"], str))):
                _invalid_message()


def _validate_config(config):
    _exact_fields(config, {"auto_sync", "interval"})
    if not isinstance(config["auto_sync"], bool) or not _nonnegative_integer(config["interval"]):
        _invalid_message()


def _validate_menu(menu):
    if not isinstance(menu, list):
        _invalid_message()
    for item in menu:
        if (not isinstance(item, dict) or not isinstance(item.get("t"), str)
                or item["t"] not in {"action", "item", "menu", "sep"}):
            _invalid_message()
        if item["t"] == "sep":
            _exact_fields(item, {"t"})
        elif item["t"] in {"action", "item"}:
            _exact_fields(item, {"t", "id", "label"})
            if not _nonempty_string(item["id"], 255) or not _nonempty_string(item["label"]):
                _invalid_message()
        else:
            _exact_fields(item, {"t", "label", "items"})
            if not _nonempty_string(item["label"]):
                _invalid_message()
            _validate_menu(item["items"])


def validate_worker_result(message_type, result):
    if not isinstance(message_type, str) or not isinstance(result, dict):
        _invalid_message()
    if message_type == "menu":
        _exact_fields(result, {"menu"})
        _validate_menu(result["menu"])
    elif message_type in {"start", "feed", "reset"}:
        _exact_fields(result, {"state", "config", "tooltips", "response"})
        _validate_runner_response(result["response"])
    elif message_type == "state":
        if set(result) == {"files"}:
            if not isinstance(result["files"], list) or any(
                    not isinstance(item, str) for item in result["files"]):
                _invalid_message()
            return result
        _exact_fields(result, {"state", "config", "tooltips"})
    elif message_type == "maintainer":
        _exact_fields(result, {"label", "deck"})
        if not _nonempty_string(result["label"]) or not _nonempty_string(result["deck"]):
            _invalid_message()
        return result
    elif message_type == "set-theme":
        _exact_fields(result, set())
        return result
    else:
        _invalid_message()
    if "state" in result:
        _validate_state(result["state"])
        _validate_config(result["config"])
        if not isinstance(result["tooltips"], list) or any(
                not isinstance(item, str) for item in result["tooltips"]):
            _invalid_message()
    return result


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
        if isinstance(action, str) and action in {"read", "auto-sync"}:
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
        if (not isinstance(payload["operation"], str)
                or payload["operation"] not in {
                    "fix", "reword", "add", "restyle", "history", "bulk", "auto",
                }):
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
            if (not isinstance(entry, dict) or set(entry) != {"type", "payload", "result"}
                    or not isinstance(entry.get("type"), str)
                    or entry["type"] in {"boot", "reset"}):
                _invalid_message()
            validate_worker_message({"type": entry["type"], "payload": entry["payload"]})
            validate_worker_result(entry["type"], entry["result"])
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
    addon_version = (f"https://api.github.com/repos/{config.ANKI_REPO}/contents/"
                     "version.json")
    real_get = net._http_get

    def _fixed_addon_version():
        return json.dumps({"version": config.ADDON_VERSION}).encode("utf8")

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
            if url.startswith(addon_version):
                return _fixed_addon_version()
            return real_get(url, token=token, accept=accept, on_chunk=on_chunk)

        net._http_get = _local_get
        return

    def _http_get(url, token=None, accept=None, timeout=None, on_chunk=None):
        if url.startswith(example):
            return _from_source(url)
        if url.startswith(addon_version):
            return _fixed_addon_version()
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
    replay = ReplayPlatform(epoch=RUNNER.fixture_revision + 1)
    with use_platform(replay):
        background._auto_sync_check()
        replay.advance(0, 100)
        if replay.pending():
            raise RuntimeError("background work did not settle")
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
        result = _worker_state()
        result["response"] = response
    elif message_type == "feed":
        response = json.loads(feed_protocol(
            json.dumps(payload["envelope"], allow_nan=False)))
        result = _worker_state()
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

    result = _redact_runtime_paths(result)
    validate_worker_result(message_type, result)
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


def _bump_in_manifest(manifest, deck_name):
    for deck in manifest["decks"]:
        if deck["name"] != deck_name:
            continue
        base = deck["version"].split("+")[0]
        number = int(deck["version"].split("+")[1]) + 1 \
            if "+" in deck["version"] else 1
        deck["version"] = f"{base}+{number}"


def _write_manifest(manifest):
    path = os.path.join(SOURCE, "manifest.json")
    with open(path, "w", encoding="utf8") as fh:
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

    elif op == "history":
        deck = decks[0]
        path = os.path.join(SOURCE, deck["apkg"])
        history = {}

        def edit(con):
            rows = list(con.execute("select id, guid, flds from notes order by id"))
            first_basic = next(row for row in rows
                               if "{{c" not in row[2].split(fs)[0])
            ordered = [first_basic] + [row for row in rows if row != first_basic]
            if len(ordered) < 4:
                raise RuntimeError("history fixture needs four notes")
            changed = ordered[:2]
            for index, (nid, guid, flds) in enumerate(changed, 1):
                parts = flds.split(fs)
                suffix = f" Coordinated revision {index}."
                if not parts[1].endswith(suffix):
                    parts[1] += suffix
                con.execute("update notes set flds=? where id=?", (fs.join(parts), nid))
                history.setdefault("changed", {})[guid] = parts
            retired_nid, retired_guid, retired_flds = ordered[2]
            moved_nid, moved_guid, moved_flds = ordered[3]
            moved_did = con.execute(
                "select did from cards where nid=? order by id limit 1",
                (moved_nid,)).fetchone()[0]
            decks_json = con.execute("select decks from col").fetchone()[0]
            moved_from = json.loads(decks_json)[str(moved_did)]["name"]
            con.execute("delete from cards where nid=?", (retired_nid,))
            con.execute("delete from notes where id=?", (retired_nid,))
            history["retired"] = (retired_guid, retired_flds.split(fs)[0])
            history["moved"] = (moved_guid, moved_flds.split(fs)[0], moved_from)

        def apply_history():
            _edit_apkg(path, edit)
            manifest = json.load(open(os.path.join(SOURCE, "manifest.json"),
                                      encoding="utf8"))
            manifest["schema"] = max(2, manifest.get("schema", 1))
            note = "coordinated revision across two cards"
            change_notes = manifest.setdefault("change_notes", {})
            for guid, fields in history["changed"].items():
                change_notes[guid] = [{
                    "kind": "maintainer", "note": note,
                    "hash": logic.note_fields_hash(fields),
                }]
            retired_guid, retired_front = history["retired"]
            manifest.setdefault("retired", {}).setdefault(deck["name"], {})[
                retired_guid] = {
                    "identity": retired_front,
                    "reason": "split into a focused replacement",
                    "superseded_by": list(history["changed"]),
                }
            moved_guid, moved_front, moved_from = history["moved"]
            manifest.setdefault("deck_moves", {})[moved_guid] = {
                "from": moved_from, "to": deck["name"] + " Review",
                "front": moved_front,
            }
            for item in manifest["decks"]:
                if item["name"] == deck["name"]:
                    item["cards"] = max(0, item.get("cards", 1) - 1)
            _bump_in_manifest(manifest, deck["name"])
            _write_manifest(manifest)

        RUNNER.maintain_fixture(apply_history)
        return json.dumps({"label": "published a grouped card-history update",
                           "deck": deck["name"].split("::")[-1]})

    elif op == "bulk":
        added = {"count": 0}

        def edit(con):
            note_columns = [column[1] for column in con.execute(
                "pragma table_info(notes)")]
            note_row = list(con.execute(
                "select * from notes order by id limit 1").fetchone())
            card_columns = [column[1] for column in con.execute(
                "pragma table_info(cards)")]
            card_row = list(con.execute(
                "select * from cards order by id limit 1").fetchone())
            next_note_id = con.execute("select max(id) from notes").fetchone()[0] + 1
            next_card_id = con.execute("select max(id) from cards").fetchone()[0] + 1
            for index in range(1, 181):
                guid = f"demo-bulk-{index:03d}"
                if con.execute("select 1 from notes where guid=?", (guid,)).fetchone():
                    continue
                note_fields = dict(zip(note_columns, note_row))
                parts = note_fields["flds"].split(fs)
                parts[0] = f"Bulk card {index:03d}"
                parts[1] = f"Example answer {index:03d}."
                for field_index in range(2, len(parts)):
                    parts[field_index] = ""
                note_fields.update(id=next_note_id, guid=guid,
                                   flds=fs.join(parts), sfld=parts[0])
                con.execute("insert into notes values (%s)" %
                            ",".join("?" * len(note_columns)),
                            [note_fields[column] for column in note_columns])
                card_fields = dict(zip(card_columns, card_row))
                card_fields.update(id=next_card_id, nid=next_note_id)
                con.execute("insert into cards values (%s)" %
                            ",".join("?" * len(card_columns)),
                            [card_fields[column] for column in card_columns])
                next_note_id += 1
                next_card_id += 1
                added["count"] += 1

        def apply_bulk():
            _edit_apkg(path, edit)
            manifest = json.load(open(os.path.join(SOURCE, "manifest.json"),
                                      encoding="utf8"))
            for item in manifest["decks"]:
                if item["name"] == deck["name"]:
                    item["cards"] = item.get("cards", 0) + added["count"]
            _bump_in_manifest(manifest, deck["name"])
            _write_manifest(manifest)

        RUNNER.maintain_fixture(apply_bulk)
        return json.dumps({"label": "added a large review batch",
                           "deck": deck["name"].split("::")[-1]})

    elif op == "auto":
        deck = decks[0]
        path = os.path.join(SOURCE, deck["apkg"])

        def edit(con):
            nid, flds = _first_basic_note(con)
            parts = flds.split(fs)
            suffix = " Automatic sync revision."
            if not parts[1].endswith(suffix):
                parts[1] += suffix
            con.execute("update notes set flds=? where id=?", (fs.join(parts), nid))

        label = "published an automatic sync revision"

    else:
        raise ValueError(op)

    def apply():
        _edit_apkg(path, edit)
        _bump(deck["name"])

    RUNNER.maintain_fixture(apply)
    return json.dumps({"label": label, "deck": deck["name"].split("::")[-1]})
