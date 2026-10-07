import harness
import pytest
from aqt.qt import QLabel, Qt
from PyQt6.QtGui import QTextDocument
from internpearls import dialogs, ui


@pytest.mark.parametrize("text", ["A <b>x</b> & B", "first\nA <a href='https://example.com'>x</a> & B"])
def test_plain_tooltip_round_trips(text):
    harness.app()
    label = QLabel(ui.plain_tooltip(text))
    label.setTextFormat(Qt.TextFormat.RichText)
    doc = QTextDocument()
    doc.setHtml(label.text())
    assert doc.toPlainText() == text
    label.deleteLater()


def test_deck_manager_shows_stale_names_and_tooltips_literally():
    mock, _ = harness.bootstrap()
    harness.app()
    text = "A <b>x</b> & B"
    rows = [{"name": text, "enabled": True, "state": "new", "version": "v1"}]
    dlg = dialogs._DeckManagerDialog(mock.mw, rows, [], "local folder", True,
                                     stale_excluded=[text])
    assert dlg._stale_label.textFormat() == Qt.TextFormat.PlainText
    assert text in dlg._stale_label.text()
    dlg._clear_stale()
    assert text in dlg._stale_label.text()
    doc = QTextDocument()
    doc.setHtml(dlg._checks[text].toolTip())
    assert doc.toPlainText() == text
    dlg.deleteLater()


def test_declined_row_detail_shows_registry_text_literally():
    mock, _ = harness.bootstrap()
    harness.app()
    dlg = dialogs._DeclinedDialog(mock.mw)
    text = "A <b>x</b> & B"
    row = dlg._row("guid", {"front": "front", "deck": text,
                            "decided": text, "state": text}, show_state=True)
    label = next(l for l in row.findChildren(QLabel) if " · " in l.text())
    assert label.text() == " · ".join([text] * 3)
    assert label.textFormat() == Qt.TextFormat.PlainText
    row.deleteLater()
    dlg.deleteLater()
