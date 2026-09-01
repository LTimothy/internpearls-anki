#!/usr/bin/env python3
# tests/fake_cli.py -- pretend agent CLI for ai_cli tests. Behavior via argv[1]:
#   ok        read stdin, emit one phase line then a claude-style result line
#   slow      sleep 30s before answering (for timeout/cancel tests)
#   garbage   emit non-JSON noise then exit 0
#   fail      exit 2 with stderr
import json
import sys
import time

mode = sys.argv[1] if len(sys.argv) > 1 else "ok"
prompt = sys.stdin.read()
if mode == "fail":
    print("boom", file=sys.stderr)
    sys.exit(2)
if mode == "slow":
    time.sleep(30)
if mode == "garbage":
    print("not json")
    sys.exit(0)
print(json.dumps({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "name": "WebSearch", "input": {}}]}}), flush=True)
cards = [{"note_type": "Basic", "fields": {"Front": "q", "Back": "a"},
          "tags": [], "images": [], "rationale": "r"}]
print(json.dumps({"type": "result", "subtype": "success",
                  "result": json.dumps(cards),
                  "usage": {"input_tokens": 10, "output_tokens": 5}}))
