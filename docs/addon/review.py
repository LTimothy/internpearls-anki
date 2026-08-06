"""The card review dialog, and the feedback digest it produces.

Its own module rather than part of dialogs.py because dialogs.py imports sync.py (for
Manage decks' manifest fetch and the Update my decks action), and this is opened *from*
sync.py's update flow, so living in dialogs.py would close that import into a cycle.

Presentation only, in both directions: it reads note fields that sync.py already pulled
out of a downloaded .apkg, and hands back what the learner typed. It also reads the
collection's own media folder, to render the learner's side of a changed-field
comparison from a picture she already has, but it writes nothing there or anywhere else,
and touches no network. That's still why the dialog has no Cancel.
"""
import datetime
import html
import os
import tempfile

from aqt import mw
from aqt.qt import (QDialog, QDialogButtonBox, QFontDatabase, QFrame, QHBoxLayout,
                    QImage, QLabel, QPlainTextEdit, QPushButton, QScrollArea, Qt,
                    QTimer, QVBoxLayout, QWidget)

from .config import ADDON_VERSION, APP_NAME, FEEDBACK, _cfg, _load_json, _save_json
from .logic import (apkg_media_index, build_feedback_digest, cloze_filled_html,
                    extract_apkg_media, field_image_names, field_preview_html,
                    field_preview_text, note_display_label, plain_text)
from .palette import colors
from .ui import _info, copy_to_clipboard, hint_label, muted_label, title_label
from .widgets import StreamingList, chip_html, section_header

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
# grey dosing block, and blue cloze fill. Every colour below is asked for by role from
# palette.colors(), which picks the light or dark set from Anki's own theme at the
# moment it is called, rather than being decided once at import.

def _preview_style():
    """Every label that can hold card HTML carries this, so a <table> or a <ul> in a
    field reads as the grid or the list the card author wrote rather than as a run-on
    line. Qt's rich text takes a <style> block with class and element selectors, but
    only a subset of CSS, so this stays to borders, padding and colour.

    A function rather than a module constant: its colours come from the palette, which
    is chosen from Anki's theme at the moment this is called, not fixed once at import.
    """
    c = colors()
    return (
        "<style>"
        f".cloze {{ color: {c['accent']}; font-weight: 600; }}"
        # The group number rides in the deletion's own colour rather than a new one, so it
        # reads as part of that blank and adds no colour to the theme problem. Only the
        # weight is dropped: it is a marker, not content. Deliberately NOT given a smaller
        # font size on top of the <sup> tag's own reduction: rendered offscreen at a 13px
        # base, "font-size: small" inside a <sup> came out an unreadable smudge, which no
        # amount of squinting at the markup would have shown. Render it before resizing it.
        " .cn { font-weight: 400; }"
        " table { border-collapse: collapse; margin: 4px 0; }"
        f" th, td {{ border: 1px solid {c['cell_rule']}; padding: 2px 7px; }}"
        f" th {{ color: {c['dim']}; font-weight: 600; }}"
        " ul, ol { margin: 4px 0; }"
        "</style>"
    )

_CARET_CLOSED = "▸"
_CARET_OPEN = "▾"

# The caret's width plus its gap to the text. The expanded body indents by exactly
# this, so the answer lines up under the primary line rather than under the caret.
_CARET_W = 14
_CARET_GAP = 6

# The row body's usable width at the dialog's 560px minimum. A picture wider than this
# is scaled down to it; a smaller one is left alone rather than blown up.
_IMAGE_MAX_W = 440


def _image_tag(local_path):
    """One extracted picture as an <img> Qt's rich text can paint, capped to the row.

    The natural width is read from the file rather than assumed, since Qt scales to
    whatever `width` says and a fixed value would enlarge a small diagram as readily as
    it shrinks a large one. A file Qt cannot decode returns None, so the caller keeps
    naming it instead of painting a broken image.
    """
    image = QImage(local_path)
    if image.isNull():
        return None
    natural = image.width()
    width = min(natural, _IMAGE_MAX_W) if natural else _IMAGE_MAX_W
    return f'<img src="{html.escape(local_path, quote=True)}" width="{width}">'


def _media_resolver(apkg_path, index, dest):
    """A field_preview_html resolver bound to one deck, extracting as it is asked.

    Nothing comes out of the archive until a row is actually opened, and each picture is
    resolved once per dialog however many fields reference it.
    """
    cache = {}

    def resolve(name):
        if name not in cache:
            found = extract_apkg_media(apkg_path, index, [name], dest)
            cache[name] = _image_tag(found[name]) if name in found else None
        return cache[name]

    return resolve


def build_resolvers(sources):
    """Per-deck picture resolvers for `sources` ({deck_name: .apkg path}), and the
    TemporaryDirectory they extract into.

    Shared by review_cards and the inline update screen (build_update_body), so a
    picture only ever gets one extract-on-first-expand pipeline, not two copies of it.
    The caller must call the returned directory's .cleanup() once whatever screen it
    feeds has closed.
    """
    media_dir = tempfile.TemporaryDirectory()
    resolvers = {}
    for name, path in (sources or {}).items():
        media_index = apkg_media_index(path)
        if media_index:
            resolvers[name] = _media_resolver(
                path, media_index, os.path.join(media_dir.name, str(len(resolvers))))
    return resolvers, media_dir


def pending_entries(boxes, flags, index, carried):
    """{guid: {note, deck, front}} for everything currently flagged: every box that
    currently holds text, plus any flag with no box here at all.

    Shared by review_cards and the inline update screen, so what gets written to disk
    mid-session is decided in exactly one place. `carried` is load_saved_feedback()'s
    own record, read once by the caller: the fallback for a flagged guid that has
    neither a box nor an `index` entry here, a note left over from an earlier session
    about a card whose deck already imported last run and so isn't shown at all this
    time. Rebuilding its deck/front from `index` alone would write the bare GUID in as
    the front instead of the name the earlier session actually saved.
    """
    entries = {g: {"note": t, "deck": index.get(g, ("", ""))[0],
                   "front": index.get(g, ("", g))[1]}
               for g, box in boxes.items()
               for t in [box.toPlainText().strip()] if t}
    for g, note in flags.items():
        if g in entries or g in boxes:
            continue
        entries[g] = dict(carried.get(g) or {}, note=note) if carried.get(g) else {
            "note": note, "deck": index.get(g, ("", ""))[0],
            "front": index.get(g, ("", g))[1]}
    return entries


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


def _changed_field_names(detail):
    """The fields this card would rewrite, in the note type's own order.

    Ordered off the note type rather than off the `was` map so two cards of the same
    type always list their changes the same way round.
    """
    was = detail.get("was") or {}
    return [n for n, _ in detail.get("fields", []) if n in was]


def _image_text(detail, resolved=frozenset()):
    """The card's Image field, named rather than rendered (field_preview_text again:
    naming rather than painting is what lets a collapsed row skip extraction). `resolved`
    is the filenames the picture strip above has already painted successfully; any of
    them drop out of the name here, so a picture that rendered once does not also sit in
    the answer as a chip naming it. A name whose resolution failed stays, since the chip
    is exactly the fallback a broken or missing picture needs. Empty when the note type
    has no Image field, the card doesn't use one, or every picture it names already
    painted.
    """
    value = _field(detail, "Image")
    text = plain_text(value)
    names = [n for n in field_image_names(value) if n not in resolved]
    if not names:
        return text
    tag = f"[image: {', '.join(names)}]"
    return f"{text} {tag}" if text else tag


def _primary_field(detail):
    """The (name, value) of the note type's primary content field: a cloze's Text, or
    otherwise the first field `_content_fields` keeps (Front for a basic note, Prompt
    for an image note). None when a note type somehow has no content field at all.

    Shared between `_primary_html`, which renders it, and `_primary_images`, which
    looks for pictures inline in it, so the note-type branching that picks it out
    lives in exactly one place rather than twice.
    """
    if _is_cloze(detail):
        return ("Text", _field(detail, "Text"))
    fields = _content_fields(detail)
    return fields[0] if fields else None


def _primary_field_names(detail):
    """Every field name that feeds the card's primary line: `_primary_field`'s own
    name, plus, for an image note, the Image field folded in beside it (see
    `_primary_html`). Used to route a `was` line for any of them to the top of the
    expanded body, since none of these fields has a block of its own down there."""
    names = ["Image"] if _is_image_note(detail) else []
    primary = _primary_field(detail)
    if primary:
        names.append(primary[0])
    return names


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
    primary = _primary_field(detail)
    text = field_preview_html(primary[1]) if primary else ""
    if _is_cloze(detail):
        return cloze_filled_html(text, escape=False)
    if _is_image_note(detail):
        image_text = html.escape(_image_text(detail))
        if image_text:
            text = f"{image_text} {text}".strip() if text else image_text
    return text


def _answer_html(detail, resolved=frozenset(), image_html=None):
    """The card's answer, shown only once its row is expanded. A cloze note has no
    answer text of its own here, since cloze_filled_html already put the answer on the
    collapsed line, but its optional Image field (separate from any image inline in
    Text) still needs naming somewhere, so it goes here too. A non-image note's
    optional Image field (a basic card with a picture on its back, say) is named and
    folded in here, alongside the answer it illustrates.

    `resolved` passes straight through to `_image_text`: once the picture strip above
    has actually painted a filename, this stops naming it too, so an opened row never
    shows the same picture and a chip for it both. `image_html` passes straight through
    to `field_preview_html` for the answer field itself, so a picture inline in the
    answer's own text (not just its Image field) renders in place too. Both are left at
    their defaults (nothing resolved, no resolver) at build time, before anything has
    been extracted; `_reveal_images` calls this again, once, with both filled in, once a
    row's pictures are actually resolved.
    """
    if _is_cloze(detail):
        image_text = _image_text(detail, resolved)
        return html.escape(image_text) if image_text else ""
    fields = _content_fields(detail)
    answer = field_preview_html(fields[1][1], image_html=image_html) if len(fields) >= 2 else ""
    if not _is_image_note(detail):
        image_text = html.escape(_image_text(detail, resolved))
        if image_text:
            answer = f"{answer} {image_text}".strip() if answer else image_text
    return answer


def _answer_field_names(detail):
    """Every field name `_answer_html` draws its expanded-body block from, mirroring
    that function's own branching: a cloze's Image field is its whole answer block;
    otherwise the second content field (Back for a basic note, Answer for an image
    note), plus, for a non-image note, its own optional Image field folded in beside
    it. Used to route a `was` line for any of them under the answer block."""
    if _is_cloze(detail):
        return ["Image"]
    fields = _content_fields(detail)
    names = [fields[1][0]] if len(fields) >= 2 else []
    if not _is_image_note(detail):
        names.append("Image")
    return names


def _primary_images(detail):
    """The pictures the collapsed line can only name: the Image field, plus any inline
    in the primary field itself.

    An image note's whole question is its picture and a cloze can carry one inside its
    Text, so without this the one note type that is entirely about pictures would be the
    one that never showed them. Fields rendered in the body resolve their own images in
    place instead, so nothing appears twice.
    """
    primary = _primary_field(detail)
    primary_value = primary[1] if primary else ""
    names = field_image_names(_field(detail, "Image")) + field_image_names(primary_value)
    return list(dict.fromkeys(names))


def _row_html(detail):
    """A collapsed row's whole line: its kind when it has one, then the card's tag, then
    its primary line.

    One rich-text paragraph rather than a tag widget beside a text widget. Two widgets
    start each row's text at a different x depending on whether that card happens to
    carry a tag, and wrap it against the tag's edge instead of the row's.
    """
    primary = _primary_html(detail)
    tag_text = field_preview_text(_field(detail, "Tag"))
    if tag_text:
        tag = html.escape(tag_text)
        primary = f'<span style="color: {colors()["dim"]};">{tag}</span>&nbsp;&nbsp;{primary}'
    return _preview_style() + chip_html(detail.get("kind")) + primary


def _rich_label(text):
    lbl = QLabel(_preview_style() + text)
    lbl.setWordWrap(True)
    lbl.setTextFormat(Qt.TextFormat.RichText)
    return lbl


def _was_label(detail, field_name):
    """(label, old value) for what a changed field says in the collection today, or
    None when it has not moved.

    Rendered rather than quoted, and dimmed, so a comparison reads as the same card in
    two states rather than as two cards. Her copy's pictures are already in the
    collection's own media folder, so they need no extraction, but a QImage decode still
    costs real work per picture: the label starts out naming any picture rather than
    painting it, and the caller is the one that resolves it, on the same first-expand
    schedule as everything else in the body, so a collapsed row decodes nothing.
    """
    old = (detail.get("was") or {}).get(field_name)
    if not old:
        return None
    label = _rich_label(f"<b>was</b> &nbsp;{field_preview_html(old)}")
    label.setStyleSheet(f"color: {colors()['dim']};")
    return label, old


def _collection_image(name):
    """One of the learner's own media files as an <img>, or None when it is not there.

    Her side of a comparison comes out of the collection, not out of the .apkg, so its
    pictures are already on disk and need no extraction.
    """
    try:
        folder = mw.col.media.dir()
    except Exception:
        return None
    if not folder:
        return None
    local = os.path.join(folder, os.path.basename(name))
    return _image_tag(local) if os.path.exists(local) else None


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
    line.setStyleSheet(f"color: {colors()['row_rule']};")
    return line


def _card_row(detail, flags, boxes, collect_feedback, resolve=None):
    """One card as a single row: a caret, its tag if it has one, and its primary
    line. Clicking the row (the caret or the line itself) reveals the answer, the why
    behind a green left rule, and dosing when present, plus, only when feedback
    collection is on, a box for what the learner makes of it.

    Pictures are named until that first expand and rendered from then on. Extraction is
    what opening a row pays for, so a long list stays cheap to scroll and a review nobody
    opens costs nothing at all.
    """
    guid = detail["guid"]
    row = QWidget()
    outer = QVBoxLayout(row)
    outer.setContentsMargins(0, 5, 0, 6)
    outer.setSpacing(4)

    body = QWidget()
    caret = QPushButton(_CARET_CLOSED)
    # Labels whose field can hold a picture, each with the field value that produced it,
    # so the first expand can re-render exactly those and leave the rest alone. The
    # answer label isn't one of these: it has its own composed rendering (folding an
    # Image field's chip in alongside its own text), so `_reveal_images` re-renders it
    # once, directly through `_answer_html`, rather than through this generic list.
    rerender = []
    # A changed field's `was` line, separately: it resolves from the learner's own
    # collection (_collection_image) rather than the deck's .apkg, so it stays on the
    # same first-expand schedule as everything else even on a row `resolve` is None for.
    was_rerender = []
    revealed = []

    def _reveal_images():
        if revealed:
            return
        revealed.append(True)
        for label, value in was_rerender:
            was_html = field_preview_html(value, image_html=_collection_image)
            label.setText(f"{_preview_style()}<b>was</b> &nbsp;{was_html}")
        if resolve is None:
            return
        image_names = _primary_images(detail)
        resolved_tags = {name: resolve(name) for name in image_names}
        succeeded = {name for name, tag in resolved_tags.items() if tag}
        for label, value in rerender:
            label.setText(_preview_style() + field_preview_html(value, image_html=resolve))
        rendered = "<br>".join(tag for tag in resolved_tags.values() if tag)
        if rendered:
            images.setText(_preview_style() + rendered)
            images.setVisible(True)
        if answer_label is not None:
            new_answer_html = _answer_html(detail, resolved=succeeded, image_html=resolve)
            answer_label.setText(_preview_style() + new_answer_html)
            answer_label.setVisible(bool(new_answer_html))

    def _toggle():
        expanded = not body.isVisible()
        if expanded:
            _reveal_images()
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
    caret.setStyleSheet(f"border: none; padding: 0; color: {colors()['dim']};")
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

    # A changed field's `was` line belongs directly under the block that field feeds,
    # per name, so the comparison reads as the same card in two states rather than as
    # one detached list. `was_placed` tracks which changed names have already found a
    # home; anything left over falls to the catch-all at the very end instead of
    # vanishing, which covers a field with no block of its own (Tag) and a field whose
    # block didn't render this time (its current value went blank).
    changed_names = _changed_field_names(detail)
    primary_names = set(_primary_field_names(detail))
    answer_names = set(_answer_field_names(detail))
    was_placed = set()

    def _add_was(name):
        was = _was_label(detail, name)
        if was is not None:
            label, value = was
            blay.addWidget(label)
            was_rerender.append((label, value))

    # The primary field (Front / a cloze's Text / an image note's Image+Prompt) is the
    # collapsed header line above the body, not a block inside it, so its `was` line
    # opens the body instead, reading as belonging to the line directly above it.
    for name in changed_names:
        if name in primary_names:
            _add_was(name)
            was_placed.add(name)

    images = _rich_label("")
    images.setVisible(False)
    blay.addWidget(images)

    # Declared here, ahead of the conditional that may or may not create it, so
    # `_reveal_images` (a closure defined earlier in this function, called later) always
    # finds a bound name to check rather than raising on a card with no answer block.
    answer_label = None
    answer_html = _answer_html(detail)
    if answer_html:
        answer_label = _rich_label(answer_html)
        blay.addWidget(answer_label)
        for name in changed_names:
            if name in answer_names:
                _add_was(name)
                was_placed.add(name)

    why_value = _field(detail, "Why")
    why_html = field_preview_html(why_value)
    if why_html:
        why_label = _rich_label(why_html)
        why_colour = colors()["why"]
        # The `border: none` reset is load-bearing: Qt ignores a lone border-left on a
        # QLabel unless the shorthand is set first, so without it the padding applies
        # and the rule itself silently never paints.
        why_label.setStyleSheet(f"border: none; border-left: 3px solid {why_colour};"
                                f" padding-left: 8px; color: {why_colour};")
        blay.addWidget(why_label)
        rerender.append((why_label, why_value))
        if "Why" in changed_names:
            _add_was("Why")
            was_placed.add("Why")

    # Dosing is deliberately left out of rerender: it is a citation, never a picture,
    # and re-rendering it from field_preview_html would drop the "Dosing" label prefix.
    dosing_html = field_preview_html(_field(detail, "Dosing"))
    if dosing_html:
        dosing_label = _rich_label(f"<b>Dosing</b> &nbsp;{dosing_html}")
        c = colors()
        # A hardcoded background needs a hardcoded foreground with it: text colour
        # otherwise comes from the platform palette, which flips white under Night
        # Mode while this block stays light. See "Colors" in README.md.
        dosing_label.setStyleSheet(f"background: {c['dosing_bg']}; color: {c['dosing_fg']};"
                                   f" padding: 6px; border-radius: 4px;")
        blay.addWidget(dosing_label)
        if "Dosing" in changed_names:
            _add_was("Dosing")
            was_placed.add("Dosing")

    # Anything still unplaced (Tag, or a field whose own block stayed empty) has
    # nowhere of its own to sit under, but still needs to surface rather than
    # disappear, so it goes at the end rather than wedged into an unrelated block.
    for name in changed_names:
        if name not in was_placed:
            _add_was(name)

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


def review_cards(parent, decks, flags, unreadable=(), sources=None):
    """Show every card this update would add or rewrite, as one row each, and collect
    notes on them when the feedback toggle asks for it.

    `decks` is [(deck_name, [detail, ...])], each detail as apkg_note_details returns it.
    `flags` is {guid: note text}: read to prefill the boxes and rewritten in place on
    close, so closing and reopening shows what she already wrote instead of quietly
    dropping it. With feedback collection off, no boxes are ever created, so this is a
    read-only preview and `flags` comes back untouched.

    `sources` is {deck_name: .apkg path} for the decks whose media can be resolved. A
    deck missing from it renders exactly as it did before pictures were possible, which
    is what a download this run could not read has to do.

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
    in between dropped them without a word. `unreadable` names decks whose pending
    cards could not be read, shown as a line here rather than as its own warning box.
    """
    collect_feedback = _cfg()["collect_feedback"]
    dlg = QDialog(parent or mw)
    dlg.setWindowTitle(f"{APP_NAME}: card review")
    dlg.setMinimumWidth(560)
    dlg.setMinimumHeight(520)
    outer = QVBoxLayout(dlg)

    total = sum(len(details) for _, details in decks)
    kinds = [d.get("kind") for _, details in decks for d in details]
    counts = [f"{kinds.count('new')} new" if "new" in kinds else "",
              f"{kinds.count('changed')} updated" if "changed" in kinds else ""]
    heading = f"{total} card(s)"
    detail_line = " · ".join(c for c in counts if c)
    outer.addWidget(title_label(f"{heading}  ({detail_line})" if detail_line else heading))
    hint = "Nothing is added until you choose Update. Click a card to open it."
    if collect_feedback:
        hint += " Say what's wrong with one and you'll get a summary to send back."
    outer.addWidget(hint_label(hint))

    if unreadable:
        outer.addWidget(muted_label(
            "Couldn't read the pending cards from " + ", ".join(unreadable) +
            ". The update itself is unaffected; those decks just aren't shown here."))

    inner = QWidget()
    ilay = QVBoxLayout(inner)
    ilay.setContentsMargins(0, 0, 0, 0)
    ilay.setSpacing(0)
    boxes = {}
    # Each card's deck and readable front, so a note saved to disk can still name the
    # card it is about in a later session, when this run's own index is long gone.
    index = {}
    resolvers, media_dir = build_resolvers(sources)
    for deck_name, details in decks:
        if not details:
            continue
        ilay.addWidget(section_header(deck_name.split("::")[-1]))
        for i, detail in enumerate(details):
            if i:
                ilay.addWidget(_separator())   # between cards, not after the last
            index[detail["guid"]] = (deck_name, note_display_label(
                [v for _, v in detail.get("fields", [])]))
            ilay.addWidget(_card_row(detail, flags, boxes, collect_feedback,
                                     resolve=resolvers.get(deck_name)))
    ilay.addStretch(1)

    # Read once. A note carried in from an earlier session is about a card this dialog
    # may not be showing at all (its deck imported last time), so this dialog cannot
    # name it on its own. pending_entries falls back to what the saved entry itself
    # knows about its deck and front.
    carried = load_saved_feedback()

    def _current_entries():
        return pending_entries(boxes, flags, index, carried)

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
    media_dir.cleanup()

    for guid, box in boxes.items():
        note = box.toPlainText().strip()
        if note:
            flags[guid] = note
        else:
            flags.pop(guid, None)   # she cleared it; treat that as unflagging
    saver.stop()                    # the final state, not whatever the debounce had
    save_feedback(_current_entries())
    return flags


def build_update_body(items, sources, flags, new_index, collect_feedback,
                      top_html, flagged_line, safety_html):
    """The Update my decks screen's body: fixed summary text, the streaming list of
    pending new and changed cards, then the flagged-card count and the safety note
    below it. `internpearls.ui._ask_with_widget` wraps whatever this returns with the
    dialog's title and its Update/Cancel buttons, the same as any other confirmation.

    `items` is a mix of ("header", deck_short_name), ("sep",), and ("card", deck_name,
    detail) entries, one per pending card, built by sync.py from every deck's new and
    changed cards. A header groups a deck's rows and a sep draws the hairline between
    two cards, the same way review_cards' own deck loop does. `sources` is
    {deck_name: .apkg path}, threaded straight into build_resolvers so a row's pictures
    extract from the same already-downloaded file review_cards used to read them from.

    `flags`/`new_index` are the {guid: note}/{guid: (deck, front)} maps update_decks()
    already carries through the run. A row's box writes into `flags` live, on every
    keystroke, rather than only when a separate review dialog closes: there is no such
    closing moment left once the rows sit on the confirmation itself.

    `flagged_line()` is called fresh on every keystroke to recompute the "N card(s)
    flagged" line shown below the list; `safety_html` is fixed.

    Returns (widget, boxes, flush). `boxes` is {guid: QPlainTextEdit}, built lazily as
    the list's own rows are. `flush()` stops the debounce save timer, writes one final
    unconditional copy of what's flagged to disk, and releases the temporary directory
    pictures were extracted into; the caller runs it once, right after the dialog this
    body sits in has closed.
    """
    resolvers, media_dir = build_resolvers(sources)
    boxes = {}
    carried = load_saved_feedback()

    def _entries():
        return pending_entries(boxes, flags, new_index, carried)

    body = QWidget()
    lay = QVBoxLayout(body)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(8)

    top = _rich_label(top_html)
    lay.addWidget(top)

    bottom = _rich_label(flagged_line() + safety_html)

    saver = QTimer(body)
    saver.setSingleShot(True)
    saver.setInterval(400)
    saver.timeout.connect(lambda: save_feedback(_entries()))

    def _on_change(guid, box):
        note = box.toPlainText().strip()
        if note:
            flags[guid] = note
        else:
            flags.pop(guid, None)   # she cleared it; treat that as unflagging
        bottom.setText(_preview_style() + flagged_line() + safety_html)
        saver.start()

    def _row(item):
        if item[0] == "header":
            return section_header(item[1])
        if item[0] == "sep":
            return _separator()
        _, deck_name, detail = item
        row = _card_row(detail, flags, boxes, collect_feedback,
                        resolve=resolvers.get(deck_name))
        box = boxes.get(detail["guid"])
        if box is not None:
            box.textChanged.connect(lambda g=detail["guid"], b=box: _on_change(g, b))
        return row

    if items:
        lay.addWidget(StreamingList(_row, items), 1)
    else:
        lay.addStretch()

    lay.addWidget(bottom)

    def flush():
        saver.stop()                    # the final state, not whatever the debounce had
        save_feedback(_entries())
        media_dir.cleanup()

    return body, boxes, flush


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
    # Every colour here is a palette reference, not a hex value, so the block follows
    # Night Mode instead of fighting it. It used to hardcode a near-white background
    # and leave the text to the palette, which is the v0.32.1 dosing-block bug exactly:
    # measured at 1.34:1 in dark mode (light grey on near-white), so the one thing in
    # this dialog she is meant to read was the one thing she could not. `base` is the
    # background a text field already uses, so it still reads as a sunken payload block
    # in both themes, and `text` on `base` is a pairing the platform guarantees.
    view.setStyleSheet("QPlainTextEdit { background: palette(base);"
                       " color: palette(text); border: 1px solid palette(mid); }")
    # Sized to its own content, up to the same 340px cap _ask_scrollable uses for a
    # long body, rather than the plain `addWidget(view, 1)` stretch this used to carry:
    # that stretch filled whatever height the dialog happened to be, so one short
    # flagged card left most of the box a blank payload-coloured slab. A long digest
    # still gets the full cap and scrolls internally past it.
    metrics = view.fontMetrics()
    line_count = text.count("\n") + 1
    # The +4 covers rounding in Qt's own layout of the document inside the viewport;
    # without it this estimate lands a couple of pixels short and triggers a scrollbar
    # nobody needs.
    content_height = int(line_count * metrics.lineSpacing()
                         + view.document().documentMargin() * 2 + view.frameWidth() * 2) + 4
    view.setFixedHeight(min(content_height, 340))
    lay.addWidget(view)
    # Without this, a dialog opened taller than its now content-sized parts (a manual
    # resize, or this add-on's own minimumHeight above) has nothing telling Qt where
    # the surplus goes, so it spreads thin gaps between the title/hint/box instead of
    # leaving one, below the content and above the buttons where it belongs.
    lay.addStretch()
    bb = QDialogButtonBox()
    again = bb.addButton("Copy again", QDialogButtonBox.ButtonRole.ActionRole)
    again.clicked.connect(lambda: copy_to_clipboard(text))
    close = bb.addButton("Close", QDialogButtonBox.ButtonRole.AcceptRole)
    close.clicked.connect(dlg.accept)
    lay.addWidget(bb)
    dlg.exec()
