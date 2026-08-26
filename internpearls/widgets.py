"""The shared row and list vocabulary: a chip, a section heading, a one-line row, and
a list that builds itself in batches instead of all at once.

Extracted out of review.py, where this vocabulary used to live trapped behind that
module's own import boundary (review.py can't be imported by anything sync.py already
imports, see its own module docstring). Nothing here touches a network or the
collection, so this module is safe for any screen to import, including ones sync.py
itself will eventually build.

May import config, logic, palette and ui. Must NOT import sync, dialogs or review:
that's the boundary that keeps this module out of the same import cycle review.py was
built to dodge.
"""
from aqt.qt import (QHBoxLayout, QLabel, QPushButton, QScrollArea, Qt, QVBoxLayout,
                     QWidget)

from .palette import colors
from .ui import section_label

# The chip labels. Their colours come from the palette, so only the wording lives here.
# "changed" reads UPDATED rather than CHANGED to match the wording review.py's rows
# already shipped; renaming the label now would be a cosmetic change no task asked for.
# "skipped"/"kept" mark a decline decision review.py already carried out; they reuse
# the retired role below rather than a colour of their own: both read as "set aside",
# though a skipped or kept card is re-offered on the deck's next version while a
# retired one never returns.
CHIPS = {"new": "NEW", "changed": "UPDATED", "retired": "RETIRED", "moved": "MOVED",
         "skipped": "SKIPPED", "kept": "KEPT YOURS"}

# A chip's palette role prefix, keyed by its kind. Not a 1:1 string match: "changed"
# reuses the existing "updated_bg"/"updated_fg" pair rather than a "changed_bg" this
# repo has never had, since the wording and the role were named independently back
# when only review.py's two markers existed.
_ROLES = {"new": "new", "changed": "updated", "retired": "retired", "moved": "moved",
          "skipped": "retired", "kept": "retired"}

# Everything about a pill except its two colours: the shape and the type size, shared
# between the real pill and the probe that measures it, so the measurement can never
# describe a pill nobody paints.
_CHIP_STYLE = ("border-radius: 3px; padding: 1px 6px; font-size: 11px;"
               " font-weight: 600;")

# chip_column_width()'s answer per chip set, measured once each. Deliberately not
# computed at import: these modules are imported before a QApplication exists, and font
# metrics before that point are meaningless.
_CHIP_W = {}

# The caret column, and the gap between every column in a row's header. A card row
# draws its expander in that column (review._card_row); a row that cannot expand
# reserves it and leaves it empty. Sizes live here rather than in review.py because
# both row builders lay out against them and widgets.py is the one of the two either
# may import.
CARET_W = 14
CARET_GAP = 6


def chip_column_width(kinds=None):
    """The width of the chip column, which is also the width of every pill in it.

    Measured off the widest label at the running platform's own font rather than
    hardcoded, since the same words come out at different widths on macOS and on CI,
    and a fixed pixel value would only ever be right on one of them.

    Widening the narrower pills to match the widest is what makes the column read as
    one: pills of their own natural widths inside a fixed gutter still leave as many
    different right edges down the list.

    `kinds` is the chips the screen being laid out can actually show, and measuring
    against those rather than against every chip in the add-on is what keeps one
    screen's longest word out of another's gutter: KEPT YOURS is half again as wide as
    NEW, and measuring it into Sync decks, Reconcile and Manage decks widened every
    pill and every indent on three screens that can never show a decline at all. Left
    at None it measures every kind, which is what the one screen carrying all of them
    wants. Cached per set, since a screen's set is the same on every open.
    """
    key = tuple(kinds) if kinds is not None else tuple(CHIPS)
    if key not in _CHIP_W:
        widest = 0
        for kind in key:
            label = CHIPS.get(kind)
            if not label:
                continue
            probe = QLabel(label)
            probe.setStyleSheet(_CHIP_STYLE)
            # A stylesheet's font only reaches the widget on polish, so an unpolished
            # sizeHint here would measure the default font instead of the pill's.
            probe.ensurePolished()
            widest = max(widest, probe.sizeHint().width())
        _CHIP_W[key] = widest
    return _CHIP_W[key]


def row_text_indent(chip_columns=1, kinds=None):
    """How far a row's primary text sits from the row's own left edge: the caret
    column, then a chip column and its gap for each column the row draws in front of
    its primary label.

    What a card row's expanded body indents by, so its answer lines up under the line
    it belongs to rather than under the caret. Read from the same sizes `simple_row`
    lays out below, so the two row builders cannot drift apart, and counted from the
    caller's own leading columns rather than assumed to be one: a row that grows a
    second column in front of its text and leaves this alone hangs its whole body a
    chip-width left of the line it belongs to.
    """
    return CARET_W + CARET_GAP + chip_columns * (chip_column_width(kinds) + CARET_GAP)


def chip_cell(kind, kinds=None):
    """A row's kind as a pill, in a fixed-width column of its own.

    The container is what is fixed-width, and a `kind` not in CHIPS (including None)
    gets an empty one rather than nothing at all, so an unchipped row reserves the same
    gutter and its text starts at the same x as every other row's. Without that, one
    list runs its primary text out from five different left edges depending on which
    word each row's chip happens to be, which reads as ragged prose rather than a
    column.

    A real widget rather than the inline `<span>` this used to be, because Qt's
    rich-text engine silently drops both `border-radius` and `padding` on an inline
    span: measured, a span carrying either one comes out at exactly the width of a span
    carrying neither. The same declarations on a QLabel round and pad correctly. So
    alignment and shape are the same fix, not two.

    A background always needs its own foreground alongside it, since plain text colour
    comes from the platform palette and a hardcoded background does not flip with it
    under Night Mode.

    `kinds` is the screen's own chip set, passed straight to chip_column_width: every
    cell in one list has to be measured against the same set or the column stops being
    a column.
    """
    width = chip_column_width(kinds)
    cell = QWidget()
    cell.setFixedWidth(width)
    label = CHIPS.get(kind)
    if not label:
        return cell
    lay = QHBoxLayout(cell)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)
    role = _ROLES[kind]
    c = colors()
    pill = QLabel(label)
    pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
    pill.setMinimumWidth(width)
    pill.setStyleSheet(f"background-color: {c[f'{role}_bg']}; color: {c[f'{role}_fg']};"
                       f" {_CHIP_STYLE}")
    lay.addWidget(pill)
    return cell


def decision_cell(options, state, on_change, card_label=""):
    """A row's decision as a compact segmented control: one checkable button per
    option, exactly one checked. Selected colour says what the choice does: accept
    roles for Import/Apply, updated for Skip/Keep, decline for Never.

    `card_label` names the card these buttons decide about, for anyone not reading the
    row they sit on: a 300-row list otherwise announces hundreds of controls called
    nothing but "Import", with no way to tell which card any of them belongs to.

    Painted as one rounded group rather than a row of separate buttons: only the
    outer corners round (the first button's left, the last button's right), and
    every button after the first drops its own left border so the shared edge
    between two buttons is drawn once, not twice. `_HEIGHT` is fixed so a selected
    button's bold weight can't nudge that one button taller than its unselected
    neighbours.
    """
    _SELECTED_ROLE = {"import": "accept", "apply": "accept",
                      "skip": "updated", "keep": "updated", "never": "decline"}
    _RADIUS = 6
    _HEIGHT = 22
    cell = QWidget()
    lay = QHBoxLayout(cell)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)
    c = colors()
    cell.buttons = {}
    count = len(options)

    def _shape(index):
        left = _RADIUS if index == 0 else 0
        right = _RADIUS if index == count - 1 else 0
        return (f"border: 1px solid {c['cell_rule']};"
                f"{' border-left: none;' if index else ''}"
                f" border-top-left-radius: {left}px; border-bottom-left-radius: {left}px;"
                f" border-top-right-radius: {right}px;"
                f" border-bottom-right-radius: {right}px;"
                f" min-height: {_HEIGHT}px; max-height: {_HEIGHT}px;")

    def _style(value, checked, index):
        base = f"{_shape(index)} padding: 2px 9px; font-size: 11px;"
        if not checked:
            return (f"QPushButton {{ {base}"
                    f" color: {c['dim']}; background: transparent; }}")
        role = _SELECTED_ROLE.get(value, "retired")
        return (f"QPushButton {{ {base} font-weight: 600;"
                f" color: {c[role + '_fg']}; background: {c[role + '_bg']}; }}")

    def set_state(value):
        for i, (v, b) in enumerate(cell.buttons.items()):
            checked = v == value
            b.setChecked(checked)
            b.setStyleSheet(_style(v, checked, i))
    cell.set_state = set_state

    for value, label in options:
        b = QPushButton(label)
        b.setCheckable(True)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setAccessibleName(f"{label}: {card_label}" if card_label else label)
        b.clicked.connect(lambda _=False, v=value: (set_state(v), on_change(v)))
        cell.buttons[value] = b
        lay.addWidget(b)
    set_state(state)
    return cell


def section_header(text):
    """A bold heading atop a group of rows, e.g. a deck name grouping its cards.

    top_margin matches the spacing review.py already uses between one deck's rows and
    the next section's heading, so a screen built on this module lines up with the one
    it was extracted from.
    """
    return section_label(text, top_margin=14)


def simple_row(chip_kind, primary_html, trailing_html="", card_columns=True,
               chips=None):
    """One line: an optional chip, the primary content, and optional trailing text.
    No caret, no expansion: for a screen where the row itself is the whole content
    rather than a summary that opens into more.

    `card_columns` reserves the two columns a card row carries in front of its own
    text (the caret column, then chip_cell's fixed-width chip column), whether or not
    this row has anything to put in either, so a retired card's row and a new card's
    row can stand under one deck heading and read as one list rather than two.

    Both columns exist purely to line up against something, so a section holding
    nothing to line up against passes False and starts its text at the row's left edge,
    flush with the heading above it and whatever reads below. That is a decision about
    the section, not about the row: do NOT infer it from `chip_kind` being None
    instead. The update screen's card sections keep the columns for their own unchipped
    rows, since a chipped row sits beside them; its per-deck summary is a section of
    its own where nothing is ever chipped and nothing ever expands, so that one
    declines them even though rows further down the same screen do not.

    `chips` is the chip kinds this row's own screen can show, passed to chip_cell so
    the column is measured against those rather than against every chip the add-on has
    (see chip_column_width). Every row in one list must be given the same set.

    Top-aligned, so a chip beside a wrapping primary sits against its first line rather
    than floating halfway down it. `trailing_html` is a second, non-wrapping label in
    muted text off to the row's right (a count, a destination) rather than folded into
    the same paragraph, since it is secondary information the reader compares across
    rows rather than reads inline with the primary text. The row carries a right margin
    of its own so that label stops short of whatever frame the list is drawn inside
    rather than running its glyphs up against the border line.
    """
    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(CARET_W + CARET_GAP if card_columns else 0, 5, CARET_GAP, 6)
    lay.setSpacing(CARET_GAP)

    if card_columns:
        lay.addWidget(chip_cell(chip_kind, chips), 0, Qt.AlignmentFlag.AlignTop)

    primary = QLabel(primary_html)
    primary.setWordWrap(True)
    primary.setTextFormat(Qt.TextFormat.RichText)
    lay.addWidget(primary, 1)

    if trailing_html:
        trailing = QLabel(trailing_html)
        trailing.setTextFormat(Qt.TextFormat.RichText)
        trailing.setStyleSheet(f"color: {colors()['muted']};")
        lay.addWidget(trailing, 0)

    return row


class StreamingList(QScrollArea):
    """A scroll area that builds its rows in batches instead of all at once.

    Measured offscreen, building and showing the current review list costs about 2ms
    per card: a 2903-card first sync takes 5.7 seconds of dead time with no feedback
    before the dialog even appears. Building only the first `batch` rows up front, and
    the next batch only once the reader has actually scrolled near the bottom, is what
    turns that multi-second freeze into a screen that opens in roughly the time one
    batch costs, whatever is still pending.

    `build_row(item)` builds one row's widget; it is called once per item, in order,
    the first time that item's batch is built. `shown()`/`total()` report progress for
    a caller that wants to show it. `fill_all()` is for a test, or a caller that
    genuinely needs every row built (e.g. before printing the whole list), and is safe
    to call on an already-exhausted list: it does nothing rather than rebuilding.

    Batching is driven by two things, not one. Scrolling is the obvious one; the other
    is the viewport growing past what is already built, which produces no scroll at all:
    the scrollbar's range collapses to zero, `valueChanged` never fires again, and every
    unbuilt row is stranded (a confirmation that silently listed 50 of 300 cards for
    anyone who enlarged the dialog before scrolling). So a resize refills to the bottom
    of the viewport as well. Both paths still build a batch at a time rather than
    everything, so the property this class exists for holds either way.
    """

    def __init__(self, build_row, items, batch=50):
        super().__init__()
        self._build_row = build_row
        self._items = items
        self._batch = batch
        self._shown = 0

        # Rows live in their own inner container with its own layout, kept separate
        # from the stretch below it. Appending only ever touches the rows layout, so
        # the stretch this container sits above in the outer layout never needs to be
        # found, removed, and re-added: it simply never moves.
        self._rows_container = QWidget()
        self._rows_layout = QVBoxLayout(self._rows_container)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(0)

        body = QWidget()
        outer = QVBoxLayout(body)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._rows_container)
        outer.addStretch()   # keeps a short list pinned to the top, not floating

        self.setWidgetResizable(True)
        self.setWidget(body)

        self.verticalScrollBar().valueChanged.connect(self._maybe_extend)
        self._extend()

    def shown(self):
        return self._shown

    def total(self):
        return len(self._items)

    def _maybe_extend(self, _value=None):
        bar = self.verticalScrollBar()
        if bar.maximum() - bar.value() <= self.viewport().height():
            self._extend()

    def resizeEvent(self, event):
        """Refill after Qt has given this list its real height.

        Fires on the first layout (the viewport is 0-tall while __init__ runs, so the
        one batch built there is a guess) and again on every enlargement, which is the
        case the scroll handler above cannot see at all: growing the dialog past the
        built rows leaves nothing to scroll, so `valueChanged` never fires again.
        """
        super().resizeEvent(event)
        self._fill_viewport()

    def _fill_viewport(self):
        """Build batches until the rows are taller than the viewport, or run out.

        Measured off the rows' own sizeHint rather than the scrollbar's range: the range
        is only recomputed on Qt's own layout pass, so inside this loop it still reports
        the height from before the batch just appended, and the loop would build
        everything. A wrapping row's sizeHint is its unwrapped height, so this can
        overshoot the viewport by a row or two; it never undershoots, which is the
        direction that would strand rows again.
        """
        while (self._shown < self.total()
               and self._rows_container.sizeHint().height() <= self.viewport().height()):
            self._extend()

    def _extend(self):
        """Build the next `batch` rows and append them, or do nothing once every item
        has already been built."""
        if self._shown >= self.total():
            return
        end = min(self._shown + self._batch, self.total())
        for item in self._items[self._shown:end]:
            row = self._build_row(item)
            self._rows_layout.addWidget(row)
            # A row appended to an already-visible list is only shown on Qt's next
            # layout pass, and a hidden item contributes nothing to its layout's
            # sizeHint. Showing it here is what lets _fill_viewport below measure the
            # batch it just built rather than the height from before it.
            row.setVisible(True)
        self._shown = end

    def fill_all(self):
        while self._shown < self.total():
            self._extend()
