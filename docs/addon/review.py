"""The new-card review dialog, and the feedback digest it produces.

Its own module rather than part of dialogs.py because dialogs.py imports sync.py (for
Manage decks' manifest fetch and the Update my decks action), and this is opened *from*
sync.py's update flow, so living in dialogs.py would close that import into a cycle.

Presentation only, in both directions: it reads note fields that sync.py already pulled
out of a downloaded .apkg, and hands back what the learner typed. Nothing here touches
the collection, the network, or config, which is also why the dialog has no Cancel.
"""
import datetime
import html

from aqt import mw
from aqt.qt import (QDialog, QDialogButtonBox, QFrame, QLabel, QPlainTextEdit,
                    QScrollArea, Qt, QVBoxLayout, QWidget)

from .config import ADDON_VERSION, APP_NAME
from .logic import build_feedback_digest, field_preview_text
from .ui import (copy_to_clipboard, hint_label, muted_label, section_label,
                 title_label)

# The learner's own annotation space, left empty by every spec on purpose. Showing it
# would be a blank row on every single card, captioned with the one field name that
# can't have anything to say at review time.
_SKIP_FIELDS = {"Notes"}


def _card_block(detail, flags, boxes):
    """One card: its fields, then a box for what the learner makes of it.

    Empty fields are skipped rather than shown blank, since a spec leaves whichever of
    Why/Dosing/Tag a given card doesn't need unset, and a column of empty captions
    would bury the two or three lines that actually carry the card.
    """
    frame = QFrame()
    frame.setFrameShape(QFrame.Shape.StyledPanel)
    lay = QVBoxLayout(frame)
    lay.setSpacing(3)

    if detail.get("notetype"):
        lay.addWidget(hint_label(detail["notetype"]))

    for name, value in detail.get("fields", []):
        if name in _SKIP_FIELDS:
            continue
        text = field_preview_text(value)
        if not text:
            continue
        row = QLabel(f"<b>{html.escape(name)}</b> &nbsp;{html.escape(text)}")
        row.setWordWrap(True)
        row.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(row)

    box = QPlainTextEdit(flags.get(detail["guid"], ""))
    box.setPlaceholderText("Anything wrong with this card? (optional)")
    box.setFixedHeight(58)
    lay.addWidget(box)
    boxes[detail["guid"]] = box
    return frame


def review_new_cards(parent, decks, flags):
    """Show every card this update would add, in full, and collect notes on them.

    `decks` is [(deck_name, [detail, ...])], each detail as apkg_note_details returns it.
    `flags` is {guid: note text}: read to prefill the boxes and rewritten in place on
    close, so closing and reopening shows what she already wrote instead of quietly
    dropping it.

    A card is flagged by writing something about it. A checkbox on top of a note box
    would be two ways to say one thing, and a flag with no note ("this card is wrong",
    but not how) isn't actionable enough to be worth collecting.

    There's no Cancel button, and every exit path keeps what she typed: nothing in this
    dialog changes anything, so the only thing a Cancel could throw away is her own
    work, which is never what she'd mean by it.
    """
    dlg = QDialog(parent or mw)
    dlg.setWindowTitle(f"{APP_NAME}: new cards")
    dlg.setMinimumWidth(560)
    dlg.setMinimumHeight(520)
    outer = QVBoxLayout(dlg)

    total = sum(len(details) for _, details in decks)
    outer.addWidget(title_label(f"{total} new card(s)"))
    outer.addWidget(muted_label(
        "These aren't in your collection yet, and nothing is added until you choose "
        "Update. If a card looks wrong, or reads as more than one fact at once, say so "
        "underneath it: you'll get a summary to send back when you close this."))

    inner = QWidget()
    ilay = QVBoxLayout(inner)
    boxes = {}
    for deck_name, details in decks:
        if not details:
            continue
        ilay.addWidget(section_label(deck_name.split("::")[-1], top_margin=10))
        for detail in details:
            ilay.addWidget(_card_block(detail, flags, boxes))
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
    can run past a message box's height with nothing to grab.
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
    lay.addWidget(view, 1)
    bb = QDialogButtonBox()
    close = bb.addButton("Close", QDialogButtonBox.ButtonRole.AcceptRole)
    close.clicked.connect(dlg.accept)
    lay.addWidget(bb)
    dlg.exec()
