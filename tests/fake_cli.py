#!/usr/bin/env python3
# tests/fake_cli.py -- pretend agent CLI for ai_cli tests. Behavior via argv[1]:
#   ok              read stdin, emit one phase line then a claude-style result line
#   slow            sleep 30s before answering (for timeout/cancel tests)
#   event_then_slow emit one phase line, then sleep 30s (for a mid-run-exception test:
#                   the caller's on_event fires while this process is still alive)
#   garbage         emit non-JSON noise then exit 0
#   fail            exit 2 with stderr
#   not_signed_in   exit 1 with an auth-failure stderr message (test_connection)
#   badjson         emit a result line whose "result" text is not valid card JSON
#   two_cards       emit two fixed cards, identically on every invocation (for
#                   revision tests that need the "same shape" case with >1 card)
#   codex_top       codex-style item.completed with text at the top level
#   codex_nested    codex-style item.completed with text nested under "item"
#                   (the shape parse_stream_event's codex branch was actually
#                   reading before I9; kept alongside codex_top since neither
#                   shape is confirmed against a live codex binary)
#   agy_ok          antigravity-style "result" event
#   with_image      one card carrying a url: image (I2 review-gate tests)
import json
import sys
import time

mode = sys.argv[1] if len(sys.argv) > 1 else "ok"
prompt = sys.stdin.read()
# "Study Deck - Basic" (not the bare "Basic" note type) so a flow test can carry
# this card all the way through import against the mock collection's default model.
CARDS = [{"note_type": "Study Deck - Basic",
         "fields": {"Front": "q", "Back": "a"},
         "tags": [], "images": [], "rationale": "r"}]
CARDS_JSON = json.dumps(CARDS)
USAGE = {"input_tokens": 10, "output_tokens": 5}

if mode == "fail":
    print("boom", file=sys.stderr)
    sys.exit(2)
if mode == "not_signed_in":
    print("Error: You are not authenticated. Run `claude login` first.",
         file=sys.stderr)
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
    print(json.dumps({"type": "result", "result": CARDS_JSON, "usage": USAGE}))
    sys.exit(0)
print(json.dumps({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "name": "WebSearch", "input": {}}]}}), flush=True)
print(json.dumps({"type": "result", "subtype": "success",
                  "result": CARDS_JSON, "usage": USAGE}))
