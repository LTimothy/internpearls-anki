"""Intern Pearls: AI Backends. One compact row per assistant, then one settings
panel for the preferred one: where the three assistants are chosen, ignored,
pointed at, tested, and given their default model and effort. The wizard only
shows a one-line summary and an AI Backends link that opens this."""
import threading

from aqt import mw
from aqt.qt import (QApplication, QComboBox, QDesktopServices, QDialog,
                    QDialogButtonBox, QFileDialog, QGridLayout, QHBoxLayout,
                    QLabel, QLayout, QLineEdit, QPushButton, Qt, QTimer, QUrl,
                    QVBoxLayout, QWidget, pyqtSignal)

from . import ai_cli
from .config import ADDON_PACKAGE, APP_NAME, _cfg
from .palette import colors
from .ui import (_safe, hint_label, link_button, section_label, section_rule,
                 title_label)
from .widgets import CARET_GAP, align_field_column, chip_cell, field_slot

_POLL_MS = 200

# The width of a settings grid's label column, so every field in one starts at the
# same x after it. Module level rather than only on _SettingsPanel: the wizard's own
# Advanced panel lays out against the same column so the two screens read as one
# form (ai_dialog._build_input).
LABEL_W = 108

# The chips a backend row can wear, and the one the preferred row wears instead
# of a Use link. Two sets rather than one because they are two columns: the
# leading state chip is measured against the four words it can actually say, and
# widening that gutter to fit PREFERRED (which never appears in it) would indent
# every row's text for nothing. See widgets.chip_column_width.
_STATE_CHIPS = ("found", "notfound", "notresponding", "ignored")
_PREFERRED_CHIPS = ("preferred",)

# A second name a reader may know a backend by, shown muted beside its executable.
# Not in ai_cli.BACKENDS: nothing outside this window has any use for it. Kept
# short on purpose: this sits on the unwrapped label line, so anything longer
# than a bare alias pushes the whole dialog's minimum width out with it. The
# fuller shutdown note lives in _SHUTDOWN_NOTE instead, on the wrapped detail
# line, where a long sentence costs height, not width.
_ALSO_KNOWN = {"agy": "formerly Gemini CLI"}

# Folded into the wrapped muted detail line, after the safety sentence, rather
# than the unwrapped label line above: see _ALSO_KNOWN's comment for why.
_SHUTDOWN_NOTE = {"agy": "Gemini CLI stopped serving personal Google AI Pro "
                        "and Ultra accounts on 2026-06-18."}


def _write(key, value):
    conf = mw.addonManager.getConfig(ADDON_PACKAGE) or {}
    conf[key] = value
    mw.addonManager.writeConfig(ADDON_PACKAGE, conf)


def _write_map(key, kind, value):
    conf = mw.addonManager.getConfig(ADDON_PACKAGE) or {}
    m = conf.get(key)
    if not isinstance(m, dict):
        # A legacy flat-string config value (e.g. an un-migrated ai_cli_path)
        # reads back through _cfg() already migrated into the preferred
        # backend's entry. Seed from that migrated map instead of {} so the
        # first per-backend save doesn't silently drop it.
        m = dict(_cfg()[key])
    m[kind] = value
    conf[key] = m
    mw.addonManager.writeConfig(ADDON_PACKAGE, conf)


def _clear(layout):
    """Empty a layout, the way dialogs._DeclinedDialog._rebuild does: takeAt only
    detaches a widget from the layout, not from the dialog's widget tree, and
    deleteLater's removal is deferred past this call, so a taken-out row still
    paints until it is hidden explicitly."""
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.setVisible(False)
            w.deleteLater()


def _capability_pill(text, role):
    """A capability stated on a backend's own row: what it can do with an
    attachment, and what its account costs. A small pill rather than more muted
    prose, so the one line that differs between two assistants is the line that
    stands out. Same shape as widgets.chip_cell's pill, but sized to its own
    words instead of a shared column: these sit inline in a sentence, where a
    fixed-width gutter would only pad them apart."""
    c = colors()
    pill = QLabel(text)
    pill.setStyleSheet(f"background-color: {c[role + '_bg']}; color: {c[role + '_fg']};"
                       " border-radius: 3px; padding: 1px 6px; font-size: 11px;"
                       " font-weight: 600;")
    return pill


class _WrappedHint(QLabel):
    """A hint label that will not let a layout squeeze its wrapped text.

    A word-wrapped QLabel reports a minimum height of one line, whatever it
    actually wraps to, and a QVBoxLayout builds a window's minimum height out of
    exactly those minimums: height for width never reaches it. So this window,
    which opens at a size chosen before any label has a real width, could open
    shorter than its text needs, and every wrapped paragraph and backend row was
    then squeezed below its own minimum until "Works with a ... ." was cut off
    mid-glyph at the row's bottom edge.

    v0.56.1 tried to fix this from resizeEvent alone: once the label has a
    width, it claims the height that width really requires as its own minimum,
    which every layout above it does honour, but only on the next layout pass.
    22589b4 then tried to force that next pass synchronously, from right here,
    by reading the top-level window's minimumSizeHint() and resizing it inline.
    That read is itself unreliable: the intermediate box layouts between this
    label and the window keep cached geometry until Qt runs its own deferred
    layout pass, so the value read here can be stale, and the resize this
    branch computes from it can equal the window's current size even though
    the window has not actually settled yet, in which case the branch's own
    guard skips the resize and nothing happens. So this label still only ever
    claims its own minimum; making the window honour that minimum on first
    paint is `_settle_min_size`'s job now (see its docstring), not this
    label's.
    """

    def resizeEvent(self, event):
        super().resizeEvent(event)
        need = self.heightForWidth(self.width()) if self.width() > 0 else 0
        if need > 0 and self.minimumHeight() != need:
            self.setMinimumHeight(need)


def _wrapped_hint(text):
    """hint_label, built so its wrapped height is a minimum rather than a wish.
    Every multi-line hint in this window goes through here: one paragraph left on
    a plain hint_label puts the window's minimum height back below what it needs
    and the squeeze comes back for everything under it."""
    return hint_label(text, cls=_WrappedHint)


def _settle_min_size(dialog):
    """Make sure `dialog` is at least as big as its layout's true minimum,
    right now. Called from a dialog's showEvent, and once more from a
    QTimer.singleShot(0, ...) right after that.

    The layout's own SetMinimumSize constraint (set on the dialog's top-level
    layout in its __init__) already keeps Qt's internal notion of the window's
    minimum size in sync with its content, and QWidget.resize() already clamps
    to that minimum on its own, so in the ordinary case this call is a no-op.
    It exists for the first paint specifically: a wrapped _WrappedHint claims
    its real minimum height only once it has a real width, and it can only get
    a real width from a layout pass, which on some platforms does not finish
    settling before that very first frame is painted. Reading and reapplying
    minimumSizeHint() here, once after showEvent's own layout().activate()
    call and once more on the next event loop turn, catches the window up to
    whatever the layout had actually settled on by each of those two later
    points, so a first frame that opened too small never survives to be the
    one the reader sees."""
    dialog.layout().activate()
    hint = dialog.minimumSizeHint()
    w = max(dialog.width(), hint.width())
    h = max(dialog.height(), hint.height())
    if w != dialog.width() or h != dialog.height():
        dialog.resize(w, h)


def _safe_settle(dialog):
    """_settle_min_size, for the QTimer.singleShot(0, ...) call: the dialog can
    have been closed (and its C++ object deleted) in the moment between
    showEvent scheduling this and the event loop actually running it, which
    _settle_min_size itself has no reason to guard against."""
    try:
        _settle_min_size(dialog)
    except RuntimeError:
        pass


class ModelEffortControls(QWidget):
    """Model and Effort for one backend, with the honesty rules from
    ai_cli.BACKENDS: a closed alias list plus Custom where aliases exist, free
    text where the CLI takes any name and there is no short alias list worth
    closing over (codex, agy), and Effort only where a flag is verified.

    Builds the controls and owns their state; it does not lay them out. `rows()`
    hands the settings panel a (label, field) pair per row so both rows go into
    that panel's own grid and Model's field starts at the same x as Effort's.
    This widget is therefore never shown itself: it is the controls' owner, not
    their container.
    """
    changed = pyqtSignal()

    def __init__(self, kind, model, effort, parent=None):
        super().__init__(parent)
        self.kind = kind
        meta = ai_cli.BACKENDS[kind]
        self.combo = QComboBox()
        self.custom = QLineEdit()
        self.custom.setPlaceholderText(meta["model_hint"])
        # A closed combo only for a backend with a real short alias list
        # (claude); agy and codex now also carry a default_model (agy's own,
        # codex's read from its config.toml), but that alone must not turn
        # free text into a single-item combo.
        aliases = ([a for a in ([meta["default_model"]] + list(meta["model_aliases"]))
                   if a] if meta.get("model_aliases") else [])
        self._aliases = list(dict.fromkeys(aliases))
        self.model_field = QWidget()
        model_field_lay = QVBoxLayout(self.model_field)
        model_field_lay.setContentsMargins(0, 0, 0, 0)
        model_field_lay.setSpacing(2)
        model_field_lay.addWidget(self.combo)
        model_field_lay.addWidget(self.custom)
        if self._aliases:
            self.combo.addItems(self._aliases + ["Custom"])
            if model in self._aliases or not model:
                self.combo.setCurrentText(model or meta["default_model"])
                self.custom.hide()
            else:
                self.combo.setCurrentText("Custom")
                self.custom.setText(model)
        else:
            self.combo.hide()
            self.custom.setText(model)
        self.effort = QComboBox()
        levels = meta.get("effort_levels") or []
        # Read from the metadata rather than from the combo's own visibility:
        # the effort combo lives in the panel's grid, not inside this widget, so
        # "is it visible to me" is no longer a question this widget can answer.
        self.has_effort = bool(levels)
        if self.has_effort:
            # A backend with no default_effort of its own (agy) sends no flag
            # at all when this is left alone, so the label must not name one.
            self.effort.addItem(f"Default ({meta['default_effort']})"
                                if meta["default_effort"] else "Default", "")
            for lv in levels:
                self.effort.addItem(lv, lv)
            idx = self.effort.findData(effort if effort in levels else "")
            self.effort.setCurrentIndex(max(idx, 0))
        else:
            self.effort.hide()
        self.combo.currentTextChanged.connect(self._on_combo)
        self.custom.textEdited.connect(lambda _t: self.changed.emit())
        self.effort.currentIndexChanged.connect(lambda _i: self.changed.emit())

    def rows(self):
        """The (label, field) rows the settings panel lays into its grid, in order.
        Effort is absent entirely where no effort flag is verified, rather than
        present and disabled: an inert control still claims the choice exists."""
        rows = [("Model", self.model_field)]
        if self.has_effort:
            rows.append(("Effort", self.effort))
        return rows

    def _on_combo(self, text):
        if self._aliases:
            self.custom.setVisible(text == "Custom")
        self.changed.emit()

    def values(self):
        if self._aliases:
            model = self.custom.text().strip() if self.combo.currentText() == "Custom" \
                else self.combo.currentText()
        else:
            model = self.custom.text().strip()
        effort = self.effort.currentData() if self.has_effort else ""
        return model, effort or ""


class _BackendRow(QWidget):
    """One assistant, on one row: what detection found, what it is called and
    what it can do, what account it needs, and the two decisions this window
    offers about it (use it, or set it aside)."""

    def __init__(self, kind, info, preferred, dlg, cfg):
        super().__init__()
        meta = ai_cli.BACKENDS[kind]
        self.kind = kind
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 6, CARET_GAP, 6)
        lay.setSpacing(CARET_GAP)
        lay.addWidget(chip_cell(_state_chip(info), _STATE_CHIPS), 0,
                      Qt.AlignmentFlag.AlignTop)

        body = QWidget()
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.setSpacing(2)
        exe = meta["exe"]
        also = _ALSO_KNOWN.get(kind)
        exe_text = f"{exe}, {also}" if also else exe
        self.title = QLabel(f"<b>{meta['label']}</b> "
                            f"<span style='color:{colors()['muted']}'>({exe_text})</span>")
        self.title.setTextFormat(Qt.TextFormat.RichText)
        body_lay.addWidget(self.title)

        caps = QWidget()
        caps_lay = QHBoxLayout(caps)
        caps_lay.setContentsMargins(0, 0, 0, 0)
        caps_lay.setSpacing(CARET_GAP)
        if ai_cli.image_capable(kind):
            caps_lay.addWidget(_capability_pill("image attach: supported", "accept"))
        free_tier = meta["free_tier"]
        if free_tier == "throttled":
            caps_lay.addWidget(_capability_pill("free tier, throttled", "new"))
        elif free_tier == "capped":
            caps_lay.addWidget(_capability_pill("free tier available", "new"))
        caps_lay.addStretch()
        body_lay.addWidget(caps)

        self.model_line = _wrapped_hint(ai_cli.model_effort_line(
            kind, cfg["ai_model"][kind], cfg["ai_effort"][kind],
            path=info["path"]))
        body_lay.addWidget(self.model_line)

        detail_text = f"Works with a {meta['subscription']}. {meta['safety']}."
        shutdown_note = _SHUTDOWN_NOTE.get(kind)
        if shutdown_note:
            detail_text += f" {shutdown_note}"
        self.detail = _wrapped_hint(detail_text)
        body_lay.addWidget(self.detail)
        lay.addWidget(body, 1)

        trailing = QWidget()
        trail_lay = QHBoxLayout(trailing)
        trail_lay.setContentsMargins(0, 0, 0, 0)
        trail_lay.setSpacing(CARET_GAP)
        self.guide_link = link_button(
            "install guide",
            on_click=lambda: dlg._guard(_open_url, meta["install_url"]))
        trail_lay.addWidget(self.guide_link)
        self.use_link = None
        if preferred == kind:
            trail_lay.addWidget(chip_cell("preferred", _PREFERRED_CHIPS))
        elif info["enabled"]:
            self.use_link = link_button(
                f"Use {meta['label']}",
                on_click=lambda: dlg._guard(dlg.use_backend, kind))
            trail_lay.addWidget(self.use_link)
        self.ignore_link = link_button(
            "use again" if not info["enabled"] else "ignore",
            on_click=lambda: dlg._guard(dlg.toggle_ignored, kind))
        trail_lay.addWidget(self.ignore_link)
        lay.addWidget(trailing, 0, Qt.AlignmentFlag.AlignTop)

    def text(self):
        """Everything this row says, as one string: what a test reads instead of
        walking three labels that are one sentence between them."""
        return " ".join([self.title.text(), self.model_line.text(), self.detail.text()])


def _state_chip(info):
    """Which of the four states detection put this backend in. The same three
    the README names, plus the one the reader chose: a backend set aside is not
    a detection result and must not read as one."""
    if not info["enabled"]:
        return "ignored"
    if info["ok"]:
        return "found"
    if info["path"]:
        return "notresponding"
    return "notfound"


def _open_url(url):
    if not url.lower().startswith(("http://", "https://")):
        return
    QDesktopServices.openUrl(QUrl(url))


class _SettingsPanel(QWidget):
    """Executable path, Model, Effort and Test connection for one backend.

    A QGridLayout with a fixed-width label column, never a QFormLayout: on macOS
    a form centres its rows, which floated Model and Effort in the middle of the
    window and clipped the Model combo. Every label starts at the panel's left
    edge and every field at the same x after it.
    """
    def __init__(self, kind, cfg, dlg):
        super().__init__()
        self.kind = kind
        self.dlg = dlg
        meta = ai_cli.BACKENDS[kind]
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)
        outer.addWidget(section_label(f"{meta['label']} settings"))
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(CARET_GAP)
        grid.setVerticalSpacing(6)
        grid.setColumnMinimumWidth(0, LABEL_W)
        grid.setColumnStretch(1, 1)

        self.path = QLineEdit(cfg["ai_cli_path"][kind])
        self.path.setPlaceholderText("leave blank to auto-detect")
        browse = QPushButton("Browse")
        path_box = QWidget()
        path_lay = QHBoxLayout(path_box)
        path_lay.setContentsMargins(0, 0, 0, 0)
        path_lay.setSpacing(CARET_GAP)
        path_lay.addWidget(self.path, 1)
        path_lay.addWidget(browse)
        grid.addWidget(self._label("Executable path"), 0, 0)
        grid.addWidget(path_box, 0, 1)

        self.model = ModelEffortControls(kind, cfg["ai_model"][kind],
                                         cfg["ai_effort"][kind], self)
        self._effort_slot = None
        row = 1
        for text, field in self.model.rows():
            if text == "Effort":
                field = self._effort_slot = field_slot(field)
            grid.addWidget(self._label(text), row, 0)
            grid.addWidget(field, row, 1)
            row += 1

        self.test_btn = QPushButton("Test connection")
        self.test_status = _wrapped_hint("Not tested yet")
        self._path_box = path_box
        self._test_box = test_box = QWidget()
        test_lay = QHBoxLayout(test_box)
        test_lay.setContentsMargins(0, 0, 0, 0)
        test_lay.setSpacing(CARET_GAP)
        test_lay.addWidget(self.test_btn)
        test_lay.addWidget(self.test_status, 1)
        grid.addWidget(test_box, row, 1)
        outer.addLayout(grid)
        self._realign()

        self.path.editingFinished.connect(lambda: dlg._guard(self._commit_path))
        browse.clicked.connect(lambda: dlg._guard(self._browse))
        self.model.changed.connect(lambda: dlg._guard(self._commit_model))
        self.model.changed.connect(lambda: dlg._guard(self._realign))
        self.test_btn.clicked.connect(lambda: dlg._guard(dlg._test, kind))

    def showEvent(self, event):
        """A panel rebuilt by recheck() after the dialog is already on screen
        (a Re-check click, a preferred-backend switch) never gets the dialog's
        own showEvent again, only its own first one - and _realign() run back
        in __init__ measured this panel's fields before they had ever actually
        been laid out on screen, which under macOS's native style is not the
        same geometry they settle at once shown. Same two-pass settle as
        _settle_min_size's own docstring: once here, once more on the next
        event loop turn, so a first frame that compensated against provisional
        geometry never survives to be the one the reader sees."""
        super().showEvent(event)
        self._realign()
        QTimer.singleShot(0, self._safe_realign)

    def _safe_realign(self):
        try:
            self._realign()
        except RuntimeError:      # the panel can already be gone by then
            pass

    def _realign(self):
        """Line every field's drawn left edge up on one x (see
        widgets.align_field_column): reapplied on every Model/Effort change, not
        just once at open, because a Model field's leading control itself changes
        (its combo vs. its free-text line edit, see ModelEffortControls._on_combo)
        and the two draw at different insets under macOS's native style."""
        rows = [(self._path_box, self.path), (self._test_box, self.test_btn),
                (self.model.model_field,
                 self.model.combo if self.model.combo.isVisible() else self.model.custom)]
        if self._effort_slot is not None:
            rows.append((self._effort_slot, self.model.effort))
        align_field_column(rows)

    def _label(self, text):
        lbl = QLabel(text)
        lbl.setFixedWidth(LABEL_W)
        lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        return lbl

    def apply(self, info):
        self.test_btn.setEnabled(bool(info["path"]))

    def _commit_path(self):
        _write_map("ai_cli_path", self.kind, self.path.text().strip())
        self.dlg.recheck()

    def _browse(self):
        p, _ = QFileDialog.getOpenFileName(self, f"Locate {ai_cli.BACKENDS[self.kind]['exe']}")
        if p:
            self.path.setText(p)
            self._commit_path()

    def _commit_model(self):
        model, effort = self.model.values()
        _write_map("ai_model", self.kind, model)
        _write_map("ai_effort", self.kind, effort)


def run_connection_test_async(owner, kind, path, on_status, on_done=None,
                             is_live=None, guard=None):
    """Run ai_cli.test_connection off the UI thread and report the one readable
    line it comes back with.

    Shared by this window and the wizard, which ran the same daemon-thread-plus-
    QTimer twice: a blocking call on the UI thread freezes Anki, and a thread or
    timer held only in a local would be collected out from under the live poll, so
    both are parked on `owner` for the run.

    `on_status(text)` is called once, from the poll and only from the poll, with the
    final line: never the CLI's own stderr, which ai_cli.test_connection has already
    turned into one short sentence. `on_done()` runs first and always, so a caller's
    own bookkeeping is cleaned up even when the result has nowhere left to go.
    `is_live()` is that case: the AI Backends window rebuilds its settings panel when
    the preferred backend changes, so a result arriving after the rebuild is consumed
    and dropped rather than written into a panel about a different assistant.
    `guard` wraps the poll the way a dialog wraps its own callbacks.
    """
    box = {}

    def worker():
        try:
            box["r"] = ai_cli.test_connection(kind, path)
        except Exception as e:
            box["e"] = e
    t = threading.Thread(target=worker, daemon=True)
    timer = QTimer(owner)
    refs = getattr(owner, "_conn_test_refs", None)
    if refs is None:
        refs = owner._conn_test_refs = []
    refs.append((t, timer))

    def poll():
        if t.is_alive():
            return
        timer.stop()
        if on_done:
            on_done()
        if is_live is not None and not is_live():
            return
        if "e" in box:
            on_status(f"Test failed: {box['e']}")
        else:
            r = box["r"]
            on_status(("Working: " if r["state"] == "working" else "Not working: ")
                      + r["detail"])
    timer.timeout.connect((lambda: guard(poll)) if guard else poll)
    t.start()
    timer.start(_POLL_MS)


class _AIBackendsDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME}: AI Backends")
        self._testing = set()
        self.rows = {}
        self.panel = None
        self.preferred = None
        lay = QVBoxLayout(self)
        # SetMinimumSize keeps this window's real minimum size in sync with its
        # content on every layout pass, not just the first: see
        # _settle_min_size's docstring for why the first paint still needs its
        # own showEvent settle on top of this.
        lay.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        lay.addWidget(title_label("AI Backends"))
        lay.addWidget(_wrapped_hint(
            "Card generation runs through an assistant you sign into yourself. This "
            "add-on never sees or stores your credentials: there is no API key field "
            "here, or anywhere else in the add-on."))

        rows_container = QWidget()
        self._rows_lay = QVBoxLayout(rows_container)
        self._rows_lay.setContentsMargins(0, 0, 0, 0)
        self._rows_lay.setSpacing(0)
        lay.addWidget(rows_container)
        lay.addWidget(_wrapped_hint(
            "After installing, run the tool once in a terminal and sign in there, "
            "then Re-check."))
        lay.addWidget(section_rule())

        panel_container = QWidget()
        self._panel_lay = QVBoxLayout(panel_container)
        self._panel_lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(panel_container)

        brow = QHBoxLayout()
        self.recheck_btn = link_button("Re-check", on_click=lambda: self._guard(self.recheck))
        self.overall = _wrapped_hint("")
        brow.addWidget(self.recheck_btn)
        brow.addWidget(self.overall, 1)
        lay.addLayout(brow)
        lay.addWidget(_wrapped_hint(
            "Test connection runs a real, trivial prompt through a detected CLI to "
            "confirm it can generate, not just that the binary runs. It costs one "
            "model turn, so it only runs when you click it."))
        bb = QDialogButtonBox()
        bb.addButton("Close", QDialogButtonBox.ButtonRole.RejectRole)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

        self.recheck()

        # Opened at a size that gives the rows their full width without outgrowing
        # the screen it lands on, clamped the same way _GenerateDialog's own
        # open_size does (ai_dialog.py): a fixed target, shrunk to fit whatever
        # screen is actually available. No scroll area: the rows are one line of
        # decisions each, so the whole window fits a laptop screen unfolded.
        # This clamp can ask for a height below the layout's real minimum on a
        # small screen; the SetMinimumSize constraint above wins that conflict,
        # so the window opens at whatever the layout actually needs instead of
        # the clamp's number, which is the right trade on a screen that small.
        open_w, open_h = 720, 620
        try:
            geo = QApplication.primaryScreen().availableGeometry()
            open_w = min(open_w, geo.width() - 60)
            open_h = min(open_h, geo.height() - 80)
        except Exception:
            pass
        self.resize(max(open_w, 420), max(open_h, 300))

    def showEvent(self, event):
        super().showEvent(event)
        _settle_min_size(self)
        QTimer.singleShot(0, lambda: _safe_settle(self))

    # --- state -----------------------------------------------------------
    def _guard(self, fn, *args):
        try:
            fn(*args)
        except Exception as e:      # never let a callback take Anki down
            self.overall.setText(f"Error: {e}")

    def _preferred_kind(self, cfg, res):
        """Which backend the settings panel belongs to: the one the reader chose,
        as long as it is still enabled, else whichever detection would actually
        use, else the first enabled backend, so the panel is never empty and
        never points somewhere the wizard would not go.

        Ignoring the currently preferred backend does not touch ai_backend in
        config (ignoring is not choosing a new preference), so a stored
        preference that is now disabled must not still be shown as preferred
        here: that would leave the panel and the PREFERRED chip on a backend
        the wizard will not actually use. A stored preference that is merely
        not found (but still enabled) is unaffected by this and stays
        preferred, since that is exactly the backend a reader would set a
        path for."""
        chosen = cfg.get("ai_backend", "")
        if chosen in ai_cli.BACKENDS and res["backends"][chosen]["enabled"]:
            return chosen
        if res["chosen"]:
            return res["chosen"]
        for kind in ai_cli.BACKENDS:
            if res["backends"][kind]["enabled"]:
                return kind
        return next(iter(ai_cli.BACKENDS))

    def _set_overall(self, res):
        ch = res["chosen"]
        self.overall.setText(f"Ready: {ai_cli.BACKENDS[ch]['label']} will be used." if ch
                             else "No usable assistant detected yet.")

    def recheck(self):
        cfg = _cfg()
        res = ai_cli.detect_backends(cfg)
        preferred = self._preferred_kind(cfg, res)
        _clear(self._rows_lay)
        self.rows = {}
        for i, kind in enumerate(ai_cli.BACKENDS):
            if i:
                self._rows_lay.addWidget(section_rule())
            row = _BackendRow(kind, res["backends"][kind], preferred, self, cfg)
            self.rows[kind] = row
            self._rows_lay.addWidget(row)
        if preferred != self.preferred:
            # Rebuilt only when the panel would be about a different backend: a
            # Re-check (or a path commit) while a Test connection run is in flight
            # must not replace the very widgets that run is writing its result to.
            self.preferred = preferred
            _clear(self._panel_lay)
            self.panel = _SettingsPanel(preferred, cfg, self)
            self._panel_lay.addWidget(self.panel)
        if self.preferred not in self._testing:
            self.panel.apply(res["backends"][self.preferred])
        self._set_overall(res)

    def use_backend(self, kind):
        _write("ai_backend", kind)
        self.recheck()

    def toggle_ignored(self, kind):
        enabled = _cfg()["ai_backend_enabled"][kind]
        _write_map("ai_backend_enabled", kind, not enabled)
        self.recheck()

    # --- test connection ---------------------------------------------------
    def _test(self, kind):
        path = ai_cli.detect_backends(_cfg())["backends"][kind]["path"]
        if not path or kind in self._testing:
            return
        self._testing.add(kind)
        self.panel.test_btn.setEnabled(False)
        self.panel.test_status.setText("Testing connection")

        def live():
            # A preference switch mid-test rebuilds the panel (use_backend ->
            # recheck), so the panel this run started against may no longer be
            # the one on screen, and may be about a different assistant. Looked
            # up by kind rather than captured, so a stale result is dropped
            # instead of written into a panel it is not about.
            return self.panel is not None and self.panel.kind == kind

        def done():
            # Runs whichever panel is live, so a later Test connection on this
            # backend is never left thinking one is still running.
            self._testing.discard(kind)

        def status(text):
            self.panel.test_btn.setEnabled(True)
            self.panel.test_status.setText(text)
        run_connection_test_async(self, kind, path, status, on_done=done,
                                  is_live=live, guard=self._guard)


@_safe
def open_ai_backends(parent=None):
    dlg = _AIBackendsDialog(parent or mw)
    dlg.exec()
