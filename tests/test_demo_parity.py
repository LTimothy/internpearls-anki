"""Keeps the GitHub Pages live demo generated from the add-on, never drifting.

The demo (docs/index.html + docs/demo_harness.py) doesn't re-implement the
add-on: it executes the real modules under Pyodide against the same mock-Anki
harness pytest uses. Menu structure, dialog layout and wording, version, and
behavior therefore all come from the code itself at runtime. The only thing
that CAN drift is the mirrored copy of the source that the static site serves —
build.sh refreshes docs/addon/ on every build, and this test fails if a copy
is stale, so shipping a code change without re-mirroring is impossible.

Card content isn't duplicated either: the demo downloads the example deck
repo's real manifest and .apkg files at load.
"""
import os

HERE = os.path.dirname(__file__)
ADDON = os.path.join(HERE, "..", "internpearls")
DOCS_ADDON = os.path.join(HERE, "..", "docs", "addon")


def _read(path):
    with open(path, "rb") as fh:
        return fh.read()


def test_docs_addon_mirror_is_current():
    expected = {f: _read(os.path.join(ADDON, f))
                for f in os.listdir(ADDON) if f.endswith(".py")}
    expected["mock_anki.py"] = _read(os.path.join(HERE, "mock_anki.py"))
    stale = []
    for name, want in sorted(expected.items()):
        mirrored = os.path.join(DOCS_ADDON, name)
        if not os.path.exists(mirrored):
            stale.append(f"missing: docs/addon/{name}")
        elif _read(mirrored) != want:
            stale.append(f"stale: docs/addon/{name}")
    extras = [f for f in os.listdir(DOCS_ADDON) if f.endswith(".py")
              and f not in expected]
    stale += [f"orphaned: docs/addon/{f}" for f in extras]
    assert not stale, ("the live demo's mirrored source is out of date — run "
                       "./build.sh to refresh docs/addon/:\n  " +
                       "\n  ".join(stale))
