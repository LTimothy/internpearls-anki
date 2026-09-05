#!/usr/bin/env python3
"""Render a real add-on dialog to a PNG, without running Anki.

Why this exists: our dialogs are Qt widgets, and Qt fails silently. A stylesheet
declaration it doesn't like is not an error, it's just absent, so a rule can read
correctly, pass review, pass its tests, and still never paint. v0.32.1 fixed two of
those. tests/ can't catch them, because it runs a fake Qt that never paints. This does.

This is the "look at it" half. The "assert on it" half is qt_tests/, which renders the
same scenes through the same harness and fails a build over them. Both import
qt_tests/harness.py, so the tool and the tests can never disagree about what a scene is.

Requires PyQt6 (`pip install PyQt6`). Anki's own aqt.qt is nothing but
`from PyQt6.QtCore/QtGui/QtWidgets import *`, so real PyQt6 here builds the same
widgets Anki does. Nothing in this file ships in the .ankiaddon.

Usage:
    python3 tools/render_dialog.py --list
    python3 tools/render_dialog.py confirm --out confirm.png
    python3 tools/render_dialog.py confirm --expand 1 --feedback --dark
    python3 tools/render_dialog.py manage-decks --decks-dir ~/some/deck-source

No card content lives in this file: every scene's fixture is synthetic.
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "qt_tests"))

try:
    import harness
except ImportError:
    sys.exit("This tool needs PyQt6 to render real widgets: pip install PyQt6")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("scene", nargs="?", default="confirm")
    ap.add_argument("--list", action="store_true", help="list scenes and exit")
    ap.add_argument("--out", default="dialog.png")
    ap.add_argument("--size", default="640x560", help="WxH, default 640x560")
    ap.add_argument("--dark", action="store_true",
                    help="select palette.py's dark colour set, the one a real dialog "
                         "uses under Anki's night mode, and approximate Anki's window "
                         "colours for anything that still reads the platform palette. "
                         "That second part is an approximation, not a reproduction of "
                         "night mode")
    ap.add_argument("--feedback", action="store_true", help="confirm: feedback boxes on")
    ap.add_argument("--grouped", action="store_true",
                    help="confirm: render a shared change-note group instead")
    ap.add_argument("--expand", default="", help="row indices to open, e.g. 0,2")
    ap.add_argument("--limit", type=int, default=0, help="confirm: cap the card count")
    ap.add_argument("--decks-dir", default="", help="manage-decks: a local source folder")
    args = ap.parse_args()

    if args.list:
        for name, (_, desc) in sorted(harness.SCENES.items()):
            print(f"  {name:16} {desc}")
        return
    if args.scene not in harness.SCENES:
        sys.exit(f"unknown scene {args.scene!r}; try --list")

    width, height = (int(v) for v in args.size.lower().split("x"))
    shot = harness.render(
        args.scene,
        theme="dark" if args.dark else "light",
        expand=[int(i) for i in args.expand.split(",") if i.strip()],
        size=(width, height),
        limit=args.limit, feedback=args.feedback, grouped=args.grouped,
        decks_dir=args.decks_dir,
    )
    shot.image.save(args.out)
    print(f"wrote {args.out}  [{shot.dialog.windowTitle()}]")


if __name__ == "__main__":
    main()
