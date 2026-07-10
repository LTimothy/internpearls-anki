"""
Intern Pearls Deck Tools: an Anki add-on for history-safe deck sync.

"Sync decks" is the only button most people need. It pulls any changed decks (from the
private GitHub repo, or a local folder) and applies each one so your review history and
your own annotations in any preserved field are kept. Fixing note types and importing a
single deck run automatically as part of Sync and are exposed under "Advanced" only as
a fallback.

Before touching the collection, Sync takes its own timestamped backup, so nothing here
depends on the user remembering to export one first.

This file is only the menu and startup wiring. The work lives in focused modules:

- logic.py       pure Python (no aqt/anki imports), unit-tested with plain pytest
- config.py      constants, config access, persistent state under user_files/
- ui.py          dialog wrappers (_info/_warn/_ask/_prompt), _safe/_bg_safe, styling
- net.py         HTTP + GitHub contents-API fetches, the timeout policy
- collection.py  everything that touches mw.col: backups, snapshot/restore, imports
- sync.py        the sync flows (Sync decks, Import single deck, source resolution)
- updates.py     add-on self-update (version fetch, download, manual check)
- background.py  QueryOp dispatch, the startup update check, the auto-sync poll
- dialogs.py     Manage decks, Settings, About, and source configuration
"""
from aqt import gui_hooks, mw
from aqt.qt import QAction, QMenu

from .background import _schedule_background_checks
from .collection import (backup_collection_now, backup_deck_now, export_deck,
                         import_deck, restore_from_backup, update_notetypes)
from .dialogs import about, manage_decks, open_settings
from .sync import import_single, reconcile_decks, sync_decks
from .updates import check_updates


def _menu():
    menu = QMenu("&Intern Pearls", mw)

    def add(target, label, fn):
        act = QAction(label, mw)
        act.setMenuRole(QAction.MenuRole.NoRole)  # macOS Qt auto-moves items whose label
        act.triggered.connect(lambda checked=False, fn=fn: fn())  # Qt's triggered signal passes a
                                                   # checked bool; discard it since these all take
                                                   # no args and Qt can't introspect through _safe's
                                                   # *args wrapper to know that.
        target.addAction(act)                     # the app menu unless told not to

    # Two primary actions up top, everything occasional tucked under Advanced (including
    # the manual add-on-update check, which most people never need since the background
    # notice already covers it), and a small Settings/About pair at the bottom. Deck
    # source configuration lives inside Manage decks itself now, not as its own item,
    # since it only matters in the context of what decks are available to manage.
    add(menu, "Sync decks", sync_decks)
    add(menu, "Manage decks", manage_decks)
    menu.addSeparator()
    adv = menu.addMenu("Advanced")
    add(adv, "Import single deck (manual)", import_single)
    add(adv, "Fix note types", update_notetypes)
    add(adv, "Reconcile my decks", reconcile_decks)
    adv.addSeparator()
    add(adv, "Backup intern pearls deck", backup_deck_now)
    add(adv, "Import intern pearls deck", import_deck)
    add(adv, "Export intern pearls deck", export_deck)
    adv.addSeparator()
    add(adv, "Backup full collection", backup_collection_now)
    add(adv, "Restore full collection", restore_from_backup)
    adv.addSeparator()
    add(adv, "Check for add-on updates", check_updates)
    menu.addSeparator()
    add(menu, "Settings", open_settings)
    add(menu, "About", about)

    try:
        mw.form.menubar.insertMenu(mw.form.menuHelp.menuAction(), menu)
    except Exception:
        mw.form.menuTools.addMenu(menu)


gui_hooks.main_window_did_init.append(_menu)
gui_hooks.main_window_did_init.append(_schedule_background_checks)
