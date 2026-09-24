"""Real-Qt render checks for a grouped change note on the update confirmation: one
shared note over two cards, a retired row carrying its own reason, and a plain row
with no chip, all in the same section. See harness._scene_confirm's grouped=True.

The fold tests below use the separate group_size=N fixture instead: a real
("group_note", note, card_count) 3-tuple over N plain member cards, which is what
review._GROUP_COLLAPSE_MIN actually gates on. None of the three tests above can tell
a collapse from an ordinary render (findChildren and .text() both see through a hidden
widget); these assert .isVisible() instead, the idiom test_declined.py and
test_declined_rows.py already use.
"""
import os

import harness


def _visible_labels(dialog, q):
    return [w for w in dialog.findChildren(q.QLabel) if w.isVisible()]


def _member_marker(i):
    return f"Group member card number {i}?"


def test_group_header_renders_once_over_both_member_cards():
    shot = harness.render("confirm", grouped=True, size=(880, 800))
    texts = [w.text() for w in shot.dialog.findChildren(harness.bootstrap()[1].QLabel)
            if w.text().strip()]
    joined = "\n".join(texts)
    assert "an example reviewer request naming both cards" in joined
    # Shown once, as the group header, not repeated per member card.
    assert joined.count("an example reviewer request naming both cards") == 1


def test_retired_row_shows_its_reason_and_a_plain_row_still_renders():
    shot = harness.render("confirm", grouped=True, size=(880, 800))
    q = harness.bootstrap()[1]
    texts = "\n".join(w.text() for w in shot.dialog.findChildren(q.QLabel)
                      if w.text().strip())
    assert "An older phrasing of a since-split card" in texts
    assert "split into two focused cards" in texts
    # The plain (untagged) row from synthetic_details() still renders in this section.
    assert "An untagged row, to check the left edge lines up?" in texts


def test_grouped_confirm_render_saved_as_png(tmp_path):
    out_dir = os.environ.get("IP_SHOT_DIR") or str(tmp_path)
    os.makedirs(out_dir, exist_ok=True)
    saved = []
    for theme in ("light", "dark"):
        shot = harness.render("confirm", theme=theme, grouped=True, size=(880, 800))
        png = os.path.join(out_dir, f"change-note-group-{theme}.png")
        shot.image.save(png, "PNG")
        saved.append(png)
    for png in saved:
        assert os.path.exists(png)


def test_a_five_member_group_hides_its_members_and_shows_its_header():
    _, q = harness.bootstrap()
    shot = harness.render("confirm", group_size=5, size=(880, 800))
    texts = "\n".join(w.text() for w in _visible_labels(shot.dialog, q))
    assert "an example reviewer note spanning 5 cards" in texts, (
        "the group's own header must show even while its members are folded")
    shown = [i for i in range(5) if _member_marker(i) in texts]
    assert shown == [], f"member(s) {shown} rendered visible in a folded group"
    toggle = next(b for b in shot.dialog.findChildren(q.QPushButton)
                  if b.text() == "Show 5 cards")
    assert toggle.isVisible()
    assert toggle.accessibleName() == (
        "Show 5 cards: an example reviewer note spanning 5 cards")


def test_pressing_the_toggle_reveals_a_folded_groups_members():
    _, q = harness.bootstrap()
    shot = harness.render("confirm", group_size=5, size=(880, 800),
                          click_labels=("Show 5 cards",))
    texts = "\n".join(w.text() for w in _visible_labels(shot.dialog, q))
    missing = [i for i in range(5) if _member_marker(i) not in texts]
    assert missing == [], f"member(s) {missing} still hidden after the toggle was clicked"
    toggle = next(b for b in shot.dialog.findChildren(q.QPushButton)
                  if b.accessibleName().startswith("Hide 5 cards:"))
    assert toggle.text() == "Hide 5 cards"


def test_a_three_member_group_is_unaffected():
    _, q = harness.bootstrap()
    shot = harness.render("confirm", group_size=3, size=(880, 800))
    texts = "\n".join(w.text() for w in _visible_labels(shot.dialog, q))
    missing = [i for i in range(3) if _member_marker(i) not in texts]
    assert missing == [], f"member(s) {missing} rendered hidden below the fold threshold"
    toggles = [b for b in shot.dialog.findChildren(q.QPushButton)
              if b.text() in ("Show 3 cards", "Hide 3 cards")]
    assert toggles == [], "a group below _GROUP_COLLAPSE_MIN must not offer a toggle"


def test_a_folded_groups_last_member_does_not_swallow_the_next_decks_first_card():
    """sync._section appends a deck's ("header", ...) with no separator before it,
    and a folded group's members end on a card with no trailing sep either, so the
    group must end at the next deck's heading rather than fold that deck's first card.
    """
    _, q = harness.bootstrap()
    shot = harness.render("confirm", group_size=5, second_deck=True, size=(880, 800))
    texts = "\n".join(w.text() for w in _visible_labels(shot.dialog, q))
    assert "Second Deck" in texts
    assert "Second deck's first pending card?" in texts, (
        "the next deck's first card must render visible, not folded into the "
        "previous deck's group")
    assert "Second deck's next card?" in texts


def test_every_member_of_a_large_immediate_fold_is_reachable_after_expanding():
    """Expanding a large fold reaches every member; fill_all() stands in for
    scrolling the rest of the list into view."""
    from internpearls.widgets import StreamingList
    _, q = harness.bootstrap()
    n = 200
    shot = harness.render("confirm", group_size=n, size=(880, 400))
    dialog = shot.dialog
    lst = dialog.findChild(StreamingList)

    toggle = next(b for b in dialog.findChildren(q.QPushButton)
                  if b.text() == f"Show {n} cards")
    toggle.click()
    harness.app().processEvents()

    lst.fill_all()
    harness.app().processEvents()

    texts = "\n".join(w.text() for w in _visible_labels(dialog, q))
    missing = [i for i in range(n) if _member_marker(i) not in texts]
    assert missing == [], (
        f"member(s) {missing} unreachable after the fold was expanded and the "
        "list filled")


def test_a_large_immediate_fold_still_reveals_the_next_decks_first_card_on_idle():
    """A deck after a large folded group still shows its first pending card
    without any click or scroll: the fold is one row, so the list reaches it."""
    import time
    from internpearls.widgets import StreamingList
    _, q = harness.bootstrap()
    shot = harness.render("confirm", group_size=200, second_deck=True, size=(880, 400))
    lst = shot.dialog.findChild(StreamingList)
    app = harness.app()
    deadline = time.monotonic() + 10
    texts = ""
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
        texts = "\n".join(w.text() for w in _visible_labels(shot.dialog, q))
        if "Second deck's first pending card?" in texts:
            break
    assert "Second Deck" in texts
    assert "Second deck's first pending card?" in texts, (
        "the second deck's first pending card never became visible on idle")
    assert lst.shown() == lst.total(), (
        f"{lst.shown()} of {lst.total()} shown: the idle timer stopped re-arming "
        "before it finished the list")


def test_a_folded_group_builds_none_of_its_members_until_expanded():
    """Folding has to save the work, not just hide it: a folded group's member rows
    are built the first time it is expanded, and kept for later toggles."""
    _, q = harness.bootstrap()
    n = 200
    shot = harness.render("confirm", group_size=n, size=(880, 400))
    dialog = shot.dialog

    def built():
        texts = "\n".join(w.text() for w in dialog.findChildren(q.QLabel))
        return [i for i in range(n) if _member_marker(i) in texts]

    assert built() == [], "a folded group built member rows before it was expanded"

    toggle = next(b for b in dialog.findChildren(q.QPushButton)
                  if b.text() == f"Show {n} cards")
    toggle.click()
    harness.app().processEvents()
    texts = "\n".join(w.text() for w in _visible_labels(dialog, q))
    assert [i for i in range(n) if _member_marker(i) not in texts] == []

    toggle.click()
    harness.app().processEvents()
    texts = "\n".join(w.text() for w in _visible_labels(dialog, q))
    assert [i for i in range(n) if _member_marker(i) in texts] == []
    assert len(built()) == n, "collapsing again must keep the rows, not rebuild them"
