"""Dialog wrappers, error-safety decorators, and shared widget styling.

Route every new dialog through these wrappers instead of raw aqt.utils calls, so it carries
the "Intern Pearls" title and scrolls with its buttons outside the scroll area. A screen
that lists cards, decks or settings builds rows and goes through _ask_with_widget.

Also holds the guard for interactive flows that write the collection (`_manual_flow`,
`manual_sync_in_progress`), because sync.py and collection.py both need it and cannot share
one without a circular import."""
import functools
import html
import sys
import time
import traceback
from contextlib import contextmanager

from aqt import mw
from aqt.qt import (QApplication, QCheckBox, QDialog, QDialogButtonBox, QEventLoop, QFrame,
                    QLabel, QMessageBox, QProgressDialog, QPushButton, QScrollArea,
                    QSizePolicy, Qt, QVBoxLayout)
from aqt.utils import askUser, showInfo, showWarning, tooltip

from .config import APP_NAME
from .palette import colors
from .platform import new_work_request, platform, wait_for_mock_work


# Depth of interactive flows that write the collection. The auto-sync poll skips its tick
# while this is non-zero, since its apply can land while a flow sits in a modal dialog's
# event loop.
_manual_flow_depth = 0


def manual_sync_in_progress():
    """Whether an interactive flow that writes the collection is running. A depth counter
    keeps nested flows guarded until the outermost returns."""
    return _manual_flow_depth > 0


def _manual_flow(fn):
    """Hold `manual_sync_in_progress()` for the whole of an interactive flow. Applied under
    @_safe; the guard is released even when the flow raises."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        global _manual_flow_depth
        _manual_flow_depth += 1
        try:
            return fn(*args, **kwargs)
        finally:
            _manual_flow_depth -= 1
    return wrapper


def _info(text, **kw):
    kw.setdefault("title", APP_NAME)
    kw.setdefault("textFormat", "rich")
    return showInfo(text, **kw)


def _warn(text, **kw):
    kw.setdefault("title", APP_NAME)
    kw.setdefault("textFormat", "rich")
    return showWarning(text, **kw)


def _ask(text, yes_label=None, no_label=None, default_yes=False, **kw):
    """A yes/no confirmation. Pass both labels to name the two actions instead of Yes/No.

    The named-button form is a QMessageBox built here, not Anki's askUserDialog, which gives
    every button AcceptRole, so Escape and the close box do nothing and Return picks the
    first button. Here the declining answer has RejectRole and is the default and the escape
    button, so Return, Escape and the close box all decline. `default_yes=True` makes Return
    choose the affirming answer; Escape and closing still decline."""
    kw.setdefault("title", APP_NAME)
    if yes_label and no_label:
        box = QMessageBox(mw)
        box.setWindowTitle(kw["title"])
        box.setIcon(QMessageBox.Icon.Question)
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(text)
        yes = box.addButton(yes_label, QMessageBox.ButtonRole.AcceptRole)
        no = box.addButton(no_label, QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(yes if default_yes else no)
        box.setEscapeButton(no)
        box.exec()
        answered = box.clickedButton() is yes
        box.deleteLater()   # parented to mw, so closing alone leaves it behind
        return answered
    return askUser(text, **kw)


def _ask_scrollable(text, yes_label="Continue", no_label="Cancel", max_height=340,
                    extra_label=None, on_extra=None, checkbox=None, title=None,
                    open_external_links=False):
    """Like _ask, but for long prose: the body scrolls in a fixed-height viewport with the
    buttons pinned outside it. A screen that lists cards or decks uses _ask_with_widget
    instead.

    yes_label/no_label default to "Continue"/"Cancel"; pass labels that name the action.
    `no_label=None` drops the second button. `title` overrides the window title.

    `checkbox` is an optional {"label": ..., "checked": bool} dict, shown under the body and
    written back in place when the dialog closes. Use it only for a decision subordinate to
    this one.

    `extra_label` adds a third button with ActionRole: clicking it runs `on_extra` and
    leaves the dialog open. `on_extra` may return a string that replaces the body text, or a
    `(body_text, button_label)` pair; None leaves that part alone.

    `open_external_links` defaults to off because several callers interpolate unescaped
    collection content, and a link in it must not open the browser."""
    dlg = QDialog(mw)
    dlg.setWindowTitle(title or APP_NAME)
    dlg.setMinimumWidth(460)
    lay = QVBoxLayout(dlg)

    # Qt paints an <a href> anchor in its built-in link colour and ignores the palette's
    # Link role. Rich text does honour a <style> block in the document, so one is prepended
    # to every body.
    link_style = f"<style>a {{ color: {colors()['accent']}; }}</style>"

    def _rich(t):
        return link_style + t

    body = QLabel(_rich(text))
    body.setWordWrap(True)
    # setWidgetResizable(True) stretches this label to the viewport, and Qt centres label
    # text vertically by default.
    body.setAlignment(Qt.AlignmentFlag.AlignTop)
    body.setTextFormat(Qt.TextFormat.RichText)
    body.setOpenExternalLinks(open_external_links)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setMaximumHeight(max_height)
    # Preferred, not the default Expanding, so the scroll area sizes to its content and
    # leftover height goes to the addStretch() below.
    scroll.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
    scroll.setWidget(body)
    lay.addWidget(scroll)

    box = None
    if checkbox:
        box = QCheckBox(checkbox["label"])
        box.setChecked(bool(checkbox.get("checked")))
        lay.addWidget(box)

    # Collects leftover height between the content and the buttons.
    lay.addStretch()

    bb = QDialogButtonBox()
    yes = bb.addButton(yes_label, QDialogButtonBox.ButtonRole.AcceptRole)
    if no_label:
        bb.addButton(no_label, QDialogButtonBox.ButtonRole.RejectRole)
    if extra_label:
        extra = bb.addButton(extra_label, QDialogButtonBox.ButtonRole.ActionRole)

        def _run_extra():
            updated = on_extra(dlg) if on_extra else None
            new_text, new_label = ((updated if isinstance(updated, tuple)
                                    else (updated, None)))
            if new_text is not None:
                body.setText(_rich(new_text))
            if new_label is not None:
                extra.setText(new_label)
        extra.clicked.connect(_run_extra)
    yes.clicked.connect(dlg.accept)
    bb.rejected.connect(dlg.reject)
    lay.addWidget(bb)

    answered = bool(dlg.exec())
    if box is not None:
        checkbox["checked"] = box.isChecked()
    # The dialog is parented to mw, so it lives until Anki quits unless deleted. Not
    # WA_DeleteOnClose: close runs before exec() returns and would free the checkbox read
    # above.
    dlg.deleteLater()
    return answered


def _place_checkbox(dialog_layout, body, box):
    """Insert the checkbox directly above the body's scroll area, under the fixed text that
    explains it. A body with no scroll area gets it at the bottom of the dialog.

    The box lands under the last of the fixed text, so a caller must put the paragraph it
    answers last."""
    lay = body.layout() if hasattr(body, "layout") else None
    for i in range(lay.count() if lay is not None else 0):
        item = lay.itemAt(i)
        if isinstance(item.widget() if item else None, QScrollArea):
            lay.insertWidget(i, box)
            return
    dialog_layout.addWidget(box)


def _ask_with_widget(body, yes_label="Continue", no_label="Cancel", checkbox=None,
                     title=None, min_width=560, min_height=520, on_close=None,
                     open_size=None, extra=None, actions=(), parent=None):
    """Like _ask_scrollable, but the body is a caller-built widget that owns its layout and
    scrolling. This adds the title, the optional checkbox (written back in place), and the
    Continue/Cancel buttons.

    `open_size` is the (width, height) the dialog opens at, clamped to its screen. None
    opens at the minimums.

    `on_close` runs once the reader has answered, while the dialog is still alive. It is the
    caller's last chance to read its widgets: Qt destroys them when this returns. The
    mock-Anki suite cannot catch a mistake here, since its widgets have no C++ lifetime.

    `extra`, when given, is {"label", "tooltip", "visible"} for one more accepting button,
    added before the yes button. While the dialog is open its live button is in
    extra["button"]; clicking it sets extra["clicked"] and answers yes. The button entry is
    dropped on return. `actions` holds (label, callback) pairs for buttons that leave the
    dialog open."""
    dlg = QDialog(parent or mw)
    dlg.setWindowTitle(title or APP_NAME)
    dlg.setMinimumWidth(min_width)
    dlg.setMinimumHeight(min_height)
    if open_size:
        w, h = open_size
        # Best-effort clamp: the mock Qt the tests run on has no screens to ask, and a
        # failure to measure one should cost the clamp, never the resize.
        try:
            geo = QApplication.primaryScreen().availableGeometry()
            w, h = min(w, geo.width() - 60), min(h, geo.height() - 80)
        except Exception:
            pass
        dlg.resize(max(w, min_width), max(h, min_height))
    lay = QVBoxLayout(dlg)
    lay.addWidget(body, 1)

    box = None
    if checkbox:
        box = QCheckBox(checkbox["label"])
        box.setChecked(bool(checkbox.get("checked")))
        _place_checkbox(lay, body, box)

    bb = QDialogButtonBox()
    for label, callback in actions:
        action = bb.addButton(label, QDialogButtonBox.ButtonRole.ActionRole)
        action.clicked.connect(lambda checked=False, fn=callback: fn())
    if extra is not None:
        other = bb.addButton(extra["label"], QDialogButtonBox.ButtonRole.ActionRole)
        other.setToolTip(extra.get("tooltip", ""))
        other.setVisible(extra.get("visible", True))
        extra["button"] = other

        def _other_clicked(_checked=False):
            extra["clicked"] = True
            dlg.accept()
        other.clicked.connect(_other_clicked)
    yes = bb.addButton(yes_label, QDialogButtonBox.ButtonRole.AcceptRole)
    if no_label:
        bb.addButton(no_label, QDialogButtonBox.ButtonRole.RejectRole)
    yes.clicked.connect(dlg.accept)
    bb.rejected.connect(dlg.reject)
    lay.addWidget(bb)

    answered = bool(dlg.exec())
    if box is not None:
        checkbox["checked"] = box.isChecked()
    if on_close is not None:
        on_close()
    # After on_close, never before: this frees the widgets that callback reads.
    dlg.deleteLater()
    if extra is not None:
        extra.pop("button", None)
    return answered


def copy_to_clipboard(text):
    """Put `text` on the system clipboard, returning True if it landed. Never raises: a
    missing clipboard (mocked or headless Qt) returns False."""
    try:
        QApplication.clipboard().setText(text)
        return True
    except Exception:
        print(traceback.format_exc())
        return False


def _window_alive():
    """False once Anki's main window is gone or the application is quitting, when
    showing a dialog would only raise again."""
    try:
        closing = getattr(QApplication, "closingDown", None)
        if closing is not None and closing():
            return False
        if mw is not None:
            mw.isVisible()
    except RuntimeError:
        return False
    except AttributeError:
        pass
    return mw is not None


def _safe(fn):
    """Wrap a menu action so an exception shows a plain warning dialog instead of Anki's
    traceback box. The full traceback goes to stdout."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            print(traceback.format_exc())
            if not _window_alive():
                print(f"Intern Pearls: something went wrong while Anki was closing: {e}",
                      file=sys.stderr)
                return None
            _warn(f"Something went wrong: {html.escape(str(e), quote=False)}<br><br>"
                  "If a backup was taken before this ran, Advanced has tools to "
                  "revert to it: Restore intern pearls deck or Restore full collection.")
    return wrapper


def _bg_safe(fn):
    """Like `_safe`, but for calls that fire on their own (a startup check, a poll timer).
    Failures print to the console and show a transient tooltip, not a modal."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            print(traceback.format_exc())
            if not _window_alive():
                return None
            try:
                tooltip(f"Intern Pearls: background check failed ({html.escape(str(e), quote=False)})",
                       period=4000, parent=mw)
            except Exception:
                pass
    return wrapper


@contextmanager
def cancellable_progress(title, total, cancel_text="Cancel"):
    """A determinate, cancellable progress dialog for a loop of `total` steps. Uses a
    QProgressDialog because `mw.progress` shows no percentage and never checks for a cancel.

    Yields `step(i, label)`: call it right before the i-th (1-based) unit of work. It
    returns False if the user has clicked Cancel, and the caller must then stop before
    starting that unit.

    `step.pump()` repaints, processes pending clicks, and returns whether Cancel is still
    un-clicked. It takes an ignored positional argument so it can be passed as
    `net._http_get`'s `on_chunk`; a False return there aborts only the download in flight.

    Pumping runs queued events, which is safe only because this dialog is WindowModal on
    `mw` and blocks all other input."""
    dlg = QProgressDialog(title, cancel_text, 0, total, mw)
    dlg.setMinimumWidth(420)
    # Line 1 says where the run is, line 2 any download under way; the empty second
    # line keeps the window the same height either way.
    label = QLabel(f"{title}\n")
    label.setTextFormat(Qt.TextFormat.PlainText)
    dlg.setLabel(label)
    dlg.setWindowTitle(APP_NAME)
    dlg.setWindowModality(Qt.WindowModality.WindowModal)
    dlg.setMinimumDuration(0)
    dlg.setAutoClose(True)
    dlg.setValue(0)

    share = [0, 1]   # where the current step starts, and how many units it owns
    line = [title]

    def pump(_bytes_so_far=None):
        QApplication.processEvents()
        return not dlg.wasCanceled()

    def part(done, of, text=None):
        # Stopping one short of the step's share keeps the bar off the maximum, which
        # would reset the dialog and clear Cancel.
        if dlg.wasCanceled():
            return False    # setValue would show the cancelled window again
        if of:
            dlg.setValue(share[0] + (share[1] - 1) * min(done, of) // of)
        if text:
            # Not setLabelText: tests count those calls as steps.
            label.setText(f"{line[0]}\n{text}")
        return pump()

    def step(i, text, span=1):
        share[:] = [i - 1, max(1, span)]
        line[0] = text
        dlg.setLabelText(f"{text}\n")
        dlg.setValue(i - 1)
        return pump()

    pump.part = part
    step.pump = pump

    try:
        yield step
    finally:
        dlg.setValue(total)
        dlg.close()
        dlg.deleteLater()   # parented to mw, so closing alone leaves it behind


class ProgressCancelled(Exception):
    """The user cancelled a check before its result was used."""


def run_with_progress(label, fn):
    """Run file or network work off-thread while pumping a delayed, modal check.

    `fn` must not access Qt or the collection. Its result or exception returns on
    the main thread; Cancel abandons the work without waiting for it to finish.
    Platforms without blocking work support run inline without a progress window.
    """
    if not platform().supports_blocking_work:
        return fn()

    dlg = QProgressDialog(label, "Cancel", 0, 0, mw)
    text = QLabel(label)
    text.setTextFormat(Qt.TextFormat.PlainText)
    dlg.setLabel(text)
    dlg.setLabelText(label)
    dlg.setWindowTitle(APP_NAME)
    dlg.setWindowModality(Qt.WindowModality.WindowModal)
    dlg.setMinimumDuration(300)
    dlg.setAutoClose(False)
    dlg.setValue(0)
    outcome = []
    active = True
    handle = None

    def finished(result, error):
        if active:
            outcome.append((result, error))

    request = new_work_request(dlg, "background-fetch", "progress.fetch")
    try:
        handle = platform().start_work(
            request, lambda _context: fn(),
            lambda result: finished(result, None),
            lambda error: finished(None, error))
        handle.start()
        while True:
            if dlg.isVisible():
                QApplication.processEvents()
            else:
                QApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
            if not _window_alive() or dlg.wasCanceled():
                raise ProgressCancelled()
            if not handle.is_alive():
                wait_for_mock_work(handle)
            if outcome:
                result, error = outcome[0]
                if error is not None:
                    raise error
                return result
            time.sleep(0.005)
    finally:
        active = False
        if handle is not None:
            handle.cancel()
        try:
            dlg.close()
            dlg.deleteLater()
        except RuntimeError:    # the main window and its children were deleted
            pass


# ------------------------------------------------------------------ widget helpers
def title_label(text):
    """The large heading at the top of a dialog body."""
    lbl = QLabel(text)
    lbl.setStyleSheet("font-size: 17px; font-weight: 600;")
    return lbl


def section_label(text, top_margin=0):
    """A bold in-dialog section heading, e.g. "Deck sync" / "Preserved fields"."""
    lbl = QLabel(text)
    margin = f" margin-top: {top_margin}px;" if top_margin else ""
    lbl.setStyleSheet(f"font-weight: 600;{margin}")
    return lbl


def section_rule():
    """The hairline between two sections of a form.

    A real HLine, not a border-top on the next heading: Qt does not paint a lone border on a
    plain container, and a selector-less stylesheet propagates into its children. Uses
    `panel_rule`, which is stronger than the `row_rule` between rows of one list."""
    return _hairline("panel_rule")


def row_rule():
    """The faint hairline between two rows of one list."""
    return _hairline("row_rule")


def _hairline(role):
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFrameShadow(QFrame.Shadow.Plain)
    line.setFixedHeight(1)
    line.setStyleSheet(f"color: {colors()[role]};")
    return line


def muted_label(text):
    """Secondary explanatory text at the dialog's normal font size."""
    lbl = QLabel(text)
    lbl.setTextFormat(Qt.TextFormat.PlainText)
    lbl.setWordWrap(True)
    lbl.setStyleSheet(f"color: {colors()['muted']};")
    return lbl


def plain_tooltip(text):
    return "<p style='white-space:pre-wrap'>" + html.escape(str(text), quote=False) + "</p>"


def hint_label(text, top_margin=0, cls=QLabel):
    """Small-print fine detail under a control.

    `cls` builds the label out of a QLabel subclass instead of a plain one, so a
    caller that needs the wrapped text to defend its own height can supply one
    without restating this styling (see ai_setup._WrappedHint)."""
    lbl = cls(text)
    lbl.setWordWrap(True)
    margin = f" margin-top: {top_margin}px;" if top_margin else ""
    lbl.setStyleSheet(f"color: {colors()['muted']}; font-size: 11px;{margin}")
    return lbl


class _FocusButton(QPushButton):
    """An inset focus ring that leaves the button's sizing and borders alone."""

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.hasFocus() or not self.isEnabled():
            return
        from aqt.qt import QColor, QPainter, QPen, QRectF
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(colors()['accent']), 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(2, 2, -2, -2), 3, 3)
        painter.end()


def link_button(label, on_click=None, tooltip_text=None, align_left=False):
    """A flat, accent-colored button that reads as a link rather than a push button."""
    btn = _FocusButton(label)
    btn.setFlat(True)
    btn.setAutoDefault(False)
    btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    c = colors()
    align = " text-align: left;" if align_left else ""
    btn.setStyleSheet(
        f"QPushButton {{ color: {c['accent']}; font-size: 12px;{align} }}"
        f"QPushButton:disabled {{ color: {c['muted']}; }}")
    if tooltip_text:
        btn.setToolTip(tooltip_text)
    if on_click:
        btn.clicked.connect(on_click)
    return btn
