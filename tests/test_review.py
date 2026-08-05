"""Tests for internpearls/review.py's pure row-content helpers.

`_primary_html` and `_answer_html` take a plain `detail` dict (the shape
`apkg_note_details` returns) and return strings, so they're testable directly without
building a dialog or driving the mock Qt widget tree. mock_anki's aqt stubs are already
installed by conftest.py before this module imports, same as every other test file here.
"""
import os

from internpearls import review

_ADDON_DIR = os.path.dirname(review.__file__)


def _image_note_detail(image_field='<img src="femoral.jpg">'):
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


def _basic_note_detail(image_field='<img src="femoral.jpg">'):
    return {
        "notetype": "Study Deck - Basic",
        "fields": [
            ("Front", "What nerve block covers the anterior thigh?"),
            ("Back", "Femoral nerve block"),
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
    assert "femoral.jpg" in primary
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
    assert "femoral.jpg" not in primary
    assert "femoral.jpg" in answer
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
    detail = _basic_note_detail()
    detail["fields"] = [(n, "" if n == "Tag" else v) for n, v in detail["fields"]]
    row_html = review._row_html(detail)
    assert f'<span style="color: {review._DIM};">' not in row_html
    assert "What nerve block covers the anterior thigh?" in row_html


def test_cloze_row_carries_the_preview_style_block_once():
    """_primary_html no longer prepends the style itself, so a row that dropped it
    would render its deletions in body text with no visible fill at all, and any table
    on the card with no gridlines."""
    row_html = review._row_html(_cloze_note_detail("The {{c1::lumbar}} plexus."))
    assert row_html.count(review._PREVIEW_STYLE) == 1
    assert '<span class="cloze">lumbar</span>' in row_html


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
    styles = [n.get("style") or "" for n in _row_nodes(_basic_note_detail())]
    why = next(s for s in styles if "border-left" in s)
    assert why.index("border: none") < why.index("border-left")
    assert review._WHY_RULE in why


def test_no_card_row_widget_carries_a_border_of_its_own():
    """The rule between cards belongs to a separator widget. Set on the row instead,
    a selector-less stylesheet propagates into every child, so each row drew a second
    inset rule under its own header on top of its own.
    """
    for node in _row_nodes(_basic_note_detail(), collect_feedback=True):
        assert "border-bottom" not in (node.get("style") or "")


def test_separator_is_an_hline_carrying_the_rule_colour():
    node = review._separator().node()
    assert node["t"] == "hline"
    assert review._ROW_RULE in node["style"]


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
def _apkg_with_image(tmp_path, name="femoral.jpg"):
    """A minimal .apkg carrying one media file, enough for the resolver to find it."""
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
    with zipfile.ZipFile(path, "w") as z:
        z.write(db, "collection.anki2")
        z.writestr("0", b"pretend image bytes")
        z.writestr("media", json.dumps({"0": name}))
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
        _apkg_with_image(tmp_path), {"femoral.jpg": "0"}, str(tmp_path / "out"))
    row = review._card_row(dict(_basic_note_detail(), guid="g1"), {}, {}, False,
                           resolve=resolve)
    texts = " ".join(n.get("text") or "" for n in _walk(row.node()))
    assert "[image: femoral.jpg]" in texts
    assert "<img" not in texts
    assert not os.path.exists(str(tmp_path / "out"))


def test_expanding_a_row_renders_its_image_for_real(tmp_path):
    resolve = review._media_resolver(
        _apkg_with_image(tmp_path), {"femoral.jpg": "0"}, str(tmp_path / "out"))
    row = review._card_row(dict(_basic_note_detail(), guid="g1"), {}, {}, False,
                           resolve=resolve)
    caret = next(n for n in _walk(row.node()) if n.get("t") == "button")
    _click(caret["id"], row)
    texts = " ".join(n.get("text") or "" for n in _walk(row.node()))
    assert "<img src=" in texts and "femoral.jpg" in texts
    assert f'width="{review._IMAGE_MAX_W}"' in texts


def test_a_row_with_no_resolver_keeps_naming_its_image():
    """Sync can hand over a deck whose .apkg could not be read. That row must still
    render, exactly as it did before pictures were possible."""
    row = review._card_row(dict(_basic_note_detail(), guid="g1"), {}, {}, False)
    caret = next(n for n in _walk(row.node()) if n.get("t") == "button")
    _click(caret["id"], row)
    texts = " ".join(n.get("text") or "" for n in _walk(row.node()))
    assert "[image: femoral.jpg]" in texts and "<img" not in texts


def test_image_tag_declines_a_file_qt_cannot_decode(tmp_path):
    assert review._image_tag(str(tmp_path / "nothing-here.jpg")) is None


# ------------------------------------------------------------------ mock Qt surface
def test_the_mock_qt_provides_the_qimage_review_reads_widths_from():
    """review.py reads an extracted file's natural width to cap it. The mock has to
    carry that name or every image test fails on import rather than on behaviour."""
    from aqt.qt import QImage
    assert QImage("/definitely/not/a/file.jpg").isNull() is True
    assert QImage(__file__).isNull() is False
    assert QImage(__file__).width() > 0
