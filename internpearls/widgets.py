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
from aqt.qt import QHBoxLayout, QLabel, QScrollArea, Qt, QVBoxLayout, QWidget

from .palette import colors
from .ui import section_label

# The chip labels. Their colours come from the palette, so only the wording lives here.
# "changed" reads UPDATED rather than CHANGED to match the wording review.py's rows
# already shipped; renaming the label now would be a cosmetic change no task asked for.
CHIPS = {"new": "NEW", "changed": "UPDATED", "retired": "RETIRED", "moved": "MOVED"}

# A chip's palette role prefix, keyed by its kind. Not a 1:1 string match: "changed"
# reuses the existing "updated_bg"/"updated_fg" pair rather than a "changed_bg" this
# repo has never had, since the wording and the role were named independently back
# when only review.py's two markers existed.
_ROLES = {"new": "new", "changed": "updated", "retired": "retired", "moved": "moved"}


def chip_html(kind):
    """A row's kind as a small inline pill, ready to sit inside a rich-text paragraph.

    Empty string for `kind` not in CHIPS (including None), so a caller can always
    prepend the result with no branching of its own.

    Same markup shape as review._marker_html, on purpose: a background always needs
    its own foreground alongside it, since plain text colour comes from the platform
    palette and a hardcoded background does not flip with it under Night Mode. And the
    chip lives inside the row's own single rich-text paragraph rather than as a widget
    beside it, because a separate marker widget starts each row's text at a different
    x depending on whether that row happens to have one, and wraps against the
    marker's edge instead of the row's.
    """
    label = CHIPS.get(kind)
    if not label:
        return ""
    role = _ROLES[kind]
    c = colors()
    background, foreground = c[f"{role}_bg"], c[f"{role}_fg"]
    return (f'<span style="background-color: {background}; color: {foreground};'
            f' font-size: 11px;">&nbsp;{label}&nbsp;</span>&nbsp;&nbsp;')


def section_header(text):
    """A bold heading atop a group of rows, e.g. a deck name grouping its cards.

    top_margin matches the spacing review.py already uses between one deck's rows and
    the next section's heading, so a screen built on this module lines up with the one
    it was extracted from.
    """
    return section_label(text, top_margin=14)


def simple_row(chip_kind, primary_html, trailing_html=""):
    """One line: an optional chip, the primary content, and optional trailing text.
    No caret, no expansion: for a screen where the row itself is the whole content
    rather than a summary that opens into more.

    The chip rides inside the primary label's own rich text (see chip_html), never as
    a widget of its own beside it, for the reason chip_html's docstring gives.
    `trailing_html` is a second, non-wrapping label in muted text off to the row's
    right (a count, a timestamp) rather than folded into the same paragraph, since it
    is secondary information the reader compares across rows rather than reads inline
    with the primary text.
    """
    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 5, 0, 6)
    lay.setSpacing(6)

    primary = QLabel(chip_html(chip_kind) + primary_html)
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

    def _extend(self):
        """Build the next `batch` rows and append them, or do nothing once every item
        has already been built."""
        if self._shown >= self.total():
            return
        end = min(self._shown + self._batch, self.total())
        for item in self._items[self._shown:end]:
            self._rows_layout.addWidget(self._build_row(item))
        self._shown = end

    def fill_all(self):
        while self._shown < self.total():
            self._extend()
