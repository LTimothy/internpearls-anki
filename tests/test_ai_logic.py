"""Pure-logic tests for AI card generation. No Anki install needed."""
from internpearls import ai_logic


def test_generated_guid_prefix_and_uniqueness():
    a, b = ai_logic.generated_guid(), ai_logic.generated_guid()
    assert a.startswith("iplocal-") and b.startswith("iplocal-")
    assert a != b
    assert len(a) <= 64   # anki guid column is text; keep it tidy


def test_is_generated_guid():
    assert ai_logic.is_generated_guid("iplocal-abc123")
    assert not ai_logic.is_generated_guid("Xy9#kQ")
    assert not ai_logic.is_generated_guid(None)


FIELD_MAP = {
    "Study Deck - Basic": ["Front", "Back", "Why", "Image", "Tag", "Dosing", "Notes"],
    "Study Deck - Cloze": ["Text", "Why", "Image", "Dosing", "Notes"],
    "Basic": ["Front", "Back"],
}
ALLOWED = list(FIELD_MAP)

GOOD = '''[
 {"note_type": "Study Deck - Basic",
  "fields": {"Front": "First sign category of LAST?", "Back": "CNS excitation",
             "Why": "CNS precedes CV collapse."},
  "tags": ["LAST"], "rationale": "core fact"},
 {"note_type": "Study Deck - Cloze",
  "fields": {"Text": "Lipid bolus {{c1::1.5 mL/kg}} of {{c2::20%}} emulsion",
             "Why": "ASRA checklist."},
  "images": [{"source": "svg:<svg xmlns='http://www.w3.org/2000/svg'></svg>",
              "alt": "diagram", "attribution": ""}]}
]'''


def test_parse_good_cards():
    cards, errors = ai_logic.parse_cards_json(GOOD, ALLOWED, FIELD_MAP)
    assert errors == []
    assert len(cards) == 2
    assert cards[0]["fields"]["Front"].startswith("First sign")
    assert cards[0]["tags"] == ["LAST"]
    assert cards[1]["images"][0]["source"].startswith("svg:")


def test_parse_strips_markdown_fence():
    fenced = "Here you go:\n```json\n" + GOOD + "\n```\nDone."
    cards, errors = ai_logic.parse_cards_json(fenced, ALLOWED, FIELD_MAP)
    assert errors == [] and len(cards) == 2


def test_parse_rejects_unknown_note_type_and_fields():
    bad = '[{"note_type": "Fancy", "fields": {"Front": "x"}}]'
    cards, errors = ai_logic.parse_cards_json(bad, ALLOWED, FIELD_MAP)
    assert cards == [] and any("Fancy" in e for e in errors)
    bad2 = '[{"note_type": "Basic", "fields": {"Front": "x", "Sideways": "y"}}]'
    cards, errors = ai_logic.parse_cards_json(bad2, ALLOWED, FIELD_MAP)
    assert cards == [] and any("Sideways" in e for e in errors)


def test_parse_rejects_non_list_and_garbage():
    for text in ("not json at all", '{"note_type": "Basic"}', "[]"):
        cards, errors = ai_logic.parse_cards_json(text, ALLOWED, FIELD_MAP)
        assert cards == [] and errors


def test_parse_rejects_empty_primary_field():
    bad = '[{"note_type": "Basic", "fields": {"Front": " ", "Back": "y"}}]'
    cards, errors = ai_logic.parse_cards_json(bad, ALLOWED, FIELD_MAP)
    assert cards == [] and any("Front" in e for e in errors)


def test_parse_rejects_bad_image_entry():
    bad = ('[{"note_type": "Basic", "fields": {"Front": "x", "Back": "y"},'
           '"images": [{"source": "ftp://nope"}]}]')
    cards, errors = ai_logic.parse_cards_json(bad, ALLOWED, FIELD_MAP)
    assert cards == [] and any("image" in e.lower() for e in errors)


def test_parse_rejects_tags_as_string():
    bad = '[{"note_type": "Basic", "fields": {"Front": "x", "Back": "y"}, "tags": "LAST"}]'
    cards, errors = ai_logic.parse_cards_json(bad, ALLOWED, FIELD_MAP)
    assert cards == [] and any("tags" in e.lower() for e in errors)
