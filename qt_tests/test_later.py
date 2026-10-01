"""Later on the real Update my decks screen: the option sets, the LATER chip, a
returning card's note and hint, the migration line and the group's Later all."""
import pytest

import harness
from sampling import text_contrast, widget_rect

MIGRATION = "Skip is now Later. 1 card you skipped earlier is below, still set to Later."


def _visible_labels(dialog, q):
    return [w for w in dialog.findChildren(q.QLabel) if w.isVisible()]


def _cells(dialog, q):
    """Every visible row decision control, in row order."""
    cells = [w for w in dialog.findChildren(q.QWidget)
             if isinstance(getattr(w, "buttons", None), dict) and w.isVisible()
             and "all" not in w.buttons]
    return sorted(cells, key=lambda w: widget_rect(dialog, w).top())


def test_new_and_changed_rows_render_their_option_sets():
    _, q = harness.bootstrap()
    s = harness.render("confirm", size=(880, 900))
    labels = [[b.text() for b in c.buttons.values() if b.isVisible()]
              for c in _cells(s.dialog, q)]
    assert labels[0] == ["Import", "Later", "Never"]
    assert labels[1] == ["Apply", "Keep yours", "Later", "Never"]


@pytest.mark.parametrize("theme", sorted(harness.THEMES))
def test_the_later_chip_paints_with_contrast(shot, theme):
    _, q = harness.bootstrap()
    s = shot("confirm-later", theme=theme, size=(880, 900))
    chips = [l for l in _visible_labels(s.dialog, q) if l.text() == "LATER"]
    assert len(chips) == 3
    measured = text_contrast(s, chips[0])
    assert measured is not None and measured[0] >= 4.5, (
        f"{theme}: LATER measures {measured}")


def test_returning_rows_start_where_they_should():
    """An old Skip and a note not yet acted on start at Later; a noted card updated
    since starts at Apply."""
    _, q = harness.bootstrap()
    s = harness.render("confirm-later", size=(880, 900))
    checked = [next(v for v, b in c.buttons.items() if b.isChecked())
               for c in _cells(s.dialog, q)]
    assert checked == ["held", "apply", "held"]   # rows 0, 1 and 3 carry a control


def test_a_returning_noted_row_shows_the_note_and_its_hint(shot):
    _, q = harness.bootstrap()
    s = shot("confirm-later", size=(880, 900))
    texts = [l.text() for l in _visible_labels(s.dialog, q)]
    assert "Your note: an example note asking for a fix" in texts
    assert "Updated since your note" in texts
    assert "Your note: another example note, still waiting" in texts
    assert "Not updated yet" in texts


def test_the_migration_line_renders_once_above_the_list(shot):
    from internpearls import widgets
    _, q = harness.bootstrap()
    s = shot("confirm-later", size=(880, 900))
    lines = [l for l in _visible_labels(s.dialog, q) if l.text() == MIGRATION]
    assert len(lines) == 1
    stream = s.dialog.findChildren(widgets.StreamingList)[0]
    assert widget_rect(s.dialog, lines[0]).bottom() <= widget_rect(s.dialog, stream).top()


def test_no_migration_line_without_an_old_skip(shot):
    _, q = harness.bootstrap()
    s = shot("confirm", size=(880, 900))
    assert not any("Skip is now Later" in l.text() for l in _visible_labels(s.dialog, q))


@pytest.mark.parametrize("kind, options", [
    ("new", ["Import all", "Later all"]),
    ("changed", ["Apply all", "Keep all yours", "Later all"])])
def test_a_folded_group_offers_later_all(kind, options):
    _, q = harness.bootstrap()
    s = harness.render("confirm", group_size=6, group_kind=kind, size=(880, 900))
    texts = [b.text() for b in s.dialog.findChildren(q.QPushButton) if b.isVisible()]
    assert [t for t in texts if t.endswith(" all") or t == "Keep all yours"] == options
    later_all = next(b for b in s.dialog.findChildren(q.QPushButton)
                     if b.text() == "Later all")
    later_all.click()
    harness.app().processEvents()
    assert later_all.isChecked()
