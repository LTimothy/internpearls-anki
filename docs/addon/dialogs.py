"""The add-on's dialogs: source configuration, Manage decks, Settings, and About.

Everything here is presentation plus config writes; the flows that touch the
collection or the network live in sync.py / collection.py and are called from here.
"""
from aqt import mw
from aqt.qt import (QCheckBox, QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel,
                    QLineEdit, QPushButton, QScrollArea, QSpinBox, QVBoxLayout,
                    QWidget)

from .background import _restart_auto_sync_timer, _stop_auto_sync_timer
from .collection import installed_matching_collection
from .config import (ADDON_PACKAGE, ADDON_VERSION, ANKI_REPO, APP_NAME,
                     AUTO_SYNC_INTERVAL_FLOOR_MIN, EXAMPLE_DECK_NAME, EXAMPLE_REPO,
                     EXAMPLE_SCOPE_TAG, EXPORT_DECK, INSTALLED, STATE, _cfg, _load_json)
from .logic import (deck_status, manifest_scope_suggestion, parse_fields,
                    plural, version_at_least)
from .palette import colors
from .review import append_rows, build_list_body
from .sync import _fetch_manifest, update_decks
from .ui import (_ask, _ask_scrollable, _ask_with_widget, _info, _prompt, _safe, _warn,
                 hint_label, link_button, muted_label, section_label, section_rule,
                 title_label, wait_cursor)
from .widgets import chip_cell


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


# Wide enough that no option's one-line hint wraps to three lines, and the margin Qt
# puts around a dialog layout by default. Named rather than inlined because the two are
# what the wrapping labels below are told to measure themselves against.
_SOURCE_DIALOG_W = 460
_SOURCE_DIALOG_MARGIN = 12

# The three sources, in the order they're offered. The example deck leads because it's
# the only one someone with no decks of their own can actually pick, and this screen is
# the first thing anybody meets: nothing in the add-on does anything until a source is
# set. Being first also makes it the dialog's default button, so it's what Enter takes.
_SOURCE_OPTIONS = (
    ("example", "Try the example deck",
     "A small public demo repo you can sync right away. Nothing about it is "
     "permanent; point this at your own decks whenever you're ready."),
    ("github", "GitHub repo",
     "Decks published in a repository. A public one needs only its name, a private "
     "one also takes a read-only token."),
    ("local", "Local folder",
     "A folder on this computer holding manifest.json and its .apkg files."),
)


class _SourceChoiceDialog(QDialog):
    """Where decks come from, as a vertical choice rather than a row of buttons.

    This used to be a QMessageBox, which could only put its three sources on one row
    next to Cancel: four same-weight buttons, each with nothing but its own label to
    explain it, under Qt's stock question icon. Every other dialog here is laid out in
    the add-on's own vocabulary, and this is the one a new user meets first, so it
    reads top to bottom instead: one full-width button per source with the sentence
    that explains it underneath, and Cancel by itself in the button box where it isn't
    competing with the actual choice.

    Holds no config logic. It records which option was clicked in `choice` (None if
    cancelled) and configure_source() acts on it.
    """

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME}: Configure deck source")
        self.setMinimumWidth(_SOURCE_DIALOG_W)
        self.choice = None

        outer = QVBoxLayout(self)
        outer.setSpacing(12)

        # Every label here wraps, so each one's height depends on the width it is given.
        # Collected as they are built and sized at the end, see the note below.
        wrapping = []

        outer.addWidget(title_label("Where should decks come from?"))
        blurb = muted_label(
            "No cards ship with the add-on itself. Point it at a source and it keeps "
            "those decks up to date, without touching your review history. You can "
            "change this later from Manage decks.")
        wrapping.append(blurb)
        outer.addWidget(blurb)

        accent = colors()["accent"]
        for i, (key, label, hint) in enumerate(_SOURCE_OPTIONS):
            option = QVBoxLayout()
            option.setSpacing(3)
            btn = QPushButton(label)
            btn.setMinimumHeight(32)
            btn.clicked.connect(lambda _=False, k=key: self._choose(k))
            option.addWidget(btn)
            # The recommendation is carried by the word itself, not by the accent
            # colour alone: a colour is the one part of this nobody reads out loud.
            lead = f"<span style='color:{accent};'>Recommended.</span> " if not i else ""
            note = hint_label(lead + hint)
            wrapping.append(note)
            option.addWidget(note)
            outer.addLayout(option)

        bb = QDialogButtonBox()
        bb.addButton(QDialogButtonBox.StandardButton.Cancel)
        bb.rejected.connect(self.reject)
        outer.addWidget(bb)
        # No addStretch above the buttons, unlike the wrappers in ui.py. Theirs hold a
        # body whose height depends on how much a caller passed, so the stretch is what
        # pins their buttons to the bottom of a dialog sized for the longest case. This
        # one always shows the same three options, so a stretch only ever leaves a band
        # of empty window above Cancel.
        #
        # Removing it is not enough on its own. A wrapping label's height depends on the
        # width it gets, but the dialog's own sizeHint is computed at the width the
        # layout would like (around 295px here) rather than the wider one setMinimumWidth
        # forces, so every paragraph is measured as if it wrapped onto more lines than it
        # actually will. That over-measurement, not the stretch, is where most of the
        # empty band came from: 409px of hinted height for 341px of content. Telling the
        # labels the width they are really going to get is what makes the two agree.
        for label in wrapping:
            label.setMinimumWidth(_SOURCE_DIALOG_W - 2 * _SOURCE_DIALOG_MARGIN)

    def _choose(self, key):
        self.choice = key
        self.accept()


@_safe
def configure_source():
    """Set where decks come from: a GitHub repo (token optional; only needed for a
    private one), a local folder, or the public example repo for anyone who just wants
    to see the add-on do something before pointing it at real decks."""
    conf = mw.addonManager.getConfig(ADDON_PACKAGE) or {}

    chooser = _SourceChoiceDialog(mw)
    chooser.exec()
    choice = chooser.choice

    if choice == "github":
        repo, token, ok = _github_source_form(conf.get("github_decks_repo", ""),
                                              conf.get("github_token", ""))
        if not ok or not repo:
            return
        conf["github_decks_repo"] = repo
        conf["github_token"] = token
        conf["decks_dir"] = ""
    elif choice == "example":
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
    elif choice == "local":
        path, ok = _prompt("Folder with manifest.json + .apkg files:",
                          default=conf.get("decks_dir", ""))
        if not ok or not path.strip():
            return
        conf["decks_dir"] = path.strip()
        # A lingering repo name would win: _fetch_manifest checks gh_repo first and
        # never even looks at decks_dir while one is set. Clear it so picking a local
        # folder actually takes effect, mirroring how the GitHub branch above clears
        # decks_dir. The token is left alone; it's inert with no repo configured, and
        # she'll need it again if she switches back.
        conf["github_decks_repo"] = ""
    else:
        return  # Cancel, or the dialog was closed

    # Undo the example-deck scope/backup override when moving on to a real source: those
    # two values were set by the example button above, not chosen by the user, so
    # leaving them behind would silently mis-scope every future sync. A custom value the
    # user set themselves is never touched (the example button doesn't overwrite one,
    # and this only resets the exact example values).
    if choice != "example":
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
    _offer_manifest_scope(manifest)
    _info(f"Saved and connected to <b>{source}</b>, found "
          f"{plural(len(manifest['decks']), 'deck')}.<br><br>Run <i>Intern Pearls → "
          "Update my decks</i> whenever you're ready.")


# Two rows and two sentences, so this one opens shorter than the confirmations that
# list a whole run's worth of cards (review._CONFIRM_HEIGHT). Still a floor: the list
# grows the dialog if a future manifest suggests more.
_SCOPE_DIALOG_H = 260


def _offer_manifest_scope(manifest):
    """Offer the deck author's suggested scope_tag / export_deck from the manifest.

    Without this, subscribing to a third-party deck leaves both at the Intern Pearls
    defaults, so field protection and the automatic pre-sync backup silently cover the
    wrong deck; the only fix was hand-editing raw config keys. Consent-gated, and only
    ever run from this interactive configure flow, never by a background sync.
    """
    cfg = _cfg()
    scope_tag, export_deck = manifest_scope_suggestion(
        manifest, cfg["scope_tag"], cfg["export_deck"])
    if not scope_tag and not export_deck:
        return
    changes = []
    if scope_tag:
        changes.append(f"Scope tag: <b>{scope_tag}</b> (which cards this add-on "
                       "manages and protects)")
    if export_deck:
        changes.append(f"Backup deck: <b>{export_deck}</b> (what the automatic "
                       "pre-sync backup covers)")
    # Two settings, both the same kind of thing, so neither is chipped and neither has
    # anything to line up against: card_columns declined (see widgets.simple_row).
    items = []
    append_rows(items, [("row", None, change, "") for change in changes])
    # The question this used to close on ("Apply them?") is the accept button's own
    # label now, so asking it again above the button that answers it would be the
    # reader's only ambiguity here spelled twice.
    if not _ask_with_widget(
        build_list_body(items, card_columns=False, top_html=(
            "This deck source recommends settings so your own notes on cards survive "
            "updates and backups cover its decks:")),
        yes_label="Apply", min_height=_SCOPE_DIALOG_H
    ):
        return
    conf = mw.addonManager.getConfig(ADDON_PACKAGE) or {}
    if scope_tag:
        conf["scope_tag"] = scope_tag
    if export_deck:
        conf["export_deck"] = export_deck
    mw.addonManager.writeConfig(ADDON_PACKAGE, conf)


# -------------------------------------------------------------------- deck manager
# A deck's sync state as the chip the rest of the add-on already gives that same fact: a
# deck the collection has none of is NEW, one with a content update waiting is UPDATED,
# the two kinds Sync decks' own confirmation lists its decks by. Both screens list decks
# and a reader moves between them, so they say it the same way.
#
# "current" maps to no chip on purpose. It is what most rows read most of the time, so a
# third colour there would compete with the deck names for the whole list; it stays as
# the muted trailing text _deck_row writes below.
_STATE_CHIP = {"new": "new", "update": "changed", "current": None}


class _DeckManagerDialog(QDialog):
    """Pick which decks sync and which fields are preserved, in one clean panel.

    Deck-source configuration lives here too, behind a "Configure source" / "Change
    source" button, rather than its own top-level menu item: the source only matters in
    the context of what decks are available to manage, so it made the menu bar noisier
    without adding a use case of its own.

    Mostly a thin rendering layer over already-computed rows (from logic.deck_status):
    it renders checkboxes and state chips, then hands back the user's choices via
    excluded_decks()/protected_fields(). No network or collection access lives here,
    except indirectly through change_source_requested, which the caller acts on after
    this dialog closes. Sync automation and add-on update behavior live in a separate
    Settings dialog: this one answers "which decks, which fields, from where," not "how
    automatic" (a different kind of choice that doesn't belong in the same panel).

    Purely configuration, no live preview: what's actually pending — per-deck kept/new
    counts, retired cards, cards to relocate — is Update my decks' confirmation's job,
    not this dialog's. It used to also carry a "Check what will sync" button computing
    that same preview, which meant checking twice: once here, then again in the
    confirmation when actually running it. Removed rather than kept as a duplicate.
    """

    def __init__(self, parent, rows, protected, source, configured):
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME}: Manage decks")
        self.setMinimumWidth(480)
        self.update_requested = False
        self.change_source_requested = False
        self._checks = {}   # deck name -> QCheckBox

        outer = QVBoxLayout(self)
        outer.setSpacing(10)

        outer.addWidget(title_label("Manage decks"))

        source_row = QHBoxLayout()
        source_label = QLabel(f"Source: {source}")
        source_label.setStyleSheet(f"color: {colors()['muted']};")
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
        outer.addLayout(bar)

        if rows:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            holder = QWidget()
            col = QVBoxLayout(holder)
            col.setSpacing(6)
            col.setContentsMargins(0, 0, 6, 0)
            # The 230px floor only makes sense once there's a list to scroll: it gives
            # a few rows room before a scrollbar kicks in.
            scroll.setMinimumHeight(230)
            for r in rows:
                col.addWidget(self._deck_row(r))
            col.addStretch()
            scroll.setWidget(holder)
            outer.addWidget(scroll, 1)
        else:
            # No QScrollArea here: it's a single static line with nothing to scroll,
            # and a QScrollArea defaults to an Expanding vertical size policy, which
            # claims the dialog's leftover height for itself and leaves the label
            # floating mid-box instead of at its own natural size (the same bug fixed
            # in ui.py's _ask_scrollable). A plain label has no such policy, so it sizes
            # to its own content and the addStretch() below collects all the leftover
            # space instead.
            outer.addWidget(muted_label(
                "No decks available yet. Use the button above to set up or "
                "fix your deck source."))
            outer.addStretch()

        outer.addWidget(section_label("Preserved fields"))
        self._pf_edit = QLineEdit(", ".join(protected))
        self._pf_edit.setPlaceholderText("Notes")
        outer.addWidget(self._pf_edit)
        outer.addWidget(hint_label(
            "Comma-separated fields holding your own annotations. Sync "
            "snapshots and restores them, so importing an updated deck "
            "never overwrites what you've written."))

        outer.addWidget(hint_label(
            "Save keeps these choices for your next update. Save and update "
            "now also pulls and tidies up right away.", top_margin=4))
        outer.addSpacing(10)

        bb = QDialogButtonBox()
        save = bb.addButton("Save", QDialogButtonBox.ButtonRole.AcceptRole)
        update = bb.addButton("Save and update now", QDialogButtonBox.ButtonRole.ApplyRole)
        bb.addButton(QDialogButtonBox.StandardButton.Cancel)
        save.clicked.connect(self.accept)
        update.clicked.connect(self._save_and_update)
        bb.rejected.connect(self.reject)
        outer.addWidget(bb)

    def _deck_row(self, r):
        row = QFrame()
        row.setObjectName("deckRow")
        # The outline that makes each deck read as its own card rather than a line in a
        # block of text, so it needs a value per theme like everything else drawn against
        # the window: one colour dark enough to see on a light window disappears into a
        # dark one, and the list goes flat in Night Mode.
        row.setStyleSheet(f"#deckRow {{ border: 1px solid {colors()['panel_rule']};"
                          " border-radius: 6px; }")
        h = QHBoxLayout(row)
        h.setContentsMargins(11, 8, 11, 8)
        cb = QCheckBox(r["short"])
        cb.setChecked(r["enabled"])
        cb.setStyleSheet("font-weight: 600;")
        self._checks[r["name"]] = cb
        h.addWidget(cb)
        h.addStretch()
        cards = r.get("cards")
        parts = [plural(cards, "card")] if cards is not None else []
        if _STATE_CHIP[r["state"]] is None:
            # The one state with no chip still has to say what it is; the other two are
            # named by the chip beside them and would only repeat themselves here.
            parts.append("Up to date")
        if parts:
            trailing = QLabel(" · ".join(parts))
            trailing.setStyleSheet(f"color: {colors()['muted']};")
            h.addWidget(trailing)
        # Last rather than beside the deck name: a chip after a name is at a different x
        # on every row, while the fixed-width cell against the row's right edge (empty on
        # an up-to-date row) keeps the counts and the chips each in a column of their own.
        h.addWidget(chip_cell(_STATE_CHIP[r["state"]]))
        return row

    def _set_all(self, val):
        for cb in self._checks.values():
            cb.setChecked(val)

    def _save_and_update(self):
        self.update_requested = True
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
    manifest, source, error = None, None, None
    if cfg["gh_repo"] or cfg["decks_dir"]:
        try:
            with wait_cursor():
                manifest, _, source = _fetch_manifest(cfg)
        except Exception as e:
            error = str(e)
    source_label = source if manifest else (f"error: {error}" if error else "not configured")

    installed = installed_matching_collection(_load_json(INSTALLED, {}), cfg["scope_tag"])
    rows = deck_status(manifest, installed, cfg["excluded"]) if manifest else []

    dlg = _DeckManagerDialog(mw, rows, cfg["protected"], source_label,
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

    if dlg.update_requested:
        update_decks()
        return
    if not rows:
        _info("Saved. No decks are available from this source yet.")
        return
    kept = sum(1 for r in rows if r["name"] not in conf["excluded_decks"])
    excluded_n = len(rows) - kept
    verb = "is" if kept == 1 else "are"
    scope = (f"{plural(kept, 'deck')} {verb} set to sync" if not excluded_n
             else f"{kept} of {plural(len(rows), 'deck')} {verb} set to sync "
                  f"({excluded_n} excluded)")
    # Auto-sync is a separate, independent setting (Intern Pearls -> Settings), so this
    # dialog only reports whether it's currently on, not whether it changed here.
    next_step = (" Auto-sync is on, so these will keep applying on their own."
                if cfg["auto_sync_decks"] else
                " Nothing pulled yet, run <b>Update my decks</b> when you're ready "
                "(or use <i>Save and update now</i> next time to do both at once).")
    _info(f"Saved. {scope}, preserving {', '.join(conf['protected_fields'])}."
          f"<br><br>{next_step}")


class _SettingsDialog(QDialog):
    """How the add-on behaves on its own, kept apart from Manage decks.

    Manage decks answers "which decks, which fields" (what gets synced). This dialog
    answers "how automatic, how often" (whether it happens on its own), and alongside
    that the two display choices that belong to no particular deck: dimming bright
    pictures in Night Mode, and whether an update offers a box for flagging a card.
    Keeping the two dialogs separate is what stops either one from turning into a
    catch-all as more toggles get added.
    """

    def __init__(self, parent, auto_sync, interval_minutes, notify_updates, auto_update,
                dim_images_night_mode, collect_feedback):
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
            "Changed decks apply without asking. A backup is still taken first, the "
            "same as a manual sync."))

        outer.addWidget(section_rule())
        outer.addWidget(section_label("Add-on updates"))

        self._notify_cb = QCheckBox("Notify me when a new add-on version is out")
        self._notify_cb.setChecked(notify_updates)
        outer.addWidget(self._notify_cb)

        self._auto_update_cb = QCheckBox("Install add-on updates automatically")
        self._auto_update_cb.setChecked(auto_update)
        outer.addWidget(self._auto_update_cb)

        outer.addWidget(hint_label(
            "Anki needs a restart to load a new version, however it arrives."))

        outer.addWidget(section_rule())
        outer.addWidget(section_label("Night mode"))

        self._dim_images_cb = QCheckBox("Dim bright images in Night Mode")
        self._dim_images_cb.setChecked(dim_images_night_mode)
        outer.addWidget(self._dim_images_cb)

        outer.addWidget(hint_label(
            "Applies to every deck in your collection, not just Intern Pearls ones."))

        outer.addWidget(section_rule())
        outer.addWidget(section_label("Card review"))

        self._feedback_cb = QCheckBox("Let me flag problems with cards as they sync")
        self._feedback_cb.setChecked(collect_feedback)
        outer.addWidget(self._feedback_cb)

        outer.addWidget(hint_label(
            "Puts a note box under each card on the Update my decks screen, and hands "
            "back a summary of what you flagged whether or not you go ahead."))

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
            "dim_images_night_mode": self._dim_images_cb.isChecked(),
            "collect_card_feedback": self._feedback_cb.isChecked(),
        }


@_safe
def open_settings():
    """Open Settings: what the add-on does on its own, and how a card is shown."""
    cfg = _cfg()
    dlg = _SettingsDialog(mw, cfg["auto_sync_decks"], cfg["auto_sync_interval_minutes"],
                          cfg["notify_addon_updates"], cfg["auto_update_addon"],
                          cfg["dim_images_night_mode"], cfg["collect_feedback"])
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
        f"Deck sync checks every "
        f"{plural(values['auto_sync_interval_minutes'], 'minute')} and applies updates "
        "on its own." if values["auto_sync_decks"] else
        "Deck sync stays manual, use Sync decks when you're ready.")
    if values["auto_update_addon"]:
        update_line = "Add-on updates install automatically."
    elif values["notify_addon_updates"]:
        update_line = "You'll get a notice when a new add-on version is out."
    else:
        update_line = "Add-on update checks are off."
    dim_line = ("Bright images will be dimmed in Night Mode."
               if values["dim_images_night_mode"] else
               "Night Mode image dimming is off.")
    _info(f"Settings saved.<br><br>{sync_line}<br>{update_line}<br>{dim_line}")


@_safe
def about():
    cfg = _cfg()
    sync_status = (
        f"on, checking every {plural(cfg['auto_sync_interval_minutes'], 'minute')}"
        if cfg["auto_sync_decks"] else "off")
    if cfg["auto_update_addon"]:
        update_status = "installs automatically"
    elif cfg["notify_addon_updates"]:
        update_status = "notifies you, you install it"
    else:
        update_status = "off"

    # "Latest known" reads the same cached state.json value the menu label and startup
    # tooltip already use — no fresh network call here, since About opening shouldn't
    # block on one. It can be stale (only as fresh as the last background/manual check),
    # but it's the same staleness the menu label already accepts, and far better than
    # About only ever showing the installed version with no way to tell an update exists.
    latest_known = _load_json(STATE, {}).get("last_notified_addon_version")
    update_suffix = ""
    if latest_known and not version_at_least(ADDON_VERSION, latest_known):
        warning = colors()["warning"]
        update_suffix = (f" &nbsp;<span style='color:{warning};'>(v{latest_known} "
                         f"available — Advanced → Check for add-on updates)</span>")

    muted = colors()["muted"]
    text = (
        f"<b>Intern Pearls Deck Tools</b> &nbsp;<span style='color:{muted};'>v{ADDON_VERSION}"
        f"</span>{update_suffix}<br><br>"
        "Keeps a set of Anki decks in sync with a source you control, without losing "
        "review history or the annotations you keep in any preserved field. Cards are "
        "matched by ID, preserved fields are snapshotted and restored around every "
        "import, and a backup runs automatically before anything changes."
        # Three label-and-value lines rather than a bulleted list: they read as part of
        # this paragraph of prose, which is what About is, and a bullet per setting made
        # three short facts look like three things to act on.
        "<br><br><b>Current settings</b><br>"
        f"Auto-sync: {sync_status}<br>"
        f"Add-on updates: {update_status}<br>"
        f"Preserved fields: {', '.join(cfg['protected']) or 'none set'}"
        "<br><br>"
        "Change these under <i>Manage decks</i> (which decks, which fields, and where "
        "from) or <i>Settings</i> (how automatic)."
        "<br><br>No deck content ships with the add-on itself. Set your source, a "
        "GitHub repo or a local folder, from <i>Manage decks</i>."
        "<br><br>"
        f'<a href="https://github.com/{ANKI_REPO}">github.com/{ANKI_REPO}</a>')
    # A QDialog built through _ask_scrollable rather than a bare QMessageBox, so About
    # carries the same title, width, and scrollable-body styling every other dialog
    # here does. no_label=None drops the second button: About only ever has the one.
    # open_external_links=True is the one deliberate exception to the wrapper's default:
    # this body is fixed, add-on-authored text (never collection content), and its own
    # repository anchor is meant to be clickable.
    _ask_scrollable(text, yes_label="OK", no_label=None, title=f"{APP_NAME}: About",
                    open_external_links=True)
