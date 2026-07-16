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
from .ui import copy_to_clipboard, muted_label, section_label, title_label

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
_DOSING_BG = "#eef2f7"
_CLOZE_COLOR = "#2563eb"
_CLOZE_STYLE = f"<style>.cloze {{ color: {_CLOZE_COLOR}; font-weight: 600; }}</style>"

_CARET_CLOSED = "▸"
_CARET_OPEN = "▾"


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
        return _CLOZE_STYLE + cloze_filled_html(text)
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


def _rich_label(text):
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setTextFormat(Qt.TextFormat.RichText)
    return lbl


def _card_row(detail, flags, boxes, collect_feedback):
    """One card as a single row: a caret, its tag if it has one, and its primary
    line. Clicking the row (the caret or the line itself) reveals the answer, the why
    behind a green left rule, and dosing when present, plus, only when feedback
    collection is on, a box for what the learner makes of it.
    """
    guid = detail["guid"]
    row = QWidget()
    row.setStyleSheet("border-bottom: 1px solid #e2e2e2;")
    outer = QVBoxLayout(row)
    outer.setSpacing(2)

    body = QWidget()
    caret = QPushButton(_CARET_CLOSED)

    def _toggle():
        expanded = not body.isVisible()
        body.setVisible(expanded)
        caret.setText(_CARET_OPEN if expanded else _CARET_CLOSED)

    header = QWidget()
    hlay = QHBoxLayout(header)
    hlay.setSpacing(6)

    caret.setFlat(True)
    caret.clicked.connect(_toggle)
    hlay.addWidget(caret)

    tag_text = field_preview_text(_field(detail, "Tag"))
    if tag_text:
        tag_label = QLabel(html.escape(tag_text))
        tag_label.setStyleSheet("color: gray; font-size: 11px;")
        hlay.addWidget(tag_label)

    primary = _ClickableLabel(_primary_html(detail), _toggle)
    primary.setWordWrap(True)
    primary.setTextFormat(Qt.TextFormat.RichText)
    hlay.addWidget(primary, 1)
    outer.addWidget(header)

    body.setVisible(False)
    blay = QVBoxLayout(body)
    blay.setSpacing(4)

    answer_html = _answer_html(detail)
    if answer_html:
        blay.addWidget(_rich_label(answer_html))

    why_text = field_preview_text(_field(detail, "Why"))
    if why_text:
        why_label = _rich_label(html.escape(why_text))
        why_label.setStyleSheet(
            f"border-left: 3px solid {_WHY_RULE}; padding-left: 8px; color: {_WHY_RULE};")
        blay.addWidget(why_label)

    dosing_text = field_preview_text(_field(detail, "Dosing"))
    if dosing_text:
        dosing_label = _rich_label(f"<b>Dosing</b> &nbsp;{html.escape(dosing_text)}")
        dosing_label.setStyleSheet(
            f"background: {_DOSING_BG}; padding: 6px; border-radius: 4px;")
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
    if collect_feedback:
        hint = ("Click a card to see its answer and the reasoning behind it. These "
                "aren't in your collection yet, and nothing is added until you choose "
                "Update. If a card looks wrong, or reads as more than one fact at "
                "once, say so once it's open: you'll get a summary to send back when "
                "you close this.")
    else:
        hint = ("Click a card to see its answer and the reasoning behind it. These "
                "aren't in your collection yet, and nothing is added until you "
                "choose Update.")
    outer.addWidget(muted_label(hint))

    inner = QWidget()
    ilay = QVBoxLayout(inner)
    boxes = {}
    for deck_name, details in decks:
        if not details:
            continue
        ilay.addWidget(section_label(deck_name.split("::")[-1], top_margin=10))
        for detail in details:
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
