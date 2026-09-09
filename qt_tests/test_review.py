"""Real-Qt regressions for the audit fixes in the owned UI modules."""
from pathlib import Path

import harness
from aqt import mw
from aqt.qt import QLabel, QPlainTextEdit, QPushButton, QSpinBox
from internpearls import config, dialogs, review


def test_settings_roundtrips_a_week_and_labels_focus_their_spinboxes():
    harness.bootstrap()
    harness.app()
    settings = dialogs._SettingsDialog(
        None, True, config.AUTO_SYNC_INTERVAL_CEILING_MIN, True, False)
    interval = settings.findChildren(QSpinBox)[0]
    interval_label = next(label for label in settings.findChildren(QLabel)
                          if label.text() == "Check every")
    assert settings.values()["auto_sync_interval_minutes"] == \
        config.AUTO_SYNC_INTERVAL_CEILING_MIN
    assert interval.maximum() == config.AUTO_SYNC_INTERVAL_CEILING_MIN
    assert interval_label.buddy() is interval
    assert interval.accessibleName() == "Check every"

    dimming = dialogs._NightModeDimmingDialog(None, True, 30, "images")
    percent = dimming._percent_spin
    percent_label = next(label for label in dimming.findChildren(QLabel)
                         if label.text() == "Dim by")
    assert percent_label.buddy() is percent
    assert percent.accessibleName() == "Dim by"


def test_old_svg_preview_never_changes_collection_media_bytes(tmp_path, monkeypatch):
    harness.bootstrap()
    harness.app()
    media = tmp_path / "media"
    media.mkdir()
    svg = media / "figure.svg"
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20">'
        '<rect width="20" height="20" fill="red"/></svg>', encoding="utf8")
    sibling = media / "figure.svg.png"
    sibling.write_bytes(b"existing collection media must remain byte-identical")
    before = {path.name: path.read_bytes() for path in media.iterdir()}
    monkeypatch.setattr(
        mw.col, "media", type("Media", (), {"dir": lambda self: str(media)})())

    rendered = review._old_version_html(
        {"notetype": "Basic"}, "Front", '<img src="figure.svg">')

    assert "<img" in rendered
    assert {path.name: path.read_bytes() for path in media.iterdir()} == before


def test_svg_thumbnail_paths_are_flat_and_distinguish_equal_basenames(tmp_path):
    harness.bootstrap()
    harness.app()
    paths = []
    for folder, fill in (("first", "red"), ("second", "blue")):
        source_dir = tmp_path / folder
        source_dir.mkdir()
        svg = source_dir / "figure.svg"
        svg.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
            f'<rect width="10" height="10" fill="{fill}"/></svg>', encoding="utf8")
        paths.append(Path(review._svg_thumbnail(str(svg))))

    assert paths[0].parent == paths[1].parent
    assert paths[0].name != paths[1].name
    assert all(path.suffix == ".png" and path.exists() for path in paths)


def test_review_controls_name_the_card_and_feedback_field():
    harness.bootstrap()
    harness.app()
    detail = {
        "guid": "g-accessible",
        "kind": "new",
        "notetype": "Basic",
        "fields": [("Front", "Which card is this?"), ("Back", "This one")],
    }
    row = review._card_row(detail, {}, {}, {}, lambda *args: None)
    card_label = review._card_label(detail)
    buttons = row.findChildren(QPushButton)
    caret = next(button for button in buttons
                 if button.text() in (review._CARET_CLOSED, review._CARET_OPEN))
    add_note = next(button for button in buttons if button.text() == "Add note")
    skip = next(button for button in buttons if button.text() == "Skip")
    box = row.findChildren(QPlainTextEdit)[0]

    assert caret.accessibleName() == f"Show card: {card_label}"
    assert add_note.accessibleName() == f"Add note: {card_label}"
    assert box.accessibleName() == f"Feedback note: {card_label}"
    skip.click()
    caption = next(label for label in row.findChildren(QLabel)
                   if label.text() == review._DECLINE_CAPTION["skip"])
    assert caption.buddy() is box
