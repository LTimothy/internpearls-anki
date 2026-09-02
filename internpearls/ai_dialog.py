"""The "Generate cards with AI" wizard.

A single QDialog holds a QStackedWidget of four pages: setup, input, progress,
review. This file builds the setup and input pages; the progress and review
pages here are placeholders, filled in by later work.
"""
import os
import tempfile
import time

from aqt import mw
from aqt.qt import (QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel,
                    QPlainTextEdit, QPushButton, QRadioButton, QSpinBox,
                    QStackedWidget, QVBoxLayout, QWidget)

from . import ai_cli, ai_logic
from .config import (APP_NAME, TARGET_FIELDS, _cfg, load_ai_usage,
                     load_deck_skill, save_deck_skill)
from .ui import _ask_scrollable, _info, _warn, hint_label, link_button, title_label

# Note types a generated card may name. Keep in sync with
# collection._GENERATED_ALLOWED_TYPES: the types this add-on manages, plus
# Anki's own core Basic and Cloze.
FIELD_MAP = dict(TARGET_FIELDS, Basic=["Front", "Back"],
                 Cloze=["Text", "Back Extra"])
SOFT_SOURCE_LIMIT = 25000


class _Session:
    """State shared by every page of the wizard."""

    def __init__(self):
        self.backend = None          # kind string
        self.cli_path = None
        self.mode = "thorough"       # or "quick"
        self.source = ""
        self.instructions = ""
        self.count = 10
        self.note_types = []         # selected type names
        self.deck_name = ""
        self.attachments = []        # [(path, {"text", "images"})]
        self.scratch = None          # tempfile.mkdtemp for this session
        self.cards = []              # current draft (ai_logic card dicts)
        self.included = []           # bool per card
        self.notes = {}              # {index: revision note}
        self.checks = []             # mechanical_checks output
        self.updated = set()         # indexes changed by last revision
        self.tokens_last_run = 0
        self.rate_limits = None


class _GenerateDialog(QDialog):
    def __init__(self):
        super().__init__(mw)
        self.setWindowTitle(f"{APP_NAME}: Generate cards with AI")
        self.setMinimumWidth(480)
        self.session = s = _Session()
        cfg = _cfg()
        s.deck_name = cfg["export_deck"] + "::" + ai_logic.GENERATED_DECK_LEAF

        self.stack = QStackedWidget()
        lay = QVBoxLayout(self)
        lay.addWidget(self.stack)

        self.setup_page = self._build_setup()
        self.input_page = self._build_input()
        self.progress_page = self._build_progress()
        self.review_page = self._build_review()
        for page in (self.setup_page, self.input_page,
                    self.progress_page, self.review_page):
            self.stack.addWidget(page)

        self._detect(cfg)

    # -- setup ---------------------------------------------------------------
    def _build_setup(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.addWidget(title_label("Set up an AI assistant"))
        lay.addWidget(hint_label(
            "Card generation runs through an assistant you sign into yourself. "
            "This add-on never sees or stores your credentials: there is no API "
            "key field here, or anywhere else in the add-on."))
        self.setup_rows = {}
        for kind, meta in ai_cli.BACKENDS.items():
            row = QLabel()
            row.setWordWrap(True)
            self.setup_rows[kind] = row
            lay.addWidget(row)
        self.recheck_btn = QPushButton("Re-check")
        self.recheck_btn.clicked.connect(lambda: self._detect(_cfg()))
        lay.addWidget(self.recheck_btn)
        lay.addWidget(hint_label(
            "Install one of these, run it once in a terminal, and sign in "
            "there yourself. Then come back and re-check."))
        return page

    def _detect(self, cfg):
        s = self.session
        s.backend, s.cli_path = None, None
        preferred = cfg.get("ai_backend", "")
        for kind, meta in ai_cli.BACKENDS.items():
            override = cfg.get("ai_cli_path", "") if preferred == kind else ""
            path = ai_cli.find_cli(kind, override)
            if path:
                res = ai_cli.probe(kind, path)
                ok = res["ok"]
                status = res["detail"] if ok else "found, but not working"
            else:
                ok, status = False, "not found"
            imgs = ("can view attached images" if ai_cli.image_capable(kind)
                    else "reads attached PDFs as text only, no images")
            # The safety string is shown verbatim, per-backend: the three
            # backends are not equally safe and this is not the place to
            # soften or average that out.
            self.setup_rows[kind].setText(
                f"{meta['label']} ({meta['subscription']}): {status}\n"
                f"{meta['safety']}. {imgs}.")
            if ok and (s.backend is None or preferred == kind):
                s.backend, s.cli_path = kind, path
        self.stack.setCurrentWidget(
            self.input_page if s.backend else self.setup_page)
        if s.backend:
            self._refresh_backend_row()

    # -- input -----------------------------------------------------------------
    def _build_input(self):
        page = QWidget()
        lay = QVBoxLayout(page)

        self.source_box = QPlainTextEdit()
        self.source_box.setPlaceholderText(
            "Paste lecture notes, an article excerpt, or a topic outline")
        self.source_box.textChanged.connect(self._source_changed)
        lay.addWidget(QLabel("Source material"))
        lay.addWidget(self.source_box)
        self.char_label = hint_label("0 characters")
        lay.addWidget(self.char_label)

        self.attach_btn = QPushButton("Attach images or PDFs")
        self.attach_btn.clicked.connect(self._attach)
        self.attach_label = hint_label("")
        attach_row = QHBoxLayout()
        attach_row.addWidget(self.attach_btn)
        attach_row.addWidget(self.attach_label)
        lay.addLayout(attach_row)

        self.instructions_box = QPlainTextEdit()
        self.instructions_box.setMaximumHeight(60)
        self.instructions_box.setPlaceholderText(
            'Anything specific to focus on, e.g. "emphasize dosing"')
        lay.addWidget(QLabel("Instructions (optional)"))
        lay.addWidget(self.instructions_box)

        self.thorough_radio = QRadioButton(
            "Thorough: drafts, verifies facts online, then self-reviews "
            "(1 to 3 min)")
        self.quick_radio = QRadioButton(
            "Quick draft: one pass, no web access (15 to 30 s)")
        self.thorough_radio.setChecked(True)
        lay.addWidget(QLabel("Quality"))
        lay.addWidget(self.thorough_radio)
        lay.addWidget(self.quick_radio)

        self.count_spin = QSpinBox()
        self.count_spin.setRange(1, 50)
        self.count_spin.setValue(10)
        count_row = QHBoxLayout()
        count_row.addWidget(QLabel("Target count"))
        count_row.addWidget(self.count_spin)
        lay.addLayout(count_row)

        self.type_boxes = {}
        lay.addWidget(QLabel("Note types"))
        for name in FIELD_MAP:
            box = QCheckBox(name)
            # Image ID cards need a real matching image, which generation
            # can't reliably supply, so it starts unchecked rather than off.
            box.setChecked(name != "Study Deck - Image ID")
            self.type_boxes[name] = box
            lay.addWidget(box)

        self.deck_combo = QComboBox()
        self.deck_combo.setEditable(True)
        self.deck_combo.addItem(self.session.deck_name)
        lay.addWidget(QLabel("Destination deck"))
        lay.addWidget(self.deck_combo)

        self.backend_row = hint_label("")
        lay.addWidget(self.backend_row)
        self.skills_link = link_button("View skills", on_click=self._view_skills)
        lay.addWidget(self.skills_link)
        self.usage_row = hint_label("")
        lay.addWidget(self.usage_row)

        btn_row = QHBoxLayout()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.generate_btn = QPushButton("Generate")
        self.generate_btn.setEnabled(False)
        # Wired up once the worker thread lands; a click does nothing yet.
        btn_row.addStretch(1)
        btn_row.addWidget(cancel)
        btn_row.addWidget(self.generate_btn)
        lay.addLayout(btn_row)
        return page

    def _source_changed(self):
        text = self.source_box.toPlainText()
        n = len(text)
        warn = "; consider splitting the material" if n > SOFT_SOURCE_LIMIT else ""
        self.char_label.setText(f"{n:,} characters{warn}")
        self.generate_btn.setEnabled(bool(text.strip()))

    def _refresh_backend_row(self):
        s = self.session
        meta = ai_cli.BACKENDS[s.backend]
        self.backend_row.setText(f"Backend: {meta['label']} (signed in)")
        reg = load_ai_usage()
        self.usage_row.setText(ai_logic.usage_line(
            reg, s.backend, now=time.time(), free_tier=(s.backend == "agy")))

    def _attach(self):
        from aqt.qt import QFileDialog
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Attach source files", "",
            "Images and PDFs (*.png *.jpg *.jpeg *.webp *.gif *.pdf)")
        s = self.session
        if s.scratch is None:
            s.scratch = tempfile.mkdtemp(prefix="ip-aigen-")
        for p in paths:
            try:
                s.attachments.append((p, ai_logic.extract_attachment(p, s.scratch)))
            except ValueError as e:
                _warn(str(e))
        self.attach_label.setText(
            ", ".join(os.path.basename(p) for p, _ in s.attachments))

    def _view_skills(self):
        deck = load_deck_skill()
        parts = ["Bundled: InternPearls authoring (ships with the add-on)", "",
                 ai_logic.load_bundled_skill()]
        if deck:
            state = "enabled" if deck.get("enabled") else "disabled"
            parts += ["", f"Deck skill v{deck.get('version')} ({state}, "
                          f"consented {deck.get('consented_on')})", "",
                      deck.get("text", "")]
        if deck and _ask_scrollable(
                "\n".join(parts), yes_label="Close",
                no_label=("Disable deck skill" if deck.get("enabled")
                          else "Enable deck skill")) is False:
            deck["enabled"] = not deck.get("enabled")
            save_deck_skill(deck)
        elif not deck:
            _info("\n".join(parts))

    # -- placeholders, built out fully later ----------------------------------
    def _build_progress(self):
        page = QWidget()
        QVBoxLayout(page).addWidget(QLabel("Generating cards"))
        return page

    def _build_review(self):
        page = QWidget()
        QVBoxLayout(page).addWidget(QLabel("Review drafted cards"))
        return page
