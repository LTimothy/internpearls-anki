"""Tests for internpearls/review.py's pure row-content helpers.

`_primary_html` and `_answer_html` take a plain `detail` dict (the shape
`apkg_note_details` returns) and return strings, so they're testable directly without
building a dialog or driving the mock Qt widget tree. mock_anki's aqt stubs are already
installed by conftest.py before this module imports, same as every other test file here.
"""
import os

from internpearls import review

_ADDON_DIR = os.path.dirname(review.__file__)


def _image_note_detail(image_field='<img src="sample-a.jpg">'):
    return {
        "notetype": "Study Deck - Image ID",
        "fields": [
            ("Image", image_field),
            ("Prompt", "Which block does this coverage map show?"),
            ("Answer", "Femoral nerve block"),
            ("Why", "runs with the saphenous nerve along the same sheath"),
            ("Notes", ""),
        ],
    }


def _basic_note_detail(image_field='<img src="sample-a.jpg">', back="Femoral nerve block"):
    return {
        "notetype": "Study Deck - Basic",
        "fields": [
            ("Front", "What nerve block covers the anterior thigh?"),
            ("Back", back),
            ("Why", "runs with the saphenous nerve along the same sheath"),
            ("Image", image_field),
            ("Tag", "Pharm"),
            ("Dosing", ""),
            ("Notes", ""),
        ],
    }


def test_image_note_primary_line_names_its_image():
    """The picture is the question on an image note, so its collapsed-row line
    (_primary_html) has to name it or a reader can't tell which image is under
    review. This is the exact regression: Image landing in _STRUCTURAL_FIELDS
    dropped it from every line, collapsed and expanded alike."""
    primary = review._primary_html(_image_note_detail())
    assert "sample-a.jpg" in primary
    assert "Which block does this coverage map show?" in primary
    assert "<img" not in primary


def test_image_note_with_no_image_field_value_has_no_bracket_tag():
    primary = review._primary_html(_image_note_detail(image_field=""))
    assert "[image:" not in primary
    assert "Which block does this coverage map show?" in primary


def test_basic_note_answer_names_its_image_when_expanded():
    """A basic card's image sits on the back, so it belongs with the answer
    (_answer_html), not the collapsed primary line."""
    primary = review._primary_html(_basic_note_detail())
    answer = review._answer_html(_basic_note_detail())
    assert "sample-a.jpg" not in primary
    assert "sample-a.jpg" in answer
    assert "Femoral nerve block" in answer
    assert "<img" not in primary
    assert "<img" not in answer


def test_basic_note_without_an_image_is_unaffected():
    answer = review._answer_html(_basic_note_detail(image_field=""))
    assert answer == "Femoral nerve block"


def _cloze_note_detail(text, image_field=""):
    return {
        "notetype": "Study Deck - Cloze",
        "fields": [
            ("Text", text),
            ("Why", "why text"),
            ("Image", image_field),
            ("Dosing", ""),
            ("Notes", ""),
        ],
    }


def test_cloze_note_primary_line_still_fills_deletions_not_images():
    """Plain-cloze behavior, unchanged by the inline-HTML fix: a Text field with no
    markup of its own still just fills its deletion, and _answer_html stays empty."""
    detail = _cloze_note_detail("The {{c1::lumbar}} plexus is compressed.")
    assert "lumbar" in review._primary_html(detail)
    assert review._answer_html(detail) == ""


def test_cloze_note_with_inline_image_names_it_with_no_escaped_markup():
    """A real cloze Text field carries its own HTML: an inline <img>, &nbsp;, <br>.
    Escaping the whole field (the regression) dumps that markup into the row as
    visible text and never names the image. field_preview_text first strips it to
    plain text with the image named, leaving {{c1::...}} for cloze_filled_html."""
    detail = _cloze_note_detail(
        '<img src="ecg-strip.jpg">&nbsp;<br>increasing the tidal volume would '
        '{{c1::increase}} it'
    )
    primary = review._primary_html(detail)
    assert "&lt;img" not in primary
    assert "[image: ecg-strip.jpg]" in primary
    assert '<span class="cloze">increase</span>' in primary
    assert "increasing the tidal volume would" in primary


def test_cloze_note_with_escaped_comparator_renders_inside_its_deletion():
    """A spec-escaped comparator inside a deletion (&lt;94%, meaning a literal '<')
    must still round-trip to a real '<' inside the filled span, not stay escaped
    text or get mangled by the tag-stripping pass."""
    detail = _cloze_note_detail("SpO2 {{c1::&lt;94%}} is low")
    primary = review._primary_html(detail)
    assert '<span class="cloze">&lt;94%</span>' in primary
    assert "SpO2" in primary
    assert "is low" in primary


def test_cloze_note_with_populated_image_field_names_it_when_expanded():
    """Study Deck - Cloze also has its own Image field, separate from anything inline
    in Text. It should still get named somewhere, consistent with how a basic note's
    Image field is named in its (expand-only) answer area."""
    detail = _cloze_note_detail(
        "The {{c1::lumbar}} plexus is compressed.",
        image_field='<img src="lumbar-plexus.jpg">',
    )
    answer = review._answer_html(detail)
    assert "lumbar-plexus.jpg" in answer
    assert "<img" not in answer


# ------------------------------------------------------- structure survives preview
_TABLE_BACK = (
    "<table><tr><th></th><th>PTH</th></tr>"
    "<tr><td>Primary</td><td>&#8593;</td></tr></table>"
)


def test_basic_note_answer_keeps_its_table_instead_of_flattening_it():
    """The defect this exists to prevent: a card back written as a table arrived as a
    run-on line of cell text, so real feedback came back calling a correct card
    "just jumbled text". The grid is the answer; it has to survive the preview."""
    detail = _basic_note_detail(image_field="")
    detail["fields"] = [(n, _TABLE_BACK if n == "Back" else v)
                        for n, v in detail["fields"]]
    answer = review._answer_html(detail)
    assert "<table>" in answer and "<td>" in answer
    assert "&lt;table" not in answer
    assert "&#8593;" in answer          # the arrow is still an entity, not escaped


def test_cloze_note_keeps_a_table_around_its_deletions():
    """A cloze whose Text field is a table (one row blanked per card) is the shape that
    breaks hardest when flattened: every cell runs together and the rows are lost."""
    detail = _cloze_note_detail(
        "PTH:<table><tr><td>Primary</td><td>{{c1::&#8593;}}</td></tr></table>")
    primary = review._primary_html(detail)
    assert "<table>" in primary
    assert '<span class="cloze">&#8593;</span>' in primary


def test_bulleted_answer_keeps_its_list_markup():
    detail = _basic_note_detail(image_field="")
    detail["fields"] = [
        (n, '<ul style="padding-left:1.1em;"><li>Etomidate</li><li>Alfentanil</li></ul>'
            if n == "Back" else v)
        for n, v in detail["fields"]]
    answer = review._answer_html(detail)
    assert answer.count("<li>") == 2
    assert "<ul" in answer


def test_preview_drops_tags_it_cannot_render_but_keeps_their_text():
    detail = _basic_note_detail(image_field="")
    detail["fields"] = [
        (n, '<a href="http://x">Barash</a>, page 551' if n == "Back" else v)
        for n, v in detail["fields"]]
    answer = review._answer_html(detail)
    assert "<a " not in answer and "href" not in answer
    assert "Barash" in answer and "page 551" in answer


# --------------------------------------------------------------- row composition
def test_tagged_row_carries_its_tag_in_the_same_line_as_its_primary_text():
    """Tag and primary text must be one label, not two widgets side by side: two
    widgets start each row's text at a different x depending on whether that card
    carries a tag, which reads as a ragged left edge down the list."""
    row_html = review._row_html(_basic_note_detail())
    assert "Pharm" in row_html
    assert "What nerve block covers the anterior thigh?" in row_html


def test_untagged_row_has_no_empty_tag_lead_in():
    from internpearls import palette
    detail = _basic_note_detail()
    detail["fields"] = [(n, "" if n == "Tag" else v) for n, v in detail["fields"]]
    row_html = review._row_html(detail)
    assert f'<span style="color: {palette.colors()["dim"]};">' not in row_html
    assert "What nerve block covers the anterior thigh?" in row_html


def test_cloze_row_carries_the_preview_style_block_once():
    """_primary_html no longer prepends the style itself, so a row that dropped it
    would render its deletions in body text with no visible fill at all, and any table
    on the card with no gridlines."""
    row_html = review._row_html(_cloze_note_detail("The {{c1::lumbar}} plexus."))
    assert row_html.count(review._preview_style()) == 1
    assert '<span class="cloze">lumbar</span>' in row_html


def test_review_holds_no_colour_literals_of_its_own():
    """Every colour has to come from the palette, or a theme switch silently misses one.
    Checked against the source rather than a render, since a literal that is only reached
    on one code path would not show up in any single dialog."""
    import re
    source = open(os.path.join(_ADDON_DIR, "review.py"), encoding="utf8").read()
    literals = set(re.findall(r"#[0-9a-fA-F]{6}\b", source))
    assert not literals, f"review.py still hardcodes {sorted(literals)}"


def test_row_markers_use_the_active_palette():
    from internpearls import palette
    markup = review._row_html(dict(_basic_note_detail(), kind="new"))
    active = palette.colors()
    assert active["new_bg"] in markup and active["new_fg"] in markup


# ------------------------------------------------------------- rendered structure
def _walk(node, out=None):
    out = out if out is not None else []
    out.append(node)
    for c in node.get("children", []) or []:
        _walk(c, out)
    return out


def _row_nodes(detail, collect_feedback=False):
    row = review._card_row(dict(detail, guid="g1"), {}, {}, collect_feedback)
    return _walk(row.node())


def test_why_rule_resets_the_border_shorthand_before_setting_border_left():
    """Qt silently ignores a lone border-left on a QLabel unless the border shorthand
    is set first: the padding still applies, so the why reads as deliberately indented
    and the green rule it's supposed to hang off never paints. Asserted on the
    stylesheet because no mock, and no headless Qt, can be asked whether it painted.
    """
    from internpearls import palette
    styles = [n.get("style") or "" for n in _row_nodes(_basic_note_detail())]
    why = next(s for s in styles if "border-left" in s)
    assert why.index("border: none") < why.index("border-left")
    assert palette.colors()["why"] in why


def test_no_card_row_widget_carries_a_border_of_its_own():
    """The rule between cards belongs to a separator widget. Set on the row instead,
    a selector-less stylesheet propagates into every child, so each row drew a second
    inset rule under its own header on top of its own.
    """
    for node in _row_nodes(_basic_note_detail(), collect_feedback=True):
        assert "border-bottom" not in (node.get("style") or "")


# ----------------------------------------------------------------- new vs changed
def test_a_new_row_and_a_changed_row_are_marked_differently():
    new_html = review._row_html(dict(_basic_note_detail(), kind="new"))
    changed_html = review._row_html(dict(_basic_note_detail(), kind="changed"))
    assert "NEW" in new_html and "UPDATED" not in new_html
    assert "UPDATED" in changed_html and "NEW" not in changed_html


def test_an_unmarked_row_carries_no_marker_at_all():
    """A review opened for one kind only, and every pre-existing caller, must render
    exactly as before."""
    assert "NEW" not in review._row_html(_basic_note_detail())
    assert "UPDATED" not in review._row_html(_basic_note_detail())


def test_every_marker_sets_a_foreground_with_its_background():
    """The v0.32.1 rule, applied to markup rather than to a stylesheet: this one lives
    inside the row's rich text, so the setStyleSheet lint cannot see it.

    Matched on "; color:" rather than "color:", which "background-color:" contains and
    would pass this test with no foreground set at all.
    """
    for kind in ("new", "changed"):
        markup = review._row_html(dict(_basic_note_detail(), kind=kind))
        span = markup[markup.index("background-color"):]
        span = span[:span.index(">")]
        assert "; color:" in span, f"{kind} marker sets a background with no foreground"


def _luminance(hex_colour):
    """WCAG relative luminance of a "#rrggbb" string, 0.0 (black) to 1.0 (white)."""
    hex_colour = hex_colour.lstrip("#")
    r, g, b = (int(hex_colour[i:i + 2], 16) / 255.0 for i in (0, 2, 4))

    def channel(v):
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _contrast_ratio(a, b):
    """WCAG contrast ratio between two "#rrggbb" strings: 1.0 identical, 21.0 black on
    white."""
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_every_marker_pill_clears_wcag_aa_against_its_own_background():
    """qt_tests/test_contrast.py cannot see this: it measures one dominant foreground
    and background per widget, and a row's own body text always outcompetes its small
    inline marker for that spot, so the pill's own colour pair is structurally never
    what that suite samples. This test is what stands guard over the pair instead.

    A bare, foreground-only marker was the obvious design and isn't available: measured
    against the render suite's own light and dark window colours, no single colour
    clears AA on both, so a marker without a matched background would be unreadable on
    one of the two themes. The ratio is computed rather than hardcoded, so a changed
    colour is re-checked rather than just trusted.
    """
    from internpearls import palette
    AA = 4.5
    active = palette.colors()
    pairs = {"new": (active["new_bg"], active["new_fg"]),
             "changed": (active["updated_bg"], active["updated_fg"])}
    for kind, label in review._MARKER_LABELS.items():
        background, foreground = pairs[kind]
        ratio = _contrast_ratio(background, foreground)
        assert ratio >= AA, (
            f"{kind} marker ({label}) is {ratio:.2f}:1, foreground {foreground} on "
            f"background {background}; WCAG AA needs {AA}:1")


def test_a_changed_row_shows_what_the_field_used_to_say():
    detail = dict(_basic_note_detail(), guid="g1", kind="changed",
                  was={"Back": "the answer she has today"})
    texts = " ".join(n.get("text") or "" for n in _walk(
        review._card_row(detail, {}, {}, False).node()))
    assert "the answer she has today" in texts
    assert "was" in texts


def test_a_changed_row_shows_nothing_extra_for_an_unchanged_field():
    detail = dict(_basic_note_detail(), guid="g1", kind="changed",
                  was={"Back": "old back"})
    texts = " ".join(n.get("text") or "" for n in _walk(
        review._card_row(detail, {}, {}, False).node()))
    assert "runs with the saphenous nerve" in texts   # the Why, rendered once
    assert texts.count("was") == 1


def _text_nodes(detail):
    """Every label's text in a card row, in the same depth-first order the layout
    walks them, so a test can compare *positions* rather than just presence."""
    row = review._card_row(dict(detail, guid="g1"), {}, {}, False)
    return [n.get("text") or "" for n in _walk(row.node()) if n.get("t") == "label"]


def _first_index(texts, needle):
    return next(i for i, t in enumerate(texts) if needle in t)


def test_a_changed_why_shows_its_previous_value_after_the_current_one():
    """The defect this guards: every `was` line used to land at one fixed spot, after
    the answer and before Why, so a changed Why's previous value rendered ABOVE the
    current Why instead of under it. Positional, not just "both appear": the pre-fix
    layout puts the fixed slot (and so this `was` line) before the Why block, which
    would put `was_i` before `current_i` and fail this assertion.
    """
    detail = dict(_basic_note_detail(), guid="g1", kind="changed",
                  was={"Why": "an older explanation of the same mechanism"})
    texts = _text_nodes(detail)
    current_i = _first_index(texts, "runs with the saphenous nerve")
    was_i = _first_index(texts, "an older explanation of the same mechanism")
    assert was_i > current_i


def test_a_changed_dosing_shows_its_previous_value_after_the_current_one():
    """Same defect as Why, for Dosing: the fixed slot sat before the dosing block, so
    a changed Dosing's `was` line rendered above the current dosing instead of under
    it. Positional for the same reason as the Why test above.
    """
    detail = dict(_basic_note_detail(), guid="g1", kind="changed")
    detail["fields"] = [(n, "0.5 mg/kg IV" if n == "Dosing" else v)
                        for n, v in detail["fields"]]
    detail["was"] = {"Dosing": "1 mg/kg IV, per an older source"}
    texts = _text_nodes(detail)
    current_i = _first_index(texts, "0.5 mg/kg IV")
    was_i = _first_index(texts, "1 mg/kg IV, per an older source")
    assert was_i > current_i


def test_a_changed_primary_field_shows_its_previous_value_first_in_the_body():
    """Front is the collapsed header line, never rendered inside the expandable body
    at all, so its `was` line used to appear detached from anything, sitting in the
    fixed slot after the answer. It belongs first in the body instead, immediately
    under the header it describes. Checked as adjacency (immediately after the header
    label) rather than just "before the answer": the pre-fix layout also puts it
    before Why and Dosing, so a looser check would pass against the defect too.
    """
    detail = dict(_basic_note_detail(), guid="g1", kind="changed",
                  was={"Front": "what block used to cover the anterior thigh?"})
    texts = _text_nodes(detail)
    header_i = _first_index(texts, "What nerve block covers the anterior thigh?")
    was_i = _first_index(texts, "what block used to cover the anterior thigh?")
    assert was_i == header_i + 1


def test_two_changed_fields_show_was_lines_after_their_own_fields_in_order():
    """Why and Dosing changed together: each `was` line lands after its own current
    field, not both dumped together in one spot, and the two lines keep the note
    type's own field order (Why before Dosing). Fully positional: the pre-fix fixed
    slot puts both `was` lines before either block renders, so `why_i < why_was_i`
    alone already fails against it (why_was_i would be smaller, not larger).
    """
    detail = dict(_basic_note_detail(), guid="g1", kind="changed")
    detail["fields"] = [(n, "0.5 mg/kg IV" if n == "Dosing" else v)
                        for n, v in detail["fields"]]
    detail["was"] = {"Why": "an older explanation of the same mechanism",
                     "Dosing": "1 mg/kg IV, per an older source"}
    texts = _text_nodes(detail)
    why_i = _first_index(texts, "runs with the saphenous nerve")
    why_was_i = _first_index(texts, "an older explanation of the same mechanism")
    dosing_i = _first_index(texts, "0.5 mg/kg IV")
    dosing_was_i = _first_index(texts, "1 mg/kg IV, per an older source")
    assert why_i < why_was_i < dosing_i < dosing_was_i


def test_separator_is_an_hline_carrying_the_rule_colour():
    from internpearls import palette
    node = review._separator().node()
    assert node["t"] == "hline"
    assert palette.colors()["row_rule"] in node["style"]


def test_no_widget_sets_a_background_without_setting_a_foreground():
    """Text colour comes from the platform palette when a style doesn't set it, and
    the palette flips under Night Mode while a hardcoded background does not. So a
    background-only style renders white-on-light in dark mode, which is what the
    dosing block did. A colour-only style is safe; this is about backgrounds.
    """
    detail = dict(_basic_note_detail())
    detail["fields"] = [(n, "0.5 mg IV" if n == "Dosing" else v)
                        for n, v in detail["fields"]]
    styled = [n.get("style") or "" for n in _row_nodes(detail, collect_feedback=True)]
    offenders = [s for s in styled if "background" in s and "color:" not in s]
    assert not offenders, f"background with no foreground: {offenders}"


def _stylesheet_literals():
    """Every setStyleSheet() call in the add-on, as the literal text it applies.

    Read from the source with ast rather than from rendered widgets, because the
    rendered check above only ever sees the widgets one dialog happens to build. The
    digest box was a QPlainTextEdit in a different dialog entirely, so it sat outside
    that check for four versions while shipping the exact bug the check exists for.
    An f-string's placeholders drop out and its literal parts remain, which is all this
    needs: "background:" and "color:" are always literal.
    """
    import ast
    import glob
    out = []
    for path in sorted(glob.glob(os.path.join(_ADDON_DIR, "*.py"))):
        tree = ast.parse(open(path, encoding="utf8").read())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "setStyleSheet" and node.args):
                continue
            parts = []
            for piece in ast.walk(node.args[0]):
                if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                    parts.append(piece.value)
            out.append((os.path.basename(path), "".join(parts)))
    return out


def test_no_stylesheet_anywhere_sets_a_background_without_a_foreground():
    """The rendered check above, applied to every widget in the add-on rather than to
    the card rows of one dialog. A hardcoded background with the text colour left to
    the palette renders white-on-near-white in Night Mode: it did it once in the dosing
    block (v0.32.1) and once in the feedback digest, where it measured 1.34:1 and made
    the one thing that dialog exists to show unreadable.
    """
    offenders = [(mod, css) for mod, css in _stylesheet_literals()
                 if "background" in css and "color:" not in css]
    assert not offenders, f"background with no foreground: {offenders}"


# --------------------------------------------------------------------- images
def _apkg_with_image(tmp_path, name="sample-a.jpg"):
    """A minimal .apkg carrying one media file, enough for the resolver to find it."""
    return _apkg_with_images(tmp_path, [name])


def _apkg_with_images(tmp_path, names):
    """A minimal .apkg carrying several media files, so a resolver built against it can
    find some names and not others (whichever weren't included)."""
    import json
    import sqlite3
    import zipfile
    path = str(tmp_path / "deck.apkg")
    db = path + ".tmp.db"
    con = sqlite3.connect(db)
    con.execute("create table notes (id integer primary key, guid text, mid integer, "
                "flds text)")
    con.commit()
    con.close()
    media = {}
    with zipfile.ZipFile(path, "w") as z:
        z.write(db, "collection.anki2")
        for i, name in enumerate(names):
            z.writestr(str(i), b"pretend image bytes")
            media[str(i)] = name
        z.writestr("media", json.dumps(media))
    os.remove(db)
    return path


def _click(wid, root):
    """Fire the clicked signal of the widget with this id, found by walking the real
    widget objects rather than the node dicts (which carry no callables). mock_anki's
    layouts keep their children in `_children`, not `_items`."""
    seen, stack = set(), [root]
    while stack:
        w = stack.pop()
        if id(w) in seen:
            continue
        seen.add(id(w))
        if getattr(w, "wid", None) == wid and hasattr(w, "clicked"):
            w.clicked.emit()
            return True
        stack.extend(v for v in vars(w).values() if hasattr(v, "wid"))
        layout = getattr(w, "_layout", None)
        if layout is not None:
            stack.extend(getattr(layout, "_children", []) or [])
    return False


def test_a_row_names_its_image_until_it_is_expanded(tmp_path):
    """Collapsed rows stay one line each. Extraction is what expanding pays for, so a
    review nobody opens extracts nothing."""
    resolve = review._media_resolver(
        _apkg_with_image(tmp_path), {"sample-a.jpg": "0"}, str(tmp_path / "out"))
    row = review._card_row(dict(_basic_note_detail(), guid="g1"), {}, {}, False,
                           resolve=resolve)
    texts = " ".join(n.get("text") or "" for n in _walk(row.node()))
    assert "[image: sample-a.jpg]" in texts
    assert "<img" not in texts
    assert not os.path.exists(str(tmp_path / "out"))


def test_expanding_a_row_renders_its_image_for_real(tmp_path):
    resolve = review._media_resolver(
        _apkg_with_image(tmp_path), {"sample-a.jpg": "0"}, str(tmp_path / "out"))
    row = review._card_row(dict(_basic_note_detail(), guid="g1"), {}, {}, False,
                           resolve=resolve)
    caret = next(n for n in _walk(row.node()) if n.get("t") == "button")
    _click(caret["id"], row)
    texts = " ".join(n.get("text") or "" for n in _walk(row.node()))
    assert "<img src=" in texts and "sample-a.jpg" in texts
    assert f'width="{review._IMAGE_MAX_W}"' in texts


def test_expanding_a_row_rerenders_a_back_field_inline_image_in_place(tmp_path):
    """The other half of the image fix: a field rendered in the body (here the answer,
    from Back) that carries its own inline <img> re-renders itself through the
    `rerender` list, separately from the picture strip `_primary_images` feeds. A basic
    note with no dedicated Image field value exercises that path in isolation, since the
    strip has nothing to show and cannot be the one putting a real <img> on the page.
    """
    detail = _basic_note_detail(image_field="")
    detail["fields"] = [
        (n, '<img src="sample-a.jpg"> Femoral nerve block' if n == "Back" else v)
        for n, v in detail["fields"]]
    resolve = review._media_resolver(
        _apkg_with_image(tmp_path), {"sample-a.jpg": "0"}, str(tmp_path / "out"))
    row = review._card_row(dict(detail, guid="g1"), {}, {}, False, resolve=resolve)

    before = next(n for n in _walk(row.node())
                 if n.get("t") == "label" and "Femoral nerve block" in (n.get("text") or ""))
    assert "[image: sample-a.jpg]" in before["text"]
    assert "<img" not in before["text"]
    answer_id = before["id"]

    caret = next(n for n in _walk(row.node()) if n.get("t") == "button")
    _click(caret["id"], row)

    after = next(n for n in _walk(row.node()) if n.get("id") == answer_id)
    assert "<img src=" in after["text"] and "sample-a.jpg" in after["text"]
    assert "Femoral nerve block" in after["text"]


def test_expanding_a_row_rerenders_a_why_fields_inline_image_in_place(tmp_path):
    """The highest-value case for the in-place path: most real pictures live in Why,
    not in a dedicated Image field. A basic note with no Image field value and a plain
    (image-free) Back isolates it the same way the Back test above isolates its field.
    """
    detail = _basic_note_detail(image_field="")
    detail["fields"] = [
        (n, '<img src="mechanism.png"> runs with the saphenous nerve along the same sheath'
            if n == "Why" else v)
        for n, v in detail["fields"]]
    resolve = review._media_resolver(
        _apkg_with_image(tmp_path, name="mechanism.png"), {"mechanism.png": "0"},
        str(tmp_path / "out"))
    row = review._card_row(dict(detail, guid="g1"), {}, {}, False, resolve=resolve)

    before = next(n for n in _walk(row.node())
                 if n.get("t") == "label" and "saphenous nerve" in (n.get("text") or ""))
    assert "[image: mechanism.png]" in before["text"]
    assert "<img" not in before["text"]
    why_id = before["id"]

    caret = next(n for n in _walk(row.node()) if n.get("t") == "button")
    _click(caret["id"], row)

    after = next(n for n in _walk(row.node()) if n.get("id") == why_id)
    assert "<img src=" in after["text"] and "mechanism.png" in after["text"]
    assert "saphenous nerve" in after["text"]


def test_a_row_with_no_resolver_keeps_naming_its_image():
    """Sync can hand over a deck whose .apkg could not be read. That row must still
    render, exactly as it did before pictures were possible."""
    row = review._card_row(dict(_basic_note_detail(), guid="g1"), {}, {}, False)
    caret = next(n for n in _walk(row.node()) if n.get("t") == "button")
    _click(caret["id"], row)
    texts = " ".join(n.get("text") or "" for n in _walk(row.node()))
    assert "[image: sample-a.jpg]" in texts and "<img" not in texts


def test_image_tag_declines_a_file_qt_cannot_decode(tmp_path):
    assert review._image_tag(str(tmp_path / "nothing-here.jpg")) is None


def test_expanding_a_row_drops_the_chip_once_the_strip_paints_the_picture(tmp_path):
    """The bug this guards: a picture and its `[image: name]` chip both rendering,
    stacked, for the same file. Before the fix, `_answer_html` baked the chip into the
    answer label's text once, at construction time, and nothing ever revisited it, so
    it survived a successful expand unchanged sitting right under the picture it names.
    """
    resolve = review._media_resolver(
        _apkg_with_image(tmp_path), {"sample-a.jpg": "0"}, str(tmp_path / "out"))
    row = review._card_row(dict(_basic_note_detail(), guid="g1"), {}, {}, False,
                           resolve=resolve)
    caret = next(n for n in _walk(row.node()) if n.get("t") == "button")
    _click(caret["id"], row)
    texts = " ".join(n.get("text") or "" for n in _walk(row.node()))
    assert "<img src=" in texts and "sample-a.jpg" in texts   # the strip painted it
    assert "[image:" not in texts, (
        "the answer still names a picture the strip already painted")
    assert "Femoral nerve block" in texts   # the rest of the answer survives


def test_expanding_a_row_keeps_the_chip_when_the_picture_cannot_be_resolved():
    """The other direction: a name missing from the archive (or any other resolution
    failure) must keep the chip, since naming the picture is the fallback for exactly
    this case."""
    resolve = review._media_resolver("unused.apkg", {}, "unused-dest")
    row = review._card_row(dict(_basic_note_detail(), guid="g1"), {}, {}, False,
                           resolve=resolve)
    caret = next(n for n in _walk(row.node()) if n.get("t") == "button")
    _click(caret["id"], row)
    texts = " ".join(n.get("text") or "" for n in _walk(row.node()))
    assert "<img src=" not in texts
    assert "[image: sample-a.jpg]" in texts


def test_a_changed_rows_previous_image_is_not_resolved_before_expand(monkeypatch):
    """A collapsed changed row's `was` line resolves its picture through
    `_collection_image`, a real `mw.col.media.dir()` call, an `os.path.exists`, and a
    QImage decode per picture. Building the row must not pay for any of that before it
    is ever opened, same invariant the strip's own resolver already keeps: a review
    nobody opens costs nothing.
    """
    calls = []
    real = review._collection_image

    def spy(name):
        calls.append(name)
        return real(name)

    monkeypatch.setattr(review, "_collection_image", spy)
    detail = dict(_basic_note_detail(), guid="g1", kind="changed",
                  was={"Back": '<img src="sample-b.jpg">'})
    row = review._card_row(detail, {}, {}, False)
    assert calls == [], "the was-line resolved its picture before the row was ever opened"

    caret = next(n for n in _walk(row.node()) if n.get("t") == "button")
    _click(caret["id"], row)
    assert calls == ["sample-b.jpg"], "expanding the row should resolve it exactly once"


def test_expanding_a_row_resolves_an_image_field_by_name_not_all_or_nothing(tmp_path):
    """An Image field can hold more than one picture, and the strip resolves each one
    independently: one missing file must not take the rest of the chip down with it, and
    one successful file must not clear a name that never resolved. Every existing image
    test either fully succeeds or fully fails, so the per-name filter (`_image_text`'s
    `resolved` set) has never actually been asked to keep some names and drop others in
    the same chip.

    That filter alone can't be made to fail on its own: with no other picture in the
    answer, a non-image note whose Image field holds anything at all was always routed
    around the old write-twice bug entirely (its answer only ever went through the
    single, already-correct guarded write). So `mechanism.png` is added as the answer's
    own separate inline picture, giving this test the same failure mode
    `test_expanding_a_row_renders_an_answers_own_inline_image_in_place` guards, on top of
    the per-name filter it's actually here to check.
    """
    apkg = _apkg_with_images(tmp_path, ["sample-a.jpg", "mechanism.png"])
    resolve = review._media_resolver(
        apkg, {"sample-a.jpg": "0", "mechanism.png": "1"}, str(tmp_path / "out"))
    detail = _basic_note_detail(
        image_field='<img src="sample-a.jpg"><img src="sample-b.jpg">',
        back='<img src="mechanism.png"> Femoral nerve block',
    )
    row = review._card_row(dict(detail, guid="g1"), {}, {}, False, resolve=resolve)
    caret = next(n for n in _walk(row.node()) if n.get("t") == "button")
    _click(caret["id"], row)
    texts = " ".join(n.get("text") or "" for n in _walk(row.node()))
    assert "[image: sample-a.jpg]" not in texts, "a resolved name must drop its chip"
    assert "[image: sample-b.jpg]" in texts, "an unresolved name must keep its chip"
    assert "<img src=" in texts and "mechanism.png" in texts
    assert "[image: mechanism.png]" not in texts


def test_expanding_a_row_renders_an_answers_own_inline_image_in_place(tmp_path):
    """The regression this guards: the answer label used to be written twice on first
    expand when a strip picture also resolved, first in place (with the resolver) and
    then a second time to strip the Image field's now-redundant chip, and that second
    write dropped the resolver, silently reverting the answer's own inline picture back
    to a chip. Needs both a strip picture (an Image field that resolves, so the old
    second write actually ran) and a separate inline picture in the answer itself.
    """
    apkg = _apkg_with_images(tmp_path, ["sample-a.jpg", "mechanism.png"])
    resolve = review._media_resolver(
        apkg, {"sample-a.jpg": "0", "mechanism.png": "1"}, str(tmp_path / "out"))
    detail = _basic_note_detail(
        image_field='<img src="sample-a.jpg">',
        back='<img src="mechanism.png"> Femoral nerve block',
    )
    row = review._card_row(dict(detail, guid="g1"), {}, {}, False, resolve=resolve)
    caret = next(n for n in _walk(row.node()) if n.get("t") == "button")
    _click(caret["id"], row)
    texts = " ".join(n.get("text") or "" for n in _walk(row.node()))
    assert "<img src=" in texts and "mechanism.png" in texts
    assert "[image: mechanism.png]" not in texts


# ------------------------------------------------------------------ mock Qt surface
def test_the_mock_qt_provides_the_qimage_review_reads_widths_from():
    """review.py reads an extracted file's natural width to cap it. The mock has to
    carry that name or every image test fails on import rather than on behaviour."""
    from aqt.qt import QImage
    assert QImage("/definitely/not/a/file.jpg").isNull() is True
    assert QImage(__file__).isNull() is False
    assert QImage(__file__).width() > 0
