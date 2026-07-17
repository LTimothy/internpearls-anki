# Real-Qt render tests

These render the add-on's real dialogs with real PyQt6 and assert on what gets painted.
`tests/` does the opposite: it runs a fake Qt and asserts on structure. Both are needed,
and they cannot share a process.

## Running them

    python3 -m venv .venv-qt
    .venv-qt/bin/pip install PyQt6 pytest
    QT_QPA_PLATFORM=offscreen .venv-qt/bin/python -m pytest qt_tests/ -q

No display and no Anki install are needed. `QT_QPA_PLATFORM=offscreen` is what makes
that true; without it Qt tries to open a window.

Anki's bundled interpreter has PyQt6 but no pytest, and installing pytest into Anki's
own runtime is not something this repo does. A plain pip venv gives both, and it is the
same PyQt6.

## Why a separate directory and a separate invocation

Every internpearls module binds its Qt names at import time (`from aqt.qt import
QLabel`). Whichever Qt is installed into `aqt.qt` first therefore wins for the whole
process, and swapping it afterward does not reach modules that already imported. So if
these tests were collected alongside `tests/`, one of the two suites would silently
assert against the wrong widgets and still pass.

`pytest.ini` pins `testpaths = tests`, so a bare `pytest` runs only the mock suite and
this one is opt-in by path. `harness.bootstrap()` raises if internpearls was imported
before it ran, or if aqt.qt already holds mock widgets, which turns that mistake into
a loud failure rather than a quiet one. `addon/conftest.py` adds a second guard that
rejects any command-line invocation naming both tests/ and qt_tests/.

Known limitation: both guards read command-line arguments, so a programmatic
`pytest.main(['tests', 'qt_tests'])` call, which never puts those paths in argv, can
still slip past them. That path can end the process with a native crash rather than
a clean error message.

## What belongs here

Anything that can only be answered by looking at pixels or real font metrics: does this
rule paint, does this text contrast its background, does this row align, does this label
fit. Anything answerable from structure belongs in `tests/`, which is faster and needs
no PyQt6.

Assertions must hold on both macOS (Qt 6.9, via Anki) and ubuntu CI (Qt 6.10, via pip).
Fonts differ between them, so assert presence and relationships, never magnitudes.
