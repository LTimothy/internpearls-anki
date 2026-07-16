"""The new-card review dialog, and the feedback digest it produces.

Its own module rather than part of dialogs.py because dialogs.py imports sync.py (for
Manage decks' manifest fetch and the Update my decks action), and this is opened *from*
sync.py's update flow, so living in dialogs.py would close that import into a cycle.

Presentation only, in both directions: it reads note fields that sync.py already pulled
out of a downloaded .apkg, and hands back what the learner typed. Nothing here touches
the collection or the network, which is also why the dialog has no Cancel.
"""
import datetime
import html

from aqt import mw
from aqt.qt import (QDialog, QDialogButtonBox, QFontDatabase, QFrame, QHBoxLayout,
                    QLabel, QPlainTextEdit, QPushButton, QScrollArea, Qt,
                    QVBoxLayout, QWidget)

from .config import ADDON_VERSION, APP_NAME, _cfg
from .logic import build_feedback_digest, cloze_filled_html, field_preview_text
from .ui import (copy_to_clipboard, hint_label, muted_label, section_label,
                 title_label)

# The learner's own annotation space, left empty by every spec on purpose. Showing it
# would be a blank row on every single card.
_SKIP_FIELDS = {"Notes"}

# Fields with their own dedicated treatment below rather than being read as generic
# prompt/answer content: Why gets the green rule, Dosing gets its label, Tag becomes
# the row's dim header text, and Image is named rather than rendered (see
# field_preview_text) and folded into the primary or answer line instead of shown on
# its own (see _image_text).
_STRUCTURAL_FIELDS = {"Why", "Dosing", "Tag", "Image"}

# Matches the deck's own CSS so review looks like study: the same green why rule,
# grey dosing block, and blue cloze fill.
_WHY_RULE = "#2e6b3e"
# A hardcoded background needs a hardcoded foreground with it: text colour otherwise
# comes from the platform palette, which flips white under Night Mode while this
# block stays light. See "Colors" in README.md.
_DOSING_BG = "#eef2f7"
_DOSING_FG = "#334155"
_CLOZE_COLOR = "#2563eb"
_CLOZE_STYLE = f"<style>.cloze {{ color: {_CLOZE_COLOR}; font-weight: 600; }}</style>"

_DIM = "#8a9aa2"        # the tag lead-in and the caret
_ROW_RULE = "#d6d6d6"   # the hairline between two cards

_CARET_CLOSED = "▸"
_CARET_OPEN = "▾"

# The caret's width plus its gap to the text. The expanded body indents by exactly
# this, so the answer lines up under the primary line rather than under the caret.
_CARET_W = 14
_CARET_GAP = 6


class _ClickableLabel(QLabel):
    """A QLabel that also toggles its row open, so the click target is the text a
    reader is already looking at, not just the small caret next to it."""

    def __init__(self, text, on_click):
        super().__init__(text)
        self._on_click = on_click

    def mousePressEvent(self, event):
        self._on_click()


def _field(detail, name):
    return next((v for n, v in detail.get("fields", []) if n == name), "")


def _is_cloze(detail):
    return "cloze" in (detail.get("notetype") or "").lower()


def _is_image_note(detail):
    return "image" in (detail.get("notetype") or "").lower()


def _content_fields(detail):
    """The fields that carry the card's prompt/answer, in the note type's own field
    order, once Notes and the structural fields are out of the way. Basic notes reduce
    to (Front, Back); image notes reduce to (Prompt, Answer)."""
    return [(n, v) for n, v in detail.get("fields", [])
            if n not in _SKIP_FIELDS and n not in _STRUCTURAL_FIELDS]


def _image_text(detail):
    """The card's Image field, named rather than rendered (field_preview_text again:
    the review dialog never extracts .apkg media, so an <img> tag would paint broken).
    Empty when the note type has no Image field or the card doesn't use one."""
    return field_preview_text(_field(detail, "Image"))


def _primary_html(detail):
    """The card's collapsed-row line, always its primary line whatever the note type:
    a cloze note's text with its deletions filled in (the fact under review lives in
    the deletions, so it's shown rather than blanked), otherwise the prompt field. For
    an image note the picture is the question itself, so its name is folded in here
    too: without it, a generic prompt gives no way to tell which image it's about.

    A cloze field is run through field_preview_text before cloze_filled_html, since a
    real cloze Text field carries its own HTML (inline images, br, entities), not the
    plain text cloze_filled_html's escaping assumes. field_preview_text strips that down
    to plain text with any inline image named, without touching {{c1::...}} markup, and
    cloze_filled_html escapes and fills deletions from there same as always.
    """
    if _is_cloze(detail):
        text = field_preview_text(_field(detail, "Text"))
        return cloze_filled_html(text)
    fields = _content_fields(detail)
    text = field_preview_text(fields[0][1]) if fields else ""
    if _is_image_note(detail):
        image_text = _image_text(detail)
        if image_text:
            text = f"{image_text} {text}".strip() if text else image_text
    return html.escape(text)


def _answer_html(detail):
    """The card's answer, shown only once its row is expanded. A cloze note has no
    answer text of its own here, since cloze_filled_html already put the answer on the
    collapsed line, but its optional Image field (separate from any image inline in
    Text) still needs naming somewhere, so it goes here too. A non-image note's
    optional Image field (a basic card with a picture on its back, say) is named and
    folded in here, alongside the answer it illustrates."""
    if _is_cloze(detail):
        image_text = _image_text(detail)
        return html.escape(image_text) if image_text else ""
    fields = _content_fields(detail)
    answer = field_preview_text(fields[1][1]) if len(fields) >= 2 else ""
    if not _is_image_note(detail):
        image_text = _image_text(detail)
        if image_text:
            answer = f"{answer} {image_text}".strip() if answer else image_text
    return html.escape(answer) if answer else ""


def _row_html(detail):
    """A collapsed row's whole line: the card's tag, then its primary line.

    One rich-text paragraph rather than a tag widget beside a text widget. Two widgets
    start each row's text at a different x depending on whether that card happens to
    carry a tag, and wrap it against the tag's edge instead of the row's.
    """
    primary = _primary_html(detail)
    tag_text = field_preview_text(_field(detail, "Tag"))
    if tag_text:
        tag = html.escape(tag_text)
        primary = f'<span style="color: {_DIM};">{tag}</span>&nbsp;&nbsp;{primary}'
    return _CLOZE_STYLE + primary


def _rich_label(text):
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setTextFormat(Qt.TextFormat.RichText)
    return lbl


def _separator():
    """The hairline between two cards.

    A real HLine, not a border-bottom on the row: Qt won't paint a lone border-bottom
    on a plain container widget, and a selector-less stylesheet on the row propagates
    into its children, which each draw their own rule.
    """
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFrameShadow(QFrame.Shadow.Plain)
    line.setFixedHeight(1)
    line.setStyleSheet(f"color: {_ROW_RULE};")
    return line


def _card_row(detail, flags, boxes, collect_feedback):
    """One card as a single row: a caret, its tag if it has one, and its primary
    line. Clicking the row (the caret or the line itself) reveals the answer, the why
    behind a green left rule, and dosing when present, plus, only when feedback
    collection is on, a box for what the learner makes of it.
    """
    guid = detail["guid"]
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
    hlay.setSpacing(_CARET_GAP)

    caret.setFlat(True)
    # Unconstrained, this is a real push button at its platform minimum (~80px on
    # macOS) around a 6px glyph, which is a wide dead gutter down the whole list.
    caret.setFixedWidth(_CARET_W)
    caret.setStyleSheet(f"border: none; padding: 0; color: {_DIM};")
    caret.setCursor(Qt.CursorShape.PointingHandCursor)
    caret.clicked.connect(_toggle)
    hlay.addWidget(caret, 0, Qt.AlignmentFlag.AlignTop)

    primary = _ClickableLabel(_row_html(detail), _toggle)
    primary.setWordWrap(True)
    primary.setTextFormat(Qt.TextFormat.RichText)
    primary.setCursor(Qt.CursorShape.PointingHandCursor)
    hlay.addWidget(primary, 1)
    outer.addWidget(header)

    body.setVisible(False)
    blay = QVBoxLayout(body)
    blay.setContentsMargins(_CARET_W + _CARET_GAP, 2, 0, 2)
    blay.setSpacing(4)

    answer_html = _answer_html(detail)
    if answer_html:
        blay.addWidget(_rich_label(answer_html))

    why_text = field_preview_text(_field(detail, "Why"))
    if why_text:
        why_label = _rich_label(html.escape(why_text))
        # The `border: none` reset is load-bearing: Qt ignores a lone border-left on a
        # QLabel unless the shorthand is set first, so without it the padding applies
        # and the rule itself silently never paints.
        why_label.setStyleSheet(f"border: none; border-left: 3px solid {_WHY_RULE};"
                                f" padding-left: 8px; color: {_WHY_RULE};")
        blay.addWidget(why_label)

    dosing_text = field_preview_text(_field(detail, "Dosing"))
    if dosing_text:
        dosing_label = _rich_label(f"<b>Dosing</b> &nbsp;{html.escape(dosing_text)}")
        dosing_label.setStyleSheet(f"background: {_DOSING_BG}; color: {_DOSING_FG};"
                                   f" padding: 6px; border-radius: 4px;")
        blay.addWidget(dosing_label)

    if collect_feedback:
        box = QPlainTextEdit(flags.get(guid, ""))
        box.setPlaceholderText("Anything wrong with this card? (optional)")
        box.setFixedHeight(50)
        blay.addWidget(box)
        boxes[guid] = box

    outer.addWidget(body)
    return row


def review_new_cards(parent, decks, flags):
    """Show every card this update would add, as one row each, and collect notes on
    them when the feedback toggle asks for it.

    `decks` is [(deck_name, [detail, ...])], each detail as apkg_note_details returns it.
    `flags` is {guid: note text}: read to prefill the boxes and rewritten in place on
    close, so closing and reopening shows what she already wrote instead of quietly
    dropping it. With feedback collection off, no boxes are ever created, so this is a
    read-only preview and `flags` comes back untouched.

    A card is flagged by writing something about it. A checkbox on top of a note box
    would be two ways to say one thing, and a flag with no note ("this card is wrong",
    but not how) isn't actionable enough to be worth collecting.

    There's no Cancel button, and every exit path keeps what she typed: nothing in this
    dialog changes anything, so the only thing a Cancel could throw away is her own
    work, which is never what she'd mean by it.
    """
    collect_feedback = _cfg()["collect_feedback"]
    dlg = QDialog(parent or mw)
    dlg.setWindowTitle(f"{APP_NAME}: new cards")
    dlg.setMinimumWidth(560)
    dlg.setMinimumHeight(520)
    outer = QVBoxLayout(dlg)

    total = sum(len(details) for _, details in decks)
    outer.addWidget(title_label(f"{total} new card(s)"))
    hint = "Nothing is added until you choose Update. Click a card to open it."
    if collect_feedback:
        hint += " Say what's wrong with one and you'll get a summary to send back."
    outer.addWidget(hint_label(hint))

    inner = QWidget()
    ilay = QVBoxLayout(inner)
    ilay.setContentsMargins(0, 0, 0, 0)
    ilay.setSpacing(0)
    boxes = {}
    for deck_name, details in decks:
        if not details:
            continue
        ilay.addWidget(section_label(deck_name.split("::")[-1], top_margin=14))
        for i, detail in enumerate(details):
            if i:
                ilay.addWidget(_separator())   # between cards, not after the last
            ilay.addWidget(_card_row(detail, flags, boxes, collect_feedback))
    ilay.addStretch(1)

    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setWidget(inner)
    outer.addWidget(scroll, 1)

    bb = QDialogButtonBox()
    done = bb.addButton("Done", QDialogButtonBox.ButtonRole.AcceptRole)
    done.clicked.connect(dlg.accept)
    outer.addWidget(bb)

    dlg.exec()

    for guid, box in boxes.items():
        note = box.toPlainText().strip()
        if note:
            flags[guid] = note
        else:
            flags.pop(guid, None)   # she cleared it; treat that as unflagging
    return flags


def offer_feedback_digest(parent, entries):
    """Put the flagged-card summary on the clipboard and show it.

    Shown as well as copied, for two reasons: she sees exactly what's being sent before
    she sends it, and a clipboard that silently didn't take (a mocked or headless Qt)
    costs a manual select-and-copy instead of costing her the notes she wrote. Read-only
    and scrollable rather than an _info box, since this text is meant to be selected and
    can run past a message box's height with nothing to grab. Monospaced and styled as a
    payload block, since it's indent-structured plain text, not prose. Copy again is the
    recovery if something else lands on the clipboard before she gets to paste.
    """
    text = build_feedback_digest(entries, version=ADDON_VERSION,
                                 date=datetime.date.today().isoformat())
    if not text:
        return
    copied = copy_to_clipboard(text)

    dlg = QDialog(parent or mw)
    dlg.setWindowTitle(f"{APP_NAME}: card feedback")
    dlg.setMinimumWidth(520)
    dlg.setMinimumHeight(380)
    lay = QVBoxLayout(dlg)
    lay.addWidget(title_label(f"{len(entries)} card(s) flagged"))
    lay.addWidget(muted_label(
        "Copied to your clipboard, ready to paste into a message."
        if copied else
        "Select and copy the text below to send it."))
    view = QPlainTextEdit(text)
    view.setReadOnly(True)
    view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
    view.setStyleSheet(
        "QPlainTextEdit { background: #f6f7f8; border: 1px solid #d8dee4; }")
    lay.addWidget(view, 1)
    bb = QDialogButtonBox()
    again = bb.addButton("Copy again", QDialogButtonBox.ButtonRole.ActionRole)
    again.clicked.connect(lambda: copy_to_clipboard(text))
    close = bb.addButton("Close", QDialogButtonBox.ButtonRole.AcceptRole)
    close.clicked.connect(dlg.accept)
    lay.addWidget(bb)
    dlg.exec()
