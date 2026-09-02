"""The "Generate cards with AI" wizard.

A single QDialog holds a QStackedWidget of four pages: setup, input, progress,
review. This file builds the setup and input pages; the progress and review
pages here are placeholders, filled in by later work.
"""
import os
import tempfile
import threading
import time
from collections import deque

from aqt import mw
from aqt.qt import (QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel,
                    QPlainTextEdit, QPushButton, QRadioButton, QSpinBox,
                    QStackedWidget, QTimer, QVBoxLayout, QWidget)

from . import ai_cli, ai_logic, collection
from .config import (APP_NAME, TARGET_FIELDS, _cfg, load_ai_usage,
                     save_ai_usage, load_deck_skill, save_deck_skill)
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
        self.generate_btn.clicked.connect(self._start_generation)
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

    # -- progress --------------------------------------------------------------
    def _build_progress(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.addWidget(title_label("Generating cards"))
        self.progress_label = QLabel("Starting")
        self.phase_label = hint_label("")
        self.elapsed_label = hint_label("")
        lay.addWidget(self.progress_label)
        lay.addWidget(self.phase_label)
        lay.addWidget(self.elapsed_label)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self._cancel_generation)
        lay.addWidget(cancel)
        return page

    def _start_generation(self, revision=False):
        s = self.session
        s.mode = "thorough" if self.thorough_radio.isChecked() else "quick"
        s.source = self.source_box.toPlainText()
        s.instructions = self.instructions_box.toPlainText()
        s.count = self.count_spin.value()
        s.note_types = [n for n, b in self.type_boxes.items() if b.isChecked()]
        s.deck_name = self.deck_combo.currentText().strip() or s.deck_name
        if s.scratch is None:
            s.scratch = tempfile.mkdtemp(prefix="ip-aigen-")
        extra_text = "\n\n".join(a[1]["text"] for a in s.attachments if a[1]["text"])
        image_names = [name for _, meta in s.attachments for name in meta["images"]]
        prompt = ai_logic.build_prompt(
            skills=ai_logic.active_skills(load_deck_skill()),
            source=(s.source + ("\n\n## Attached document text\n" + extra_text
                                if extra_text else "")),
            note_types=s.note_types, field_map=FIELD_MAP, count=s.count,
            instructions=s.instructions,
            attachments=image_names if ai_cli.image_capable(s.backend) else [],
            cards=s.cards if revision else None,
            feedback=self.feedback_box.toPlainText() if revision else "",
            notes=s.notes if revision else None,
            checks=s.checks if revision else None)
        # A deque, not a plain list: the worker (producer) appends and the
        # poller (consumer) pops from the other end, so each event's removal
        # is one atomic op with no gap between "read" and "clear" where a
        # concurrently appended event could be silently dropped.
        self._events = deque()
        self._worker_error, self._worker_result = None, None
        self._gen_done = False
        self._cancel_flag = threading.Event()
        self._t0 = time.monotonic()
        image_paths = ([os.path.join(s.scratch, n) for n in image_names]
                       if ai_cli.image_capable(s.backend) else [])

        def work():
            try:
                self._worker_result = ai_cli.run_generation(
                    s.backend, s.cli_path, prompt, s.mode, s.scratch,
                    image_paths=image_paths, on_event=self._events.append,
                    cancel=self._cancel_flag.is_set)
            except Exception as e:
                self._worker_error = e

        self._worker = threading.Thread(target=work, daemon=True)
        self._worker.start()
        self.progress_label.setText(
            "Drafting cards with " + ai_cli.BACKENDS[s.backend]["label"])
        self.phase_label.setText("")
        self.stack.setCurrentWidget(self.progress_page)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll_worker)
        self._timer.start(200)

    def _poll_worker(self):
        # A stopped QTimer can still be fired once more by a queued signal, or
        # (in the test mock, which has no real event loop) by unrelated code
        # holding a reference to it; this generation cycle only ever runs
        # _finish_generation once.
        if self._gen_done:
            return
        while self._events:
            evt = self._events.popleft()
            if evt["type"] == "phase":
                self.phase_label.setText(evt["phase"])
            elif evt["type"] == "rate_limits":
                self.session.rate_limits = evt
        self.elapsed_label.setText(
            f"Elapsed {int(time.monotonic() - self._t0)}s")
        if self._worker.is_alive():
            return
        self._timer.stop()
        self._gen_done = True
        self._finish_generation()

    def _wait_for_worker(self, timeout=15):
        """Test helper: run the poll loop synchronously, without a live QTimer."""
        end = time.time() + timeout
        while self._worker.is_alive() and time.time() < end:
            time.sleep(0.05)
        self._timer.stop()
        self._gen_done = True
        self._finish_generation()

    def _cancel_generation(self):
        self._cancel_flag.set()

    def _finish_generation(self):
        s = self.session
        err, res = self._worker_error, self._worker_result
        if isinstance(err, ai_cli.GenerationCancelled):
            self.stack.setCurrentWidget(self.input_page)
            return
        if err or not res:
            _warn(f"Generation failed: {err}")
            self.stack.setCurrentWidget(self.input_page)
            return
        cards, errors = ai_logic.parse_cards_json(res["text"], s.note_types, FIELD_MAP)
        if errors:
            _warn("The assistant's reply could not be used:\n" + "\n".join(errors[:5]))
            self.stack.setCurrentWidget(self.input_page)
            return
        s.tokens_last_run = res["tokens"]
        reg = ai_logic.record_usage(load_ai_usage(), s.backend, res["tokens"],
                                    now=time.time())
        save_ai_usage(reg)
        prev = {i: c for i, c in enumerate(s.cards)}
        s.updated = ({i for i, c in enumerate(cards)
                     if prev.get(i) and prev[i] != c} if prev else set())
        s.cards = cards
        s.checks = ai_logic.mechanical_checks(
            cards, collection.existing_front_map(_cfg()["scope_tag"]))
        s.included = [not any(c["level"] == "block" for c in per)
                     for per in s.checks]
        s.notes = {}
        self._rebuild_review()
        self.stack.setCurrentWidget(self.review_page)

    # -- review ------------------------------------------------------------
    def _rebuild_review(self):
        """Populate the review page from session state. Built out in full by
        the next stage; this stage only needs the seam _finish_generation calls."""

    def _build_review(self):
        page = QWidget()
        QVBoxLayout(page).addWidget(QLabel("Review drafted cards"))
        return page
