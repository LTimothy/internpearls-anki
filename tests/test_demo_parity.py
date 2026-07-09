"""Keeps docs/index.html (the GitHub Pages live demo) in sync with the add-on.

The demo is a parallel JavaScript implementation of the add-on's user-visible
behavior, so it can silently drift when a dialog string, menu item, or version
changes in the Python source. These tests make drift a red test instead of an
afterthought:

- every menu item label is parsed out of __init__.py (not hand-listed here), so
  adding or renaming a menu item fails until the demo shows it too;
- ADDON_VERSION is read from config.py, so a release bump fails until the demo
  reports the same version;
- sentinel phrases from the sync/dialog flows — the wording the demo claims to
  mirror — are greppable in both the Python module that owns them and the demo.

Card *content* needs no test: the demo fetches it live from the example deck
repo at load (see fetchLiveContent in index.html).
"""
import os
import re

HERE = os.path.dirname(__file__)
ADDON = os.path.join(HERE, "..", "internpearls")
DEMO = os.path.join(HERE, "..", "docs", "index.html")


def _read(*path):
    with open(os.path.join(*path), encoding="utf8") as fh:
        return fh.read()


def test_demo_shows_every_menu_item():
    init = _read(ADDON, "__init__.py")
    labels = re.findall(r'add\((?:menu|adv), "([^"]+)"', init)
    assert len(labels) >= 12, "menu parsing broke — expected the full menu"
    demo = _read(DEMO)
    missing = [l for l in labels if l not in demo]
    assert not missing, f"menu items missing from docs/index.html: {missing}"


def test_demo_reports_the_current_addon_version():
    config = _read(ADDON, "config.py")
    version = re.search(r'ADDON_VERSION = "([^"]+)"', config).group(1)
    assert f'VERSION = "{version}"' in _read(DEMO), (
        f"docs/index.html must set VERSION = \"{version}\" to match config.py — "
        "update the demo when releasing")


# The demo's dialogs claim to mirror the add-on's wording. Each sentinel below is a
# phrase the demo reproduces, paired with the Python module that owns it, so a reword
# on either side fails until both match again.
SENTINELS = [
    ("sync.py", "Update these decks?"),
    ("sync.py", "A backup is taken automatically first"),
    ("sync.py", "kept history,"),
    ("sync.py", "Apply the new look now?"),
    ("sync.py", "one-time full sync"),
    ("sync.py", "All selected decks are up to date (source:"),
    ("background.py", "run Sync decks to review it"),
    ("dialogs.py", "Check the decks you want to keep synced"),
    ("dialogs.py", "Preserved fields"),
    ("dialogs.py", "Save and sync now"),
    ("dialogs.py", "Sync decks automatically when updates are available"),
    ("dialogs.py", "Notify me when a new add-on version is out"),
    ("dialogs.py", "Install add-on updates automatically"),
    ("dialogs.py", "Where should decks come from?"),
    ("dialogs.py", "Try the example deck"),
    # Sentinels must sit inside ONE string literal in the Python source (this test
    # greps source text, so a phrase split across adjacent literals won't match).
    ("collection.py", "Matching cards are updated in"),
]


def _normalize(s):
    return re.sub(r"\s+", " ", s)


def test_dialog_sentinels_exist_in_both_sides():
    demo = _normalize(_read(DEMO))
    problems = []
    for module, phrase in SENTINELS:
        want = _normalize(phrase)
        if want not in _normalize(_read(ADDON, module)):
            problems.append(f"{module} no longer contains: {phrase!r}")
        if want not in demo:
            problems.append(f"docs/index.html is missing ({module}): {phrase!r}")
    assert not problems, "demo/add-on wording drift:\n  " + "\n  ".join(problems)
