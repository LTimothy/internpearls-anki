"""Dialog wrappers, error-safety decorators, and shared widget styling.

Thin wrappers so every dialog carries the "Intern Pearls" title (Anki's helpers
default to the generic "Anki") and list-style messages get real HTML formatting
instead of hand-indented text. Route any new dialog through these, not the raw
aqt.utils calls, so a future addition here stays consistent automatically.

The label/button helpers at the bottom exist for the same reason: every dialog's
headings, hints, and link-style buttons share one look defined here, instead of each
dialog carrying its own copy of the stylesheet strings.
"""
import functools
import traceback
from contextlib import contextmanager

from aqt import mw
from aqt.qt import (QApplication, QCheckBox, QDialog, QDialogButtonBox, QFrame,
                    QLabel, QProgressDialog, QPushButton, QScrollArea, QSizePolicy, Qt,
                    QVBoxLayout)
from aqt.utils import askUser, getText, showInfo, showWarning, tooltip

from .config import APP_NAME
from .palette import colors


def _info(text, **kw):
    kw.setdefault("title", APP_NAME)
    kw.setdefault("textFormat", "rich")
    return showInfo(text, **kw)


def _warn(text, **kw):
    kw.setdefault("title", APP_NAME)
    kw.setdefault("textFormat", "rich")
    return showWarning(text, **kw)


def _ask(text, **kw):
    kw.setdefault("title", APP_NAME)
    return askUser(text, **kw)


def _ask_scrollable(text, yes_label="Continue", no_label="Cancel", max_height=340,
                    extra_label=None, on_extra=None, checkbox=None, title=None,
                    open_external_links=False):
    """Like _ask, but for content whose length isn't bounded by anything short: a
    bullet list of cards or decks that can grow into the dozens. A plain QMessageBox
    (what askUser/_ask use) has no scroll area, so long text just makes the box taller,
    and once it's taller than the screen its Yes/No buttons end up off-screen with no
    way to reach them, an unusable and undismissable dialog. This scrolls the body in a
    fixed-height viewport instead, with the buttons pinned outside it, so they're
    always reachable no matter how long the content is.

    yes_label/no_label default to action-neutral "Continue"/"Cancel" rather than
    "Yes"/"No": a caller with a specific action (e.g. "Archive & relocate") should
    pass its own labels, since a generic Yes/No forces the reader back up to the
    question to know what they're agreeing to.

    `checkbox` is an optional {"label": ..., "checked": bool} dict, shown under the
    body and written back in place when the dialog closes. It exists so a second
    decision that used to interrupt the run with its own question can be made here,
    while the reader is already deciding, instead of mid-flight three dialogs later.
    Only for a decision that is genuinely subordinate to this one: a checkbox on a
    confirmation is answered by the same click, so anything needing its own yes or no
    still needs its own dialog.

    `extra_label` adds a third button that does NOT answer the question. It carries
    ActionRole, so clicking it leaves this dialog open, runs `on_extra`, and returns
    the reader to the same undecided confirmation, which is the point: going to look
    at something in more detail shouldn't cost you the decision you were making. If
    `on_extra` returns a string, it replaces the body text, so the confirmation can
    reflect whatever happened while it was open; returning None leaves the body alone.

    `no_label=None` drops the second button entirely, for a caller with nothing to
    decline, just long or richly formatted content to show in a scrollable, consistently
    styled dialog with a single acknowledgement button (About uses this).

    `title` overrides the window title beyond the plain APP_NAME every other caller
    here is fine with, for a caller (About) whose title carries a suffix the way the
    add-on's other standalone dialogs (Settings, Manage decks) do.

    `open_external_links` defaults to off. This is the shared confirmation wrapper, and
    several callers interpolate content that is not escaped: a card front, a
    retired-card identity, a raw note field straight out of the collection. Before
    Qt's anchor color was fixed, a link in that content rendered as inert text; leaving
    external links on for every caller would make it clickable, launching the system
    browser on whatever URL that unescaped content happened to contain. About is the
    only caller with a link it actually wants opened (its own repository anchor), so it
    passes `True` explicitly instead of this being the default for everyone.
    """
    dlg = QDialog(mw)
    dlg.setWindowTitle(title or APP_NAME)
    dlg.setMinimumWidth(460)
    lay = QVBoxLayout(dlg)

    # Qt paints an <a href> anchor in its own built-in link colour, never in any colour
    # set on the widget (its palette's Link role included: verified that setting it has
    # no effect on what a QLabel's rich text actually paints), so an anchor in body text
    # would otherwise stay the same shade on both themes and go unreadable on a dark
    # window. A <style> block in the document itself is what Qt's rich text honours, so
    # it's prepended here rather than left to each caller: every body this wrapper ever
    # renders, About's link today or another dialog's tomorrow, gets it for free.
    link_style = f"<style>a {{ color: {colors()['accent']}; }}</style>"

    def _rich(t):
        return link_style + t

    body = QLabel(_rich(text))
    body.setWordWrap(True)
    # setWidgetResizable(True) below stretches this label's box to fill the whole
    # scroll viewport, and Qt vertically centres a label's text within its own box by
    # default, so a short body would otherwise float with blank space above it.
    body.setAlignment(Qt.AlignmentFlag.AlignTop)
    body.setTextFormat(Qt.TextFormat.RichText)
    body.setOpenExternalLinks(open_external_links)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setMaximumHeight(max_height)
    # Preferred rather than the QScrollArea default of Expanding: Expanding claims any
    # leftover height the dialog has beyond every widget's own natural size, which is
    # exactly what stretched this scroll area to its full max_height cap even for a
    # one-line body, leaving nothing to tell Qt where the *rest* of the leftover space
    # (beyond that cap) should go, so it spread thinly above, inside, and below the
    # scroll area instead. Preferred lets it size to its content and hands leftover
    # space to the addStretch() below instead.
    scroll.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
    scroll.setWidget(body)
    lay.addWidget(scroll)

    box = None
    if checkbox:
        box = QCheckBox(checkbox["label"])
        box.setChecked(bool(checkbox.get("checked")))
        lay.addWidget(box)

    # Collects all leftover height here, between the content and the buttons, instead
    # of Qt spreading it across every gap (see scroll's size policy above): a short
    # confirmation now sits at the top of the dialog with its buttons pinned to the
    # bottom, and a long one still scrolls within max_height with the buttons reachable.
    lay.addStretch()

    bb = QDialogButtonBox()
    yes = bb.addButton(yes_label, QDialogButtonBox.ButtonRole.AcceptRole)
    if no_label:
        bb.addButton(no_label, QDialogButtonBox.ButtonRole.RejectRole)
    if extra_label:
        extra = bb.addButton(extra_label, QDialogButtonBox.ButtonRole.ActionRole)

        def _run_extra():
            updated = on_extra(dlg) if on_extra else None
            if updated is not None:
                body.setText(_rich(updated))
        extra.clicked.connect(_run_extra)
    yes.clicked.connect(dlg.accept)
    bb.rejected.connect(dlg.reject)
    lay.addWidget(bb)

    answered = bool(dlg.exec())
    if box is not None:
        checkbox["checked"] = box.isChecked()
    return answered


def copy_to_clipboard(text):
    """Put `text` on the system clipboard, returning True if it landed.

    Best-effort on purpose: every caller shows the text itself as well, so a clipboard
    that isn't there (a mocked or headless Qt, as in the Pyodide demo) costs the reader
    a manual select-and-copy rather than costing them what they wrote.
    """
    try:
        QApplication.clipboard().setText(text)
        return True
    except Exception:
        print(traceback.format_exc())
        return False


def _prompt(text, **kw):
    kw.setdefault("title", APP_NAME)
    return getText(text, **kw)


def _safe(fn):
    """Wrap a menu action so a bug here shows a plain warning dialog instead of
    Anki's raw traceback box. The full traceback still goes to stdout (visible in
    Anki's debug console) for anyone actually trying to fix it; the dialog only needs
    enough for a user to describe what happened.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            print(traceback.format_exc())
            _warn(f"Something went wrong: {e}<br><br>"
                  "If a backup was taken before this ran, Advanced has tools to "
                  "revert to it: Restore intern pearls deck or Restore full collection.")
    return wrapper


def _bg_safe(fn):
    """Like `_safe`, but for calls that fire on their own (a startup check, a poll timer)
    rather than from a menu click. A blocking warning dialog is fine for a menu action —
    the user just clicked something and is looking at the screen — but popping one up
    unprompted, possibly mid-review, is jarring. Background failures print to console
    (same as `_safe`) and surface as a transient tooltip instead of a modal.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            print(traceback.format_exc())
            try:
                tooltip(f"Intern Pearls: background check failed ({e})",
                       period=4000, parent=mw)
            except Exception:
                pass
    return wrapper


@contextmanager
def wait_cursor():
    """Show the busy cursor around a blocking call on the main thread (a manifest
    fetch, a preview download). The work still blocks; this makes the wait read as
    "working" instead of "frozen". Restores the cursor even if the call raises.
    """
    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    try:
        yield
    finally:
        QApplication.restoreOverrideCursor()


@contextmanager
def cancellable_progress(title, total):
    """A determinate, cancellable progress dialog for a loop of `total` steps.

    `mw.progress.start()`/`.update()` is Anki's simple busy-indicator API: no
    percentage, and nothing ever checks for a cancel, so a Cancel button (if one
    even shows) does nothing. On a multi-deck update that's a real per-deck network
    fetch, that reads as a hang with no way out — this is the one place in the
    add-on where a single step can plausibly take a while. Bypasses `mw.progress`
    for exactly that reason, in favor of a real `QProgressDialog`.

    Yields `step(i, label)`: call it right before doing the i-th (1-based) unit of
    work. Returns False if the user has clicked Cancel since the last call, in
    which case the caller must stop *before* starting that unit of work — every
    cancel point in this add-on sits between whole decks, never mid-import, so the
    collection is always left in a consistent, already-backed-up state.
    """
    dlg = QProgressDialog(title, "Cancel", 0, total, mw)
    dlg.setWindowModality(Qt.WindowModality.WindowModal)
    dlg.setMinimumDuration(0)
    dlg.setAutoClose(True)
    dlg.setValue(0)

    def step(i, label):
        dlg.setLabelText(label)
        dlg.setValue(i - 1)
        QApplication.processEvents()
        return not dlg.wasCanceled()

    try:
        yield step
    finally:
        dlg.setValue(total)
        dlg.close()


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


def muted_label(text):
    """Secondary explanatory text at the dialog's normal font size."""
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setStyleSheet(f"color: {colors()['muted']};")
    return lbl


def hint_label(text, top_margin=0):
    """Small-print fine detail under a control."""
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    margin = f" margin-top: {top_margin}px;" if top_margin else ""
    lbl.setStyleSheet(f"color: {colors()['muted']}; font-size: 11px;{margin}")
    return lbl


def link_button(label, on_click=None, tooltip_text=None, align_left=False):
    """A flat, accent-colored button that reads as a link rather than a push button."""
    btn = QPushButton(label)
    btn.setFlat(True)
    align = " text-align: left;" if align_left else ""
    btn.setStyleSheet(f"color: {colors()['accent']}; font-size: 12px;{align}")
    if tooltip_text:
        btn.setToolTip(tooltip_text)
    if on_click:
        btn.clicked.connect(on_click)
    return btn
