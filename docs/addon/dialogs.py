"""The add-on's dialogs: source configuration, Manage decks, Settings, and About.

Everything here is presentation plus config writes; the flows that touch the
collection or the network live in sync.py / collection.py and are called from here.
"""
from aqt import mw
from aqt.qt import (QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFrame,
                    QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea, QSpinBox,
                    Qt, QVBoxLayout, QWidget)

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
from .ui import (_ask, _ask_scrollable, _ask_with_widget, _info, _safe, _warn,
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
    ok = bool(dlg.exec())
    repo, token = repo_edit.text().strip(), token_edit.text().strip()
    dlg.deleteLater()   # its fields are read above; see ui._ask_scrollable
    if not ok:
        return "", "", False
    return repo, token, True


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
     "Pick the folder on this computer that holds manifest.json and the .apkg files."),
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
    chooser.deleteLater()

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
        # A picker rather than a typed path: a mistyped folder is the one source error
        # that a prompt invites and a picker cannot make. The mock Qt answers this
        # through the same prompt payload the live demo already draws, so the demo
        # keeps a usable path here without a native dialog to open.
        #
        # Which folder to pick is said on the option's own line back in _SOURCE_OPTIONS,
        # where it is always read: macOS opens its native directory picker with no
        # caption at all, so this one is a convenience on the platforms that show it and
        # never the only place the instruction appears.
        path = QFileDialog.getExistingDirectory(
            mw, f"{APP_NAME}: folder with manifest.json + .apkg files",
            conf.get("decks_dir", ""))
        if not path.strip():
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

# How wide a deck's label may paint before the middle is elided, in pixels of whatever
# font the platform is actually drawing it in: the 480px dialog, less the row's own
# margins and the count and chip columns it shares the row with.
_DECK_LABEL_W = 260

# The same cap in characters, for a caller with no font to measure against (the mock Qt
# tests/ runs on has no font engine, the same reason a sizeHint reads 0 there). Only ever
# an approximation of the line above: a character is not a fixed width.
_DECK_LABEL_MAX = 42

# The weight a deck's name is drawn at, on the row's checkbox and on the probe that
# measures how much of it fits.
_DECK_LABEL_STYLE = "font-weight: 600;"


def _deck_labels(rows):
    """What each row's checkbox is labelled: the deck's leaf name, or the shortest tail
    of its path that tells it apart from another offered deck with the same leaf.

    A source is free to publish "Cardiology::Basics" and "Renal::Basics", and both
    reduce to "Basics", so a list of leaves alone leaves two rows nothing to tell them
    apart by (their state and count often match too). Only the ambiguous rows lengthen:
    the common case is a list of plain leaf names, and prefixing every one of them with
    a path nobody needed to read would cost that.
    """
    paths = [r["name"].split("::") for r in rows]
    labels = []
    for i, parts in enumerate(paths):
        tail = parts
        for k in range(1, len(parts) + 1):
            tail = parts[-k:]
            if all(other[-k:] != tail for j, other in enumerate(paths) if j != i):
                break
        labels.append("::".join(tail))
    return labels


def _elide(text):
    """`text` shortened from the middle once it passes _DECK_LABEL_MAX.

    From the middle rather than the end because both ends carry meaning here: the tail
    is the deck's own name and the head is whatever parent _deck_labels kept to
    disambiguate it. The row's tooltip holds the full path either way.
    """
    if len(text) <= _DECK_LABEL_MAX:
        return text
    head = (_DECK_LABEL_MAX - 1) // 2
    return text[:head] + "…" + text[-(_DECK_LABEL_MAX - 1 - head):]


def _fit_label(text):
    """`text` elided from the middle to what actually fits _DECK_LABEL_W pixels in the
    font a deck row paints it in.

    Counting characters cannot do this job on its own: a wide glyph is roughly twice
    the width of a Latin one, so a label well inside the character cap still runs past
    the width that cap stands for and widens the panel it was meant to keep narrow.
    Font metrics measure the string that is actually going to be drawn.

    Measured off a probe carrying the row's own label style rather than a bare default
    font, the way widgets.chip_column_width measures a pill: bold text is wider, and a
    measurement taken at the wrong weight is a measurement of a label nobody paints.
    Falls back to the character cap where there are no metrics to ask, which is the mock
    Qt tests/ runs on and nothing a real user has.
    """
    probe = QCheckBox(text)
    probe.setStyleSheet(_DECK_LABEL_STYLE)
    probe.ensurePolished()   # a stylesheet's font only reaches the widget on polish
    metrics = getattr(probe, "fontMetrics", None)
    if metrics is None:
        return _elide(text)
    return metrics().elidedText(text, Qt.TextElideMode.ElideMiddle, _DECK_LABEL_W)


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

    def __init__(self, parent, rows, protected, source, configured, source_failed=False):
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
        # A failed local folder puts the whole path in this line, so without wrapping
        # one long path widens the dialog to the error's own width.
        source_label.setWordWrap(True)
        # A source that didn't load reads in the warning colour, not the muted one every
        # other value here carries: "error: …" in the same grey as a working source name
        # is the line most easily mistaken for the source simply being called that.
        role = "warning" if source_failed else "muted"
        source_label.setStyleSheet(f"color: {colors()[role]};")
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

        if rows:
            # Inside the branch: with no rows there is nothing for either link to
            # select, so above "No decks available yet" they are two inert words
            # competing with the one button that empty state actually offers.
            bar = QHBoxLayout()
            for label, val in (("Select all", True), ("Select none", False)):
                bar.addWidget(link_button(
                    label, on_click=lambda _=False, v=val: self._set_all(v),
                    align_left=True))
            bar.addStretch()
            outer.addLayout(bar)

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
            for r, label in zip(rows, _deck_labels(rows)):
                col.addWidget(self._deck_row(r, label))
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

    def _deck_row(self, r, label):
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
        cb = QCheckBox(_fit_label(label))
        cb.setChecked(r["enabled"])
        # The full path, always, whatever the row ended up showing: it is the only thing
        # that names the deck exactly, and both the label rules above can shorten it.
        cb.setToolTip(r["name"])
        cb.setStyleSheet(_DECK_LABEL_STYLE)
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
        # change_source_requested first, carries the choices made here across, and
        # reopens this same dialog against whatever the source is then. Changing the
        # source is not a decision to throw away the ticks and fields already edited.
        self.change_source_requested = True
        self.reject()

    def excluded_decks(self, previous=()):
        """Which decks to exclude: the unticked rows, plus every deck in `previous`
        this dialog never rendered a row for.

        `previous` is what was excluded going in, and merging against it rather than
        answering from the checkboxes alone is what keeps an opt-out from being zeroed
        by a dialog that couldn't see it. A source that failed to load renders no rows
        at all, so an empty checkbox map is not "nothing is excluded", it is "this
        dialog was never told about a single deck". Save used to write that over every
        saved exclusion, which then rode along into the reopen after Change source, the
        one button someone with a broken source is most likely to press.

        The partial case is the same rule one row down: a source offering deck A knows
        nothing about deck B, so B's exclusion is preserved rather than dropped. That
        leaves a stale name behind when a source genuinely retires a deck, which costs
        nothing (an excluded name matching no deck excludes nothing) and is the only
        direction that cannot silently lose an opt-out. Dropping it means a deck that
        merely went missing for one fetch comes back ticked and re-imports the cards
        someone opted out of.
        """
        unticked = [name for name, cb in self._checks.items() if not cb.isChecked()]
        return unticked + [name for name in previous if name not in self._checks]

    def protected_fields(self):
        return parse_fields(self._pf_edit.text())


@_safe
def manage_decks(pending=None):
    """Open the deck manager: choose which decks sync, which fields are preserved, and
    which source to pull from.

    Never dead-ends on a missing or unreachable source; the dialog always opens, with
    an empty deck list and a "Configure source" / "Change source" button front and
    center, since that button is now the only way to reach deck-source configuration.

    `pending` is {"excluded": [...], "protected": [...]} from a dialog that closed to
    configure the source, and is how those edits survive the reopen below rather than
    being thrown away by a click that never asked to discard them. Nothing else passes
    it: the saved config is what a fresh open reads.
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

    excluded = pending["excluded"] if pending else cfg["excluded"]
    protected = pending["protected"] if pending else cfg["protected"]
    installed = installed_matching_collection(_load_json(INSTALLED, {}), cfg["scope_tag"])
    rows = deck_status(manifest, installed, excluded) if manifest else []

    # Whether a source is *set*, not whether it loaded: a broken repo or a folder that
    # has moved is still configured, and offering to "Configure source" for one reads as
    # though nothing had ever been set up.
    dlg = _DeckManagerDialog(mw, rows, protected, source_label,
                             configured=bool(cfg["gh_repo"] or cfg["decks_dir"]),
                             source_failed=bool(error))
    result = dlg.exec()
    change_source, update_now = dlg.change_source_requested, dlg.update_requested
    # Merged against what was excluded going in, not read from the checkboxes alone:
    # this dialog only ever knows about the decks it rendered (see excluded_decks).
    choices = {"excluded": dlg.excluded_decks(excluded),
               "protected": dlg.protected_fields()}
    dlg.deleteLater()   # every read of it is above; see ui._ask_scrollable

    if change_source:
        configure_source()
        manage_decks(choices)   # reopen against whatever the source is now
        return
    if not result:
        return   # cancelled

    conf = mw.addonManager.getConfig(ADDON_PACKAGE) or {}
    conf["excluded_decks"] = choices["excluded"]
    conf["protected_fields"] = choices["protected"]
    mw.addonManager.writeConfig(ADDON_PACKAGE, conf)

    if update_now:
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
        # Nothing checks on an interval while auto-sync is off, so the control that sets
        # one follows the checkbox rather than sitting there editable and inert.
        self._interval_spin.setEnabled(auto_sync)
        self._auto_sync_cb.toggled.connect(self._interval_spin.setEnabled)
        interval_row.addWidget(self._interval_spin)
        interval_row.addStretch()
        outer.addLayout(interval_row)

        outer.addWidget(hint_label(
            "Changed decks apply without asking, apart from a deck whose card template, "
            "look, or note-type format changed, which is held back for a manual run. A "
            "backup is still taken first, the same as a manual sync."))

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
    saved = bool(dlg.exec())
    values = dlg.values()
    dlg.deleteLater()   # its controls are read above; see ui._ask_scrollable
    if not saved:
        return

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
        "Deck sync stays manual, use Update my decks when you're ready.")
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
