#!/usr/bin/env python3
# tests/fake_cli.py: pretend agent CLI for ai_cli tests. Behavior via argv[1]:
#   ok              read stdin, emit one phase line then a claude-style result line
#   slow            sleep 30s before answering (for timeout/cancel tests)
#   event_then_slow emit one phase line, then sleep 30s (for a mid-run-exception test:
#                   the caller's on_event fires while this process is still alive)
#   garbage         emit non-JSON noise then exit 0
#   fail            exit 2 with stderr
#   not_signed_in   exit 1 with an auth-failure stderr message (test_connection)
#   error_result    claude-style result line with subtype "success" but
#                   is_error true and a human message in "result", empty
#                   stderr, then exit 1: the real shape a v2.1.251 claude
#                   with an expired login actually emits
#   badjson         emit a result line whose "result" text is not valid card JSON
#   two_cards       emit two fixed cards, identically on every invocation (for
#                   revision tests that need the "same shape" case with >1 card)
#   codex_top       codex-style item.completed with text at the top level
#   codex_nested    codex-style item.completed with text nested under "item"
#                   (the shape parse_stream_event's codex branch was actually
#                   reading before I9; kept alongside codex_top since neither
#                   shape is confirmed against a live codex binary)
#   agy_ok          antigravity-style "event"-keyed step_update then a
#                   SUCCESS result, the shape agy 1.1.24 actually emits
#   with_image      one card carrying a url: image (I2 review-gate tests)
import json
import os
import sys
import time

mode = sys.argv[1] if len(sys.argv) > 1 else "ok"

if "--help" in sys.argv:
    # supports_flag's own probe call: it runs `[path, "--help"]` (and
    # `[path, <subcommand>, "--help"]`) via subprocess.run without
    # redirecting stdin, so reading it here (as the real modes below do)
    # risked blocking on whatever the test process's own stdin is. Answer
    # without touching stdin and report every flag build_argv might gate on.
    print("--sandbox  --model  --effort  --disable-slash-commands  -p, --print")
    sys.exit(0)

prompt = sys.stdin.read()
# "Study Deck - Basic" (not the bare "Basic" note type) so a flow test can carry
# this card all the way through import against the mock collection's default model.
CARDS = [{"note_type": "Study Deck - Basic",
         "fields": {"Front": "q", "Back": "a"},
         "tags": [], "images": [], "rationale": "r"}]
CARDS_JSON = json.dumps(CARDS)
USAGE = {"input_tokens": 10, "output_tokens": 5}

# When set, dump the exact argv this process received and what (if anything)
# it read from stdin to that path, as JSON, then answer as an agy-shaped
# success so a caller driving the real build_argv (not a stubbed one) still
# gets a usable result. Lets a test inspect the real argv/stdin a backend's
# own build_argv constructs, rather than the pre-picked mode this script
# otherwise dispatches on.
# Recorder mode answers with an agy-shaped SUCCESS whatever the mode says, so it
# proves only how the prompt arrived (argv vs stdin). Do not combine it with an
# error mode and expect that error; the record path wins.
_record_path = os.environ.get("FAKE_CLI_RECORD")
if _record_path:
    with open(_record_path, "w", encoding="utf8") as f:
        json.dump({"argv": sys.argv, "stdin": prompt}, f)
    print(json.dumps({"event": "result", "result": {
        "status": "SUCCESS", "response": CARDS_JSON, "num_turns": 1,
        "usage": dict(USAGE, total_tokens=15)}}))
    sys.exit(0)

if mode == "fail":
    print("boom", file=sys.stderr)
    sys.exit(2)
if mode == "not_signed_in":
    print("Error: You are not authenticated. Run `claude login` first.",
         file=sys.stderr)
    sys.exit(1)
if mode == "error_result":
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True,
                      "num_turns": 1,
                      "result": "Failed to authenticate: OAuth session expired "
                                "and could not be refreshed",
                      "terminal_reason": "api_error"}))
    sys.exit(1)
if mode == "event_then_slow":
    print(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "WebSearch", "input": {}}]}}), flush=True)
    time.sleep(30)
if mode == "slow":
    time.sleep(30)
if mode == "garbage":
    print("not json")
    sys.exit(0)
if mode == "badjson":
    print(json.dumps({"type": "result", "subtype": "success",
                      "result": "sorry, here is some prose instead of JSON",
                      "usage": USAGE}))
    sys.exit(0)
if mode == "two_cards":
    print(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "WebSearch", "input": {}}]}}), flush=True)
    cards = [{"note_type": "Study Deck - Basic",
              "fields": {"Front": "Clean front", "Back": "a"},
              "tags": [], "images": [], "rationale": "r"},
             {"note_type": "Study Deck - Basic",
              "fields": {"Front": "Duplicate front", "Back": "a"},
              "tags": [], "images": [], "rationale": "r"}]
    print(json.dumps({"type": "result", "subtype": "success",
                      "result": json.dumps(cards), "usage": USAGE}))
    sys.exit(0)
if mode == "codex_top":
    print(json.dumps({"type": "item.completed", "text": CARDS_JSON,
                      "usage": USAGE}))
    sys.exit(0)
if mode == "codex_nested":
    print(json.dumps({"type": "item.completed",
                      "item": {"type": "agent_message", "text": CARDS_JSON},
                      "usage": USAGE}))
    sys.exit(0)
if mode == "with_image":
    cards = [{"note_type": "Study Deck - Basic",
             "fields": {"Front": "q", "Back": "a"},
             "tags": [], "images": [{"source": "url:https://example.com/pic.png",
                                     "alt": "", "attribution": ""}],
             "rationale": "r"}]
    print(json.dumps({"type": "result", "subtype": "success",
                      "result": json.dumps(cards), "usage": USAGE}))
    sys.exit(0)
if mode == "agy_ok":
    print(json.dumps({"event": "step_update", "step_update": {
        "step_type": "agent_response", "state": "DONE", "text_delta": "..."}}),
        flush=True)
    print(json.dumps({"event": "result", "result": {
        "status": "SUCCESS", "response": CARDS_JSON, "num_turns": 1,
        "usage": dict(USAGE, total_tokens=15)}}))
    sys.exit(0)
if mode == "agy_error_result":
    # The captured ERROR result line from tests/agy_stream_samples.ndjson: an
    # empty-prompt failure reported through the "result" event, not stderr.
    print(json.dumps({"event": "result", "result": {
        "conversation_id": "", "status": "ERROR", "response": "",
        "error": 'Error: empty prompt. Usage: agy --print "your prompt here"',
        "duration_seconds": 0.0, "num_turns": 0,
        "usage": {"input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0,
                  "cache_read_tokens": 0, "total_tokens": 0}}}))
    sys.exit(1)
if mode == "agy_permission_denied":
    print('jetski: no output produced ... a tool required the "write_file" '
         "permission that headless mode cannot prompt for, so it was "
         "auto-denied ...", file=sys.stderr)
    sys.exit(1)
print(json.dumps({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "name": "WebSearch", "input": {}}]}}), flush=True)
print(json.dumps({"type": "result", "subtype": "success",
                  "result": CARDS_JSON, "usage": USAGE}))
