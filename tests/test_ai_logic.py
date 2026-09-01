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


def _card(ntype="Study Deck - Basic", **fields):
    base = {k: "" for k in FIELD_MAP[ntype]}
    base.update(fields)
    return {"note_type": ntype, "fields": base, "tags": [], "images": [],
            "rationale": ""}


def test_check_duplicate_against_collection():
    cards = [_card(Front="What is LAST?", Back="x")]
    checks = ai_logic.mechanical_checks(cards, {"what is last?": "What is LAST?"})
    assert any(c["code"] == "duplicate" and c["level"] == "block"
               for c in checks[0])


def test_check_cloze_syntax():
    ok = _card("Study Deck - Cloze", Text="{{c1::1.5 mL/kg}} bolus")
    bad = _card("Study Deck - Cloze", Text="{{c1:broken}} and {{c2::fine}")
    none = _card("Study Deck - Cloze", Text="no deletions at all")
    checks = ai_logic.mechanical_checks([ok, bad, none], {})
    assert all(c["code"] != "cloze" for c in checks[0])
    assert any(c["code"] == "cloze" and c["level"] == "block" for c in checks[1])
    assert any(c["code"] == "cloze" for c in checks[2])


def test_check_cloze_syntax_ignores_literal_braces_in_prose():
    # one real deletion, plus prose that just talks about cloze syntax
    card = _card("Study Deck - Cloze",
                  Text="{{c1::normal answer}} but the syntax uses {{ and }} in Anki")
    checks = ai_logic.mechanical_checks([card], {})
    assert all(c["code"] != "cloze" for c in checks[0])


def test_check_long_answer_warns():
    long_back = " ".join(["word"] * 120)
    checks = ai_logic.mechanical_checks([_card(Front="q", Back=long_back)], {})
    assert any(c["code"] == "long-answer" and c["level"] == "warn"
               for c in checks[0])


def test_check_long_answer_warns_on_short_back_long_why():
    # house style: short Back, long Why. Neither field alone trips the
    # threshold, but what the learner reads on the back does.
    long_why = " ".join(["word"] * 120)
    checks = ai_logic.mechanical_checks(
        [_card(Front="q", Back="short", Why=long_why)], {})
    assert any(c["code"] == "long-answer" and c["level"] == "warn"
               for c in checks[0])


def test_clean_card_gets_ok():
    checks = ai_logic.mechanical_checks([_card(Front="q", Back="a", Why="w")], {})
    assert checks[0] == [{"code": "ok", "level": "ok", "message": "checks pass"}]


def test_prompt_stable_prefix_across_revision():
    kw = dict(skills=["SKILL A"], source="SRC", note_types=["Basic"],
              field_map=FIELD_MAP, count=5)
    first = ai_logic.build_prompt(**kw)
    second = ai_logic.build_prompt(**kw, cards=[_card(Front="q", Back="a")],
                                   feedback="shorter", notes={0: "trim"})
    # the ENTIRE generation-turn prompt (including its trailing newline, which
    # the revision turn simply continues past with a blank line) must be a
    # literal prefix of the revision prompt, or vendor prompt caching can't hit.
    assert second.startswith(first)
    # the changing material must sit after everything shared
    assert second.index("SRC") < second.index("shorter")


def test_prompt_carries_contract_and_keep_verbatim():
    p = ai_logic.build_prompt(
        skills=["S"], source="SRC", note_types=["Basic"], field_map=FIELD_MAP,
        count=3, cards=[_card(Front="keepme", Back="a"),
                        _card(Front="fixme", Back="b")],
        feedback="overall shorter", notes={1: "split this"})
    assert "JSON" in p and "note_type" in p
    assert "keep verbatim" in p.lower()
    assert p.index("keepme") < p.index("split this")


def test_prompt_lists_attachments_and_fields():
    p = ai_logic.build_prompt(skills=["S"], source="x", note_types=["Basic"],
                              field_map=FIELD_MAP, count=2,
                              attachments=["slide3.png"])
    assert "slide3.png" in p and '"Front"' in p
