"""Pure-logic tests for AI card generation. No Anki install needed."""
import os

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
    "Cloze": ["Text", "Back Extra"],
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


def test_primary_field_has_a_cloze_entry():
    # I6: a core "Cloze" note used to fall back to "Front", which it has no
    # such field, so its review row rendered empty.
    assert ai_logic.PRIMARY_FIELD["Cloze"] == "Text"


def test_check_cloze_syntax_applies_to_core_cloze_note_type_too():
    # Keyed on the primary field being "Text", not on the exact type name
    # "Study Deck - Cloze": a core "Cloze" note must get the same validation.
    no_deletion = _card("Cloze", Text="no deletions at all")
    valid = _card("Cloze", Text="{{c1::one}} deletion")
    checks = ai_logic.mechanical_checks([no_deletion, valid], {})
    assert any(c["code"] == "cloze" and c["level"] == "block" for c in checks[0])
    assert all(c["code"] != "cloze" for c in checks[1])


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


# === I2: a failed image resolution becomes a mechanical check, not a modal ===

def test_image_error_becomes_a_block_level_check():
    cards = [_card(Front="q", Back="a")]
    checks = ai_logic.mechanical_checks(cards, {}, {0: ["network is down"]})
    assert any(c["code"] == "image" and c["level"] == "block"
              and "network is down" in c["message"] for c in checks[0])


def test_image_error_only_applies_to_the_named_card():
    cards = [_card(Front="q1", Back="a"), _card(Front="q2", Back="a")]
    checks = ai_logic.mechanical_checks(cards, {}, {1: ["boom"]})
    assert all(c["code"] != "image" for c in checks[0])
    assert any(c["code"] == "image" for c in checks[1])


def test_no_image_errors_is_the_same_as_omitting_the_argument():
    cards = [_card(Front="q", Back="a", Why="w")]
    assert (ai_logic.mechanical_checks(cards, {})
           == ai_logic.mechanical_checks(cards, {}, {}))
    assert (ai_logic.mechanical_checks(cards, {})
           == ai_logic.mechanical_checks(cards, {}, None))


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


# === C1: the prompt itself must name the mode, since backends whose CLI gives
# us no flags (codex, agy) have no other way to enforce a workflow difference.
_PROMPT_KW = dict(skills=["S"], source="SRC", note_types=["Basic"],
                  field_map=FIELD_MAP, count=3)


def test_prompt_differs_between_modes_and_names_expected_workflow():
    thorough = ai_logic.build_prompt(**_PROMPT_KW, mode="thorough")
    quick = ai_logic.build_prompt(**_PROMPT_KW, mode="quick")
    assert thorough != quick
    assert "verify" in thorough.lower() and "self-review" in thorough.lower()
    assert "single pass" in quick.lower()
    # Quick explicitly tells the model not to verify: the one enforcement
    # mechanism available on a backend whose CLI gives us no tool-gating flag.
    assert "do not" in quick.lower() and "verifying facts online" in quick.lower()


def test_prompt_defaults_to_thorough_mode():
    p = ai_logic.build_prompt(**_PROMPT_KW)
    assert "Mode: Thorough" in p


def test_prompt_unknown_mode_falls_back_to_thorough_text():
    p = ai_logic.build_prompt(**_PROMPT_KW, mode="bogus")
    assert "Mode: Thorough" in p


def test_default_mode_thresholds():
    assert ai_logic.AUTO_DEPTH_CHARS == 1500
    assert ai_logic.default_mode(1499, 0) == "quick"
    assert ai_logic.default_mode(1500, 0) == "thorough"
    assert ai_logic.default_mode(10, 1) == "thorough"


def test_prompt_auto_count_states_ceiling_and_rule():
    p = ai_logic.build_prompt(**{**_PROMPT_KW, "count": None})
    assert "up to 40 cards" in p
    assert "one card per point the source actually teaches" in p
    assert "do not pad" in p.lower()
    p3 = ai_logic.build_prompt(**{**_PROMPT_KW, "count": 3})
    assert "exactly 3 cards" in p3


def test_auto_count_instruction_sits_after_the_stable_prefix():
    a = ai_logic.build_prompt(**{**_PROMPT_KW, "count": None})
    b = ai_logic.build_prompt(**{**_PROMPT_KW, "count": 7})
    cut = min(a.find("## Task"), b.find("## Task"))
    assert cut > 0 and a[:cut] == b[:cut]


def test_parse_accepts_count_wrapper():
    text = ('{"count": 1, "cards": [{"note_type": "Study Deck - Basic", '
            '"fields": {"Front": "q", "Back": "a", "Why": "w"}}]}')
    cards, errors = ai_logic.parse_cards_json(text, ALLOWED, FIELD_MAP)
    assert not errors and len(cards) == 1


import json as _json


def test_parse_claude_result_event():
    line = _json.dumps({"type": "result", "subtype": "success",
                        "result": "[{\"x\": 1}]",
                        "usage": {"input_tokens": 900, "output_tokens": 100}})
    evt = ai_logic.parse_stream_event("claude", line)
    assert evt == {"type": "result", "text": "[{\"x\": 1}]", "tokens": 1000}


def test_parse_claude_tool_use_maps_to_phase():
    line = _json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "WebSearch", "input": {}}]}})
    evt = ai_logic.parse_stream_event("claude", line)
    assert evt == {"type": "phase", "phase": "Verify online",
                   "activity": "Searched the web"}


def test_parse_claude_read_tool_use_names_the_basename():
    line = _json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Read",
         "input": {"file_path": "/private/tmp/x/notes.pdf"}}]}})
    evt = ai_logic.parse_stream_event("claude", line)
    assert evt == {"type": "phase", "phase": "Working",
                   "activity": "Read notes.pdf"}


def test_parse_claude_read_tool_use_ignores_other_parameters():
    # Only file_path may name a basename; nothing else the tool_use block
    # carries (a search query, a URL) should ever reach the activity text.
    line = _json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash",
         "input": {"command": "cat /secret/source.txt"}}]}})
    evt = ai_logic.parse_stream_event("claude", line)
    assert evt["activity"] == "Ran a command"
    assert "secret" not in evt["activity"]


def test_parse_claude_unknown_tool_falls_back_to_used_name():
    line = _json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "NotebookEdit", "input": {}}]}})
    evt = ai_logic.parse_stream_event("claude", line)
    assert evt["activity"] == "Used NotebookEdit"


def test_parse_claude_partial_message_delta():
    line = _json.dumps({"type": "stream_event", "event": {
        "type": "content_block_delta",
        "delta": {"type": "text_delta", "text": "some cards"}}})
    assert ai_logic.parse_stream_event("claude", line) == {
        "type": "delta", "text": "some cards"}


def test_parse_claude_stream_event_other_shapes_return_none():
    for line in (
        _json.dumps({"type": "stream_event", "event": {"type": "message_start"}}),
        _json.dumps({"type": "stream_event", "event": {
            "type": "content_block_delta",
            "delta": {"type": "input_json_delta", "partial_json": "{}"}}}),
        _json.dumps({"type": "stream_event"}),
    ):
        assert ai_logic.parse_stream_event("claude", line) is None


def test_parse_claude_error_result_event():
    # Real shape from a v2.1.251 claude with an expired login: subtype stays
    # "success" even though is_error is true, and the human message rides in
    # the same "result" field a successful run puts card text in.
    line = _json.dumps({"type": "result", "subtype": "success", "is_error": True,
                        "num_turns": 1,
                        "result": "Failed to authenticate: OAuth session "
                                  "expired and could not be refreshed",
                        "terminal_reason": "api_error"})
    evt = ai_logic.parse_stream_event("claude", line)
    assert evt == {"type": "error",
                   "text": "Failed to authenticate: OAuth session expired "
                           "and could not be refreshed"}


def test_parse_claude_error_result_never_looks_like_a_result_event():
    line = _json.dumps({"type": "result", "subtype": "success", "is_error": True,
                        "result": "some failure"})
    evt = ai_logic.parse_stream_event("claude", line)
    assert evt["type"] != "result"


def test_parse_codex_token_count_with_rate_limits():
    line = _json.dumps({"type": "token_count",
                        "info": {"total_tokens": 5000},
                        "rate_limits": {
                            "primary": {"used_percent": 12.5,
                                        "resets_at": "2026-08-26T20:00:00Z"},
                            "secondary": {"used_percent": 40.0}}})
    evt = ai_logic.parse_stream_event("codex", line)
    assert evt["type"] == "rate_limits"
    assert evt["primary_pct"] == 12.5 and evt["secondary_pct"] == 40.0


def test_parse_codex_result_top_level_text():
    line = _json.dumps({"type": "item.completed", "text": "[{\"x\": 1}]",
                        "usage": {"input_tokens": 900, "output_tokens": 100}})
    evt = ai_logic.parse_stream_event("codex", line)
    assert evt == {"type": "result", "text": "[{\"x\": 1}]", "tokens": 1000}


def test_parse_codex_result_text_nested_under_item():
    # The shape the codex branch was actually reading before I9 (text
    # nested under "item" rather than top-level) must also be picked up,
    # since which one a real codex binary emits is unverified here.
    line = _json.dumps({"type": "turn.completed",
                        "item": {"type": "agent_message", "text": "[{\"x\": 1}]"},
                        "usage": {"input_tokens": 900, "output_tokens": 100}})
    evt = ai_logic.parse_stream_event("codex", line)
    assert evt == {"type": "result", "text": "[{\"x\": 1}]", "tokens": 1000}


def test_parse_codex_result_neither_shape_present_is_none():
    line = _json.dumps({"type": "item.completed", "item": {"type": "reasoning"}})
    assert ai_logic.parse_stream_event("codex", line) is None


def test_parse_codex_item_started_names_activity_by_item_type():
    line = _json.dumps({"type": "item.started",
                        "item": {"id": "1", "type": "command_execution"}})
    assert ai_logic.parse_stream_event("codex", line) == {
        "type": "activity", "text": "Ran a command"}


def test_parse_codex_item_started_unknown_type_falls_back():
    line = _json.dumps({"type": "item.started", "item": {"id": "1", "type": "mystery"}})
    assert ai_logic.parse_stream_event("codex", line) == {
        "type": "activity", "text": "Used mystery"}


def test_parse_codex_item_started_without_item_type_is_none():
    for line in (_json.dumps({"type": "item.started"}),
                 _json.dumps({"type": "item.started", "item": {}}),
                 _json.dumps({"type": "item.started", "item": "not a dict"})):
        assert ai_logic.parse_stream_event("codex", line) is None


# The agy cases below are driven by tests/agy_stream_samples.ndjson: lines
# captured verbatim from a real agy 1.1.24 run (a trivial prompt, and the
# empty-prompt failure), so these pin the parser to the shape the binary
# actually emits rather than to an assumption about it.
_AGY_SAMPLES = os.path.join(os.path.dirname(__file__), "agy_stream_samples.ndjson")


def _agy_lines():
    with open(_AGY_SAMPLES, encoding="utf8") as fh:
        return [ln for ln in fh.read().splitlines() if ln.strip()]


def _agy_line(event, index=0):
    hits = [ln for ln in _agy_lines() if _json.loads(ln).get("event") == event]
    return hits[index]


def test_parse_agy_success_result_from_captured_sample():
    evt = ai_logic.parse_stream_event("agy", _agy_line("result", 0))
    assert evt == {"type": "result", "text": "ok\n", "tokens": 13815}


def test_parse_agy_error_result_from_captured_sample():
    # status ERROR carries its message in "error", never in "response", and
    # must never come back as a result the caller could treat as card text.
    evt = ai_logic.parse_stream_event("agy", _agy_line("result", 1))
    assert evt["type"] == "error"
    assert "empty prompt" in evt["text"]


def test_parse_agy_error_result_without_a_message_still_reads():
    line = _json.dumps({"event": "result", "result": {"status": "ERROR"}})
    assert ai_logic.parse_stream_event("agy", line) == {
        "type": "error", "text": "Antigravity reported an error"}


def test_parse_agy_init_and_step_updates_from_captured_samples():
    assert ai_logic.parse_stream_event("agy", _agy_line("init")) is None
    steps = [ai_logic.parse_stream_event("agy", ln) for ln in _agy_lines()
             if _json.loads(ln).get("event") == "step_update"]
    # The second step is the fixture's agent_response, which now also carries
    # its text_delta as its own event kind, so a failed run's assembly has
    # something to fall back to (see ai_cli._run_argv's agy_deltas
    # accumulation).
    assert steps == [{"type": "phase", "phase": "Working"},
                     {"type": "delta", "text": "ok\n"}]


def test_parse_agy_tool_step_names_a_web_phase():
    # Built from the documented step_update keys (the captured run used no
    # tool), with agy's own web tool names as listed in the init line above.
    web = _json.dumps({"event": "step_update", "step_update": {
        "step_type": "tool", "state": "ACTIVE", "tool_name": "search_web"}})
    other = _json.dumps({"event": "step_update", "step_update": {
        "step_type": "tool", "state": "ACTIVE", "tool_name": "view_file"}})
    assert ai_logic.parse_stream_event("agy", web) == {
        "type": "phase", "phase": "Verify online", "activity": "Searched the web"}
    assert ai_logic.parse_stream_event("agy", other) == {
        "type": "phase", "phase": "Working", "activity": "Viewed a file"}


def test_parse_agy_tool_active_names_basename_from_tool_info_parameters():
    # Verified live against agy 1.1.24: a view_file call's basename rides in
    # tool_info.parameters.AbsolutePath.
    line = _json.dumps({"event": "step_update", "step_update": {
        "step_type": "tool", "state": "ACTIVE", "tool_name": "view_file",
        "tool_info": {"name": "view_file",
                     "parameters": {"AbsolutePath": "/tmp/x/notes.txt"}}}})
    evt = ai_logic.parse_stream_event("agy", line)
    assert evt["activity"] == "Viewed notes.txt"


def test_parse_agy_tool_done_state_carries_no_activity():
    # Only ACTIVE names the step, so a tool that fires ACTIVE then DONE is
    # named once, not twice.
    line = _json.dumps({"event": "step_update", "step_update": {
        "step_type": "tool", "state": "DONE", "tool_name": "view_file"}})
    evt = ai_logic.parse_stream_event("agy", line)
    assert "activity" not in evt


def test_parse_agy_malformed_shapes_return_none_rather_than_raising():
    for line in ('{"event": "result"}',
                 '{"event": "result", "result": "a string"}',
                 '{"event": "step_update", "step_update": 7}',
                 '{"event": "something_new"}',
                 '{"type": "result", "result": "old shape"}'):
        assert ai_logic.parse_stream_event("agy", line) is None


def test_parse_garbage_line_is_none():
    assert ai_logic.parse_stream_event("claude", "not json") is None
    assert ai_logic.parse_stream_event("agy", "{}") is None


def test_usage_rolling_window_and_line():
    reg = {}
    reg = ai_logic.record_usage(reg, "claude", 18000, now=1000000)
    reg = ai_logic.record_usage(reg, "claude", 2000, now=1000600)
    old = 1000000 - 8 * 86400
    reg = ai_logic.record_usage(reg, "claude", 99999, now=old)
    line = ai_logic.usage_line(reg, "claude", now=1000700)
    assert "2 runs" in line and "20" in line          # 20k tokens, 2 runs
    assert "99" not in line                            # >7 days pruned
    assert "this add-on" in line


def test_usage_line_free_tier_counts_runs():
    reg = ai_logic.record_usage({}, "agy", 3000, now=500)
    assert "runs today" in ai_logic.usage_line(reg, "agy", now=600,
                                               free_tier=True)


def test_usage_state_corrupt_file_degrades_to_no_history():
    # A hand-edited ai_usage.json, mirroring test_duration_state_corrupt_
    # file_degrades_to_no_history: a bad "claude" value, and rows missing or
    # mistyping "ts"/"tokens", must not raise: usage_line runs from dialog
    # construction, so a crash here would stop the wizard from opening at all.
    assert "0 runs" in ai_logic.usage_line({"claude": "not a list"}, "claude", now=1000)
    assert "0 runs" in ai_logic.usage_line(
        {"claude": [None, "bad", {}, {"tokens": 5}, {"ts": "nope", "tokens": 5}]},
        "claude", now=1000)
    # recording on top of a corrupt block never raises, and yields a clean
    # single-entry history rather than propagating the garbage
    reg = ai_logic.record_usage({"claude": "not a list"}, "claude", 10, now=1000)
    assert reg["claude"] == [{"ts": 1000, "tokens": 10}]


def test_record_duration_per_backend_and_mode():
    reg = {}
    reg = ai_logic.record_duration(reg, "claude", "thorough", 100)
    reg = ai_logic.record_duration(reg, "claude", "quick", 20)
    reg = ai_logic.record_duration(reg, "codex", "thorough", 300)
    assert reg["durations"]["claude-thorough"] == [100]
    assert reg["durations"]["claude-quick"] == [20]
    assert reg["durations"]["codex-thorough"] == [300]


def test_record_duration_prunes_to_last_ten():
    reg = {}
    for s in range(1, 13):   # 12 runs recorded, only last 10 kept
        reg = ai_logic.record_duration(reg, "claude", "thorough", s)
    assert reg["durations"]["claude-thorough"] == list(range(3, 13))


def test_median_duration_is_median_not_mean():
    reg = {}
    for s in (10, 10, 10, 10, 100):   # mean=28, median=10
        reg = ai_logic.record_duration(reg, "claude", "thorough", s)
    assert ai_logic.median_duration(reg, "claude", "thorough") == 10


def test_duration_estimate_line_no_history_returns_none():
    assert ai_logic.duration_estimate_line({}, "claude", "thorough") is None
    reg = ai_logic.record_duration({}, "claude", "quick", 20)
    # history exists for a different mode, not the one being asked about
    assert ai_logic.duration_estimate_line(reg, "claude", "thorough") is None


def test_duration_estimate_line_with_history():
    reg = {}
    for s in (95, 100, 105):   # median 100s -> "1m 40s"
        reg = ai_logic.record_duration(reg, "claude", "thorough", s)
    line = ai_logic.duration_estimate_line(reg, "claude", "thorough")
    assert line == "your recent Thorough runs averaged 1m 40s"


def test_duration_estimate_line_quick_mode_label():
    reg = ai_logic.record_duration({}, "claude", "quick", 22)
    line = ai_logic.duration_estimate_line(reg, "claude", "quick")
    assert line == "your recent Quick runs averaged 22s"


def test_format_duration_sub_minute_and_multi_minute():
    assert ai_logic.format_duration(48) == "48s"
    assert ai_logic.format_duration(100) == "1m 40s"
    assert ai_logic.format_duration(120) == "2m"


def test_duration_state_corrupt_file_degrades_to_no_history():
    # a hand-edited durations block: wrong type, wrong element types
    assert ai_logic.duration_estimate_line(
        {"durations": "not a dict"}, "claude", "thorough") is None
    assert ai_logic.duration_estimate_line(
        {"durations": {"claude-thorough": "not a list"}},
        "claude", "thorough") is None
    assert ai_logic.duration_estimate_line(
        {"durations": {"claude-thorough": [None, "bad", {}]}},
        "claude", "thorough") is None
    # recording on top of a corrupt block never raises, and yields a clean
    # single-entry history rather than propagating the garbage
    reg = ai_logic.record_duration(
        {"durations": "not a dict"}, "claude", "thorough", 50)
    assert reg["durations"]["claude-thorough"] == [50]


def test_rate_limit_line():
    s = ai_logic.rate_limit_line({"type": "rate_limits", "primary_pct": 12.5,
                                  "secondary_pct": 40.0,
                                  "resets": "2026-08-26T20:00:00Z"})
    assert "87" in s and "60" in s   # percent LEFT, not used


def test_parse_malformed_nested_shapes_never_raises():
    # Syntactically valid JSON objects with wrong-typed nested fields: each
    # must degrade to None, never raise, since this parses raw subprocess
    # output from three different vendors' CLIs.
    cases = [
        ("claude", _json.dumps({"type": "assistant",
                                "message": {"content": ["not a dict"]}})),
        ("claude", _json.dumps({"type": "result", "usage": [1, 2, 3]})),
        ("codex", _json.dumps({"type": "token_count",
                               "rate_limits": [1, 2]})),
        ("codex", _json.dumps({"type": "token_count",
                               "rate_limits": {"primary": "notadict"}})),
        ("codex", _json.dumps({"type": "item.completed", "item": "not a dict"})),
        ("codex", _json.dumps({"type": "item.completed",
                               "item": {"text": ["not", "a", "string"]}})),
    ]
    for kind, line in cases:
        assert ai_logic.parse_stream_event(kind, line) is None


def test_parse_claude_result_non_bool_is_error_never_raises():
    # "is_error" mistyped by a vendor CLI (a string, not a bool) must not
    # raise; it's treated as not-an-error rather than silently dropping what
    # might be real card text.
    line = _json.dumps({"type": "result", "is_error": "yes", "result": "x"})
    evt = ai_logic.parse_stream_event("claude", line)
    assert evt["type"] == "result" and evt["text"] == "x"


def test_parse_tool_use_with_non_string_name_never_raises():
    # A tool_use block with a non-string name field is structurally valid JSON
    # but has an unusable name. Should return a phase event (treating it like
    # an unrecognized tool name), never raise.
    cases = [
        ("claude", _json.dumps({"type": "assistant",
                                "message": {"content": [{"type": "tool_use",
                                                        "name": ["nested", "list"]}]}})),
        ("claude", _json.dumps({"type": "assistant",
                                "message": {"content": [{"type": "tool_use",
                                                        "name": {"a": 1}}]}})),
    ]
    for kind, line in cases:
        result = ai_logic.parse_stream_event(kind, line)
        assert result == {"type": "phase", "phase": "Working"}


def test_bundled_skill_loads_and_reads_like_a_skill():
    text = ai_logic.load_bundled_skill()
    assert "internpearls-authoring" in text
    assert "Never generate a raster image" in text


def test_active_skills_ordering_and_disable():
    deck = {"text": "DECK SKILL", "enabled": True}
    both = ai_logic.active_skills(deck)
    assert len(both) == 2 and both[1] == "DECK SKILL"
    assert both[0].startswith("---")   # bundled first
    assert len(ai_logic.active_skills({"text": "x", "enabled": False})) == 1
    assert len(ai_logic.active_skills(None)) == 1


def test_active_skills_appends_user_rules_last():
    deck = {"enabled": True, "text": "deck text"}
    out = ai_logic.active_skills(deck, "  my rules  ")
    assert out[0] == ai_logic.load_bundled_skill()
    assert out[1] == "deck text"
    assert out[2].endswith("my rules")
    assert ai_logic.active_skills(None, "") == [ai_logic.load_bundled_skill()]
    assert ai_logic.USER_SKILL_MAX_CHARS == 20000


def test_active_skills_user_block_carries_the_ranking_heading():
    """The heading that states how the learner's own rules rank appears only
    when there is a user skill to rank, comes before the rules themselves, and
    leaves the bundled and deck texts untouched (the stable cache prefix)."""
    deck = {"enabled": True, "text": "deck text"}
    with_user = ai_logic.active_skills(deck, "no mnemonics")
    without_user = ai_logic.active_skills(deck)
    assert with_user[:2] == without_user
    assert "The learner's own rules" not in with_user[0]
    assert "The learner's own rules" not in with_user[1]
    heading, _, rules = with_user[2].partition("\n\n")
    assert "The learner's own rules" in heading
    assert "the learner's rules win" in heading
    assert rules == "no mnemonics"


# A hand-written minimal PDF byte string (no real xref table) was tried first
# and rejected by pypdf's parser ("startxref not found"). tests/fixtures/sample.pdf
# was generated once with the vendored pypdf's own PdfWriter (see git history)
# and committed instead, so this test exercises real text extraction.
FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def test_extract_pdf_text(tmp_path):
    out = ai_logic.extract_attachment(os.path.join(FIXTURES, "sample.pdf"), str(tmp_path))
    assert "Hello LAST" in out["text"]
    assert out["images"] == []
    assert out["images_undecoded"] is False   # sample.pdf really has no images: not a decode failure


def test_extract_image_copies_file(tmp_path):
    # the returned name is no longer guaranteed to equal the original basename
    # (see the collision-safety tests below) - just the sanitized stem plus a
    # content-hash suffix and the original extension.
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"0" * 20
    png = tmp_path / "slide3.png"
    png.write_bytes(png_bytes)
    dest = tmp_path / "scratch"
    dest.mkdir()
    out = ai_logic.extract_attachment(str(png), str(dest))
    assert out["text"] == ""
    assert len(out["images"]) == 1
    name = out["images"][0]
    assert name.startswith("slide3-") and name.endswith(".png")
    assert (dest / name).read_bytes() == png_bytes


def test_extract_image_same_basename_different_content_both_survive(tmp_path):
    # two attachments sharing a filename (two slide exports both named
    # "figure1.png") must not silently overwrite each other in dest_dir.
    src_a = tmp_path / "a"
    src_b = tmp_path / "b"
    src_a.mkdir()
    src_b.mkdir()
    (src_a / "figure1.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"A" * 20)
    (src_b / "figure1.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"B" * 20)
    dest = tmp_path / "scratch"
    dest.mkdir()
    out_a = ai_logic.extract_attachment(str(src_a / "figure1.png"), str(dest))
    out_b = ai_logic.extract_attachment(str(src_b / "figure1.png"), str(dest))
    name_a, name_b = out_a["images"][0], out_b["images"][0]
    assert name_a != name_b
    assert (dest / name_a).exists() and (dest / name_b).exists()
    assert (dest / name_a).read_bytes() != (dest / name_b).read_bytes()


def test_extract_image_stem_sanitized_against_path_traversal(tmp_path):
    hostile = tmp_path / "..-evil.png"
    hostile.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 20)
    dest = tmp_path / "scratch"
    dest.mkdir()
    out = ai_logic.extract_attachment(str(hostile), str(dest))
    name = out["images"][0]
    assert os.sep not in name and ".." not in name


def test_extract_unknown_extension_raises(tmp_path):
    p = tmp_path / "notes.docx"
    p.write_bytes(b"x")
    import pytest
    with pytest.raises(ValueError):
        ai_logic.extract_attachment(str(p), str(tmp_path))


# These three exercise the filename sanitizing/collision-proofing logic in
# extract_attachment, which is pure string handling over an image's (id, name)
# and doesn't need a real decoded image behind it: so, like
# test_extract_pdf_image_extension_sanitized_against_hostile_name below, they
# fake out page.images with a stand-in ImageFile rather than decoding
# tests/fixtures/with_image.pdf's real embedded image. That keeps them honest
# in both environments: pypdf needs Pillow to actually decode an image, and
# Anki's own bundled Python doesn't carry it (see
# test_extract_pdf_real_image_decodes_when_pillow_is_available, and
# test_extract_pdf_images_undecoded_flag_when_pillow_unavailable for the
# degraded path these three used to fail on silently).
def _fake_page_image(name, data=b"stand-in-image-bytes"):
    pypdf = ai_logic._pypdf()
    img = pypdf._page.ImageFile()
    img.name = name
    img.data = data
    return img


def test_extract_pdf_images_use_safe_collision_proof_names(tmp_path, monkeypatch):
    pypdf = ai_logic._pypdf()
    monkeypatch.setattr(pypdf._page.PageObject, "images",
                        property(lambda self: [_fake_page_image("img0.png")]))
    out = ai_logic.extract_attachment(os.path.join(FIXTURES, "with_image.pdf"), str(tmp_path))
    assert out["images"] == ["with_image-p1-img0.png"]
    assert (tmp_path / "with_image-p1-img0.png").read_bytes() == b"stand-in-image-bytes"
    assert out["images_undecoded"] is False


def test_extract_pdf_image_names_dont_collide_across_pdfs(tmp_path, monkeypatch):
    # Two different source PDFs extracting page 1 image 0 must not overwrite
    # each other's output: the original stem is part of every image filename.
    pypdf = ai_logic._pypdf()
    monkeypatch.setattr(pypdf._page.PageObject, "images",
                        property(lambda self: [_fake_page_image("img0.png")]))
    src_bytes = open(os.path.join(FIXTURES, "with_image.pdf"), "rb").read()
    a, b = tmp_path / "with_image.pdf", tmp_path / "renamed.pdf"
    a.write_bytes(src_bytes)
    b.write_bytes(src_bytes)
    out_a = ai_logic.extract_attachment(str(a), str(tmp_path))
    out_b = ai_logic.extract_attachment(str(b), str(tmp_path))
    assert out_a["images"] == ["with_image-p1-img0.png"]
    assert out_b["images"] == ["renamed-p1-img0.png"]
    assert (tmp_path / "with_image-p1-img0.png").exists()
    assert (tmp_path / "renamed-p1-img0.png").exists()


def test_extract_pdf_stem_sanitized_against_path_traversal(tmp_path, monkeypatch):
    # A hostile-looking basename (already stripped of any directory component
    # by os.path.basename, but still containing traversal-shaped characters)
    # must not leak "/" or ".." into the generated image filename.
    pypdf = ai_logic._pypdf()
    monkeypatch.setattr(pypdf._page.PageObject, "images",
                        property(lambda self: [_fake_page_image("img0.png")]))
    hostile = tmp_path / "..-evil-p1-img0.pdf"
    hostile.write_bytes(open(os.path.join(FIXTURES, "with_image.pdf"), "rb").read())
    out = ai_logic.extract_attachment(str(hostile), str(tmp_path))
    assert out["images"] == ["_-evil-p1-img0-p1-img0.png"]
    assert os.sep not in out["images"][0] and ".." not in out["images"][0]


def test_extract_pdf_real_image_decodes_when_pillow_is_available(tmp_path):
    # The full end-to-end path, actually decoded, when Pillow happens to be
    # importable in this interpreter (a developer's system python typically
    # has it; Anki's bundled python typically doesn't: this is exactly the
    # gap that let PDF image extraction ship silently broken for every real
    # user, per the vendored-pypdf docstring pypdf raises against). Skipped
    # rather than failed where Pillow is genuinely absent, since that's the
    # one thing this specific test can't fake its way past.
    import pytest
    pytest.importorskip("PIL")
    out = ai_logic.extract_attachment(os.path.join(FIXTURES, "with_image.pdf"), str(tmp_path))
    assert out["images"] == ["with_image-p1-img0.png"]
    assert (tmp_path / "with_image-p1-img0.png").exists()
    assert out["images_undecoded"] is False


def test_extract_pdf_images_undecoded_flag_when_pillow_unavailable(tmp_path, monkeypatch):
    # Simulates pypdf's real "Pillow not importable" failure (an ImportError
    # out of page.images's decode step) regardless of whether this interpreter
    # actually has Pillow, so the degraded-but-honest path is verified here
    # rather than only in whatever environment happens to run the suite. Text
    # extraction needs no Pillow and must still come through; the caller must
    # be able to tell this apart from "this PDF had no images" (images == []
    # with images_undecoded == False, see test_extract_pdf_text).
    class _UndecodableImages:
        def __len__(self):
            return 1

        def __getitem__(self, index):
            raise ImportError("pillow is required to do image extraction")

    pypdf = ai_logic._pypdf()
    monkeypatch.setattr(pypdf._page.PageObject, "images",
                        property(lambda self: _UndecodableImages()))
    # two_page.pdf (not with_image.pdf, which has no extractable text of its
    # own) so the "text still comes through" half of this has something real
    # to check against.
    out = ai_logic.extract_attachment(os.path.join(FIXTURES, "two_page.pdf"), str(tmp_path))
    assert out["images"] == []
    assert out["images_undecoded"] is True
    assert "First page text" in out["text"]   # text extraction is unaffected by the missing dependency


def test_extract_pdf_image_extension_sanitized_against_hostile_name(tmp_path, monkeypatch):
    # pypdf's own ImageFile.name docstring warns it "can contain arbitrary
    # characters" (read from the PDF's internal resource naming) and must be
    # sanitized before use as a filename. Simulate a hostile value for it.
    pypdf = ai_logic._pypdf()
    fake_img = pypdf._page.ImageFile()
    fake_img.name = "../../etc/passwd"
    fake_img.data = b"not-really-an-image"
    monkeypatch.setattr(pypdf._page.PageObject, "images", property(lambda self: [fake_img]))

    out = ai_logic.extract_attachment(os.path.join(FIXTURES, "sample.pdf"), str(tmp_path))
    assert out["images"] == ["sample-p1-img0.png"]
    assert (tmp_path / "sample-p1-img0.png").read_bytes() == b"not-really-an-image"


def test_extract_pdf_bad_page_does_not_lose_other_pages_text(tmp_path, monkeypatch):
    # tests/fixtures/two_page.pdf has "First page text" / "Second page text" on
    # its two pages, generated once with the vendored pypdf's own writer.
    pypdf = ai_logic._pypdf()
    real_extract_text = pypdf._page.PageObject.extract_text

    def flaky_extract_text(self, *args, **kwargs):
        text = real_extract_text(self, *args, **kwargs)
        if "First" in text:
            raise RuntimeError("simulated page decode failure")
        return text

    monkeypatch.setattr(pypdf._page.PageObject, "extract_text", flaky_extract_text)
    out = ai_logic.extract_attachment(os.path.join(FIXTURES, "two_page.pdf"), str(tmp_path))
    assert "Second page text" in out["text"]
    assert "First page text" not in out["text"]


def test_extract_corrupt_pdf_raises_valueerror_naming_file(tmp_path):
    import pytest
    bad = tmp_path / "corrupt.pdf"
    bad.write_bytes(b"this is not a pdf at all, just some bytes")
    with pytest.raises(ValueError) as exc:
        ai_logic.extract_attachment(str(bad), str(tmp_path))
    assert "corrupt.pdf" in str(exc.value)


def test_extract_pdf_broken_pypdf_import_raises_valueerror(tmp_path, monkeypatch):
    # a missing/broken vendor dir must surface as this function's own ValueError
    # contract, never a raw ModuleNotFoundError from _pypdf().
    import pytest

    def broken_pypdf():
        raise ModuleNotFoundError("No module named 'pypdf'")

    monkeypatch.setattr(ai_logic, "_pypdf", broken_pypdf)
    with pytest.raises(ValueError) as exc:
        ai_logic.extract_attachment(os.path.join(FIXTURES, "sample.pdf"), str(tmp_path))
    assert "sample.pdf" in str(exc.value)
    assert "pypdf" in str(exc.value)


# --- svg_to_media: model-drawn SVG as a card image ---


def test_svg_to_media_and_script_rejection():
    import pytest
    name, data = ai_logic.svg_to_media("<svg xmlns='x'><rect/></svg>", 2)
    assert name == "generated-2.svg" and data.startswith(b"<svg")
    with pytest.raises(ValueError):
        ai_logic.svg_to_media("<svg><script>alert(1)</script></svg>", 0)
    with pytest.raises(ValueError):
        ai_logic.svg_to_media("<div>not svg</div>", 0)


def test_svg_to_media_script_check_is_case_insensitive():
    import pytest
    with pytest.raises(ValueError):
        ai_logic.svg_to_media("<svg><SCRIPT>alert(1)</SCRIPT></svg>", 0)


def test_svg_to_media_rejects_non_integer_index():
    """The filename is built from `index` verbatim; coercing to int keeps a stray
    string from ever landing a path separator in a media filename."""
    import pytest
    with pytest.raises(ValueError):
        ai_logic.svg_to_media("<svg></svg>", "../evil")


def test_svg_to_media_rejects_event_handler_attribute():
    import pytest
    with pytest.raises(ValueError):
        ai_logic.svg_to_media('<svg onload="alert(1)"><rect/></svg>', 0)


def test_svg_to_media_rejects_event_handler_with_space_before_equals():
    import pytest
    with pytest.raises(ValueError):
        ai_logic.svg_to_media('<svg onload ="alert(1)"><rect/></svg>', 0)


def test_svg_to_media_rejects_event_handler_mixed_case():
    import pytest
    with pytest.raises(ValueError):
        ai_logic.svg_to_media('<svg OnLoad="alert(1)"><rect/></svg>', 0)


def test_svg_to_media_rejects_javascript_uri():
    import pytest
    with pytest.raises(ValueError):
        ai_logic.svg_to_media(
            '<svg><a xlink:href="javascript:alert(1)"><text>x</text></a></svg>', 0)


def test_svg_to_media_accepts_a_real_diagram():
    """The rejection checks must not catch the shapes, text, groups, and styling a
    model-drawn diagram is actually made of."""
    markup = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        '<path d="M10 10 L90 90" stroke="black" fill="none"/>'
        '<rect x="5" y="5" width="20" height="20" fill="#3366cc" stroke="black" '
        'stroke-width="2"/>'
        '<text x="10" y="50" font-size="12">Femoral nerve</text>'
        '<g transform="translate(10,10)">'
        '<circle cx="5" cy="5" r="3" style="fill:red;stroke:none"/>'
        '</g>'
        '</svg>'
    )
    name, data = ai_logic.svg_to_media(markup, 1)
    assert name == "generated-1.svg"
    assert data == markup.encode("utf8")


# === Stale scratch sweep. The wizard removes its own mkdtemp dir when it
# closes, so everything here is about what a crash leaves behind.
def _scratch(tmp_path, name, age_s, size=0, now=1_000_000.0):
    d = tmp_path / name
    d.mkdir()
    if size:
        (d / "blob.bin").write_bytes(b"x" * size)
    os.utime(d, (now - age_s, now - age_s))
    return d


def test_sweep_removes_only_old_prefixed_dirs(tmp_path):
    now = 1_000_000.0
    old = _scratch(tmp_path, "ip-aigen-old", 90000, now=now)
    fresh = _scratch(tmp_path, "ip-aigen-fresh", 10, now=now)
    other = _scratch(tmp_path, "someone-elses-old", 90000, now=now)
    loose = tmp_path / "ip-aigen-not-a-dir.txt"
    loose.write_text("x")
    removed = ai_logic.sweep_stale_scratch(str(tmp_path), now=now)
    assert removed == [str(old)]
    assert not old.exists()
    assert fresh.exists() and other.exists() and loose.exists()


def test_sweep_trims_oldest_first_when_survivors_exceed_the_byte_cap(tmp_path):
    now = 1_000_000.0
    oldest = _scratch(tmp_path, "ip-aigen-a", 300, size=6000, now=now)
    middle = _scratch(tmp_path, "ip-aigen-b", 200, size=6000, now=now)
    newest = _scratch(tmp_path, "ip-aigen-c", 100, size=6000, now=now)
    removed = ai_logic.sweep_stale_scratch(
        str(tmp_path), max_total_bytes=13000, now=now)
    assert removed == [str(oldest)]
    assert middle.exists() and newest.exists()


def test_sweep_under_the_cap_removes_nothing(tmp_path):
    now = 1_000_000.0
    keep = _scratch(tmp_path, "ip-aigen-a", 100, size=500, now=now)
    assert ai_logic.sweep_stale_scratch(str(tmp_path), now=now) == []
    assert keep.exists()


def test_sweep_never_raises_on_a_missing_dir_or_an_unremovable_entry(tmp_path,
                                                                     monkeypatch):
    assert ai_logic.sweep_stale_scratch(str(tmp_path / "nope")) == []
    now = 1_000_000.0
    _scratch(tmp_path, "ip-aigen-old", 90000, now=now)

    def _boom(path):
        raise OSError("permission denied")

    monkeypatch.setattr(ai_logic.shutil, "rmtree", _boom)
    assert ai_logic.sweep_stale_scratch(str(tmp_path), now=now) == []


def test_sweep_counts_an_unremovable_stale_dir_toward_the_byte_cap(tmp_path,
                                                                    monkeypatch):
    # A stale directory whose deletion fails used to be dropped from
    # `survivors` outright, so its bytes escaped the total and the cap
    # sweep below never saw them. It must still count as a survivor: here
    # that's what forces the fresh (not-stale) dir to be evicted instead,
    # since the total only exceeds the cap once the stuck dir's bytes count.
    now = 1_000_000.0
    stuck = _scratch(tmp_path, "ip-aigen-stuck", 90000, size=9000, now=now)
    fresh = _scratch(tmp_path, "ip-aigen-fresh", 10, size=9000, now=now)
    real_rmtree = ai_logic.shutil.rmtree

    def _flaky(path):
        if path == str(stuck):
            raise OSError("permission denied")
        return real_rmtree(path)

    monkeypatch.setattr(ai_logic.shutil, "rmtree", _flaky)
    removed = ai_logic.sweep_stale_scratch(str(tmp_path), max_total_bytes=10000,
                                           now=now)
    assert removed == [str(fresh)]
    assert stuck.exists()   # never removed, but its bytes still counted
    assert not fresh.exists()


def test_sweep_computes_dir_bytes_once_per_survivor(tmp_path, monkeypatch):
    now = 1_000_000.0
    a = _scratch(tmp_path, "ip-aigen-a", 300, size=6000, now=now)
    b = _scratch(tmp_path, "ip-aigen-b", 100, size=6000, now=now)
    calls = []
    real_dir_bytes = ai_logic._dir_bytes

    def spy(path):
        calls.append(path)
        return real_dir_bytes(path)

    monkeypatch.setattr(ai_logic, "_dir_bytes", spy)
    ai_logic.sweep_stale_scratch(str(tmp_path), max_total_bytes=5000, now=now)
    assert sorted(calls) == sorted([str(a), str(b)])
    assert len(calls) == 2   # once per survivor, not recomputed in the cap loop


def test_codex_token_count_carries_usage_alongside_rate_limits():
    # Verified against a live short run: codex reports rate limits and token
    # usage on the same token_count event ("info" beside "rate_limits"), and
    # a short run's terminal item.completed carries no usage at all. Before
    # this fix, the rate_limits branch returned early and info's usage was
    # dropped on the floor, so a run like this reported 0 tokens.
    line = _json.dumps({"type": "token_count",
                        "rate_limits": {"primary": {"used_percent": 10.0,
                                                    "resets_at": "2026-09-02"}},
                        "info": {"total_tokens": 15, "input_tokens": 10,
                                "output_tokens": 5}})
    ev = ai_logic.parse_stream_event("codex", line)
    assert ev and ev["type"] == "rate_limits"
    assert ev.get("tokens") == 15


def test_config_count_ceiling_matches_ai_logic():
    # config.AI_COUNT_CEILING is a deliberate duplicate of this constant (see
    # config.py's own comment on why it isn't imported instead); catch the two
    # drifting apart.
    from internpearls import config
    assert config.AI_COUNT_CEILING == ai_logic.AUTO_COUNT_CEILING
