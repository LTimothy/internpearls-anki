"""Real-PyQt6 harness: the add-on's dialogs, built with the widgets Anki really uses.

Shared by tools/render_dialog.py (render and look at it) and qt_tests/ (render and
assert on it), so the tool and the tests can never disagree about what a scene is.

Why this lives outside tests/: every internpearls module binds its Qt names at import
time (`from aqt.qt import QLabel`), so whichever Qt is installed into aqt.qt first wins
for the entire process. tests/ installs the mock; this installs real PyQt6. The two
cannot share one. That is not a theory: after swapping aqt.qt's names, an
already-imported internpearls.ui still holds mock_anki.QLabel. So these run as their
own pytest invocation, guarded by pytest.ini's testpaths and by bootstrap() below.

    tests/       fake Qt -> assert on structure, no display, no PyQt6 needed
    qt_tests/    real Qt -> assert on pixels, needs PyQt6, offscreen

Requires PyQt6 from pip. Anki's own aqt.qt is nothing but
`from PyQt6.QtCore/QtGui/QtWidgets import *`, so real PyQt6 here builds the same
widgets Anki does. Nothing in this directory ships in the .ankiaddon.
"""
import os
import sys
import types
from collections import namedtuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Duplicates review.py's private caret glyphs deliberately: internpearls cannot be
# imported until bootstrap() has installed real Qt.
CARET_CLOSED = "▸"
CARET_OPEN = "▾"

Shot = namedtuple("Shot", "image dialog theme scene")

_BOOT = None
_APP = None


def bootstrap():
    """Install the mock Anki world, then replace its fake Qt with real PyQt6.

    Order matters and is not recoverable: every internpearls module binds its Qt names
    at import time, so aqt.qt must already hold the real classes before the first one is
    imported. Idempotent.
    """
    global _BOOT
    if _BOOT is not None:
        return _BOOT
    if "internpearls.ui" in sys.modules or "internpearls.review" in sys.modules:
        raise RuntimeError(
            "internpearls was imported before real Qt was installed, so its modules "
            "hold mock widgets and every assertion here would pass while proving "
            "nothing. This happens when qt_tests/ is collected alongside tests/. Run "
            "it as its own invocation: pytest qt_tests/")

    existing_qt = sys.modules.get("aqt.qt")
    existing_label = getattr(existing_qt, "QLabel", None)
    if existing_label is not None and getattr(existing_label, "__module__", "") == "mock_anki":
        raise RuntimeError(
            "aqt.qt already holds mock widgets, so tests/conftest.py's mock Anki has "
            "already installed itself in this process and bootstrapping real Qt on top "
            "of it would leave some internpearls modules holding mock widgets and "
            "others holding real ones. This happens when tests/ is collected alongside "
            "qt_tests/. Run it as its own invocation: pytest qt_tests/")

    sys.path.insert(0, os.path.join(ROOT, "tests"))
    sys.path.insert(0, ROOT)
    from PyQt6 import QtCore, QtGui, QtWidgets

    import mock_anki
    mock = mock_anki.install()

    import aqt.qt as aqt_qt
    for module in (QtCore, QtGui, QtWidgets):
        for name in dir(module):
            if name.startswith("Q") or name == "Qt":
                setattr(aqt_qt, name, getattr(module, name))

    # internpearls/palette.py reads Anki's own theme through aqt.theme.theme_manager,
    # not through the Qt palette apply_theme() below sets, so it needs its own stub
    # here. Starts light; apply_theme() flips night_mode to match its own theme name.
    aqt_theme = types.ModuleType("aqt.theme")
    aqt_theme.theme_manager = types.SimpleNamespace(night_mode=False)
    sys.modules["aqt.theme"] = aqt_theme
    sys.modules["aqt"].theme = aqt_theme

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

    _BOOT = (mock, aqt_qt)
    return _BOOT


def app():
    """The process's one QApplication. Qt permits exactly one, so this is a singleton
    rather than a fixture that could be torn down and rebuilt per test."""
    global _APP
    _, q = bootstrap()
    if _APP is None:
        _APP = q.QApplication([])
        # Turn text antialiasing off application-wide, before any widget is built. The
        # colour tests read a label's foreground as the highest-contrast pixel in its
        # rect, which only works when the glyph is one flat colour. Antialiasing paints
        # blended edge pixels, and on some platforms (ubuntu CI, not macOS) those edges
        # are subpixel-coloured fringes, which are more contrasty in a hue direction than
        # the real glyph, so the reader locks onto a fringe and the ratio is wrong. Off
        # means every glyph pixel is the declared colour, identically on every platform.
        _font = _APP.font()
        _font.setStyleStrategy(q.QFont.StyleStrategy.NoAntialias)
        _APP.setFont(_font)
    return _APP


# Qt's own default palettes, hardcoded rather than read from the running one: the
# offscreen platform always reports light whatever the host OS is set to, so reading
# the live palette would render light twice and report green. Values measured from a
# real render on each platform, 2026-07-16.
THEMES = {
    "light": {"Window": "#efefef", "WindowText": "#000000", "Base": "#ffffff",
              "Text": "#000000", "Button": "#efefef", "ButtonText": "#000000"},
    "dark": {"Window": "#2f2f31", "WindowText": "#d7d7d7", "Base": "#2f2f31",
             "Text": "#d7d7d7", "Button": "#2f2f31", "ButtonText": "#d7d7d7"},
}


def apply_theme(name):
    """Force a palette explicitly.

    Deliberately not styleHints().setColorScheme(): that is a verified no-op on Qt 6.9
    under both the offscreen and cocoa platforms, which is why the render tool's old
    --dark flag never did anything at all. An explicit palette is what actually
    repaints.

    This approximates Anki's night theme, it does not reproduce it. It shows whether a
    hardcoded colour survives a dark window, which is the bug class we have actually
    hit; it does not show that night mode is correct.

    Also flips the aqt.theme.theme_manager stub bootstrap() installs, so
    internpearls/palette.py picks the same set this scene is actually painted with:
    without this a "dark" scene still asked the palette for its light colours, which
    were never tuned against this window.
    """
    _, q = bootstrap()
    pal = q.QPalette()
    for role, colour in THEMES[name].items():
        pal.setColor(getattr(q.QPalette.ColorRole, role), q.QColor(colour))
    app().setPalette(pal)
    sys.modules["aqt.theme"].theme_manager.night_mode = (name == "dark")


def synthetic_details():
    """One of every branch the review list can take, so a render exercises the whole
    layout: tagged and untagged, one-line and wrapping, cloze and basic and image, with
    and without dosing. Invented content, deliberately: no real card belongs in this
    repo.

    Index 1 is the only row carrying a Dosing field. Tests that assert on the dosing
    block must expand row 1, not row 0.

    Rows 0 and 1 also carry a `kind`, one of each ("new" and "changed"), so a render
    actually paints both row markers widgets.chip_cell can produce for a card; row 1's
    `was` entry paints its previous-value line too. Without this, test_contrast.py's
    fixture never exercised the marker pill at all.
    """
    return [
        {"guid": "g1", "notetype": "Study Deck - Basic", "kind": "new",
         "fields": [("Front", "Which widget is this, in one short line?"),
                    ("Back", "A basic note with a tag."),
                    ("Why", "Short rows are the common case."),
                    ("Image", ""), ("Tag", "Widgets"), ("Dosing", ""),
                    ("Notes", "")]},
        {"guid": "g2", "notetype": "Study Deck - Basic", "kind": "changed",
         "was": {"Back": "A wrapping basic note, before its Back field was rewritten."},
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
                    ("Answer", "An image note's picture is its question."),
                    ("Why", "Opening a row is what extracts it."),
                    ("Notes", "")]},
    ]


# ----------------------------------------------------------------------- scenes
_FIXTURE_APKG = None


def _fixture_image_apkg():
    """A throwaway .apkg carrying one solid magenta PNG under the filename
    synthetic_details()'s image note references.

    Written with real Qt rather than checked in: a binary fixture in this repo would be
    one more thing to keep in step with the note that points at it, and QImage.save is
    already available here.

    Built once per process, in a TemporaryDirectory registered with atexit: the dialog
    extracts from the .apkg lazily when a row expands, well after this function has
    returned, so the directory must outlive the call; atexit is what removes it again
    when the process ends. Contents never vary, so caching the path also saves rebuilding
    it on every render.
    """
    global _FIXTURE_APKG
    if _FIXTURE_APKG is not None:
        return _FIXTURE_APKG
    import atexit
    import json
    import sqlite3
    import tempfile
    import zipfile
    _, q = bootstrap()
    tmpdir = tempfile.TemporaryDirectory(prefix="internpearls_qt_fixture_")
    atexit.register(tmpdir.cleanup)
    folder = tmpdir.name
    # Named .jpg because that's the filename the demo note references, but the bytes
    # written below are PNG; Qt sniffs image format from content, not the extension.
    png = os.path.join(folder, "example-diagram.jpg")
    image = q.QImage(120, 80, q.QImage.Format.Format_RGB32)
    image.fill(q.QColor("#ff00ff"))
    image.save(png, "PNG")
    db = os.path.join(folder, "collection.anki2")
    con = sqlite3.connect(db)
    con.execute("create table notes (id integer primary key, guid text, mid integer, "
                "flds text)")
    con.commit()
    con.close()
    path = os.path.join(folder, "fixture.apkg")
    with zipfile.ZipFile(path, "w") as z:
        z.write(db, "collection.anki2")
        z.write(png, "0")
        z.writestr("media", json.dumps({"0": "example-diagram.jpg"}))
    _FIXTURE_APKG = path
    return path


def _scene_digest(mock, opts):
    from internpearls import review
    entries = [{"deck": "Example Deck", "guid": "g1",
                "front": "Which widget is this, in one short line?",
                "note": "reads as two facts at once"}]
    return lambda: review.offer_feedback_digest(None, entries)


def _scene_settings(mock, opts):
    from internpearls import dialogs
    return dialogs.open_settings


_MANAGE_DECKS_FIXTURE = None


def _manage_decks_fixture(mock):
    """A manifest with one deck per sync state (new, update, current), plus the
    matching installed state file and collection notes, so all three state pills
    actually paint. Without this the scene's deck list was empty and the pills that
    read _STATE_STYLE were never measured by the render suite at all. Deck names are
    invented, like the rest of this file.

    Built once and cached: this writes real notes into the shared mock collection
    (see render()'s docstring: mock.mw.col is not reset between scenes), so a second
    call would just duplicate them for no benefit.
    """
    global _MANAGE_DECKS_FIXTURE
    if _MANAGE_DECKS_FIXTURE is not None:
        return _MANAGE_DECKS_FIXTURE
    import atexit
    import json
    import tempfile

    tmpdir = tempfile.TemporaryDirectory(prefix="internpearls_qt_manage_decks_")
    atexit.register(tmpdir.cleanup)
    folder = tmpdir.name

    root = "Intern Pearls::Intern Custom"
    new_deck = f"{root}::Widget Basics"
    update_deck = f"{root}::Gadget Care"
    current_deck = f"{root}::Gizmo Repair"
    decks = [
        {"name": new_deck, "apkg": "widget-basics.apkg", "version": "v2", "cards": 4},
        {"name": update_deck, "apkg": "gadget-care.apkg", "version": "v3", "cards": 6},
        {"name": current_deck, "apkg": "gizmo-repair.apkg", "version": "v1", "cards": 5},
    ]
    with open(os.path.join(folder, "manifest.json"), "w", encoding="utf8") as fh:
        json.dump({"schema": 2, "front_aliases": {}, "decks": decks,
                   "retired": {}, "deck_moves": {}}, fh)

    # installed.json, read through dialogs.INSTALLED (patched below). update_deck's
    # installed version differs from what the manifest now offers, so it reads
    # "update"; current_deck's matches, so it reads "current". new_deck has no entry
    # at all, so it reads "new".
    installed_path = os.path.join(folder, "installed.json")
    with open(installed_path, "w", encoding="utf8") as fh:
        json.dump({update_deck: "v2", current_deck: "v1"}, fh)
    from internpearls import dialogs
    dialogs.INSTALLED = installed_path

    # installed_matching_collection only trusts installed.json for a deck that also
    # has at least one of her notes actually sitting in it, so update_deck and
    # current_deck each need one.
    mock.col.add_note("manage-decks-update",
                      ["A gadget-care front", "back", "", "", "", "", ""],
                      ["InternPearls"], deck=update_deck)
    mock.col.add_note("manage-decks-current",
                      ["A gizmo-repair front", "back", "", "", "", "", ""],
                      ["InternPearls"], deck=current_deck)

    _MANAGE_DECKS_FIXTURE = folder
    return folder


_EMPTY_MANAGE_DECKS_FIXTURE = None


def _empty_manage_decks_fixture():
    """A local folder with no manifest.json, so `manage_decks()` opens with `rows`
    empty and the source reading "not configured". That's the empty-state branch
    `_scene_manage_decks` otherwise never exercises, since its default fixture always
    seeds three decks. Built once per process, same lifetime pattern as the other
    fixtures in this file.
    """
    global _EMPTY_MANAGE_DECKS_FIXTURE
    if _EMPTY_MANAGE_DECKS_FIXTURE is not None:
        return _EMPTY_MANAGE_DECKS_FIXTURE
    import atexit
    import tempfile
    tmpdir = tempfile.TemporaryDirectory(prefix="internpearls_qt_manage_decks_empty_")
    atexit.register(tmpdir.cleanup)
    _EMPTY_MANAGE_DECKS_FIXTURE = tmpdir.name
    return tmpdir.name


def _scene_manage_decks(mock, opts):
    from internpearls import dialogs
    if opts.get("empty"):
        mock.mw._config = {"decks_dir": _empty_manage_decks_fixture()}
    elif opts.get("decks_dir"):
        mock.mw._config = {"decks_dir": os.path.expanduser(opts["decks_dir"])}
    else:
        mock.mw._config = {"decks_dir": _manage_decks_fixture(mock)}
    return dialogs.manage_decks


def _scene_about(mock, opts):
    from internpearls import dialogs
    return dialogs.about


def _scene_configure_source(mock, opts):
    """The first screen a new user meets: which of the three sources decks come from.

    Runs the real configure_source(), which opens the choice dialog first, so this
    captures that dialog rather than any of the forms behind it. render()'s patched
    exec answers nothing, so the flow returns as if cancelled and never reaches the
    GitHub form, the folder prompt, or a fetch.
    """
    from internpearls import dialogs
    return dialogs.configure_source


def _scene_ask_scrollable(mock, opts):
    """A plain _ask_scrollable confirmation: reconcile_decks, clean_up_duplicates,
    remove_empty_cards, and Sync decks' own "Update these decks?" all render through
    this wrapper today. Kept as its own scene (distinct from "confirm", which moved to
    a widget body once it became the Update my decks screen) so this shared wrapper's
    own behavior, the scroll area, the top-alignment, the closed-by-default external
    links, stays under render test regardless of what any one caller currently does.
    """
    from internpearls.ui import _ask_scrollable
    body = (
        "<b>Example Deck</b><ul>"
        + "".join(f"<li>{d['fields'][0][1]}</li>" for d in synthetic_details()[:3])
        + "</ul><p>Nothing is added until you choose Continue.</p>")
    checkbox = ({"label": "Also apply the new card look (forces a one-time full "
                          "AnkiWeb sync)", "checked": False}
                if opts.get("checkbox") else None)
    return lambda: _ask_scrollable(body, yes_label="Continue", no_label="Cancel",
                                   checkbox=checkbox)


def _scene_confirm(mock, opts):
    """The Update my decks confirmation: fixed summary text above the streaming list,
    which opens with a per-deck summary section and then carries one section per deck
    holding everything pending for it, built the same way sync.py's update_decks()
    builds it (widgets.StreamingList over review._card_row rows for the new/changed
    cards, and widgets.simple_row for the deck-summary, retired and moved ones),
    wrapped by ui._ask_with_widget rather than _ask_scrollable, since the list needs to
    take the dialog's available height instead of sitting inside one scrollable label.

    All four row kinds sit under the one deck heading, which is what the real screen
    does: a retired card belongs to the deck it is retired out of, and a relocated one
    to the deck it is currently sitting in, with only its destination named on the row.

    The retired and moved rows are invented, generic fixture content, same as
    synthetic_details() above: no real card or deck name belongs in this repo.
    """
    from internpearls import review
    from internpearls.ui import _ask_with_widget
    details = synthetic_details()
    if opts.get("limit"):
        details = details[:opts["limit"]]
    mock.mw._config = {"collect_card_feedback": opts.get("feedback", False)}
    items = [("header", "1 deck has updates:"),
             ("deck", "Example Deck", "3 kept (1 changing) · 2 new"),
             ("header", "Example Deck")]
    for i, d in enumerate(details):
        if i:
            items.append(("sep",))
        items.append(("card", "Example Deck", d))
    items += [("sep",),
              ("retired", "An older phrasing of a since-split card"),
              ("sep",),
              ("moved", "A card whose deck was reorganized", "Regional Basics")]
    sources = {"Example Deck": _fixture_image_apkg()} if opts.get("image") else {}
    # What is left above the list now that the per-deck summary is the list's own first
    # section: update_decks()'s fixed notes about the run as a whole.
    top_html = ("<i>This looks like a one-time catch-up, likely your first update in a "
                "while. Future updates should be much shorter.</i>")
    safety = ("This is a preview: nothing above has been applied yet. Your review "
              "history and any personal notes on existing cards are kept (matched by "
              "card, not overwritten). A backup is taken automatically first.")
    flags = {}
    new_index = {d["guid"]: ("Example Deck", d["fields"][0][1]) for d in details}
    checkbox = ({"label": "Also apply the new card look (forces a one-time full "
                          "AnkiWeb sync)", "checked": False}
               if opts.get("checkbox") else None)

    def _flagged_line():
        return ""

    def _open():
        body, _boxes, flush = review.build_update_body(
            items, sources, flags, new_index, opts.get("feedback", False),
            top_html, _flagged_line, safety)
        _ask_with_widget(body, yes_label="Update", checkbox=checkbox)
        flush()

    return _open


def _scene_sync_confirm(mock, opts):
    """Sync decks' confirmation: one row per deck the source has an update for, its
    size in the trailing column and a chip for which of the two things that deck is.

    Built the way sync.py's sync_decks() builds it (review.build_list_body over
    widgets.simple_row rows, wrapped by ui._ask_with_widget), so the screen this
    renders is the one the Advanced menu opens rather than a mock-up of it. The deck
    names are invented, same as every other fixture here.
    """
    from internpearls import review, sync
    from internpearls.ui import _ask_with_widget
    items = [("header", "Update these decks?"),
             ("row", "changed", "Gadget Care", "6 cards"),
             ("sep",),
             ("row", "new", "Widget Basics", "4 cards"),
             ("sep",),
             # Long enough to wrap, so the render shows what a wrapped deck name does
             # to the chip beside it and to the count off to its right.
             ("row", "new", "A deck whose name runs long enough to wrap onto a second "
                            "line inside this dialog", "128 cards")]
    bottom = ("Your review history and any personal notes on existing cards are kept "
              "(matched by card, not overwritten). A backup is taken automatically "
              "first, so this is safe to undo if anything looks wrong afterward.")
    return lambda: _ask_with_widget(
        review.build_list_body(items, bottom_html=bottom),
        yes_label="Update", min_height=sync._CONFIRM_HEIGHT)


def _scene_reconcile_confirm(mock, opts):
    """Reconcile my decks' confirmation: each of the three things it can find, each
    group led by the sentence explaining it and followed by its own rows.

    All three groups show at once, which is the widest this screen ever gets; a real
    run usually finds one of them. The reworded pair keeps both wordings in the row's
    own primary line rather than splitting across the trailing column, since a card
    front is long enough to wrap and that column is not.

    Content is invented, same as every other fixture here.
    """
    from internpearls import review, sync
    from internpearls.palette import colors
    from internpearls.ui import _ask_with_widget
    muted = colors()["muted"]
    items = [
        ("note", "<b>2 retired cards</b> are still in your collection: split or "
                 "reworded since, with the replacements already added separately, so "
                 "these just duplicate your reviews now."),
        ("row", "retired", "An older phrasing of a since-split card", "Gadget Care"),
        ("sep",),
        ("row", "retired", "A card the source has since replaced with two",
         "Widget Basics"),
        ("note", "<b>1 card</b> is in your collection twice, in an older and a newer "
                 "wording of the same question, because the wording changed after your "
                 "first import. Your progress on the older copy moves to the newer "
                 "one, then the older copy is archived."),
        ("row", "retired", "An older wording of a question "
                           f"<span style='color:{muted};'>→ The wording it has "
                           "now, long enough to wrap</span>", ""),
        ("note", "<b>1 card</b> belongs to a deck that's since been reorganized."),
        ("row", "moved", "A card whose deck was reorganized", "→ Regional Basics"),
    ]
    top = ("<i>This looks like a one-time catch-up, likely your first Reconcile since "
           "a larger update. Future runs should be much shorter.</i>")
    bottom = ("Nothing is deleted. Archived cards keep their review history and can be "
              "brought back anytime by unsuspending them or moving them out of the "
              "Retired deck, and any personal notes on them carry over to the "
              "replacement first. A backup is taken automatically before anything "
              "changes.")
    return lambda: _ask_with_widget(
        review.build_list_body(items, top_html=top, bottom_html=bottom),
        yes_label="Archive and relocate", min_height=sync._CONFIRM_HEIGHT)


def _scene_result(mock, opts):
    """The end of a run: completion summary and flagged-card digest in one dialog.

    The summary reads in the same title/row vocabulary as the confirmation it
    follows: a title, then one widgets.simple_row per result line, built the same way
    sync.py's update_decks() builds them (see its `_finish` calls), rather than one
    HTML blob with a `<ul>` inside it.
    """
    from internpearls import review
    mock.mw._config = {"collect_card_feedback": True}
    entries = [{"deck": "Example Deck", "guid": "g1",
                "front": "Which widget is this, in one short line?",
                "note": "reads as two facts at once"}]
    title = "Update complete (source: example-decks)"
    rows = ["Example Deck: 29 kept, 3 new", "Archived <b>2 retired cards</b>"]
    footer = "A backup of the deck was saved before anything changed."
    return lambda: review.show_result_with_feedback(title, rows, footer, entries)


SCENES = {
    "digest": (_scene_digest, "the flagged-card feedback digest"),
    "settings": (_scene_settings, "the Settings dialog"),
    "manage-decks": (_scene_manage_decks,
                     "the deck manager (decks_dir for a source, empty for no decks)"),
    "about": (_scene_about, "the About dialog"),
    "configure-source": (_scene_configure_source, "the deck-source choice screen"),
    "confirm": (_scene_confirm, "the Update my decks confirmation (inline card list)"),
    "ask-scrollable": (_scene_ask_scrollable, "a plain _ask_scrollable confirmation"),
    "sync-confirm": (_scene_sync_confirm, "Sync decks' confirmation (one row per deck)"),
    "reconcile-confirm": (_scene_reconcile_confirm,
                          "Reconcile my decks' confirmation (retired, reworded, moved)"),
    "result": (_scene_result, "the end-of-run summary and feedback digest, one dialog"),
}


def render(scene, theme="light", expand=(), size=(640, 560), **opts):
    """Build a scene's dialog, show it offscreen, expand any requested rows, grab it.

    Returns a Shot. The live dialog rides along on the Shot because geometry questions
    (mapTo, isVisible, sizeHint) need the widget, not just the image.

    QDialog.exec is patched for the duration rather than permanently: a scene that
    opens a nested dialog should still block on it the normal way.
    """
    mock, q = bootstrap()
    a = app()
    apply_theme(theme)
    if scene not in SCENES:
        raise KeyError(f"unknown scene {scene!r}; known: {sorted(SCENES)}")
    # Each scene starts from an empty config so it cannot inherit a key a prior render
    # left on this shared mock. config._cfg() defaults every key, so empty is safe; the
    # scenes that need config set it in their builder below.
    mock.mw._config = {}
    opener = SCENES[scene][0](mock, opts)

    shots = []

    def fake_exec(self):
        self.resize(*size)
        self.show()
        a.processEvents()
        carets = [b for b in self.findChildren(q.QPushButton)
                  if b.text() in (CARET_CLOSED, CARET_OPEN)]
        for i in expand:
            if i < len(carets):
                carets[i].click()
        a.processEvents()
        # A dialog forced below its sizeHint clips content that fits at its natural
        # size, which is a harness artifact rather than a real add-on layout bug, so
        # the requested size is a floor, not a fixed size.
        hint = self.sizeHint()
        grown_w = max(self.width(), hint.width())
        grown_h = max(self.height(), hint.height())
        if grown_w != self.width() or grown_h != self.height():
            self.resize(grown_w, grown_h)
            a.processEvents()
        shots.append(Shot(self.grab().toImage(), self, theme, scene))
        return 1

    original = q.QDialog.exec
    q.QDialog.exec = fake_exec
    try:
        opener()
    finally:
        q.QDialog.exec = original

    if not shots:
        raise RuntimeError(
            f"scene {scene!r} opened no dialog (it may have returned early)")
    return shots[0]
