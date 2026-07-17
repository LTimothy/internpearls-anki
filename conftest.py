"""Root conftest, kept deliberately empty of fixtures.

Its only job is the guard below. pytest loads conftest files by walking each
argument's directory chain from the repo root down to the leaf, so whichever
argument is processed first, this file is always imported before tests/conftest.py
or qt_tests/conftest.py. A pytest_sessionstart hook would be too late: all initial
conftests, including qt_tests/conftest.py's module-level harness.bootstrap() call,
finish loading before pytest_sessionstart ever fires. Plain module-level code here
is the only point left where a dual-path invocation is still recoverable, because
tests/ and qt_tests/ cannot share a process: both install a Qt into aqt.qt, and
whichever lands first wins for every internpearls import for the rest of the run.
"""
import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
_TESTS = os.path.join(_ROOT, "tests")
_QT_TESTS = os.path.join(_ROOT, "qt_tests")


def _under(path, directory):
    return path == directory or path.startswith(directory + os.sep)


def _requested_dirs():
    """Command-line arguments, resolved to absolute paths with any ::nodeid suffix
    stripped. Reads sys.argv directly rather than a Config object: no Config exists
    yet at conftest-import time, only argv."""
    dirs = set()
    for arg in sys.argv[1:]:
        if not arg or arg.startswith("-"):
            continue
        candidate = os.path.normpath(os.path.join(_ROOT, arg.split("::", 1)[0]))
        if _under(candidate, _TESTS):
            dirs.add(_TESTS)
        elif _under(candidate, _QT_TESTS):
            dirs.add(_QT_TESTS)
    return dirs


_requested = _requested_dirs()
if _TESTS in _requested and _QT_TESTS in _requested:
    sys.exit(
        "tests/ and qt_tests/ were both passed to one pytest invocation. They "
        "cannot share a process: both install a Qt into aqt.qt, and whichever "
        "loads first wins for every internpearls import for the rest of the run. "
        "Run them separately, for example:\n"
        "  python3 -m pytest tests/\n"
        "  QT_QPA_PLATFORM=offscreen .venv-qt/bin/python -m pytest qt_tests/"
    )
