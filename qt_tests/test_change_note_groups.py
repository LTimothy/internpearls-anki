"""Real-Qt render checks for a grouped change note on the update confirmation: one
shared note over two cards, a retired row carrying its own reason, and a plain row
with no chip, all in the same section. See harness._scene_confirm's grouped=True."""
import os

import harness


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
