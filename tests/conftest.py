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

# widgets.chip_column_width() measures four probe labels the first time anything asks
# for it, and every widget the mock builds takes the next widget id. Left cold, that
# measurement lands mid-flow on whichever run first builds a chip, shifting every id
# after it on that run alone. The replay driver's whole contract is that a flow's ids
# are identical on every run of it, so a recorded click would then answer the wrong
# widget. Spending those four ids here, before any flow starts, is what keeps that true.
import internpearls.widgets  # noqa: E402

internpearls.widgets.chip_column_width()


@pytest.fixture
def anki(tmp_path, monkeypatch):
    """A fresh mock-Anki world per test: empty collection, empty dialog record,
    and all persistent add-on state (installed.json, deck backups) redirected
    into tmp_path so tests never touch the repo's real user_files/."""
    import internpearls.background as background
    import internpearls.collection as collection
    import internpearls.config as config
    import internpearls.review as review
    import internpearls.sync as sync
    import internpearls.updates as updates
    import aqt.qt as aqt_qt

    aqt_qt.QProgressDialog.cancel_after = {}   # a prior test's cancel hook must not leak in
    _mock.mw.col = mock_anki.MockCollection()
    _mock.mw._config = {}
    _mock.mw.reset_count = 0
    _mock.gui.interactive = False
    for lst in (_mock.gui.infos, _mock.gui.warnings, _mock.gui.tooltips,
                _mock.gui.asks, _mock.gui.answers, _mock.gui.file_picks,
                _mock.gui.interactions, _mock.gui.payloads, _mock.gui.clipboard):
        lst.clear()
    mock_anki.reset_run()

    installed = str(tmp_path / "installed.json")
    for mod in (config, sync, background, collection):
        monkeypatch.setattr(mod, "INSTALLED", installed)
    # Each of these modules does `from .config import STATE` (a direct name import), so
    # patching config.STATE alone doesn't reach them — every module holding its own
    # bound copy of the name needs patching individually, same as INSTALLED above.
    state = str(tmp_path / "state.json")
    for mod in (config, background, updates):
        monkeypatch.setattr(mod, "STATE", state)
    user_files = tmp_path / "user_files"
    user_files.mkdir(exist_ok=True)
    monkeypatch.setattr(collection, "_USER_FILES", str(user_files))
    # FEEDBACK (review.py) and SHIPPED (sync.py) are real on-disk paths too, and were
    # not test-isolated like INSTALLED/STATE above, until now. A test that left a card
    # flagged or a field baseline behind used to leak into whatever test happened to
    # run after it in the same session, since both live fixed under the add-on's own
    # user_files/ rather than under tmp_path.
    monkeypatch.setattr(review, "FEEDBACK", str(user_files / "card_feedback.json"))
    monkeypatch.setattr(sync, "SHIPPED", str(user_files / "shipped_fields.json"))
    background._tpl_deferred_notified.clear()
    background._last_reconcile_notified = 0
    sync._reconcile_action = None   # a prior test's registered stub must not leak in
    sync._apkg_cache.clear()        # preview-download cache must not leak across tests
    return _mock
