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
