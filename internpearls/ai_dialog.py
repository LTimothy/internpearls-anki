"""The "Generate cards with AI" wizard.

A single QDialog holds a QStackedWidget of four pages: setup, input, progress,
review. Nothing here touches the collection until Import (_do_import); review,
editing, notes, and revisions are all in-memory session state, and closing the
dialog mid-review discards it after a confirm (see _GenerateDialog.reject).
"""
import html
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
from collections import deque

from aqt import mw
from aqt.qt import (QApplication, QCheckBox, QComboBox, QDialog,
                    QDialogButtonBox, QFrame, QHBoxLayout, QKeySequence, QLabel,
                    QPlainTextEdit, QPushButton, QRadioButton, QScrollArea,
                    QSpinBox, QStackedWidget, Qt, QTimer, QVBoxLayout, QWidget)

from . import ai_cli, ai_logic, collection
from .config import (ADDON_PACKAGE, APP_NAME, TARGET_FIELDS, _cfg, load_ai_usage,
                     save_ai_usage, load_deck_skill, save_deck_skill)
from .logic import cloze_filled_html, field_preview_html, plural
from .net import fetch_card_image
from .palette import colors
from .review import _CARET_CLOSED, _CARET_OPEN, _image_tag, _rich_label, _separator
from .ui import (_ask, _ask_scrollable, _info, _prompt, _safe, _warn, hint_label,
                 link_button, muted_label, title_label, tooltip)
from .widgets import CARET_GAP, CARET_W, chip_cell, chip_column_width

# Note types a generated card may name. Keep in sync with
# collection._GENERATED_ALLOWED_TYPES: the types this add-on manages, plus
# Anki's own core Basic and Cloze. "Study Deck - Image ID" is deliberately
# excluded from what's OFFERED for generation (see _build_input): its primary
# field IS the image, which the model has no way to supply. Images travel in
# a card's separate "images" list, never in a field value, so every such
# card fails parse_cards_json's empty-primary check and takes the whole reply
# down with it. The mapping stays here since import (collection.py) and
# mechanical_checks still need it for any such card already on disk.
FIELD_MAP = dict(TARGET_FIELDS, Basic=["Front", "Back"],
                 Cloze=["Text", "Back Extra"])
SOFT_SOURCE_LIMIT = 25000
# Image resolution (download/read/render) runs off the UI thread and polls on
# this cadence, the same as the generation worker's own poll below.
_IMG_POLL_MS = 200


def _skills_html(parts):
    """Render View skills' parts (plain multi-line skill text: model-authored
    and possibly containing literal '<' from the card-craft rules themselves,
    e.g. "an HTML <table>") as HTML a RichText dialog body shows readably.

    Escaped first, so a stray "<table>" or "&lt;94%" in the skill text reads as
    the literal characters it is, rather than being interpreted as markup:
    full transparency means showing exactly what was sent, not a mangled
    rendering of it. '\\n' is then turned into '<br>', since a plain newline
    collapses to a space in HTML and would otherwise run every line together.
    """
    return html.escape("\n".join(parts)).replace("\n", "<br>")


_FALLBACK_UNDO_SHORTCUT = "Cmd+Z" if sys.platform == "darwin" else "Ctrl+Z"


def _undo_shortcut():
    """The platform's own Undo accelerator, e.g. "Cmd+Z" on macOS, "Ctrl+Z"
    elsewhere: rendered by Qt itself from the standard Undo key sequence rather
    than hardcoded, so the import success message always names the key Edit >
    Undo actually shows, on whatever platform this happens to be running.

    Building a QKeySequence from a StandardKey asks Qt's platform theme for the
    binding, and with no live QApplication that lookup dereferences a null
    pointer and hard-crashes the whole process (SIGSEGV, not a catchable
    exception). Inside Anki a QApplication always exists, but a plain script
    or test importing this module does not get one for free: so check first
    and fall back to a plain literal rather than ever touching StandardKey with
    nothing to consult."""
    if QApplication.instance() is None:
        return _FALLBACK_UNDO_SHORTCUT
    return QKeySequence(QKeySequence.StandardKey.Undo).toString(
        QKeySequence.SequenceFormat.NativeText)


def _url_host(url):
    """The host a web image came from, for the review row: shown so the
    user can see where the model's suggested picture actually points before
    ever downloading it for real."""
    try:
        return urllib.parse.urlparse(url).hostname or url
    except Exception:
        return url


def _resolve_one_image(im, scratch):
    """Resolve one card image reference to bytes, off the UI thread. Never
    raises: every failure (a bad URL, a network error, a missing attachment)
    comes back as {"state": "error", ...} instead, so one bad image becomes
    one mechanical-check entry rather than an exception the caller has to
    guard every iteration against. Mirrors what _do_import used to do at
    import time (see ai_dialog module docstring): the only difference now is
    *when* this runs and that the result is kept for reuse instead of thrown
    away.
    """
    src = im.get("source", "")
    kind = ("url" if src.startswith("url:") else
           "svg" if src.startswith("svg:") else
           "attached" if src.startswith("attached:") else "other")
    try:
        if kind == "svg":
            name, data = ai_logic.svg_to_media(src[4:], 0)
            return {"state": "ok", "kind": "svg", "bytes": data, "name": name}
        if kind == "url":
            url = src[4:]
            data, ext = fetch_card_image(url)
            return {"state": "ok", "kind": "url", "bytes": data, "ext": ext,
                    "host": _url_host(url)}
        if kind == "attached":
            name = src.split(":", 1)[1]
            path = os.path.join(scratch, name)
            with open(path, "rb") as fh:
                data = fh.read()
            return {"state": "ok", "kind": "attached", "bytes": data,
                    "name": name, "path": path}
        return {"state": "error", "kind": kind,
                "error": f"unrecognized image source: {src!r}"}
    except Exception as e:
        err = {"state": "error", "kind": kind, "error": str(e)}
        if kind == "url":
            err["host"] = _url_host(src[4:])
        return err


def _image_row_html(session, i, card):
    """The review row's image line(s): a rendered thumbnail when one resolved
    to a local file Qt can decode, a plain failure message when it didn't, and
    for a web image the URL's host either way: this is I2's "the user sees
    what they're accepting" gate, so a card with an image never reviews as if
    it had none. Returns "" for a card with no images at all.
    """
    imgs = card.get("images") or []
    if not imgs:
        return ""
    results = session.image_data.get(i) or []
    lines = []
    for j, im in enumerate(imgs):
        res = results[j] if j < len(results) else None
        if res is None:
            lines.append("[image] resolving…")
            continue
        host = res.get("host")
        host_txt = f" (from {html.escape(host)})" if host else ""
        if res["state"] != "ok":
            lines.append(f"[image failed{host_txt}]: {html.escape(res.get('error', ''))}")
            continue
        tag = _image_tag(res["path"]) if res.get("path") else None
        lines.append((tag or "[image]") + host_txt)
    return "<br>".join(lines)


# The chip kinds the review row can actually show, in the priority order a card's
# checks are read for: any blocking check outranks a warning, which outranks a
# revision marker, which outranks a clean row. Passed to every chip_cell/
# chip_column_width call on this page so the gutter is measured against exactly
# these four words and none of the update screen's (see widgets.py:67-74).
_REVIEW_CHIP_KINDS = ("blocked", "warn", "ok", "revised")

# The include checkbox's own column width, measured the same way chip_column_width
# measures a pill: an unpolished QCheckBox's sizeHint reports the wrong font, and a
# fixed width is what keeps every row's checkbox (and everything after it) lined up
# in one column rather than each row's box floating at its own natural width.
_CHECK_W = {}


def _checkbox_column_width():
    if "w" not in _CHECK_W:
        probe = QCheckBox()
        probe.ensurePolished()
        _CHECK_W["w"] = probe.sizeHint().width()
    return _CHECK_W["w"]


def _review_row_indent():
    """Where the review row's primary text sits, and what its expanded body and its
    reason lines indent by: past the caret, the checkbox, and the chip, each with
    its own gap. Mirrors widgets.row_text_indent's arithmetic, but can't reuse it
    directly: that helper assumes every leading column is chip-width, and the
    checkbox here is not one."""
    return (CARET_W + CARET_GAP + _checkbox_column_width() + CARET_GAP
           + chip_column_width(_REVIEW_CHIP_KINDS) + CARET_GAP)


def _review_row_kind(entries, updated):
    """The one chip a review row wears: the worst thing true about the card, or
    (once nothing is wrong with it) whether the last revision touched it at all."""
    levels = {e.get("level") for e in entries}
    if "block" in levels:
        return "blocked"
    if "warn" in levels:
        return "warn"
    if updated:
        return "revised"
    return "ok"


def _accent_row(html_text, role, indent):
    """One dim line under a card's header, with a left accent bar in the given
    palette role: the same idiom review._change_note_row uses for a deck source's
    change note, reused here for a check's reason and for a queued revision note,
    so both read as the same kind of annotation on the row they belong to."""
    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(indent, 0, 0, 0)
    lay.setSpacing(0)
    label = muted_label(html_text)
    label.setTextFormat(Qt.TextFormat.RichText)
    c = colors()
    # The border-none reset is load-bearing: Qt drops a lone border-left on a QLabel
    # unless the border shorthand is cleared first.
    label.setStyleSheet(f"border: none; border-left: 3px solid {c[role + '_bg']};"
                        f" padding-left: 8px; color: {c['muted']};")
    lay.addWidget(label)
    return row


def _check_reason_row(entry, indent):
    """A check's own account of why it flagged the card: its message, and for a
    duplicate, the existing card it matched: both already computed by
    ai_logic.mechanical_checks and, before this, never shown anywhere (a
    [duplicate] chip said something was wrong and nothing said what)."""
    msg = html.escape(entry.get("message", ""))
    existing = entry.get("existing")
    if existing:
        msg += f" &middot; existing card: &ldquo;{html.escape(existing)}&rdquo;"
    role = "decline" if entry.get("level") == "block" else "updated"
    return _accent_row(f"<i>{msg}</i>", role, indent)


def _queued_note_row(note, indent):
    """The revision note queued for this card (see _note_card), shown on the row
    it belongs to instead of only living inside a modal prompt's memory."""
    text = f"<i>&ldquo;{html.escape(note)}&rdquo;</i>&nbsp;&nbsp;" \
          f"<span style='color:{colors()['dim']}; font-size:11px'>" \
          "&middot; sent on the next Revise all</span>"
    return _accent_row(text, "updated", indent)


def _card_primary_html(card):
    """The row's bold collapsed line: the front, or a cloze note's text with its
    deletions filled in: the fact under review lives in the deletions, so it is
    shown rather than blanked (mirrors review._primary_html)."""
    ntype = card["note_type"]
    primary_field = ai_logic.PRIMARY_FIELD.get(ntype, "Front")
    text = field_preview_html(card["fields"].get(primary_field, ""))
    if primary_field == "Text":
        text = cloze_filled_html(text, escape=False)
    return f"<b>{text}</b>"


def _card_body_fields(card):
    """The card's remaining fields, in the note type's own order, once the primary
    field and the never-shown Notes/Tag are out of the way: what a review row's
    expanded body actually has to show: the back, the why, and whatever else the
    note type carries (e.g. dosing)."""
    ntype = card["note_type"]
    primary_field = ai_logic.PRIMARY_FIELD.get(ntype, "Front")
    skip = {primary_field, "Notes", "Tag"}
    order = FIELD_MAP.get(ntype) or list(card["fields"])
    return [(n, card["fields"].get(n, "")) for n in order if n not in skip]


class _Session:
    """State shared by every page of the wizard."""

    def __init__(self):
        self.backend = None          # kind string
        self.cli_path = None
        self.mode = "thorough"       # or "quick"
        self.source = ""
        self.instructions = ""
        self.count = 10
        self.note_types = []         # selected type names
        self.deck_name = ""
        self.attachments = []        # [(path, {"text", "images", "images_undecoded"})]
        self.scratch = None          # tempfile.mkdtemp for this session
        # True once the "this PDF's images couldn't be decoded here" warning has
        # been shown for this session, so a second attached PDF hitting the same
        # environment limitation doesn't repeat it.
        self.pdf_image_warning_shown = False
        self.cards = []              # current draft (ai_logic card dicts)
        self.included = []           # bool per card
        self.notes = {}              # {index: revision note}
        self.checks = []             # mechanical_checks output
        self.updated = set()         # indexes changed by last revision
        # {card index: [resolved-image dict, ...]}, parallel to that card's
        # "images" list: see _resolve_one_image. Populated at review time
        # (before the review page ever shows), reused unchanged by import.
        self.image_data = {}
        # True when the last revision came back with a different card count than
        # it was sent: the one shape the prompt promises but nothing verifies.
        # See _finish_generation: this disables the per-index diff entirely.
        self.revision_shape_mismatch = False
        self.tokens_last_run = 0
        self.rate_limits = None
        # Per backend kind ({"claude": "", "codex": "", "agy": ""}), resolved from
        # config in __init__: see _cfg(). A value set under one backend must never
        # read back as if it applied to another; index by self.backend, never bare.
        self.ai_model = {}
        self.ai_effort = {}


class _GenerateDialog(QDialog):
    def __init__(self):
        super().__init__(mw)
        self.setWindowTitle(f"{APP_NAME}: Generate cards with AI")
        self.setMinimumWidth(480)
        # Opened at a size that gives the review page's card list real room, clamped
        # to the screen the way _ask_with_widget's own open_size does (ui.py), rather
        # than left to whatever the tallest page's natural sizeHint happens to be:
        # the input page's expanding source box used to set that for every page,
        # leaving the review and progress pages floating in space they never asked for.
        open_w, open_h = 640, 680
        try:
            geo = QApplication.primaryScreen().availableGeometry()
            open_w = min(open_w, geo.width() - 60)
            open_h = min(open_h, geo.height() - 80)
        except Exception:
            pass
        self.resize(max(open_w, 480), open_h)
        self.session = s = _Session()
        self._retried_json = False   # the single-retry budget on malformed model output
        # Backend kinds with a "Test connection" run currently in flight (from
        # either the setup page's per-backend button or the input page's single
        # backend button: both call _run_connection_test for the same kind).
        # Guards against Re-check re-enabling a button mid-test and a second
        # click starting a concurrent test that races the first to write the
        # same status label.
        self._testing_kinds = set()
        cfg = _cfg()
        s.deck_name = cfg["export_deck"] + "::" + ai_logic.GENERATED_DECK_LEAF
        s.ai_model = cfg["ai_model"]
        s.ai_effort = cfg["ai_effort"]
        # Guards the model/effort widgets' change signals while _refresh_model_
        # effort_controls repopulates them from session state, so that refresh
        # doesn't read back as a user edit and re-write config with what it just
        # loaded from config.
        self._updating_backend_controls = False

        self.stack = QStackedWidget()
        lay = QVBoxLayout(self)
        lay.addWidget(self.stack)

        self.setup_page = self._build_setup()
        self.input_page = self._build_input()
        self.progress_page = self._build_progress()
        self.review_page = self._build_review()
        for page in (self.setup_page, self.input_page,
                    self.progress_page, self.review_page):
            self.stack.addWidget(page)

        self._detect(cfg)

    def _guard(self, fn, *args, **kwargs):
        """Run a widget-signal callback the way @_safe protects a menu action, so a
        bug in it shows a plain, titled Intern Pearls dialog instead of Anki's raw
        crash box.

        @_safe on generate_cards() (module bottom) only covers exceptions that
        unwind back through *its own* call stack. A button's clicked signal doesn't
        go through that stack at all: Qt dispatches it directly from its own event
        loop, so an exception raised inside a slot reaches Anki's excepthook
        instead, which is exactly what happened to _do_import before this existed.
        Every callback wired to a widget signal that can meaningfully raise (import,
        revise, edit, note, test connection, attach, generate) goes through this.

        Always called from an explicit lambda with its own fixed argument list
        (e.g. `lambda: self._guard(self._attach)`), never connected to a signal
        directly: connecting a *args-style callable loses Qt's ability to
        introspect how many arguments the real slot wants, which is the same
        reason __init__.py's own menu wiring discards `checked` in a lambda rather
        than passing it through a decorator (see that file's comment). The lambda's
        own signature is what Qt reads, so this stays invisible to that mechanism.
        """
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            print(traceback.format_exc())
            _warn(f"Something went wrong: {e}")

    def _guard_completion(self, fn, *args, **kwargs):
        """Like _guard, but for the two QTimer callbacks that drive generation to
        completion (_poll_worker -> _finish_generation, and _poll_image_worker ->
        _apply_review_state) rather than a button click.

        _guard alone doesn't reach this path: a QTimer's timeout is dispatched by
        Qt's event loop exactly the same way a button's clicked signal is, so an
        unguarded exception here reached Anki's raw crash box too. But an exception
        here is also worse than an unguarded button click, not just as bad: by the
        time either poll fires its *last* time, it has already set _gen_done (or
        _img_done) and stopped its own timer (correct, so the cycle can't complete
        twice), which means if the completion code itself then raises, there is no
        longer any live worker or timer left for Cancel to act on. The dialog stayed
        on the progress page showing "Generating cards" forever, with Cancel wired to
        cancel a run that had already finished; closing the window was the only way
        out.

        So on top of _guard's own dialog, this makes the latched state consistent
        (both flags true, both timers stopped: redundant on the already-correct
        path, cheap insurance on the exception one) and always lands the user back on
        a page they can act on: the same place a cancelled or failed generation
        already lands (_return_to_input_or_review), with a hard fallback to the input
        page if even that raises.
        """
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            print(traceback.format_exc())
            _warn(f"Something went wrong: {e}")
            self._gen_done, self._img_done = True, True
            if hasattr(self, "_cancel_flag"):
                self._cancel_flag.set()
            if hasattr(self, "_img_cancel_flag"):
                self._img_cancel_flag.set()
            if hasattr(self, "_timer"):
                self._timer.stop()
            if hasattr(self, "_img_timer"):
                self._img_timer.stop()
            try:
                self._return_to_input_or_review()
            except Exception:
                print(traceback.format_exc())
                self.stack.setCurrentWidget(self.input_page)

    # === setup ================================================================
    def _build_setup(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.addWidget(title_label("Set up an AI assistant"))
        lay.addWidget(hint_label(
            "Card generation runs through an assistant you sign into yourself. "
            "This add-on never sees or stores your credentials: there is no API "
            "key field here, or anywhere else in the add-on."))
        self.setup_rows = {}
        self.test_buttons = {}
        self.test_status = {}
        self._detected_paths = {}
        for kind, meta in ai_cli.BACKENDS.items():
            row = QLabel()
            row.setWordWrap(True)
            self.setup_rows[kind] = row
            lay.addWidget(row)
            test_row = QHBoxLayout()
            btn = QPushButton("Test connection")
            btn.setEnabled(False)
            btn.clicked.connect(lambda _, k=kind: self._guard(self._test_setup_connection, k))
            status = hint_label("Not tested yet")
            self.test_buttons[kind] = btn
            self.test_status[kind] = status
            test_row.addWidget(btn)
            test_row.addWidget(status, 1)
            lay.addLayout(test_row)
        recheck_row = QHBoxLayout()
        self.recheck_btn = link_button("Re-check", on_click=lambda: self._detect(_cfg()))
        self.setup_status = hint_label("")
        recheck_row.addWidget(self.recheck_btn)
        recheck_row.addWidget(self.setup_status, 1)
        lay.addLayout(recheck_row)
        lay.addWidget(hint_label(
            "Install one of these, run it once in a terminal, and sign in "
            "there yourself. Then come back and re-check. \"Test connection\" "
            "runs a real, trivial prompt through a detected CLI to confirm it "
            "can actually generate, not just that the binary runs: unlike "
            "the status above, which is a cheap, free check."))
        lay.addStretch()
        bb = QDialogButtonBox()
        self.close_btn = bb.addButton("Close", QDialogButtonBox.ButtonRole.RejectRole)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        return page

    def _detect(self, cfg):
        s = self.session
        s.backend, s.cli_path = None, None
        preferred = cfg.get("ai_backend", "")
        for kind, meta in ai_cli.BACKENDS.items():
            override = cfg.get("ai_cli_path", "") if preferred == kind else ""
            path = ai_cli.find_cli(kind, override)
            self._detected_paths[kind] = path
            if kind not in self._testing_kinds:
                # A live test's own on_done re-enables its button and its poll
                # callback owns its status text; re-checking must not touch
                # either mid-test, or a second click here could start a
                # concurrent test racing the first to the same label.
                self.test_buttons[kind].setEnabled(bool(path))
                self.test_status[kind].setText("Not tested yet")
            if path:
                res = ai_cli.probe(kind, path)
                ok = res["ok"]
                # The three states the README promises: "installed and
                # working" here means only "the binary runs and exits 0":
                # NOT that it's actually signed in and can generate. That
                # deeper check is Test connection, deliberately on-demand
                # since it costs a real model call; this stays a cheap,
                # free --version probe.
                status = (f"installed and working ({res['detail']})" if ok
                          else "found, but not responding")
            else:
                ok, status = False, "not found"
            imgs = ("can view attached images" if ai_cli.image_capable(kind)
                    else "reads attached PDFs as text only, no images")
            # The safety string is shown verbatim, per-backend: the three
            # backends are not equally safe and this is not the place to
            # soften or average that out.
            self.setup_rows[kind].setText(
                f"{meta['label']} ({meta['subscription']}): {status}\n"
                f"{meta['safety']}. {imgs}.")
            if ok and (s.backend is None or preferred == kind):
                s.backend, s.cli_path = kind, path
        self.setup_status.setText(
            f"Ready: {ai_cli.BACKENDS[s.backend]['label']} detected." if s.backend
            else "No assistant detected yet.")
        self.stack.setCurrentWidget(
            self.input_page if s.backend else self.setup_page)
        if s.backend:
            self._refresh_backend_row()

    def _test_setup_connection(self, kind):
        path = self._detected_paths.get(kind)
        if not path or kind in self._testing_kinds:
            return
        self._testing_kinds.add(kind)
        btn, status = self.test_buttons[kind], self.test_status[kind]
        btn.setEnabled(False)

        def _done():
            self._testing_kinds.discard(kind)
            btn.setEnabled(True)
        self._run_connection_test(kind, path, status.setText, on_done=_done)

    def _run_connection_test(self, kind, path, on_status, on_done=None):
        """Run ai_cli.test_connection off the UI thread, polling the same way
        every other background call in this dialog does (a daemon thread plus
        a QTimer, never a blocking call on the UI thread). `on_status(text)`
        is called once with the final readable result; never the CLI's raw
        stderr, since ai_cli.test_connection already turned that into one
        short sentence. Referenced test threads/timers are kept alive on
        `self` for the duration of the test only (nothing this dialog owns
        long-term), since a local variable would be GC'd out from under a
        live QTimer.
        """
        on_status("Testing connection…")
        box = {}

        def worker():
            try:
                box["r"] = ai_cli.test_connection(kind, path)
            except Exception as e:
                box["e"] = e

        t = threading.Thread(target=worker, daemon=True)
        timer = QTimer(self)
        self._conn_test_refs = getattr(self, "_conn_test_refs", [])
        self._conn_test_refs.append((t, timer))

        def poll():
            if t.is_alive():
                return
            timer.stop()
            if "e" in box:
                on_status(f"Test failed: {box['e']}")
            else:
                r = box["r"]
                prefix = "Working: " if r["state"] == "working" else "Not working: "
                on_status(prefix + r["detail"])
            if on_done:
                on_done()

        timer.timeout.connect(poll)
        t.start()
        timer.start(_IMG_POLL_MS)

    # === input ==================================================================
    def _build_input(self):
        page = QWidget()
        lay = QVBoxLayout(page)

        self.source_box = QPlainTextEdit()
        self.source_box.setPlaceholderText(
            "Paste lecture notes, an article excerpt, or a topic outline")
        self.source_box.textChanged.connect(self._source_changed)
        lay.addWidget(QLabel("Source material"))
        lay.addWidget(self.source_box)
        self.char_label = hint_label("0 characters")
        lay.addWidget(self.char_label)

        self.attach_btn = QPushButton("Attach images or PDFs")
        self.attach_btn.clicked.connect(lambda: self._guard(self._attach))
        self.attach_label = hint_label("No files attached")
        attach_row = QHBoxLayout()
        attach_row.addWidget(self.attach_btn)
        attach_row.addWidget(self.attach_label)
        lay.addLayout(attach_row)

        self.instructions_box = QPlainTextEdit()
        self.instructions_box.setMaximumHeight(60)
        self.instructions_box.setPlaceholderText(
            'Anything specific to focus on, e.g. "emphasize dosing"')
        lay.addWidget(QLabel("Instructions (optional)"))
        lay.addWidget(self.instructions_box)

        # The radio itself carries only the short, stable name; the per-backend
        # truthful sentence (ai_cli.BACKENDS' "modes", set once a backend is known,
        # see _refresh_backend_row) moves to a hint_label underneath, word-
        # wrapped. A radio button doesn't wrap its own text, and that sentence runs
        # 150-200 characters, which used to set the radio's sizeHint to its full
        # width and, since a stacked widget's minimum is the max of its pages,
        # forced the whole dialog to roughly 1000px wide on every page. What mode
        # enforcement actually exists differs by backend, so the sentence still has
        # to be shown in full, honestly, per backend: it just no longer has to be
        # the thing that can't wrap.
        self.thorough_radio = QRadioButton("Thorough")
        self.quick_radio = QRadioButton("Quick draft")
        for radio in (self.thorough_radio, self.quick_radio):
            radio.setStyleSheet("font-weight: 600;")
        self.thorough_radio.setChecked(True)
        self.thorough_hint = hint_label("")
        self.quick_hint = hint_label("")
        lay.addWidget(QLabel("Quality"))
        lay.addWidget(self.thorough_radio)
        lay.addWidget(self.thorough_hint)
        lay.addWidget(self.quick_radio)
        lay.addWidget(self.quick_hint)

        self.count_spin = QSpinBox()
        self.count_spin.setRange(1, 50)
        self.count_spin.setValue(10)
        count_row = QHBoxLayout()
        count_row.addWidget(QLabel("Target count"))
        count_row.addWidget(self.count_spin)
        lay.addLayout(count_row)

        self.type_boxes = {}
        lay.addWidget(QLabel("Note types"))
        for name in FIELD_MAP:
            # Study Deck - Image ID isn't offered here at all: its primary
            # field IS the image, and a card's images travel separately from
            # its fields (see FIELD_MAP's comment), so the model has no way
            # to fill it. One such card fails parse_cards_json's empty-primary
            # check and takes the whole reply down with it.
            if name == "Study Deck - Image ID":
                continue
            # A managed type (Study Deck - Basic/Cloze) only exists in THIS
            # collection once its deck has been synced at least once:
            # _ensure_notetypes reconciles fields on a type that's already there,
            # it never creates one. Basic/Cloze are checked the same way rather
            # than assumed present, since a renamed or missing core type is exactly
            # as unusable here. Offering a type this collection doesn't have would
            # let someone write source material, wait through a whole generation,
            # and only discover at the very last click (Import) that
            # add_generated_notes rejects the entire batch: see collection.py's
            # _GENERATED_ALLOWED_TYPES check. Shown disabled rather than omitted,
            # so it's clear the type exists and *why* it isn't offered yet, not
            # just silently missing from the list.
            available = bool(mw.col.models.by_name(name))
            box = QCheckBox(name if available else f"{name} (sync your decks first)")
            box.setChecked(available)
            box.setEnabled(available)
            if not available:
                box.setToolTip(
                    f'"{name}" isn\'t in this collection yet. Sync your Intern '
                    "Pearls decks at least once to add it, or generate onto "
                    "Basic/Cloze instead.")
            self.type_boxes[name] = box
            lay.addWidget(box)

        self.deck_combo = QComboBox()
        self.deck_combo.setEditable(True)
        self.deck_combo.addItem(self.session.deck_name)
        lay.addWidget(QLabel("Destination deck"))
        lay.addWidget(self.deck_combo)

        self.backend_row = hint_label("")
        lay.addWidget(self.backend_row)

        # Model/effort controls: shown per-backend honesty, not a shared UI:
        # a backend with no verified model or effort flag doesn't get a control
        # it can't actually honor. See _refresh_model_effort_controls.
        self.model_label = QLabel("Model")
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_readonly = hint_label("")
        model_row = QHBoxLayout()
        model_row.addWidget(self.model_label)
        model_row.addWidget(self.model_combo, 1)
        model_row.addWidget(self.model_readonly, 1)
        lay.addLayout(model_row)
        self.model_combo.currentTextChanged.connect(
            lambda text: self._guard(self._model_changed, text))

        self.effort_label = QLabel("Effort")
        self.effort_combo = QComboBox()
        effort_row = QHBoxLayout()
        effort_row.addWidget(self.effort_label)
        effort_row.addWidget(self.effort_combo, 1)
        lay.addLayout(effort_row)
        self.effort_combo.currentIndexChanged.connect(
            lambda _: self._guard(self._effort_changed))

        backend_test_row = QHBoxLayout()
        self.backend_test_btn = link_button(
            "Test connection",
            on_click=lambda: self._guard(self._test_backend_connection))
        self.backend_test_status = hint_label("Not tested yet")
        backend_test_row.addWidget(self.backend_test_btn)
        backend_test_row.addWidget(self.backend_test_status, 1)
        lay.addLayout(backend_test_row)
        self.skills_link = link_button("View skills", on_click=self._view_skills)
        lay.addWidget(self.skills_link)
        self.usage_row = hint_label("")
        lay.addWidget(self.usage_row)

        lay.addStretch()
        bb = QDialogButtonBox()
        bb.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        self.generate_btn = bb.addButton(
            "Generate", QDialogButtonBox.ButtonRole.AcceptRole)
        self.generate_btn.setEnabled(False)
        self.generate_btn.setDefault(True)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(lambda: self._guard(self._start_generation))
        lay.addWidget(bb)
        return page

    def _source_changed(self):
        text = self.source_box.toPlainText()
        n = len(text)
        warn = "; consider splitting the material" if n > SOFT_SOURCE_LIMIT else ""
        self.char_label.setText(f"{n:,} characters{warn}")
        self.generate_btn.setEnabled(bool(text.strip()))

    def _refresh_backend_row(self):
        s = self.session
        meta = ai_cli.BACKENDS[s.backend]
        self.backend_row.setText(f"Backend: {meta['label']} (signed in)")
        self.thorough_hint.setText(meta["modes"]["thorough"])
        self.quick_hint.setText(meta["modes"]["quick"])
        reg = load_ai_usage()
        self.usage_row.setText(ai_logic.usage_line(
            reg, s.backend, now=time.time(), free_tier=(s.backend == "agy")))
        self.backend_test_status.setText("Not tested yet")
        self._refresh_model_effort_controls()

    def _refresh_model_effort_controls(self):
        """Rebuild the Model/Effort row for the now-current backend. Honesty
        pattern: a backend with no verified flag doesn't get a live control for
        it: agy's model is read-only text (there is no way to honor a choice
        there, see ai_cli.BACKENDS["agy"]), and effort is hidden entirely for
        any backend without a verified --effort flag (today, only claude has
        one). Guarded by _updating_backend_controls so repopulating these
        widgets from session/config state doesn't fire the change handlers and
        write that same state right back as if the user had edited it."""
        s = self.session
        meta = ai_cli.BACKENDS[s.backend]
        self._updating_backend_controls = True
        try:
            if s.backend == "agy":
                self.model_label.setText("Model")
                self.model_combo.setVisible(False)
                self.model_readonly.setVisible(True)
                self.model_readonly.setText(meta["model_hint"])
            else:
                self.model_readonly.setVisible(False)
                self.model_combo.setVisible(True)
                self.model_label.setText("Model")
                self.model_combo.clear()
                options = []
                if meta["default_model"]:
                    options.append(meta["default_model"])
                for alias in meta.get("model_aliases", []):
                    if alias not in options:
                        options.append(alias)
                self.model_combo.addItems(options)
                self.model_combo.setToolTip(meta["model_hint"])
                current = s.ai_model.get(s.backend, "") or meta["default_model"]
                self.model_combo.setEditText(current)
            effort_levels = meta.get("effort_levels")
            has_effort = bool(effort_levels)
            self.effort_label.setVisible(has_effort)
            self.effort_combo.setVisible(has_effort)
            if has_effort:
                self.effort_combo.clear()
                self.effort_combo.addItem(
                    f"Default ({meta['default_effort']})", "")
                for level in effort_levels:
                    self.effort_combo.addItem(level, level)
                # A hand-edited config value outside effort_levels must not show as
                # if it were a live override: fall back to the same "Default
                # (...)" entry build_argv's own fallback (resolve_claude_effort)
                # would actually send, so the combo always shows the effective
                # value, never a typo it can't find in its own list.
                raw_effort = s.ai_effort.get(s.backend, "")
                effective_effort = raw_effort if raw_effort in effort_levels else ""
                idx = self.effort_combo.findData(effective_effort)
                self.effort_combo.setCurrentIndex(idx if idx >= 0 else 0)
        finally:
            self._updating_backend_controls = False

    def _model_changed(self, text):
        if self._updating_backend_controls:
            return
        self.session.ai_model[self.session.backend] = text.strip()
        self._save_ai_model_effort()

    def _effort_changed(self):
        if self._updating_backend_controls:
            return
        self.session.ai_effort[self.session.backend] = (
            self.effort_combo.currentData() or "")
        self._save_ai_model_effort()

    def _save_ai_model_effort(self):
        conf = mw.addonManager.getConfig(ADDON_PACKAGE) or {}
        conf["ai_model"] = self.session.ai_model
        conf["ai_effort"] = self.session.ai_effort
        mw.addonManager.writeConfig(ADDON_PACKAGE, conf)

    def _test_backend_connection(self):
        s = self.session
        if not s.backend or not s.cli_path or s.backend in self._testing_kinds:
            return
        self._testing_kinds.add(s.backend)
        self.backend_test_btn.setEnabled(False)

        def _done():
            self._testing_kinds.discard(s.backend)
            self.backend_test_btn.setEnabled(True)
        self._run_connection_test(
            s.backend, s.cli_path, self.backend_test_status.setText, on_done=_done)

    def _attach(self):
        from aqt.qt import QFileDialog
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Attach source files", "",
            "Images and PDFs (*.png *.jpg *.jpeg *.webp *.gif *.pdf)")
        s = self.session
        if s.scratch is None:
            s.scratch = tempfile.mkdtemp(prefix="ip-aigen-")
        images_undecoded = False
        for p in paths:
            try:
                meta = ai_logic.extract_attachment(p, s.scratch)
            except ValueError as e:
                _warn(str(e))
                continue
            s.attachments.append((p, meta))
            images_undecoded = images_undecoded or meta["images_undecoded"]
        self.attach_label.setText(
            ", ".join(os.path.basename(p) for p, _ in s.attachments)
            or "No files attached")
        # Anki's own bundled Python doesn't carry Pillow, which pypdf needs to
        # decode a PDF's embedded images (its text still comes through fine):
        # say so once per session, right when it happens, rather than leaving
        # a PDF's figures silently missing with no explanation.
        if images_undecoded and not s.pdf_image_warning_shown:
            s.pdf_image_warning_shown = True
            _warn("The text came through, but embedded images in that PDF "
                  "couldn't be read in Anki's own Python. If you want any of "
                  "its figures on a card, attach them separately as image files.")

    def _view_skills(self):
        """Show what's actually sent to the model. Dismissing this dialog (Close,
        Escape, the window's close box) must never itself change consent: only
        an explicit click on the toggle button may. Rebuilt through _ask_scrollable's
        extra_label/on_extra: that button carries ActionRole, so clicking it runs
        the toggle and leaves the dialog open rather than answering (and closing)
        it, which is what made Escape and "decline" indistinguishable before."""
        deck = load_deck_skill()

        def _body(d):
            parts = ["Bundled: InternPearls authoring (ships with the add-on)", "",
                     ai_logic.load_bundled_skill()]
            if d:
                state = "enabled" if d.get("enabled") else "disabled"
                parts += ["", f"Deck skill v{d.get('version')} ({state}, "
                              f"consented {d.get('consented_on')})", "",
                          d.get("text", "")]
            return _skills_html(parts)

        if not deck:
            # The plain QMessageBox _info opens has no scroll area, so long text
            # just makes the box taller: with the bundled skill's own 48 lines
            # of prose, that box outgrew an 891px screen and left its own Close
            # button unreachable. _ask_scrollable's fixed-height viewport with the
            # button pinned outside it is what the deck-skill branch below
            # already uses; this is that same fix for the common (no deck skill
            # installed) case.
            _ask_scrollable(_body(deck), yes_label="Close", no_label=None)
            return

        def _label_for(d):
            return "Disable deck skill" if d.get("enabled") else "Enable deck skill"

        def _toggle(dlg):
            deck["enabled"] = not deck.get("enabled")
            save_deck_skill(deck)
            return _body(deck), _label_for(deck)

        _ask_scrollable(_body(deck), yes_label="Close", no_label=None,
                        extra_label=_label_for(deck), on_extra=_toggle)

    # === progress ===============================================================
    def _build_progress(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.addWidget(title_label("Generating cards"))
        self.progress_label = QLabel("Starting")
        self.phase_label = hint_label("")
        self.elapsed_label = hint_label("")
        lay.addWidget(self.progress_label)
        lay.addWidget(self.phase_label)
        lay.addWidget(self.elapsed_label)
        lay.addStretch()
        bb = QDialogButtonBox()
        cancel = bb.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        # Not bb.rejected -> self.reject: that would route Cancel through the
        # "discard the drafted cards" confirm reject() opens for the review page,
        # which this run in flight isn't on. This only stops the run; Escape and
        # the window's close box still go through reject()'s own generation-in-
        # progress confirm.
        cancel.clicked.connect(self._cancel_generation)
        lay.addWidget(bb)
        return page

    def _start_generation(self, revision=False, extra_error=None):
        s = self.session
        self._last_revision = revision
        # extra_error is only set on our own re-entry after a malformed reply
        # (see _finish_generation); any other call is a fresh request, so the
        # one-retry budget resets here rather than in the caller.
        if extra_error is None:
            self._retried_json = False
            if not revision:
                # A genuinely fresh (non-revision) request: Back-then-Generate
                # can reach here with an unrelated earlier draft still sitting
                # in s.cards, and _finish_generation's revision detection goes
                # only by "is s.cards non-empty": left uncleared, a brand
                # new draft would read as a revision of that stale one and
                # report a bogus "updated N, kept M verbatim" diff against it.
                s.cards, s.included, s.notes = [], [], {}
                s.updated, s.image_data = set(), {}
                s.revision_shape_mismatch = False
        s.mode = "thorough" if self.thorough_radio.isChecked() else "quick"
        self._duration_estimate = ai_logic.duration_estimate_line(
            load_ai_usage(), s.backend, s.mode)
        s.source = self.source_box.toPlainText()
        s.instructions = self.instructions_box.toPlainText()
        s.count = self.count_spin.value()
        s.note_types = [n for n, b in self.type_boxes.items() if b.isChecked()]
        s.deck_name = self.deck_combo.currentText().strip() or s.deck_name
        if s.scratch is None:
            s.scratch = tempfile.mkdtemp(prefix="ip-aigen-")
        extra_text = "\n\n".join(a[1]["text"] for a in s.attachments if a[1]["text"])
        image_names = [name for _, meta in s.attachments for name in meta["images"]]
        prompt = ai_logic.build_prompt(
            skills=ai_logic.active_skills(load_deck_skill()),
            source=(s.source + ("\n\n## Attached document text\n" + extra_text
                                if extra_text else "")),
            note_types=s.note_types, field_map=FIELD_MAP, count=s.count,
            instructions=s.instructions,
            attachments=image_names if ai_cli.image_capable(s.backend) else [],
            cards=s.cards if revision else None,
            feedback=self.feedback_box.toPlainText() if revision else "",
            notes=s.notes if revision else None,
            checks=s.checks if revision else None, mode=s.mode)
        if extra_error:
            prompt += ("\n\n## Your previous reply failed validation\n"
                      + "\n".join(extra_error))
        # A deque, not a plain list: the worker (producer) appends and the
        # poller (consumer) pops from the other end, so each event's removal
        # is one atomic op with no gap between "read" and "clear" where a
        # concurrently appended event could be silently dropped.
        self._events = deque()
        self._worker_error, self._worker_result = None, None
        self._gen_done = False
        self._cancel_flag = threading.Event()
        self._t0 = time.monotonic()
        image_paths = ([os.path.join(s.scratch, n) for n in image_names]
                       if ai_cli.image_capable(s.backend) else [])

        def work():
            try:
                self._worker_result = ai_cli.run_generation(
                    s.backend, s.cli_path, prompt, s.mode, s.scratch,
                    image_paths=image_paths, on_event=self._events.append,
                    cancel=self._cancel_flag.is_set,
                    model=s.ai_model.get(s.backend, ""),
                    effort=s.ai_effort.get(s.backend, ""))
            except Exception as e:
                self._worker_error = e

        self._worker = threading.Thread(target=work, daemon=True)
        self._worker.start()
        self.progress_label.setText(
            "Drafting cards with " + ai_cli.BACKENDS[s.backend]["label"])
        self.phase_label.setText("")
        self.stack.setCurrentWidget(self.progress_page)
        self._timer = QTimer(self)
        self._timer.timeout.connect(lambda: self._guard_completion(self._poll_worker))
        self._timer.start(200)

    def _poll_worker(self):
        # A stopped QTimer can still be fired once more by a queued signal, or
        # (in the test mock, which has no real event loop) by unrelated code
        # holding a reference to it; this generation cycle only ever runs
        # _finish_generation once.
        if self._gen_done:
            return
        while self._events:
            evt = self._events.popleft()
            if evt["type"] == "phase":
                self.phase_label.setText(evt["phase"])
            elif evt["type"] == "rate_limits":
                self.session.rate_limits = evt
        text = f"Elapsed {int(time.monotonic() - self._t0)}s"
        if self._duration_estimate:
            text += " · " + self._duration_estimate
        self.elapsed_label.setText(text)
        if self._worker.is_alive():
            return
        self._timer.stop()
        self._gen_done = True
        self._finish_generation()

    def _wait_for_worker(self, timeout=15):
        """Test helper: run the poll loop synchronously, without a live QTimer.

        A draft with images kicks off a second background phase once the CLI
        call itself finishes (see _start_image_phase), still on its own
        worker/timer pair rather than this one: drain that too, so a caller
        gets exactly the same end state a live QTimer eventually reaches,
        without needing to know a second phase happened at all.
        """
        end = time.time() + timeout
        while self._worker.is_alive() and time.time() < end:
            time.sleep(0.05)
        self._timer.stop()
        self._gen_done = True
        self._finish_generation()
        while (hasattr(self, "_img_worker") and not self._img_done
              and time.time() < end):
            while self._img_worker.is_alive() and time.time() < end:
                time.sleep(0.05)
            self._img_timer.stop()
            self._img_done = True
            self.session.image_data = self._img_results
            self._apply_review_state()

    def _cancel_generation(self):
        if hasattr(self, "_cancel_flag"):
            self._cancel_flag.set()
        if hasattr(self, "_img_cancel_flag"):
            self._img_cancel_flag.set()

    def _generation_in_progress(self):
        gen = hasattr(self, "_worker") and not getattr(self, "_gen_done", True)
        img = hasattr(self, "_img_worker") and not getattr(self, "_img_done", True)
        return gen or img

    def _cancel_running_generation(self):
        """Closing the dialog mid-generation must not orphan the run: this
        signals cancellation and stops the poll timer immediately (so a
        hidden dialog can never receive a queued _poll_worker -> _finish_
        generation call and pop a modal or navigate after the user is gone),
        but does NOT block the close on however long the child takes to die.
        _gen_done/_img_done latch right here, before either worker thread has
        actually exited, which is what makes that guarantee unconditional.
        Covers both the CLI generation phase and the (optional) image-
        resolution phase that can follow it: whichever is actually running.

        The scratch dir is the live workers' cwd (the CLI's --add-dir target,
        and where image resolution reads/writes), so a background reaper
        thread waits on every worker before removing it, rather than racing
        it: each join is capped at 30s, after which the reaper deletes the
        directory regardless, so a wedged worker can't leak it forever."""
        self._gen_done = True
        self._img_done = True
        if hasattr(self, "_cancel_flag"):
            self._cancel_flag.set()
        if hasattr(self, "_img_cancel_flag"):
            self._img_cancel_flag.set()
        if hasattr(self, "_timer"):
            self._timer.stop()
        if hasattr(self, "_img_timer"):
            self._img_timer.stop()
        workers = [w for w in (getattr(self, "_worker", None),
                               getattr(self, "_img_worker", None)) if w]
        session = self.session

        def _reap():
            for w in workers:
                w.join(timeout=30)
            if session.scratch:
                shutil.rmtree(session.scratch, ignore_errors=True)
                session.scratch = None

        threading.Thread(target=_reap, daemon=True).start()

    def _finish_generation(self):
        s = self.session
        err, res = self._worker_error, self._worker_result
        if isinstance(err, ai_cli.GenerationCancelled):
            self._return_to_input_or_review()
            return
        if err or not res:
            _warn(f"Generation failed: {err}")
            self._return_to_input_or_review()
            return
        cards, errors = ai_logic.parse_cards_json(res["text"], s.note_types, FIELD_MAP)
        if errors:
            if not self._retried_json:
                self._retried_json = True
                self._start_generation(revision=self._last_revision,
                                       extra_error=errors)
                return
            _warn("The assistant's reply still could not be used after a "
                  "retry:\n" + "\n".join(errors[:5]))
            self._return_to_input_or_review()
            return
        s.tokens_last_run = res["tokens"]
        reg = ai_logic.record_usage(load_ai_usage(), s.backend, res["tokens"],
                                    now=time.time())
        reg = ai_logic.record_duration(reg, s.backend, s.mode, res["duration_s"])
        save_ai_usage(reg)

        # Card matching across a revision, by position: the prompt sends the
        # previous draft and instructs the model to return the SAME cards in the
        # SAME order, marking only the ones without a note "keep verbatim" (see
        # ai_logic.build_prompt). Nothing here can verify that promise was kept,
        # so a card count that doesn't match what was sent is the one signal
        # available that positional matching can no longer be trusted at all.
        # is_revision is False only for the very first draft (nothing to diff
        # against yet); same_shape is False either for that first draft or for
        # a genuine reply-shape mismatch.
        prev_cards, prev_included = s.cards, s.included
        is_revision = bool(prev_cards)
        same_shape = is_revision and len(cards) == len(prev_cards)
        s.revision_shape_mismatch = is_revision and not same_shape

        if same_shape:
            s.updated = {i for i in range(len(cards))
                        if prev_cards[i] != cards[i]}
        elif is_revision:
            # Can't tell which (if any) of the new cards match the old ones, so
            # nothing is claimed "kept verbatim": see _rebuild_review's header.
            s.updated = set(range(len(cards)))
        else:
            s.updated = set()

        s.cards = cards
        s.notes = {}
        # Carried through to _apply_review_state once images (if any) have
        # resolved: None means "not a revision", so every card falls back to
        # the mechanical-check default; otherwise it's the pre-revision
        # include list, consulted only for cards _rebuild_review's diff
        # didn't mark as updated.
        self._pending_prev_included = prev_included if same_shape else None
        self._start_image_phase()

    def _start_image_phase(self):
        """Resolve every image on the current draft before review ever shows
        one: I2's gate. No images anywhere is the common case (most cards
        carry none) and stays exactly as cheap as before: straight to
        _apply_review_state with nothing to wait on. Any image at all moves
        the resolution work (network downloads included) onto its own
        background thread, polled the same way the CLI generation phase
        itself is polled just above, so this can never freeze the dialog."""
        s = self.session
        if not any(card["images"] for card in s.cards):
            s.image_data = {}
            self._apply_review_state()
            return
        self._run_image_resolution(list(s.cards))

    def _run_image_resolution(self, cards):
        s = self.session
        self._img_results = {}
        self._img_cancel_flag = threading.Event()
        self._img_done = False

        def work():
            results = {}
            for i, card in enumerate(cards):
                per = []
                for im in card["images"]:
                    if self._img_cancel_flag.is_set():
                        per.append({"state": "error", "kind": "cancelled",
                                   "error": "cancelled"})
                        continue
                    res = _resolve_one_image(im, s.scratch)
                    if res["state"] == "ok" and res["kind"] != "attached":
                        # attached: images already live in scratch under
                        # their own name (res["path"] is set for that kind
                        # above); url:/svg: bytes only ever existed in
                        # memory, so a thumbnail needs its own file to hand
                        # Qt's QImage a path to load.
                        ext = res["ext"] if res["kind"] == "url" else "svg"
                        thumb = os.path.join(s.scratch, f"_thumb-{i}-{len(per)}.{ext}")
                        try:
                            with open(thumb, "wb") as fh:
                                fh.write(res["bytes"])
                            res["path"] = thumb
                        except OSError:
                            pass
                    per.append(res)
                results[i] = per
            self._img_results = results

        self._img_worker = threading.Thread(target=work, daemon=True)
        self._img_worker.start()
        self.progress_label.setText("Resolving images")
        self.phase_label.setText("")
        self.stack.setCurrentWidget(self.progress_page)
        self._img_timer = QTimer(self)
        self._img_timer.timeout.connect(
            lambda: self._guard_completion(self._poll_image_worker))
        self._img_timer.start(_IMG_POLL_MS)

    def _poll_image_worker(self):
        if self._img_done:
            return
        if self._img_worker.is_alive():
            return
        self._img_timer.stop()
        self._img_done = True
        self.session.image_data = self._img_results
        self._apply_review_state()

    def _apply_review_state(self):
        """Compute mechanical checks (including any image-resolution
        failures) and default include state, then show the review page.
        Called directly by _start_image_phase when a draft has no images, or
        once the background image-resolution phase above has finished."""
        s = self.session
        image_errors = {i: [r["error"] for r in results if r.get("state") == "error"]
                        for i, results in s.image_data.items()}
        image_errors = {i: msgs for i, msgs in image_errors.items() if msgs}
        s.checks = ai_logic.mechanical_checks(
            s.cards, collection.existing_front_map(_cfg()["scope_tag"]),
            image_errors)
        default_included = [
            not any(c["level"] == "block" for c in per) and not s.cards[i]["images"]
            for i, per in enumerate(s.checks)]
        prev_included = self._pending_prev_included
        if prev_included is not None:
            # A card the revision left verbatim keeps whatever the user set for
            # it (an override she made on purpose survives); only a genuinely
            # new or changed card falls back to the mechanical-check default
            # (which, per I2, excludes any card carrying an image she hasn't
            # had a chance to look at yet in ITS current form).
            s.included = [prev_included[i] if i not in s.updated
                         else default_included[i] for i in range(len(s.cards))]
        else:
            s.included = default_included
        self._pending_prev_included = None
        self._rebuild_review()
        self.stack.setCurrentWidget(self.review_page)

    def _return_to_input_or_review(self):
        """Where a cancelled or failed request lands. A revision (Revise all)
        starts from an existing draft that _finish_generation never touches
        until a new draft actually parses, so on cancel/failure s.cards is
        still the pre-revision draft: send the user back to it (with its
        hand edits, include choices, and queued notes intact) rather than the
        input page, which is otherwise a dead end no Back button reaches. A
        first generation has no draft yet, so it still falls back to input."""
        if self._last_revision and self.session.cards:
            self._rebuild_review()
            self.stack.setCurrentWidget(self.review_page)
        else:
            self.stack.setCurrentWidget(self.input_page)

    # === review =================================================================
    def _build_review(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.addWidget(title_label("Review drafted cards"))
        self.review_header = muted_label("")
        lay.addWidget(self.review_header)
        # The card list owns the dialog's surplus height instead of leaving it to
        # spread across every row (see widgets.StreamingList / review.py:1289): a
        # scroll area sized by stretch factor 1, with the button row (Import
        # included) outside it and always reachable, however many cards are drafted.
        cards_container = QWidget()
        self.cards_lay = QVBoxLayout(cards_container)
        cards_scroll = QScrollArea()
        cards_scroll.setWidgetResizable(True)
        cards_scroll.setFrameShape(QFrame.Shape.NoFrame)
        cards_scroll.setWidget(cards_container)
        lay.addWidget(cards_scroll, 1)
        # Run-level facts (token spend, the rate-limit window, the revision diff
        # summary) rather than what she's deciding between: see
        # _update_review_summary. Hidden entirely when there's nothing to say
        # (the very first draft of a session, before any run has billed anything).
        self.review_footer = hint_label("")
        lay.addWidget(self.review_footer)
        lay.addWidget(QLabel("Feedback on the whole set (optional)"))
        self.feedback_box = QPlainTextEdit()
        self.feedback_box.setMaximumHeight(60)
        self.feedback_box.setPlaceholderText(
            "e.g. shorter answers, add one card on avoided drugs")
        lay.addWidget(self.feedback_box)
        bb = QDialogButtonBox()
        back = bb.addButton("Back", QDialogButtonBox.ButtonRole.ActionRole)
        back.clicked.connect(lambda: self.stack.setCurrentWidget(self.input_page))
        self.revise_btn = bb.addButton(
            "Revise all", QDialogButtonBox.ButtonRole.ActionRole)
        self.revise_btn.clicked.connect(lambda: self._guard(self._revise_all))
        self.import_btn = bb.addButton(
            "Import", QDialogButtonBox.ButtonRole.AcceptRole)
        self.import_btn.clicked.connect(lambda: self._guard(self._do_import))
        self.import_btn.setDefault(True)
        lay.addWidget(bb)
        return page

    def _rebuild_review(self):
        """(Re)populate the review page's card list from session state, on the same
        row skeleton the update screen's own review._card_row draws: a caret column,
        a fixed chip column, a bold primary line, and a body the caret reveals
        holding the back, why, dosing and images. Hairlined between rows rather than
        around, the same convention build_list_body's append_rows uses."""
        s = self.session
        while self.cards_lay.count():
            item = self.cards_lay.takeAt(0)
            w = item.widget() if item else None
            if w is not None:
                # A row taken out of the layout still paints (and still answers
                # isVisible()) until Qt gets around to deleteLater's deferred
                # removal; hiding it here is what keeps a stale row from flashing
                # on screen for one frame while the new rows are being built.
                w.setVisible(False)
                w.deleteLater()
        self.include_boxes = []
        for i, card in enumerate(s.cards):
            if i:
                self.cards_lay.addWidget(_separator())
            self.cards_lay.addWidget(self._build_review_row(i, card))
        self.cards_lay.addStretch()   # keeps a short list pinned to the top, not floating
        self._update_review_summary()

    def _build_review_row(self, i, card):
        """One drafted card as a row: caret, chip, include checkbox, bold front.
        The check(s) flagging it and any queued revision note sit under the header,
        visible whether the row is open or not (the same treatment
        review._change_note_row gives a deck source's own change note); the back,
        why, dosing and images sit in the body the caret reveals. Edit and Note are
        link_buttons at the end of that body (review.py's own "Add note"
        placement) rather than native push buttons crowding every row.
        """
        s = self.session
        entries = s.checks[i]
        kind = _review_row_kind(entries, i in s.updated)
        indent = _review_row_indent()

        row = QWidget()
        outer = QVBoxLayout(row)
        outer.setContentsMargins(0, 5, 0, 6)
        outer.setSpacing(4)

        body = QWidget()
        caret = QPushButton(_CARET_CLOSED)

        def _toggle():
            expanded = not body.isVisible()
            body.setVisible(expanded)
            caret.setText(_CARET_OPEN if expanded else _CARET_CLOSED)

        header = QWidget()
        hlay = QHBoxLayout(header)
        hlay.setContentsMargins(0, 0, 0, 0)
        hlay.setSpacing(CARET_GAP)

        caret.setFlat(True)
        caret.setFixedWidth(CARET_W)
        caret.setStyleSheet(f"border: none; padding: 0; font-weight: 600;"
                            f" color: {colors()['caret']};")
        caret.setCursor(Qt.CursorShape.PointingHandCursor)
        caret.clicked.connect(_toggle)
        hlay.addWidget(caret, 0, Qt.AlignmentFlag.AlignTop)

        hlay.addWidget(chip_cell(kind, _REVIEW_CHIP_KINDS), 0, Qt.AlignmentFlag.AlignTop)

        box = QCheckBox()
        box.setChecked(s.included[i])
        box.setAccessibleName("Include this card")
        box.toggled.connect(lambda v, idx=i: self._guard(self._on_include_toggled, idx, v))
        self.include_boxes.append(box)
        hlay.addWidget(box, 0, Qt.AlignmentFlag.AlignTop)

        primary = _rich_label(_card_primary_html(card))
        primary.setWordWrap(True)
        hlay.addWidget(primary, 1)

        # The note type: no longer a parenthetical crowding the front, but a
        # quiet trailing label off to the row's right, the same treatment
        # widgets.simple_row gives a trailing count or destination: metadata about
        # the row, read after the card itself rather than inline with it.
        type_label = hint_label(html.escape(card["note_type"]))
        type_label.setWordWrap(False)
        hlay.addWidget(type_label, 0, Qt.AlignmentFlag.AlignTop)

        outer.addWidget(header)

        for entry in entries:
            if entry.get("level") != "ok":
                outer.addWidget(_check_reason_row(entry, indent))
        if i in s.notes:
            outer.addWidget(_queued_note_row(s.notes[i], indent))

        body.setVisible(False)
        blay = QVBoxLayout(body)
        blay.setContentsMargins(indent, 2, 0, 2)
        blay.setSpacing(4)

        for name, value in _card_body_fields(card):
            html_value = field_preview_html(value)
            if not html_value:
                continue
            if name == "Why":
                label = _rich_label(html_value)
                why_colour = colors()["why"]
                label.setStyleSheet(
                    f"border: none; border-left: 3px solid {why_colour};"
                    f" padding-left: 8px; color: {why_colour};")
            elif name == "Dosing":
                c = colors()
                label = _rich_label(f"<b>Dosing</b> &nbsp;{html_value}")
                label.setStyleSheet(f"background: {c['dosing_bg']}; color: {c['dosing_fg']};"
                                    f" padding: 6px; border-radius: 4px;")
            else:
                label = _rich_label(html_value)
            blay.addWidget(label)

        img_html = _image_row_html(s, i, card)
        if img_html:
            blay.addWidget(_rich_label(img_html))

        edit_btn = link_button("Edit", on_click=lambda: self._guard(self._edit_card, i))
        note_btn = link_button("Note", on_click=lambda: self._guard(self._note_card, i))
        links = QWidget()
        llay = QHBoxLayout(links)
        llay.setContentsMargins(0, 0, 0, 0)
        llay.setSpacing(CARET_GAP)
        llay.addWidget(edit_btn)
        llay.addWidget(note_btn)
        llay.addStretch()
        blay.addWidget(links)

        outer.addWidget(body)
        return row

    def _on_include_toggled(self, i, value):
        """A card's checkbox flipped: write the new state and refresh what depends
        on it. Split from _rebuild_review's own toggled.connect (which used to
        write s.included and stop there) because that left the toggle with no
        visible effect at all: the box itself flips (Qt paints that on its own),
        but nothing ever recomputed the "N included" header or the "Import N
        cards" button label, so a click that genuinely worked read as doing
        nothing. Updates only those two labels rather than calling the full
        _rebuild_review: a checkbox toggle doesn't change any card's text, badges,
        or note, so nothing else on the page needs to move, and rebuilding every
        row (including the very one mid-click) is unnecessary churn.
        """
        self.session.included[i] = value
        self._update_review_summary()

    def _update_review_summary(self):
        """Recompute the review header, the run-level footer, and the two button
        labels from current session state. Called after a full _rebuild_review,
        and on its own by _on_include_toggled, which needs exactly this and
        nothing more.

        Split in two, the way the update screen separates what the reader is
        deciding from run-level facts (review.py:1238-1246): the header under
        the title stays what she's actually choosing between (how many cards,
        how many she's kept included), and everything about the run itself
        (token spend, the rate-limit window, the revision diff) moves to a
        small-print footer under the list, rather than one line carrying both.
        """
        s = self.session
        n_inc = sum(s.included)
        self.review_header.setText(
            f"Review {plural(len(s.cards), 'draft card')} · {n_inc} included")

        footer = (f"Last run ~{round(s.tokens_last_run / 1000)}k tokens"
                 if s.tokens_last_run else "")
        if s.rate_limits:
            footer += (" · " if footer else "") + ai_logic.rate_limit_line(s.rate_limits)
        if s.revision_shape_mismatch:
            # Degrade honestly: a card count that doesn't match what was sent
            # means the per-index diff isn't trustworthy, so this says so
            # instead of showing a confident "kept N verbatim" that could be wrong.
            footer += (" · " if footer else "") + (
                "the assistant returned a different number of cards than "
                "before, so nothing here is marked kept-verbatim")
        elif s.updated:
            kept = len(s.cards) - len(s.updated)
            footer += (" · " if footer else "") + f"updated {len(s.updated)}, kept {kept} verbatim"
        self.review_footer.setText(footer)
        self.review_footer.setVisible(bool(footer))

        self.import_btn.setText(f"Import {plural(n_inc, 'card')}")
        self.revise_btn.setText(
            "Revise all" + (f" ({plural(len(s.notes), 'note')})" if s.notes else ""))

    def _edit_card(self, i):
        """Hand-edit one card's fields, right in the review list. Nothing here
        touches the model: this is a plain in-memory edit, prompted field by
        field, cancellable at any field without losing the ones already typed."""
        card = self.session.cards[i]
        for name in FIELD_MAP[card["note_type"]]:
            new = _prompt(f"{name}:", default=card["fields"].get(name, ""))
            if new is None:
                return
            card["fields"][name] = new
        self._rebuild_review()

    def _note_card(self, i):
        """Queue (or clear) a per-card revision note. This never calls the
        model by itself: it only marks the card for the next Revise all,
        which is what sends every queued note in one turn (see _revise_all)."""
        note = _prompt("Revision note for this card:",
                       default=self.session.notes.get(i, ""))
        if note is not None:
            if note.strip():
                self.session.notes[i] = note.strip()
            else:
                self.session.notes.pop(i, None)
            self._rebuild_review()

    def _revise_all(self):
        """The only path that sends card-level feedback to the model: one CLI
        turn carrying the set-level feedback plus every queued note, with
        every un-noted card marked keep-verbatim (see ai_logic.build_prompt)."""
        self._start_generation(revision=True)

    def _do_import(self):
        """Write the included cards to the collection. Every image was already
        resolved to bytes back at review time (see _start_image_phase /
        session.image_data), so this only reuses that: it never re-touches
        the network, never re-reads an attachment, and can't block. Hands
        add_generated_notes the bytes plus the filenames it chose, per its
        documented media contract.

        Nothing selected is not an import: it neither writes anything nor
        closes the wizard (there is no undo step to claim), so the draft
        stays on the review page instead of being silently discarded.

        svg_index is a running counter across the WHOLE batch, not per card:
        the filename is chosen here from the index alone, so two cards each
        drawing their own SVG would collide on the same filename (and one
        image silently overwrite the other) if each card started counting
        from 0 again. A url:/attached: image can't collide with an svg: one
        either, since a url: filename always carries a raster extension and
        an attached name is scoped to its own scratch dir.
        """
        s = self.session
        pairs = [(i, c) for i, (c, inc) in enumerate(zip(s.cards, s.included)) if inc]
        if not pairs:
            _info("Nothing is selected to import. Check at least one card, "
                  "or Cancel to discard the draft.")
            return 0
        media = {}
        svg_index = 0
        for pos, (orig_i, card) in enumerate(pairs):
            files = []
            resolved = s.image_data.get(orig_i) or []
            for j in range(len(card["images"])):
                res = resolved[j] if j < len(resolved) else None
                if not res or res.get("state") != "ok":
                    # One bad image is not worth losing the whole card over:
                    # warn and move on, same as a mechanical check would flag
                    # it (and, per I2, already did: this is the fallback for
                    # a card the user included anyway).
                    _warn(f"Skipping an image on card {pos + 1}: "
                          f"{(res or {}).get('error', 'not resolved')}")
                    continue
                if res["kind"] == "svg":
                    name = f"generated-{svg_index}.svg"
                    svg_index += 1
                elif res["kind"] == "url":
                    name = f"generated-{pos}-{len(files)}.{res['ext']}"
                else:  # attached
                    name = res["name"]
                media[name] = res["bytes"]
                files.append(name)
            card["_media_files"] = files
        cards = [c for _, c in pairs]
        # add_generated_notes can raise (e.g. Basic/Cloze missing or renamed on a
        # non-English profile). Cleanup waits until AFTER a successful import:
        # if this raises, the scratch dir must still be there for a retry.
        n = collection.add_generated_notes(cards, media, s.deck_name,
                                           _cfg()["scope_tag"])
        # add_generated_notes only writes the collection; nothing about that tells
        # Anki's main window a new undo entry exists or that the deck list changed
        # underneath it. mw.reset() is the same notification every other
        # collection-writing action here already gives (see collection.py), and it
        # does both jobs at once: it calls mw.update_undo_actions() (enabling Edit >
        # Undo for the entry add_generated_notes just merged) and fires the reset
        # hooks the deck browser listens for (refreshing the deck list and its
        # counts) without needing a second, narrower call for either.
        mw.reset()
        self._cleanup_scratch()
        # A transient toast, not a modal: this is what Anki's own Add shows after
        # adding notes, and a click-through confirmation here is one extra click
        # for news that doesn't need an answer.
        tooltip(f"{plural(n, 'card')} added to {s.deck_name}. This is one undo "
               f"step: {_undo_shortcut()} reverts it.", period=6000, parent=mw)
        self.accept()
        return n

    def _cleanup_scratch(self):
        """Remove the session's scratch directory (extracted attachment images,
        PDF-embedded images, real source material). Called directly on every
        path that ends this dialog EXCEPT closing mid-generation, which hands
        the same cleanup to a background reaper instead (see
        _cancel_running_generation) so closing never blocks on a live worker.
        Best-effort: a failure to delete must never raise into the user's face
        or block the dialog from closing, and nothing about a session may
        outlive it (see the module docstring's storage policy)."""
        if self.session.scratch:
            shutil.rmtree(self.session.scratch, ignore_errors=True)
            self.session.scratch = None

    def reject(self):
        """Closing mid-review discards everything unsaved: nothing about a
        draft, a note, or a prompt is ever written to disk (see the module
        docstring), so this confirmation is the only chance to back out.

        Qt routes Escape and the window's close box through this same method
        (QDialog's own closeEvent calls reject()), so this is the one place
        that has to handle closing mid-generation too: a run in flight is
        real, billed work, not something to silently throw away on a stray
        Escape, so it gets the same kind of confirm as discarding a draft:
        but once confirmed, the actual cancel is handed off (see
        _cancel_running_generation) rather than blocking this call on
        however long the subprocess takes to die."""
        if (self.stack.currentWidget() is self.review_page
                and self.session.cards
                and not _ask(
                    "Discard the drafted cards? Nothing from this session "
                    "is saved between sessions.",
                    yes_label="Discard", no_label="Keep editing")):
            return
        if self._generation_in_progress():
            if not _ask(
                    "A generation is still running. Closing now cancels it "
                    "and discards this run.",
                    yes_label="Cancel and close", no_label="Keep waiting"):
                return
            self._cancel_running_generation()
        else:
            self._cleanup_scratch()
        super().reject()


@_safe
def generate_cards():
    _GenerateDialog().exec()
