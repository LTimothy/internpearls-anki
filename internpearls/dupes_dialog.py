"""Scan for duplicates: lexical duplicate candidates across two scopes of the
collection, on the update screen's own row vocabulary (chip, bold line, muted detail,
trailing links, rules between rows), plus an optional one-turn AI judging pass.

The scan itself (`dupes.find_candidates`) is pure CPU work with no collection access,
so it runs on a background thread while a busy line with elapsed time covers it; the
collection reads that feed it, and the only write this dialog ever makes (suspending a
card), happen on the main thread, through collection.py's own API. Judging with AI runs
the same way: one CLI call on a background thread, a busy line with elapsed time rather
than the AI wizard's own activity feed, since reusing that feed's session-driven state
machine here would be a rewrite of this screen for a single optional button.
"""
import tempfile
import threading
import time

from aqt import mw
from aqt.qt import (QComboBox, QDialog, QDialogButtonBox, QFrame,
                    QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea, Qt,
                    QTimer, QVBoxLayout, QWidget)

from . import ai_cli, ai_logic
from .collection import note_rows, suspend_notes
from .config import (APP_NAME, _cfg, add_dupes_ignored, set_dupes_excluded_decks,
                     set_dupes_threshold)
from .dupes import find_candidates, pair_key
from .logic import field_preview_text, plain_text
from .palette import colors
from .ui import _safe, copy_to_clipboard, hint_label, link_button, section_label, title_label
from .widgets import CARET_GAP, CARET_W

# This dialog's own chip vocabulary. Unlike widgets.CHIPS, a candidate's label carries
# its own band and score ("Similar 0.64"), so it can't be one of a fixed finite set of
# labels the way every other screen's chips are. The column is still measured the way
# widgets.chip_column_width measures one, just against this dialog's own fixed set of
# exemplar labels rather than CHIPS: "Likely duplicate 0.00" is the widest a real band
# label can render (the score is always two decimal places, see `_band_label`), so the
# set never has to be recomputed against whatever pairs happen to be on screen.
_CHIP_STYLE = ("border-radius: 3px; padding: 1px 6px; font-size: 11px; font-weight: 600;")
_ROLES = {"candidate": "new", "duplicate": "accept", "overlaps": "updated",
         "suspended": "retired"}
_CHIP_LABELS = ("Likely duplicate 0.00", "DUPLICATE", "OVERLAPS", "SUSPENDED")

# The Sensitivity combo: label shown, and the cosine threshold it sets. Order matches
# the combo's own item order, so an index round-trips straight into this list.
_SENSITIVITY_LEVELS = (("Strict", 0.6), ("Normal", 0.5), ("Loose", 0.4))

# chip_column_width()'s answer, measured once: not computed at import, since these
# modules load before a QApplication exists and font metrics before that point are
# meaningless (see widgets._CHIP_W).
_CHIP_W = {}

_HINT = ("Compares two scopes of your collection by the words each card actually uses, "
        "so a paraphrase counts even when the wording doesn't match.")

_SCORE_HINT = ("The score is how much of the rare vocabulary the two cards share, from "
              "0 to 1; common words count for little, drug names and numbers count for "
              "a lot.")


def _band_label(score):
    """The score's readability band, plus the number itself: 'Likely duplicate 0.77',
    'Similar 0.64', 'Weak match 0.52'. Bands, not a bare number, are what tells a
    reader whether a pair is worth looking at without doing the math themselves."""
    if score >= 0.7:
        band = "Likely duplicate"
    elif score >= 0.6:
        band = "Similar"
    else:
        band = "Weak match"
    return f"{band} {score:.2f}"


def _pill(text, kind):
    lbl = QLabel(text)
    c = colors()
    role = _ROLES[kind]
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lbl.setStyleSheet(f"background-color: {c[f'{role}_bg']}; color: {c[f'{role}_fg']};"
                      f" {_CHIP_STYLE}")
    return lbl


def _chip_label(pair):
    if pair.get("suspended"):
        return "SUSPENDED", "suspended"
    judged = pair.get("judged")
    if judged == "same":
        return "DUPLICATE", "duplicate"
    if judged == "overlaps":
        return "OVERLAPS", "overlaps"
    return _band_label(pair["score"]), "candidate"


def _chip_column_width():
    """The width of this dialog's chip column, measured against the widest label it can
    ever show (see `_CHIP_LABELS`) the same way widgets.chip_column_width measures one:
    every pill in the column widens to match, so the column reads as one edge rather
    than a different right edge per row."""
    if "w" not in _CHIP_W:
        widest = 0
        for label in _CHIP_LABELS:
            probe = QLabel(label)
            probe.setStyleSheet(_CHIP_STYLE)
            probe.ensurePolished()
            widest = max(widest, probe.sizeHint().width())
        _CHIP_W["w"] = widest or 1
    return _CHIP_W["w"]


def _row_text_indent():
    """How far a row's primary text sits from the row's own left edge: the caret
    column, then the chip column and its gap. Same arithmetic as
    widgets.row_text_indent, for this dialog's one leading chip column."""
    return CARET_W + CARET_GAP + _chip_column_width() + CARET_GAP


def _row_rule():
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFrameShadow(QFrame.Shadow.Plain)
    line.setFixedHeight(1)
    line.setStyleSheet(f"color: {colors()['row_rule']};")
    return line


def _note_texts(nid):
    """A note's front and back as plain text. A front that is only a picture
    (an image-identification note) is named rather than left blank."""
    note = mw.col.get_note(nid)
    fields = list(note.fields)
    front = field_preview_text(fields[0]) if fields else ""
    if not front.strip():
        front = next((field_preview_text(f) for f in fields[1:]
                      if field_preview_text(f).strip()), "")
    back = plain_text(fields[1]) if len(fields) > 1 else ""
    return front, back


def _esc(text):
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class _DuplicateScanDialog(QDialog):
    def __init__(self, scope_tag):
        super().__init__(mw)
        self._scope_tag = scope_tag
        self.setWindowTitle(f"{APP_NAME}: Scan for duplicates")
        # Wide enough for the per-row action links (Suspend ours/theirs, Keep both,
        # Ignore pair) to sit beside a two-line "ours:"/"theirs:" label without
        # squeezing it down to a many-line wrap: at 640px the four links alone claimed
        # over half the row's width.
        self.setMinimumSize(800, 560)
        self._pairs = []
        self._left_count = self._right_count = 0
        self._deck_names = sorted({d.name for d in mw.col.decks.all_names_and_ids()})
        self._fold_open = False
        self._any_excluded = False
        self._right_dropped = 0
        self._last_scanned_exclude_text = None

        outer = QVBoxLayout(self)
        outer.addWidget(title_label("Scan for duplicates"))
        outer.addWidget(hint_label(_HINT))

        scope_row = QHBoxLayout()
        self.left_combo = QComboBox()
        self.left_combo.addItem("Cards this add-on manages")
        for name in self._deck_names:
            self.left_combo.addItem(name)
        self.right_combo = QComboBox()
        self.right_combo.addItem("Everything else")
        for name in self._deck_names:
            self.right_combo.addItem(name)
        scope_row.addWidget(self.left_combo, 1)
        scope_row.addWidget(QLabel("vs."))
        scope_row.addWidget(self.right_combo, 1)
        outer.addLayout(scope_row)

        links_row = QHBoxLayout()
        links_row.addWidget(link_button("Rescan", self._rescan))
        cfg = _cfg()
        links_row.addWidget(QLabel("Sensitivity:"))
        self.sensitivity_combo = QComboBox()
        for label, _ in _SENSITIVITY_LEVELS:
            self.sensitivity_combo.addItem(label)
        current_threshold = cfg["dupes_threshold"]
        idx = next((i for i, (_, v) in enumerate(_SENSITIVITY_LEVELS)
                   if v == current_threshold), 1)
        self.sensitivity_combo.setCurrentIndex(idx)
        self.sensitivity_combo.currentIndexChanged.connect(self._sensitivity_changed)
        links_row.addWidget(self.sensitivity_combo)
        chosen = ai_cli.detect_backends(cfg)["chosen"]
        self._judge_backend = chosen
        self.judge_btn = link_button("Judge with AI", self._judge_with_ai)
        self.judge_btn.setEnabled(bool(chosen))
        if not chosen:
            self.judge_btn.setToolTip("Set up an AI backend first (Generate cards (AI)"
                                      " > Setup).")
        links_row.addWidget(self.judge_btn)
        links_row.addStretch()
        outer.addLayout(links_row)

        outer.addWidget(section_label("Exclude decks"))
        self.exclude_edit = QLineEdit(", ".join(_cfg()["dupes_excluded_decks"]))
        self.exclude_edit.setPlaceholderText("deck names or parts of names, comma separated")
        self.exclude_edit.editingFinished.connect(self._exclude_edited)
        outer.addWidget(self.exclude_edit)

        self.summary_label = hint_label("")
        outer.addWidget(self.summary_label)
        outer.addWidget(hint_label(_SCORE_HINT))

        self._rows_container = QWidget()
        self._rows_layout = QVBoxLayout(self._rows_container)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(0)
        # Rows sit in their own container, separate from the stretch below it, the same
        # split StreamingList uses: rebuilding the list only ever touches the rows
        # layout, so the stretch never needs to be found and re-added. Without it, a
        # short list has nothing to consume the scroll area's leftover height, so
        # QScrollArea's widgetResizable stretches the rows themselves to fill it
        # instead, one row at a time, wider than their own content.
        rows_body = QWidget()
        rows_outer = QVBoxLayout(rows_body)
        rows_outer.setContentsMargins(0, 0, 0, 0)
        rows_outer.setSpacing(0)
        rows_outer.addWidget(self._rows_container)
        rows_outer.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        # Always-on vertical, always-off horizontal: a policy-driven scrollbar that
        # only appears once content overflows narrows the viewport when it shows up,
        # which reflows these word-wrapped labels taller, which can push content past
        # the threshold that made the bar appear in the first place, and so on. A
        # bar that never appears or disappears has nothing left to oscillate.
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(rows_body)
        outer.addWidget(scroll, 1)

        outer.addWidget(link_button("Copy list", self._copy_list, align_left=True))

        bb = QDialogButtonBox()
        bb.addButton("Close", QDialogButtonBox.ButtonRole.RejectRole)
        bb.rejected.connect(self.reject)
        outer.addWidget(bb)

        self._rescan()

    # ------------------------------------------------------------------ scope
    def _excluded_decks(self):
        return [t.strip() for t in self.exclude_edit.text().split(",") if t.strip()]

    def _apply_exclusions(self, rows):
        excluded = [e.lower() for e in self._excluded_decks()]
        if not excluded:
            return rows
        filtered = [r for r in rows
                   if not any(e in r[2].lower() for e in excluded)]
        if len(filtered) != len(rows):
            self._any_excluded = True
        return filtered

    def _exclude_edited(self):
        """editingFinished fires on any focus loss, not just a real edit, so clicking
        into the candidate list after typing used to rescan and rebuild the rows
        underfoot. Only rescan when the text actually moved since the last scan;
        Enter and the Rescan link both still work, since the text before either has
        already changed (Enter) or Rescan bypasses this gate entirely."""
        text = self.exclude_edit.text()
        if text == self._last_scanned_exclude_text:
            return
        set_dupes_excluded_decks(self._excluded_decks())
        self._rescan()

    def _sensitivity_changed(self, index):
        set_dupes_threshold(_SENSITIVITY_LEVELS[index][1])
        self._rescan()

    def _left_rows(self):
        if self.left_combo.currentIndex() == 0:
            return note_rows(mw.col, scope_tag=self._scope_tag)
        deck = self._deck_names[self.left_combo.currentIndex() - 1]
        return self._apply_exclusions(note_rows(mw.col, deck_name=deck))

    def _right_rows(self):
        if self.right_combo.currentIndex() == 0:
            left_ids = {r[0] for r in self._left_rows()}
            rows = [r for r in note_rows(mw.col) if r[0] not in left_ids]
        else:
            deck = self._deck_names[self.right_combo.currentIndex() - 1]
            rows = note_rows(mw.col, deck_name=deck)
        filtered = self._apply_exclusions(rows)
        self._right_dropped = len(rows) - len(filtered)
        return filtered

    # ------------------------------------------------------------------ scan
    @_safe
    def _rescan(self, *_):
        self._any_excluded = False
        self._last_scanned_exclude_text = self.exclude_edit.text()
        left_rows = self._left_rows()
        right_rows = self._right_rows()
        self._left_count, self._right_count = len(left_rows), len(right_rows)
        self._fold_open = False
        self.summary_label.setText("Scanning...")
        self._scan_result = None
        self._scan_error = None
        self._t0 = time.monotonic()
        threshold = _cfg()["dupes_threshold"]

        def work():
            try:
                self._scan_result = find_candidates(left_rows, right_rows,
                                                    threshold=threshold, top=3)
            except Exception as e:
                self._scan_error = e

        self._worker = threading.Thread(target=work, daemon=True)
        self._worker.start()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll_scan)
        self._timer.start(100)

    def _poll_scan(self):
        if self._worker.is_alive():
            elapsed = int(time.monotonic() - self._t0)
            self.summary_label.setText(f"Scanning... {elapsed}s elapsed")
            return
        self._timer.stop()
        self._finish_scan()

    def _finish_scan(self):
        if self._scan_error:
            self.summary_label.setText(f"Scan failed: {self._scan_error}")
            return
        ignored = set(_cfg()["dupes_ignored"])
        self._pairs = []
        for score, left, right, shares in self._scan_result:
            key = pair_key(left[0], right[0])
            if key in ignored:
                continue
            self._pairs.append({"score": score, "left": left, "right": right,
                               "key": key, "judged": None, "note": "",
                               "suspended": set(), "shares": shares})
        self._rebuild_list()

    def _wait_for_scan(self, timeout=15):
        """Test helper: run the scan to completion synchronously, without a live
        QTimer, the same shape ai_dialog._wait_for_worker uses."""
        end = time.time() + timeout
        while self._worker.is_alive() and time.time() < end:
            time.sleep(0.02)
        self._timer.stop()
        self._finish_scan()

    # ------------------------------------------------------------------ list
    def _exclusion_feedback(self):
        """Two lines about the Exclude decks field, or none: whether it actually did
        anything ("Excluding 1 deck (2,903 cards)"), and, separately, any entry that
        matched no deck in the collection at all ("No deck matches 'CC Anki'", in the
        warning colour) rather than the field silently doing nothing. Without this a
        typo in the field looked identical to it working."""
        entries = self._excluded_decks()
        if not entries:
            return None, None
        deck_names_lower = [d.lower() for d in self._deck_names]
        unmatched = [e for e in entries
                    if not any(e.lower() in name for name in deck_names_lower)]
        matched = [e for e in entries if e not in unmatched]
        info = None
        if matched:
            n = len(matched)
            dropped = self._right_dropped
            info = (f"Excluding {n} deck{'s' if n != 1 else ''} "
                    f"({dropped:,} card{'s' if dropped != 1 else ''})")
        warn = None
        if unmatched:
            c = colors()
            names = "; ".join(f"No deck matches '{_esc(e)}'" for e in unmatched)
            warn = f"<span style='color:{c['warning']};'>{names}</span>"
        return info, warn

    def _summary_text(self):
        text = (f"{self._left_count} scanned against {self._right_count}, "
               f"{len(self._pairs)} candidates")
        same = sum(1 for p in self._pairs if p["judged"] == "same")
        if same:
            text += f", {same} judged the same"
        lines = [text]
        info, warn = self._exclusion_feedback()
        if info:
            lines.append(info)
        if warn:
            lines.append(warn)
        return "<br>".join(lines)

    def _rebuild_list(self):
        self.summary_label.setText(self._summary_text())
        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        shown = [p for p in self._pairs if p["judged"] != "different"]
        different = [p for p in self._pairs if p["judged"] == "different"]

        for i, pair in enumerate(shown):
            if i:
                self._rows_layout.addWidget(_row_rule())
            self._rows_layout.addWidget(self._build_row(pair))

        if different:
            if shown:
                self._rows_layout.addWidget(_row_rule())
            fold = link_button(
                f"Judged different ({len(different)})", self._toggle_fold,
                align_left=True)
            self._rows_layout.addWidget(fold)
            if self._fold_open:
                for pair in different:
                    self._rows_layout.addWidget(_row_rule())
                    self._rows_layout.addWidget(self._build_row(pair))

    def _toggle_fold(self, *_):
        self._fold_open = not self._fold_open
        self._rebuild_list()

    def _build_row(self, pair):
        left_front, left_back = _note_texts(pair["left"][0])
        right_front, right_back = _note_texts(pair["right"][0])
        c = colors()

        row = QWidget()
        outer = QVBoxLayout(row)
        outer.setContentsMargins(0, 5, 0, 6)
        outer.setSpacing(4)

        header = QWidget()
        hl = QHBoxLayout(header)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(CARET_GAP)

        caret = QPushButton("▸")
        caret.setFlat(True)
        caret.setFixedWidth(CARET_W)
        hl.addWidget(caret, 0, Qt.AlignmentFlag.AlignTop)

        label, role = _chip_label(pair)
        width = _chip_column_width()
        chip_cell = QWidget()
        chip_cell.setFixedWidth(width)
        cl = QHBoxLayout(chip_cell)
        cl.setContentsMargins(0, 0, 0, 0)
        pill = _pill(label, role)
        pill.setMinimumWidth(width)
        cl.addWidget(pill)
        hl.addWidget(chip_cell, 0, Qt.AlignmentFlag.AlignTop)

        shares_html = ""
        if pair.get("shares"):
            shares_html = (f"<br><span style='color:{c['muted']};'>shares: "
                          f"{_esc(', '.join(pair['shares']))}</span>")
        primary = QLabel(
            f"<b>ours:</b> {_esc(left_front)}<br>"
            f"<span style='color:{c['muted']};'>theirs: {_esc(right_front)}"
            f" ({_esc(pair['right'][2])}, {_esc(pair['right'][3])})</span>"
            f"{shares_html}")
        primary.setWordWrap(True)
        primary.setTextFormat(Qt.TextFormat.RichText)
        hl.addWidget(primary, 1)

        body = QWidget()
        body.setVisible(False)

        # Trailing action links, at the right of the header rather than tucked inside
        # the expanded body: a collapsed row used to hide every action it offers. Two
        # short rows rather than one long one: four links wide enough to name each
        # action in full (Suspend ours/theirs, Keep both, Ignore pair) don't fit beside
        # a two-line "ours:"/"theirs:" label at this dialog's own floor width without
        # squeezing that label down to single-word wrapping. Top aligned, so the block
        # sits level with the chip and the "ours:" line.
        trailing = QWidget()
        tv = QVBoxLayout(trailing)
        tv.setContentsMargins(0, 0, 0, 0)
        tv.setSpacing(2)
        top_links = QWidget()
        tl1 = QHBoxLayout(top_links)
        tl1.setContentsMargins(0, 0, 0, 0)
        tl1.setSpacing(CARET_GAP)
        tl1.addWidget(link_button("Suspend ours", lambda: self._suspend(pair, "left")))
        tl1.addWidget(link_button("Suspend theirs", lambda: self._suspend(pair, "right")))
        tv.addWidget(top_links)
        bottom_links = QWidget()
        tl2 = QHBoxLayout(bottom_links)
        tl2.setContentsMargins(0, 0, 0, 0)
        tl2.setSpacing(CARET_GAP)
        tl2.addWidget(link_button("Keep both", lambda: self._keep_both(caret, body)))
        tl2.addWidget(link_button("Ignore pair", lambda: self._ignore(pair)))
        tv.addWidget(bottom_links)
        hl.addWidget(trailing, 0, Qt.AlignmentFlag.AlignTop)

        outer.addWidget(header)

        blay = QVBoxLayout(body)
        blay.setContentsMargins(_row_text_indent(), 0, 0, 0)
        ours_answer = QLabel(f"<b>ours answer:</b> {_esc(left_back)}")
        ours_answer.setWordWrap(True)
        theirs_answer = QLabel(f"<b>theirs answer:</b> {_esc(right_back)}")
        theirs_answer.setWordWrap(True)
        blay.addWidget(ours_answer)
        blay.addWidget(theirs_answer)
        if pair.get("note"):
            blay.addWidget(hint_label(pair["note"]))
        outer.addWidget(body)

        def _toggle():
            expanded = not body.isVisible()
            body.setVisible(expanded)
            caret.setText("▾" if expanded else "▸")
        caret.clicked.connect(_toggle)

        return row

    def _keep_both(self, caret, body):
        body.setVisible(False)
        caret.setText("▸")

    def _suspend(self, pair, side):
        row = pair["left"] if side == "left" else pair["right"]
        suspend_notes(mw.col, [row[0]])
        pair["suspended"].add(side)
        self._rebuild_list()

    def _ignore(self, pair):
        add_dupes_ignored(pair["key"])
        self._pairs.remove(pair)
        self._rebuild_list()

    def _copy_list(self, *_):
        lines = []
        for p in self._pairs:
            left_front, _ = _note_texts(p["left"][0])
            right_front, _ = _note_texts(p["right"][0])
            lines.append(f"{p['score']:.2f} | {left_front} | "
                        f"{right_front} ({p['right'][2]})")
        copy_to_clipboard("\n".join(lines))

    # ------------------------------------------------------------------ AI judging
    @_safe
    def _judge_with_ai(self, *_):
        if not self._judge_backend or not self._pairs:
            return
        cfg = _cfg()
        kind = self._judge_backend
        path = ai_cli.detect_backends(cfg)["backends"][kind]["path"]
        payload = []
        judged_pairs = list(self._pairs)
        for p in judged_pairs:
            left_front, left_back = _note_texts(p["left"][0])
            right_front, right_back = _note_texts(p["right"][0])
            payload.append({"ours": {"front": left_front, "back": left_back},
                           "theirs": {"front": right_front, "back": right_back}})
        prompt = ai_logic.build_dupes_judge_prompt(payload)
        scratch = tempfile.mkdtemp(prefix="ip-dupejudge-")
        self._judge_pairs = judged_pairs
        self._judge_scratch = scratch
        self._judge_result = None
        self._judge_error = None
        self._judge_t0 = time.monotonic()

        def work():
            try:
                self._judge_result = ai_cli.run_generation(
                    kind, path, prompt, "thorough", scratch,
                    model=cfg["ai_model"].get(kind, ""),
                    effort=cfg["ai_effort"].get(kind, ""))
            except Exception as e:
                self._judge_error = e

        self._judge_worker = threading.Thread(target=work, daemon=True)
        self._judge_worker.start()
        self.judge_btn.setEnabled(False)
        self.summary_label.setText("Judging with AI...")
        self._judge_timer = QTimer(self)
        self._judge_timer.timeout.connect(self._poll_judge)
        self._judge_timer.start(200)

    def _poll_judge(self):
        if self._judge_worker.is_alive():
            elapsed = int(time.monotonic() - self._judge_t0)
            self.summary_label.setText(f"Judging with AI... {elapsed}s elapsed")
            return
        self._judge_timer.stop()
        self._finish_judge()

    def _finish_judge(self):
        self.judge_btn.setEnabled(bool(self._judge_backend))
        if self._judge_error or not self._judge_result:
            self._rebuild_list()
            return
        verdicts, errors = ai_logic.parse_dupes_verdicts_json(
            self._judge_result["text"], len(self._judge_pairs))
        for i, pair in enumerate(self._judge_pairs):
            v = verdicts.get(i)
            if v:
                pair["judged"] = v["verdict"]
                pair["note"] = v["note"]
        self._rebuild_list()

    def _wait_for_judge(self, timeout=15):
        """Test helper: run the judging call to completion synchronously, without a
        live QTimer, the same shape `_wait_for_scan` uses."""
        end = time.time() + timeout
        while self._judge_worker.is_alive() and time.time() < end:
            time.sleep(0.02)
        self._judge_timer.stop()
        self._finish_judge()


@_safe
def open_duplicate_scan():
    """Experimental > Scan for duplicates."""
    dlg = _DuplicateScanDialog(_cfg()["scope_tag"])
    dlg.exec()
    dlg.deleteLater()
