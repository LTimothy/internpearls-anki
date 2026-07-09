"""Wires the mock Anki (see mock_anki.py) in before any internpearls import.

The real package __init__.py builds menus and pulls in the Qt-heavy dialogs
module, none of which these tests exercise — so the package is registered here
with just its __path__, letting `import internpearls.sync` load submodules
without ever executing __init__.py.
"""
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import mock_anki  # noqa: E402

_mock = mock_anki.install()

_pkg = types.ModuleType("internpearls")
_pkg.__path__ = [os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "internpearls"))]
sys.modules["internpearls"] = _pkg


@pytest.fixture
def anki(tmp_path, monkeypatch):
    """A fresh mock-Anki world per test: empty collection, empty dialog record,
    and all persistent add-on state (installed.json, deck backups) redirected
    into tmp_path so tests never touch the repo's real user_files/."""
    import internpearls.background as background
    import internpearls.collection as collection
    import internpearls.config as config
    import internpearls.sync as sync

    _mock.mw.col = mock_anki.MockCollection()
    _mock.mw._config = {}
    _mock.mw.reset_count = 0
    _mock.gui.interactive = False
    for lst in (_mock.gui.infos, _mock.gui.warnings, _mock.gui.tooltips,
                _mock.gui.asks, _mock.gui.answers, _mock.gui.interactions,
                _mock.gui.payloads):
        lst.clear()
    mock_anki.reset_run()

    installed = str(tmp_path / "installed.json")
    for mod in (config, sync, background):
        monkeypatch.setattr(mod, "INSTALLED", installed)
    monkeypatch.setattr(config, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(collection, "_USER_FILES", str(tmp_path / "user_files"))
    background._tpl_deferred_notified.clear()
    return _mock
