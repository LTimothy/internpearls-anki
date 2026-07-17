"""Bootstraps real PyQt6 before pytest imports any test module.

conftest.py is imported before the test modules beside it, which is exactly the hook
this needs: harness.bootstrap() has to run before the first `import internpearls.x`
anywhere in the process. See harness.py's module docstring for why that ordering is
not recoverable after the fact.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

pytest.importorskip("PyQt6", reason="the real-Qt suite needs PyQt6: see qt_tests/README.md")

import harness  # noqa: E402

harness.bootstrap()


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
