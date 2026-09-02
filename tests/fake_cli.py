#!/usr/bin/env python3
# tests/fake_cli.py -- pretend agent CLI for ai_cli tests. Behavior via argv[1]:
#   ok              read stdin, emit one phase line then a claude-style result line
#   slow            sleep 30s before answering (for timeout/cancel tests)
#   event_then_slow emit one phase line, then sleep 30s (for a mid-run-exception test:
#                   the caller's on_event fires while this process is still alive)
#   garbage         emit non-JSON noise then exit 0
#   fail            exit 2 with stderr
#   badjson         emit a result line whose "result" text is not valid card JSON
import json
import sys
import time

mode = sys.argv[1] if len(sys.argv) > 1 else "ok"
prompt = sys.stdin.read()
if mode == "fail":
    print("boom", file=sys.stderr)
    sys.exit(2)
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
                      "usage": {"input_tokens": 10, "output_tokens": 5}}))
    sys.exit(0)
print(json.dumps({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "name": "WebSearch", "input": {}}]}}), flush=True)
# "Study Deck - Basic" (not the bare "Basic" note type) so a flow test can carry
# this card all the way through import against the mock collection's default model.
cards = [{"note_type": "Study Deck - Basic",
          "fields": {"Front": "q", "Back": "a"},
          "tags": [], "images": [], "rationale": "r"}]
print(json.dumps({"type": "result", "subtype": "success",
                  "result": json.dumps(cards),
                  "usage": {"input_tokens": 10, "output_tokens": 5}}))
