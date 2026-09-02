"""Intern Pearls: AI Backends. Where the three assistants are enabled, chosen,
pointed at, tested, and given their default model and effort. The wizard only
shows a one-line summary and a Change link that opens this."""
import threading

from aqt import mw
from aqt.qt import (QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                    QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                    QPushButton, QRadioButton, QTimer, QVBoxLayout, QWidget, pyqtSignal)

from . import ai_cli
from .config import ADDON_PACKAGE, APP_NAME, _cfg
from .ui import _safe, hint_label, link_button, title_label

_POLL_MS = 200


def _write(key, value):
    conf = mw.addonManager.getConfig(ADDON_PACKAGE) or {}
    conf[key] = value
    mw.addonManager.writeConfig(ADDON_PACKAGE, conf)


def _write_map(key, kind, value):
    conf = mw.addonManager.getConfig(ADDON_PACKAGE) or {}
    m = conf.get(key)
    if not isinstance(m, dict):
        # A legacy flat-string config value (e.g. an un-migrated ai_cli_path)
        # reads back through _cfg() already migrated into the preferred
        # backend's entry. Seed from that migrated map instead of {} so the
        # first per-backend save doesn't silently drop it.
        m = dict(_cfg()[key])
    m[kind] = value
    conf[key] = m
    mw.addonManager.writeConfig(ADDON_PACKAGE, conf)


class ModelEffortControls(QWidget):
    """Model and Effort for one backend, with the honesty rules from
    ai_cli.BACKENDS: a closed alias list plus Custom where aliases exist,
    free text where the CLI takes any name, read-only text where nothing can
    be honoured (agy), and Effort only where a flag is verified."""
    changed = pyqtSignal()

    def __init__(self, kind, model, effort, parent=None):
        super().__init__(parent)
        self.kind = kind
        meta = ai_cli.BACKENDS[kind]
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        # Model and Effort share one QFormLayout so their field column lines up:
        # an editable QComboBox (free text) and a non-editable one (a closed
        # list) render at different heights and insets on macOS, and a plain
        # QHBoxLayout per row leaves each field starting wherever its own
        # ("Model" vs "Effort") label happens to end.
        self.combo = QComboBox()
        self.readonly = hint_label(meta["model_hint"])
        self.custom = QLineEdit()
        self.custom.setPlaceholderText(meta["model_hint"])
        aliases = [a for a in ([meta["default_model"]] + list(meta.get("model_aliases", []))) if a]
        self._aliases = list(dict.fromkeys(aliases))
        model_field = QWidget()
        model_field_lay = QVBoxLayout(model_field)
        model_field_lay.setContentsMargins(0, 0, 0, 0)
        model_field_lay.setSpacing(2)
        model_field_lay.addWidget(self.combo)
        model_field_lay.addWidget(self.readonly)
        model_field_lay.addWidget(self.custom)
        if kind == "agy":
            self.combo.hide(); self.custom.hide()
        elif self._aliases:
            self.combo.addItems(self._aliases + ["Custom"])
            self.readonly.hide()
            if model in self._aliases or not model:
                self.combo.setCurrentText(model or meta["default_model"])
                self.custom.hide()
            else:
                self.combo.setCurrentText("Custom")
                self.custom.setText(model)
        else:
            self.combo.hide(); self.readonly.hide()
            self.custom.setText(model)
        self.effort = QComboBox()
        levels = meta.get("effort_levels") or []
        has_effort = bool(levels)
        if has_effort:
            self.effort.addItem(f"Default ({meta['default_effort']})", "")
            for lv in levels:
                self.effort.addItem(lv, lv)
            idx = self.effort.findData(effort if effort in levels else "")
            self.effort.setCurrentIndex(max(idx, 0))
        else:
            self.effort.hide()
        form = QFormLayout()
        form.addRow(QLabel("Model"), model_field)
        if has_effort:
            form.addRow(QLabel("Effort"), self.effort)
        lay.addLayout(form)
        self.combo.currentTextChanged.connect(self._on_combo)
        self.custom.textEdited.connect(lambda _t: self.changed.emit())
        self.effort.currentIndexChanged.connect(lambda _i: self.changed.emit())

    def _on_combo(self, text):
        if self._aliases and self.kind != "agy":
            self.custom.setVisible(text == "Custom")
        self.changed.emit()

    def values(self):
        if self.kind == "agy":
            return "", ""
        if self._aliases:
            model = self.custom.text().strip() if self.combo.currentText() == "Custom" \
                else self.combo.currentText()
        else:
            model = self.custom.text().strip()
        effort = self.effort.currentData() if self.effort.isVisibleTo(self) else ""
        return model, effort or ""


class _BackendGroup(QGroupBox):
    def __init__(self, kind, info, cfg, preferred_group, parent):
        meta = ai_cli.BACKENDS[kind]
        super().__init__(meta["label"], parent)
        self.kind = kind
        self.dlg = parent
        lay = QVBoxLayout(self)
        self.status = hint_label("")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        toprow = QHBoxLayout()
        self.enabled = QCheckBox("Use this assistant")
        self.enabled.setChecked(cfg["ai_backend_enabled"][kind])
        self.preferred = QRadioButton("Preferred")
        preferred_group.addButton(self.preferred)
        self.preferred.setChecked(cfg["ai_backend"] == kind)
        toprow.addWidget(self.enabled); toprow.addWidget(self.preferred); toprow.addStretch()
        lay.addLayout(toprow)
        prow = QHBoxLayout()
        prow.addWidget(QLabel("Executable path"))
        self.path = QLineEdit(cfg["ai_cli_path"][kind])
        self.path.setPlaceholderText("leave blank to auto-detect")
        browse = QPushButton("Browse")
        prow.addWidget(self.path, 1); prow.addWidget(browse)
        lay.addLayout(prow)
        self.model = ModelEffortControls(kind, cfg["ai_model"][kind], cfg["ai_effort"][kind], self)
        lay.addWidget(self.model)
        trow = QHBoxLayout()
        self.test_btn = QPushButton("Test connection")
        self.test_status = hint_label("Not tested yet")
        trow.addWidget(self.test_btn); trow.addWidget(self.test_status, 1)
        lay.addLayout(trow)
        self.enabled.toggled.connect(lambda on: self.dlg._guard(self._on_enabled, on))
        self.preferred.toggled.connect(lambda on: on and self.dlg._guard(_write, "ai_backend", kind))
        self.path.editingFinished.connect(lambda: self.dlg._guard(self._commit_path))
        browse.clicked.connect(lambda: self.dlg._guard(self._browse))
        self.model.changed.connect(lambda: self.dlg._guard(self._commit_model))
        self.test_btn.clicked.connect(lambda: self.dlg._guard(self.dlg._test, kind))
        self.apply(info)

    def apply(self, info):
        meta = ai_cli.BACKENDS[self.kind]
        if not info["enabled"]:
            status = "disabled"
        elif info["ok"]:
            status = f"installed and working ({info['detail']})"
        elif info["path"]:
            status = "found, but not responding"
        else:
            status = "not found"
        imgs = ("can view attached images" if ai_cli.image_capable(self.kind)
                else "reads attached PDFs as text only, no images")
        self.status.setText(f"{meta['subscription']}: {status}. {meta['safety']}. {imgs}.")
        on = info["enabled"]
        for w in (self.preferred, self.path, self.model):
            w.setEnabled(on)
        self.test_btn.setEnabled(on and bool(info["path"]))

    def _on_enabled(self, on):
        _write_map("ai_backend_enabled", self.kind, bool(on))
        self.dlg.recheck()

    def _commit_path(self):
        _write_map("ai_cli_path", self.kind, self.path.text().strip())
        self.dlg.recheck()

    def _browse(self):
        p, _ = QFileDialog.getOpenFileName(self, f"Locate {ai_cli.BACKENDS[self.kind]['exe']}")
        if p:
            self.path.setText(p)
            self._commit_path()

    def _commit_model(self):
        model, effort = self.model.values()
        _write_map("ai_model", self.kind, model)
        _write_map("ai_effort", self.kind, effort)


class _AIBackendsDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME}: AI Backends")
        self._testing = set()
        self._refs = []
        lay = QVBoxLayout(self)
        lay.addWidget(title_label("AI Backends"))
        lay.addWidget(hint_label(
            "Card generation runs through an assistant you sign into yourself. This "
            "add-on never sees or stores your credentials: there is no API key field "
            "here, or anywhere else in the add-on. Install one, run it once in a "
            "terminal, sign in there, then Re-check."))
        cfg = _cfg()
        res = ai_cli.detect_backends(cfg)
        self._pref_group = QButtonGroup(self)
        self.groups = {}
        for kind in ai_cli.BACKENDS:
            g = _BackendGroup(kind, res["backends"][kind], cfg, self._pref_group, self)
            self.groups[kind] = g
            lay.addWidget(g)
        brow = QHBoxLayout()
        self.recheck_btn = link_button("Re-check", on_click=lambda: self._guard(self.recheck))
        self.overall = hint_label("")
        brow.addWidget(self.recheck_btn); brow.addWidget(self.overall, 1)
        lay.addLayout(brow)
        lay.addWidget(hint_label(
            "Test connection runs a real, trivial prompt through a detected CLI to "
            "confirm it can generate, not just that the binary runs. It costs one "
            "model turn, so it only runs when you click it."))
        bb = QDialogButtonBox()
        bb.addButton("Close", QDialogButtonBox.ButtonRole.RejectRole)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._set_overall(res)

    def _guard(self, fn, *args):
        try:
            fn(*args)
        except Exception as e:      # never let a callback take Anki down
            self.overall.setText(f"Error: {e}")

    def _set_overall(self, res):
        ch = res["chosen"]
        self.overall.setText(f"Ready: {ai_cli.BACKENDS[ch]['label']} will be used." if ch
                             else "No enabled assistant detected yet.")

    def recheck(self):
        res = ai_cli.detect_backends(_cfg())
        for kind, g in self.groups.items():
            if kind not in self._testing:
                g.apply(res["backends"][kind])
        self._set_overall(res)

    def _test(self, kind):
        g = self.groups[kind]
        path = ai_cli.detect_backends(_cfg())["backends"][kind]["path"]
        if not path or kind in self._testing:
            return
        self._testing.add(kind)
        g.test_btn.setEnabled(False)
        g.test_status.setText("Testing connection")
        box = {}

        def worker():
            try:
                box["r"] = ai_cli.test_connection(kind, path)
            except Exception as e:
                box["e"] = e
        t = threading.Thread(target=worker, daemon=True)
        timer = QTimer(self)
        self._refs.append((t, timer))

        def poll():
            if t.is_alive():
                return
            timer.stop()
            self._testing.discard(kind)
            g.test_btn.setEnabled(True)
            if "e" in box:
                g.test_status.setText(f"Test failed: {box['e']}")
            else:
                r = box["r"]
                g.test_status.setText(("Working: " if r["state"] == "working"
                                       else "Not working: ") + r["detail"])
        timer.timeout.connect(poll)
        t.start()
        timer.start(_POLL_MS)


@_safe
def open_ai_backends(parent=None):
    dlg = _AIBackendsDialog(parent or mw)
    dlg.exec()
