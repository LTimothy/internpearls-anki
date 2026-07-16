"""Tests for internpearls/review.py's pure row-content helpers.

`_primary_html` and `_answer_html` take a plain `detail` dict (the shape
`apkg_note_details` returns) and return strings, so they're testable directly without
building a dialog or driving the mock Qt widget tree. mock_anki's aqt stubs are already
installed by conftest.py before this module imports, same as every other test file here.
"""
from internpearls import review


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
    assert review._DIM not in row_html
    assert "What nerve block covers the anterior thigh?" in row_html


def test_cloze_row_carries_the_cloze_style_block_once():
    """_primary_html no longer prepends the style itself, so a row that dropped it
    would render its deletions in body text with no visible fill at all."""
    row_html = review._row_html(_cloze_note_detail("The {{c1::lumbar}} plexus."))
    assert row_html.count(review._CLOZE_STYLE) == 1
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
