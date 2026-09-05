"""Scan for duplicates: lexical duplicate candidates across two scopes of the
collection, on the update screen's own row vocabulary (chip, bold line, muted detail,
trailing links, rules between rows).

The scan itself (`dupes.find_candidates`) is pure CPU work with no collection access,
so it runs on a background thread while a busy line with elapsed time covers it; the
collection reads that feed it, and the only write this dialog ever makes (suspending a
card), happen on the main thread, through collection.py's own API.
"""
import threading
import time

from aqt import mw
from aqt.qt import (QComboBox, QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel,
                    QPushButton, QScrollArea, Qt, QTimer, QVBoxLayout, QWidget)

from .collection import note_rows, suspend_notes
from .config import APP_NAME, _cfg, add_dupes_ignored
from .dupes import find_candidates, pair_key
from .logic import plain_text
from .palette import colors
from .ui import _safe, copy_to_clipboard, hint_label, link_button, title_label

# This dialog's own chip vocabulary. Unlike widgets.CHIPS, a candidate's label carries
# its own score (CANDIDATE 0.77), so it can't be one of a fixed finite set of labels the
# way every other screen's chips are; the column width below is measured over each
# list's own current labels instead of a cached, name-keyed set.
_CHIP_STYLE = ("border-radius: 3px; padding: 1px 6px; font-size: 11px; font-weight: 600;")
_ROLES = {"candidate": "new", "duplicate": "accept", "overlaps": "updated",
         "suspended": "retired"}

_HINT = ("Compares two scopes of your collection by the words each card actually uses, "
        "so a paraphrase counts even when the wording doesn't match.")


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
    return f"CANDIDATE {pair['score']:.2f}", "candidate"


def _measure_chip_width(pairs):
    widest = 0
    labels = {"SUSPENDED", "DUPLICATE", "OVERLAPS"}
    labels |= {f"CANDIDATE {p['score']:.2f}" for p in pairs}
    for label in labels:
        probe = QLabel(label)
        probe.setStyleSheet(_CHIP_STYLE)
        probe.ensurePolished()
        widest = max(widest, probe.sizeHint().width())
    return widest or 1


def _row_rule():
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFrameShadow(QFrame.Shadow.Plain)
    line.setFixedHeight(1)
    line.setStyleSheet(f"color: {colors()['row_rule']};")
    return line


def _note_texts(nid):
    note = mw.col.get_note(nid)
    front = plain_text(note.fields[0]) if note.fields else ""
    back = plain_text(note.fields[1]) if len(note.fields) > 1 else ""
    return front, back


def _esc(text):
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class _DuplicateScanDialog(QDialog):
    def __init__(self, scope_tag):
        super().__init__(mw)
        self._scope_tag = scope_tag
        self.setWindowTitle(f"{APP_NAME}: Scan for duplicates")
        self.setMinimumSize(640, 560)
        self._pairs = []
        self._left_count = self._right_count = 0
        self._deck_names = sorted({d.name for d in mw.col.decks.all_names_and_ids()})
        self._fold_open = False

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

        outer.addWidget(link_button("Rescan", self._rescan, align_left=True))

        self.summary_label = hint_label("")
        outer.addWidget(self.summary_label)

        self._rows_container = QWidget()
        self._rows_layout = QVBoxLayout(self._rows_container)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._rows_container)
        outer.addWidget(scroll, 1)

        outer.addWidget(link_button("Copy list", self._copy_list, align_left=True))

        bb = QDialogButtonBox()
        bb.addButton("Close", QDialogButtonBox.ButtonRole.RejectRole)
        bb.rejected.connect(self.reject)
        outer.addWidget(bb)

        self._rescan()

    # ------------------------------------------------------------------ scope
    def _left_rows(self):
        if self.left_combo.currentIndex() == 0:
            return note_rows(mw.col, scope_tag=self._scope_tag)
        deck = self._deck_names[self.left_combo.currentIndex() - 1]
        return note_rows(mw.col, deck_name=deck)

    def _right_rows(self):
        if self.right_combo.currentIndex() == 0:
            left_ids = {r[0] for r in self._left_rows()}
            return [r for r in note_rows(mw.col) if r[0] not in left_ids]
        deck = self._deck_names[self.right_combo.currentIndex() - 1]
        return note_rows(mw.col, deck_name=deck)

    # ------------------------------------------------------------------ scan
    @_safe
    def _rescan(self):
        left_rows = self._left_rows()
        right_rows = self._right_rows()
        self._left_count, self._right_count = len(left_rows), len(right_rows)
        self._fold_open = False
        self.summary_label.setText("Scanning...")
        self._scan_result = None
        self._scan_error = None
        self._t0 = time.monotonic()

        def work():
            try:
                self._scan_result = find_candidates(left_rows, right_rows,
                                                    threshold=0.5, top=3)
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
        for score, left, right in self._scan_result:
            key = pair_key(left[0], right[0])
            if key in ignored:
                continue
            self._pairs.append({"score": score, "left": left, "right": right,
                               "key": key, "judged": None, "note": "",
                               "suspended": set()})
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
    def _summary_text(self):
        text = (f"{self._left_count} scanned against {self._right_count}, "
               f"{len(self._pairs)} candidates")
        same = sum(1 for p in self._pairs if p["judged"] == "same")
        if same:
            text += f", {same} judged the same"
        return text

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

    def _toggle_fold(self):
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
        hl.setSpacing(6)

        caret = QPushButton("▸")
        caret.setFlat(True)
        caret.setFixedWidth(18)
        hl.addWidget(caret, 0, Qt.AlignmentFlag.AlignTop)

        label, role = _chip_label(pair)
        width = _measure_chip_width(self._pairs)
        chip_cell = QWidget()
        chip_cell.setFixedWidth(width)
        cl = QHBoxLayout(chip_cell)
        cl.setContentsMargins(0, 0, 0, 0)
        pill = _pill(label, role)
        pill.setMinimumWidth(width)
        cl.addWidget(pill)
        hl.addWidget(chip_cell, 0, Qt.AlignmentFlag.AlignTop)

        primary = QLabel(
            f"<b>ours:</b> {_esc(left_front)}<br>"
            f"<span style='color:{c['muted']};'>theirs: {_esc(right_front)}"
            f" ({_esc(pair['right'][2])}, {_esc(pair['right'][3])})</span>")
        primary.setWordWrap(True)
        primary.setTextFormat(Qt.TextFormat.RichText)
        hl.addWidget(primary, 1)
        outer.addWidget(header)

        body = QWidget()
        body.setVisible(False)
        blay = QVBoxLayout(body)
        blay.setContentsMargins(24, 0, 0, 0)
        ours_answer = QLabel(f"<b>ours answer:</b> {_esc(left_back)}")
        ours_answer.setWordWrap(True)
        theirs_answer = QLabel(f"<b>theirs answer:</b> {_esc(right_back)}")
        theirs_answer.setWordWrap(True)
        blay.addWidget(ours_answer)
        blay.addWidget(theirs_answer)
        if pair.get("note"):
            blay.addWidget(hint_label(pair["note"]))

        links = QWidget()
        ll = QHBoxLayout(links)
        ll.setContentsMargins(0, 4, 0, 0)
        ll.addWidget(link_button("Suspend ours", lambda: self._suspend(pair, "left")))
        ll.addWidget(link_button("Suspend theirs", lambda: self._suspend(pair, "right")))
        ll.addWidget(link_button("Keep both", lambda: self._keep_both(caret, body)))
        ll.addWidget(link_button("Ignore pair", lambda: self._ignore(pair)))
        ll.addStretch()
        blay.addWidget(links)
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

    def _copy_list(self):
        lines = []
        for p in self._pairs:
            left_front, _ = _note_texts(p["left"][0])
            right_front, _ = _note_texts(p["right"][0])
            lines.append(f"{p['score']:.2f} | {left_front} | "
                        f"{right_front} ({p['right'][2]})")
        copy_to_clipboard("\n".join(lines))


@_safe
def open_duplicate_scan():
    """Experimental > Scan for duplicates."""
    dlg = _DuplicateScanDialog(_cfg()["scope_tag"])
    dlg.exec()
    dlg.deleteLater()
