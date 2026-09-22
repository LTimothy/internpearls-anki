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


def test_toggle_keeps_prefetched_members_visible_through_a_later_extend():
    """Critical 1: the toggle must maintain ip_stay_hidden on every widget it flips,
    not just call setVisible on it. widgets.StreamingList._extend re-applies
    `setVisible(not ip_stay_hidden)` to any row it pops out of `_prebuilt`, so a
    member built by the idle prefetcher while the group was still folded, then
    revealed by the toggle, goes hidden again the moment `_extend` reaches it.

    This needs real PyQt6 (harness.render), not tests/mock_anki.py: the mock reports
    every geometry as 0, so its StreamingList never has a viewport to fill and never
    prefetches or runs a second `_extend`, which is exactly the path this bug lives
    on.
    """
    from internpearls.widgets import StreamingList
    _, q = harness.bootstrap()
    n = 60
    # pad=20 filler cards ahead of the group so the initial batch (50 items) reaches
    # the group's own header and a few early members, then runs out mid-group: see
    # harness._scene_confirm's group_size fixture for why padding is what makes that
    # happen rather than every folded member being built up front.
    shot = harness.render("confirm", group_size=n, pad=20, size=(880, 400))
    dialog = shot.dialog
    lst = dialog.findChild(StreamingList)
    assert lst is not None

    built_before = lst.built()
    assert built_before < lst.total(), (
        "fixture must leave members unbuilt after the first batch")

    # Build the next chunk into _prebuilt the way StreamingList._idle_extend's own
    # `build` closure does, while the group is still folded, so these rows are built
    # with ip_stay_hidden=True.
    end = min(built_before + 12, lst.total())
    for item in lst._items[built_before:end]:
        row = lst._build_row(item)
        row.setVisible(False)
        lst._rows_layout.addWidget(row)
        lst._prebuilt.append(row)

    toggle = next(b for b in dialog.findChildren(q.QPushButton)
                  if b.text().startswith("Show"))
    toggle.click()
    harness.app().processEvents()

    # The reader scrolling to the end: StreamingList pops the prebuilt rows first,
    # then builds the rest fresh, both under the now-expanded group. fill_all rather
    # than a single _extend() so every member is actually built by the time the
    # assertion below runs; a member this test never reached is not a regression.
    lst.fill_all()
    harness.app().processEvents()

    texts = "\n".join(w.text() for w in _visible_labels(dialog, q))
    missing = [i for i in range(n) if _member_marker(i) not in texts]
    assert missing == [], (
        f"member(s) {missing} hidden after the toggle expanded the group and a "
        "later _extend() ran")


def test_a_folded_groups_last_member_does_not_swallow_the_next_decks_first_card():
    """Critical 2: sync._section appends a deck's ("header", ...) with no separator
    before it, and a folded group's own member items end on a card with no trailing
    sep either (see harness._scene_confirm's group_size fixture). So when a deck's
    final group is large, review._row's tracker used to carry the group across the
    next deck's heading and register that deck's first card into it, rendering the
    card hidden even though nothing asked to fold it.
    """
    _, q = harness.bootstrap()
    shot = harness.render("confirm", group_size=5, second_deck=True, size=(880, 800))
    texts = "\n".join(w.text() for w in _visible_labels(shot.dialog, q))
    assert "Second Deck" in texts
    assert "Second deck's first pending card?" in texts, (
        "the next deck's first card must render visible, not folded into the "
        "previous deck's group")
    assert "Second deck's next card?" in texts
