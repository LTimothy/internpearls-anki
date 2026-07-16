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
