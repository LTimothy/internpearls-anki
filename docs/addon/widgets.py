"""Shared row and list widgets: a chip, a section heading, a one-line row, and a list that
builds its rows in batches.

May import config, logic, palette, platform and ui. Must not import sync, dialogs or review,
which would create an import cycle."""
from contextlib import contextmanager

from aqt.qt import (QApplication, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea,
                     QSize, Qt, QVBoxLayout, QWidget)

from .palette import colors
from .platform import new_work_request, platform, platform_owner_id
from .ui import _FocusButton, hint_label, section_label

# Chip labels by kind. Colours come from the palette through _ROLES.
CHIPS = {"new": "NEW", "changed": "UPDATED", "retired": "RETIRED", "moved": "MOVED",
         "kept": "KEPT YOURS", "held": "LATER",
         # Review row kinds for the AI wizard (ai_dialog.py).
         "blocked": "BLOCKED", "warn": "WARNING", "ok": "OK", "revised": "REVISED",
         "imported": "IMPORTED",
         # Row kinds for the AI Backends window (ai_setup.py).
         "found": "FOUND", "notfound": "NOT FOUND",
         "notresponding": "NOT RESPONDING", "ignored": "IGNORED",
         "preferred": "PREFERRED",
         # Input-page row kinds for the AI wizard.
         "ready": "READY", "notsetup": "NOT SET UP",
         "auto": "AUTO", "thorough": "THOROUGH", "quick": "QUICK",
         "deck": "DECK", "skills": "SKILLS",
         # Progress-row kinds for the AI wizard: the stage a run is in.
         "drafting": "DRAFTING", "verifying": "VERIFYING",
         "reviewing": "REVIEWING", "working": "WORKING",
         "checking": "CHECKING"}

# A chip's palette role prefix by kind. Kinds share roles: "changed" uses the "updated"
# pair.
_ROLES = {"new": "new", "changed": "updated", "retired": "retired", "moved": "moved",
          "kept": "retired", "held": "updated",
          "blocked": "decline", "warn": "updated", "ok": "accept", "revised": "updated",
          "imported": "accept",
          "found": "accept", "notfound": "decline", "notresponding": "updated",
          "ignored": "retired", "preferred": "new",
          "ready": "accept", "notsetup": "decline",
          "auto": "new", "thorough": "new", "quick": "new",
          "deck": "retired", "skills": "retired",
          "drafting": "new", "verifying": "new",
          "reviewing": "new", "working": "new", "checking": "new"}

# A pill's shape and type size, shared by the real pill and the probe that measures it.
_CHIP_STYLE = ("border-radius: 3px; padding: 1px 6px; font-size: 11px;"
               " font-weight: 600;")

# chip_column_width() results per chip set. Not computed at import: font metrics need a
# QApplication.
_CHIP_W = {}

# The caret column width and the gap between columns in a row's header. Both row builders
# lay out against them.
CARET_W = 14
CARET_GAP = 6

# field_inset()'s fallback for disabled widgets, cached per class.
_FIELD_INSET = {}


def _visual_inset(widget, at=None):
    """Pixels from `widget`'s left edge to where the running style starts painting, read
    from a grab of the widget. QStyle's subControlRect and subElementRect are unreliable for
    this under macOS's native style.

    Scans `widget`'s middle row, or the row level with `at` when given, for the first two
    adjacent pixels that differ from the window background. Scanning the full width also
    catches an offset the layout adds in front of the leading control.

    QPalette is imported lazily because the fake Qt in tests/ has none."""
    from aqt.qt import QPalette
    pix = widget.grab()
    img = pix.toImage()
    if img.isNull() or img.height() == 0:
        return 0
    dpr = img.devicePixelRatio() or 1
    if at is not None:
        row = round(at.mapTo(widget, at.rect().center()).y() * dpr)
        row = max(0, min(row, img.height() - 1))
    else:
        row = img.height() // 2
    bg = widget.palette().color(QPalette.ColorRole.Window)
    limit = min(img.width(), int(widget.width() * dpr))
    run = 0
    for px in range(limit):
        if img.pixelColor(px, row) != bg:
            run += 1
            if run >= 2:
                return round((px - 1) / dpr)
        else:
            run = 0
    return 0


def field_inset(container, leading):
    """Pixels from `container`'s left edge to where the running style starts painting its
    content, scanned level with `leading`, the control that draws the field's left edge.

    A disabled `leading` paints too faintly to scan, so its class is measured from a fresh
    enabled (and checked, if checkable) probe, cached per class. Returns 0 when `container`
    cannot be grabbed, as under the fake Qt in tests/."""
    if not hasattr(container, "grab"):
        return 0
    if leading.isEnabled():
        return _visual_inset(container, at=leading)
    cls = type(leading)
    if cls not in _FIELD_INSET:
        probe = cls()
        setter = getattr(probe, "setChecked", None)
        if setter is not None:
            setter(True)
        probe.resize(probe.sizeHint())
        probe.ensurePolished()
        _FIELD_INSET[cls] = _visual_inset(probe)
    return _FIELD_INSET[cls]


def field_slot(widget):
    """Wrap a bare field in a box with a zero-margin layout, so `align_field_column` has a
    left margin to adjust. A field already in a box of its own does not need it."""
    slot = QWidget()
    lay = QHBoxLayout(slot)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)
    lay.addWidget(widget)
    return slot


def align_field_column(rows):
    """Add left margin to a settings grid's field containers so every field's painted left
    edge lands on the same x as the most-inset one. Native styles inset controls differently
    (a macOS QComboBox paints about 4px inside its geometry); Fusion insets nothing, so this
    is a no-op there.

    `rows` is `[(container, leading), ...]`: `container` is the widget in the grid's field
    column (wrap a bare leaf with `field_slot` first) and `leading` is the control inside it
    that draws the field's left edge. Safe to call again when a leading control changes:
    margins are reset to 0 before measuring.

    The applied margin is measured and corrected, since a native style can move a control
    less than the margin asked for. Does nothing under the fake Qt in tests/, whose layouts
    have no contentsMargins()."""
    if not rows or not hasattr(rows[0][0], "grab"):
        return
    for container, _leading in rows:
        lay = container.layout()
        m = lay.contentsMargins()
        if m.left():
            lay.setContentsMargins(0, m.top(), m.right(), m.bottom())
        lay.activate()
    insets = [field_inset(container, leading) for container, leading in rows]
    target = max(insets, default=0)
    if not target:
        return
    for (container, leading), inset in zip(rows, insets):
        want = target - inset
        if not want:
            continue
        lay = container.layout()
        applied = 0
        # The shortfall is a few near-constant pixels, so this converges in a pass or two;
        # the cap guards against an unmeasured style.
        for _ in range(5):
            m = lay.contentsMargins()
            lay.setContentsMargins(applied, m.top(), m.right(), m.bottom())
            lay.activate()
            actual = leading.mapTo(container, leading.rect().topLeft()).x()
            if actual >= want:
                break
            applied += want - actual


def chip_column_width(kinds=None):
    """The width of the chip column, which is also the width of every pill in it: the widest
    label at the running platform's font.

    `kinds` is the chips the screen can show, so one screen's longest word does not widen
    another's column. None measures every kind. Cached per set."""
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
    """How far a row's primary text sits from the row's left edge: the caret column, then a
    chip column and its gap for each column drawn in front of the primary label. A card
    row's expanded body indents by this."""
    return CARET_W + CARET_GAP + chip_columns * (chip_column_width(kinds) + CARET_GAP)


def chip_cell(kind, kinds=None):
    """A row's kind as a pill in a fixed-width column.

    A `kind` not in CHIPS (including None) gets an empty container of the same width, so
    every row's text starts at the same x. The pill is a QLabel because Qt's rich text drops
    `border-radius` and `padding` on an inline span. A background is always set with its own
    foreground, since palette text colour flips under Night Mode and a fixed background does
    not.

    `kinds` goes to chip_column_width; every cell in one list must use the same set."""
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
    """A row's decision as a compact segmented control: one checkable button per option,
    exactly one checked. Selected colour says what the choice does: accept roles for
    Import/Apply, updated for Later/Keep, decline for Never.

    `card_label` names the card these buttons decide about, for a reader who cannot see the
    row they sit on.

    Only the outer corners round, and every button after the first drops its left border so
    a shared edge is drawn once. `_HEIGHT` is fixed so a selected button's bold weight
    cannot make it taller."""
    _SELECTED_ROLE = {"import": "accept", "apply": "accept",
                      "held": "updated", "keep": "updated",
                      "never": "decline", "frozen": "decline"}
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
        disabled = (f"QPushButton:disabled {{ color: {c['muted']};"
                    " background: transparent; }")
        if not checked:
            return (f"QPushButton {{ {base}"
                    f" color: {c['dim']}; background: transparent; }}" + disabled)
        role = _SELECTED_ROLE.get(value, "retired")
        return (f"QPushButton {{ {base} font-weight: 600;"
                f" color: {c[role + '_fg']}; background: {c[role + '_bg']}; }}" + disabled)

    def set_state(value):
        for i, (v, b) in enumerate(cell.buttons.items()):
            checked = v == value
            b.setChecked(checked)
            b.setStyleSheet(_style(v, checked, i))
    cell.set_state = set_state

    for value, label in options:
        b = _FocusButton(label)
        b.setCheckable(True)
        b.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setAccessibleName(f"{label}: {card_label}" if card_label else label)
        b.clicked.connect(lambda _=False, v=value: (set_state(v), on_change(v)))
        cell.buttons[value] = b
        lay.addWidget(b)
    set_state(state)
    return cell


def section_header(text):
    """A bold heading above a group of rows, such as a deck name. Rendered as plain text,
    since a deck name is the source's own string."""
    label = section_label(text, top_margin=14)
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


def simple_row(chip_kind, primary_html, trailing_html="", card_columns=True,
               chips=None):
    """One line: an optional chip, the primary content, and optional trailing text. No caret
    and no expansion.

    `card_columns` reserves the caret and chip columns a card row has, so this row lines up
    with card rows in the same section. Pass False for a section with nothing to line up
    against. Decide it per section; do not infer it from `chip_kind` being None.

    `chips` is the chip kinds the screen can show, passed to chip_cell. Every row in one
    list must be given the same set.

    `trailing_html` is a separate non-wrapping muted label at the row's right. The row is
    top-aligned, so a chip sits against the first line of a wrapping primary."""
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


class _Page(QWidget):
    """One batch of StreamingList rows that reports a plain height instead of a
    height-for-width, so a layout pass does not ask every visible row for its height. The
    height comes from the page's layout at the list's width and is asked again when the
    width changes."""

    def __init__(self):
        super().__init__()
        self._hint_width = None

    def hasHeightForWidth(self):
        return False

    def sizeHint(self):
        parent = self.parentWidget()
        width = parent.width() if parent is not None else self.width()
        self._hint_width = width
        lay = self.layout()
        height = (lay.totalHeightForWidth(width) if lay.hasHeightForWidth()
                  else lay.totalSizeHint().height())
        return QSize(width, height)

    def minimumSizeHint(self):
        return QSize(0, self.sizeHint().height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # The list asked for this page's height at another width; wrapped rows need a
        # different height at this one.
        if self._hint_width is not None and self._hint_width != self.width():
            self.updateGeometry()


class StreamingList(QScrollArea):
    """A scroll area that builds its rows in batches: the first `batch` up front, the next
    when the reader scrolls near the bottom or enlarges the viewport, so a long list opens
    in about the time one batch costs.

    `build_row(item)` builds one row's widget and is called once per item, in order.
    `shown()` and `total()` report progress. `fill_all()` builds every remaining row and
    does nothing on a full list."""

    def __init__(self, build_row, items, batch=50):
        super().__init__()
        self._build_row = build_row
        self._items = items
        self._batch = batch
        self._shown = 0
        self._built = 0
        self._generation = 0
        # One page widget per batch. Rows are built into a hidden page that is shown once:
        # showing a row inside a visible list lays out every visible row again.
        self._pages = []

        # Rows have their own container and layout, so appending never touches the stretch
        # below.
        self._rows_container = QWidget()
        self._rows_layout = QVBoxLayout(self._rows_container)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(0)

        body = QWidget()
        outer = QVBoxLayout(body)
        # Right gutter, so a row's last control clears the frame line and the overlay
        # scrollbar.
        outer.setContentsMargins(0, 0, 10, 0)
        outer.setSpacing(0)
        outer.addWidget(self._rows_container)
        outer.addStretch()   # keeps a short list pinned to the top, not floating

        self.setWidgetResizable(True)
        self.setWidget(body)

        self._last_scroll = 0.0
        self._scroll_value = 0
        self._scroll_maximum = 0
        self._range_shown = 0
        self._view_size = None
        self._check_bottom = False
        self._editing = False
        bar = self.verticalScrollBar()
        bar.valueChanged.connect(self._maybe_extend)
        bar.actionTriggered.connect(self._scroll_action)
        bar.rangeChanged.connect(self._range_changed)

        # Idle prefetch: once the viewport is filled, build a bounded lookahead a few rows
        # at a time. Prefetched rows wait in a hidden page, so the scrollbar only moves when
        # the reader scrolls. A tick is skipped while the reader is scrolling.
        self._prefetching = False
        self._idle = platform().create_timer(
            platform_owner_id(self), self._idle_extend, self.IDLE_DELAY_MS,
            single_shot=True)
        self._geometry = platform().create_timer(
            platform_owner_id(self), self._check_geometry, 0, single_shot=True)
        self._extend()

    PREFETCH_AHEAD = 100
    IDLE_CHUNK = 3
    IDLE_DELAY_MS = 150
    SCROLL_QUIET_S = 0.4

    def built(self):
        """Rows constructed so far, shown or waiting hidden."""
        return self._built

    def rows(self):
        """The row widgets built so far, in item order."""
        return [page.layout().itemAt(i).widget()
                for page in self._pages for i in range(page.layout().count())]

    def _page(self, index):
        """The page at `index`, created hidden and appended if it is new."""
        while len(self._pages) <= index:
            page = _Page()
            lay = QVBoxLayout(page)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(0)
            page.setVisible(False)
            self._rows_layout.addWidget(page)
            self._pages.append(page)
        return self._pages[index]

    def _build_upto(self, end):
        """Build rows up to item `end` into their (still hidden) pages."""
        while self._built < end:
            row = self._build_row(self._items[self._built])
            if (not self._pages or self._pages[-1].layout().count() == self._batch
                    or self._built == self._shown):
                self._page(len(self._pages))
            self._pages[-1].layout().addWidget(row)
            self._built += 1

    def _idle_extend(self):
        limit = min(self.total(), self.shown() + self.PREFETCH_AHEAD)
        if self.built() >= limit:
            self._idle.stop()
            return
        if self._prefetching:       # one chunk in flight at a time
            self._idle.start()
            return
        if (self.isVisible()
                and platform().monotonic() - self._last_scroll > self.SCROLL_QUIET_S):
            start = self.built()
            end = min(start + self.IDLE_CHUNK, limit)
            generation = self._generation

            def build(end):
                if generation != self._generation:
                    return    # the list was replaced while this chunk was in flight
                self._prefetching = False
                if self.built() != start:
                    return    # a scroll built these rows first; the next tick resumes
                self._build_upto(end)

            def abandon(_error):
                if generation == self._generation:
                    self._prefetching = False

            request = new_work_request(
                self, "list-prefetch", "streaming-list.prefetch",
                inputs={"count": end - start, "start": start})

            def prefetch(context):
                context.checkpoint("list-prefetch:start")
                context.checkpoint("list-prefetch:complete")
                return end

            self._prefetching = True
            platform().start_work(request, prefetch, build, abandon).start()
        if self.isVisible():
            self._start_prefetch()   # hidden, showEvent restarts it

    def _start_prefetch(self):
        if (self.built() < min(self.total(), self.shown() + self.PREFETCH_AHEAD)
                and not self._idle.is_active()):
            self._idle.start()

    def showEvent(self, event):
        super().showEvent(event)
        self._start_prefetch()

    def shown(self):
        return self._shown

    def total(self):
        return len(self._items)

    def _maybe_extend(self, _value=None):
        self._last_scroll = platform().monotonic()
        bar = self.verticalScrollBar()
        previous, self._scroll_value = self._scroll_value, bar.value()
        # A shrinking range can clamp the value without the reader scrolling.
        if self._editing or (_value is not None and bar.value() <= previous):
            return
        if bar.maximum() - bar.value() <= self.viewport().height():
            self._extend()

    def wheelEvent(self, event):
        shown = self.shown()
        super().wheelEvent(event)
        delta = event.pixelDelta().y() or event.angleDelta().y()
        if delta < 0 and self.shown() == shown:
            self._maybe_extend()

    def _scroll_action(self, action):
        bar = self.verticalScrollBar()
        actions = bar.SliderAction
        if (bar.value() == bar.maximum()
                and action in (actions.SliderSingleStepAdd.value,
                               actions.SliderPageStepAdd.value, actions.SliderToMaximum.value)):
            self._maybe_extend()

    def keyPressEvent(self, event):
        shown = self.shown()
        space = (event.key() == Qt.Key.Key_Space
                 and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if space:
            bar = self.verticalScrollBar()
            bar.triggerAction(bar.SliderAction.SliderPageStepAdd)
            event.accept()
        else:
            super().keyPressEvent(event)
        if (self.shown() == shown
                and (space or event.key() in (Qt.Key.Key_Down, Qt.Key.Key_PageDown,
                                              Qt.Key.Key_End))):
            self._maybe_extend()

    def _range_changed(self, _minimum, maximum):
        previous, self._scroll_maximum = self._scroll_maximum, maximum
        shown, self._range_shown = self._range_shown, self.shown()
        if maximum < previous:
            self._scroll_value = self.verticalScrollBar().value()
        if self._editing or self.viewport().height() <= 0:
            return
        # A growing range may come from the batch just revealed.
        self._check_bottom = (self._check_bottom or maximum < previous
                              or self.shown() <= shown)
        self._geometry.start()

    def _check_geometry(self):
        check_bottom, self._check_bottom = self._check_bottom, False
        if check_bottom:
            self._refill()
        elif not self._editing:
            self._fill_viewport()

    def _refill(self):
        if not self._editing and self.viewport().height() > 0:
            self._maybe_extend()
            self._fill_viewport()

    def resizeEvent(self, event):
        """Refill once Qt has given this list its real height. The viewport is 0-tall during
        __init__, and enlarging past the built rows produces no scroll event."""
        super().resizeEvent(event)
        previous, self._view_size = self._view_size, (self.width(), self.height())
        self._fill_viewport()
        if previous is not None and previous != self._view_size:
            self._check_bottom = True
            self._geometry.start()

    def _fill_viewport(self):
        """Build batches until the rows are taller than the viewport or run out.

        Measures the rows' sizeHint, since the scrollbar's range is stale until Qt's next
        layout pass. A wrapping row's sizeHint is its unwrapped height, so this can
        overshoot by a row or two."""
        while (self._shown < self.total() and self._rows_container.sizeHint().height()
               <= self.viewport().height()):
            self._extend()

    def _extend(self):
        """Reveal the next batch of rows, building any the prefetch has not, or do
        nothing once every item is shown."""
        if self._shown >= self.total():
            return
        end = min(self._shown + self._batch, self.total())
        self._build_upto(end)
        # Shown here rather than on Qt's next layout pass, so _fill_viewport measures
        # the batch it just revealed.
        start = 0
        for page in self._pages:
            count = page.layout().count()
            if start == self._shown:
                page.setVisible(True)
                self._shown += count
                break
            start += count
        self._start_prefetch()

    def fill_all(self):
        while self._shown < self.total():
            self._extend()

    def index_of(self, predicate):
        """The first matching item's index, or None if it is absent."""
        return next((i for i, item in enumerate(self._items) if predicate(item)), None)

    def _row_at(self, index):
        for page in self._pages:
            if index < page.layout().count():
                return page, index
            index -= page.layout().count()
        return None, None

    @staticmethod
    def _controls(row):
        return [w for w in [row] + row.findChildren(QWidget)
                if w.focusPolicy() != Qt.FocusPolicy.NoFocus and w.isEnabled()]

    @staticmethod
    def _discard(widget):
        widget.setVisible(False)
        widget.setParent(None)
        widget.deleteLater()

    @contextmanager
    def _edit(self):
        bar = self.verticalScrollBar()
        position = bar.value()
        self._editing = True
        self._generation += 1
        self._prefetching = False
        try:
            yield
        finally:
            if bar.value() != position:
                bar.setValue(position)
            self._scroll_value = bar.value()
            self._editing = False
            self._refill()
            self._start_prefetch()

    def _focus_after_removal(self, index):
        end = min(self.total(), max(self.built(), index + self._batch))
        for i in range(index, end):
            self._build_upto(i + 1)
            page, offset = self._row_at(i)
            controls = self._controls(page.layout().itemAt(offset).widget())
            if controls:
                start = 0
                for page in self._pages:
                    page.setVisible(True)
                    start += page.layout().count()
                    if start > i:
                        self._shown = max(self._shown, start)
                        break
                controls[0].setFocus()
                return
        for row in reversed(self.rows()[:index]):
            controls = self._controls(row)
            if controls:
                controls[0].setFocus()
                return

    def replace_item(self, index, item):
        """Replace one item, rebuilding only its row if it is already built."""
        if not 0 <= index < self.total():
            raise IndexError(index)
        with self._edit():
            if index < self.built():
                page, offset = self._row_at(index)
                layout = page.layout()
                old = layout.itemAt(offset).widget()
                controls = self._controls(old)
                focus = QApplication.focusWidget()
                focus_index = controls.index(focus) if focus in controls else None
                row = self._build_row(item)
                layout.takeAt(offset)
                layout.insertWidget(offset, row)
                self._discard(old)
                row.setVisible(True)
                if focus_index is not None:
                    controls = self._controls(row)
                    if controls:
                        controls[min(focus_index, len(controls) - 1)].setFocus()
            self._items[index] = item

    def remove_item(self, index):
        """Remove one item and its row, retaining every later row and page."""
        if not 0 <= index < self.total():
            raise IndexError(index)
        with self._edit():
            focused = False
            if index < self.built():
                page, offset = self._row_at(index)
                row = page.layout().takeAt(offset).widget()
                focused = QApplication.focusWidget() in self._controls(row)
                self._discard(row)
                self._built -= 1
                if index < self.shown():
                    self._shown -= 1
                if not page.layout().count():
                    page_index = self._pages.index(page)
                    self._rows_layout.takeAt(page_index)
                    self._pages.pop(page_index)
                    self._discard(page)
            del self._items[index]
            if focused:
                self._focus_after_removal(index)

    def reset(self, items):
        """Replace the items and rebuild only the first batch. Anything holding the old
        rows must let go of them first."""
        self._generation += 1
        layout = self._rows_layout
        while layout.count():
            widget = layout.takeAt(0).widget()
            if widget is not None:
                widget.setVisible(False)
                widget.setParent(None)
                widget.deleteLater()
        self._items = items
        self._pages = []
        self._prefetching = False
        self._shown = 0
        self._built = 0
        bar = self.verticalScrollBar()
        if bar.value():
            bar.setValue(0)
        self._extend()
        if self.viewport().height() > 0:
            self._fill_viewport()


class _SearchField(QLineEdit):
    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.returnPressed.emit()
            event.accept()
        elif event.key() == Qt.Key.Key_Escape and self.text():
            self.clear()
            event.accept()
        else:
            super().keyPressEvent(event)


class FilterBar(QWidget):
    """Filter options and a search field, with a muted count line that shows while
    either is active. `on_change(mode, query)` runs when an option is picked and
    shortly after the search text settles. Escape in the field clears it; Enter applies
    it at once and never reaches the dialog's default button."""

    SEARCH_DELAY_MS = 150

    def __init__(self, modes, on_change, placeholder="Search cards"):
        super().__init__()
        self._mode = modes[0][0]
        self._on_change = on_change
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        row = QWidget()
        rlay = QHBoxLayout(row)
        rlay.setContentsMargins(0, 0, 0, 0)
        rlay.setSpacing(10)
        self.options = decision_cell(list(modes), self._mode, self._pick)
        for value, label in modes:
            self.options.buttons[value].setAccessibleName(f"Show {label.lower()} cards")
        rlay.addWidget(self.options, 0)
        self.search = _SearchField()
        self.search.setPlaceholderText(placeholder)
        self.search.setAccessibleName(placeholder)
        rlay.addWidget(self.search, 1)
        lay.addWidget(row)
        self._count = hint_label("")
        self._count.setVisible(False)
        lay.addWidget(self._count)
        self._timer = platform().create_timer(
            platform_owner_id(self), self._apply, self.SEARCH_DELAY_MS, single_shot=True)
        self.search.textChanged.connect(lambda _text: self._timer.start())
        self.search.returnPressed.connect(lambda: self._pick(self._mode))

    def _pick(self, mode):
        self._mode = mode
        self._timer.stop()
        self._apply()

    def _apply(self):
        self._on_change(self._mode, self.search.text())

    def set_count(self, text):
        self._count.setText(text)
        self._count.setVisible(bool(text))
