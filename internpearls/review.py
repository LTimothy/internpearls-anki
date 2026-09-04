"""The update screen's card rows, the end-of-run summary, and the feedback digest.

Its own module rather than part of dialogs.py because dialogs.py imports sync.py (for
Manage decks' manifest fetch and the Update my decks action), and this is built *from*
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
                    QImage, QLabel, QPlainTextEdit, QPushButton, QScrollArea,
                    QSizePolicy, Qt, QTimer, QVBoxLayout, QWidget)

from .config import ADDON_VERSION, APP_NAME, FEEDBACK, _load_json, _save_json
from .logic import (apkg_media_index, build_feedback_digest, cloze_answer_changes,
                    cloze_filled_html, cloze_hint_changes,
                    extract_apkg_media, field_image_names,
                    field_preview_html, field_preview_text, merged_word_diff,
                    note_display_label, plain_text, plural, word_diff_ratio)
from .palette import colors
from .ui import (_ask_with_widget, _info, copy_to_clipboard, hint_label, link_button,
                 muted_label, title_label)
from .widgets import (CARET_GAP, CARET_W, StreamingList, chip_cell,
                      chip_column_width, decision_cell, row_text_indent,
                      section_header, simple_row)

# The learner's own annotation space, left empty by every spec on purpose. Showing it
# would be a blank row on every single card.
_SKIP_FIELDS = {"Notes"}

# Fields with their own dedicated treatment below rather than being read as generic
# prompt/answer content: Why gets the green rule, Dosing gets its label, Tag becomes
# the row's dim header text, and Image is named rather than rendered (see
# field_preview_text) and folded into the primary or answer line instead of shown on
# its own (see _image_text).
_STRUCTURAL_FIELDS = {"Why", "Dosing", "Tag", "Image"}

# How tall a build_list_body confirmation opens. Each of them lists one Advanced
# action's worth of rows, so they start shorter than Update my decks, whose list covers
# everything pending at once and takes ui._ask_with_widget's own taller default. A
# floor, not a fixed height: a longer list still grows the dialog and then scrolls
# inside it. Lives here rather than in sync.py because collection.py builds one of
# these confirmations too and cannot import sync.py (sync.py imports it).
_CONFIRM_HEIGHT = 380

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

    The `a` rule mirrors internpearls.ui._ask_scrollable's own link_style: Qt paints an
    anchor in its own built-in link colour, never in anything set on the widget, so
    without a rule here an anchor hiding in unescaped card content (a front, a Why
    field) would render in a colour that doesn't track the theme. Combined with every
    caller of `_rich_label` below also closing off `setOpenExternalLinks`, a link in
    that content reads as inert, on-theme text rather than something clickable.
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
        # The hint half of a deletion is the prompt, not the fact, so it is set apart
        # from the answer's blue rather than sharing it: same role `th` takes in a
        # table here, a label beside the content it introduces.
        f" .ch {{ color: {c['dim']}; }}"
        " table { border-collapse: collapse; margin: 4px 0; }"
        f" th, td {{ border: 1px solid {c['cell_rule']}; padding: 2px 7px; }}"
        f" th {{ color: {c['dim']}; font-weight: 600; }}"
        " ul, ol { margin: 4px 0; }"
        f" a {{ color: {c['accent']}; }}"
        "</style>"
    )

_CARET_CLOSED = "▸"
_CARET_OPEN = "▾"

# The row body's usable width at the dialog's 660px minimum. A picture wider than this
# is scaled down to it; a smaller one is left alone rather than blown up.
_IMAGE_MAX_W = 540


def _svg_thumbnail(svg_path):
    """Rasterize a drawn SVG to a PNG thumbnail, its background painted white by the
    add-on rather than trusted to the SVG's own markup or to Qt's SVG image plugin:
    QImage(path) sizes a root with no absolute width/height against a default
    viewport, not the SVG's own viewBox, which is what used to leave a white square
    in one corner with the drawing spilling past it (svg_to_media normalizes the
    file itself for the same reason, but this covers a file that predates that fix).

    Returns the PNG's path, or None if PyQt6.QtSvg isn't available or the file
    doesn't parse as SVG; the caller falls back to the plain QImage(local_path) path
    either way.
    """
    try:
        from PyQt6.QtCore import Qt as _Qt
        from PyQt6.QtGui import QImage as _QImage, QPainter
        from PyQt6.QtSvg import QSvgRenderer
    except ImportError:
        return None
    renderer = QSvgRenderer(svg_path)
    if not renderer.isValid():
        return None
    size = renderer.defaultSize()
    if size.width() <= 0 or size.height() <= 0:
        return None
    width = min(size.width(), _IMAGE_MAX_W)
    height = max(1, round(size.height() * width / size.width()))
    image = _QImage(width, height, _QImage.Format.Format_ARGB32)
    image.fill(_Qt.GlobalColor.white)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    png_path = svg_path + ".png"
    return png_path if image.save(png_path, "PNG") else None


def _image_tag(local_path):
    """One extracted picture as an <img> Qt's rich text can paint, capped to the row.

    The natural width is read from the file rather than assumed, since Qt scales to
    whatever `width` says and a fixed value would enlarge a small diagram as readily as
    it shrinks a large one. A file Qt cannot decode returns None, so the caller keeps
    naming it instead of painting a broken image.
    """
    if local_path.lower().endswith(".svg"):
        thumb = _svg_thumbnail(local_path)
        if thumb:
            local_path = thumb
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

    Used by the update screen (build_update_body) to extract a card's picture the first
    time its row expands. The caller must call the returned directory's .cleanup() once
    the screen it feeds has closed.
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

    Kept separate from build_update_body so what gets written to disk mid-session is
    decided in exactly one place. `carried` is load_saved_feedback()'s own record, read
    once by the caller: the fallback for a flagged guid that has neither a box nor an
    `index` entry here, a note left over from an earlier session about a card whose
    deck already imported last run and so isn't shown at all this time. Rebuilding its
    deck/front from `index` alone would write the bare GUID in as the front instead of
    the name the earlier session actually saved.
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
    """A collapsed row's whole line: the card's tag when it has one, then its primary
    line.

    One rich-text paragraph rather than a tag widget beside a text widget. Two widgets
    start each row's text at a different x depending on whether that card happens to
    carry a tag, and wrap it against the tag's edge instead of the row's.

    The kind is deliberately not here: a chip is one of a fixed set of four and belongs
    in its own column (widgets.chip_cell), while a tag is free text that reads as a
    lead-in to this card's own line.
    """
    primary = _primary_html(detail)
    tag_text = field_preview_text(_field(detail, "Tag"))
    if tag_text:
        tag = html.escape(tag_text)
        primary = f'<span style="color: {colors()["dim"]};">{tag}</span>&nbsp;&nbsp;{primary}'
    return _preview_style() + primary


def _rich_label(text):
    """A QLabel for a block of card or confirmation HTML, closed to external links by
    default: every caller here interpolates content straight out of the collection
    (a field, a deck name, a retired card's identity), never escaped, so an anchor
    hiding in it must not be able to launch the system browser on a click. Matches
    internpearls.ui._ask_scrollable's own default for the same reason; see that
    function's docstring.
    """
    lbl = QLabel(_preview_style() + text)
    lbl.setWordWrap(True)
    lbl.setTextFormat(Qt.TextFormat.RichText)
    lbl.setOpenExternalLinks(False)
    return lbl


def _plain_prose(value):
    """Whether a field value is plain prose `merged_word_diff` can be shown for: text
    carrying no tags, no cloze braces, and no math markup. Splitting any of those on
    spaces would tear the markup apart mid-token, so a field carrying them keeps the
    verbatim rendering instead. Entities pass, since they render as themselves on
    either side of a diff."""
    return bool(value) and "<" not in value and "{{" not in value and "\\" not in value


def _diff_html(segments):
    """`merged_word_diff`'s segments as one rich-text line: dropped words struck
    through, added words carried on the UPDATED chip's own colour pair, unchanged
    words left to the line's dim base colour. One line that states the whole change,
    instead of two near-identical paragraphs the reader has to compare word by word.

    Strikethrough is the same convention a Never row already uses: text she will not
    have. The chip pair for additions ties them to the UPDATED marker at the row's
    head, and is a background/foreground pairing the palette already guarantees.
    """
    c = colors()
    parts = []
    for op, text in segments:
        if op == "removed":
            parts.append(f"<s>{text}</s>")
        elif op == "added":
            parts.append(f"<span style='background-color: {c['updated_bg']};"
                         f" color: {c['updated_fg']};'>{text}</span>")
        else:
            parts.append(text)
    return " ".join(parts)


def _blank_changes_html(removed, added, hints=()):
    """A cloze change that left the sentence's words alone said outright, naming the
    moved blanks and re-hinted deletions in bold. The filled words themselves did not
    change, so repeating the whole sentence would show a difference nowhere in it;
    which words are blanked, and what each one is hinted as, IS the change.

    A hint edit reaches here because both versions fill to the same sentence, and it
    has to be named for the same reason a moved blank does: the header line above
    shows the new hint, but nothing on the row would say it is what moved. Without it
    a hint-only edit fell through to the regrouped line below and described a change
    that had not happened.

    Bold rather than the cloze blue the answers used to carry: accent is also the
    colour of every clickable link in this body (Add note, Show yours), and a blue
    word that answers no click is an affordance lie. Inside the revealed old version
    the blue spans stay, since there they are card content rendered as the card
    renders it, not words in a summary line.
    """
    def named(answers):
        return ", ".join(f"<b>{a}</b>" for a in answers)

    def hint_said(answer, before, after):
        if not before:
            return f"hint added to <b>{answer}</b>: <b>{after}</b>"
        if not after:
            return f"hint dropped from <b>{answer}</b>: <b>{before}</b>"
        return f"hint on <b>{answer}</b>: <b>{before}</b> → <b>{after}</b>"

    parts = []
    if removed:
        parts.append(f"no longer blanked: {named(removed)}")
    if added:
        parts.append(f"newly blanked: {named(added)}")
    parts.extend(hint_said(*hint) for hint in hints)
    return " &middot; ".join(parts) or "the blanks were regrouped into different cards"


# The floor under a change's difflib-style similarity (word_diff_ratio) for the
# word-diff rendering: below it, the change reads as a rewrite and takes the summary
# line instead. 0.75 rather than difflib's conventional 0.6, because the bound that
# matters here is how much of the MERGED line ends up marked: at 0.6 similarity about
# 57% of the rendered line is struck or highlighted (markup outweighing message, the
# exact wall this floor exists to stop), while 0.75 caps it near 40%. Both design
# reviews asked for a majority-marked line to be unreachable, and this one constant
# is that guarantee.
_DIFF_MIN_SIMILARITY = 0.75


def _inline_change_html(detail, name, old, new):
    """One changed field as a single self-contained line, or None when the change is
    a rewrite that takes `_rewrite_row`'s summary-and-reveal treatment instead:

    - a field that used to be empty says so ("was empty");
    - a cloze Text whose words survived but whose blanks or hints moved names what
      moved (`cloze_answer_changes`, `cloze_hint_changes`), instead of reprinting the
      sentence;
    - plain prose on both sides renders as a single word-level diff (`_diff_html`),
      while the versions stay mostly the same text (`word_diff_ratio`).

    Everything else is a replacement rather than an edit: a rewritten paragraph, a
    restructured table, a swapped picture, a reworded cloze. None of those can be
    shown as one honest line, so they return None and the caller builds the widget
    that summarizes the change and holds the old version behind a click.
    """
    label = f"<b>{html.escape(name)}</b>&nbsp;&nbsp;"
    if not old:
        return label + "was empty"
    if _is_cloze(detail) and name == "Text":
        changes = cloze_answer_changes(old, new)
        if changes is not None:
            return label + _blank_changes_html(*changes, cloze_hint_changes(old, new))
        return None
    if _plain_prose(old) and _plain_prose(new):
        segments = merged_word_diff(old, new)
        if word_diff_ratio(segments) >= _DIFF_MIN_SIMILARITY:
            return label + _diff_html(segments)
    return None


def _rewrite_summary(old, new):
    """The receipt line's verb phrase for a replaced field: what happened and how
    much, without showing any of the changed text.

    "rewritten" carries the similarity verdict that routed the change here; the
    word-count delta is the only magnitude fact a glance needs, and it is appended
    only when the length genuinely moved (by a quarter or more), so a same-size
    rewrite stays one word. A field whose visible text vanished says "cleared", and
    one with no visible text to count (a swapped picture) says "replaced".
    """
    old_words = len(plain_text(old).split())
    new_words = len(plain_text(new).split())
    if not old_words:
        return "replaced"
    if not new_words:
        return "cleared"
    if new_words <= old_words * 0.75:
        return f"rewritten, shortened ({old_words} → {plural(new_words, 'word')})"
    if new_words >= old_words * 1.25:
        return f"rewritten, expanded ({old_words} → {plural(new_words, 'word')})"
    return "rewritten"


def _old_version_html(detail, name, old):
    """The old field rendered the way the card itself renders: structure kept,
    pictures resolved from the learner's own collection (they are her cards, already
    on disk), and a cloze's deletions filled in blue like the header line, never raw
    {{c1::…}} braces. Built on first reveal, not at batch-build time."""
    rendered = field_preview_html(old, image_html=_collection_image)
    if _is_cloze(detail) and name == "Text":
        rendered = cloze_filled_html(rendered, escape=False)
    return rendered


def _rewrite_row(detail, name, old, new, card_label):
    """A rewritten field as a one-line receipt with the old version behind a click:
    "Why  rewritten, shortened (104 → 66 words)   Show yours".

    The design both independent reviews converged on. The new version is already the
    card rendered above, so for the reader deciding Apply the old text is reference
    material, not content: the summary line is everything she needs at a glance, and
    the reader weighing Keep yours pays one click (the same flat-link idiom as Add
    note, worded in the decision button's own vocabulary) to see what "yours" says.

    The reveal is the old field verbatim and unmarked, behind a muted left rule that
    mirrors the green why rule's shape: same structural device, different palette
    role, so ruled block reads as "a whole explanation" and the colour says which
    era it belongs to. Built lazily on the first click, so a 50-row batch pays for
    one short label and one flat button per rewritten field and decodes nothing.
    """
    row = QWidget()
    lay = QVBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(2)

    line = QWidget()
    hlay = QHBoxLayout(line)
    hlay.setContentsMargins(0, 0, 0, 0)
    hlay.setSpacing(8)
    summary = _rich_label(f"<b>{html.escape(name)}</b>&nbsp;&nbsp;"
                          f"{_rewrite_summary(old, new)}")
    summary.setStyleSheet(f"color: {colors()['dim']};")
    # A one-line receipt by design, so it must not wrap: a word-wrapped QLabel asks
    # the layout for a "reasonable" width rather than its full text, and with the
    # link beside it the phrase folded onto three short lines in an all-but-empty row.
    summary.setWordWrap(False)
    hlay.addWidget(summary, 0)
    link = link_button("Show yours")
    # Collapsed to its text height, the same styling that keeps the caret compact:
    # without border and padding rules a flat button still carries the platform's
    # native button metrics (32px on macOS), which stretched every receipt line to
    # double its text and read as a band of dead space between the rows.
    link.setStyleSheet(f"color: {colors()['accent']}; font-size: 12px;"
                       " border: none; padding: 0;")
    link.setCursor(Qt.CursorShape.PointingHandCursor)
    hlay.addWidget(link, 0)
    hlay.addStretch()
    lay.addWidget(line)

    old_label = _rich_label("")
    old_label.setVisible(False)
    c = colors()
    # The border: none reset is load-bearing, same as the why rule: Qt ignores a lone
    # border-left on a QLabel unless the shorthand is set first.
    old_label.setStyleSheet(f"border: none; border-left: 3px solid {c['muted']};"
                            f" padding-left: 8px; color: {c['dim']};")
    lay.addWidget(old_label)

    def _name_link(showing):
        # Named for the card, like the caret and the decision buttons: a list of
        # forty links all called "Show yours" says nothing about which card's old
        # version any one of them opens.
        verb = "Hide yours" if showing else "Show yours"
        link.setText(verb)
        link.setAccessibleName(f"{verb}: {card_label}")

    # An explicit built flag rather than "is the label still empty": _rich_label
    # seeds every label with the preview <style> block, so its text is never empty
    # and an emptiness check would skip the build forever.
    built = []

    def _toggle(_checked=False):
        showing = not old_label.isVisible()
        if showing and not built:
            built.append(True)
            old_label.setText(_preview_style() + _old_version_html(detail, name, old))
        old_label.setVisible(showing)
        _name_link(showing)

    link.clicked.connect(_toggle)
    _name_link(False)
    return row


def _change_row(detail, name, card_label):
    """One changed field's row in the What changed group: a plain line for a change
    one line can state honestly (`_inline_change_html`), or the receipt-and-reveal
    widget for a rewrite (`_rewrite_row`)."""
    old = (detail.get("was") or {}).get(name) or ""
    new = _field(detail, name)
    inline = _inline_change_html(detail, name, old, new)
    if inline is None:
        return _rewrite_row(detail, name, old, new, card_label)
    label = _rich_label(inline)
    label.setStyleSheet(f"color: {colors()['dim']};")
    return label


def _changes_heading():
    """The small heading over the What changed group, so the dim lines under it read
    as one comparison block rather than as more of the card."""
    label = QLabel("What changed")
    label.setStyleSheet(f"color: {colors()['dim']}; font-size: 11px;"
                        " font-weight: 600; margin-top: 4px;")
    return label


def _change_note_html(note):
    """The row's account of why this card changed. Feedback is shown verbatim in
    quotes; the add-on cannot know which learner sent it, so the suffix says "from
    feedback", never "your feedback"."""
    text = html.escape(note.get("note", ""))
    if note.get("kind") == "feedback":
        return (f"<i>&ldquo;{text}&rdquo;</i>&nbsp;&nbsp;"
                f"<span style='color:{colors()['dim']}; font-size:11px'>"
                f"&middot; from feedback</span>")
    return f"<i>{text}</i>"


def _change_note_row(note, indent):
    """One `change_notes` entry as a row under the card header: a 3px accent bar (the
    same idiom as the Why rule, but in `updated_bg` rather than `why`) and the note's
    own text, in the row's text column so it reads as part of that card.

    Built on `muted_label` for its word-wrap and its base colour, with the stylesheet
    replaced rather than appended: Qt silently ignores a lone border-left on a QLabel
    unless the border shorthand is reset first (the same fix the Why rule in
    `_card_row` carries), so the reset has to come before border-left in this string.
    """
    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(indent, 0, 0, 0)
    lay.setSpacing(0)
    label = muted_label(_change_note_html(note))
    label.setTextFormat(Qt.TextFormat.RichText)
    c = colors()
    # The bar is decorative and deliberately quiet; the text carries the meaning, in
    # the measured `muted`/`dim` roles. `updated_bg` is borrowed here for its tie to
    # the UPDATED chip, not for its own contrast against the window.
    label.setStyleSheet(f"border: none; border-left: 3px solid {c['updated_bg']};"
                        f" padding-left: 8px; color: {c['muted']};")
    lay.addWidget(label)
    return row


def _chip_with_source(detail, chips):
    """The row's chip column, with the deck source's where-this-came-from label as a
    small tag directly under the chip when the card carries one.

    Under the chip rather than as a line in the text column, where it used to sit: a
    reference number in the flow of the card's own words read as part of the card,
    weirdly placed under whichever line happened to be above it. The chip column is
    already the row's metadata gutter (NEW, UPDATED), and a question ID is the same
    kind of fact about the card, so it stacks there and the text column stays purely
    the card. One tag per whitespace-separated reference, so a card drawn from two
    questions reads as two small lines rather than one overflowing the column.

    The string is the deck source's, rendered as plain text: never parsed here, and
    escaped rather than trusted, since it arrives from a fetched manifest.
    """
    cell = chip_cell(_row_chip(detail), chips)
    label = (detail.get("card_source") or "").strip()
    if not label:
        return cell
    wrap = QWidget()
    lay = QVBoxLayout(wrap)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(1)
    lay.addWidget(cell)
    dim = colors()["dim"]
    for ref in label.split():
        tag = QLabel(html.escape(ref))
        tag.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tag.setStyleSheet(f"color: {dim}; font-size: 10px;")
        lay.addWidget(tag)
    # The chip column's own fixed width, so a tag can never widen the gutter and
    # push every row's text out of line.
    wrap.setFixedWidth(chip_column_width(chips))
    return wrap


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


_NEW_OPTIONS = [("import", "Import"), ("skip", "Skip"), ("never", "Never")]
# The changed row's three options mirror the new row's, and "frozen" is Never's
# counterpart here. It is a state of its own rather than "never" reused, because the two
# mean different things once the card is in her collection and the Declined cards screen
# has to be able to say which is which: Never imported, against a card she keeps whose
# updates she has turned off. The button says Never on both kinds; what it turns down is
# whatever that row is offering, and the caption on the click spells that out.
_CHANGED_OPTIONS = [("apply", "Apply"), ("keep", "Keep yours"), ("frozen", "Never")]
_DEFAULT_DECISION = {"new": "import", "changed": "apply"}

# Where a declined card actually comes back from, spelled the same way in both captions.
# A skipped card's deck is still recorded installed at the new version, so it is only
# re-offered when the source ships another version of that deck: on a deck that rarely
# changes, "next update" was a promise nothing here can keep, and Declined cards is the
# one way back that does not depend on the source shipping anything.
_BACK_FROM_DECLINE = ("offered again the next time this deck changes, or any time from "
                      "Manage decks → Declined cards")
_DECLINE_CAPTION = {
    "skip": f"Set aside. It's {_BACK_FROM_DECLINE}.",
    "keep": f"Your card stays as it is. The change is {_BACK_FROM_DECLINE}.",
    # No "offered again" clause on purpose: unlike the two above, this one is not a
    # not-this-time. Manage decks → Declined cards is the only way back, and saying so
    # is what keeps it from reading as the same soft decision with a different label.
    "frozen": ("Your card stays as it is, and changes to it won't be offered again. "
               "Undo from Manage decks → Declined cards."),
}

# Nothing is transmitted from here: a note is folded into the digest at the end of the
# run, copied to the clipboard, and pasted by the reader herself. The placeholder used
# to say it was sent to the deck author, which is the one thing it is not, and asked
# what was wrong with the card, on a box the neutral Add note link opens too.
_FEEDBACK_PLACEHOLDER = ("Anything to pass on about this card? You'll get a copy at the "
                         "end to paste and send.")


# Every decision that turns a card down, which is every decision worth offering the
# note box for on the spot. Wider than _DECLINE_CAPTION, which is only the two that
# earn a caption saying where the card comes back from; Never is a decision about the
# card rather than about this offer, so it has nothing to come back from and still has
# the most to explain.
_TURNED_DOWN = frozenset({"skip", "keep", "never", "frozen"})

_DECLINE_CHIP = {"skip": "skipped", "keep": "kept", "frozen": "kept"}


def _row_chip(detail):
    """The one chip a card row wears.

    A re-offered decline shows that decline rather than its kind: Skip is only ever
    offered on a card the collection doesn't have and Keep yours only on a change to
    one it does, so SKIPPED / KEPT YOURS says which kind the row is as well as what was
    already decided about it. Stacking both (and, on a card changed since that decline,
    an UPDATED beside them) cost three fixed-width columns plus the decision control,
    which at the dialog's own 660px floor left the card's own words about 150px: the
    exact crush the wider dialog had just undone, on the rows most worth reading. What
    the third pill said is said better by the hint line under the header, which names
    the decline it changed since.
    """
    return _DECLINE_CHIP.get(detail.get("declined_state")) or detail.get("kind")


def _card_label(detail):
    """The card's own short name, for a control that would otherwise announce itself
    by its bare verb. Same helper every row that names a card already uses, at a length
    that reads as a label rather than as the card."""
    return note_display_label([v for _, v in detail.get("fields", [])], max_len=60)


def _card_row(detail, flags, boxes, decisions, on_decide, resolve=None, chips=None):
    """One card as a single row: a caret, its one chip column (see `_row_chip`), its
    tag if it has one, and its primary line. Clicking the row (the caret or the line
    itself) reveals the answer, the why behind a green left rule, and dosing when
    present. A "new" or
    "changed" row also carries a segmented decision control (widgets.decision_cell) at
    the right of its header, defaulting to Import/Apply; choosing Skip/Keep reveals a
    feedback box for what the learner makes of it, and Never collapses the row with a
    struck-through primary line. Any row whose box is closed, a re-offered decline
    included, can open that same box with its own quiet "Add note" link, at the end of
    the expanded body, so feedback is never gated behind a decline; it costs the same
    click as reading the card would.

    Pictures are named until that first expand and rendered from then on. Extraction is
    what opening a row pays for, so a long list stays cheap to scroll and a review nobody
    opens costs nothing at all.

    A `card_source` label renders as a small tag under the row's chip (see
    `_chip_with_source`), and any `change_notes` entries render directly under the
    header, visible whether the row is collapsed or open: the deck source's own
    account of why the card changed (see `_change_note_row`).
    """
    guid = detail["guid"]
    kind = detail.get("kind")
    card_label = _card_label(detail)
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
    # The What changed group needs no rerender list of its own any more: its inline
    # lines are text-only by construction (the plain-prose gate), and a rewrite's old
    # version, the one place a picture can appear, is rendered at reveal-click time
    # (see _rewrite_row), which is even later than the first expand.
    revealed = []
    # The rows _change_note_row builds for this card, kept local so
    # _apply_decision_visuals can hide them under Never and restore them if the
    # decision moves off Never again; they don't exist yet the first time that
    # function runs, but a predeclined "never" never reaches this row at all
    # (sync.py drops it before it's built), so the initial call has nothing to hide.
    # The card_source tag is not one of them: it lives in the chip column and stays
    # visible under Never, since it names the card the struck line is about.
    note_rows = []

    def _reveal_images():
        if revealed:
            return
        revealed.append(True)
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

    def _name_caret(expanded):
        """What the caret is called to anyone not reading the glyph: a screen reader
        announced it as "▸", and a hovering reader got nothing at all. Kept in step with
        the direction it points, since that is the only other thing saying which way it
        will go, and carrying the card's own name, since a list of three hundred carets
        all called "Show card" says nothing about which card any one of them opens.
        """
        verb = "Hide card" if expanded else "Show card"
        # The card's name goes to the accessible name and not to the tooltip: a reader
        # hovering this glyph already has the row's own front an inch to its right,
        # while a screen reader moving between carets has nothing else at all.
        caret.setAccessibleName(f"{verb}: {card_label}")
        caret.setToolTip(verb)

    def _toggle():
        expanded = not body.isVisible()
        if expanded:
            _reveal_images()
        body.setVisible(expanded)
        caret.setText(_CARET_OPEN if expanded else _CARET_CLOSED)
        _name_caret(expanded)

    header = QWidget()
    hlay = QHBoxLayout(header)
    hlay.setContentsMargins(0, 0, 0, 0)
    hlay.setSpacing(CARET_GAP)

    caret.setFlat(True)
    # Unconstrained, this is a real push button at its platform minimum (~80px on
    # macOS) around a 6px glyph, which is a wide dead gutter down the whole list.
    caret.setFixedWidth(CARET_W)
    # Stronger and heavier than the dim text beside it, at the same width: this glyph is
    # the only thing on the row saying it opens, and it used to read as punctuation.
    # Contrast and weight are the whole lever here. A wider gutter would move every
    # column on the screen, and a larger glyph would outshout the card it belongs to.
    caret.setStyleSheet(f"border: none; padding: 0; font-weight: 600;"
                        f" color: {colors()['caret']};")
    caret.setCursor(Qt.CursorShape.PointingHandCursor)
    # Left in the tab order deliberately, one stop per row and all. ClickFocus would cut
    # a 300-row list down to no tab stops at all, but the row's own label is a QLabel and
    # takes no focus, so it would also leave no way to open a card from the keyboard.
    caret.clicked.connect(_toggle)
    _name_caret(False)
    hlay.addWidget(caret, 0, Qt.AlignmentFlag.AlignTop)

    # Every column this row draws in front of its primary label, collected rather than
    # counted twice: the expanded body indents by exactly these (see `indent` below),
    # so a column added here can never leave the body hanging left of its own line.
    # Top-aligned like the caret: the chip marks the row, so it belongs beside the
    # first line of a wrapping one rather than centred against the whole block. The
    # decline keys `_row_chip` reads are only ever set by the caller that already knows
    # about the decline registry; nothing here writes them.
    declined_state = detail.get("declined_state")
    changed_since_decline = bool(detail.get("changed_since_decline"))
    leading = [_chip_with_source(detail, chips)]
    for cell in leading:
        hlay.addWidget(cell, 0, Qt.AlignmentFlag.AlignTop)
    indent = row_text_indent(len(leading), chips)

    primary = _ClickableLabel(_row_html(detail), _toggle)
    primary.setWordWrap(True)
    primary.setTextFormat(Qt.TextFormat.RichText)
    primary.setCursor(Qt.CursorShape.PointingHandCursor)
    hlay.addWidget(primary, 1)

    # The feedback box and its caption are built for every row, whatever its kind, but
    # stay invisible until something earns them: an existing note carried over in
    # `flags`, a decline already on the card, or a click on Skip/Keep/Add note below.
    caption = muted_label("")
    caption.setVisible(False)
    box = QPlainTextEdit(flags.get(guid, ""))
    box.setPlaceholderText(_FEEDBACK_PLACEHOLDER)
    box.setFixedHeight(50)
    # A note already written is what opens this, not a decline the reader made in some
    # earlier run: ten re-offered skipped cards used to arrive as ten empty boxes
    # parked open, which is the state _apply_decision_visuals goes out of its way to
    # avoid everywhere else. Add note (below) is what reopens one on a row like that.
    box.setVisible(bool(flags.get(guid)))
    boxes[guid] = box

    default = _DEFAULT_DECISION.get(kind)
    never_note = hint_label("won't be offered again")
    never_note.setVisible(False)
    add_note = link_button("Add note")
    add_note.setVisible(False)

    def _reveal_box(_checked=False):
        box.setVisible(True)
        add_note.setVisible(False)
    add_note.clicked.connect(_reveal_box)

    def _apply_decision_visuals(state, clicked=False):
        declined = state in _DECLINE_CAPTION
        caption.setVisible(declined)
        if declined:
            caption.setText(_DECLINE_CAPTION[state])
        # Sticky once there's something to lose (a saved flag or typed-but-unsaved
        # text), but a decline back to default with nothing written in it closes
        # again, restoring the quiet Add note affordance rather than leaving an
        # empty box parked open. `clicked` is what keeps that rule true of a decline
        # the row opened already carrying: declining here and now is a moment worth
        # offering the box for, while a decline made in an earlier run is not.
        has_note = bool(flags.get(guid)) or bool(box.toPlainText().strip())
        show_box = has_note or (state in _TURNED_DOWN and clicked)
        box.setVisible(show_box)
        # Offered whenever the box is closed, whatever the row decided: a re-offered
        # decline is not a default row, and gating this on the default left exactly
        # those rows with no way to write a note at all.
        add_note.setVisible(not show_box)
        # Struck through only for Never, which is a card she will not have. A frozen
        # row keeps a card she does have, so striking it would say the opposite of
        # what happened.
        gone = state == "never"
        font = primary.font()
        font.setStrikeOut(gone)
        primary.setFont(font)
        never_note.setVisible(state in ("never", "frozen"))
        for note_row in note_rows:
            note_row.setVisible(not gone)
        if gone:
            body.setVisible(False)
            caret.setText(_CARET_CLOSED)
            _name_caret(False)
        # The box lives outside the body, so a Never row keeps it on screen while
        # collapsing everything else. That is the whole reason Never can be offered a
        # note at all: the row folds away and the one thing left is the question of
        # why. It used to hide the box and the link both, which made the loudest
        # decision in the dialog the only silent one.

    if kind in _DEFAULT_DECISION:
        options = _NEW_OPTIONS if kind == "new" else _CHANGED_OPTIONS
        # A predeclined detail's `decisions` entry is seeded by build_update_body,
        # before any row (this one included) is ever built, so it just reads back
        # here like any other decision.
        initial = decisions.get(guid, default)

        def _on_change(state):
            if state == default:
                decisions.pop(guid, None)
            else:
                decisions[guid] = state
            _apply_decision_visuals(state, clicked=True)
            on_decide(guid, state)

        hlay.addStretch()
        hlay.addWidget(decision_cell(options, initial, _on_change, card_label),
                       0, Qt.AlignmentFlag.AlignTop)
        hlay.addWidget(never_note, 0, Qt.AlignmentFlag.AlignTop)
        _apply_decision_visuals(initial)

    outer.addWidget(header)

    for note in detail.get("change_notes") or []:
        note_row = _change_note_row(note, indent)
        note_rows.append(note_row)
        outer.addWidget(note_row)

    if changed_since_decline:
        since = ("since you skipped it" if declined_state == "skip"
                else "since you kept yours")
        # In the row's own text column, like the body below it: this line is about the
        # card, and drawn from the row's left edge it started left of even the caret.
        hint_row = QWidget()
        hint_lay = QHBoxLayout(hint_row)
        hint_lay.setContentsMargins(indent, 0, 0, 0)
        hint_lay.setSpacing(0)
        hint_lay.addWidget(muted_label(f"Changed {since}. Worth another look."))
        outer.addWidget(hint_row)

    body.setVisible(False)
    blay = QVBoxLayout(body)
    # Every column in front of the primary label, so an expanded body lines up under
    # the line it belongs to rather than under the caret or the chip. Counted from
    # `leading` above rather than assumed to be one chip: a second column in front of
    # the label leaves every body hanging a chip-width left of its own text.
    blay.setContentsMargins(indent, 2, 0, 2)
    blay.setSpacing(4)

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

    # Everything this update rewrites, as one quiet group after the card rather than a
    # `was` line wedged under each block. Interleaved, the body read as an alternation
    # of card and ghost (why, then old why; note, then old text) and never as either
    # one whole; this way the card above reads clean, and the group below states each
    # field's delta once, in the note type's own field order. Field-name labels are
    # what map a line back to its block, doing the job adjacency used to do.
    changed_names = _changed_field_names(detail)
    if changed_names:
        blay.addWidget(_changes_heading())
        for name in changed_names:
            blay.addWidget(_change_row(detail, name, card_label))

    # Last in the body, past every field block: reading down to it is the same click
    # that opens the card at all, which is what keeps this quiet rather than a header
    # column competing with the decision control for attention.
    if kind in _DEFAULT_DECISION:
        blay.addWidget(add_note)

    outer.addWidget(body)
    outer.addWidget(caption)
    outer.addWidget(box)
    return row


def _chip_kinds(items):
    """The chip kinds a list can actually paint, read off the rows it was handed.

    Both item vocabularies at once (build_list_body's ("row", kind, ...) and
    build_update_body's card/retired/moved rows), since both hand their answer to the
    same chip_cell. A screen's column is measured against this rather than against
    every chip the add-on has, so Sync decks, Reconcile and Manage decks stop reserving
    room for a decline chip none of them can ever show (see widgets.chip_column_width).
    Derived rather than declared by each caller: the rows are already in hand, and a
    declared set is one more thing to keep in step with what a screen actually builds.
    """
    kinds = []
    for item in items:
        if item[0] == "card":
            kinds.append(_row_chip(item[2]))
        elif item[0] == "row":
            kinds.append(item[1])
        elif item[0] in ("retired", "moved"):
            kinds.append(item[0])
    return tuple(dict.fromkeys(k for k in kinds if k))


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


def build_update_body(items, sources, flags, new_index, decisions,
                      top_html, status_line, safety_html, touched=None):
    """The Update my decks screen's body: fixed summary text, the streaming list of
    pending new and changed cards plus any retired or relocated ones, then the
    status line and the safety note below it. `internpearls.ui._ask_with_widget`
    wraps whatever this returns with the dialog's title and its Update/Cancel buttons,
    the same as any other confirmation.

    `items` is a mix of ("header", text), ("note", html), ("sep",), ("deck", deck_short,
    counts), ("card", deck_name, detail), ("retired", identity), and ("moved", front,
    dest_deck_short) entries, one per row, built by sync.py from every deck's new and
    changed cards (_gather_pending_items) and from the retired/relocated cards it finds
    pending (_retired_moved_items). The first three are the shapes build_list_body takes
    too, and are drawn by the same builder: a header groups a run of rows, a note is the
    sentence introducing one, and a sep draws the hairline between two rows. A "deck"
    row is the per-deck summary that opens the list, in a section of its own where
    nothing is ever chipped and nothing ever expands, so it declines the caret and chip
    columns (see simple_row) and its deck names share a left edge with the heading
    directly above them. Alignment is decided per section, by whether anything in that
    section is chipped, which is why the card sections below keep the columns their own
    unchipped rows would otherwise not need. A "retired" or "moved" row renders through
    widgets.simple_row rather than `_card_row`: single-line and never expanding, since
    a retired or relocated card is known only by its front (or identity) and a deck,
    with nothing more to read out of the collection for it. `sources` is
    {deck_name: .apkg path}, threaded straight into build_resolvers so a row's picture
    extracts from the same already-downloaded file this screen read the rest of the
    card from.

    `flags`/`new_index` are the {guid: note}/{guid: (deck, front)} maps update_decks()
    already carries through the run. A row's box writes into `flags` live, on every
    keystroke, since there is no separate closing moment: the rows sit on the
    confirmation itself. `decisions` is the matching live map for a row's segmented
    control, {guid: state}; a row writes into it the same way, sparsely (a guid absent
    from the dict means that row's default, Import or Apply).

    `status_line()` is called fresh on every keystroke and every decision change to
    recompute the line shown below the list (flag counts, decision tallies, whatever
    the caller wants there); `safety_html` is fixed.

    `touched`, when given, collects every guid whose decision control she actually
    clicked this run, even a click that lands back on the row's own default or
    reaffirms the state it was already showing. update_decks() reads this back after
    the dialog closes to tell an active re-decline (which should refresh the stored
    hash) and an active un-decline on a kind-flipped row (which should remove the
    entry) apart from a row simply left as the confirmation seeded it.

    Returns (widget, boxes, flush). `boxes` is {guid: QPlainTextEdit}, built lazily as
    the list's own rows are. `flush()` stops the debounce save timer, writes one final
    unconditional copy of what's flagged to disk, and releases the temporary directory
    pictures were extracted into; the caller runs it once, right after the dialog this
    body sits in has closed.
    """
    resolvers, media_dir = build_resolvers(sources)
    boxes = {}
    carried = load_saved_feedback()
    touched = touched if touched is not None else set()

    # A predeclined card's `decisions` entry has to exist before any row is ever
    # built, not just once its own row happens to render: StreamingList only builds
    # the first batch up front and the rest on scroll, so seeding this inside
    # _card_row would leave a predeclined card past the first batch permanently
    # missing from the dict a caller reads back. One pass over `items` costs nothing
    # (no widgets), and only ever writes a state that row's own control can actually
    # express, so a value the control couldn't show never silently reaches the
    # persisted dict either.
    for item in items:
        if item[0] != "card":
            continue
        d = item[2]
        state = d.get("declined_state")
        kind = d.get("kind")
        default = _DEFAULT_DECISION.get(kind)
        options = {v for v, _ in (_NEW_OPTIONS if kind == "new"
                                  else _CHANGED_OPTIONS if kind == "changed" else ())}
        if state and state != default and state in options:
            decisions.setdefault(d["guid"], state)

    def _entries():
        return pending_entries(boxes, flags, new_index, carried)

    body = QWidget()
    lay = QVBoxLayout(body)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(8)

    # Skipped entirely when there is nothing to say. Now that the per-deck summary is
    # the list's own first section, a routine update often has no fixed text above the
    # list at all, and an empty label still claims a line's height plus the layout's
    # spacing, which reads as the list having been nudged down for no reason.
    if top_html:
        lay.addWidget(_rich_label(top_html))

    # The safety note is standing reassurance, not this run's news, so it renders as
    # small print: at body size it was the tallest block on the screen, taking height
    # the list itself is here for. The status line above it keeps the body size, since
    # it reports what she just did.
    safety_html = (f"<span style='color: {colors()['muted']}; font-size: 11px;'>"
                   f"{safety_html}</span>")
    bottom = _rich_label(status_line() + safety_html)

    saver = QTimer(body)
    saver.setSingleShot(True)
    saver.setInterval(400)
    saver.timeout.connect(lambda: save_feedback(_entries()))

    def _refresh_bottom():
        bottom.setText(_preview_style() + status_line() + safety_html)

    def _on_change(guid, box):
        note = box.toPlainText().strip()
        if note:
            flags[guid] = note
        else:
            flags.pop(guid, None)   # she cleared it; treat that as unflagging
        _refresh_bottom()
        saver.start()

    def _on_decide(guid, state):
        touched.add(guid)
        _refresh_bottom()

    # Measured once for the whole screen, not per row: every row in one list has to be
    # given the same chip set or the column stops lining up.
    chips = _chip_kinds(items)

    def _row(item):
        if item[0] in ("header", "note", "sep"):
            return _list_row(item, chips=chips)
        if item[0] == "deck":
            _, deck_short, counts = item
            return simple_row(None, deck_short, counts, card_columns=False)
        if item[0] == "retired":
            return simple_row("retired", item[1], chips=chips)
        if item[0] == "moved":
            _, front, dest_short = item
            return simple_row("moved", front, f"→ {dest_short}", chips=chips)
        _, deck_name, detail = item
        row = _card_row(detail, flags, boxes, decisions, _on_decide,
                        resolve=resolvers.get(deck_name), chips=chips)
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


def _list_row(item, card_columns=True, chips=None):
    """One entry of `build_list_body`'s item list as a widget. See that function for
    what each shape means, and for what `card_columns` decides. `chips` is the whole
    list's own chip set (see _chip_kinds), passed on to every row that draws one."""
    if item[0] == "header":
        return section_header(item[1])
    if item[0] == "note":
        label = _rich_label(item[1])
        # The same top margin section_header carries, for the same reason: a note opens
        # a group, and without it a group's first line sits as close to the last row of
        # the group above as to its own rows.
        label.setStyleSheet("margin-top: 14px;")
        return label
    if item[0] == "sep":
        return _separator()
    _, kind, primary_html, trailing_html = item
    return simple_row(kind, primary_html, trailing_html, card_columns=card_columns,
                      chips=chips)


def append_rows(items, rows):
    """Add one group's rows to a build_list_body item list, hairlined between rather
    than around: a rule above the first row would cut the group off from the sentence
    that introduces it."""
    for i, row in enumerate(rows):
        if i:
            items.append(("sep",))
        items.append(row)


def build_list_body(items, top_html="", bottom_html="", card_columns=True):
    """A confirmation whose body is a list of rows: fixed text above it, the streaming
    list itself, fixed text below. `internpearls.ui._ask_with_widget` wraps whatever
    this returns with the dialog's title and its buttons, the same as build_update_body.

    Sync decks and Reconcile my decks build from this. Neither shows a card that opens
    into more, so neither needs `_card_row`, the feedback boxes or the picture
    resolvers build_update_body threads through. What they do need is the same rows,
    the same hairlines and the same streaming as the screen they each run half of, so
    they share the list rather than each hand-rolling a container that would drift from
    it.

    `items` entries:

      ("header", text)                    a bold heading over the rows below it
      ("note", html)                      a paragraph reading with the rows below it
      ("sep",)                            the hairline between two rows
      ("row", kind, primary, trailing)    one row, marked by `kind` (widgets.CHIPS),
                                          or by nothing when `kind` is None

    `card_columns` is the caret-and-chip-column decision widgets.simple_row documents,
    made once for the whole list because it is a decision about the screen rather than
    about any one row. A mixed list (Sync decks marks a deck NEW or UPDATED, Reconcile
    marks a card RETIRED or MOVED) keeps the columns, so its chipped and unchipped rows
    read as one column of text. A list where every row is the same sort of thing has
    nothing to line up against, so it declines them and starts flush with the heading
    above it: Clean up duplicates and Remove empty cards each list one kind of card and
    pass False.
    """
    body = QWidget()
    lay = QVBoxLayout(body)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(8)
    if top_html:
        lay.addWidget(_rich_label(top_html))
    chips = _chip_kinds(items)
    lay.addWidget(
        StreamingList(lambda item: _list_row(item, card_columns, chips), items), 1)
    if bottom_html:
        lay.addWidget(_rich_label(bottom_html))
    return body


def _summary_block(title, items):
    """The run's own outcome as widgets: its headline at the dialog's largest size, then
    one row or paragraph per item in the same vocabulary build_list_body takes.

    The rows carry no chip and nothing here expands, so they decline the caret and chip
    columns (see widgets.simple_row): with nothing on this screen to line up against,
    reserving them would float every outcome line to the right of the heading above it
    and the backup line below it.

    Not a StreamingList: every caller's item count is bounded by how many decks the run
    touched, so there is no long list to build lazily, and both callers want this block
    to size to its own content inside whatever scroll area they put it in.
    """
    summary = QWidget()
    slay = QVBoxLayout(summary)
    slay.setContentsMargins(0, 0, 0, 0)
    slay.setSpacing(0)
    slay.addWidget(title_label(title))
    for item in items:
        slay.addWidget(_list_row(item, card_columns=False))
    return summary


def _scrolled(widget, max_height):
    """`widget` in a frameless scroll area that stops growing at `max_height`.

    Sized Preferred rather than the QScrollArea default of Expanding, for the reason
    ui._ask_scrollable spells out: Expanding claims every pixel of leftover height, so a
    two-line summary would be stretched to the full cap with blank space inside it.
    """
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setMaximumHeight(max_height)
    scroll.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
    scroll.setWidget(widget)
    return scroll


def show_result(title, items):
    """The end of a run with nothing flagged: the summary on its own, acknowledged and
    closed.

    A real dialog rather than the _info box this used to be, for the same reason
    ui._ask_scrollable exists: a message box has no scroll area, so a run reporting a
    deck per line plus a list of collided cards just grew the box, and a long enough one
    put its own OK button past the bottom of the screen. It also means the one thing the
    reader is being told about the run reads in the row vocabulary the confirmation she
    just answered was built from, instead of as a `<ul>` dropped into a label.
    """
    body = QWidget()
    lay = QVBoxLayout(body)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(_scrolled(_summary_block(title, items), 420))
    # Leftover height collects here, below the summary and above the button, rather
    # than being spread through the gaps between the rows.
    lay.addStretch()
    _ask_with_widget(body, yes_label="OK", no_label=None, min_width=460, min_height=0)


# What a run that stopped before it changed anything says, so the two places that can
# happen (cancelling the preview download, and cancelling the retry after the
# confirmation) say it the same way rather than one of them saying nothing at all.
NOTHING_CHANGED = "Update cancelled, nothing was changed."


def show_result_with_feedback(title, items, entries, nothing_note=""):
    """The end of a run, as one dialog instead of two.

    A completion summary and a feedback digest used to arrive as separate boxes, back
    to back, at the exact point in the run where the reader is most done paying
    attention: the summary lands first, gets dismissed, and the digest, the one thing
    she cannot reproduce by running the update again, appears behind it looking like
    yet another popup. They are one dialog now, summary on top, digest below it.

    `title` is the run's own headline ("Update complete (source: X)" or "Update stopped
    early (source: X)"); `items` is everything below it in build_list_body's own
    vocabulary, so an outcome per deck reads as a row, the preserved-field and collision
    notes read as paragraphs, and the cards a collision names read as rows of their own.

    Degrades in both directions on purpose: a run with no flagged cards is the summary
    alone, and a digest with no summary (she backed out of the update but still wrote
    notes) is the digest on its own.

    `nothing_note` is the one sentence to show when there is neither: a run that
    stopped with nothing to summarize and nothing flagged used to return in silence,
    so cancelling after clicking Update looked exactly like the click having done
    nothing at all. A plain _info rather than the summary dialog, since a headline with
    no rows under it is a sentence, not a summary. Left empty by a caller whose own
    exit needs no announcing (declining the confirmation, where Cancel says it).
    """
    if not entries:
        if title:
            show_result(title, items)
        elif nothing_note:
            _info(nothing_note)
        return
    offer_feedback_digest(None, entries, title=title, items=items)


def _digest_heading(entries):
    """What the digest is a digest of, counted as two separate things.

    An entry carries a note, a decision, or both (logic.feedback_entries builds one for
    a decision with nothing written on it too), and calling all of them flagged told a
    reader who skipped two cards and flagged none that she had flagged two. Each half
    is named only when it has something to count.
    """
    flagged = sum(1 for e in entries if (e.get("note") or "").strip())
    decided = sum(1 for e in entries if e.get("decision"))
    parts = [text for text, n in ((f"{plural(flagged, 'card')} flagged", flagged),
                                  (f"{plural(decided, 'decision')} recorded", decided))
             if n]
    return ", ".join(parts) or plural(len(entries), "card")


def offer_feedback_digest(parent, entries, title=None, items=()):
    """Put the flagged-card summary on the clipboard and show it.

    Shown as well as copied, for two reasons: she sees exactly what's being sent before
    she sends it, and a clipboard that silently didn't take (a mocked or headless Qt)
    costs a manual select-and-copy instead of costing her the notes she wrote. Read-only
    and scrollable rather than an _info box, since this text is meant to be selected and
    can run past a message box's height with nothing to grab. Monospaced and styled as a
    payload block, since it's indent-structured plain text, not prose. Copy again is the
    recovery if something else lands on the clipboard before she gets to paste.

    `title`/`items` are the end-of-run summary, drawn by _summary_block in the same
    title/row vocabulary the confirmation this dialog follows already uses. Left at
    their defaults for a bare digest with no summary at all (she backed out of the
    update but still flagged a card), which is why the whole block is skipped when
    `title` is empty rather than rendered with a blank heading.
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
    if title:
        # The run's own outcome, at the dialog's largest size: it is what the whole
        # screen is reporting. The flagged-card heading below is subordinate to it and
        # says so by being smaller, which is the way round these two used to read.
        lay.addWidget(_scrolled(_summary_block(title, items), 200))
    lay.addWidget(section_header(_digest_heading(entries)))
    # Nothing here sends anything, and this is the last screen that can say so before
    # the notes are cleared: "copied, ready to paste" left it possible to read the
    # digest, close it, and assume the deck author now had it.
    lay.addWidget(muted_label(
        "Nothing is sent automatically. This is on your clipboard: paste it into a "
        "message to the deck author."
        if copied else
        "Nothing is sent automatically. Select and copy the text below, then paste it "
        "into a message to the deck author."))
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
    dlg.deleteLater()   # parented to mw otherwise, which owns it until Anki quits
