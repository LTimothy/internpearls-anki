#!/usr/bin/env python3
"""Render a real add-on dialog to a PNG, without running Anki.

Why this exists: our dialogs are Qt widgets, and Qt fails silently. A stylesheet
declaration it doesn't like is not an error, it's just absent, so a rule can read
correctly, pass review, pass its tests, and still never paint. v0.32.1 fixed two of
those. Neither pytest nor the Pyodide demo can catch them, because neither renders
real Qt. This does.

It reuses tests/mock_anki.py for the whole fake Anki world (collection, config,
aqt.utils, the anki package) and then replaces just that harness's fake Qt with real
PyQt6. So the two are deliberate opposites, and are not redundant:

    tests/mock_anki.py   fake Qt   -> assert on structure, in CI, no display needed
    tools/render_dialog.py real Qt -> look at pixels, locally, needs PyQt6

Requires PyQt6 (`pip install PyQt6`). Anki's own aqt.qt is nothing but
`from PyQt6.QtCore/QtGui/QtWidgets import *`, so real PyQt6 here builds the same
widgets Anki does. Nothing in this file ships in the .ankiaddon.

Usage:
    python3 tools/render_dialog.py --list
    python3 tools/render_dialog.py review --out review.png
    python3 tools/render_dialog.py review --expand 1 --feedback --dark
    python3 tools/render_dialog.py review --apkg ~/some/deck.apkg
    python3 tools/render_dialog.py manage-decks --decks-dir ~/some/deck-source

No card content lives in this file: the default fixture is synthetic, and --apkg
reads a real deck through the add-on's own apkg_note_details.
"""
import argparse
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _bootstrap():
    """Install the mock Anki world, then swap its fake Qt for real PyQt6.

    Order matters: every internpearls module binds its Qt names at import time
    (`from aqt.qt import QLabel`), so aqt.qt must already hold the real classes
    before the first one is imported.
    """
    sys.path.insert(0, os.path.join(ROOT, "tests"))
    sys.path.insert(0, ROOT)
    try:
        from PyQt6 import QtCore, QtGui, QtWidgets
    except ImportError:
        sys.exit("This tool needs PyQt6 to render real widgets: pip install PyQt6")

    import mock_anki
    mock = mock_anki.install()

    import aqt.qt as aqt_qt
    for module in (QtCore, QtGui, QtWidgets):
        for name in dir(module):
            if name.startswith("Q") or name == "Qt":
                setattr(aqt_qt, name, getattr(module, name))

    # Every dialog parents itself to mw, which here is the mock's plain object rather
    # than a QWidget, and real Qt rejects that outright. Parentless is fine for a grab.
    _dialog_init = QtWidgets.QDialog.__init__

    def _init(self, parent=None, *a, **k):
        if not isinstance(parent, QtWidgets.QWidget):
            parent = None
        _dialog_init(self, parent, *a, **k)

    QtWidgets.QDialog.__init__ = _init

    # Same trick conftest.py uses: register the package by path only, so importing a
    # submodule never runs __init__.py's menu and startup wiring.
    pkg = types.ModuleType("internpearls")
    pkg.__path__ = [os.path.join(ROOT, "internpearls")]
    sys.modules["internpearls"] = pkg
    return mock, aqt_qt


# --------------------------------------------------------------------- fixtures
def _synthetic_details():
    """One of every branch the review list can take, so a render exercises the whole
    layout: tagged and untagged, one-line and wrapping, cloze and basic and image,
    with and without dosing. Invented content, deliberately: no real card belongs in
    this repo.
    """
    return [
        {"guid": "g1", "notetype": "Study Deck - Basic",
         "fields": [("Front", "Which widget is this, in one short line?"),
                    ("Back", "A basic note with a tag."),
                    ("Why", "Short rows are the common case."),
                    ("Image", ""), ("Tag", "Widgets"), ("Dosing", ""),
                    ("Notes", "")]},
        {"guid": "g2", "notetype": "Study Deck - Basic",
         "fields": [("Front", "A deliberately long prompt, written to run past the "
                              "dialog's width so the wrap lands under the text and "
                              "not under the caret, which is where it used to go?"),
                    ("Back", "A wrapping basic note carrying dosing."),
                    ("Why", "The wrap point is what the tag column used to break."),
                    ("Image", ""), ("Tag", "Layout"),
                    ("Dosing", "example 1-2 units/kg, cited source"),
                    ("Notes", "")]},
        {"guid": "g3", "notetype": "Study Deck - Basic",
         "fields": [("Front", "An untagged row, to check the left edge lines up?"),
                    ("Back", "No tag on this one."), ("Why", ""),
                    ("Image", ""), ("Tag", ""), ("Dosing", ""), ("Notes", "")]},
        {"guid": "g4", "notetype": "Study Deck - Cloze",
         "fields": [("Text", "A cloze note fills {{c1::one}} deletion, and "
                             "{{c2::another}} one, in the deck's own blue."),
                    ("Why", "Deletions are shown filled: the fact is in them."),
                    ("Image", ""), ("Dosing", ""), ("Notes", "")]},
        {"guid": "g5", "notetype": "Study Deck - Image ID",
         "fields": [("Image", '<img src="example-diagram.jpg">'),
                    ("Prompt", "What is this, and what does it show?"),
                    ("Answer", "An image note names its picture rather than "
                               "painting it."),
                    ("Why", "Review never extracts the .apkg's media."),
                    ("Notes", "")]},
    ]


def _details_from_apkg(path):
    from internpearls.logic import apkg_note_details
    return apkg_note_details(os.path.expanduser(path))


# ----------------------------------------------------------------------- scenes
def _scene_review(args, mock):
    from internpearls import review
    details = (_details_from_apkg(args.apkg) if args.apkg else _synthetic_details())
    if args.limit:
        details = details[:args.limit]
    mock.mw._config = {"collect_card_feedback": args.feedback}
    name = os.path.basename(args.apkg).replace(".apkg", "") if args.apkg else "Example Deck"
    return lambda: review.review_new_cards(None, [(name, details)], {})


def _scene_digest(args, mock):
    from internpearls import review
    entries = [{"deck": "Example Deck", "guid": "g1",
                "front": "Which widget is this, in one short line?",
                "note": "reads as two facts at once"}]
    return lambda: review.offer_feedback_digest(None, entries)


def _scene_settings(args, mock):
    from internpearls import dialogs
    return dialogs.open_settings


def _scene_manage_decks(args, mock):
    from internpearls import dialogs
    if args.decks_dir:
        mock.mw._config = {"decks_dir": os.path.expanduser(args.decks_dir)}
    return dialogs.manage_decks


SCENES = {
    "review": (_scene_review, "the new-card review list (--apkg, --expand, --feedback)"),
    "digest": (_scene_digest, "the flagged-card feedback digest"),
    "settings": (_scene_settings, "the Settings dialog"),
    "manage-decks": (_scene_manage_decks, "the deck manager (--decks-dir for a source)"),
}


# ----------------------------------------------------------------------- render
def _carets(dlg, aqt_qt):
    return [b for b in dlg.findChildren(aqt_qt.QPushButton)
            if b.text() in ("▸", "▾")]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("scene", nargs="?", default="review")
    ap.add_argument("--list", action="store_true", help="list scenes and exit")
    ap.add_argument("--out", default="dialog.png")
    ap.add_argument("--size", default="640x560", help="WxH, default 640x560")
    ap.add_argument("--dark", action="store_true",
                    help="approximate dark via Qt's colour scheme. NOT Anki's night "
                         "theme, so it shows whether hardcoded colours survive a dark "
                         "background, not that night mode is correct")
    ap.add_argument("--feedback", action="store_true", help="review: feedback boxes on")
    ap.add_argument("--expand", default="", help="review: row indices to open, e.g. 0,2")
    ap.add_argument("--limit", type=int, default=0, help="review: cap the card count")
    ap.add_argument("--apkg", default="", help="review: render a real .apkg's notes")
    ap.add_argument("--decks-dir", default="", help="manage-decks: a local source folder")
    args = ap.parse_args()

    if args.list:
        for name, (_, desc) in SCENES.items():
            print(f"  {name:14} {desc}")
        return
    if args.scene not in SCENES:
        sys.exit(f"unknown scene {args.scene!r}; try --list")

    mock, aqt_qt = _bootstrap()
    app = aqt_qt.QApplication(sys.argv)
    if args.dark:
        try:
            app.styleHints().setColorScheme(aqt_qt.Qt.ColorScheme.Dark)
        except AttributeError:
            print("warning: this Qt is too old for --dark, rendering light",
                  file=sys.stderr)

    open_dialog = SCENES[args.scene][0](args, mock)
    width, height = (int(v) for v in args.size.lower().split("x"))
    expand = [int(i) for i in args.expand.split(",") if i.strip()]
    grabbed = []

    def fake_exec(self):
        """Show and capture instead of blocking on a modal loop.

        Patched onto the class, so it covers dialogs the add-on subclasses too.
        """
        self.resize(width, height)
        self.show()
        app.processEvents()
        carets = _carets(self, aqt_qt)
        for i in expand:
            if i < len(carets):
                carets[i].click()
        app.processEvents()
        self.grab().save(args.out)
        grabbed.append(self.windowTitle())
        return 1

    aqt_qt.QDialog.exec = fake_exec
    open_dialog()
    if not grabbed:
        sys.exit(f"scene {args.scene!r} opened no dialog (it may have returned early)")
    print(f"wrote {args.out}  [{grabbed[0]}]")


if __name__ == "__main__":
    main()
