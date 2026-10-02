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
from sampling import widget_rect


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
    assert "5 cards" in texts, "the box must say how many cards it holds"
    # The first three are named in the box's preview; the rest are counted.
    named = [i for i in range(5) if _member_marker(i) in texts]
    assert named == [0, 1, 2], f"preview named {named}, expected the first three"
    assert "and 2 more" in texts
    toggle = next(b for b in shot.dialog.findChildren(q.QPushButton)
                  if b.text() == "Show 5 cards")
    assert toggle.isVisible()
    assert toggle.accessibleName() == (
        "Show 5 cards: an example reviewer note spanning 5 cards")


def test_a_folded_groups_preview_shows_quotes_and_ampersands_as_text():
    """The field holds a raw apostrophe and an encoded ampersand; the preview must
    paint both as characters, not as `&#x27;` or `&amp;`."""
    _, q = harness.bootstrap()
    shot = harness.render("confirm", group_size=5, size=(880, 800),
                          group_front="The agent's vapor, hot &amp; dry {i}")
    shown = [harness.shown(w) for w in _visible_labels(shot.dialog, q)]
    assert "The agent's vapor, hot & dry 0" in shown, [s for s in shown if "agent" in s]


def test_a_feedback_group_never_folds():
    """Cards changed because of reviewer feedback are the ones most worth checking,
    so their group stays open however big it is."""
    _, q = harness.bootstrap()
    shot = harness.render("confirm", group_size=6, group_note_kind="feedback",
                          size=(880, 800))
    texts = "\n".join(w.text() for w in _visible_labels(shot.dialog, q))
    missing = [i for i in range(6) if _member_marker(i) not in texts]
    assert missing == [], f"member(s) {missing} hidden in a feedback group"
    assert not [b for b in shot.dialog.findChildren(q.QPushButton)
                if b.text() in ("Show 6 cards", "Hide 6 cards")]


def _checked(dialog, q, label):
    return [b for b in dialog.findChildren(q.QPushButton)
            if b.text() == label and b.isVisible() and b.isChecked()]


def test_one_group_decision_sets_every_member():
    _, q = harness.bootstrap()
    shot = harness.render("confirm", group_size=5, group_kind="changed", size=(880, 800))
    dialog, app = shot.dialog, harness.app()
    keep_all = next(b for b in dialog.findChildren(q.QPushButton)
                    if b.text() == "Keep all yours")
    keep_all.click()
    app.processEvents()
    next(b for b in dialog.findChildren(q.QPushButton)
         if b.text() == "Show 5 cards").click()
    app.processEvents()
    assert len(_checked(dialog, q, "Keep yours")) == 5, (
        "a member built after the group decision must start on it")
    # Members were decided by the group, not clicked one by one, so no note box opens.
    boxes = [b for b in dialog.findChildren(q.QPlainTextEdit) if b.isVisible()]
    assert boxes == []


def test_a_member_decided_on_its_own_leaves_the_group_mixed():
    _, q = harness.bootstrap()
    shot = harness.render("confirm", group_size=5, group_kind="changed", size=(880, 800),
                          click_labels=("Show 5 cards",))
    dialog, app = shot.dialog, harness.app()
    keep = next(b for b in dialog.findChildren(q.QPushButton)
                if b.accessibleName() == "Keep yours: Group member card number 1?")
    keep.click()
    app.processEvents()
    assert not _checked(dialog, q, "Apply all") and not _checked(dialog, q, "Keep all yours")
    assert any(w.text() == "mixed" for w in _visible_labels(dialog, q))


def test_the_status_line_counts_unopened_cards_and_forgets_an_opened_group():
    _, q = harness.bootstrap()
    shot = harness.render("confirm", group_size=5, group_kind="changed", progress=True,
                          size=(880, 800))
    dialog, app = shot.dialog, harness.app()
    texts = "\n".join(w.text() for w in _visible_labels(dialog, q))
    assert "5 of 5 cards not opened yet</b>, 5 of them in folded groups" in texts
    next(b for b in dialog.findChildren(q.QPushButton)
         if b.text() == "Show 5 cards").click()
    app.processEvents()
    texts = "\n".join(w.text() for w in _visible_labels(dialog, q))
    assert "not opened yet" not in texts, "opening the group counts its cards as seen"


def test_folded_group_render_saved_as_png(tmp_path):
    out_dir = os.environ.get("IP_SHOT_DIR") or str(tmp_path)
    os.makedirs(out_dir, exist_ok=True)
    for theme in ("light", "dark"):
        shot = harness.render("confirm", theme=theme, group_size=12, group_kind="changed",
                              progress=True, second_deck=True, size=(880, 800))
        png = os.path.join(out_dir, f"folded-group-{theme}.png")
        shot.image.save(png, "PNG")
        assert os.path.exists(png)


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

    # A built member row is known by its caret, named for its card; the box's own
    # preview names the first few fronts too, so label text cannot tell them apart.
    def carets(visible_only=False):
        return [b for b in dialog.findChildren(q.QPushButton)
                if b.accessibleName().startswith("Show card: Group member card number")
                and (b.isVisible() or not visible_only)]

    def built():
        return carets()

    assert built() == [], "a folded group built member rows before it was expanded"

    toggle = next(b for b in dialog.findChildren(q.QPushButton)
                  if b.text() == f"Show {n} cards")
    toggle.click()
    harness.app().processEvents()
    texts = "\n".join(w.text() for w in _visible_labels(dialog, q))
    assert [i for i in range(n) if _member_marker(i) not in texts] == []

    toggle.click()
    harness.app().processEvents()
    assert carets(visible_only=True) == []
    assert len(built()) == n, "collapsing again must keep the rows, not rebuild them"


def _caret(dialog, q, marker):
    found = [b for b in dialog.findChildren(q.QPushButton)
             if b.isVisible() and b.accessibleName() == f"Show card: {marker}"]
    assert len(found) == 1, marker
    return widget_rect(dialog, found[0])


def _note_label(dialog, q, size):
    found = [l for l in _visible_labels(dialog, q)
             if f"an example reviewer note spanning {size} cards" in l.text()
             and "<i>" in l.text()]
    assert len(found) == 1
    return widget_rect(dialog, found[0])


def test_an_open_groups_members_sit_indented_under_its_note():
    """Members used to start at the same x as the rows around them, so nothing but a
    hairline said where the group ended."""
    _, q = harness.bootstrap()
    shot = harness.render("confirm", group_size=3, second_deck=True, size=(880, 800))
    d = shot.dialog
    members = [_caret(d, q, _member_marker(i)).left() for i in range(3)]
    plain = _caret(d, q, "Second deck's first pending card?").left()
    note = _note_label(d, q, 3).left()
    assert len(set(members)) == 1, members
    assert members[0] > plain and members[0] > note, (members[0], plain, note)


def test_a_group_members_text_starts_one_member_indent_right_of_a_plain_rows():
    """The one place rows leave the shared text column: a group's members, on purpose,
    by exactly review._GROUP_MEMBER_INDENT."""
    from internpearls import review
    _, q = harness.bootstrap()
    shot = harness.render("confirm", group_size=3, second_deck=True, size=(880, 800))

    def text_x(marker):
        found = [l for l in _visible_labels(shot.dialog, q) if marker in l.text()]
        assert len(found) == 1, marker
        return widget_rect(shot.dialog, found[0]).left()

    assert (text_x(_member_marker(0))
            == text_x("Second deck's first pending card?") + review._GROUP_MEMBER_INDENT)


def test_a_folded_groups_members_sit_as_far_in_from_its_note_as_an_open_groups():
    _, q = harness.bootstrap()
    shot = harness.render("confirm", group_size=3, size=(880, 800))
    open_delta = (_caret(shot.dialog, q, _member_marker(0)).left()
                  - _note_label(shot.dialog, q, 3).left())
    shot = harness.render("confirm", group_size=5, size=(880, 800),
                          click_labels=("Show 5 cards",))
    folded_delta = (_caret(shot.dialog, q, _member_marker(0)).left()
                    - _note_label(shot.dialog, q, 5).left())
    assert open_delta == folded_delta > 0, (open_delta, folded_delta)
