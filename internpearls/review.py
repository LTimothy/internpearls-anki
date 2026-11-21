"""The update screen's card rows, the end-of-run summary, and the feedback digest.

Separate from dialogs.py to avoid an import cycle: dialogs.py imports sync.py, and
sync.py's update flow builds this.

Presentation only: it reads note fields sync.py pulled from a downloaded .apkg and
returns what the learner typed. Card notes and feedback digests are saved under
user_files/; collection media is read only. No network.
"""
import datetime
import hashlib
import html
import os
import tempfile

from aqt import mw
from aqt.utils import getSaveFile, tooltip
from aqt.qt import (QFontDatabase, QFrame, QHBoxLayout,
                    QImage, QLabel, QPlainTextEdit, QPushButton, QScrollArea,
                    QSizePolicy, Qt, QTimer, QVBoxLayout, QWidget)

from . import config
from .config import (ADDON_VERSION, APP_NAME, FEEDBACK, _collection_state_path, _load_json,
                     _save_json)
from .logic import (FILTER_MIN_CARDS, FILTER_MODES, KeptEdit, apkg_media_index,
                    build_feedback_digest, cloze_answer_changes, cloze_filled_html,
                    cloze_hint_changes, count_cards, extract_apkg_media,
                    field_image_names, field_preview_html, field_preview_text,
                    filter_update_items, group_run, holdable_guids, later_migration_line,
                    merged_word_diff, note_display_label, plain_text, plural, truncate,
                    word_diff_ratio)
from .palette import colors
from .ui import (_ask_with_widget, _info, _safe, _warn, copy_to_clipboard, hint_label, link_button,
                 muted_label, row_rule, title_label)
from .widgets import (CARET_GAP, CARET_W, FilterBar, StreamingList, chip_cell,
                      chip_column_width, decision_cell, row_text_indent,
                      section_header, simple_row)

# The learner's own annotation space, empty in every spec.
_SKIP_FIELDS = {"Notes"}

# Fields with their own treatment rather than generic prompt/answer content.
_STRUCTURAL_FIELDS = {"Why", "Dosing", "Tag", "Image"}

# Minimum opening height of a build_list_body confirmation; a longer list grows the
# dialog, then scrolls. Here rather than in sync.py because collection.py needs it and
# cannot import sync.py.
_CONFIRM_HEIGHT = 380

# A change group bigger than this folds behind its header. Above three so the demo's
# three-member fixture still renders its members (browser_tests/parity.spec.mjs).
_GROUP_COLLAPSE_MIN = 5

# How many member fronts a folded group names under its header.
_GROUP_PREVIEW = 3

# A group's member indent: the note's 3px bar plus 8px padding.
_GROUP_MEMBER_INDENT = 11


def _preview_style():
    """The <style> block every label holding card HTML carries, matching the deck's CSS.
    Qt rich text takes only a subset of CSS, so this stays to borders, padding and
    colour.

    A function, not a constant: palette colours follow Anki's theme at call time.

    The `a` rule is needed because Qt paints an anchor in its built-in link colour,
    ignoring the widget's.
    """
    c = colors()
    return (
        "<style>"
        f".cloze {{ color: {c['accent']}; font-weight: 600; }}"
        # The group number takes the deletion's colour at normal weight. No font-size on
        # top of <sup>: it renders unreadably small.
        " .cn { font-weight: 400; }"
        # A deletion's hint is the prompt, not the fact, so it is dim rather than the
        # answer's blue.
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

# The row body's usable width at the dialog's 660px minimum. Wider pictures scale down
# to it; smaller ones are left alone.
_IMAGE_MAX_W = 540

# Kept alive while the module is loaded: a QLabel reads the thumbnail path after
# `_svg_thumbnail` returns. Thumbnails are named by a hash of the absolute source path.
_SVG_THUMBNAIL_DIR = None


def _svg_thumbnail_path(svg_path):
    global _SVG_THUMBNAIL_DIR
    if _SVG_THUMBNAIL_DIR is None:
        _SVG_THUMBNAIL_DIR = tempfile.TemporaryDirectory(prefix="internpearls-svg-")
    absolute = os.path.abspath(svg_path)
    name = hashlib.sha256(os.fsencode(absolute)).hexdigest() + ".png"
    return os.path.join(_SVG_THUMBNAIL_DIR.name, name)


def _svg_thumbnail(svg_path):
    """Rasterize an SVG to a PNG thumbnail on a white background. QImage(path) sizes a
    root with no absolute width/height against a default viewport rather than the
    viewBox, so the add-on renders it itself.

    Returns a PNG path under the add-on's private temporary directory, or None if
    PyQt6.QtSvg isn't available or the file doesn't parse as SVG. Collection media is
    read only: nothing is written beside the source.
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
    png_path = _svg_thumbnail_path(svg_path)
    return png_path if image.save(png_path, "PNG") else None


def _image_tag(local_path):
    """One extracted picture as an <img> for Qt rich text, scaled down to the row width
    but never enlarged. None when Qt cannot decode the file.
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
    """A field_preview_html resolver bound to one deck. Extracts lazily, once per
    picture per dialog.
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
    TemporaryDirectory they extract into. The caller must call the directory's
    .cleanup() once the screen has closed.
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
    holds text, plus any flag with no box here.

    `carried` is load_saved_feedback()'s record: the deck/front fallback for a flagged
    guid with neither a box nor an `index` entry, such as a note from an earlier session
    on a card not shown this time.
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
    """A QLabel that toggles its row open when clicked."""

    def __init__(self, text, on_click):
        super().__init__(text)
        self._on_click = on_click

    def mousePressEvent(self, event):
        self._on_click()


def _field(detail, name):
    return next((v for n, v in detail.get("fields", []) if n == name), "")


def _as_imported(detail):
    """`detail` with each field the import keeps as the learner's own showing the
    learner's text, so the card renders what will actually land. The package's text
    stays in `detail` for `_change_row`, which offers it as the deck's version."""
    was = detail.get("was") or {}
    return {**detail, "fields": [(n, str(was[n]) if isinstance(was.get(n), KeptEdit) else v)
                                 for n, v in detail.get("fields", [])]}


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
    """The fields this card would rewrite, in the note type's field order."""
    was = detail.get("was") or {}
    return [n for n, _ in detail.get("fields", []) if n in was]


def _image_text(detail, resolved=frozenset()):
    """The card's Image field, named rather than rendered. Filenames in `resolved`,
    which the picture strip already painted, drop out. Empty when no name is left.
    """
    value = _field(detail, "Image")
    text = plain_text(value)
    names = [n for n in field_image_names(value) if n not in resolved]
    if not names:
        return text
    tag = f"[image: {', '.join(names)}]"
    return f"{text} {tag}" if text else tag


def _primary_field(detail):
    """The (name, value) of the note type's primary content field: a cloze's Text,
    otherwise the first field `_content_fields` keeps. None when the note type has no
    content field.
    """
    if _is_cloze(detail):
        return ("Text", _field(detail, "Text"))
    fields = _content_fields(detail)
    return fields[0] if fields else None


def _primary_html(detail):
    """The collapsed row's line: a cloze's text with its deletions filled in, otherwise
    the prompt field. An image note's picture name is folded in.

    A cloze field goes through field_preview_html before cloze_filled_html; the result
    is already safe and must not be escaped again.
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
    """The card's answer, shown once its row is expanded. A cloze has no answer text
    here (its answer is on the collapsed line), but its Image field is still named here.

    `resolved` passes to `_image_text`; `image_html` passes to `field_preview_html` so a
    picture inline in the answer renders in place. Both default to nothing at build
    time; `_reveal_images` calls this again with both filled in.
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
    in the primary field. Fields rendered in the body resolve their own images in place.
    """
    primary = _primary_field(detail)
    primary_value = primary[1] if primary else ""
    names = field_image_names(_field(detail, "Image")) + field_image_names(primary_value)
    return list(dict.fromkeys(names))


def _row_html(detail):
    """A collapsed row's line: the card's tag when it has one, then its primary line.
    One rich-text paragraph, so every row's text starts at the same x, tagged or not.
    """
    primary = _primary_html(detail)
    tag_text = field_preview_text(_field(detail, "Tag"))
    if tag_text:
        tag = html.escape(tag_text)
        primary = f'<span style="color: {colors()["dim"]};">{tag}</span>&nbsp;&nbsp;{primary}'
    return _preview_style() + primary


def _rich_label(text):
    """A QLabel for card or confirmation HTML, closed to external links: callers
    interpolate unescaped collection content, so an anchor in it must not open the
    browser.
    """
    lbl = QLabel(_preview_style() + text)
    lbl.setWordWrap(True)
    lbl.setTextFormat(Qt.TextFormat.RichText)
    lbl.setOpenExternalLinks(False)
    return lbl


def _plain_prose(value):
    """Whether a field value is plain prose `merged_word_diff` can show: no tags, cloze
    braces or math markup, which splitting on spaces would tear apart. Entities pass.
    """
    return bool(value) and "<" not in value and "{{" not in value and "\\" not in value


def _diff_html(segments):
    """`merged_word_diff`'s segments as one rich-text line: dropped words struck
    through, added words in the UPDATED chip's colour pair, unchanged words dim.
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
    """A cloze change that left the sentence's words alone, as a line naming the moved
    blanks and re-hinted deletions in bold. Bold rather than cloze blue because accent
    is also the link colour in this body.
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


# Minimum word_diff_ratio for the word-diff rendering; below it the change reads as a
# rewrite and takes the summary line. 0.75 keeps the marked share of the merged line
# near 40% (at 0.6 it is about 57%).
_DIFF_MIN_SIMILARITY = 0.75


def _inline_change_html(detail, name, old, new):
    """One changed field as a single self-contained line, or None when the change is
    a rewrite that takes `_rewrite_row`'s summary-and-reveal treatment instead:

    - a field that used to be empty says so ("was empty");
    - a cloze Text whose words survived but whose blanks or hints moved names what
      moved (`cloze_answer_changes`, `cloze_hint_changes`);
    - plain prose on both sides renders as a single word-level diff (`_diff_html`),
      while the versions stay mostly the same text (`word_diff_ratio`).

    Everything else (a rewritten paragraph, a restructured table, a swapped picture,
    a reworded cloze) returns None.
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
    """The receipt line's verb phrase for a replaced field: "rewritten", plus the
    word-count change when the length moved by a quarter or more. "cleared" when the
    visible text vanished, "replaced" when there is no visible text to count (a swapped
    picture).
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


def _old_version_html(detail, name, old, image_html):
    """The old field rendered the way the card itself renders: structure kept,
    pictures resolved by `image_html` (the learner's own collection, already on disk, or
    the card's resolver), and a cloze's deletions filled in blue like the header line,
    never raw {{c1::…}} braces. Built on first reveal, not at batch-build time."""
    rendered = field_preview_html(old, image_html=image_html)
    if _is_cloze(detail) and name == "Text":
        rendered = cloze_filled_html(rendered, escape=False)
    return rendered


def _rewrite_row(detail, name, old, new, card_label, receipt=None, whose="yours",
                 choice=None, image_html=None):
    """A rewritten field as a one-line receipt with the old version behind a click: "Why
    rewritten, shortened (104 → 66 words)   Show yours".

    The reveal is the old field verbatim behind a muted left rule, built on the first
    click.

    `receipt` and `whose` word the row for the case where `old` is not the learner's: a
    field they edited, which the import keeps, offers the deck's text behind "Show the
    deck's". `choice`, a (set, key) pair, adds the "Use the deck's version" link inside
    that reveal, toggling `key` in the set; the receipt then says the deck's version is
    taken while the card above, rendered from the learner's text, stays as it is.
    `image_html` resolves pictures in the revealed version (the learner's collection by
    default).
    """
    image_html = image_html or _collection_image
    row = QWidget()
    lay = QVBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(2)

    line = QWidget()
    hlay = QHBoxLayout(line)
    hlay.setContentsMargins(0, 0, 0, 0)
    hlay.setSpacing(8)
    receipt = receipt or _rewrite_summary(old, new)
    summary = _rich_label(f"<b>{html.escape(name)}</b>&nbsp;&nbsp;{receipt}")
    summary.setStyleSheet(f"color: {colors()['dim']};")
    # One line by design: a word-wrapped QLabel asks the layout for less than its full
    # text width and folds.
    summary.setWordWrap(False)
    hlay.addWidget(summary, 0)
    link = link_button(f"Show {whose}")
    # Without border and padding rules a flat button keeps the platform's native button
    # height (32px on macOS).
    link.setStyleSheet(link.styleSheet() +
                      "QPushButton { border: none; padding: 0; }")
    link.setCursor(Qt.CursorShape.PointingHandCursor)
    hlay.addWidget(link, 0)
    hlay.addStretch()
    lay.addWidget(line)

    old_label = _rich_label("")
    old_label.setVisible(False)
    c = colors()
    # `border: none` first: Qt ignores a lone border-left on a QLabel unless the
    # shorthand is set.
    old_label.setStyleSheet(f"border: none; border-left: 3px solid {c['muted']};"
                            f" padding-left: 8px; color: {c['dim']};")
    lay.addWidget(old_label)

    use = None
    if choice is not None:
        chosen, key = choice
        use = link_button("")
        use.setStyleSheet(link.styleSheet())
        use.setCursor(Qt.CursorShape.PointingHandCursor)
        use.setVisible(False)

        def _use_label():
            taking = key in chosen
            text = "Keep your edit" if taking else "Use the deck's version"
            use.setText(text)
            use.setAccessibleName(f"{text}: {card_label}")
            # The receipt carries the choice once the reveal is closed again.
            summary.setText(_preview_style() + f"<b>{html.escape(name)}</b>&nbsp;&nbsp;"
                            + ("taking the deck's version (the card above still shows yours)"
                               if taking else receipt))

        def _use(_checked=False):
            chosen.symmetric_difference_update({key})
            _use_label()

        use.clicked.connect(_use)
        _use_label()
        lay.addWidget(use)

    def _name_link(showing):
        # Named for the card, so a list of links doesn't all read "Show yours".
        verb = f"Hide {whose}" if showing else f"Show {whose}"
        link.setText(verb)
        link.setAccessibleName(f"{verb}: {card_label}")

    # An explicit built flag: _rich_label seeds every label with the <style> block, so
    # an emptiness check would never fire.
    built = []

    def _toggle(_checked=False):
        showing = not old_label.isVisible()
        if showing and not built:
            built.append(True)
            old_label.setText(_preview_style()
                              + _old_version_html(detail, name, old, image_html))
        old_label.setVisible(showing)
        if use is not None:
            use.setVisible(showing)
        _name_link(showing)

    link.clicked.connect(_toggle)
    _name_link(False)
    return row


def _change_row(detail, name, card_label, take_deck=None, resolve=None):
    """One changed field's row in the What changed group: a plain line for a change
    one line can state honestly (`_inline_change_html`), or the receipt-and-reveal
    widget for a rewrite (`_rewrite_row`).

    `detail` is the package's own detail, not `_as_imported`'s: a field the import keeps
    as the learner's has `was` holding their text as a KeptEdit and `new` the deck's, and
    that row offers the deck's text behind a click, with a link to take it. `take_deck`
    is the set that link toggles; `resolve` is the card's image resolver, so a picture in
    the deck's version renders the same way the card body's do (the collection's own
    resolver when None)."""
    old = (detail.get("was") or {}).get(name)
    new = _field(detail, name)
    if isinstance(old, KeptEdit):
        # The import keeps the learner's text; the reveal offers the deck's instead.
        return _rewrite_row(detail, name, new, old, card_label,
                            receipt="you edited this, so yours is kept",
                            whose="the deck's",
                            choice=None if take_deck is None else (take_deck, (old.guid, name)),
                            image_html=resolve or _collection_image)
    old = old or ""
    inline = _inline_change_html(detail, name, old, new)
    if inline is None:
        return _rewrite_row(detail, name, old, new, card_label)
    label = _rich_label(inline)
    label.setStyleSheet(f"color: {colors()['dim']};")
    return label


def _changes_heading():
    """The small heading over the What changed group."""
    label = QLabel("What changed")
    label.setStyleSheet(f"color: {colors()['dim']}; font-size: 12px;"
                        " font-weight: 600; margin-top: 4px;")
    return label


def _change_note_html(note):
    """The row's account of why this card changed. Feedback is shown verbatim in
    quotes. A stale-hash feedback entry is explicitly labelled as earlier context;
    the add-on cannot know which learner sent it, so current feedback says "from
    feedback", never "your feedback"."""
    text = html.escape(note.get("note", ""))
    if note.get("kind") == "feedback":
        if note.get("historical"):
            when = html.escape(str(note.get("on") or ""))
            suffix = "earlier feedback"
            if when:
                suffix += f" &middot; {when}"
            return (f"<i>&ldquo;{text}&rdquo;</i>&nbsp;&nbsp;"
                    f"<span style='color:{colors()['dim']}; font-size:11px'>"
                    f"&middot; {suffix}</span>")
        return (f"<i>&ldquo;{text}&rdquo;</i>&nbsp;&nbsp;"
                f"<span style='color:{colors()['dim']}; font-size:11px'>"
                f"&middot; from feedback</span>")
    return f"<i>{text}</i>"


def _change_note_row(note, indent):
    """One `change_notes` entry as a row under the card header: a 3px accent bar and the
    note's text, in the row's text column.

    The stylesheet replaces muted_label's rather than appending: Qt ignores a lone
    border-left on a QLabel unless the border shorthand is reset first.
    """
    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(indent, 0, 0, 0)
    lay.setSpacing(0)
    label = muted_label(_change_note_html(note))
    label.setTextFormat(Qt.TextFormat.RichText)
    c = colors()
    # Feedback reads at full strength; a maintainer's note and older feedback stay
    # muted.
    voice = (note.get("kind") == "feedback" and not note.get("historical"))
    colour = "" if voice else f" color: {c['muted']};"
    label.setStyleSheet(f"border: none; border-left: 3px solid {c['updated_bg']};"
                        f" padding-left: 8px;{colour}")
    lay.addWidget(label)
    return row


def _folds(item):
    """Whether a ("group_note", note, card_count) header folds its members away. A group
    driven by reviewer feedback never folds: those are the cards most worth checking.
    """
    if item[0] != "group_note" or (item[2] if len(item) > 2 else 0) < _GROUP_COLLAPSE_MIN:
        return False
    return (item[1] or {}).get("kind") != "feedback"


def _fold_groups(items):
    """`items` with each foldable group's members moved inside its own
    ("group_note", note, card_count, members) item, so the streaming list never builds
    a row a fold would hide. Members are the run of cards, retired rows and grouped
    seps right after the header; anything else ends the group."""
    out, i = [], 0
    while i < len(items):
        item = items[i]
        i += 1
        if not _folds(item):
            out.append(item)
            continue
        members, i = group_run(items, i)
        out.append((item[0], item[1], item[2], members))
    return out


def _open_group_members(items):
    """ids of the items under a group header whose members are not folded inside it,
    so the list can indent them. A folded group indents its own as it builds them."""
    out, i = set(), 0
    while i < len(items):
        item = items[i]
        i += 1
        if item[0] == "group_note" and len(item) <= 3:
            members, i = group_run(items, i)
            out.update(id(m) for m in members)
    return out


def _member_row(row):
    wrap = QWidget()
    lay = QVBoxLayout(wrap)
    lay.setContentsMargins(_GROUP_MEMBER_INDENT, 0, 0, 0)
    lay.setSpacing(0)
    lay.addWidget(row)
    return wrap


def folded_guids(items):
    """{guid: deck_name} for every card that sits inside a folded group, for the
    caller's own counts of what the reader cannot see without opening something."""
    return {m[2]["guid"]: m[1]
            for item in _fold_groups(items) if item[0] == "group_note" and len(item) > 3
            for m in item[3] if m[0] == "card"}


def _group_title(members):
    """What a folded group holds, counted by kind: "12 cards updated", "3 new, 9
    updated", with any retired rows after."""
    kinds = [m[2].get("kind") for m in members if m[0] == "card"]
    parts = []
    new, changed = kinds.count("new"), kinds.count("changed")
    other = len(kinds) - new - changed
    retired = sum(1 for m in members if m[0] == "retired")
    if new and not changed and not other:
        parts.append(f"{plural(new, 'new card')}")
    elif changed and not new and not other:
        parts.append(f"{plural(changed, 'card')} updated")
    else:
        parts.append(plural(len(kinds), "card"))
        detail = [f"{n} {w}" for n, w in ((new, "new"), (changed, "updated")) if n]
        if detail:
            parts[-1] += f" ({', '.join(detail)})"
    if retired:
        parts.append(f"{retired} retired")
    return ", ".join(parts)


# A group's one decision, offered only when every card in it is the same kind. Never is
# left out: that is decided one card at a time.
_GROUP_OPTIONS = {"new": [("import", "Import all"), ("held", "Later all")],
                  "changed": [("apply", "Apply all"), ("keep", "Keep all yours"),
                              ("held", "Later all")]}


def _group_note_row(note, card_count, members=(), build_row=None, ctx=None):
    """The header over a change group: the shared note (see _change_note_row above),
    and, for a group folded by _fold_groups, a bordered box around it that says what
    the group holds, names its first few cards, offers one decision for all of them,
    and has a button that builds the members the first time it is pressed.

    `ctx` is build_update_body's own hooks: `decisions`, `set_members(guids, kind,
    state)` to decide the members whether or not their rows are built yet, `listen(fn)`
    to hear about every decision on the screen, and `on_expand(guids)` to refresh
    the screen when the group reveals its member rows."""
    row = _change_note_row(note, 0)
    row.layout().setStretch(0, 1)
    if not members:
        return row

    ctx = ctx or {}
    c = colors()
    note_text = truncate(plain_text(note.get("note", "")), 60)
    cards = [m[2] for m in members if m[0] == "card"]
    guids = [d["guid"] for d in cards]

    group = QFrame()
    group.setObjectName("changeGroup")
    # Scoped to the frame's name: a bare border rule would cascade into every label
    # inside it.
    group.setStyleSheet(f"QFrame#changeGroup {{ border: 1px solid {c['panel_rule']};"
                        " border-radius: 6px; }")
    group_lay = QVBoxLayout(group)
    group_lay.setContentsMargins(10, 8, 10, 8)
    group_lay.setSpacing(6)

    head = QWidget()
    hlay = QHBoxLayout(head)
    hlay.setContentsMargins(0, 0, 0, 0)
    hlay.setSpacing(8)
    title = QLabel(_group_title(members))
    title.setStyleSheet("font-weight: 600;")
    hlay.addWidget(title, 1)

    kinds = {d.get("kind") for d in cards}
    options = _GROUP_OPTIONS.get(next(iter(kinds))) if len(kinds) == 1 else None
    if options and ctx.get("set_members"):
        kind = next(iter(kinds))
        default = _DEFAULT_DECISION[kind]
        decisions = ctx["decisions"]
        mixed = hint_label("mixed")

        def _current():
            states = {decisions.get(g, default) for g in guids}
            return next(iter(states)) if len(states) == 1 else None

        def _decide_all(state):
            ctx["set_members"](guids, kind, state)

        cell = decision_cell(options, _current(), _decide_all,
                             f"{plural(len(guids), 'card')}: {note_text}")

        def _refresh(_guid=None, _state=None):
            state = _current()
            cell.set_state(state)
            mixed.setVisible(state not in dict(options))
        _refresh()
        ctx["listen"](_refresh)
        hlay.addWidget(mixed, 0, Qt.AlignmentFlag.AlignVCenter)
        hlay.addWidget(cell, 0, Qt.AlignmentFlag.AlignTop)

    toggle = QPushButton(f"Show {plural(card_count, 'card')}")
    toggle.setCursor(Qt.CursorShape.PointingHandCursor)
    hlay.addWidget(toggle, 0, Qt.AlignmentFlag.AlignTop)
    group_lay.addWidget(head)
    group_lay.addWidget(row)

    preview = QWidget()
    play = QVBoxLayout(preview)
    play.setContentsMargins(_GROUP_MEMBER_INDENT, 0, 0, 0)
    play.setSpacing(1)
    for d in cards[:_GROUP_PREVIEW]:
        line = QLabel(_card_label(d))
        line.setTextFormat(Qt.TextFormat.PlainText)
        line.setStyleSheet(f"color: {c['dim']};")
        play.addWidget(line)
    if len(cards) > _GROUP_PREVIEW:
        more = QLabel(f"and {len(cards) - _GROUP_PREVIEW} more")
        more.setStyleSheet(f"color: {c['muted']}; font-style: italic;")
        play.addWidget(more)
    group_lay.addWidget(preview)

    body = QWidget()
    body_lay = QVBoxLayout(body)
    body_lay.setContentsMargins(0, 0, 0, 0)
    body_lay.setSpacing(0)
    body.setVisible(False)
    group_lay.addWidget(body)
    state = {"expanded": False, "built": False}

    def _name_toggle(expanded):
        verb = f"{'Hide' if expanded else 'Show'} {plural(card_count, 'card')}"
        toggle.setText(verb)
        toggle.setAccessibleName(f"{verb}: {note_text}" if note_text else verb)

    def _toggle(_checked=False):
        state["expanded"] = not state["expanded"]
        if state["expanded"] and not state["built"]:
            state["built"] = True
            for member in members:
                body_lay.addWidget(build_row(member))
            if ctx.get("on_expand"):
                ctx["on_expand"](guids)
        body.setVisible(state["expanded"])
        preview.setVisible(not state["expanded"])
        _name_toggle(state["expanded"])

    toggle.clicked.connect(_toggle)
    _name_toggle(False)
    # Room between this box and the rows around it, which sit flush to their hairlines.
    wrap = QWidget()
    wlay = QVBoxLayout(wrap)
    wlay.setContentsMargins(0, 6, 0, 6)
    wlay.setSpacing(0)
    wlay.addWidget(group)
    return wrap


def _retired_row(identity, reason, chips):
    """A retired ledger entry's row: its identity through `simple_row` and, when the
    ledger recorded a reason, a second muted line beneath in the maintainer-note style
    (`_change_note_html`). The reason is its own label, indented by `row_text_indent`,
    so the identity stays readable as one line. Non-expanding.
    """
    row = simple_row("retired", identity, chips=chips)
    if not reason:
        return row
    wrap = QWidget()
    lay = QVBoxLayout(wrap)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)
    lay.addWidget(row)
    label = muted_label(f"<i>{html.escape(reason)}</i>")
    label.setTextFormat(Qt.TextFormat.RichText)
    line = QWidget()
    line_lay = QHBoxLayout(line)
    line_lay.setContentsMargins(row_text_indent(1, chips), 0, 0, 4)
    line_lay.setSpacing(0)
    line_lay.addWidget(label)
    lay.addWidget(line)
    return wrap


def _chip_with_source(detail, chips):
    """The row's chip column, with the deck source's `card_source` label as small tags
    under the chip, one per whitespace-separated reference.

    The string comes from a fetched manifest, so it is rendered as plain text, never
    markup.
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
        tag = QLabel(ref)
        tag.setTextFormat(Qt.TextFormat.PlainText)
        tag.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tag.setStyleSheet(f"color: {dim}; font-size: 11px;")
        lay.addWidget(tag)
    # Fixed width, so a tag cannot widen the gutter and misalign the rows.
    wrap.setFixedWidth(chip_column_width(chips))
    return wrap


def _collection_image(name):
    """One of the learner's own media files as an <img>, or None when it is not there.
    """
    try:
        folder = mw.col.media.dir()
    except Exception:
        return None
    if not folder:
        return None
    local = os.path.join(folder, os.path.basename(name))
    return _image_tag(local) if os.path.exists(local) else None


_NEW_OPTIONS = [("import", "Import"), ("held", "Later"), ("never", "Never")]
# "frozen" is Never's counterpart on a changed row, kept as its own state so the
# Declined cards screen can tell a card never imported from a card kept with its updates
# turned off. The button says Never on both kinds.
_CHANGED_OPTIONS = [("apply", "Apply"), ("keep", "Keep yours"), ("held", "Later"),
                    ("frozen", "Never")]
_DEFAULT_DECISION = {"new": "import", "changed": "apply"}

_DECLINE_CAPTION = {
    "keep": ("Your card stays as it is. A change is offered again only when this "
             "card's source content changes, or any time from Manage decks → Later and declined cards."),
    # No "offered again" clause: Manage decks → Later and declined cards is the only way
    # back.
    "frozen": ("Your card stays as it is, and changes to it won't be offered again. "
               "Undo from Manage decks → Later and declined cards."),
}

# Nothing is transmitted from here: a note goes into the end-of-run digest, which the
# reader copies and pastes.
_FEEDBACK_PLACEHOLDER = ("Anything to pass on about this card? You'll get a copy at the "
                         "end to paste and send.")


def _later_caption(kind, detail=None):
    """What Later does, said on the row that chose it. Its default is the row's own. A
    noted card still waiting for its update says that instead."""
    verb = "Apply" if kind == "changed" else "Import"
    detail = detail or {}
    if detail.get("later_wait") and detail.get("later_note_status") == "not_updated":
        return f"Waiting for this card to be updated. Choose {verb} to take it as it is."
    return (f"Comes back the next time you run Update my decks, set to {verb}. "
            "With a note, it waits until the card is updated.")


# Decisions that turn a card down, and so offer the note box on the spot. Later defers
# rather than judges, so it is not one.
_TURNED_DOWN = frozenset({"keep", "never", "frozen"})

_DECLINE_CHIP = {"keep": "kept", "frozen": "kept", "held": "held"}

# A returning noted Later row's hint, by logic.later_status's `later_note_status`.
_LATER_NOTE_HINT = {"not_updated": "Not updated yet", "updated": "Updated since your note"}


def _row_chip(detail):
    """The one chip a card row wears. A re-offered decline shows that decline (LATER /
    KEPT YOURS) rather than its kind, so the row keeps a single chip column; the hint
    line under the header says the rest.
    """
    return _DECLINE_CHIP.get(detail.get("declined_state")) or detail.get("kind")


def _card_label(detail):
    """The card's short name, for a control that would otherwise be labelled by a bare
    verb.
    """
    return note_display_label([v for _, v in detail.get("fields", [])], max_len=60)


def _card_row(detail, flags, boxes, decisions, on_decide, resolve=None, chips=None,
              on_open=None, register=None, on_box=None, take_deck=None):
    """One card as a single row: a caret, its chip column (see `_row_chip`), its tag if
    it has one, and its primary line. Clicking the row reveals the answer, the why, and
    dosing when present. A "new" or "changed" row also carries a segmented decision
    control (widgets.decision_cell), defaulting to Import/Apply; choosing Keep or Never
    reveals a feedback box, and Never collapses the row with a struck-through primary
    line. Any row whose box is closed offers an "Add note" link at the end of the
    expanded body.

    Pictures are named until the first expand and extracted then.

    A `card_source` label renders under the row's chip (see `_chip_with_source`), and
    `change_notes` entries render under the header whether the row is collapsed or open
    (see `_change_note_row`).
    """
    guid = detail["guid"]
    kind = detail.get("kind")
    # What the card renders is what lands; `detail` itself still feeds `_change_row`.
    shown = _as_imported(detail)
    card_label = _card_label(shown)
    row = QWidget()
    outer = QVBoxLayout(row)
    outer.setContentsMargins(0, 5, 0, 6)
    outer.setSpacing(4)

    body = QWidget()
    caret = QPushButton(_CARET_CLOSED)
    # Labels whose field can hold a picture, each with its field value, for the first
    # expand to re-render. The answer label is re-rendered separately by
    # `_reveal_images` through `_answer_html`.
    rerender = []
    # The What changed group needs no rerender: its inline lines are text only, and a
    # rewrite's old version renders at reveal time (see _rewrite_row).
    revealed = []
    # This card's _change_note_row rows, kept so _apply_decision_visuals can hide them
    # under Never and restore them after. The card_source tag stays visible under Never.
    note_rows = []

    def _reveal_images():
        if revealed:
            return
        revealed.append(True)
        if resolve is None:
            return
        image_names = _primary_images(shown)
        resolved_tags = {name: resolve(name) for name in image_names}
        succeeded = {name for name, tag in resolved_tags.items() if tag}
        for label, value in rerender:
            label.setText(_preview_style() + field_preview_html(value, image_html=resolve))
        rendered = "<br>".join(tag for tag in resolved_tags.values() if tag)
        if rendered:
            images.setText(_preview_style() + rendered)
            images.setVisible(True)
        if answer_label is not None:
            new_answer_html = _answer_html(shown, resolved=succeeded, image_html=resolve)
            answer_label.setText(_preview_style() + new_answer_html)
            answer_label.setVisible(bool(new_answer_html))

    def _name_caret(expanded):
        """Name the caret for screen readers and hover, in step with the direction it
        points.
        """
        verb = "Hide card" if expanded else "Show card"
        # The card's name goes in the accessible name only: a hovering reader already
        # sees the row's front.
        caret.setAccessibleName(f"{verb}: {card_label}")
        caret.setToolTip(verb)

    def _toggle():
        expanded = not body.isVisible()
        if expanded:
            _reveal_images()
            if on_open is not None:
                on_open(guid)
        body.setVisible(expanded)
        caret.setText(_CARET_OPEN if expanded else _CARET_CLOSED)
        _name_caret(expanded)

    header = QWidget()
    hlay = QHBoxLayout(header)
    hlay.setContentsMargins(0, 0, 0, 0)
    hlay.setSpacing(CARET_GAP)

    caret.setFlat(True)
    # Unconstrained, the button takes its platform minimum width (~80px on macOS).
    caret.setFixedWidth(CARET_W)
    # Heavier and stronger than the dim text beside it: the glyph is the only sign the
    # row opens.
    caret.setStyleSheet(f"border: none; padding: 0; font-weight: 600;"
                        f" color: {colors()['caret']};")
    caret.setCursor(Qt.CursorShape.PointingHandCursor)
    # Kept in the tab order: the row's label is a QLabel and takes no focus, so the
    # caret is the only keyboard way to open a card.
    caret.clicked.connect(_toggle)
    _name_caret(False)
    hlay.addWidget(caret, 0, Qt.AlignmentFlag.AlignTop)

    # Every column drawn in front of the primary label; the expanded body indents by
    # exactly these (see `indent` below). Top-aligned, so the chip sits beside the first
    # line of a wrapping row.
    declined_state = detail.get("declined_state")
    changed_since_decline = bool(detail.get("changed_since_decline"))
    leading = [_chip_with_source(detail, chips)]
    for cell in leading:
        hlay.addWidget(cell, 0, Qt.AlignmentFlag.AlignTop)
    indent = row_text_indent(len(leading), chips)

    primary = _ClickableLabel(_row_html(shown), _toggle)
    primary.setWordWrap(True)
    primary.setTextFormat(Qt.TextFormat.RichText)
    primary.setCursor(Qt.CursorShape.PointingHandCursor)
    hlay.addWidget(primary, 1)

    # In the text column. Hidden while both caption and box are, so a row without them
    # gains no spacing.
    decision = QWidget()
    dlay = QVBoxLayout(decision)
    dlay.setContentsMargins(indent, 0, 0, 0)
    dlay.setSpacing(4)
    decision.setVisible(bool(flags.get(guid)))
    caption = None
    box = None

    def _ensure_caption():
        nonlocal caption
        if caption is None:
            caption = muted_label("")
            caption.setVisible(False)
            dlay.addWidget(caption)

    def _ensure_box():
        nonlocal box
        if box is not None:
            return
        _ensure_caption()
        box = QPlainTextEdit(flags.get(guid, ""))
        box.setAccessibleName(f"Feedback note: {card_label}")
        if hasattr(caption, "setBuddy"):
            caption.setBuddy(box)
        box.setPlaceholderText(_FEEDBACK_PLACEHOLDER)
        box.setFixedHeight(50)
        box.setVisible(bool(flags.get(guid)))
        dlay.addWidget(box)
        boxes[guid] = box
        if on_box is not None:
            on_box(guid, box)

    if flags.get(guid):
        _ensure_box()

    default = _DEFAULT_DECISION.get(kind)
    never_note = hint_label("won't be offered again")
    never_note.setVisible(False)
    add_note = link_button("Add note")
    add_note.setAccessibleName(f"Add note: {card_label}")
    add_note.setVisible(False)

    def _reveal_box(_checked=False):
        _ensure_box()
        box.setVisible(True)
        add_note.setVisible(False)
        decision.setVisible(True)
    add_note.clicked.connect(_reveal_box)

    def _apply_decision_visuals(state, clicked=False):
        text = (_later_caption(kind, detail) if state == "held"
                else _DECLINE_CAPTION.get(state))
        if text:
            _ensure_caption()
            caption.setText(text)
        if caption is not None:
            caption.setVisible(bool(text))
        # The box stays open once there is a saved flag or typed text; a return to the
        # default with nothing written closes it. `clicked` limits opening the box to a
        # decline made now, not one the row arrived with.
        has_note = bool(flags.get(guid)) or (box is not None
                                           and bool(box.toPlainText().strip()))
        show_box = has_note or (state in _TURNED_DOWN and clicked)
        if show_box:
            _ensure_box()
        if box is not None:
            box.setVisible(show_box)
        decision.setVisible(bool(text) or show_box)
        # Offered whenever the box is closed, whatever the row decided, so a re-offered
        # decline can still take a note.
        add_note.setVisible(not show_box)
        # Struck through only for Never. A frozen row keeps a card the learner has.
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
        # everything else collapses.

    if kind in _DEFAULT_DECISION:
        options = _NEW_OPTIONS if kind == "new" else _CHANGED_OPTIONS
        # A predeclined detail's `decisions` entry is seeded by build_update_body before
        # any row is built.
        initial = decisions.get(guid, default)

        def _decide(state, clicked):
            if state == default:
                decisions.pop(guid, None)
            else:
                decisions[guid] = state
            _apply_decision_visuals(state, clicked=clicked)
            on_decide(guid, state)

        def _on_change(state):
            _decide(state, True)

        hlay.addStretch()
        cell = decision_cell(options, initial, _on_change, card_label)
        hlay.addWidget(cell, 0, Qt.AlignmentFlag.AlignTop)
        if register is not None:
            # A group's decision reaches a built row through here. It is not a click on
            # this row, so it opens no note box.
            register(guid, lambda state: (cell.set_state(state), _decide(state, False)))
        hlay.addWidget(never_note, 0, Qt.AlignmentFlag.AlignTop)
        _apply_decision_visuals(initial)

    outer.addWidget(header)

    for note in detail.get("change_notes") or []:
        note_row = _change_note_row(note, indent)
        note_rows.append(note_row)
        outer.addWidget(note_row)

    # In the row's text column, like the body below.
    hints = []
    if changed_since_decline and declined_state != "held":
        hints.append(muted_label("Changed since you kept yours. Worth another look."))
    later_note = detail.get("later_note")
    if declined_state == "held" and later_note:
        # The learner's own words, shown as typed rather than read as markup.
        note_label = muted_label(f"Your note: {later_note}")
        note_label.setTextFormat(Qt.TextFormat.PlainText)
        hints.append(note_label)
        status = _LATER_NOTE_HINT.get(detail.get("later_note_status"))
        if status:
            hints.append(muted_label(status))
    if hints:
        hint_row = QWidget()
        hint_lay = QVBoxLayout(hint_row)
        hint_lay.setContentsMargins(indent, 0, 0, 0)
        hint_lay.setSpacing(0)
        for hint in hints:
            hint_lay.addWidget(hint)
        outer.addWidget(hint_row)

    body.setVisible(False)
    blay = QVBoxLayout(body)
    # Indented by every column in front of the primary label, counted from `leading`, so
    # the body lines up under its own line.
    blay.setContentsMargins(indent, 2, 0, 2)
    blay.setSpacing(4)

    images = _rich_label("")
    images.setVisible(False)
    blay.addWidget(images)

    # Bound before the conditional so `_reveal_images` always finds the name, even on a
    # card with no answer block.
    answer_label = None
    answer_html = _answer_html(shown)
    if answer_html:
        answer_label = _rich_label(answer_html)
        blay.addWidget(answer_label)

    why_value = _field(shown, "Why")
    why_html = field_preview_html(why_value)
    if why_html:
        why_label = _rich_label(why_html)
        why_colour = colors()["why"]
        # `border: none` first: Qt ignores a lone border-left on a QLabel unless the
        # shorthand is set.
        why_label.setStyleSheet(f"border: none; border-left: 3px solid {why_colour};"
                                f" padding-left: 8px; color: {why_colour};")
        blay.addWidget(why_label)
        rerender.append((why_label, why_value))

    # Dosing stays out of rerender: it never holds a picture, and re-rendering would
    # drop the "Dosing" label prefix.
    dosing_html = field_preview_html(_field(shown, "Dosing"))
    if dosing_html:
        dosing_label = _rich_label(f"<b>Dosing</b> &nbsp;{dosing_html}")
        c = colors()
        # A hardcoded background needs a hardcoded foreground: platform text colour
        # flips under Night Mode. See "Colors" in README.md.
        dosing_label.setStyleSheet(f"background: {c['dosing_bg']}; color: {c['dosing_fg']};"
                                   f" padding: 6px; border-radius: 4px;")
        blay.addWidget(dosing_label)

    # Everything this update rewrites, as one group after the card, in the note type's
    # field order. Field-name labels map each line back to its block.
    changed_names = _changed_field_names(detail)
    if changed_names:
        blay.addWidget(_changes_heading())
        for name in changed_names:
            blay.addWidget(_change_row(detail, name, card_label, take_deck, resolve))

    # Last in the body, past every field block.
    if kind in _DEFAULT_DECISION:
        blay.addWidget(add_note)

    outer.addWidget(body)
    outer.addWidget(decision)
    return row


def _chip_kinds(items):
    """The chip kinds a list can paint, read off its rows, for
    widgets.chip_column_width. Handles both item vocabularies: build_list_body's ("row",
    kind, ...) and build_update_body's card/retired/moved rows.
    """
    kinds = []
    for item in items:
        if item[0] == "card":
            kinds.append(_row_chip(item[2]))
        elif item[0] == "row":
            kinds.append(item[1])
        elif item[0] in ("retired", "moved"):
            kinds.append(item[0])
        elif item[0] == "group_note":
            pass   # the header carries no chip of its own
    return tuple(dict.fromkeys(k for k in kinds if k))


_update_flushes = {}


def flush_update_notes():
    for flush in tuple(_update_flushes):
        try:
            flush(preserve_saved=True)
        except Exception as exc:
            print(f"Could not save card feedback: {exc}")


def save_feedback(entries):
    """Write the in-progress notes to user_files/, or clear the file when there are
    none. Called as the learner types, so notes survive a crash.
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
    """Drop the saved notes, once they have been archived and shown to the learner."""
    path = _collection_state_path(FEEDBACK)
    try:
        if path:
            os.remove(path)
    except OSError:
        pass


def _feedback_digest_folder():
    state = _collection_state_path(FEEDBACK)
    return os.path.join(os.path.dirname(state), "feedback_digests") if state else None


def _feedback_digest_names(folder):
    names = []
    for name in os.listdir(folder):
        if not name.endswith(".txt"):
            continue
        try:
            sequence = int(name.split("-", 1)[0])
        except ValueError:
            continue
        if sequence > 0:
            names.append((sequence, name))
    return [name for sequence, name in sorted(names, reverse=True)]


def _write_feedback_digest(text, entries):
    folder = _feedback_digest_folder()
    if folder is None:
        return
    os.makedirs(folder, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    existing = _feedback_digest_names(folder)
    sequence = int(existing[0].split("-", 1)[0]) + 1 if existing else 1
    stem = f"{sequence:06d}-{stamp}"
    path = os.path.join(folder, stem + ".txt")
    counts = {"notes": sum(1 for e in entries if (e.get("note") or "").strip()),
              "decisions": sum(1 for e in entries if e.get("decision"))}
    _save_json(os.path.join(folder, stem + ".json"), counts)
    config.atomic_write(path, text)
    return folder


def save_feedback_digest(text, entries):
    """Return whether the text was archived, even if pruning older digests fails."""
    try:
        folder = _write_feedback_digest(text, entries)
        if folder is None:
            return False
    except Exception as exc:
        print(f"Could not save card feedback digest: {exc}")
        return False
    try:
        for name in _feedback_digest_names(folder)[20:]:
            for victim in (name, name[:-4] + ".json"):
                try:
                    os.remove(os.path.join(folder, victim))
                except FileNotFoundError:
                    pass
    except Exception as exc:
        print(f"Could not prune card feedback digests: {exc}")
    return True


def load_feedback_digests():
    """Saved text and counts for the open collection and source, newest first."""
    folder = _feedback_digest_folder()
    if folder is None or not os.path.isdir(folder):
        return []
    saved = []
    try:
        names = _feedback_digest_names(folder)
    except OSError as exc:
        print(f"Could not read card feedback digests: {exc}")
        return saved
    for name in names:
        try:
            when = datetime.datetime.strptime(name.split("-", 1)[1][:-4], "%Y-%m-%dT%H-%M-%S")
            with open(os.path.join(folder, name), encoding="utf8", newline="") as fh:
                text = fh.read()
        except (OSError, ValueError) as exc:
            print(f"Could not read card feedback digest: {exc}")
            continue
        counts = _load_json(os.path.join(folder, name[:-4] + ".json"), None)
        if not isinstance(counts, dict) or any(
                type(counts.get(key)) is not int for key in ("notes", "decisions")):
            counts = {"notes": None, "decisions": None}
        saved.append({"text": text, "when": when,
                      "notes": counts["notes"], "decisions": counts["decisions"]})
    return saved


@_safe
def open_recent_feedback():
    saved = load_feedback_digests()
    body = QWidget()
    lay = QVBoxLayout(body)
    lay.setContentsMargins(0, 0, 0, 0)
    rows = QWidget()
    rlay = QVBoxLayout(rows)
    rlay.setContentsMargins(0, 0, 0, 0)
    rlay.setSpacing(0)
    if not saved:
        rlay.addWidget(simple_row(None, "No card feedback has been saved yet.",
                                   card_columns=False))
    for i, digest in enumerate(saved):
        when = digest["when"]
        counts = ("counts unavailable" if digest["notes"] is None else
                  f"{plural(digest['notes'], 'note')}, {plural(digest['decisions'], 'decision')}")
        label = f"{when.day} {when.strftime('%b %Y, %H:%M')} · {counts}"
        if i:
            rlay.addWidget(row_rule())
        row = simple_row(None, html.escape(label), card_columns=False)
        row.layout().addWidget(link_button(
            "Show", lambda checked=False, d=digest: show_feedback_digest(
                None, d["text"], heading=_digest_count_heading(d["notes"], d["decisions"]) or "Card feedback")))
        rlay.addWidget(row)
    rlay.addStretch()
    lay.addWidget(_scrolled(rows, 420))
    lay.addStretch()
    _ask_with_widget(body, yes_label="Close", no_label=None,
                     title=f"{APP_NAME}: recent card feedback", min_height=_CONFIRM_HEIGHT)


def build_update_body(items, sources, flags, new_index, decisions,
                      top_html, status_line, safety_html, touched=None, opened=None,
                      on_review=None, take_deck=None):
    """The Update my decks screen's body: fixed summary text, the streaming list of
    pending new and changed cards plus any retired or relocated ones, then the status
    line and the safety note. `internpearls.ui._ask_with_widget` wraps it with the
    dialog's title and buttons.

    `items` entries, one per row, built by sync.py (see `_grouped_rows`):

      ("header", text), ("note", html), ("sep",)   as in build_list_body
      ("deck", deck_short, counts)       a per-deck summary row, without the caret and
                                         chip columns
      ("card", deck_name, detail)        a card, through `_card_row`
      ("retired", identity[, reason])    one line, plus a muted reason line when given
      ("moved", front, dest_deck_short)  one line
      ("group_note", note, card_count)   a shared change note over the member rows
                                         beneath it; a group of at least
                                         `_GROUP_COLLAPSE_MIN` cards can fold behind it

    `sources` is {deck_name: .apkg path}, passed to build_resolvers.

    `flags`/`new_index` are {guid: note}/{guid: (deck, front)}. A row's box writes into
    `flags` on every keystroke. `decisions` is the live {guid: state} map for the
    decision controls; an absent guid means the row's default (Import or Apply).

    `status_line()` is called on every keystroke and decision change to recompute the
    line below the list; `safety_html` is fixed.

    `touched`, when given, collects every guid whose decision control the learner
    clicked this run, even a click that lands on the state already shown, so
    update_decks() can tell an active re-decline or un-decline from a row left as seeded.

    `opened` collects every guid whose row the learner expanded, and `on_review()`
    runs after each expand and each decision click.

    From `FILTER_MIN_CARDS` card rows up, a FilterBar sits above the list. Filtering
    rebuilds the stream from `filter_update_items`; decisions, flags and the touched
    set stay put.

    Returns (widget, boxes, flush). `boxes` is {guid: QPlainTextEdit}, populated only
    when a built row carries a note or opens its editor. `flush()` stops the debounce
    save timer, writes a final copy of what's flagged to disk, and releases the
    temporary picture directory; the caller runs it once, after the dialog has closed.
    """
    resolvers, media_dir = build_resolvers(sources)
    boxes = {}
    carried = load_saved_feedback()
    touched = touched if touched is not None else set()
    opened = opened if opened is not None else set()

    # Seed every predeclined card's `decisions` entry here, before any row is built:
    # StreamingList builds rows past the first batch only on scroll, so seeding in
    # _card_row would leave them missing from the dict the caller reads back. Only a
    # state the row's control can express is written. A Later card returns at its
    # default unless it is waiting (`later_wait`: an old Skip, or a noted card not
    # updated yet).
    migrated = 0
    for item in items:
        if item[0] != "card":
            continue
        d = item[2]
        state = d.get("declined_state")
        kind = d.get("kind")
        default = _DEFAULT_DECISION.get(kind)
        options = {v for v, _ in (_NEW_OPTIONS if kind == "new"
                                  else _CHANGED_OPTIONS if kind == "changed" else ())}
        migrated += bool(d.get("later_migrated"))
        if state == "held" and not d.get("later_wait"):
            continue
        if state and state != default and state in options:
            decisions.setdefault(d["guid"], state)

    def _entries():
        return pending_entries(boxes, flags, new_index, carried)

    body = QWidget()
    lay = QVBoxLayout(body)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(8)

    # Skipped when empty: an empty label still takes a line's height plus spacing.
    if top_html:
        lay.addWidget(_rich_label(top_html))
    if migrated:
        lay.addWidget(muted_label(later_migration_line(migrated)))

    # The safety note is standing reassurance, so it renders as small print; the status
    # line keeps body size.
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
            flags.pop(guid, None)   # the learner cleared it; treat that as unflagging
        _refresh_bottom()
        saver.start()

    def _on_box(guid, box):
        box.textChanged.connect(lambda: _on_change(guid, box))

    listeners = []
    setters = {}
    deciding_all = False

    def _notify_decision(guid=None, state=None):
        for fn in listeners:
            fn(guid, state)
        _refresh_bottom()
        if on_review is not None:
            on_review()

    def _on_decide(guid, state):
        touched.add(guid)
        if not deciding_all:
            _notify_decision(guid, state)

    def _on_open(guid):
        opened.add(guid)
        _refresh_bottom()
        if on_review is not None:
            on_review()

    def _set_member(guid, kind, state):
        """One card of a group decided from the group's own control: through its row
        when that row is built, otherwise straight into `decisions`, which is what the
        row reads its starting state from if it is built later."""
        if guid in setters:
            setters[guid](state)
            return
        if state == _DEFAULT_DECISION.get(kind):
            decisions.pop(guid, None)
        else:
            decisions[guid] = state
        _on_decide(guid, state)

    def _set_members(guids, kind, state):
        nonlocal deciding_all
        deciding_all = True
        try:
            for guid in guids:
                _set_member(guid, kind, state)
        finally:
            deciding_all = False
        _notify_decision()

    def _on_expand(guids):
        _refresh_bottom()
        if on_review is not None:
            on_review()

    group_ctx = {"decisions": decisions, "set_members": _set_members,
                 "listen": listeners.append, "on_expand": _on_expand}

    # Measured once for the whole screen, not per row: every row in one list has to be
    # given the same chip set or the column stops lining up.
    chips = _chip_kinds(items)

    open_members = set()

    def _row(item):
        built = _build_row(item)
        return _member_row(built) if id(item) in open_members else built

    def _build_row(item):
        if item[0] == "group_note":
            return _group_note_row(item[1], item[2] if len(item) > 2 else 0,
                                   item[3] if len(item) > 3 else (),
                                   lambda m: _member_row(_build_row(m)), group_ctx)
        if item[0] in ("header", "note", "sep"):
            return _list_row(item, chips=chips)
        if item[0] == "deck":
            _, deck_short, counts = item
            return simple_row(None, deck_short, counts, card_columns=False)
        if item[0] == "retired":
            # A stranded-pair row (see sync._stranded_items) is a 2-tuple with no
            # reason of its own; a ledger retirement carries one as its 3rd element.
            reason = item[2] if len(item) > 2 else ""
            return _retired_row(item[1], reason, chips)
        if item[0] == "moved":
            _, front, dest_short = item
            return simple_row("moved", front, f"→ {dest_short}", chips=chips)
        _, deck_name, detail = item
        row = _card_row(detail, flags, boxes, decisions, _on_decide,
                        resolve=resolvers.get(deck_name), chips=chips, on_open=_on_open,
                        register=setters.__setitem__, on_box=_on_box,
                        take_deck=take_deck)
        return row

    if items:
        folded = _fold_groups(items)
        open_members.update(_open_group_members(folded))
        stream = StreamingList(_row, folded)
        total = count_cards(items)
        if total >= FILTER_MIN_CARDS:
            def _refilter(mode, query):
                shown = filter_update_items(folded, mode, query, touched | opened)
                # The rows being replaced take their boxes, setters and group
                # listeners with them; flags and decisions hold what they showed.
                boxes.clear()
                setters.clear()
                listeners.clear()
                open_members.clear()
                open_members.update(_open_group_members(shown))
                stream.reset(shown)
                active = mode != "all" or bool(query.strip())
                bar.set_count(f"Showing {count_cards(shown)} of {total} cards"
                              if active else "")
            bar = FilterBar(FILTER_MODES, _refilter)
            lay.addWidget(bar)
        lay.addWidget(stream, 1)
    else:
        lay.addStretch()

    lay.addWidget(bottom)

    def flush(*, preserve_saved=False):
        if flush not in _update_flushes:
            return
        _update_flushes.pop(flush, None)
        try:
            try:
                saver.stop()            # the final state, not whatever the debounce had
            except RuntimeError:        # its owner was deleted, and the QTimer with it
                return
            entries = _entries()
            if entries or not preserve_saved:
                save_feedback(entries)
        finally:
            media_dir.cleanup()

    _update_flushes[flush] = None
    body.destroyed.connect(lambda *_: _update_flushes.pop(flush, None))

    return body, boxes, flush


HOLD_TOOLTIP = ("Cards you opened or chose for are updated as shown. The rest are left "
                "for later: they come back the next time you run Update my decks, set "
                "to Import or Apply.")


def hold_label(n):
    return f"Update, and leave {n} unopened for later"


def hold_control(registry, card_kinds, reviewed, decisions=None):
    """The confirmation's "hold for later" button: the `extra` dict for
    ui._ask_with_widget, or None when no row on this screen could be held, plus a
    refresh() that keeps its count in step with `reviewed()`, the rows opened or
    decided on so far, and with the live `decisions` map."""
    n = len(holdable_guids(registry, card_kinds, reviewed(), decisions))
    if not n:
        return None, lambda: None
    extra = {"label": hold_label(n), "tooltip": HOLD_TOOLTIP, "visible": True}

    def refresh():
        button = extra.get("button")
        if button is None:
            return
        left = len(holdable_guids(registry, card_kinds, reviewed(), decisions))
        button.setText(hold_label(left))
        button.setVisible(left > 0)
    return extra, refresh


def _list_row(item, card_columns=True, chips=None):
    """One entry of `build_list_body`'s item list as a widget. See that function for
    what each shape means, and for what `card_columns` decides. `chips` is the whole
    list's own chip set (see _chip_kinds), passed on to every row that draws one."""
    if item[0] == "header":
        return section_header(item[1])
    if item[0] == "note":
        label = _rich_label(item[1])
        # The same top margin section_header carries: a note opens a group.
        label.setStyleSheet("margin-top: 14px;")
        return label
    if item[0] == "sep":
        return row_rule()
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
    list itself, fixed text below. `internpearls.ui._ask_with_widget` wraps it with the
    dialog's title and buttons.

    Sync decks and Reconcile my decks build from this: the update screen's rows,
    hairlines and streaming, without `_card_row`, feedback boxes or picture resolvers.

    `items` entries:

      ("header", text)                    a bold heading over the rows below it
      ("note", html)                      a paragraph reading with the rows below it
      ("sep",)                            the hairline between two rows
      ("row", kind, primary, trailing)    one row, marked by `kind` (widgets.CHIPS),
                                          or by nothing when `kind` is None

    `card_columns` is the caret-and-chip-column choice widgets.simple_row documents,
    made once for the whole list. A list mixing chipped and unchipped rows keeps the
    columns so its text lines up; a list of one kind of row (Clean up duplicates,
    Remove empty cards) passes False and starts flush with the heading.
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
    """The run's outcome as widgets: its headline at the dialog's largest size, then one
    row or paragraph per item in build_list_body's vocabulary. Rows take no caret or
    chip columns (see widgets.simple_row).

    Not a StreamingList: the item count is bounded by the decks the run touched, and
    callers want the block sized to its content.
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
    """`widget` in a frameless scroll area that stops growing at `max_height`. Sized
    Preferred, not QScrollArea's default Expanding, which would stretch a short summary
    to the full cap.
    """
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setMaximumHeight(max_height)
    scroll.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
    scroll.setWidget(widget)
    return scroll


def show_result(title, items):
    """The end of a run with nothing flagged: the summary on its own, in a scrollable
    dialog so a long report cannot push OK off the screen.
    """
    body = QWidget()
    lay = QVBoxLayout(body)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(_scrolled(_summary_block(title, items), 420))
    # Leftover height collects here, below the summary and above the button.
    lay.addStretch()
    _ask_with_widget(body, yes_label="OK", no_label=None, min_width=460, min_height=0)


# What a run that stopped before changing anything says, shared by every place that can
# happen.
NOTHING_CHANGED = "Update cancelled, nothing was changed."


def show_result_with_feedback(title, items, entries, nothing_note="",
                              standing_declines=None, excluded=(), edits=()):
    """The end of a run as one dialog: the summary on top, the feedback digest below.

    Returns whether the shown digest was archived, or True when there was no digest.

    `title` is the run's headline; `items` is everything below it in build_list_body's
    vocabulary. A run with no flagged cards shows the summary alone, and a digest with
    no summary shows alone too.

    `standing_declines` is the current sparse decline registry to append to any digest;
    it does not make a digest appear when the run has no entries of its own. Its Later
    count leaves out cards in the `excluded` decks.

    `nothing_note` is the sentence to show, as a plain _info, when there is neither
    summary nor digest. Empty when the caller's exit needs no announcing.
    """
    if not entries:
        if title:
            show_result(title, items)
        elif nothing_note:
            _info(nothing_note)
        return True
    return offer_feedback_digest(None, entries, title=title, items=items,
                                 standing_declines=standing_declines, excluded=excluded,
                                 edits=edits)


def _digest_heading(entries, edits=()):
    """What the digest holds, with notes and decisions counted separately. Each half is
    named only when it has something to count.
    """
    flagged = sum(1 for e in entries if (e.get("note") or "").strip())
    decided = sum(1 for e in entries if e.get("decision"))
    edited = len({e["guid"] for e in edits})
    return _digest_count_heading(flagged, decided, edited) or plural(len(entries), "card")


def _digest_count_heading(flagged, decided, edited=0):
    parts = [text for text, n in ((f"{plural(flagged, 'card')} flagged", flagged),
                                  (f"{plural(decided, 'decision')} recorded", decided),
                                  (f"{plural(edited, 'card')} edited", edited))
             if n]
    return ", ".join(parts)


def offer_feedback_digest(parent, entries, title=None, items=(), standing_declines=None,
                          excluded=(), edits=()):
    """Put the flagged-card summary on the clipboard and show it, so the learner sees
    what will be pasted and can copy it again if the clipboard didn't take.

    Returns whether the shown digest was archived, or True when there was no digest.

    `title`/`items` are the end-of-run summary, drawn by _summary_block; the block is
    skipped when `title` is empty. `standing_declines` appends the current decline
    registry to the copied payload.
    """
    text = build_feedback_digest(entries, version=ADDON_VERSION,
                                 date=datetime.date.today().isoformat(),
                                 standing_declines=standing_declines,
                                 excluded=excluded, edits=edits)
    if not text:
        return True
    archived = save_feedback_digest(text, entries)
    show_feedback_digest(parent, text, heading=_digest_heading(entries, edits), title=title,
                         items=items, archived=archived)
    return archived


@_safe
def _save_feedback_file(text):
    date = datetime.date.today().isoformat()
    header = text.partition("\n")[0]
    if header.startswith("Intern Pearls card feedback (") and header.endswith(")"):
        try:
            date = datetime.date.fromisoformat(header.rsplit("(", 1)[1][:-1]).isoformat()
        except ValueError:
            pass
    path = getSaveFile(mw, "Save Intern Pearls feedback", "internPearlsFeedback",
                       "Text file", ".txt", fname=f"Intern Pearls feedback {date}.txt")
    if not path:
        return
    try:
        config.atomic_write(path, text)
    except Exception as exc:
        _warn(f"Could not save feedback: {exc}", textFormat="plain")
        return
    tooltip(f"Saved to {html.escape(os.path.basename(path))}")


def show_feedback_digest(parent, text, heading="Card feedback", title=None, items=(), *,
                         archived=False):
    """Show and copy ready text without rebuilding it or changing saved card notes."""
    copied = copy_to_clipboard(text)
    body = QWidget()
    lay = QVBoxLayout(body)
    lay.setContentsMargins(0, 0, 0, 0)
    if title:
        # The run's outcome at the dialog's largest size; the flagged-card heading below
        # is smaller.
        lay.addWidget(_scrolled(_summary_block(title, items), 200))
    lay.addWidget(section_header(heading))
    # Nothing here sends anything, and this is the last screen that can say so before
    # the notes are cleared.
    hint = (
        "Nothing is sent automatically. This is on your clipboard: paste it into a "
        "message to the deck author, or save a file to attach."
        if copied else
        "Nothing is sent automatically. Select and copy the text below, then paste it "
        "into a message to the deck author, or save a file to attach.")
    if archived:
        hint += " A copy is saved under Advanced → Recent card feedback."
    lay.addWidget(muted_label(hint))
    view = QPlainTextEdit(text)
    view.setReadOnly(True)
    view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
    # Palette references, not hex values, so the block follows Night Mode. `text` on
    # `base` is a pairing the platform guarantees.
    view.setStyleSheet("QPlainTextEdit { background: palette(base);"
                       " color: palette(text); border: 1px solid palette(mid); }")
    # Sized to its content, up to the 340px cap _ask_scrollable uses; a longer digest
    # scrolls.
    metrics = view.fontMetrics()
    line_count = text.count("\n") + 1
    # +4 covers rounding in Qt's document layout, which would otherwise show a needless
    # scrollbar.
    content_height = int(line_count * metrics.lineSpacing()
                         + view.document().documentMargin() * 2 + view.frameWidth() * 2) + 4
    view.setFixedHeight(min(content_height, 340))
    lay.addWidget(view)
    # Surplus height collects here, below the content and above the buttons.
    lay.addStretch()
    _ask_with_widget(body, yes_label="Close", no_label=None,
                     title=f"{APP_NAME}: card feedback", min_width=520, min_height=380,
                     actions=(("Copy again", lambda: copy_to_clipboard(text)),
                              ("Save as file", lambda: _save_feedback_file(view.toPlainText()))),
                     parent=parent)
