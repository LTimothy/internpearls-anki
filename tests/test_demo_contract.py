import json
import os
from pathlib import Path
import subprocess
import sys

from tests import demo_contract_generated as generated


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "demo" / "schema-v1.json").read_text())

NODE_FIELDS = {
    "label": ["format", "text", "wrap", "alignment", "strike", "selectable", "link_actions"],
    "button": ["text", "checkable", "checked", "role", "default", "escape"],
    "buttons": ["button_ids", "standard_roles"],
    "check": ["text", "checked", "group_id", "exclusive"],
    "radio": ["text", "checked", "group_id", "exclusive"],
    "line": ["value", "placeholder", "selection_start", "selection_end", "password", "max_length", "max_blocks"],
    "textarea": ["value", "placeholder", "selection_start", "selection_end", "password", "max_length", "max_blocks"],
    "combo": ["options", "current_index", "current_text", "editable", "editor_value"],
    "spin": ["value", "minimum", "maximum", "step", "suffix", "special_value_text"],
    "row": ["margins", "gap", "stretches", "alignment"],
    "col": ["margins", "gap", "stretches", "alignment"],
    "box": ["margins", "gap", "stretches", "alignment"],
    "frame": ["margins", "gap", "stretches", "alignment"],
    "grid": ["cells", "column_minimums", "column_stretches"],
    "form": ["rows"],
    "stack": ["pages", "current_page"],
    "scroll": ["offset", "extent", "shown_count", "total_count", "row_ids"],
    "spacer": ["orientation", "size_policy"],
    "hline": ["orientation", "size_policy"],
}

ACTION_FIELDS = {
    "activate": ["id"],
    "toggle": ["id", "checked"],
    "select-option": ["id", "option_id"],
    "edit-text": ["id", "value", "selection_start", "selection_end", "composing"],
    "finish-edit": ["id"],
    "activate-link": ["id", "action_id"],
    "key": ["id", "key", "modifiers"],
    "scroll": ["id", "offset"],
    "select-files": ["id", "accept", "files"],
    "advance": ["elapsed_ms", "checkpoint_credits"],
    "close": ["id"],
}


def test_python_registry_matches_schema_exactly():
    assert generated.SCHEMA_VERSION == SCHEMA["schema_version"]
    assert generated.NODE_KINDS == tuple(SCHEMA["node_kinds"])
    assert generated.ACTION_KINDS == tuple(SCHEMA["action_kinds"])
    expected_required = dict(SCHEMA["required_fields"])
    expected_required["node_kinds"] = SCHEMA["node_fields"]
    assert generated.REQUIRED_FIELDS == expected_required


def test_schema_maps_every_node_kind_to_its_exact_fields():
    assert SCHEMA["node_fields"] == NODE_FIELDS
    branches = SCHEMA["$defs"]["node"]["allOf"]
    assert len(branches) == len(NODE_FIELDS)
    by_kind = {
        branch["if"]["properties"]["kind"]["const"]: branch["then"]["required"]
        for branch in branches
    }
    assert set(by_kind) == set(SCHEMA["node_kinds"])
    assert by_kind == NODE_FIELDS
    common = set(SCHEMA["required_fields"]["node"])
    assert set(SCHEMA["$defs"]["node_common_field_name"]["enum"]) == common
    for branch in branches:
        node_kind = branch["if"]["properties"]["kind"]["const"]
        allowed = branch["then"]["propertyNames"]["anyOf"]
        assert allowed[0] == {"$ref": "#/$defs/node_common_field_name"}
        assert allowed[1]["enum"] == NODE_FIELDS[node_kind]
    specialised = {
        branch["if"]["properties"]["kind"]["const"]: branch["then"]
        for branch in branches
    }
    assert specialised["line"]["properties"]["value"]["type"] == "string"
    assert specialised["textarea"]["properties"]["value"]["type"] == "string"
    assert specialised["spin"]["properties"]["value"]["type"] == "number"


def test_action_schemas_have_exact_payloads_and_reject_extra_keys():
    assert SCHEMA["action_fields"] == ACTION_FIELDS
    for action_kind, payload_fields in ACTION_FIELDS.items():
        definition = SCHEMA["$defs"]["action_" + action_kind.replace("-", "_")]
        expected = {"type", *payload_fields}
        assert set(definition["properties"]) == expected
        assert set(definition["required"]) == expected
        assert definition["additionalProperties"] is False
        assert definition["properties"]["type"] == {"const": action_kind}


def test_request_and_response_envelopes_are_closed_and_exact():
    for envelope in ("request", "response"):
        definition = SCHEMA["$defs"][envelope]
        expected = set(SCHEMA["required_fields"][envelope])
        assert set(definition["properties"]) == expected
        assert set(definition["required"]) == expected
        assert definition["additionalProperties"] is False


def test_response_envelope_matches_replay_shape():
    properties = SCHEMA["$defs"]["response"]["properties"]
    assert properties["status"]["enum"] == [
        "need", "done", "error", "stale", "contract-error",
    ]
    assert properties["pending"]["type"] == "array"
    assert properties["safe_status"]["required"] == ["code"]
    assert properties["safe_status"]["additionalProperties"] is False
    assert SCHEMA["$defs"]["response"]["allOf"] == [{
        "if": {"properties": {"status": {"enum": ["error", "stale", "contract-error"]}}},
        "then": {"properties": {"payload": {"$ref": "#/$defs/error"}}},
    }]


def test_error_codes_are_fixed_and_include_renderer_contract_failures():
    codes = SCHEMA["error_codes"]
    assert SCHEMA["$defs"]["error"]["properties"]["code"]["enum"] == codes
    assert "unknown-node-kind" in codes
    assert "unknown-widget-id" in codes
    assert SCHEMA["$defs"]["error"]["additionalProperties"] is False
    assert set(SCHEMA["$defs"]["error"]["properties"]) == {"code", "kind", "id"}


def test_semantic_action_checks_are_named_for_the_runtime_validator():
    assert SCHEMA["semantic_rules"] == [
        "action-target-id-matches-node-id",
        "action-type-permitted-by-node",
        "option-id-present-on-combo",
        "link-action-id-present-on-label",
        "target-effectively-visible",
        "target-effectively-enabled",
        "tree-node-ids-unique",
        "combo-option-ids-unique",
        "grid-cell-ids-unique",
    ]


def test_stable_id_arrays_reject_duplicates():
    properties = SCHEMA["$defs"]["node"]["properties"]
    for name in ("children", "link_actions", "button_ids", "pages", "row_ids"):
        assert properties[name]["uniqueItems"] is True


def test_contract_bounds_numbers_and_closes_enums():
    assert SCHEMA["$defs"]["bounded_integer"] == {
        "type": "integer", "minimum": -2147483648, "maximum": 2147483647,
    }
    assert SCHEMA["$defs"]["nonnegative_integer"] == {
        "type": "integer", "minimum": 0, "maximum": 2147483647,
    }
    assert SCHEMA["$defs"]["orientation"]["enum"] == ["horizontal", "vertical"]
    assert SCHEMA["$defs"]["action_key"]["properties"]["modifiers"]["items"]["enum"] == [
        "alt", "control", "meta", "shift",
    ]


def test_dispatch_requires_action_to_be_listed_on_target_node():
    branches = SCHEMA["$defs"]["action_dispatch"]["oneOf"]
    assert len(branches) == len(ACTION_FIELDS)
    for action_kind, branch in zip(SCHEMA["action_kinds"], branches):
        assert branch["properties"]["action"] == {
            "$ref": "#/$defs/action_" + action_kind.replace("-", "_")
        }
        assert branch["properties"]["node"]["properties"]["actions"]["contains"] == {
            "const": action_kind
        }


def test_worker_python_boundary_rejects_unknown_message_types():
    result = _run_harness_validation_probe("""
valid = harness.validate_worker_message({"type": "state", "payload": {"action": "read"}})
assert valid == {"type": "state", "payload": {"action": "read"}}
invalid = [
    {"type": "unknown", "payload": {}},
    {"type": 4, "payload": {}},
    {"type": "feed", "payload": {"envelope": {"actions": []}}},
]
for message in invalid:
    try:
        harness.validate_worker_message(message)
    except ValueError as error:
        assert str(error) == "invalid-message"
    else:
        raise AssertionError("invalid message type accepted")
""")
    assert result.returncode == 0, result.stdout + result.stderr


def test_worker_python_boundary_rejects_non_json_payloads():
    result = _run_harness_validation_probe("""
def feed(value):
    return {"type": "feed", "payload": {"envelope": {
        "protocol": 1,
        "epoch": 1,
        "sequence": 1,
        "render_revision": 0,
        "actions": [value],
    }}}

for message in [feed(object()), feed({1, 2}), feed(float("nan")), feed({1: "value"})]:
    try:
        harness.validate_worker_message(message)
    except ValueError as error:
        assert str(error) == "invalid-message"
    else:
        raise AssertionError("non-JSON payload accepted")
""")
    assert result.returncode == 0, result.stdout + result.stderr


def test_worker_python_boundary_rejects_malformed_typed_actions():
    result = _run_harness_validation_probe("""
def feed(action, **changes):
    envelope = {
        "protocol": 1,
        "epoch": 1,
        "sequence": 1,
        "render_revision": 1,
        "actions": [action],
    }
    envelope.update(changes)
    return {"type": "feed", "payload": {"envelope": envelope}}

invalid = [
    feed({"type": "unknown"}),
    feed({"type": "activate", "id": "button", "extra": True}),
    feed({"type": "edit-text", "id": "field", "value": "x",
          "selection_start": True, "selection_end": 1, "composing": False}),
    feed({"type": "activate", "id": "button"}, protocol=True),
    feed({"type": "activate", "id": "button"}, sequence=False),
]
for message in invalid:
    try:
        harness.validate_worker_message(message)
    except ValueError as error:
        assert str(error) == "invalid-message"
    else:
        raise AssertionError("malformed typed action accepted")
""")
    assert result.returncode == 0, result.stdout + result.stderr


def test_worker_python_boundary_rejects_malformed_results():
    result = _run_harness_validation_probe("""
valid_state = {
    "state": {"decks": [{"name": "Sample", "cards": []}], "version": "1.0"},
    "config": {"auto_sync": False, "interval": 1},
    "tooltips": [],
}
invalid = [
    ("menu", {"menu": {}}),
    ("state", {**valid_state, "config": {"auto_sync": "no", "interval": 1}}),
    ("state", {**valid_state, "tooltips": [4]}),
    ("state", {**valid_state, "state": {"decks": "bad", "version": "1.0"}}),
    ("feed", {**valid_state, "response": {
        "protocol": 1, "epoch": 1, "sequence": 1, "render_revision": 1,
        "status": "need", "payload": {"kind": "work"},
        "pending": [{"task_id": [1, 1, 1], "kind": "work", "stage": "start"}],
        "safe_status": {"code": "working"},
    }}),
]
for message_type, payload in invalid:
    try:
        harness.validate_worker_result(message_type, payload)
    except ValueError as error:
        assert str(error) == "invalid-message"
    else:
        raise AssertionError("malformed worker result accepted")
""")
    assert result.returncode == 0, result.stdout + result.stderr


def test_worker_start_and_feed_preserve_tooltips():
    result = _run_harness_validation_probe("""
import json

response = {
    "protocol": 1, "epoch": 1, "sequence": 0, "render_revision": 1,
    "status": "done", "payload": {}, "pending": [],
    "safe_status": {"code": "ready"},
}
harness.start_protocol = lambda menu_id, epoch: json.dumps(response)
harness.feed_protocol = lambda envelope: json.dumps({**response, "sequence": 1})
harness.MOCK.gui.tooltips[:] = ["Generic notice"]
started = json.loads(harness.handle_worker_message(json.dumps({
    "type": "start", "payload": {"menu_id": "sample", "epoch": 1},
})))
assert started["tooltips"] == ["Generic notice"]
harness.MOCK.gui.tooltips[:] = ["Second notice"]
fed = json.loads(harness.handle_worker_message(json.dumps({
    "type": "feed", "payload": {"envelope": {
        "protocol": 1, "epoch": 1, "sequence": 1,
        "render_revision": 1, "actions": [],
    }},
})))
assert fed["tooltips"] == ["Second notice"]
""")
    assert result.returncode == 0, result.stdout + result.stderr


def _run_harness_validation_probe(body):
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join([
        str(ROOT / "docs"), str(ROOT / "tests"), str(ROOT),
        environment.get("PYTHONPATH", ""),
    ])
    return subprocess.run(
        [sys.executable, "-c", "import demo_harness as harness\n" + body],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )
