"""Bootstraps real PyQt6 before pytest imports any test module.

conftest.py is imported before the test modules beside it, which is exactly the hook
this needs: harness.bootstrap() has to run before the first `import internpearls.x`
anywhere in the process. See harness.py's module docstring for why that ordering is
not recoverable after the fact.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

pytest.importorskip("PyQt6", reason="the real-Qt suite needs PyQt6: see qt_tests/README.md")

import harness  # noqa: E402

harness.bootstrap()

_USER_FILES_DIR = Path(harness.ROOT) / "internpearls" / "user_files"


def _user_files_snapshot():
    return {str(path.relative_to(_USER_FILES_DIR)): path.stat().st_mtime_ns
            for path in _USER_FILES_DIR.rglob("*") if path.is_file()}


def pytest_sessionstart(session):
    session._ip_user_files_start = _user_files_snapshot()


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    before = session._ip_user_files_start
    after = _user_files_snapshot()
    if after != before:
        changed = sorted(name for name in before.keys() | after.keys()
                         if before.get(name) != after.get(name))
        reporter = session.config.pluginmanager.getplugin("terminalreporter")
        if reporter is not None:
            reporter.write_sep("=", "internpearls/user_files changed: " + ", ".join(changed))
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture(scope="session")
def shot():
    """Render a scene once per (scene, theme, expand, size) and cache it.

    A render costs roughly half a second. Re-rendering per assertion would make the
    suite slow enough that people stop running it, which is the only way it fails.
    """
    cache = {}

    def _shot(scene, theme="light", expand=(), size=(640, 560), **opts):
        key = (scene, theme, tuple(expand), size, tuple(sorted(opts.items())))
        if key not in cache:
            cache[key] = harness.render(scene, theme=theme, expand=expand,
                                        size=size, **opts)
        return cache[key]

    return _shot


def pytest_report_header(config):
    from PyQt6.QtCore import QT_VERSION_STR
    return f"real-Qt suite: Qt {QT_VERSION_STR}, platform {harness.app().platformName()}"


@pytest.fixture(autouse=True)
def user_files(tmp_path, monkeypatch):
    from internpearls import (ai_dialog, background, collection, config, dialogs,
                             review, sync, updates)

    folder = tmp_path / "user_files"
    folder.mkdir()
    for mod in (config, collection):
        monkeypatch.setattr(mod, "_USER_FILES", str(folder))
    for name in ("INSTALLED", "STATE", "FEEDBACK", "SHIPPED", "DECLINED",
                 "DECK_SKILL", "AI_USAGE", "AI_LAST_RUN_LOG", "AI_DRAFT",
                 "LATER_SEEN", "USER_SKILL"):
        monkeypatch.setattr(config, name, str(folder / os.path.basename(getattr(config, name))))
    # Direct imports keep their own path bindings; harness overrides still run afterward.
    for mod, names in ((background, ("INSTALLED", "STATE")),
                       (collection, ("INSTALLED", "SHIPPED")),
                       (dialogs, ("INSTALLED",)),
                       (sync, ("INSTALLED", "SHIPPED")),
                       (updates, ("STATE",)),
                       (review, ("FEEDBACK",)),
                       (ai_dialog, ("AI_LAST_RUN_LOG",))):
        for name in names:
            monkeypatch.setattr(mod, name, getattr(config, name))
