"""The "Generate cards with AI" wizard.

A single QDialog holds a QStackedWidget of four pages: setup, input, progress,
review. Nothing here touches the collection until Import (_do_import); review,
editing, notes, and revisions are all in-memory session state, and closing the
dialog mid-review discards it after a confirm (see _GenerateDialog.reject).
"""
import html
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
from collections import Counter, deque

from aqt import mw
from aqt.qt import (QApplication, QCheckBox, QComboBox, QDialog,
                    QDialogButtonBox, QFontMetrics, QFrame, QGridLayout,
                    QHBoxLayout, QKeySequence, QLabel, QLayout, QLineEdit,
                    QPlainTextEdit, QPushButton, QRadioButton, QScrollArea,
                    QSpinBox, QStackedWidget, Qt, QTimer, QVBoxLayout, QWidget)

from . import ai_cli, ai_logic, collection
from .ai_setup import (LABEL_W, _open_url, _safe_settle, _settle_min_size,
                       _wrapped_hint, run_connection_test_async)
from .config import (AI_LAST_RUN_LOG, APP_NAME, TARGET_FIELDS, _cfg,
                     load_ai_usage, save_ai_usage, load_deck_skill,
                     save_deck_skill, load_user_skill, save_user_skill)
from .logic import cloze_filled_html, field_preview_html, note_display_label, plural
from .net import fetch_card_image
from .palette import colors
from .review import (_CARET_CLOSED, _CARET_OPEN, _ClickableLabel, _image_tag,
                     _preview_style, _rich_label, _separator)
from .ui import (_ask, _ask_scrollable, _info, _prompt, _safe, _warn, hint_label,
                 link_button, muted_label, section_rule, title_label, tooltip)
from .widgets import (CARET_GAP, CARET_W, align_field_column, chip_cell,
                      chip_column_width, decision_cell, field_slot,
                      row_text_indent)

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

# The Advanced grid's own row labels (_GenerateDialog._build_advanced), named
# once so the column-width measurement and each row's own _advanced_label
# call can never drift apart from what the other actually shows.
_LABEL_COUNT = "Exact number of cards"
_LABEL_DEPTH = "Depth"
_LABEL_TYPES = "Note types"
_LABEL_DECK = "Destination deck"


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


class _UserSkillDialog(QDialog):
    """Intern Pearls: My rules. Plain text the learner wants sent on every run,
    after the bundled and deck skills. Saved on OK; an empty box removes it."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME}: My rules")
        lay = QVBoxLayout(self)
        lay.addWidget(title_label("My rules"))
        lay.addWidget(hint_label(
            "Sent to the assistant after the bundled and deck skills, on every run. "
            "Plain text. It costs tokens each turn, so keep it short and specific: "
            "what to emphasise, what to avoid, how you like a card phrased. On "
            "style, wording, and emphasis, your own rules win over the bundled "
            "skill where the two disagree; the output format and the rule against "
            "raster images always win, over these rules included."))
        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText(
            "Prefer cloze for lists and thresholds.\n"
            "Always give doses with units and the route.\n"
            "No mnemonics.")
        self.editor.setPlainText(load_user_skill())
        lay.addWidget(self.editor, 1)
        row = QHBoxLayout()
        self.count = hint_label("")
        clear = link_button("Clear", on_click=self.editor.clear)
        row.addWidget(self.count, 1)
        row.addWidget(clear)
        lay.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                              | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.editor.textChanged.connect(self._refresh_count)
        self._refresh_count()
        self.resize(560, 420)

    def _refresh_count(self):
        n = len(self.editor.toPlainText())
        over = n > ai_logic.USER_SKILL_MAX_CHARS
        self.count.setText(f"{n:,} of {ai_logic.USER_SKILL_MAX_CHARS:,} characters"
                           + (". Too long: trim it before saving." if over else ""))
        self.count.setStyleSheet(
            f"color: {colors()['warning' if over else 'muted']}; font-size: 11px;")

    def text(self):
        return self.editor.toPlainText()

    def accept(self):
        if len(self.editor.toPlainText()) > ai_logic.USER_SKILL_MAX_CHARS:
            return
        super().accept()


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
           "attached" if src.startswith("attached:") else
           "file" if src.startswith("file:") else "other")
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
        if kind == "file":
            # A file the assistant saved in the scratch folder while it had
            # write tools (thorough mode only). name must be a bare basename
            # inside scratch, not a path elsewhere, and must be an .svg file:
            # nothing else is trusted to render safely on a card. The
            # basename check alone doesn't stop a symlink dropped in scratch
            # from pointing outside it, so also reject a symlink outright and
            # require the resolved path to actually stay under scratch.
            name = src.split(":", 1)[1]
            if os.path.basename(name) != name or not name:
                return {"state": "error", "kind": kind,
                        "error": f"invalid file image name: {name!r}"}
            if not name.lower().endswith(".svg"):
                return {"state": "error", "kind": kind,
                        "error": f"file image must be .svg: {name!r}"}
            path = os.path.join(scratch, name)
            if os.path.islink(path):
                return {"state": "error", "kind": kind,
                        "error": f"file image must not be a symlink: {name!r}"}
            real_scratch = os.path.realpath(scratch)
            real_path = os.path.realpath(path)
            if real_path != real_scratch and not real_path.startswith(
                    real_scratch + os.sep):
                return {"state": "error", "kind": kind,
                        "error": f"file image escapes scratch folder: {name!r}"}
            if not os.path.isfile(real_path):
                return {"state": "error", "kind": kind,
                        "error": f"file image not found in scratch: {name!r}"}
            with open(real_path, "rb") as fh:
                data = fh.read()
            return {"state": "ok", "kind": "file", "bytes": data,
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

# The row's decision control: Include or Skip only, no Never (there is nothing to
# remember a draft against; see review._NEW_OPTIONS for the update screen's own set).
_REVIEW_DECISION_OPTIONS = [("include", "Include"), ("skip", "Skip")]

_REVIEW_NOTE_CAPTION = "Skipped. What should change? Sent with the next Revise all."
_REVIEW_NOTE_PLACEHOLDER = "e.g. trim the answer, split into two cards"


def _chip_with_type(kind, note_type, kinds):
    """The row's chip, with its note type as a small tag underneath: the same
    shape review._chip_with_source draws for a card's source reference, adapted
    for a kind/label pair instead of a detail dict (this row's chip kind is
    already computed by _review_row_kind, not derived the way _row_chip does)."""
    cell = chip_cell(kind, kinds)
    wrap = QWidget()
    lay = QVBoxLayout(wrap)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(1)
    lay.addWidget(cell)
    tag = QLabel(html.escape(note_type))
    tag.setAlignment(Qt.AlignmentFlag.AlignCenter)
    tag.setWordWrap(True)
    tag.setStyleSheet(f"color: {colors()['dim']}; font-size: 10px;")
    lay.addWidget(tag)
    wrap.setFixedWidth(chip_column_width(kinds))
    return wrap


def _image_errors(s):
    """{card index: [error message, ...]} for cards with a failed image,
    from the session's resolved s.image_data. Shared by _apply_review_state
    and _accept_correction so both feed ai_logic.mechanical_checks the same
    inputs."""
    image_errors = {i: [r["error"] for r in results if r.get("state") == "error"]
                    for i, results in s.image_data.items()}
    return {i: msgs for i, msgs in image_errors.items() if msgs}


def _review_row_indent():
    """Where the review row's primary text sits, and what its expanded body and
    its reason lines indent by: past the caret and the chip column, each with its
    own gap. The decision control sits at the row's right edge instead, so it
    doesn't add to this."""
    return row_text_indent(1, _REVIEW_CHIP_KINDS)


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


# A Check facts verdict's own label and palette role (see _build_verdict_row):
# "unverified" has no role here since it takes colors()["warning"] directly
# rather than one of the existing "<role>_bg" pairs _accent_row's roles read.
_VERDICT_LABELS = {"confirmed": "Confirmed", "corrected": "Corrected",
                   "unverified": "Unverified"}
_VERDICT_ROLE = {"confirmed": "accept", "corrected": "updated"}


def _card_image_names(card):
    """Names for a card's collapsed-line picture tag (mirrors review._image_text):
    "drawn figure" for an inline SVG, "attached file" for an attachment, "from
    <host>" for a web image. Always lists every picture the card carries, opened
    or not: a picture that resolved and painted in the expanded body still names
    itself here, so the collapsed line stays an honest summary of what the card
    holds rather than only what hasn't been looked at yet. A source
    _resolve_one_image itself doesn't recognize (kind "other") gets no name:
    nothing here can say what it even is."""
    names = []
    for im in card.get("images") or []:
        src = im.get("source", "")
        if src.startswith("url:"):
            names.append(f"from {_url_host(src[4:])}")
        elif src.startswith("svg:"):
            names.append("drawn figure")
        elif src.startswith("attached:"):
            names.append("attached file")
        elif src.startswith("file:"):
            names.append("drawn figure")
    return names


def _card_primary_html(card):
    """The row's bold collapsed line: the front, or a cloze note's text with its
    deletions filled in: the fact under review lives in the deletions, so it is
    shown rather than blanked (mirrors review._primary_html). Ends with a
    picture tag (review._image_text's shape) naming every image the card
    carries."""
    ntype = card["note_type"]
    primary_field = ai_logic.PRIMARY_FIELD.get(ntype, "Front")
    text = field_preview_html(card["fields"].get(primary_field, ""))
    if primary_field == "Text":
        text = cloze_filled_html(text, escape=False)
    names = _card_image_names(card)
    if names:
        tag = html.escape(f"[image: {', '.join(names)}]")
        text = (f'{text}&nbsp;&nbsp;<span style="color: {colors()["dim"]};">'
                f'{tag}</span>' if text else tag)
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


# The chips the input page's own rows can wear, measured as one column so all
# four rows start their text at the same x (see widgets.chip_column_width).
_INPUT_CHIPS = ("ready", "notsetup", "auto", "thorough", "quick", "deck", "skills")

# The chips the progress row can wear, its own set (see chip_column_width): a
# stage word, never one of the input page's or a card row's own vocabulary.
_PROGRESS_CHIPS = ("drafting", "verifying", "reviewing", "working", "checking")

# What a phase event's own text maps to, checked as a case-insensitive
# substring since a phase is a short sentence a backend wrote (see
# ai_logic.parse_stream_event), not a fixed vocabulary this add-on controls.
# Order matters: the first match wins, so "self-review" being listed after
# "verify" doesn't matter here (the two never share a word), but a phase
# naming more than one stage would take the earliest one in this dict.
_PHASE_CHIP_KEYWORDS = {"drafting": "drafting", "verify": "verifying",
                        "online": "verifying", "self-review": "reviewing",
                        "review": "reviewing"}


def _phase_chip(phase_text, check=False):
    """Which of the progress row's chips a phase's own text names.
    Unmatched text (including "Working", the vendor CLIs' own generic tool-use
    label) reads as WORKING, the same catch-all role a plain assistant turn
    already gets. A check run always reads CHECKING regardless of what the
    backend's own phase text says: drafting/verifying/reviewing describe a
    draft's stages, not what a fact-check turn is doing."""
    if check:
        return "checking"
    t = (phase_text or "").lower()
    for needle, kind in _PHASE_CHIP_KEYWORDS.items():
        if needle in t:
            return kind
    return "working"


_TAG_RE = re.compile(r"<[^>]+>")


def _plain(markup):
    """A rich-text label's words with its markup taken out: what a row says,
    rather than how it is marked up. Rows carry HTML so one line can hold a bold
    noun and a muted detail, and a caller asking what the row says should not
    have to know that."""
    return html.unescape(_TAG_RE.sub("", markup or "")).strip()


def _muted(text):
    return f"<span style='color:{colors()['muted']}'>{html.escape(text)}</span>"


_MODE_LABELS = {"thorough": "Thorough: ", "quick": "Quick draft: "}
_TRAILING_TIMING_RE = re.compile(r"\s*\([^()]*\)\s*$")


def _depth_clause(backend, mode):
    """What a given backend actually does at a given depth, in the Cards and
    depth row's own voice: lower-cased, with the mode label and any trailing
    parenthetical timing (e.g. "(up to 15 turns, 1 to 3 min)") stripped, since
    the row already says the mode and states its own time estimate elsewhere.

    Derived from ai_cli.BACKENDS rather than hardcoded here, since only that
    dict actually knows whether a given backend can reach the web in a given
    mode; Codex never can, Antigravity's quick mode still might, and a
    hardcoded clause would keep telling both the same story. With no backend
    detected yet, there is nothing to derive from, so this falls back to the
    spec's own hedge, but only for thorough: quick draft never claims to
    verify anything online regardless of which backend eventually runs it, so
    hedging about one is a promise this mode was never going to keep. Empty
    tells the caller there is nothing to say, not a clause of its own.
    """
    if not backend:
        return "verifies claims online where the backend allows" if mode == "thorough" else ""
    text = ai_cli.BACKENDS[backend]["modes"][mode]
    label = _MODE_LABELS[mode]
    if text.startswith(label):
        text = text[len(label):]
    text = _TRAILING_TIMING_RE.sub("", text)
    return text[:1].lower() + text[1:] if text else text


class _InfoRow(QWidget):
    """One settled decision on the input page: a chip saying where it stands, a
    bold noun, a muted detail underneath, and the links that change it.

    The same shape as ai_setup._BackendRow, deliberately: the wizard and the AI
    Backends window are one flow, and a reader who has just chosen an assistant
    in one should not have to learn a second vocabulary in the other. The chip
    lives in its own fixed-width column, so every row's noun starts at the same
    x whatever word its chip happens to be.

    `chip_kinds` is the set the owning page measures its column against (see
    widgets.chip_column_width): the input page's rows default to their own
    seven-word set, but the progress row (its only user outside the input
    page) passes its own four-word set instead, so its single row isn't
    measured against words it can never show.
    """

    def __init__(self, chip, primary_html, detail="", links=(),
                chip_kinds=_INPUT_CHIPS):
        super().__init__()
        self._chip_kinds = chip_kinds
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 6, CARET_GAP, 6)
        lay.setSpacing(CARET_GAP)

        # The chip is rebuilt in place rather than restyled, since chip_cell
        # owns both the pill's colours and the column's width; the container is
        # what stays put in the layout.
        self.chip_kind = None
        self._chip_box = QWidget()
        self._chip_lay = QHBoxLayout(self._chip_box)
        self._chip_lay.setContentsMargins(0, 0, 0, 0)
        self._chip_lay.setSpacing(0)
        lay.addWidget(self._chip_box, 0, Qt.AlignmentFlag.AlignTop)

        body = QWidget()
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.setSpacing(2)
        self.primary = QLabel(primary_html)
        self.primary.setTextFormat(Qt.TextFormat.RichText)
        self.primary.setWordWrap(True)
        body_lay.addWidget(self.primary)
        # Every wrapped line on this page goes through _wrapped_hint, so the
        # page's own minimum height is the height its text really needs rather
        # than one line per paragraph (see ai_setup._WrappedHint).
        self.detail = _wrapped_hint(detail)
        body_lay.addWidget(self.detail)
        self.body_lay = body_lay
        lay.addWidget(body, 1)

        trailing = QWidget()
        trail_lay = QHBoxLayout(trailing)
        trail_lay.setContentsMargins(0, 0, 0, 0)
        trail_lay.setSpacing(CARET_GAP)
        self.links = {}
        for label, on_click in links:
            button = link_button(label, on_click=on_click)
            self.links[label] = button
            trail_lay.addWidget(button)
        lay.addWidget(trailing, 0, Qt.AlignmentFlag.AlignTop)
        self.set_chip(chip)

    def set_chip(self, kind):
        if kind == self.chip_kind and self._chip_lay.count():
            return
        self.chip_kind = kind
        while self._chip_lay.count():
            item = self._chip_lay.takeAt(0)
            old = item.widget()
            if old is not None:
                # Detached now, not merely hidden and queued: deleteLater only
                # runs on the next event loop pass, and until it does the old
                # pill is still a child of this row, so anything reading the
                # row's chip reads the word it used to say.
                old.setVisible(False)
                old.setParent(None)
                old.deleteLater()
        self._chip_lay.addWidget(chip_cell(kind, self._chip_kinds))

    def set_primary(self, markup):
        self.primary.setText(markup)

    def set_detail(self, text):
        self.detail.setText(text)

    def text(self):
        """Everything this row says, as one string, markup stripped: what a test
        reads instead of walking two labels that are one statement between them."""
        return " ".join(t for t in (_plain(self.primary.text()),
                                    _plain(self.detail.text())) if t)


class _Session:
    """State shared by every page of the wizard."""

    def __init__(self):
        self.backend = None          # kind string
        self.cli_path = None
        self.mode = "thorough"       # or "quick", resolved at Generate time
        self.source = ""
        self.instructions = ""
        self.count = None            # None: the assistant decides, up to the ceiling
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
        # Indices excluded by default purely because they carry an unreviewed
        # image (see _apply_review_state): what _build_review_row's reason
        # line gates on, so a card the learner unchecked herself never gets a
        # reason line explaining a default she didn't hit.
        self.image_gated = set()
        # True when the last revision came back with a different card count than
        # it was sent: the one shape the prompt promises but nothing verifies.
        # See _finish_generation: this disables the per-index diff entirely.
        self.revision_shape_mismatch = False
        self.tokens_last_run = 0
        self.rate_limits = None
        # True only while a Check facts run is in flight or has just landed:
        # _start_generation sets it, _finish_generation routes on it (verdict
        # parsing instead of card parsing), and it decides which retry nudge
        # a malformed reply gets.
        self.check = False
        # {card index: verdict dict} from the last completed check run, empty
        # until Check facts has run once. Cleared whenever a fresh draft or a
        # revision lands (_finish_generation): a verdict is about one card's
        # exact content, and a revision can change what card index i even
        # means, so carrying stale verdicts over would mislabel the new text.
        self.verdicts = {}


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
        # The same width the AI Backends window opens at (ai_setup.py's own
        # open_size): the two carry the same rows, and a row that wraps in one
        # and not the other reads as two different vocabularies.
        # This clamp can ask for a height below the current page's real
        # layout minimum on a small screen; the SetMinimumSize constraint set
        # on `lay` below wins that conflict, so the window opens at whatever
        # the layout actually needs instead of the clamp's number.
        open_w, open_h = 720, 680
        try:
            geo = QApplication.primaryScreen().availableGeometry()
            open_w = min(open_w, geo.width() - 60)
            open_h = min(open_h, geo.height() - 80)
        except Exception:
            pass
        self.resize(max(open_w, 480), open_h)
        self.session = s = _Session()
        self._retried_json = False   # the single-retry budget on malformed model output
        self._reply_chunks = []      # accumulated delta text; reset per _start_generation
        self._image_reason_rows = {}  # {card index: reason-row widget}; set by _rebuild_review
        # Backend kinds with a "Test connection" run currently in flight, from
        # the input page's single Test connection button (the setup page's own
        # per-backend buttons and Re-check now live in the separate AI Backends
        # window, ai_setup.py). Guards against a second click starting a
        # concurrent test that races the first to write the same status label.
        self._testing_kinds = set()
        cfg = _cfg()
        s.deck_name = cfg["export_deck"] + "::" + ai_logic.GENERATED_DECK_LEAF

        self.stack = QStackedWidget()
        lay = QVBoxLayout(self)
        # See ai_setup._AIBackendsDialog's own SetMinimumSize: the wizard's
        # pages wrap _wrapped_hint rows the same way that window's rows do, so
        # they need the same guarantee against opening below their real
        # minimum on first paint.
        lay.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        lay.addWidget(self.stack)

        self.setup_page = self._build_setup()
        self.input_page = self._build_input()
        self.progress_page = self._build_progress()
        self.review_page = self._build_review()
        for page in (self.setup_page, self.input_page,
                    self.progress_page, self.review_page):
            self.stack.addWidget(page)

        self._detect(cfg)

    def showEvent(self, event):
        super().showEvent(event)
        _settle_min_size(self)
        align_field_column(self._advanced_align_rows)
        QTimer.singleShot(0, lambda: (_safe_settle(self), self._safe_realign_advanced()))

    def _safe_realign_advanced(self):
        """align_field_column, for the singleShot(0) call: like _safe_settle, the
        dialog can already be closed (its C++ object deleted) by the time the
        event loop actually runs this."""
        try:
            align_field_column(self._advanced_align_rows)
        except RuntimeError:
            pass

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
        self.configure_btn = QPushButton("Set up an assistant")
        self.configure_btn.clicked.connect(lambda: self._guard(self._open_backends))
        lay.addWidget(self.configure_btn)
        self.setup_status = hint_label("")
        lay.addWidget(self.setup_status)
        lay.addStretch()
        bb = QDialogButtonBox()
        self.close_btn = bb.addButton("Close", QDialogButtonBox.ButtonRole.RejectRole)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        return page

    def _detect(self, cfg):
        s = self.session
        res = ai_cli.detect_backends(cfg)
        s.backend = res["chosen"]
        s.cli_path = res["backends"][s.backend]["path"] if s.backend else None
        self.setup_status.setText(
            f"Ready: {ai_cli.BACKENDS[s.backend]['label']} detected." if s.backend
            else "No enabled assistant detected yet. Configure one, then come back.")
        self.stack.setCurrentWidget(self.input_page if s.backend else self.setup_page)
        # Refreshed either way: the input page can be shown with nothing
        # detected (its own row says so), and Generate's enablement reads the
        # backend as well as the source.
        self._refresh_backend_row()
        self._source_changed()

    def _open_backends(self):
        from .ai_setup import open_ai_backends
        open_ai_backends(self)
        self._detect(_cfg())

    # === input ==================================================================
    def _build_input(self):
        """The input page: what to make cards from, then one row per decision
        the wizard has already made about the run, in the AI Backends window's
        own vocabulary (chip, bold noun, muted detail, trailing links).

        Every one of those decisions has a defensible default, so none of them
        is a control the reader has to answer before generating: the exact
        count, the depth, the note types and the destination deck all live
        behind one Advanced disclosure that opens in place, under the rows it
        overrides, rather than on a page or a dialog of its own. The rows stay
        on screen while it is open, so a change made in there can be read back
        off the row it changed.
        """
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.addWidget(title_label("Generate cards"))
        lay.addWidget(_wrapped_hint(
            "Paste what you are studying. The assistant reads it under the deck's "
            "own authoring rules, decides how many cards it deserves, and shows "
            "you every draft before anything is added."))

        self.source_box = QPlainTextEdit()
        self.source_box.setPlaceholderText(
            "Paste lecture notes, an article excerpt, or a topic outline")
        self.source_box.textChanged.connect(self._source_changed)
        lay.addWidget(self.source_box, 1)

        # Attach, what is attached, and how much material there is: one line,
        # with the character count pushed to the right edge, since it is the
        # number a reader compares against the soft limit rather than reads
        # inline with the filenames.
        self.attach_btn = QPushButton("Attach images or PDFs")
        self.attach_btn.clicked.connect(lambda: self._guard(self._attach))
        self.attach_label = hint_label("No files attached")
        self.char_label = hint_label("0 characters")
        attach_row = QHBoxLayout()
        attach_row.addWidget(self.attach_btn)
        attach_row.addWidget(self.attach_label)
        attach_row.addStretch()
        attach_row.addWidget(self.char_label)
        lay.addLayout(attach_row)

        lay.addWidget(section_rule())
        # One line, not a box: a focus note is a phrase, and a multi-line box
        # invited a second copy of the source material into it.
        self.instructions_box = QLineEdit()
        self.instructions_box.setPlaceholderText(
            'Focus (optional), e.g. "emphasize dosing"')
        lay.addWidget(self.instructions_box)

        lay.addWidget(section_rule())
        self.backend_row = _InfoRow(
            "notsetup", "<b>Backend</b>", "",
            links=(("Test", lambda: self._guard(self._test_backend_connection)),
                   ("Setup", lambda: self._guard(self._open_backends))))
        self.backend_test_btn = self.backend_row.links["Test"]
        self.change_link = self.backend_row.links["Setup"]
        # The test's own answer is a second muted line under the safety
        # sentence rather than folded into it: it is written by a background
        # poll that knows nothing about the rest of the row.
        self.backend_test_status = _wrapped_hint("Not tested yet")
        self.backend_row.body_lay.addWidget(self.backend_test_status)
        lay.addWidget(self.backend_row)

        self.depth_row = _InfoRow(
            "auto", "<b>Cards and depth</b>", "",
            links=(("Advanced", lambda: self._guard(self._toggle_advanced)),))
        self.advanced_link = self.depth_row.links["Advanced"]
        lay.addWidget(self.depth_row)

        self.deck_row = _InfoRow("deck", "<b>Deck</b>", "")
        lay.addWidget(self.deck_row)

        # The trailing link's own initial label already matches whether
        # there's anything to edit yet; _refresh_skills_row keeps it in sync
        # after that (the dict key below only has to name the one it starts as).
        rules_label = "Edit my rules" if load_user_skill().strip() else "Add my rules"
        self.skills_row = _InfoRow(
            "skills", "<b>Skills</b>", "Sent in that order on every run.",
            links=(("View", lambda: self._guard(self._view_skills)),
                   (rules_label, lambda: self._guard(self._edit_user_skill))))
        self.skills_link = self.skills_row.links["View"]
        self.rules_link = self.skills_row.links[rules_label]
        lay.addWidget(self.skills_row)

        self.advanced_rule = section_rule()
        lay.addWidget(self.advanced_rule)
        lay.addWidget(self._build_advanced())
        # Collapsed by default, same as the panel it introduces: two rules
        # stacking at the bottom (this one, then the one above the button box)
        # is only right while there is something of Advanced's own between
        # them to separate.
        self.advanced_rule.setVisible(False)

        lay.addStretch()
        lay.addWidget(section_rule())
        bb = QDialogButtonBox()
        bb.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        self.generate_btn = bb.addButton(
            "Generate", QDialogButtonBox.ButtonRole.AcceptRole)
        self.generate_btn.setEnabled(False)
        self.generate_btn.setDefault(True)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(lambda: self._guard(self._start_generation))
        lay.addWidget(bb)
        self.usage_row = hint_label("")
        lay.addWidget(self.usage_row)

        self._refresh_depth_row()
        self._refresh_deck_row()
        self._refresh_skills_row()
        return page

    def _advanced_label(self, text, col_w):
        """A label in the Advanced grid's own column, sized so the widest label
        in this grid ("Exact number of cards") fits without overflowing into
        the field beside it, the way ai_setup._SettingsPanel's own fixed
        LABEL_W does for its own, shorter labels. Every field still starts at
        the same x: `col_w` is the one width every call in this grid is given.
        """
        lbl = QLabel(text)
        lbl.setMinimumWidth(col_w)
        lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        return lbl

    def _build_advanced(self):
        """Everything the wizard already decided, offered for overriding: the
        exact count, the depth, the note types, and where the cards land.

        Hidden until asked for, and laid out as the same fixed-label-column grid
        the AI Backends window's settings panel uses, so every control in it
        starts at one left edge instead of stepping in and out with its label.
        """
        cfg = _cfg()
        panel = QWidget()
        grid = QGridLayout(panel)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(CARET_GAP)
        grid.setVerticalSpacing(6)
        # LABEL_W (the AI Backends settings panel's own column) is too narrow
        # for this grid's own longest label, "Exact number of cards", which
        # used to overflow into the spinbox beside it. Measured against every
        # label this grid actually shows, not just that one, so the column
        # never wins by coincidence if a shorter grid grows a longer label
        # later.
        labels = (_LABEL_COUNT, _LABEL_DEPTH, _LABEL_TYPES, _LABEL_DECK)
        metrics = QFontMetrics(panel.font())
        col_w = max(LABEL_W, max(metrics.horizontalAdvance(t) for t in labels) + 12)
        grid.setColumnMinimumWidth(0, col_w)
        grid.setColumnStretch(1, 1)

        self.count_spin = QSpinBox()
        # 0 is not "no cards": it is the minimum, and a QSpinBox shows its
        # special value text there instead of the number, which is how this
        # says "auto" without a second checkbox to mean the same thing.
        self.count_spin.setRange(0, ai_logic.AUTO_COUNT_CEILING)
        self.count_spin.setSpecialValueText("auto")
        # Wide enough for "auto" itself plus the spinbox's own up/down arrows,
        # which used to clip that word: a bare font-metrics width only covers
        # the text, not the control drawn around it.
        self.count_spin.setMinimumWidth(
            metrics.horizontalAdvance("auto") + 40)
        self.count_spin.setValue(cfg["ai_default_count"])
        self.count_spin.valueChanged.connect(
            lambda _v: self._guard(self._refresh_depth_row))
        count_box = QWidget()
        count_lay = QHBoxLayout(count_box)
        count_lay.setContentsMargins(0, 0, 0, 0)
        count_lay.setSpacing(CARET_GAP)
        count_lay.addWidget(self.count_spin)
        count_lay.addWidget(hint_label(
            f"blank lets the assistant decide, up to {ai_logic.AUTO_COUNT_CEILING}"), 1)
        grid.addWidget(self._advanced_label(_LABEL_COUNT, col_w), 0, 0)
        grid.addWidget(count_box, 0, 1)

        # The radio carries only the short, stable name; the per-backend
        # sentence (ai_cli.BACKENDS' "modes", set in _refresh_backend_row) wraps
        # underneath. A radio button does not wrap its own text, and that
        # sentence runs 150-200 characters, which used to set the whole
        # dialog's minimum width on every page.
        self.thorough_radio = QRadioButton("Thorough")
        self.quick_radio = QRadioButton("Quick draft")
        for radio in (self.thorough_radio, self.quick_radio):
            radio.setStyleSheet("font-weight: 600;")
        depth = cfg["ai_default_depth"]
        self._syncing_depth = False
        (self.quick_radio if depth == "quick" else self.thorough_radio).setChecked(True)
        # A depth set in config is a decision already taken, so it counts as
        # touched; anything else leaves the material to decide. Connected only
        # after that seeding, so seeding never reads as a click.
        self._depth_touched = depth in ("thorough", "quick")
        for radio in (self.thorough_radio, self.quick_radio):
            radio.toggled.connect(lambda _c: self._guard(self._depth_chosen))
        depth_box = QWidget()
        depth_lay = QHBoxLayout(depth_box)
        depth_lay.setContentsMargins(0, 0, 0, 0)
        depth_lay.setSpacing(CARET_GAP)
        depth_lay.addWidget(self.thorough_radio)
        depth_lay.addWidget(self.quick_radio)
        depth_lay.addStretch()
        grid.addWidget(self._advanced_label(_LABEL_DEPTH, col_w), 1, 0)
        grid.addWidget(depth_box, 1, 1)

        self.thorough_hint = _wrapped_hint("")
        self.quick_hint = _wrapped_hint("")
        modes_box = QWidget()
        modes_lay = QVBoxLayout(modes_box)
        modes_lay.setContentsMargins(0, 0, 0, 0)
        modes_lay.setSpacing(2)
        modes_lay.addWidget(self.thorough_hint)
        modes_lay.addWidget(self.quick_hint)
        grid.addWidget(modes_box, 2, 1)

        self.type_boxes = {}
        types_box = QWidget()
        types_lay = QVBoxLayout(types_box)
        types_lay.setContentsMargins(0, 0, 0, 0)
        types_lay.setSpacing(2)
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
            box.toggled.connect(lambda _c: self._guard(self._refresh_deck_row))
            self.type_boxes[name] = box
            types_lay.addWidget(box)
        grid.addWidget(self._advanced_label(_LABEL_TYPES, col_w), 3, 0,
                      Qt.AlignmentFlag.AlignTop)
        grid.addWidget(types_box, 3, 1)

        self.deck_combo = QComboBox()
        self.deck_combo.setEditable(True)
        self.deck_combo.addItem(self.session.deck_name)
        self.deck_combo.currentTextChanged.connect(
            lambda _t: self._guard(self._refresh_deck_row))
        deck_slot = field_slot(self.deck_combo)
        grid.addWidget(self._advanced_label(_LABEL_DECK, col_w), 4, 0)
        grid.addWidget(deck_slot, 4, 1)

        # Line every field's drawn left edge up on one x, the way ai_setup's own
        # settings grid does (see widgets.align_field_column): a QSpinBox, two
        # QRadioButtons, a column of QCheckBoxes and a QComboBox all sit in this
        # one field column, and macOS's native style insets a combo box's bezel
        # from its geometry by more than it insets the others. Stashed rather
        # than run right here: this panel starts hidden (below), and a hidden
        # widget's children have never actually been laid out on screen, so
        # measuring them now would compensate against provisional geometry.
        # showEvent runs it once real geometry exists, the same reason it also
        # calls _settle_min_size rather than sizing the window right here.
        self._advanced_align_rows = [
            (count_box, self.count_spin),
            (depth_box, self.thorough_radio),
            (types_box, next(iter(self.type_boxes.values()))),
            (deck_slot, self.deck_combo),
        ]

        self.advanced_panel = panel
        panel.setVisible(False)
        return panel

    def _toggle_advanced(self):
        """Open (or close) Advanced. In place, not a page of its own: the rows
        above stay on screen and keep saying what they say, so a reader can see
        what a change in here actually changed."""
        shown = not self.advanced_panel.isVisible()
        self.advanced_panel.setVisible(shown)
        self.advanced_rule.setVisible(shown)
        self.advanced_link.setText("Hide advanced" if shown else "Advanced")

    def _depth_chosen(self):
        """A depth the learner picked outranks the one the material implies, for
        the rest of this session. Never written to config: the wizard reads
        ai_default_depth, it does not set it.

        Ignored while _refresh_depth_row is moving the radios itself: showing
        the reader which depth the material implies must not read as them
        having chosen it."""
        if self._syncing_depth:
            return
        self._depth_touched = True
        self._refresh_depth_row()

    def _resolved_count(self):
        """The exact number to ask for, or None to leave it to the assistant."""
        value = self.count_spin.value()
        return value if value else None

    def _resolved_mode(self):
        """The depth this run will actually use: the learner's own pick if there
        was one, else what the material's own length and attachments imply."""
        if self._depth_touched:
            return "thorough" if self.thorough_radio.isChecked() else "quick"
        return ai_logic.default_mode(len(self.source_box.toPlainText()),
                                     len(self.session.attachments))

    def _refresh_depth_row(self):
        """What the Cards and depth row says, recomputed from everything that
        can change its answer: the count, the radios, the material's length and
        whatever is attached."""
        count = self._resolved_count()
        if count is None:
            said = (f"The assistant decides the count, up to "
                    f"{ai_logic.AUTO_COUNT_CEILING}.")
        else:
            said = f"Exactly {plural(count, 'card')}."
        undecided = (not self.source_box.toPlainText().strip()
                     and not self.session.attachments)
        if self._depth_touched:
            mode = self._resolved_mode()
            why = ("Thorough, your choice" if mode == "thorough"
                   else "Quick, your choice")
        elif undecided:
            mode = "auto"
            why = None
            said += (" Depth follows the source: thorough for longer material or "
                     "attachments, quick otherwise.")
        else:
            mode = self._resolved_mode()
            if mode == "thorough":
                why = ("Thorough because you attached a file"
                       if self.session.attachments
                       else f"Thorough because the source is over "
                            f"{ai_logic.AUTO_DEPTH_CHARS:,} characters")
            else:
                why = "Quick because the source is short"
        if why:
            clause = _depth_clause(self.session.backend, mode)
            said += f" {why}: {clause}." if clause else f" {why}."
        self.depth_row.set_chip(mode)
        self.depth_row.set_detail(said)
        if not self._depth_touched:
            # Advanced has to open on the depth this run would really use, not on
            # a stale one: an untouched radio pair is a readout, and only becomes
            # a choice when the reader moves it.
            self._syncing_depth = True
            try:
                thorough_mode = self._resolved_mode() == "thorough"
                # Both, explicitly: a real QRadioButton unchecks its sibling for
                # us, but saying so is what keeps this readout honest wherever
                # the two are not in one exclusive group.
                self.thorough_radio.setChecked(thorough_mode)
                self.quick_radio.setChecked(not thorough_mode)
            finally:
                self._syncing_depth = False

    def _refresh_deck_row(self):
        """Where accepted cards land, and on which note types."""
        deck = self.deck_combo.currentText().strip() or self.session.deck_name
        chosen = [n for n, b in self.type_boxes.items() if b.isChecked()]
        if len(chosen) > 1:
            types = ", ".join(chosen[:-1]) + " and " + chosen[-1] + "."
        elif chosen:
            types = chosen[0] + "."
        else:
            types = "No note type selected yet: pick one under Advanced."
        # The row's own Change link is gone; Advanced is the one place that
        # changes the deck now, so the detail always says where to find it,
        # once. The no-note-type case already ends on "under Advanced" itself,
        # so it stays a single sentence rather than saying so twice.
        if chosen:
            said = (f"{deck}. Every accepted card lands here, as {types} "
                    "Change it under Advanced.")
        else:
            said = f"{deck}. {types}"
        self.deck_row.set_detail(said)

    def _refresh_skills_row(self):
        """What gets sent on top of the material, in the order it is sent.

        The primary line stays "Skills", the bold noun every other row's
        primary bolds; what is actually sent is the row's detail, same as the
        Deck and Cards-and-depth rows put their own answer in the muted line
        below the noun rather than in the bold line itself."""
        parts = ["Bundled"]
        deck = load_deck_skill()
        if deck and deck.get("enabled"):
            parts.append(f"deck skill v{deck.get('version')}")
        has_rules = bool(load_user_skill().strip())
        if has_rules:
            parts.append("my rules")
        self.skills_row.set_detail(
            f"{', '.join(parts)}. Sent in that order on every run.")
        self.rules_link.setText("Edit my rules" if has_rules else "Add my rules")

    def _source_changed(self):
        text = self.source_box.toPlainText()
        n = len(text)
        warn = "; consider splitting the material" if n > SOFT_SOURCE_LIMIT else ""
        self.char_label.setText(f"{n:,} characters{warn}")
        # Both halves of "can this run at all": material to work from, and an
        # assistant to work through. The button used to go live on the source
        # alone, which offered a Generate that could only fail.
        self.generate_btn.setEnabled(bool(text.strip()) and bool(self.session.backend))
        self._refresh_depth_row()

    def _refresh_backend_row(self):
        """What the Backend row says: which assistant this run goes through, on
        what model and effort, and what it is allowed to do while it runs.

        Also handles having no assistant at all, since this page can be reached
        with none detected: the row then says so and says what to do about it,
        rather than naming a backend that isn't there.
        """
        s = self.session
        if not s.backend:
            # Defensive: _detect routes a backendless dialog straight to the
            # setup page, so only the harness's "unset" scene renders this.
            self.backend_row.set_chip("notsetup")
            self.backend_row.set_primary("<b>Backend</b>")
            self.backend_row.set_detail(
                "No assistant found. Install one, sign in once in a terminal, "
                "then set it up here.")
            self.backend_test_btn.setVisible(False)
            self.backend_test_status.setText("")
            self.thorough_hint.setText("")
            self.quick_hint.setText("")
            self.usage_row.setText("")
            return
        meta = ai_cli.BACKENDS[s.backend]
        cfg = _cfg()
        summary = ai_cli.model_effort_line(
            s.backend, cfg["ai_model"][s.backend], cfg["ai_effort"][s.backend],
            path=s.cli_path)
        self.backend_row.set_chip("ready")
        self.backend_row.set_primary(
            f"<b>Backend:</b> {html.escape(meta['label'])}, "
            f"{_muted(summary)}")
        self.backend_row.set_detail(f"{meta['safety']}.")
        self.backend_test_btn.setVisible(True)
        self.backend_test_status.setText("Not tested yet")
        self.thorough_hint.setText(meta["modes"]["thorough"])
        self.quick_hint.setText(meta["modes"]["quick"])
        reg = load_ai_usage()
        self.usage_row.setText(ai_logic.usage_line(
            reg, s.backend, now=time.time(), free_tier=(s.backend == "agy")))

    def _test_backend_connection(self):
        s = self.session
        if not s.backend or not s.cli_path or s.backend in self._testing_kinds:
            return
        self._testing_kinds.add(s.backend)
        self.backend_test_btn.setEnabled(False)

        def _done():
            self._testing_kinds.discard(s.backend)
            self.backend_test_btn.setEnabled(True)
        self.backend_test_status.setText("Testing connection…")
        # Same off-thread runner the AI Backends window uses; the wizard has one
        # backend in play at a time, so it needs no liveness predicate.
        run_connection_test_async(self, s.backend, s.cli_path,
                                  self.backend_test_status.setText, on_done=_done)

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
        # An attachment is one of the two things the depth default reads, so the
        # row has to re-answer as soon as one lands.
        self._refresh_depth_row()
        # Anki's own bundled Python doesn't carry Pillow, which pypdf needs to
        # decode a PDF's embedded images (its text still comes through fine):
        # say so once per session, right when it happens, rather than leaving
        # a PDF's figures silently missing with no explanation.
        if images_undecoded and not s.pdf_image_warning_shown:
            s.pdf_image_warning_shown = True
            _warn("The text came through, but embedded images in that PDF "
                  "couldn't be read in Anki's own Python. If you want any of "
                  "its figures on a card, attach them separately as image files.")

    def _edit_user_skill(self):
        dlg = _UserSkillDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            save_user_skill(dlg.text())
            self._refresh_skills_row()

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
            user = load_user_skill().strip()
            if user:
                n = user.count("\n") + 1
                parts += ["", f"My rules ({n} line{'s' if n != 1 else ''})", "", user]
            body = _skills_html(parts)
            if not user:
                # Nothing to show, so nothing is claimed under a "My rules"
                # heading either: one muted line pointing at where to add
                # some, instead of a heading whose only content is "none".
                body += (f'<br><br><span style="color:{colors()["muted"]}">'
                         "Add your own rules from the wizard's Skills row.</span>")
            return body

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
        # Consent can have been flipped in there, and the Skills row names what
        # is actually sent, so it has to be re-read rather than left stale. Only
        # when there is one: this method is also reachable on a dialog whose
        # input page was never built.
        # hasattr(), not a raw self.__dict__ poke, but guarded: a real PyQt
        # widget whose __init__ chain never ran (as in the qt_tests harness'
        # own __new__()-built dialogs) raises RuntimeError rather than
        # AttributeError for an attribute Qt itself doesn't know, and hasattr
        # only swallows the latter.
        try:
            has_skills_row = hasattr(self, "skills_row")
        except RuntimeError:
            has_skills_row = False
        if has_skills_row:
            self._refresh_skills_row()

    # === progress ===============================================================
    def _build_progress(self):
        """The progress page: one status row, on the same shape _InfoRow
        already gives the input page's rows, so a reader who has just read
        those doesn't meet a second vocabulary in here. Cancel rides as the
        row's own trailing link rather than a QDialogButtonBox button: it is
        wired straight to _cancel_generation below, the same direct
        connection the old button used, never through self.reject() (see
        that method's own comment for why that path can't be used here), so
        moving it into the row changes nothing about what a click does.
        """
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.addWidget(title_label("Generating cards"))
        self.progress_row = _InfoRow(
            "drafting", "<b>Starting</b>", "",
            links=(("Cancel", self._cancel_generation),),
            chip_kinds=_PROGRESS_CHIPS)
        lay.addWidget(self.progress_row)
        # A muted, auto-scrolled log of what the assistant is doing, newest
        # last: takes the stretch a one-row page otherwise left blank. Never
        # shows reply text, tool arguments, or the prompt (see _poll_worker).
        self.activity_feed = QPlainTextEdit()
        self.activity_feed.setReadOnly(True)
        self.activity_feed.setFrameShape(QFrame.Shape.NoFrame)
        self.activity_feed.setMaximumBlockCount(200)
        self.activity_feed.setStyleSheet(
            f"QPlainTextEdit {{ background: transparent; "
            f"color: {colors()['muted']}; }}")
        lay.addWidget(self.activity_feed, 1)
        return page

    def _start_generation(self, revision=False, extra_error=None, check=False):
        s = self.session
        self._last_revision = revision
        self._last_check = check
        s.check = check
        # extra_error is only set on our own re-entry after a malformed reply
        # (see _finish_generation); any other call is a fresh request, so the
        # one-retry budget resets here rather than in the caller.
        if extra_error is None:
            self._retried_json = False
            if not revision and not check:
                # A genuinely fresh (non-revision, non-check) request: Back-then-
                # Generate can reach here with an unrelated earlier draft still
                # sitting in s.cards, and _finish_generation's revision detection
                # goes only by "is s.cards non-empty": left uncleared, a brand
                # new draft would read as a revision of that stale one and
                # report a bogus "updated N, kept M verbatim" diff against it.
                s.cards, s.included, s.notes = [], [], {}
                s.updated, s.image_data = set(), {}
                s.revision_shape_mismatch = False
        # The depth the learner picked if they picked one, else the one the
        # material's own length and attachments imply (ai_logic.default_mode).
        # A check run always runs Thorough, whatever depth the draft used: the
        # spec calls it "one extra assistant turn, in Thorough mode".
        s.mode = "thorough" if check else self._resolved_mode()
        self._duration_estimate = ai_logic.duration_estimate_line(
            load_ai_usage(), s.backend, s.mode)
        s.source = self.source_box.toPlainText()
        s.instructions = self.instructions_box.text()
        # None means automatic: no number is sent and the assistant makes one
        # card per point the source teaches, up to ai_logic.AUTO_COUNT_CEILING.
        s.count = self._resolved_count()
        s.note_types = [n for n, b in self.type_boxes.items() if b.isChecked()]
        s.deck_name = self.deck_combo.currentText().strip() or s.deck_name
        if s.scratch is None:
            s.scratch = tempfile.mkdtemp(prefix="ip-aigen-")
        extra_text = "\n\n".join(a[1]["text"] for a in s.attachments if a[1]["text"])
        image_names = [name for _, meta in s.attachments for name in meta["images"]]
        user_skill_text = load_user_skill()
        if check:
            prompt = ai_logic.build_check_prompt(
                skills=ai_logic.active_skills(load_deck_skill(), user_skill_text),
                cards=s.cards, field_map=FIELD_MAP, backend=s.backend,
                web=ai_cli.web_capable(s.backend))
        else:
            prompt = ai_logic.build_prompt(
                skills=ai_logic.active_skills(load_deck_skill(), user_skill_text),
                source=(s.source + ("\n\n## Attached document text\n" + extra_text
                                    if extra_text else "")),
                note_types=s.note_types, field_map=FIELD_MAP, count=s.count,
                instructions=s.instructions,
                attachments=image_names if ai_cli.image_capable(s.backend) else [],
                cards=s.cards if revision else None,
                feedback=self.feedback_box.toPlainText() if revision else "",
                notes=s.notes if revision else None,
                checks=s.checks if revision else None, mode=s.mode,
                backend=s.backend, web=ai_cli.web_capable(s.backend))
        if extra_error:
            prompt += ("\n\n## Your previous reply failed validation\n"
                      + "\n".join(extra_error))
        # A deque, not a plain list: the worker (producer) appends and the
        # poller (consumer) pops from the other end, so each event's removal
        # is one atomic op with no gap between "read" and "clear" where a
        # concurrently appended event could be silently dropped.
        self._events = deque()
        self._worker_error, self._worker_result = None, None
        self._reply_chunks = []
        self.activity_feed.clear()
        self._gen_done = False
        self._cancel_flag = threading.Event()
        self._t0 = time.monotonic()
        image_paths = ([os.path.join(s.scratch, n) for n in image_names]
                       if ai_cli.image_capable(s.backend) else [])
        gen_cfg = _cfg()

        def work():
            try:
                self._worker_result = ai_cli.run_generation(
                    s.backend, s.cli_path, prompt, s.mode, s.scratch,
                    image_paths=image_paths, on_event=self._events.append,
                    cancel=self._cancel_flag.is_set,
                    model=gen_cfg["ai_model"][s.backend],
                    effort=gen_cfg["ai_effort"][s.backend],
                    log_path=AI_LAST_RUN_LOG,
                    # Attached document text is part of the source section the
                    # backend sees, so it needs the same echo protection.
                    redact_texts=(s.source, extra_text, s.instructions,
                                  user_skill_text))
            except Exception as e:
                self._worker_error = e

        self._worker = threading.Thread(target=work, daemon=True)
        self._worker.start()
        verb = "Checking facts with " if check else "Drafting cards with "
        self._phase_text = verb + ai_cli.BACKENDS[s.backend]["label"]
        self.progress_row.set_chip("checking" if check else "drafting")
        self.progress_row.set_primary(f"<b>{self._phase_text}</b>")
        self.progress_row.set_detail(self._progress_detail_text())
        self.stack.setCurrentWidget(self.progress_page)
        self._timer = QTimer(self)
        self._timer.timeout.connect(lambda: self._guard_completion(self._poll_worker))
        self._timer.start(200)

    def _progress_detail_text(self):
        """The progress row's muted detail line: "<elapsed> elapsed[, <learned
        estimate>].", using only what is actually known at the moment it's
        called. The current phase isn't repeated here: the row's own bold
        title already carries it (see the sites that set self._phase_text),
        and no per-backend turn count is reported either, since backends emit
        phase events at wildly different rates (Claude only on tool use,
        Antigravity on nearly every internal step, Codex never), so a raw
        count would not mean the same thing across backends. On a revision
        turn, s.cards still holds the pre-revision draft being revised (a
        fresh run clears it first), which is known from the very start,
        unlike anything about the new draft.
        """
        parts = []
        if self.session.cards and not self.session.check:
            parts.append(f"Revising {plural(len(self.session.cards), 'card')}.")
        elif self.session.check:
            parts.append(f"Checking {plural(len(self.session.cards), 'card')}.")
        elapsed = f"{ai_logic.format_duration(int(time.monotonic() - self._t0))} elapsed"
        if self._duration_estimate:
            elapsed += f", {self._duration_estimate}."
        else:
            elapsed += "."
        parts.append(elapsed)
        if self._reply_chunks and not self.session.check:
            text = "".join(self._reply_chunks)
            n = text.count('"note_type"')
            parts.append(f"{plural(n, 'card')} so far, {len(text):,} characters.")
        return " ".join(parts)

    def _append_activity(self, text):
        elapsed = ai_logic.format_duration(time.monotonic() - self._t0)
        self.activity_feed.appendPlainText(f"{elapsed}  {text}")
        bar = self.activity_feed.verticalScrollBar()
        bar.setValue(bar.maximum())

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
                # A phase line only earns a feed line of its own when it
                # actually changed: agy fires "Working" on nearly every
                # internal step, and each of those already gets its own
                # activity line (see the "activity" fan-out in _run_argv).
                if evt["phase"] != self._phase_text:
                    self._append_activity(evt["phase"])
                self._phase_text = evt["phase"]
                self.progress_row.set_chip(
                    _phase_chip(evt["phase"], check=self.session.check))
                # A check run's own title ("Checking facts with <backend>")
                # stays put rather than being overwritten by the backend's raw
                # phase text (e.g. "Verify online"): that vocabulary describes
                # a draft's stages, not a fact-check turn.
                if not self.session.check:
                    self.progress_row.set_primary(f"<b>{self._phase_text}</b>")
            elif evt["type"] == "activity":
                self._append_activity(evt["text"])
            elif evt["type"] == "delta":
                self._reply_chunks.append(evt["text"])
            elif evt["type"] == "rate_limits":
                self.session.rate_limits = evt
        self.progress_row.set_detail(self._progress_detail_text())
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
        if isinstance(err, ai_cli.EmptyReply):
            if not self._retried_json:
                # Same single-retry budget malformed JSON uses (see below):
                # one automatic re-ask per generation, whichever kind of bad
                # reply triggers it.
                self._retried_json = True
                nudge = ("Your previous reply was empty. Do not call any "
                        "tool; the only folder you may read is the scratch "
                        "folder already provided. Reply with the "
                        + ("verdicts JSON now." if s.check else "JSON now."))
                self._start_generation(
                    revision=self._last_revision, check=self._last_check,
                    extra_error=[nudge])
                self._append_activity("Reply was empty, retrying once")
                return
            _warn(f"{err}\n\nThe add-on already retried once. Try again, "
                 "pick a lower-effort model in AI Backends (for "
                 "Antigravity, an id ending in -low), or use another "
                 "assistant. The full stream is in ai_last_run.log inside "
                 "the add-on's user_files folder.")
            self._return_to_input_or_review()
            return
        if err or not res:
            _warn(f"Generation failed: {err}")
            self._return_to_input_or_review()
            return
        if s.check:
            self._finish_check(res)
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
        s.verdicts = {}   # a new draft/revision makes any prior check stale

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

    def _finish_check(self, res):
        """A Check facts run's own completion path: parse verdicts rather than
        cards, then straight back to the review page (no image phase; a check
        never touches images)."""
        s = self.session
        field_map = {i: FIELD_MAP[c["note_type"]] for i, c in enumerate(s.cards)}
        verdicts, errors = ai_logic.parse_verdicts_json(
            res["text"], len(s.cards), field_map, web=ai_cli.web_capable(s.backend))
        if errors:
            if not self._retried_json:
                self._retried_json = True
                self._start_generation(revision=self._last_revision, check=True,
                                       extra_error=errors)
                return
            _warn("The assistant's fact-check reply still could not be used "
                  "after a retry:\n" + "\n".join(errors[:5]))
            self._return_to_input_or_review()
            return
        s.tokens_last_run = res["tokens"]
        reg = ai_logic.record_usage(load_ai_usage(), s.backend, res["tokens"],
                                    now=time.time())
        reg = ai_logic.record_duration(reg, s.backend, s.mode, res["duration_s"])
        save_ai_usage(reg)
        s.verdicts = verdicts
        s.check = False
        self._rebuild_review()
        self.stack.setCurrentWidget(self.review_page)

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
        # The row's detail (phase/elapsed) is left exactly as the CLI phase
        # last set it: this phase has no phase events of its own and the
        # elapsed clock isn't this phase's own, so redrawing it here would
        # show a phase sentence and a clock that both stopped moving rather
        # than true ones.
        self.progress_row.set_chip("working")
        self.progress_row.set_primary("<b>Resolving images</b>")
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
        image_errors = _image_errors(s)
        s.checks = ai_logic.mechanical_checks(
            s.cards, collection.existing_front_map(_cfg()["scope_tag"]),
            image_errors)
        default_included = [
            not any(c["level"] == "block" for c in per) and not s.cards[i]["images"]
            for i, per in enumerate(s.checks)]
        prev_included = self._pending_prev_included
        # Only the cards whose CURRENT inclusion is actually the default computed
        # above (a fresh draft, or one this revision changed) can have been
        # excluded by the image gate; a card kept verbatim from before carries
        # her own earlier decision instead, whatever the fresh default would say.
        s.image_gated = {
            i for i, per in enumerate(s.checks)
            if (prev_included is None or i in s.updated)
            and s.cards[i]["images"] and not any(c["level"] == "block" for c in per)}
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
        or a check (Check facts) both start from an existing draft that
        _finish_generation never touches until the new reply actually parses,
        so on cancel/failure s.cards is still that pre-run draft: send the
        user back to it (with its hand edits, include choices, and queued
        notes intact) rather than the input page, which is otherwise a dead
        end no Back button reaches. A first generation has no draft yet, so
        it still falls back to input."""
        if (self._last_revision or self._last_check) and self.session.cards:
            s = self.session
            s.check = False   # a cancelled/failed check must not linger mid-run
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
        self.check_btn = bb.addButton(
            "Check facts", QDialogButtonBox.ButtonRole.ActionRole)
        self.check_btn.clicked.connect(lambda: self._guard(self._check_facts))
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
        self.decision_cells = []
        self.note_boxes = {}
        self._note_captions = {}
        self._add_note_buttons = {}
        self._image_reason_rows = {}
        for i, card in enumerate(s.cards):
            if i:
                self.cards_lay.addWidget(_separator())
            self.cards_lay.addWidget(self._build_review_row(i, card))
        self.cards_lay.addStretch()   # keeps a short list pinned to the top, not floating
        self._update_review_summary()

    def _build_review_row(self, i, card):
        """One drafted card as a row, on review._card_row's own skeleton: caret,
        chip (with the note type as a small tag underneath, review._chip_with_source's
        own placement), bold front, and a decision control (Include/Skip) at the
        header's right edge. Skip reveals the same 50px note box the update screen
        uses, under the header whether the row is open or not; the back, why, dosing
        and images sit in the body the caret reveals, with Edit and the quiet Add
        note link (shown only while the box is closed) at the end of it.
        """
        s = self.session
        entries = s.checks[i]
        kind = _review_row_kind(entries, i in s.updated)
        indent = _review_row_indent()
        card_label = note_display_label(list(card["fields"].values()), max_len=60)

        row = QWidget()
        outer = QVBoxLayout(row)
        outer.setContentsMargins(0, 5, 0, 6)
        outer.setSpacing(4)

        body = QWidget()
        caret = QPushButton(_CARET_CLOSED)

        def _name_caret(expanded):
            verb = "Hide card" if expanded else "Show card"
            caret.setAccessibleName(f"{verb}: {card_label}")
            caret.setToolTip(verb)

        def _toggle():
            expanded = not body.isVisible()
            body.setVisible(expanded)
            caret.setText(_CARET_OPEN if expanded else _CARET_CLOSED)
            _name_caret(expanded)

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
        _name_caret(False)
        hlay.addWidget(caret, 0, Qt.AlignmentFlag.AlignTop)

        hlay.addWidget(_chip_with_type(kind, card["note_type"], _REVIEW_CHIP_KINDS),
                       0, Qt.AlignmentFlag.AlignTop)

        # The primary line opens the row too (review._ClickableLabel), same as
        # the update screen: the click target is the text a reader is already
        # looking at, not just the small caret beside it.
        primary = _ClickableLabel(
            _preview_style() + _card_primary_html(card), _toggle)
        primary.setWordWrap(True)
        primary.setTextFormat(Qt.TextFormat.RichText)
        primary.setOpenExternalLinks(False)
        primary.setCursor(Qt.CursorShape.PointingHandCursor)
        hlay.addWidget(primary, 1)

        # The note box and its caption: built for every row and appended after the
        # body below, so they read whether the row is open or not (review._card_row's
        # own caption/box placement). Pre-filled and shown when a note already exists
        # (a queued note surviving a rebuild); otherwise revealed by Skip or Add note.
        # Wrapped in their own container rather than added straight to `outer`, so
        # they can carry the same left indent as the body (`blay`'s own margins,
        # below) and stop at the same right edge as the decision cell instead of
        # running the row's full width flush with the page's left edge.
        caption = muted_label(_REVIEW_NOTE_CAPTION)
        note_box = QPlainTextEdit(s.notes.get(i, ""))
        note_box.setPlaceholderText(_REVIEW_NOTE_PLACEHOLDER)
        note_box.setFixedHeight(50)
        has_note = bool(s.notes.get(i))
        caption.setVisible(has_note)
        note_box.setVisible(has_note)
        self.note_boxes[i] = note_box
        self._note_captions[i] = caption

        note_area = QWidget()
        note_lay = QVBoxLayout(note_area)
        note_lay.setContentsMargins(indent, 0, 0, 0)
        note_lay.setSpacing(4)
        note_lay.addWidget(caption)
        note_lay.addWidget(note_box)

        def _on_note_changed(idx=i, nb=note_box):
            text = nb.toPlainText().strip()
            if text:
                s.notes[idx] = text
            else:
                s.notes.pop(idx, None)
            self._update_review_summary()
        note_box.textChanged.connect(_on_note_changed)

        add_note = link_button(
            "Add note", on_click=lambda: self._guard(self._reveal_review_note, i))
        add_note.setVisible(not has_note)
        self._add_note_buttons[i] = add_note

        hlay.addStretch()
        initial = "include" if s.included[i] else "skip"
        cell = decision_cell(
            _REVIEW_DECISION_OPTIONS, initial,
            lambda v, idx=i: self._guard(self._on_review_decision, idx, v), card_label)
        self.decision_cells.append(cell)
        hlay.addWidget(cell, 0, Qt.AlignmentFlag.AlignTop)

        outer.addWidget(header)

        for entry in entries:
            if entry.get("level") != "ok":
                outer.addWidget(_check_reason_row(entry, indent))
        if i in s.image_gated:
            # Built whenever the gate applies to this card at all (that
            # membership is fixed for this render), visibility tracks the
            # decision control live: _on_review_decision shows/hides this exact
            # widget rather than requiring a full rebuild to catch up.
            reason_row = _check_reason_row(
                {"level": "warn", "message": "Has a picture: open the row to "
                                             "check it before including."}, indent)
            reason_row.setVisible(not s.included[i])
            self._image_reason_rows[i] = reason_row
            outer.addWidget(reason_row)

        verdict = s.verdicts.get(i)
        if verdict:
            outer.addWidget(self._build_verdict_row(i, verdict, indent))

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
        links = QWidget()
        llay = QHBoxLayout(links)
        llay.setContentsMargins(0, 0, 0, 0)
        llay.setSpacing(CARET_GAP)
        llay.addWidget(edit_btn)
        llay.addWidget(add_note)
        llay.addStretch()
        blay.addWidget(links)

        outer.addWidget(body)
        outer.addWidget(note_area)
        return row

    def _on_review_decision(self, i, state):
        """A card's decision control changed: write the new state and refresh what
        depends on it, without a full _rebuild_review (a decision doesn't change any
        card's text, badges, or note, so nothing else on the page needs to move).

        Skip reveals the note box (its text is what the next Revise all sends as
        this card's note); the box stays open if it already carries text, whichever
        way the control moves. A picture-gated row's reason line clears once the
        decision moves back to Include.
        """
        s = self.session
        s.included[i] = state == "include"
        reason_row = self._image_reason_rows.get(i)
        if reason_row is not None:
            reason_row.setVisible(state == "skip")
        box = self.note_boxes.get(i)
        if box is not None:
            show_box = bool(box.toPlainText().strip()) or state == "skip"
            box.setVisible(show_box)
            caption = self._note_captions.get(i)
            if caption is not None:
                caption.setVisible(show_box)
            add_note = self._add_note_buttons.get(i)
            if add_note is not None:
                add_note.setVisible(not show_box)
        self._update_review_summary()

    def _reveal_review_note(self, i):
        """Add note (in the body) opens the same box Skip reveals under the
        header, whatever the row's current decision."""
        box = self.note_boxes.get(i)
        if box is not None:
            box.setVisible(True)
        caption = self._note_captions.get(i)
        if caption is not None:
            caption.setVisible(True)
        add_note = self._add_note_buttons.get(i)
        if add_note is not None:
            add_note.setVisible(False)

    def _update_review_summary(self):
        """Recompute the review header, the run-level footer, and the two button
        labels from current session state. Called after a full _rebuild_review,
        and on its own by _on_review_decision and a note box's textChanged, which
        need exactly this and nothing more.

        Split in two, the way the update screen separates what the reader is
        deciding from run-level facts (review.py:1238-1246): the header under
        the title stays what she's actually choosing between (how many cards,
        how many included vs. skipped), and everything about the run itself
        (token spend, the rate-limit window, the revision diff) moves to a
        small-print footer under the list, rather than one line carrying both.
        """
        s = self.session
        n_inc = sum(s.included)
        n_skip = len(s.cards) - n_inc
        header = f"{plural(len(s.cards), 'card')} drafted"
        if s.attachments:
            # K = every attachment plus the pasted source itself, which is
            # always the first source whether or not there's any text in it.
            header += f" from {len(s.attachments) + 1} sources"
        header += f" · {n_inc} included, {n_skip} skipped"
        if s.verdicts:
            counts = Counter(v["verdict"] for v in s.verdicts.values())
            bits = [f"{counts[k]} {k}" for k in ai_logic._VERDICT_WORDS if counts.get(k)]
            if bits:
                header += " · " + ", ".join(bits)
        self.review_header.setText(header)

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
        self.check_btn.setEnabled(bool(s.cards))

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

    def _build_verdict_row(self, i, verdict, indent):
        """A Check facts verdict, on _check_reason_row's own shape (an accent
        bar plus one line of italic text): Confirmed in the ok colour,
        Corrected in the revised colour, Unverified in the warning colour.
        Source titles ride after the note as real links (opened through
        ai_setup._open_url, the same path the AI Backends window's install
        links use), the URL itself as each link's tooltip. A Corrected
        verdict still carrying its proposed text also gets an Accept/Keep
        mine block underneath."""
        kind = verdict["verdict"]
        label = _VERDICT_LABELS.get(kind, "Unverified")
        if kind == "corrected" and verdict.get("kept_yours"):
            label += " (kept yours)"
        msg = html.escape(f"{label}: {verdict.get('note', '')}")
        sources = verdict.get("sources") or []
        if sources:
            links = []
            for src in sources:
                url = html.escape(src.get("url", ""), quote=True)
                title = html.escape(src.get("title") or src.get("url") or "source")
                links.append(f'<a href="{url}" title="{url}">{title}</a>')
            msg += " &middot; " + ", ".join(links)

        row = QWidget()
        outer = QVBoxLayout(row)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        line = QWidget()
        llay = QHBoxLayout(line)
        llay.setContentsMargins(indent, 0, 0, 0)
        llay.setSpacing(0)
        label_widget = muted_label(f"<i>{msg}</i>")
        label_widget.setTextFormat(Qt.TextFormat.RichText)
        label_widget.setOpenExternalLinks(False)
        label_widget.linkActivated.connect(_open_url)
        c = colors()
        if kind == "unverified":
            bar = c["warning"]
        else:
            bar = c[_VERDICT_ROLE.get(kind, "updated") + "_bg"]
        # The border-none reset is load-bearing: Qt drops a lone border-left on a
        # QLabel unless the border shorthand is cleared first (see _accent_row).
        label_widget.setStyleSheet(
            f"border: none; border-left: 3px solid {bar}; padding-left: 8px;"
            f" color: {c['muted']};")
        llay.addWidget(label_widget)
        outer.addWidget(line)

        if kind == "corrected" and verdict.get("correction"):
            outer.addWidget(self._build_correction_block(i, verdict, indent))
        return row

    def _build_correction_block(self, i, verdict, indent):
        """The proposed field text under the card's current text, with Accept
        and Keep mine. Only shown while the verdict still carries a
        correction; both links clear it, one by applying it first."""
        card = self.session.cards[i]
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(indent + 8, 0, 0, 0)
        lay.setSpacing(2)
        updated_fg = colors()["updated_fg"]
        for field, new_value in verdict["correction"].items():
            current = field_preview_html(card["fields"].get(field, ""))
            proposed = field_preview_html(new_value)
            lay.addWidget(_rich_label(
                f"<b>{html.escape(field)}</b>: {current} &rarr; "
                f"<span style='color:{updated_fg}'>{proposed}</span>"))
        links = QWidget()
        llay = QHBoxLayout(links)
        llay.setContentsMargins(0, 0, 0, 0)
        llay.setSpacing(CARET_GAP)
        llay.addWidget(link_button(
            "Accept", on_click=lambda: self._guard(self._accept_correction, i)))
        llay.addWidget(link_button(
            "Keep mine", on_click=lambda: self._guard(self._keep_correction, i)))
        llay.addStretch()
        lay.addWidget(links)
        return box

    def _accept_correction(self, i):
        """Write the proposed fields into the card in place, mark it updated
        (the same set a revision's own diff uses), and drop the correction so
        the row no longer offers it. Then recompute mechanical checks the same
        way _apply_review_state does (same duplicate map, same image errors):
        an accepted correction can turn a card's front into a duplicate of one
        already in her collection, and a card that now carries a block is
        forced to Skip rather than trusting whatever decision was made before
        the correction changed its fields. A card that stays clean keeps
        whatever she'd already chosen."""
        s = self.session
        verdict = s.verdicts.get(i)
        if not verdict or not verdict.get("correction"):
            return
        s.cards[i]["fields"].update(verdict["correction"])
        s.updated.add(i)
        verdict["correction"] = None
        s.checks = ai_logic.mechanical_checks(
            s.cards, collection.existing_front_map(_cfg()["scope_tag"]),
            _image_errors(s))
        if any(c["level"] == "block" for c in s.checks[i]):
            s.included[i] = False
        self._rebuild_review()

    def _keep_correction(self, i):
        """Drop the proposed correction without touching the card; the
        verdict line stays, worded as kept rather than pending."""
        s = self.session
        verdict = s.verdicts.get(i)
        if not verdict:
            return
        verdict["correction"] = None
        verdict["kept_yours"] = True
        self._rebuild_review()

    def _revise_all(self):
        """The only path that sends card-level feedback to the model: one CLI
        turn carrying the set-level feedback plus every queued note, with
        every un-noted card marked keep-verbatim (see ai_logic.build_prompt)."""
        self._start_generation(revision=True)

    def _check_facts(self):
        """One extra assistant turn that asks for a verdict per drafted card
        rather than a revised set (see ai_logic.build_check_prompt)."""
        self._start_generation(check=True)

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
        from 0 again. A url:/attached:/file: image can't collide with an svg:
        one either, since a url: filename always carries a raster extension
        and an attached/file name is scoped to its own scratch dir.
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
                else:  # attached or file
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

    def keyPressEvent(self, event):
        """On the progress page, Escape must take the same path as the
        Cancel link (see _build_progress's own comment on why that link
        calls _cancel_generation directly rather than reject()): a run in
        flight keeps running and the page stays up until it actually stops.
        Left to QDialog's default routing, Escape reaches reject() instead,
        which asks its own "discard this run" confirm and, once answered,
        closes the whole dialog. That is a materially bigger, more
        destructive action than the Cancel link takes for the identical
        keypress, so this override sends Escape through the same path
        instead."""
        if (event.key() == Qt.Key.Key_Escape
                and self.stack.currentWidget() is self.progress_page):
            self._cancel_generation()
            return
        super().keyPressEvent(event)

    def reject(self):
        """Closing mid-review discards everything unsaved: nothing about a
        draft, a note, or a prompt is ever written to disk (see the module
        docstring), so this confirmation is the only chance to back out.

        Qt routes Escape and the window's close box through this same method
        (QDialog's own closeEvent calls reject()), except on the progress page,
        where keyPressEvent intercepts Escape above and runs the cancel path
        instead of ever reaching here. So this is the one place that has to
        handle closing mid-generation too, for the close box's own path: a
        run in flight is real, billed work, not something to silently throw
        away on a stray Escape, so it gets the same kind of confirm as
        discarding a draft. Once confirmed, though, the actual cancel is
        handed off (see _cancel_running_generation) rather than blocking this
        call on however long the subprocess takes to die."""
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
