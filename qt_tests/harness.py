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
_ORIG_LOAD_USER_SKILL = None


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

    Row 1 also carries `change_notes`, so a render exercises the because line under an
    UPDATED row too, and both rows carry a `card_source`, which sits above it and is
    the one line that shows on a NEW row as well.

    Row 1's Back `was` is plain prose sharing words with its current value, so a
    render paints the What changed group's word-diff line (struck removals,
    highlighted additions), while its Why `was` shares almost nothing and paints the
    rewrite receipt line with its Show yours link; row 3's cloze `was` differs only
    in its deletions, so a render paints the named-blank line instead.
    """
    return [
        {"guid": "g1", "notetype": "Study Deck - Basic", "kind": "new",
         "card_source": "[T10Q2]",
         "fields": [("Front", "Which widget is this, in one short line?"),
                    ("Back", "A basic note with a tag."),
                    ("Why", "Short rows are the common case."),
                    ("Image", ""), ("Tag", "Widgets"), ("Dosing", ""),
                    ("Notes", "")]},
        {"guid": "g2", "notetype": "Study Deck - Basic", "kind": "changed",
         "was": {"Back": "A wrapping basic note carrying extra dosing.",
                 "Why": "An earlier explanation of the wrap point, replaced "
                        "wholesale in this update round."},
         "card_source": "[T10Q2] [T4Q11]",
         "change_notes": [{"kind": "feedback", "note": "an example reviewer request",
                           "hash": "0" * 16}],
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
        {"guid": "g4", "notetype": "Study Deck - Cloze", "kind": "changed",
         "was": {"Text": "A cloze note fills {{c1::one}} deletion, and "
                         "{{c2::another}} {{c3::one}}, in the deck's own blue."},
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


def _scene_night_mode_dimming(mock, opts):
    from internpearls import dialogs
    if "percent" in opts:
        mock.mw._config = {"dim_images_night_mode": True,
                           "dim_images_night_mode_percent": opts["percent"]}
    if "scope" in opts:
        mock.mw._config["dim_night_mode_scope"] = opts["scope"]
    return dialogs.open_night_mode_dimming


_MANAGE_DECKS_FIXTURE = None


def _manage_decks_fixture(mock):
    """A manifest with one deck per sync state (new, update, current), plus the
    matching installed state file and collection notes, so all three states actually
    paint. Without this the scene's deck list was empty and the rows that read
    _STATE_CHIP were never measured by the render suite at all. Deck names are
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
    """A plain _ask_scrollable confirmation. About is the only caller left in the
    add-on: every screen that lists cards or decks builds rows instead. Kept as its own
    scene, and with content of its own rather than About's, so this shared wrapper's own
    behavior, the scroll area, the top-alignment, the closed-by-default external links,
    stays under render test regardless of what any one caller currently does.
    """
    from internpearls.ui import _ask_scrollable
    body = (
        "<b>Example Deck</b><br>"
        + "<br>".join(d["fields"][0][1] for d in synthetic_details()[:3])
        + "<p>Nothing is added until you choose Continue.</p>")
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
    if opts.get("declined"):
        # A re-offered decline, changed since it was turned away (row 0), beside an
        # ordinary new row carrying the same three-option control (row 2). The two
        # differ only by the decline, which is what lets a test read what the decline
        # itself costs the card's own words. Row 1 is the changed kind, declined the
        # way a changed card can be.
        details[0] = dict(details[0], declined_state="skip", changed_since_decline=True)
        details[1] = dict(details[1], declined_state="keep")
        details[2] = dict(details[2], kind="new")
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
    if opts.get("checkbox"):
        # The two paragraphs update_decks writes above the list when a run carries both
        # kinds of schema change, in its own order: the look sentence goes last because
        # the checkbox lands directly under whatever ends this text (see
        # ui._place_checkbox), and a box sitting under the format paragraph reads as
        # answering a question that has its own dialog.
        top_html += (
            "<br><br><b>4 cards</b> in this update changed format (a question and "
            "answer became a fill-in-the-blank). You'll be asked once, before anything "
            "imports, whether to move your existing cards across."
            "<br><br>This update also changes how some cards look (template or "
            "styling) for: <b>Example Note Type</b>. Your review history and card "
            "content are unaffected either way.")
    # The production string itself, not a shortened stand-in: the paint and layout
    # tests measure how this label wraps, so a paraphrase would prove the wrong length.
    from internpearls.sync import _UPDATE_SAFETY_NOTE as safety
    flags = {}
    new_index = {d["guid"]: ("Example Deck", d["fields"][0][1]) for d in details}
    decisions = {}
    checkbox = ({"label": "Also apply the new card look (forces a one-time full "
                          "AnkiWeb sync)", "checked": False}
               if opts.get("checkbox") else None)

    def _status_line():
        return ""

    def _open():
        body, _boxes, flush = review.build_update_body(
            items, sources, flags, new_index, decisions,
            top_html, _status_line, safety)
        # min_width and open_size match update_decks()'s own call: the wider floor is
        # what leaves a card's own text room beside its decision control. render()'s
        # fake exec re-sizes to each test's requested size afterwards, so open_size
        # here only proves the call accepts it, not what any test measures at.
        _ask_with_widget(body, yes_label="Update", checkbox=checkbox, min_width=660,
                         open_size=(880, 800))
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
    from internpearls import review
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
        yes_label="Update", min_height=review._CONFIRM_HEIGHT)


def _scene_reconcile_confirm(mock, opts):
    """Reconcile my decks' confirmation: each of the three things it can find, each
    group led by the sentence explaining it and followed by its own rows.

    All three groups show at once, which is the widest this screen ever gets; a real
    run usually finds one of them. The reworded pair keeps both wordings in the row's
    own primary line rather than splitting across the trailing column, since a card
    front is long enough to wrap and that column is not.

    Content is invented, same as every other fixture here.
    """
    from internpearls import review
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
        yes_label="Archive and relocate", min_height=review._CONFIRM_HEIGHT)


def _scene_result(mock, opts):
    """The end of a run: completion summary and flagged-card digest in one dialog.

    The summary reads in the same title/row vocabulary as the confirmation it
    follows: a title, then one row per result line and a paragraph for each note about
    the run as a whole, built the same way sync.py's update_decks() builds them (see
    its `_finish` calls), rather than one HTML blob with a `<ul>` inside it.
    """
    from internpearls import review
    entries = [{"deck": "Example Deck", "guid": "g1",
                "front": "Which widget is this, in one short line?",
                "note": "reads as two facts at once"}]
    title = "Update complete (source: example-decks)"
    items = [("row", None, "Example Deck: 29 kept, 3 new", ""),
             ("sep",),
             ("row", None, "Archived <b>2 retired cards</b>", ""),
             ("note", "A backup of the deck was saved before anything changed.")]
    return lambda: review.show_result_with_feedback(title, items, entries)


def _scene_result_only(mock, opts):
    """The same end of a run with nothing flagged: the summary on its own, in the
    dialog review.show_result opens for it. This is what Sync decks and Update my decks
    both finish on for a routine run, so the collision note and the cards it names ride
    along here too, since that is the longest this screen ever gets.
    """
    from internpearls import review
    items = [("row", None, "Example Deck: 29 kept, 3 new", ""),
             ("sep",),
             ("row", None, "Gadget Care: 6 kept, 0 new", ""),
             ("note", "Preserved fields restored on 12 cards."),
             ("note", "On <b>2 cards</b>, this update changed a field you had also "
                      "written in yourself. <b>Your version was kept</b> and the update "
                      "to that field was skipped, so nothing you wrote was lost."),
             ("row", None, "Which widget is this, in one short line? (Notes)", ""),
             ("sep",),
             ("row", None, "An untagged row, to check the left edge lines up? (Notes)",
              ""),
             ("note", "A backup of the deck was saved before anything changed.")]
    return lambda: review.show_result("Sync complete (source: example-decks)", items)


def _scene_scope_offer(mock, opts):
    """The settings a deck source recommends, offered right after it is configured.

    Two rows of the same kind, so nothing is chipped and the columns are declined,
    same as the two confirmations below. Runs the real _offer_manifest_scope against an
    invented manifest; render()'s patched exec answers it, which writes the mock's own
    config and touches nothing else.
    """
    from internpearls import dialogs
    manifest = {"scope_tag": "ExampleDeck", "export_deck": "Example::Custom"}
    return lambda: dialogs._offer_manifest_scope(manifest)


def _scene_duplicates_confirm(mock, opts):
    """Clean up duplicates' confirmation: one row per card that arrived twice, each
    naming which copy is kept and which is archived.

    Every row is the same thing happening to the same kind of card, so nothing here is
    chipped and the rows decline the caret and chip columns: they start flush with the
    heading above them. Built through logic.duplicate_dialog_rows and
    review.build_list_body exactly as sync.py's clean_up_duplicates() builds it.
    Content is invented, same as every other fixture here.
    """
    from internpearls import review
    from internpearls.logic import duplicate_dialog_rows
    from internpearls.palette import colors
    from internpearls.ui import _ask_with_widget

    def group(label, keep_deck, arch_deck, keep_reps, arch_reps):
        return {"model": "M", "front": label,
                "keep": {"guid": "k", "label": label, "deck": keep_deck,
                         "reps": keep_reps},
                "archive": [{"guid": "a", "label": label, "deck": arch_deck,
                             "reps": arch_reps}]}

    heading, rows = duplicate_dialog_rows([
        group("Which widget is this, in one short line?",
              "Example::Widget Basics", "Example::Widget Basics", 7, 2),
        # Long enough to wrap, so the render shows where the muted detail lands once
        # the card's own name has taken more than one line.
        group("A deliberately long prompt, written to run past the dialog's width so "
              "the wrap lands under the text and not under the caret",
              "Example::Gadget Care", "Example::Gizmo Repair", 5, 0)])
    muted = colors()["muted"]
    items = []
    review.append_rows(items, [
        ("row", None,
         f"{r['label']} <span style='color:{muted};'>{r['detail']}</span>", "")
        for r in rows])
    bottom = ("Nothing is deleted. Archived cards keep their review history and can be "
              "brought back anytime by unsuspending them or moving them out of the "
              "Retired deck, and any personal notes on them carry over to the kept copy "
              "first. A backup is taken automatically before anything changes.")
    return lambda: _ask_with_widget(
        review.build_list_body(items, top_html=heading, bottom_html=bottom,
                               card_columns=False),
        yes_label="Archive duplicates", min_height=review._CONFIRM_HEIGHT)


def _scene_empty_cards_confirm(mock, opts):
    """Remove empty cards' confirmation: one row per note carrying a card with nothing
    left to render, the deletion numbers that went missing in the trailing column.

    Single-kind like the duplicates screen above, and declining the same two columns
    for the same reason. Built through logic.empty_cards_dialog_rows and
    review.build_list_body exactly as collection.py's remove_empty_cards() builds it.
    """
    from internpearls import review
    from internpearls.logic import empty_cards_dialog_rows
    from internpearls.ui import _ask_with_widget
    heading, lines, tail = empty_cards_dialog_rows([
        {"nid": 1, "card_ids": [11, 12], "ords": [3, 4],
         "label": "A cloze note fills one deletion, and another one"},
        {"nid": 2, "card_ids": [13], "ords": [5],
         "label": "A deliberately long prompt, written to run past the dialog's width "
                  "so the wrap lands under the text and not under the caret"}],
        skipped=1)
    items = []
    review.append_rows(items,
                       [("row", None, line["label"], line["gone"]) for line in lines])
    safety = ("Only the empty cards are removed; the notes themselves, and every card "
              "that still shows something, are left exactly as they are. A backup is "
              "taken automatically before anything changes.")
    return lambda: _ask_with_widget(
        review.build_list_body(items, top_html=heading, card_columns=False,
                               bottom_html="<br><br>".join([tail, safety])),
        yes_label="Remove 3 cards", min_height=review._CONFIRM_HEIGHT)


_DECLINED_FIXTURE = None


def _declined_fixture():
    """A declined.json with one entry per state, so all three group headings and their
    Offer again buttons actually render. Built once and cached, same lifetime as the
    other file-backed fixtures in this module.
    """
    global _DECLINED_FIXTURE
    if _DECLINED_FIXTURE is not None:
        return _DECLINED_FIXTURE
    import atexit
    import json
    import tempfile

    tmpdir = tempfile.TemporaryDirectory(prefix="internpearls_qt_declined_")
    atexit.register(tmpdir.cleanup)
    path = os.path.join(tmpdir.name, "declined.json")
    with open(path, "w", encoding="utf8") as fh:
        json.dump({
            "g1": {"state": "never", "front": "Which widget is this, in one short line?",
                   "deck": "Example Deck", "decided": "2026-08-01", "hash": ""},
            "g2": {"state": "skip",
                   "front": "A deliberately long prompt, written to run past the "
                            "dialog's width so the wrap lands under the text",
                   "deck": "Example Deck", "decided": "2026-08-02", "hash": ""},
            "g3": {"state": "keep", "front": "A card whose deck was reorganized",
                   "deck": "Example Deck", "decided": "2026-08-03", "hash": ""},
        }, fh)
    _DECLINED_FIXTURE = path
    return path


_DECLINED_SINGLE_FIXTURE = None


def _declined_single_fixture():
    """One declined entry, in its own dedicated file separate from _declined_fixture()
    above. test_declined.py's interaction test clicks its Offer again button for real,
    which writes the registry back to disk; that write must never land on the shared
    three-entry fixture the render-only tests depend on staying exactly as seeded.
    """
    global _DECLINED_SINGLE_FIXTURE
    if _DECLINED_SINGLE_FIXTURE is not None:
        return _DECLINED_SINGLE_FIXTURE
    import atexit
    import json
    import tempfile

    tmpdir = tempfile.TemporaryDirectory(prefix="internpearls_qt_declined_single_")
    atexit.register(tmpdir.cleanup)
    path = os.path.join(tmpdir.name, "declined.json")
    with open(path, "w", encoding="utf8") as fh:
        json.dump({
            "g1": {"state": "never", "front": "Which widget is this, in one short line?",
                   "deck": "Example Deck", "decided": "2026-08-01", "hash": ""},
        }, fh)
    _DECLINED_SINGLE_FIXTURE = path
    return path


def _scene_declined(mock, opts):
    from internpearls import config, dialogs
    config.DECLINED = (_declined_single_fixture() if opts.get("single")
                       else _declined_fixture())
    return dialogs.open_declined_cards


def _ai_backend_available(kind_available="claude"):
    """Patch ai_cli detection so the wizard's __init__ (which runs _detect
    synchronously) lands on the requested page without a real CLI on disk.
    Set directly on the module rather than through pytest's monkeypatch,
    which isn't available here (this is a render tool, not a test); every
    scene that needs a particular state sets these explicitly, so no scene
    depends on what an earlier one left behind.
    """
    from internpearls import ai_cli

    def find_cli(kind, override=""):
        return "/usr/bin/true" if kind == kind_available else None
    ai_cli.find_cli = find_cli
    ai_cli.probe = lambda kind, path: {"ok": True, "detail": "1.0.0"}


def _scene_ai_setup(mock, opts):
    """The wizard's setup page: no backend detected, so every row reads "not
    found" and the page never advances past itself."""
    from internpearls import ai_cli, ai_dialog

    def _find_none(kind, override=""):
        return None
    ai_cli.find_cli = _find_none

    def _open():
        dlg = ai_dialog._GenerateDialog()
        dlg.exec()
    return _open


def _scene_ai_backends(mock, opts):
    """The AI Backends window on its own, reached directly rather than through
    the wizard's own Setup link: one row per assistant, then the
    settings panel for the preferred one.

    `found=1` detects every backend, which is the taller of the two states the
    height budget is measured against (every row wears a FOUND chip and the
    panel is about a working CLI); the default detects none, which is the state
    a reader with nothing installed opens onto."""
    from internpearls import ai_cli, ai_setup

    if opts.get("found"):
        ai_cli.find_cli = lambda kind, override="": "/usr/bin/true"
        ai_cli.probe = lambda kind, path: {"ok": True, "detail": "1.0.0"}
    else:
        def _find_none(kind, override=""):
            return None
        ai_cli.find_cli = _find_none

    return lambda: ai_setup.open_ai_backends(None)


def _scene_ai_input(mock, opts):
    """The wizard's input page, in one of the three states the mockup draws.

    `state` picks which: "ready" (the default: a backend detected and material
    pasted, so every row reads its settled answer), "unset" (nothing detected,
    so the backend row wears NOT SET UP: the page is forced into view here,
    since the wizard's own _detect() would land on the setup page instead), and
    "advanced" (ready, with the Advanced panel disclosed in place).
    """
    from internpearls import ai_cli, ai_dialog
    state = opts.get("state", "ready")
    if state == "unset":
        def _find_none(kind, override=""):
            return None
        ai_cli.find_cli = _find_none
    else:
        _ai_backend_available()

    def _open():
        dlg = ai_dialog._GenerateDialog()
        if state == "unset":
            dlg.stack.setCurrentWidget(dlg.input_page)
        else:
            dlg.source_box.setPlainText(SAMPLE_SOURCE)
        if state == "advanced":
            dlg._toggle_advanced()
        dlg.exec()
    return _open


# Long enough to sit over ai_logic.AUTO_DEPTH_CHARS, so the "ready" scene shows
# the settled THOROUGH answer rather than the undecided one.
SAMPLE_SOURCE = ("Neostigmine reverses non-depolarizing neuromuscular blockade by "
                 "inhibiting acetylcholinesterase. ") * 30


def _scene_ai_progress(mock, opts):
    """The wizard's progress page, mid-run: the one status row a live
    generation leaves it in (chip, bold phase, muted phase/elapsed line,
    trailing Cancel), without actually running a background worker (there
    is nothing here to poll). opts["revision"] renders the revising-a-
    draft form, where the row's own session already holds the draft being
    revised; without it this is a first-run scene, so session.cards stays
    empty the way a real fresh run's does. A few sample activity lines
    always fill the feed below the row (a live run leaves it non-empty
    almost immediately), without running an actual worker."""
    from internpearls import ai_dialog
    _ai_backend_available()

    def _open():
        dlg = ai_dialog._GenerateDialog()
        if opts.get("revision"):
            dlg.session.cards = _ai_synthetic_cards(11)
        dlg.progress_row.set_chip("verifying")
        dlg.progress_row.set_primary("<b>Verifying doses against sources</b>")
        dlg.progress_row.set_detail(
            (("Revising 11 cards. " if opts.get("revision") else "")
            + "48s elapsed, your recent Thorough runs averaged 1m 40s."))
        for line in ("12s  Listed the scratch folder",
                    "34s  Viewed _thumb-0-0.svg",
                    "41s  Searched the web",
                    "1m 12s  Verifying doses against sources"):
            dlg.activity_feed.appendPlainText(line)
        dlg.stack.setCurrentWidget(dlg.progress_page)
        dlg.exec()
    return _open


def _ai_synthetic_cards(n):
    return [{
        "note_type": "Study Deck - Basic",
        "fields": {"Front": f"What is fact {i + 1} about local anesthetic "
                            "systemic toxicity?",
                  "Back": "A structured answer explaining the mechanism.",
                  "Why": "Because the mechanism drives how it's managed.",
                  "Dosing": "", "Notes": ""},
        "tags": [], "images": [], "rationale": "",
    } for i in range(n)]


def _scene_ai_review(mock, opts):
    """The wizard's review page, with a drafted set that exercises both a
    block-level check (a duplicate) and a warn-level one (a long answer), so
    chips and reasons have something to render. `count` (default 4) lets a
    caller ask for a full 50-card draft to prove the list stays reachable."""
    from internpearls import ai_dialog, ai_logic
    _ai_backend_available()
    n = opts.get("count", 4)

    def _open():
        dlg = ai_dialog._GenerateDialog()
        s = dlg.session
        cards = _ai_synthetic_cards(n)
        s.cards = cards
        s.included = [True] * n
        s.notes = {1: "make this shorter"} if n > 1 else {}
        s.updated = {2} if n > 2 else set()
        s.checks = ai_logic.mechanical_checks(cards, {}, {})
        s.checks[0] = [{"code": "duplicate", "level": "block",
                        "existing": cards[0]["fields"]["Front"],
                        "message": "possible duplicate of an existing card"}]
        if n > 1:
            s.checks[1] = [{"code": "long-answer", "level": "warn",
                            "message": "answer is long; consider trimming"}]
        s.image_data = {}
        s.tokens_last_run = 12345
        dlg._rebuild_review()
        dlg.stack.setCurrentWidget(dlg.review_page)
        dlg.exec()
    return _open


def _scene_ai_view_skills(mock, opts):
    """View skills, no deck skill installed: every install whose source
    hasn't shipped a deck_skill.json, which is the common case. Renders the
    real bundled skill text (48 lines of prose), not a placeholder: that's
    what used to outgrow an 891px screen through a bare QMessageBox with no
    scroll area, leaving its own Close button unreachable. Never calls
    dlg.exec() on the wizard itself, only on the nested confirmation
    _view_skills() opens, which is the one this scene exists to measure.

    `user_rules`, when passed, stands in for the learner's saved My rules
    text (ai_dialog's own bound load_user_skill swapped to return it) so the
    My rules section of the body can be exercised without touching disk. Set
    unconditionally on every render (never left dangling from a prior scene's
    opts) so an omitted `user_rules` always renders the true empty state.
    """
    from internpearls import ai_dialog
    _ai_backend_available()

    global _ORIG_LOAD_USER_SKILL
    if _ORIG_LOAD_USER_SKILL is None:
        _ORIG_LOAD_USER_SKILL = ai_dialog.load_user_skill
    text = opts.get("user_rules", "")
    ai_dialog.load_user_skill = (lambda: text) if text else _ORIG_LOAD_USER_SKILL

    def _open():
        dlg = ai_dialog._GenerateDialog()
        dlg._view_skills()
    return _open


def _scene_ai_my_rules(mock, opts):
    """The My rules editor on its own, reached directly rather than through
    the wizard's Edit my rules link."""
    from internpearls import ai_dialog
    _ai_backend_available()

    def _open():
        dlg = ai_dialog._UserSkillDialog(mock.mw)
        dlg.exec()
    return _open


SCENES = {
    "digest": (_scene_digest, "the flagged-card feedback digest"),
    "settings": (_scene_settings, "the Settings dialog"),
    "night-mode-dimming": (_scene_night_mode_dimming,
                           "the Experimental > Night mode dimming dialog"),
    "manage-decks": (_scene_manage_decks,
                     "the deck manager (decks_dir for a source, empty for no decks)"),
    "about": (_scene_about, "the About dialog"),
    "configure-source": (_scene_configure_source, "the deck-source choice screen"),
    "confirm": (_scene_confirm, "the Update my decks confirmation (inline card list)"),
    "ask-scrollable": (_scene_ask_scrollable, "a plain _ask_scrollable confirmation"),
    "sync-confirm": (_scene_sync_confirm, "Sync decks' confirmation (one row per deck)"),
    "reconcile-confirm": (_scene_reconcile_confirm,
                          "Reconcile my decks' confirmation (retired, reworded, moved)"),
    "scope-offer": (_scene_scope_offer,
                    "the settings a deck source recommends, offered on configure"),
    "duplicates-confirm": (_scene_duplicates_confirm,
                           "Clean up duplicates' confirmation (one row per card)"),
    "empty-cards-confirm": (_scene_empty_cards_confirm,
                            "Remove empty cards' confirmation (one row per note)"),
    "result": (_scene_result, "the end-of-run summary and feedback digest, one dialog"),
    "result-only": (_scene_result_only, "the end-of-run summary with nothing flagged"),
    "declined": (_scene_declined,
                "the Declined cards dialog (one entry per group, Offer again)"),
    "ai-setup": (_scene_ai_setup,
                "the AI wizard's setup page (no backend detected)"),
    "ai-backends": (_scene_ai_backends,
                    "the AI Backends window (one row per backend, found=1 detects "
                    "all three)"),
    "ai-input": (_scene_ai_input,
                 "the AI wizard's input page (state=unset|ready|advanced)"),
    "ai-progress": (_scene_ai_progress, "the AI wizard's progress page, mid-run"),
    "ai-review": (_scene_ai_review,
                 "the AI wizard's review page (count=N for a full-size draft)"),
    "ai-view-skills": (_scene_ai_view_skills,
                       "View skills with no deck skill installed (real bundled text)"),
    "ai-my-rules": (_scene_ai_my_rules, "the My rules editor"),
}


def render(scene, theme="light", expand=(), size=(640, 560), click_labels=(), **opts):
    """Build a scene's dialog, show it offscreen, expand any requested rows, grab it.

    Returns a Shot. The live dialog rides along on the Shot because geometry questions
    (mapTo, isVisible, sizeHint) need the widget, not just the image.

    `click_labels` clicks every QPushButton whose text exactly matches one of these
    strings, in order, after rows are expanded: a decision_cell's "Skip" or "Never", or
    a row's "Add note" link, so a real-Qt test can exercise what a decision actually
    shows rather than only what the mock's structure records.

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
        for label in click_labels:
            button = next((b for b in self.findChildren(q.QPushButton)
                          if b.text() == label), None)
            if button is not None:
                button.click()
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
        # Every wrapper releases its dialog once exec() returns, which is the whole
        # point in Anki and fatal here: the Shot outlives this call and the tests read
        # the live widget tree off it, so the next processEvents() would free the object
        # they are still asking questions of. Neutralised per instance, after the grab.
        self.deleteLater = lambda: None
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


# ------------------------------------------------------------------- readers
def plain(text):
    """A rich-text label's words with its markup taken out, so a test can ask what
    a row says rather than how it is marked up. Delegates to ai_dialog._plain
    rather than duplicating its regex, so the two definitions can't drift apart.
    Imported lazily, inside the call: internpearls modules cannot be imported
    before bootstrap() installs real Qt (see that function's own comment)."""
    from internpearls.ai_dialog import _plain
    return _plain(text)


def texts(dialog):
    """Every label's text on a dialog, markup stripped: what the page says."""
    _, q = bootstrap()
    return [plain(l.text()) for l in dialog.findChildren(q.QLabel)]


def link_labels(dialog):
    """The labels of every flat link_button on a dialog, in tree order."""
    _, q = bootstrap()
    return [b.text() for b in dialog.findChildren(q.QPushButton) if b.isFlat()]


def left_x(dialog, widget):
    """A widget's left edge in the dialog's own coordinate space."""
    _, q = bootstrap()
    return widget.mapTo(dialog, q.QPoint(0, 0)).x()


def visual_left(dialog, widget):
    """Where the running style actually draws `widget`'s own left edge, in the
    dialog's own coordinate space - independent of `widgets.align_field_column`'s
    own compensation, which this exists to check: grabs `dialog` itself and scans
    for the first run of pixels level with `widget`'s own middle that aren't the
    plain window background, starting a little left of `widget`'s own geometry
    (its compensating margin, if any, moved it right of the label column, not
    left, so nothing legitimate can be found further left than that) and never
    reading past `widget`'s own right edge, so a neighbouring control's paint
    can't be mistaken for this one's.

    Under Fusion, which is what running this suite (QT_QPA_PLATFORM=offscreen)
    always renders with, every control's bezel starts flush with its own
    geometry, so this equals `left_x` exactly and a test built on it degenerates
    to the plain geometry check. It only diverges under a style that insets some
    control classes more than others, which macOS's native style does: verified
    separately, with no QT_QPA_PLATFORM set, by a script that imports this
    module and calls render() directly (a real display connection is required;
    pytest here always runs offscreen).
    """
    from aqt.qt import QPalette
    gx = left_x(dialog, widget)
    pix = dialog.grab()
    img = pix.toImage()
    if img.isNull() or img.height() == 0:
        return gx
    dpr = img.devicePixelRatio() or 1
    gy = widget.mapTo(dialog, widget.rect().center()).y()
    row = max(0, min(round(gy * dpr), img.height() - 1))
    bg = dialog.palette().color(QPalette.ColorRole.Window)
    pad = 20
    start = max(0, round((gx - pad) * dpr))
    end = min(img.width(), round((gx + widget.width()) * dpr))
    run = 0
    for px in range(start, end):
        if img.pixelColor(px, row) != bg:
            run += 1
            if run >= 2:
                return round((px - 1) / dpr)
        else:
            run = 0
    return gx


def chip_text(row):
    """The word on a row's chip pill, or "" for a row wearing none."""
    _, q = bootstrap()
    labels = [l for l in row.findChildren(q.QLabel) if "border-radius" in l.styleSheet()]
    return labels[0].text() if labels else ""
