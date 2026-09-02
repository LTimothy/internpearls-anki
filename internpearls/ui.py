"""Dialog wrappers, error-safety decorators, and shared widget styling.

Thin wrappers so every dialog carries the "Intern Pearls" title (Anki's helpers
default to the generic "Anki") and so anything longer than a message box can hold gets
a real scroll area with its buttons outside it. Route any new dialog through these,
not the raw aqt.utils calls, so a future addition here stays consistent automatically.

A screen that lists cards, decks or settings does not belong in any of these as text:
it builds rows (widgets.simple_row, review.build_list_body) and comes back through
_ask_with_widget. Nothing in the add-on renders an HTML bullet list any more.

The interleave guard (`_manual_flow`, `manual_sync_in_progress`) lives here for a
different reason: every menu action that writes the collection has to hold it, and
those are split across sync.py and collection.py, which cannot share a flag of their
own without a circular import.

The label/button helpers at the bottom exist for the same reason: every dialog's
headings, hints, and link-style buttons share one look defined here, instead of each
dialog carrying its own copy of the stylesheet strings.
"""
import functools
import traceback
from contextlib import contextmanager

from aqt import mw
from aqt.qt import (QApplication, QCheckBox, QDialog, QDialogButtonBox, QFrame,
                    QLabel, QMessageBox, QProgressDialog, QPushButton, QScrollArea,
                    QSizePolicy, Qt, QVBoxLayout)
from aqt.utils import askUser, getText, showInfo, showWarning, tooltip

from .config import APP_NAME
from .palette import colors


# True while an interactive flow that writes the collection is running: the sync,
# reconcile and import flows in sync.py, and the Advanced actions in collection.py that
# import, restore, delete, or reconcile note types. The unattended auto-sync poll
# checks it and skips its tick rather than interleaving, since both write the
# collection and both persist installed.json, and the poll's apply half lands from a
# QueryOp callback, which can fire while one of these flows is sitting inside a modal
# dialog's own event loop.
#
# It lives here rather than in sync.py or collection.py because both of those need it
# and sync.py imports collection.py, so a flag in either would be a circular import.
# ui.py is imported by both, imports neither, and already owns the other decorator
# every menu action wears (_safe).
_manual_in_progress = False


def manual_sync_in_progress():
    """Whether an interactive flow that writes the collection is running right now.

    Read by background.py's poll, which stays quiet and retries on the next tick rather
    than queueing behind this. A plain flag rather than a lock: nothing here nests, and
    the poll has nothing to wait for.
    """
    return _manual_in_progress


def _manual_flow(fn):
    """Hold `manual_sync_in_progress()` for the whole of an interactive flow.

    Applied under @_safe so the flag is released even when the flow raises, which is the
    one thing @_safe's own warning dialog would otherwise leave stuck on.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        global _manual_in_progress
        _manual_in_progress = True
        try:
            return fn(*args, **kwargs)
        finally:
            _manual_in_progress = False
    return wrapper


def _info(text, **kw):
    kw.setdefault("title", APP_NAME)
    kw.setdefault("textFormat", "rich")
    return showInfo(text, **kw)


def _warn(text, **kw):
    kw.setdefault("title", APP_NAME)
    kw.setdefault("textFormat", "rich")
    return showWarning(text, **kw)


def _ask(text, yes_label=None, no_label=None, **kw):
    """A yes/no confirmation, with buttons that can name the action instead.

    Pass both labels for a question whose two answers cost different things ("Apply the
    new look" / "Keep my current look"): a bare Yes/No there makes the reader scroll
    back up to the question to work out what No means, which is the same reason
    _ask_scrollable defaults to action-neutral labels rather than Yes/No.

    The named-button question is a QMessageBox built here rather than Anki's own
    askUserDialog, which gives every button AcceptRole: that leaves Escape and the
    window's close box inert, so the dialog cannot be dismissed at all, and sets no
    default, so Return activates whichever button was added first. On the questions
    that matter here that first button is the one that consents to a schema change,
    which is exactly the agreeing-by-not-reading this add-on refuses everywhere else.
    Below, the declining answer carries RejectRole and is both the default and the
    escape button, so Return, Escape and the close box all mean the safe answer and
    only a deliberate click can consent. Qt orders the two by role, so the affirming
    action still sits where the running platform puts it.
    """
    kw.setdefault("title", APP_NAME)
    if yes_label and no_label:
        box = QMessageBox(mw)
        box.setWindowTitle(kw["title"])
        box.setIcon(QMessageBox.Icon.Question)
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(text)
        yes = box.addButton(yes_label, QMessageBox.ButtonRole.AcceptRole)
        no = box.addButton(no_label, QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(no)
        box.setEscapeButton(no)
        box.exec()
        answered = box.clickedButton() is yes
        box.deleteLater()   # parented to mw, so closing alone leaves it behind
        return answered
    return askUser(text, **kw)


def _ask_scrollable(text, yes_label="Continue", no_label="Cancel", max_height=340,
                    extra_label=None, on_extra=None, checkbox=None, title=None,
                    open_external_links=False):
    """Like _ask, but for content whose length isn't bounded by anything short. A plain
    QMessageBox (what askUser/_ask use) has no scroll area, so long text just makes the
    box taller, and once it's taller than the screen its Yes/No buttons end up
    off-screen with no way to reach them, an unusable and undismissable dialog. This
    scrolls the body in a fixed-height viewport instead, with the buttons pinned outside
    it, so they're always reachable no matter how long the content is.

    For prose. Every screen that lists cards or decks builds rows and goes through
    _ask_with_widget below; About is the caller this one is left for, and its length is
    exactly why it needs the scroll area.

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
    # Parenting to mw hands Qt ownership, so without this every dialog a session opens
    # stays alive until Anki quits, card rows and decoded pictures included. Deferred
    # deletion rather than WA_DeleteOnClose: close runs before exec() returns, so that
    # attribute would free the checkbox above out from under the line that reads it.
    dlg.deleteLater()
    return answered


def _place_checkbox(dialog_layout, body, box):
    """Put the checkbox with the sentence that explains it, not under the list.

    A body's list streams as many rows as the run has pending, so a checkbox added below
    it can sit hundreds of rows past the paragraph above the list saying what ticking it
    costs, and a reader who never scrolls back never meets the two together. Inserted
    directly above the body's own scroll area instead, which puts it under whatever fixed
    text the body opens with and on the same screen as it. A body with no scroll area to
    sit above (a plain widget) keeps the old placement, at the bottom of the dialog.

    The box lands under the LAST of that fixed text, so a caller writing more than one
    paragraph there has to put the one this box answers last (see update_decks): a
    checkbox sitting directly under a paragraph about something else reads as answering
    that paragraph instead.
    """
    lay = body.layout() if hasattr(body, "layout") else None
    for i in range(lay.count() if lay is not None else 0):
        item = lay.itemAt(i)
        if isinstance(item.widget() if item else None, QScrollArea):
            lay.insertWidget(i, box)
            return
    dialog_layout.addWidget(box)


def _ask_with_widget(body, yes_label="Continue", no_label="Cancel", checkbox=None,
                     title=None, min_width=560, min_height=520, on_close=None,
                     open_size=None):
    """Like _ask_scrollable, but the body is a caller-built widget rather than an HTML
    string, for a screen whose content is more than one scrollable label can lay out
    well: fixed summary text above a list that should take whatever height the resized
    dialog leaves it, e.g. Update my decks' inline card list. The caller's widget owns
    its own internal layout and scrolling; this only wraps it with the add-on's title,
    the optional checkbox, and the Continue/Cancel buttons, on the same roles and the
    same checkbox-state-written-back-in-place contract _ask_scrollable uses.

    `open_size` is the (width, height) the dialog opens at, clamped to the screen it
    lands on so a laptop display never gets a dialog taller than itself; the minimums
    above still hold and the dialog stays resizable either way. Left at None the dialog
    opens at its minimums, which is right for a short confirmation; the update screen
    passes a larger size because its list is the densest thing the add-on draws.

    `on_close` runs once the reader has answered, and it exists because of when: adding
    `body` to this dialog's layout hands it to Qt, so when this function returns and
    lets go of the dialog, Qt destroys the whole tree. A caller that needs to read its
    own widgets one last time (Update my decks reads the notes typed into its cards, and
    stops the timer that debounces saving them) cannot do it afterwards: every one of
    those objects is freed C++ by then, and touching one raises "wrapped C/C++ object
    has been deleted". Running here, while the dialog is still held, is the last moment
    they exist. Note that the mock-Anki suite cannot catch a mistake of this shape,
    since its widgets are plain Python objects with no C++ lifetime behind them.
    """
    dlg = QDialog(mw)
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
    # After on_close, never before: that callback is the caller's last read of its own
    # widgets, and this is what frees them (see _ask_scrollable for why not sooner).
    dlg.deleteLater()
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
    """A single-line text prompt. Returns the entered text, or None if cancelled --
    unlike aqt.utils.getText's raw (text, ok) tuple, so a caller can write
    `if new is None: return` instead of unpacking at every call site."""
    kw.setdefault("title", APP_NAME)
    value, ok = getText(text, **kw)
    return value if ok else None


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

    `step.pump()` is that same check without the step: it repaints and processes
    pending clicks, then reports whether Cancel is still un-clicked. It takes an
    ignored positional argument so it can be handed straight to `net._http_get`'s
    `on_chunk`, which is the whole reason it exists. Between two steps sits one deck's
    download, a single blocking read during which nothing repaints, so Cancel could be
    clicked and never processed; on a one-deck run that made the button decorative.
    Pumping from inside the read fixes that without moving any cancel point: a False
    return there aborts only the in-flight *download* (net raises DownloadCancelled,
    before that deck's import has begun), and the import boundary stays exactly where it
    was, so "never abort partway through an import" still holds.

    Pumping runs arbitrary queued events, so it is only safe while nothing else can be
    clicked: this dialog is WindowModal and parented to `mw`, which blocks input to the
    main window and everything under it for as long as it is up.
    """
    dlg = QProgressDialog(title, "Cancel", 0, total, mw)
    dlg.setWindowTitle(APP_NAME)
    dlg.setWindowModality(Qt.WindowModality.WindowModal)
    dlg.setMinimumDuration(0)
    dlg.setAutoClose(True)
    dlg.setValue(0)

    def pump(_bytes_so_far=None):
        QApplication.processEvents()
        return not dlg.wasCanceled()

    def step(i, label):
        dlg.setLabelText(label)
        dlg.setValue(i - 1)
        return pump()

    step.pump = pump

    try:
        yield step
    finally:
        dlg.setValue(total)
        dlg.close()
        dlg.deleteLater()   # parented to mw, so closing alone leaves it behind


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

    A real HLine rather than a border-top on the heading below it: Qt won't paint a lone
    border on a plain container widget, and a selector-less stylesheet on a container
    propagates into its children, which each then draw their own rule.

    Carries `panel_rule`, the colour a bounded region of a dialog is drawn with, rather
    than `row_rule`, which is the fainter hairline between two rows of one list. The
    distinction is what each rule separates: rows of a list are still one thing, so their
    divider stays quiet, while these sections are four unrelated decisions that happen to
    share a window, so the line between them is the strongest thing saying so.
    """
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFrameShadow(QFrame.Shadow.Plain)
    line.setFixedHeight(1)
    line.setStyleSheet(f"color: {colors()['panel_rule']};")
    return line


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
