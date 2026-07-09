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
from aqt.qt import QApplication, QLabel, QPushButton, Qt
from aqt.utils import askUser, getText, showInfo, showWarning, tooltip

from .config import APP_NAME

ACCENT = "#2563eb"   # link-style buttons and the "new deck" pill; readable on both themes


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
                  "revert to it: Import intern pearls deck or Restore full collection.")
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
    lbl.setStyleSheet("color: gray;")
    return lbl


def hint_label(text, top_margin=0):
    """Small-print fine detail under a control."""
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    margin = f" margin-top: {top_margin}px;" if top_margin else ""
    lbl.setStyleSheet(f"color: gray; font-size: 11px;{margin}")
    return lbl


def link_button(label, on_click=None, tooltip_text=None, align_left=False):
    """A flat, accent-colored button that reads as a link rather than a push button."""
    btn = QPushButton(label)
    btn.setFlat(True)
    align = " text-align: left;" if align_left else ""
    btn.setStyleSheet(f"color: {ACCENT}; font-size: 12px;{align}")
    if tooltip_text:
        btn.setToolTip(tooltip_text)
    if on_click:
        btn.clicked.connect(on_click)
    return btn
