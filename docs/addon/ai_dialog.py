"""The "Generate cards with AI" wizard.

A single QDialog holds a QStackedWidget of four pages: setup, input, progress,
review. Nothing here touches the collection until Import (_do_import); review,
editing, notes, and revisions are all in-memory session state, and closing the
dialog mid-review discards it after a confirm (see _GenerateDialog.reject).
"""
import html
import os
import shutil
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
from .net import fetch_card_image
from .ui import (_ask, _ask_scrollable, _info, _prompt, _safe, _warn, hint_label,
                 link_button, title_label)

# Note types a generated card may name. Keep in sync with
# collection._GENERATED_ALLOWED_TYPES: the types this add-on manages, plus
# Anki's own core Basic and Cloze.
FIELD_MAP = dict(TARGET_FIELDS, Basic=["Front", "Back"],
                 Cloze=["Text", "Back Extra"])
SOFT_SOURCE_LIMIT = 25000


def _skills_html(parts):
    """Render View skills' parts (plain multi-line skill text -- model-authored
    and possibly containing literal '<' from the card-craft rules themselves,
    e.g. "an HTML <table>") as HTML a RichText dialog body shows readably.

    Escaped first, so a stray "<table>" or "&lt;94%" in the skill text reads as
    the literal characters it is, rather than being interpreted as markup --
    full transparency means showing exactly what was sent, not a mangled
    rendering of it. '\\n' is then turned into '<br>', since a plain newline
    collapses to a space in HTML and would otherwise run every line together.
    """
    return html.escape("\n".join(parts)).replace("\n", "<br>")


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
        # True when the last revision came back with a different card count than
        # it was sent -- the one shape the prompt promises but nothing verifies.
        # See _finish_generation: this disables the per-index diff entirely.
        self.revision_shape_mismatch = False
        self.tokens_last_run = 0
        self.rate_limits = None


class _GenerateDialog(QDialog):
    def __init__(self):
        super().__init__(mw)
        self.setWindowTitle(f"{APP_NAME}: Generate cards with AI")
        self.setMinimumWidth(480)
        self.session = s = _Session()
        self._retried_json = False   # the single-retry budget on malformed model output
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

        # Labels are per-backend and truthful (see ai_cli.BACKENDS' "modes"), set
        # once a backend is known -- see _refresh_backend_row. What mode
        # enforcement actually exists differs by backend, so one label for all
        # three would misdescribe at least one of them.
        self.thorough_radio = QRadioButton()
        self.quick_radio = QRadioButton()
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
        self.thorough_radio.setText(meta["modes"]["thorough"])
        self.quick_radio.setText(meta["modes"]["quick"])
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
        """Show what's actually sent to the model. Dismissing this dialog (Close,
        Escape, the window's close box) must never itself change consent -- only
        an explicit click on the toggle button may. Rebuilt through _ask_scrollable's
        extra_label/on_extra: that button carries ActionRole, so clicking it runs
        the toggle and leaves the dialog open rather than answering (and closing)
        it, which is what made Escape and "decline" indistinguishable before."""
        deck = load_deck_skill()

        def _body(d):
            parts = ["Bundled: InternPearls authoring (ships with the add-on)", "",
                     ai_logic.load_bundled_skill()]
            if d:
                state = "enabled" if d.get("enabled") else "disabled"
                parts += ["", f"Deck skill v{d.get('version')} ({state}, "
                              f"consented {d.get('consented_on')})", "",
                          d.get("text", "")]
            return _skills_html(parts)

        if not deck:
            _info(_body(deck))
            return

        def _toggle(dlg):
            deck["enabled"] = not deck.get("enabled")
            save_deck_skill(deck)
            return _body(deck)

        label = "Disable deck skill" if deck.get("enabled") else "Enable deck skill"
        _ask_scrollable(_body(deck), yes_label="Close", no_label=None,
                        extra_label=label, on_extra=_toggle)

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

    def _start_generation(self, revision=False, extra_error=None):
        s = self.session
        self._last_revision = revision
        # extra_error is only set on our own re-entry after a malformed reply
        # (see _finish_generation); any other call is a fresh request, so the
        # one-retry budget resets here rather than in the caller.
        if extra_error is None:
            self._retried_json = False
        s.mode = "thorough" if self.thorough_radio.isChecked() else "quick"
        self._duration_estimate = ai_logic.duration_estimate_line(
            load_ai_usage(), s.backend, s.mode)
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
            checks=s.checks if revision else None, mode=s.mode)
        if extra_error:
            prompt += ("\n\n## Your previous reply failed validation\n"
                      + "\n".join(extra_error))
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
        text = f"Elapsed {int(time.monotonic() - self._t0)}s"
        if self._duration_estimate:
            text += " · " + self._duration_estimate
        self.elapsed_label.setText(text)
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
            if not self._retried_json:
                self._retried_json = True
                self._start_generation(revision=self._last_revision,
                                       extra_error=errors)
                return
            _warn("The assistant's reply still could not be used after a "
                  "retry:\n" + "\n".join(errors[:5]))
            self.stack.setCurrentWidget(self.input_page)
            return
        s.tokens_last_run = res["tokens"]
        reg = ai_logic.record_usage(load_ai_usage(), s.backend, res["tokens"],
                                    now=time.time())
        reg = ai_logic.record_duration(reg, s.backend, s.mode, res["duration_s"])
        save_ai_usage(reg)

        # Card matching across a revision, by position: the prompt sends the
        # previous draft and instructs the model to return the SAME cards in the
        # SAME order, marking only the ones without a note "keep verbatim" (see
        # ai_logic.build_prompt). Nothing here can verify that promise was kept,
        # so a card count that doesn't match what was sent is the one signal
        # available that positional matching can no longer be trusted at all.
        # is_revision is False only for the very first draft (nothing to diff
        # against yet); same_shape is False either for that first draft or for
        # a genuine reply-shape mismatch.
        prev_cards, prev_included = s.cards, s.included
        is_revision = bool(prev_cards)
        same_shape = is_revision and len(cards) == len(prev_cards)
        s.revision_shape_mismatch = is_revision and not same_shape

        if same_shape:
            s.updated = {i for i in range(len(cards))
                        if prev_cards[i] != cards[i]}
        elif is_revision:
            # Can't tell which (if any) of the new cards match the old ones, so
            # nothing is claimed "kept verbatim" -- see _rebuild_review's header.
            s.updated = set(range(len(cards)))
        else:
            s.updated = set()

        s.cards = cards
        s.checks = ai_logic.mechanical_checks(
            cards, collection.existing_front_map(_cfg()["scope_tag"]))
        default_included = [not any(c["level"] == "block" for c in per)
                            for per in s.checks]
        if same_shape:
            # A card the revision left verbatim keeps whatever the user set for
            # it (an override she made on purpose survives); only a genuinely
            # new or changed card falls back to the mechanical-check default.
            s.included = [prev_included[i] if i not in s.updated
                         else default_included[i] for i in range(len(cards))]
        else:
            s.included = default_included
        s.notes = {}
        self._rebuild_review()
        self.stack.setCurrentWidget(self.review_page)

    # -- review ------------------------------------------------------------
    def _build_review(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.addWidget(title_label("Review drafted cards"))
        self.review_header = hint_label("")
        lay.addWidget(self.review_header)
        self.cards_lay = QVBoxLayout()
        lay.addLayout(self.cards_lay)
        lay.addWidget(QLabel("Feedback on the whole set (optional)"))
        self.feedback_box = QPlainTextEdit()
        self.feedback_box.setMaximumHeight(60)
        self.feedback_box.setPlaceholderText(
            "e.g. shorter answers, add one card on avoided drugs")
        lay.addWidget(self.feedback_box)
        btn_row = QHBoxLayout()
        back = QPushButton("Back")
        back.clicked.connect(lambda: self.stack.setCurrentWidget(self.input_page))
        self.revise_btn = QPushButton("Revise all")
        self.revise_btn.clicked.connect(self._revise_all)
        self.import_btn = QPushButton("Import")
        self.import_btn.clicked.connect(self._do_import)
        btn_row.addWidget(back)
        btn_row.addStretch(1)
        btn_row.addWidget(self.revise_btn)
        btn_row.addWidget(self.import_btn)
        lay.addLayout(btn_row)
        return page

    def _rebuild_review(self):
        """(Re)populate the review page's card list from session state."""
        s = self.session
        while self.cards_lay.count():
            item = self.cards_lay.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()
        self.include_boxes = []
        for i, card in enumerate(s.cards):
            row = QWidget()
            rowlay = QHBoxLayout(row)
            box = QCheckBox()
            box.setChecked(s.included[i])
            box.toggled.connect(lambda v, i=i: s.included.__setitem__(i, v))
            self.include_boxes.append(box)
            primary = ai_logic.PRIMARY_FIELD.get(card["note_type"], "Front")
            badges = " ".join(f"[{c['code']}]" for c in s.checks[i]
                              if c["level"] != "ok")
            upd = " [updated]" if i in s.updated else ""
            note_txt = f"  (note: {s.notes[i]})" if i in s.notes else ""
            label = QLabel(f"{card['fields'].get(primary, '')[:90]} "
                          f"({card['note_type']}) {badges}{upd}{note_txt}")
            label.setWordWrap(True)
            edit_btn = QPushButton("Edit")
            edit_btn.clicked.connect(lambda _, i=i: self._edit_card(i))
            note_btn = QPushButton("Note")
            note_btn.clicked.connect(lambda _, i=i: self._note_card(i))
            rowlay.addWidget(box)
            rowlay.addWidget(label, 1)
            rowlay.addWidget(edit_btn)
            rowlay.addWidget(note_btn)
            self.cards_lay.addWidget(row)
        n_inc = sum(s.included)
        extra = (f" · last run ~{round(s.tokens_last_run / 1000)}k tokens"
                if s.tokens_last_run else "")
        if s.rate_limits:
            extra += " · " + ai_logic.rate_limit_line(s.rate_limits)
        if s.revision_shape_mismatch:
            # Degrade honestly: a card count that doesn't match what was sent
            # means the per-index diff isn't trustworthy, so this says so
            # instead of showing a confident "kept N verbatim" that could be wrong.
            extra += (" · the assistant returned a different number of cards "
                     "than before, so nothing here is marked kept-verbatim")
        elif s.updated:
            kept = len(s.cards) - len(s.updated)
            extra += f" · updated {len(s.updated)}, kept {kept} verbatim"
        self.review_header.setText(
            f"Review {len(s.cards)} draft cards · {n_inc} included{extra}")
        self.import_btn.setText(f"Import {n_inc} cards")
        self.revise_btn.setText(
            "Revise all" + (f" ({len(s.notes)} notes)" if s.notes else ""))

    def _edit_card(self, i):
        """Hand-edit one card's fields, right in the review list. Nothing here
        touches the model: this is a plain in-memory edit, prompted field by
        field, cancellable at any field without losing the ones already typed."""
        card = self.session.cards[i]
        for name in FIELD_MAP[card["note_type"]]:
            new = _prompt(f"{name}:", default=card["fields"].get(name, ""))
            if new is None:
                return
            card["fields"][name] = new
        self._rebuild_review()

    def _note_card(self, i):
        """Queue (or clear) a per-card revision note. This never calls the
        model by itself -- it only marks the card for the next Revise all,
        which is what sends every queued note in one turn (see _revise_all)."""
        note = _prompt("Revision note for this card:",
                       default=self.session.notes.get(i, ""))
        if note is not None:
            if note.strip():
                self.session.notes[i] = note.strip()
            else:
                self.session.notes.pop(i, None)
            self._rebuild_review()

    def _revise_all(self):
        """The only path that sends card-level feedback to the model: one CLI
        turn carrying the set-level feedback plus every queued note, with
        every un-noted card marked keep-verbatim (see ai_logic.build_prompt)."""
        self._start_generation(revision=True)

    def _do_import(self):
        """Write the included cards to the collection. Resolves each image
        source to bytes here -- nothing before this point ever touched the
        network or the collection -- and hands add_generated_notes the bytes
        plus the filenames it chose, per its documented media contract.

        Nothing selected is not an import: it neither writes anything nor
        closes the wizard (there is no undo step to claim), so the draft
        stays on the review page instead of being silently discarded.

        svg_index is a running counter across the WHOLE batch, not per card:
        svg_to_media names a file solely from the index passed in, so two
        cards each drawing their own SVG would collide on the same filename
        (and one image silently overwrite the other) if each card started
        counting from 0 again. A url:/attached: image can't collide with an
        svg: one either, since fetch_card_image only ever returns a raster
        extension and an attached name is scoped to its own scratch dir.
        """
        s = self.session
        cards = [c for c, inc in zip(s.cards, s.included) if inc]
        if not cards:
            _info("Nothing is selected to import. Check at least one card, "
                  "or Cancel to discard the draft.")
            return 0
        media = {}
        svg_index = 0
        for idx, card in enumerate(cards):
            files = []
            for im in card["images"]:
                src = im["source"]
                try:
                    if src.startswith("svg:"):
                        name, data = ai_logic.svg_to_media(src[4:], svg_index)
                        svg_index += 1
                    elif src.startswith("url:"):
                        data, ext = fetch_card_image(src[4:])
                        name = f"generated-{idx}-{len(files)}.{ext}"
                    elif src.startswith("attached:"):
                        name = src.split(":", 1)[1]
                        with open(os.path.join(s.scratch, name), "rb") as fh:
                            data = fh.read()
                    else:
                        raise ValueError(f"unrecognized image source: {src!r}")
                    media[name] = data
                    files.append(name)
                except Exception as e:
                    # One bad image is not worth losing the whole card over --
                    # warn and move on, same as a mechanical check would flag it.
                    _warn(f"Skipping an image on card {idx + 1}: {e}")
            card["_media_files"] = files
        # add_generated_notes can raise (e.g. Basic/Cloze missing or renamed on a
        # non-English profile). Every attached:/PDF-embedded image this import
        # will ever read has already been read into `media` above, but cleanup
        # waits until AFTER a successful import: if this raises, the scratch dir
        # -- and every attached: image path a retry would need to resolve --
        # must still be there.
        n = collection.add_generated_notes(cards, media, s.deck_name,
                                           _cfg()["scope_tag"])
        self._cleanup_scratch()
        _info(f"{n} cards added to {s.deck_name}. This is one undo step: "
              "Ctrl+Z reverts it.")
        self.accept()
        return n

    def _cleanup_scratch(self):
        """Remove the session's scratch directory (extracted attachment images,
        PDF-embedded images -- real source material) on every path that ends
        this dialog. Best-effort: a failure to delete must never raise into the
        user's face or block the dialog from closing, and nothing about a
        session may outlive it (see the module docstring's storage policy)."""
        if self.session.scratch:
            shutil.rmtree(self.session.scratch, ignore_errors=True)
            self.session.scratch = None

    def reject(self):
        """Closing mid-review discards everything unsaved -- nothing about a
        draft, a note, or a prompt is ever written to disk (see the module
        docstring), so this confirmation is the only chance to back out."""
        if (self.stack.currentWidget() is self.review_page
                and self.session.cards
                and not _ask(
                    "Discard the drafted cards? Nothing from this session "
                    "is saved between sessions.",
                    yes_label="Discard", no_label="Keep editing")):
            return
        self._cleanup_scratch()
        super().reject()


@_safe
def generate_cards():
    _GenerateDialog().exec()
