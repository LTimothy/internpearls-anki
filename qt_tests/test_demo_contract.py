"""Real Qt parity checks for the demo action contract."""

import harness
import mock_anki
from demo_contract_cases import contract_action_table


IDS = {
    "button": "contract-button",
    "radio_a": "contract-radio-a",
    "radio_b": "contract-radio-b",
    "combo": "contract-combo",
    "line": "contract-line",
    "link": "contract-link",
    "scroll": "contract-scroll",
    "dialog": "contract-dialog",
}


def _scene():
    app = harness.app()
    from aqt.qt import (QComboBox, QDialog, QLabel, QLineEdit, QPushButton,
                        QRadioButton, QScrollArea, QVBoxLayout, QWidget)

    root = QWidget()
    layout = QVBoxLayout(root)
    widgets = {
        "button": QPushButton("Toggle"),
        "radio_a": QRadioButton("First"),
        "radio_b": QRadioButton("Second"),
        "combo": QComboBox(),
        "line": QLineEdit(),
        "link": QLabel('<a href="details">Details</a>'),
        "scroll": QScrollArea(),
        "dialog": QDialog(),
    }
    widgets["button"].setCheckable(True)
    widgets["radio_a"].setChecked(True)
    widgets["combo"].addItem("First", "option-first")
    widgets["combo"].addItem("Second", "option-second")
    widgets["link"].setOpenExternalLinks(False)
    widgets["scroll"].verticalScrollBar().setRange(0, 10)
    for name in ("button", "radio_a", "radio_b", "combo", "line", "link", "scroll"):
        layout.addWidget(widgets[name])
    for name, widget in widgets.items():
        widget.wid = IDS[name]
        mock_anki._widgets[widget.wid] = widget
    root.show()
    widgets["dialog"].show()
    app.processEvents()
    return app, root, widgets


def test_actions_match_mock_signal_order_and_final_values():
    app, root, scene = _scene()
    events = []
    scene["button"].clicked.connect(
        lambda checked: events.append(
            ("button", checked, scene["button"].isChecked())))
    scene["radio_a"].toggled.connect(
        lambda checked: events.append(("radio-a", checked)))
    scene["radio_b"].toggled.connect(
        lambda checked: events.append(("radio-b", checked)))
    scene["combo"].currentIndexChanged.connect(
        lambda index: events.append(("combo-index", index)))
    scene["combo"].currentTextChanged.connect(
        lambda text: events.append(("combo-text", text)))
    scene["line"].textEdited.connect(
        lambda text: events.append(("edited", text)))
    scene["line"].textChanged.connect(
        lambda text: events.append(("changed", text)))
    scene["line"].editingFinished.connect(
        lambda: events.append(("finished",)))
    scene["link"].linkActivated.connect(
        lambda action_id: events.append(("link", action_id)))
    scene["scroll"].verticalScrollBar().valueChanged.connect(
        lambda offset: events.append(("scroll", offset)))
    scene["dialog"].rejected.connect(
        lambda: events.append(("escape",)))

    mock_anki.apply_actions({"actions": contract_action_table(IDS)})
    app.processEvents()

    assert events == [
        ("button", True, True),
        ("radio-a", False),
        ("radio-b", True),
        ("combo-index", 1),
        ("combo-text", "Second"),
        ("edited", "x"),
        ("changed", "x"),
        ("finished",),
        ("link", "details"),
        ("scroll", 7),
        ("escape",),
    ]
    assert scene["button"].isChecked() is True
    assert scene["radio_a"].isChecked() is False
    assert scene["radio_b"].isChecked() is True
    assert scene["combo"].currentData() == "option-second"
    assert scene["line"].text() == "x"
    assert scene["scroll"].verticalScrollBar().value() == 7

    root.close()
    scene["dialog"].close()


def test_real_streaming_list_is_a_scroll_action_target_across_four_batches():
    """Replay must recognize the production subclass, not only the fake base class."""
    app = harness.app()
    from aqt.qt import QDialog, QVBoxLayout, QWidget
    from internpearls.widgets import StreamingList, decision_cell

    decisions = {}

    def build(index):
        return decision_cell(
            [("import", f"Import {index + 1}"), ("skip", f"Skip {index + 1}")],
            "import",
            lambda state, item=index: decisions.__setitem__(item, state),
        )

    dialog = QDialog()
    layout = QVBoxLayout(dialog)
    streaming = StreamingList(build, list(range(230)), batch=50)
    layout.addWidget(streaming)
    dialog.resize(640, 420)
    dialog.show()
    for _ in range(10):
        app.processEvents()
        if streaming.verticalScrollBar().maximum() > 0:
            break
    assert streaming.shown() == 50

    first = next(widget for widget in streaming.widget().findChildren(QWidget)
                 if hasattr(widget, "buttons"))
    first.buttons["skip"].click()
    assert decisions == {0: "skip"}

    streaming.wid = "real-streaming-list"
    mock_anki._widgets[streaming.wid] = streaming
    assert mock_anki._actions_for(streaming) == ["scroll"]
    shown = [streaming.shown()]
    for _ in range(4):
        mock_anki.apply_actions({"actions": [{
            "type": "scroll",
            "id": streaming.wid,
            "offset": streaming.verticalScrollBar().maximum(),
        }]})
        app.processEvents()
        shown.append(streaming.shown())

    assert shown == [50, 100, 150, 200, 230]
    assert decisions == {0: "skip"}
    dialog.close()
