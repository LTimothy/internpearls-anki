# Real-Qt render tests

These tests render the add-on's real dialogs with real PyQt6 and check what
gets painted. `tests/` runs a mock Qt and checks structure. Both are needed,
and they cannot share a process.

## Running them

    python3 -m venv .venv-qt
    .venv-qt/bin/pip install PyQt6 pytest
    QT_QPA_PLATFORM=offscreen .venv-qt/bin/python -m pytest qt_tests/ -q

The offscreen platform means no display and no Anki install are needed.
Anki's own Python has PyQt6 but no pytest, so use a separate venv. It is the
same PyQt6.

## Why a separate command

Each add-on module binds its Qt names at import time (`from aqt.qt import
QLabel`), so the first Qt installed into `aqt.qt` wins for the whole process.
If both suites ran together, one would test the wrong widgets and still pass.

Three guards prevent that:

- `pytest.ini` sets `testpaths = tests`, so a bare `pytest` runs only the mock
  suite.
- `harness.bootstrap()` raises if the add-on was imported first, or if
  `aqt.qt` already holds mock widgets.
- `conftest.py` at the repo root exits if one command names both `tests/` and
  `qt_tests/`.

The root `conftest.py` reads `sys.argv`, so a programmatic
`pytest.main(['tests', 'qt_tests'])` gets past it and can end in a native
crash instead of a clean error.

## What belongs here

Questions only pixels or real font metrics can answer: does this rule paint,
does this text contrast with its background, does this row align, does this
label fit. Anything structure can answer belongs in `tests/`, which is faster.

Tests must pass on macOS and on the Ubuntu CI runner. Both install an unpinned
PyQt6 and have different fonts, so assert presence and relationships, never
exact sizes.
