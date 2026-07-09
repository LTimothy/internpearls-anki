"""The add-on's dialogs: source configuration, Manage decks, Settings, and About.

Everything here is presentation plus config writes; the flows that touch the
collection or the network live in sync.py / collection.py and are called from here.
"""
import os

from aqt import mw
from aqt.qt import (QApplication, QCheckBox, QDialog, QDialogButtonBox, QFrame,
                    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QScrollArea,
                    QSpinBox, Qt, QVBoxLayout, QWidget)

from .background import _restart_auto_sync_timer, _stop_auto_sync_timer
from .collection import _her_front_to_guid
from .config import (ADDON_PACKAGE, ADDON_VERSION, ANKI_REPO, APP_NAME,
                     AUTO_SYNC_INTERVAL_FLOOR_MIN, EXAMPLE_DECK_NAME, EXAMPLE_REPO,
                     EXAMPLE_SCOPE_TAG, EXPORT_DECK, INSTALLED, _cfg, _load_json)
from .logic import bullets, deck_status, parse_fields, remap_cards
from .sync import _fetch_manifest, sync_decks
from .ui import (_ask, _info, _prompt, _safe, _warn, hint_label, link_button,
                 muted_label, section_label, title_label, wait_cursor)


def _github_source_form(repo_default, token_default):
    """One form for both GitHub fields, returning (repo, token, ok). The repo and its
    (optional) token are one decision, so they belong in one dialog: the previous two
    prompts in a row read as a surprise second question, and Cancel on the token prompt
    threw away the repo just typed."""
    dlg = QDialog(mw)
    dlg.setWindowTitle(f"{APP_NAME}: GitHub deck source")
    dlg.setMinimumWidth(420)
    lay = QVBoxLayout(dlg)
    lay.setSpacing(6)
    lay.addWidget(section_label("Repo"))
    repo_edit = QLineEdit(repo_default)
    repo_edit.setPlaceholderText("owner/name")
    lay.addWidget(repo_edit)
    lay.addWidget(section_label("Access token", top_margin=8))
    token_edit = QLineEdit(token_default)
    token_edit.setEchoMode(QLineEdit.EchoMode.Password)
    lay.addWidget(token_edit)
    lay.addWidget(hint_label(
        "Leave blank for a public repo. A private one needs a read-only token; "
        "it's hidden as you type and stored only in this add-on's local config."))
    bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                          | QDialogButtonBox.StandardButton.Cancel)
    bb.accepted.connect(dlg.accept)
    bb.rejected.connect(dlg.reject)
    lay.addWidget(bb)
    if not dlg.exec():
        return "", "", False
    return repo_edit.text().strip(), token_edit.text().strip(), True


@_safe
def configure_source():
    """Set where decks come from: a GitHub repo (token optional; only needed for a
    private one), a local folder, or the public example repo for anyone who just wants
    to see the add-on do something before pointing it at real decks."""
    conf = mw.addonManager.getConfig(ADDON_PACKAGE) or {}

    box = QMessageBox(mw)
    box.setWindowTitle(f"{APP_NAME}: Configure deck source")
    box.setIcon(QMessageBox.Icon.Question)
    box.setText("Where should decks come from?<br><br>"
                "<span style='color:gray;'>No decks of your own yet? Try the example "
                "deck: a small public demo repo you can sync right away, and swap out "
                "later.</span>")
    gh_btn = box.addButton("GitHub repo", QMessageBox.ButtonRole.AcceptRole)
    local_btn = box.addButton("Local folder", QMessageBox.ButtonRole.AcceptRole)
    example_btn = box.addButton("Try the example deck", QMessageBox.ButtonRole.AcceptRole)
    box.addButton(QMessageBox.StandardButton.Cancel)
    box.exec()
    clicked = box.clickedButton()

    if clicked is gh_btn:
        repo, token, ok = _github_source_form(conf.get("github_decks_repo", ""),
                                              conf.get("github_token", ""))
        if not ok or not repo:
            return
        conf["github_decks_repo"] = repo
        conf["github_token"] = token
        conf["decks_dir"] = ""
    elif clicked is example_btn:
        conf["github_decks_repo"] = EXAMPLE_REPO
        conf["github_token"] = ""
        conf["decks_dir"] = ""
        # Point the scope tag and backup deck at the example deck's own values (but only
        # if they're still at their defaults — never clobber a deliberate custom value),
        # so the demo shows the real experience: preserved fields survive re-syncs and
        # the automatic pre-sync backup finds its deck. Switching to a GitHub/local
        # source later undoes exactly this (see below).
        if conf.get("scope_tag", "InternPearls") == "InternPearls":
            conf["scope_tag"] = EXAMPLE_SCOPE_TAG
        if conf.get("export_deck", EXPORT_DECK) == EXPORT_DECK:
            conf["export_deck"] = EXAMPLE_DECK_NAME
    elif clicked is local_btn:
        path, ok = _prompt("Folder with manifest.json + .apkg files:",
                          default=conf.get("decks_dir", ""))
        if not ok or not path.strip():
            return
        conf["decks_dir"] = path.strip()
        conf["github_token"] = ""
    else:
        return  # Cancel, or the dialog was closed

    # Undo the example-deck scope/backup override when moving on to a real source: those
    # two values were set by the example button above, not chosen by the user, so
    # leaving them behind would silently mis-scope every future sync. A custom value the
    # user set themselves is never touched (the example button doesn't overwrite one,
    # and this only resets the exact example values).
    if clicked is not example_btn:
        if conf.get("scope_tag") == EXAMPLE_SCOPE_TAG:
            conf["scope_tag"] = "InternPearls"
        if conf.get("export_deck") == EXAMPLE_DECK_NAME:
            conf["export_deck"] = EXPORT_DECK

    mw.addonManager.writeConfig(ADDON_PACKAGE, conf)

    try:
        with wait_cursor():
            manifest, _, source = _fetch_manifest(_cfg())
    except Exception as e:
        _warn(f"Saved, but couldn't connect: {e}<br><br>"
              "Double-check the repo name and token (or folder path), then use "
              "<i>Change source</i> in Manage decks again.")
        return
    if not manifest:
        _warn("Saved, but nothing was found at that source yet. Check the path "
              "or repo and try again.")
        return
    _info(f"Saved and connected to <b>{source}</b>, found {len(manifest['decks'])} "
          "deck(s).<br><br>Run <i>Intern Pearls → Sync decks</i> whenever you're ready.")


# -------------------------------------------------------------------- deck manager
# Colors for a deck's sync-state pill. Deliberately readable on both Anki themes: a
# saturated mid-tone reads fine on light and dark backgrounds alike, so we don't need to
# branch on night mode.
_STATE_STYLE = {
    "new":     ("New",              "#2563eb"),
    "update":  ("Update available", "#b45309"),
    "current": ("Up to date",       "#6b7280"),
}


def _pill_style(color):
    return f"color: {color}; font-size: 12px;"


class _DeckManagerDialog(QDialog):
    """Pick which decks sync and which fields are preserved, in one clean panel.

    Deck-source configuration lives here too, behind a "Configure source" / "Change
    source" button, rather than its own top-level menu item: the source only matters in
    the context of what decks are available to manage, so it made the menu bar noisier
    without adding a use case of its own.

    Mostly a thin rendering layer over already-computed rows (from logic.deck_status):
    it renders checkboxes and status pills, then hands back the user's choices via
    excluded_decks()/protected_fields(). No network or collection access lives here,
    except indirectly through change_source_requested, which the caller acts on after
    this dialog closes. Sync automation and add-on update behavior live in a separate
    Settings dialog: this one answers "which decks, which fields, from where," not "how
    automatic" (a different kind of choice that doesn't belong in the same panel).
    """

    def __init__(self, parent, rows, protected, source, preview_fn, configured):
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME}: Manage decks")
        self.setMinimumWidth(480)
        self.sync_requested = False
        self.change_source_requested = False
        self._rows = rows
        self._preview_fn = preview_fn   # callable() -> {deck_name: (kept, new) | None}
        self._checks = {}   # deck name -> QCheckBox
        self._pills = {}    # deck name -> QLabel (status pill, updated after a preview)

        outer = QVBoxLayout(self)
        outer.setSpacing(10)

        outer.addWidget(title_label("Manage decks"))

        source_row = QHBoxLayout()
        source_label = QLabel(f"Source: {source}")
        source_label.setStyleSheet("color: gray;")
        source_row.addWidget(source_label)
        source_row.addWidget(link_button(
            "Change source" if configured else "Configure source",
            on_click=self._request_change_source))
        source_row.addStretch()
        outer.addLayout(source_row)

        outer.addWidget(muted_label(
            "Check the decks you want to keep synced. Unchecking one stops "
            "future syncs for it; cards already imported stay in your "
            "collection until you delete them in Anki."))

        bar = QHBoxLayout()
        for label, val in (("Select all", True), ("Select none", False)):
            bar.addWidget(link_button(
                label, on_click=lambda _=False, v=val: self._set_all(v),
                align_left=True))
        bar.addStretch()
        self._preview_btn = link_button(
            "Check what will sync", on_click=self._run_preview,
            tooltip_text=(
                "Download the changed decks and show, per deck, how many cards would "
                "update in place (history kept) vs. be added as new. Nothing is "
                "imported."))
        # Reflect why there's nothing to check on open, rather than leaving an inviting
        # button that just reports "nothing happened" once clicked: no decks at all
        # (source unreachable or not configured) reads differently from every deck
        # already matching what's installed.
        if not rows:
            self._preview_btn.setText("No decks available")
            self._preview_btn.setEnabled(False)
            self._preview_btn.setStyleSheet("color: gray;")
        elif not any(r["state"] != "current" for r in rows):
            self._preview_btn.setText("All decks up to date")
            self._preview_btn.setEnabled(False)
            self._preview_btn.setStyleSheet("color: gray;")
        bar.addWidget(self._preview_btn)
        outer.addLayout(bar)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setMinimumHeight(230)
        holder = QWidget()
        col = QVBoxLayout(holder)
        col.setSpacing(6)
        col.setContentsMargins(0, 0, 6, 0)
        if rows:
            for r in rows:
                col.addWidget(self._deck_row(r))
        else:
            col.addWidget(muted_label(
                "No decks available yet. Use the button above to set up or "
                "fix your deck source."))
        col.addStretch()
        scroll.setWidget(holder)
        outer.addWidget(scroll, 1)

        outer.addWidget(section_label("Preserved fields"))
        self._pf_edit = QLineEdit(", ".join(protected))
        self._pf_edit.setPlaceholderText("Notes")
        outer.addWidget(self._pf_edit)
        outer.addWidget(hint_label(
            "Comma-separated fields holding your own annotations. Sync "
            "snapshots and restores them, so importing an updated deck "
            "never overwrites what you've written."))

        outer.addWidget(hint_label(
            "Save keeps these choices for your next sync. Save and sync "
            "now also pulls the selected decks right away.", top_margin=4))

        bb = QDialogButtonBox()
        save = bb.addButton("Save", QDialogButtonBox.ButtonRole.AcceptRole)
        sync = bb.addButton("Save and sync now", QDialogButtonBox.ButtonRole.ApplyRole)
        bb.addButton(QDialogButtonBox.StandardButton.Cancel)
        save.clicked.connect(self.accept)
        sync.clicked.connect(self._save_and_sync)
        bb.rejected.connect(self.reject)
        outer.addWidget(bb)

    def _deck_row(self, r):
        row = QFrame()
        row.setObjectName("deckRow")
        row.setStyleSheet(
            "#deckRow { border: 1px solid rgba(128,128,128,0.35); border-radius: 6px; }")
        h = QHBoxLayout(row)
        h.setContentsMargins(11, 8, 11, 8)
        cb = QCheckBox(r["short"])
        cb.setChecked(r["enabled"])
        cb.setStyleSheet("font-weight: 600;")
        self._checks[r["name"]] = cb
        h.addWidget(cb)
        h.addStretch()
        label, color = _STATE_STYLE[r["state"]]
        cards = r.get("cards")
        text = f'{cards} cards · {label}' if cards is not None else label
        pill = QLabel(text)
        pill.setStyleSheet(_pill_style(color))
        self._pills[r["name"]] = pill
        h.addWidget(pill)
        return row

    def _run_preview(self):
        """Download the changed decks and fill in each row's kept/new breakdown.

        Read-only: it counts how incoming cards match the collection, but imports
        nothing. Runs on click (not on open) so opening the panel stays fast.
        """
        pending = [r for r in self._rows if r["state"] != "current"]
        if not pending:
            self._preview_btn.setText("All up to date")
            self._preview_btn.setEnabled(False)
            return
        self._preview_btn.setText("Checking…")
        self._preview_btn.setEnabled(False)
        QApplication.processEvents()   # repaint the button before the blocking fetch
        try:
            with wait_cursor():
                result = self._preview_fn()   # {deck_name: (kept, new) | None}
        except Exception as e:
            self._preview_btn.setText("Preview failed")
            self._preview_btn.setEnabled(True)
            self._preview_btn.setToolTip(str(e))
            return
        for r in pending:
            pill = self._pills.get(r["name"])
            if pill is None:
                continue
            rc = result.get(r["name"])
            if rc is None:
                pill.setText("couldn't preview")
                pill.setStyleSheet(_pill_style("#b45309"))
            else:
                kept, new = rc
                pill.setText(f"{kept} kept · {new} new")
                pill.setStyleSheet(_pill_style("#6b7280"))
        self._preview_btn.setText("Check again")
        self._preview_btn.setEnabled(True)

    def _set_all(self, val):
        for cb in self._checks.values():
            cb.setChecked(val)

    def _save_and_sync(self):
        self.sync_requested = True
        self.accept()

    def _request_change_source(self):
        # Close without treating this as a save or a plain cancel; the caller checks
        # change_source_requested first and reopens this same dialog after the source
        # configuration flow runs, so any in-progress checkbox/field edits here are
        # simply discarded, same as a Cancel would do.
        self.change_source_requested = True
        self.reject()

    def excluded_decks(self):
        return [name for name, cb in self._checks.items() if not cb.isChecked()]

    def protected_fields(self):
        return parse_fields(self._pf_edit.text())


@_safe
def manage_decks():
    """Open the deck manager: choose which decks sync, which fields are preserved, and
    which source to pull from.

    Never dead-ends on a missing or unreachable source; the dialog always opens, with
    an empty deck list and a "Configure source" / "Change source" button front and
    center, since that button is now the only way to reach deck-source configuration.
    """
    cfg = _cfg()
    manifest, fetch, source, error = None, None, None, None
    if cfg["gh_repo"] or cfg["decks_dir"]:
        try:
            with wait_cursor():
                manifest, fetch, source = _fetch_manifest(cfg)
        except Exception as e:
            error = str(e)
    source_label = source if manifest else (f"error: {error}" if error else "not configured")

    installed = _load_json(INSTALLED, {})
    rows = deck_status(manifest, installed, cfg["excluded"]) if manifest else []

    def _preview():
        """Download every changed deck and match it against the collection, returning
        {deck_name: (kept_in_place, added_new)}. Read-only — imports nothing. Runs when
        the user clicks "Check what will sync"; the download is why it's on demand.
        Only ever called when manifest/fetch exist, since the button stays disabled
        otherwise."""
        pending = [d for d in manifest["decks"]
                   if installed.get(d["name"]) != d["version"]]
        her = _her_front_to_guid(cfg["scope_tag"])
        aliases = manifest.get("front_aliases", {})
        out = {}
        for d in pending:
            src = None
            try:
                src = fetch(d)
                _, kept, new = remap_cards(src, her, aliases)
                out[d["name"]] = (kept, new)
            except Exception:
                out[d["name"]] = None
            finally:
                if src and source == "GitHub":   # only delete our temp download
                    try:
                        os.remove(src)
                    except OSError:
                        pass
        return out

    dlg = _DeckManagerDialog(mw, rows, cfg["protected"], source_label, _preview,
                             configured=bool(manifest))
    result = dlg.exec()

    if dlg.change_source_requested:
        configure_source()
        manage_decks()   # reopen against whatever the source is now
        return
    if not result:
        return   # cancelled

    conf = mw.addonManager.getConfig(ADDON_PACKAGE) or {}
    conf["excluded_decks"] = dlg.excluded_decks()
    conf["protected_fields"] = dlg.protected_fields()
    mw.addonManager.writeConfig(ADDON_PACKAGE, conf)

    if dlg.sync_requested:
        sync_decks()
        return
    if not rows:
        _info("Saved. No decks are available from this source yet.")
        return
    kept = sum(1 for r in rows if r["name"] not in conf["excluded_decks"])
    excluded_n = len(rows) - kept
    scope = (f"All {kept} deck(s) are set to sync" if not excluded_n
             else f"{kept} of {len(rows)} deck(s) are set to sync ({excluded_n} excluded)")
    # Auto-sync is a separate, independent setting (Intern Pearls -> Settings), so this
    # dialog only reports whether it's currently on, not whether it changed here.
    next_step = (" Auto-sync is on, so these will keep applying on their own."
                if cfg["auto_sync_decks"] else
                " Nothing synced yet, run <b>Sync decks</b> when you're ready to pull "
                "them (or use <i>Save and sync now</i> next time to do both at once).")
    _info(f"Saved. {scope}, preserving {', '.join(conf['protected_fields'])}."
          f"<br><br>{next_step}")


class _SettingsDialog(QDialog):
    """Sync automation and add-on update behavior, kept apart from Manage decks.

    Manage decks answers "which decks, which fields" (what gets synced). This dialog
    answers "how automatic, how often" (whether it happens on its own). Keeping the two
    separate is what stops either one from turning into a catch-all as more toggles get
    added.
    """

    def __init__(self, parent, auto_sync, interval_minutes, notify_updates, auto_update):
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME}: Settings")
        self.setMinimumWidth(440)

        outer = QVBoxLayout(self)
        outer.setSpacing(10)

        outer.addWidget(title_label("Settings"))

        outer.addWidget(section_label("Deck sync", top_margin=4))

        self._auto_sync_cb = QCheckBox("Sync decks automatically when updates are available")
        self._auto_sync_cb.setChecked(auto_sync)
        outer.addWidget(self._auto_sync_cb)

        interval_row = QHBoxLayout()
        interval_row.addWidget(QLabel("Check every"))
        self._interval_spin = QSpinBox()
        self._interval_spin.setRange(AUTO_SYNC_INTERVAL_FLOOR_MIN, 1440)
        self._interval_spin.setValue(interval_minutes)
        self._interval_spin.setSuffix(" min")
        interval_row.addWidget(self._interval_spin)
        interval_row.addStretch()
        outer.addLayout(interval_row)

        outer.addWidget(hint_label(
            "Checks the source in the background and applies any changed decks without "
            "asking. A backup is still taken first, the same as a manual sync. The check "
            "itself is built not to freeze Anki even on a slow or dead connection: it "
            "fails fast and tries again at the next check."))

        outer.addWidget(section_label("Add-on updates", top_margin=14))

        self._notify_cb = QCheckBox("Notify me when a new add-on version is out")
        self._notify_cb.setChecked(notify_updates)
        outer.addWidget(self._notify_cb)

        self._auto_update_cb = QCheckBox("Install add-on updates automatically")
        self._auto_update_cb.setChecked(auto_update)
        outer.addWidget(self._auto_update_cb)

        outer.addWidget(hint_label(
            "Checked once per launch rather than on a repeating timer, since a new "
            "add-on release isn't as time-sensitive as a new deck. Either way, Anki "
            "needs a restart to load the new version."))

        bb = QDialogButtonBox()
        save = bb.addButton("Save", QDialogButtonBox.ButtonRole.AcceptRole)
        bb.addButton(QDialogButtonBox.StandardButton.Cancel)
        save.clicked.connect(self.accept)
        bb.rejected.connect(self.reject)
        outer.addWidget(bb)

    def values(self):
        return {
            "auto_sync_decks": self._auto_sync_cb.isChecked(),
            "auto_sync_interval_minutes": self._interval_spin.value(),
            "notify_addon_updates": self._notify_cb.isChecked(),
            "auto_update_addon": self._auto_update_cb.isChecked(),
        }


@_safe
def open_settings():
    """Open Settings: sync automation and add-on update behavior."""
    cfg = _cfg()
    dlg = _SettingsDialog(mw, cfg["auto_sync_decks"], cfg["auto_sync_interval_minutes"],
                          cfg["notify_addon_updates"], cfg["auto_update_addon"])
    if not dlg.exec():
        return

    values = dlg.values()
    conf = mw.addonManager.getConfig(ADDON_PACKAGE) or {}
    conf.update(values)
    mw.addonManager.writeConfig(ADDON_PACKAGE, conf)

    # Apply immediately rather than waiting for a restart.
    if values["auto_sync_decks"]:
        _restart_auto_sync_timer(values["auto_sync_interval_minutes"])
    else:
        _stop_auto_sync_timer()

    sync_line = (
        f"Deck sync checks every {values['auto_sync_interval_minutes']} minute(s) and "
        "applies updates on its own." if values["auto_sync_decks"] else
        "Deck sync stays manual, use Sync decks when you're ready.")
    if values["auto_update_addon"]:
        update_line = "Add-on updates install automatically."
    elif values["notify_addon_updates"]:
        update_line = "You'll get a notice when a new add-on version is out."
    else:
        update_line = "Add-on update checks are off."
    _info(f"Settings saved.<br><br>{sync_line}<br>{update_line}")


@_safe
def about():
    cfg = _cfg()
    sync_status = (
        f"on, checking every {cfg['auto_sync_interval_minutes']} minute(s)"
        if cfg["auto_sync_decks"] else "off")
    if cfg["auto_update_addon"]:
        update_status = "installs automatically"
    elif cfg["notify_addon_updates"]:
        update_status = "notifies you, you install it"
    else:
        update_status = "off"

    box = QMessageBox(mw)
    box.setWindowTitle(f"{APP_NAME}: About")
    box.setIcon(QMessageBox.Icon.Information)
    box.setTextFormat(Qt.TextFormat.RichText)
    box.setText(
        f"<b>Intern Pearls Deck Tools</b> &nbsp;<span style='color:gray;'>v{ADDON_VERSION}"
        "</span><br><br>"
        "Keeps a set of Anki decks in sync with a source you control, without losing "
        "review history or the annotations you keep in any preserved field. Cards are "
        "matched by ID, preserved fields are snapshotted and restored around every "
        "import, and a backup runs automatically before anything changes."
        "<br><br><b>Current settings</b>" +
        bullets([
            f"Auto-sync: {sync_status}",
            f"Add-on updates: {update_status}",
            f"Preserved fields: {', '.join(cfg['protected']) or 'none set'}",
        ]) +
        "Change these under <i>Manage decks</i> (which decks, which fields, and where "
        "from) or <i>Settings</i> (how automatic)."
        "<br><br>No deck content ships with the add-on itself. Set your source, a "
        "GitHub repo or a local folder, from <i>Manage decks</i>."
        "<br><br>"
        f'<a href="https://github.com/{ANKI_REPO}">github.com/{ANKI_REPO}</a>')
    box.setStandardButtons(QMessageBox.StandardButton.Ok)
    box.exec()
