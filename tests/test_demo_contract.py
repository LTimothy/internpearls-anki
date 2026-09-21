import json
from pathlib import Path

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
