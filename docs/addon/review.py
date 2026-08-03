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
import os

from aqt import mw
from aqt.qt import (QDialog, QDialogButtonBox, QFontDatabase, QFrame, QHBoxLayout,
                    QLabel, QPlainTextEdit, QPushButton, QScrollArea, Qt, QTimer,
                    QVBoxLayout, QWidget)

from .config import ADDON_VERSION, APP_NAME, FEEDBACK, _cfg, _load_json, _save_json
from .logic import (build_feedback_digest, cloze_filled_html, field_preview_html,
                    field_preview_text, note_display_label)
from .ui import (_info, copy_to_clipboard, hint_label, muted_label,
                 section_label, title_label)

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

_DIM = "#8a9aa2"        # the tag lead-in and the caret
_ROW_RULE = "#d6d6d6"   # the hairline between two cards
_CELL_RULE = "#a9b4ba"  # a table's own gridlines, a mid-tone that reads on both themes

# Every label that can hold card HTML carries this, so a <table> or a <ul> in a field
# reads as the grid or the list the card author wrote rather than as a run-on line.
# Qt's rich text takes a <style> block with class and element selectors, but only a
# subset of CSS, so this stays to borders, padding and colour.
_PREVIEW_STYLE = (
    "<style>"
    f".cloze {{ color: {_CLOZE_COLOR}; font-weight: 600; }}"
    # The group number rides in the deletion's own colour rather than a new one, so it
    # reads as part of that blank and adds no colour to the theme problem. Only the
    # weight is dropped: it is a marker, not content. Deliberately NOT given a smaller
    # font size on top of the <sup> tag's own reduction: rendered offscreen at a 13px
    # base, "font-size: small" inside a <sup> came out an unreadable smudge, which no
    # amount of squinting at the markup would have shown. Render it before resizing it.
    " .cn { font-weight: 400; }"
    " table { border-collapse: collapse; margin: 4px 0; }"
    f" th, td {{ border: 1px solid {_CELL_RULE}; padding: 2px 7px; }}"
    f" th {{ color: {_DIM}; font-weight: 600; }}"
    " ul, ol { margin: 4px 0; }"
    "</style>"
)

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

    A cloze field is run through field_preview_html before cloze_filled_html, since a
    real cloze Text field carries its own HTML (a table, inline images, br, entities).
    field_preview_html keeps the structure and names any inline image, without touching
    {{c1::...}} markup, so cloze_filled_html fills the deletions into markup that is
    already safe and must not be escaped a second time.
    """
    if _is_cloze(detail):
        text = field_preview_html(_field(detail, "Text"))
        return cloze_filled_html(text, escape=False)
    fields = _content_fields(detail)
    text = field_preview_html(fields[0][1]) if fields else ""
    if _is_image_note(detail):
        image_text = html.escape(_image_text(detail))
        if image_text:
            text = f"{image_text} {text}".strip() if text else image_text
    return text


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
    answer = field_preview_html(fields[1][1]) if len(fields) >= 2 else ""
    if not _is_image_note(detail):
        image_text = html.escape(_image_text(detail))
        if image_text:
            answer = f"{answer} {image_text}".strip() if answer else image_text
    return answer


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
    return _PREVIEW_STYLE + primary


def _rich_label(text):
    lbl = QLabel(_PREVIEW_STYLE + text)
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

    why_html = field_preview_html(_field(detail, "Why"))
    if why_html:
        why_label = _rich_label(why_html)
        # The `border: none` reset is load-bearing: Qt ignores a lone border-left on a
        # QLabel unless the shorthand is set first, so without it the padding applies
        # and the rule itself silently never paints.
        why_label.setStyleSheet(f"border: none; border-left: 3px solid {_WHY_RULE};"
                                f" padding-left: 8px; color: {_WHY_RULE};")
        blay.addWidget(why_label)

    dosing_html = field_preview_html(_field(detail, "Dosing"))
    if dosing_html:
        dosing_label = _rich_label(f"<b>Dosing</b> &nbsp;{dosing_html}")
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


def save_feedback(entries):
    """Write the in-progress notes to user_files/, or clear the file when there are none.

    Called as she types rather than only when the dialog closes. Everything else a run
    does can be reproduced by clicking Update again; what she wrote about a card cannot,
    so it is the one thing that must survive a crash, a force quit, or an error thrown
    three dialogs later. Kept next to the other persistent state, so an add-on update
    does not wipe it.
    """
    if entries:
        _save_json(FEEDBACK, entries)
    else:
        clear_saved_feedback()


def load_saved_feedback():
    """Notes left over from a run that never reached its digest. {guid: {note, deck,
    front}}, empty when there is nothing pending."""
    saved = _load_json(FEEDBACK, {})
    return saved if isinstance(saved, dict) else {}


def clear_saved_feedback():
    """Drop the saved notes, once they have actually been shown to her."""
    try:
        os.remove(FEEDBACK)
    except OSError:
        pass


def review_new_cards(parent, decks, flags, unreadable=()):
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

    That principle is why the boxes also save to disk as she types (save_feedback, on a
    short debounce so it is not a write per keystroke). Closing this dialog is not the
    end of the run: the digest that actually hands her notes back comes several steps
    later, after an import that can fail, and before v0.41.0 anything that ended the run
    in between dropped them without a word. `unreadable` names decks whose new cards
    could not be read, shown as a line here rather than as its own warning box.
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

    if unreadable:
        outer.addWidget(muted_label(
            "Couldn't read the new cards from " + ", ".join(unreadable) +
            ". The update itself is unaffected; those decks just aren't shown here."))

    inner = QWidget()
    ilay = QVBoxLayout(inner)
    ilay.setContentsMargins(0, 0, 0, 0)
    ilay.setSpacing(0)
    boxes = {}
    # Each card's deck and readable front, so a note saved to disk can still name the
    # card it is about in a later session, when this run's own index is long gone.
    index = {}
    for deck_name, details in decks:
        if not details:
            continue
        ilay.addWidget(section_label(deck_name.split("::")[-1], top_margin=14))
        for i, detail in enumerate(details):
            if i:
                ilay.addWidget(_separator())   # between cards, not after the last
            index[detail["guid"]] = (deck_name, note_display_label(
                [v for _, v in detail.get("fields", [])]))
            ilay.addWidget(_card_row(detail, flags, boxes, collect_feedback))
    ilay.addStretch(1)

    # What is already on disk, read once. A note carried in from an earlier session is
    # about a card this dialog may not be showing at all (its deck imported last time),
    # so this dialog cannot name it: only the saved entry still knows its deck and
    # front. Rebuilding it from `index` instead wrote the GUID in as the front and lost
    # the card's identity on the very first re-save.
    carried = load_saved_feedback()

    def _current_entries():
        entries = {g: {"note": t, "deck": index.get(g, ("", ""))[0],
                       "front": index.get(g, ("", g))[1]}
                   for g, box in boxes.items()
                   for t in [box.toPlainText().strip()] if t}
        # Notes carried in from an earlier session are pending too: dropping them
        # because this dialog has no box for that card would lose exactly the notes
        # this whole mechanism exists to keep.
        for g, note in flags.items():
            if g in entries or g in boxes:
                continue
            entries[g] = dict(carried.get(g) or {}, note=note) if carried.get(g) else {
                "note": note, "deck": index.get(g, ("", ""))[0],
                "front": index.get(g, ("", g))[1]}
        return entries

    # Debounced rather than saving on every keystroke: a burst of typing collapses into
    # one write, and 400ms is far below the time any of the losing scenarios take.
    saver = QTimer(dlg)
    saver.setSingleShot(True)
    saver.setInterval(400)
    saver.timeout.connect(lambda: save_feedback(_current_entries()))
    for box in boxes.values():
        box.textChanged.connect(saver.start)

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
    saver.stop()                    # the final state, not whatever the debounce had
    save_feedback(_current_entries())
    return flags


def show_result_with_feedback(summary_html, entries):
    """The end of a run, as one dialog instead of two.

    A completion summary and a feedback digest used to arrive as separate boxes, back
    to back, at the exact point in the run where the reader is most done paying
    attention: the summary lands first, gets dismissed, and the digest, the one thing
    she cannot reproduce by running the update again, appears behind it looking like
    yet another popup. They are one dialog now, summary on top, digest below it.

    Degrades in both directions on purpose: a run with no flagged cards is a plain
    _info as before, and a digest with no summary (she backed out of the update but
    still wrote notes) is the digest on its own.
    """
    if not entries:
        if summary_html:
            _info(summary_html)
        return
    offer_feedback_digest(None, entries, summary_html=summary_html)


def offer_feedback_digest(parent, entries, summary_html=None):
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
    if summary_html:
        summary = QLabel(summary_html)
        summary.setWordWrap(True)
        summary.setTextFormat(Qt.TextFormat.RichText)
        summary_scroll = QScrollArea()
        summary_scroll.setWidgetResizable(True)
        summary_scroll.setFrameShape(QFrame.Shape.NoFrame)
        summary_scroll.setMaximumHeight(200)
        summary_scroll.setWidget(summary)
        lay.addWidget(summary_scroll)
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
