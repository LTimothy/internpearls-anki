import { ACTION_KINDS, NODE_KINDS, REQUIRED_FIELDS } from "./demo-contract.js";

export const DEMO_PROTOCOL_VERSION = 1;
export const BOOT_WATCHDOG_MS = 120000;
export const REQUEST_WATCHDOG_MS = 10000;
export const CANCEL_WATCHDOG_MS = 5000;
export const ADVANCE_DELAY_MS = 200;
export const EDIT_DEBOUNCE_MS = 100;

export const MESSAGE_TYPES = Object.freeze([
  "boot",
  "menu",
  "start",
  "feed",
  "state",
  "maintainer",
  "set-theme",
  "reset",
]);

const MESSAGE_TYPE_SET = new Set(MESSAGE_TYPES);
const ACTION_KIND_SET = new Set(ACTION_KINDS);
const NODE_KIND_SET = new Set(NODE_KINDS);
const PUBLIC_ERROR_CODES = new Set([
  "invalid-message", "stale-request", "boot-failed", "protocol-failed",
]);
const INTEGER_MAX = 2147483647;
const INTEGER_MIN = -2147483648;
const ALIGNMENTS = new Set(["start", "center", "end", "justify"]);
const SIZE_POLICIES = new Set([
  "fixed", "minimum", "maximum", "preferred", "expanding", "minimum-expanding", "ignored",
]);

export class DemoProtocolError extends Error {
  constructor(code) {
    super(code);
    this.name = "DemoProtocolError";
    this.code = code;
  }
}

function fail() {
  throw new DemoProtocolError("invalid-message");
}

function exactKeys(value, keys) {
  if (!isPlainObject(value)) fail();
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  if (actual.length !== expected.length
      || actual.some((key, index) => key !== expected[index])) fail();
}

function isPlainObject(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function boundedInteger(value, minimum = 0) {
  return Number.isInteger(value) && value >= minimum && value <= INTEGER_MAX;
}

function nonemptyString(value, maximum = 10000) {
  if (typeof value !== "string" || !value.length || value.length > maximum) fail();
}

function stringArray(value, unique = false) {
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) fail();
  if (unique && new Set(value).size !== value.length) fail();
}

function textValue(value) {
  if (typeof value !== "string" || value.length > 1048576) fail();
}

function identifierArray(value, unique = false) {
  stringArray(value, unique);
  for (const item of value) nonemptyString(item, 1024);
}

function signedInteger(value) {
  return Number.isInteger(value) && value >= INTEGER_MIN && value <= INTEGER_MAX;
}

function finiteNumber(value) {
  return typeof value === "number" && Number.isFinite(value);
}

function boundedNumber(value) {
  return finiteNumber(value) && value >= INTEGER_MIN && value <= INTEGER_MAX;
}

function validateMargins(value) {
  exactKeys(value, ["left", "top", "right", "bottom"]);
  for (const name of ["left", "top", "right", "bottom"]) {
    if (!boundedInteger(value[name])) fail();
  }
}

function validateNodeKind(node) {
  if (node.kind === "label") {
    if (!["plain", "rich", "markdown"].includes(node.format)) fail();
    textValue(node.text);
    if (typeof node.wrap !== "boolean" || !ALIGNMENTS.has(node.alignment)
        || typeof node.strike !== "boolean" || typeof node.selectable !== "boolean") fail();
    identifierArray(node.link_actions, true);
  } else if (node.kind === "button") {
    textValue(node.text);
    if (typeof node.checkable !== "boolean" || typeof node.checked !== "boolean"
        || !["accept", "reject", "destructive", "help", "apply", "reset", "yes", "no",
          "close", "other"].includes(node.role)
        || typeof node.default !== "boolean" || typeof node.escape !== "boolean") fail();
  } else if (node.kind === "buttons") {
    identifierArray(node.button_ids, true);
    stringArray(node.standard_roles);
    if (node.standard_roles.some((role) => role.length > 64)) fail();
  } else if (node.kind === "check" || node.kind === "radio") {
    textValue(node.text);
    if (typeof node.checked !== "boolean"
        || (node.group_id !== null && typeof node.group_id !== "string")
        || (typeof node.group_id === "string" && node.group_id.length > 1024)
        || typeof node.exclusive !== "boolean") fail();
  } else if (node.kind === "line" || node.kind === "textarea") {
    if (!(typeof node.value === "string" || finiteNumber(node.value))) fail();
    textValue(node.placeholder);
    if (!boundedInteger(node.selection_start) || !boundedInteger(node.selection_end)
        || typeof node.password !== "boolean" || !boundedInteger(node.max_length)
        || !boundedInteger(node.max_blocks)) fail();
  } else if (node.kind === "combo") {
    if (!Array.isArray(node.options) || !signedInteger(node.current_index)
        || typeof node.editable !== "boolean") fail();
    textValue(node.current_text);
    textValue(node.editor_value);
    const optionIds = new Set();
    for (const option of node.options) {
      exactKeys(option, ["id", "label"]);
      nonemptyString(option.id, 1024);
      textValue(option.label);
      if (optionIds.has(option.id)) fail();
      optionIds.add(option.id);
    }
  } else if (node.kind === "spin") {
    if (!(typeof node.value === "string" || finiteNumber(node.value))
        || !boundedNumber(node.minimum) || !boundedNumber(node.maximum)
        || !boundedNumber(node.step) || node.step <= 0) fail();
    textValue(node.suffix);
    textValue(node.special_value_text);
  } else if (["row", "col", "box", "frame"].includes(node.kind)) {
    validateMargins(node.margins);
    if (!boundedInteger(node.gap) || !Array.isArray(node.stretches)
        || node.stretches.some((value) => !boundedInteger(value))
        || !ALIGNMENTS.has(node.alignment)) fail();
  } else if (node.kind === "grid") {
    if (!Array.isArray(node.cells) || !Array.isArray(node.column_minimums)
        || !Array.isArray(node.column_stretches)
        || node.column_minimums.some((value) => !boundedInteger(value))
        || node.column_stretches.some((value) => !boundedInteger(value))) fail();
    for (const cell of node.cells) {
      exactKeys(cell, ["id", "row", "column", "row_span", "column_span", "alignment"]);
      nonemptyString(cell.id, 1024);
      if (!boundedInteger(cell.row) || !boundedInteger(cell.column)
          || !boundedInteger(cell.row_span, 1) || !boundedInteger(cell.column_span, 1)
          || !ALIGNMENTS.has(cell.alignment)) fail();
    }
  } else if (node.kind === "form") {
    if (!Array.isArray(node.rows)) fail();
    for (const row of node.rows) {
      exactKeys(row, ["label_id", "field_id"]);
      nonemptyString(row.label_id, 1024);
      nonemptyString(row.field_id, 1024);
    }
  } else if (node.kind === "stack") {
    identifierArray(node.pages, true);
    if (node.current_page !== null) nonemptyString(node.current_page, 1024);
  } else if (node.kind === "scroll") {
    for (const name of ["offset", "extent", "shown_count", "total_count"]) {
      if (!boundedInteger(node[name])) fail();
    }
    identifierArray(node.row_ids, true);
  } else if (node.kind === "spacer" || node.kind === "hline") {
    if (!["horizontal", "vertical"].includes(node.orientation)
        || !SIZE_POLICIES.has(node.size_policy)) fail();
  }
}

export function validateJsonValue(value, depth = 0, ancestors = new Set()) {
  if (depth > 64) fail();
  if (value === null || typeof value === "string" || typeof value === "boolean") {
    return value;
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) fail();
    return value;
  }
  if (typeof value !== "object") fail();
  if (ancestors.has(value)) fail();
  ancestors.add(value);
  if (Array.isArray(value)) {
    if (value.length > 10000) fail();
    for (const item of value) validateJsonValue(item, depth + 1, ancestors);
  } else {
    if (!isPlainObject(value) || Object.keys(value).length > 10000) fail();
    for (const [key, item] of Object.entries(value)) {
      if (typeof key !== "string") fail();
      validateJsonValue(item, depth + 1, ancestors);
    }
  }
  ancestors.delete(value);
  return value;
}

function validateAction(action) {
  if (!isPlainObject(action) || !ACTION_KIND_SET.has(action.type)) fail();
  exactKeys(action, REQUIRED_FIELDS.actions[action.type]);
  if ("id" in action) nonemptyString(action.id, 255);
  if ("action_id" in action) nonemptyString(action.action_id, 255);
  if ("option_id" in action) nonemptyString(action.option_id, 255);
  if ("checked" in action && typeof action.checked !== "boolean") fail();
  if ("value" in action && typeof action.value !== "string" && action.type !== "edit-text") fail();
  if (action.type === "edit-text") {
    if (typeof action.value !== "string" || typeof action.composing !== "boolean"
        || !boundedInteger(action.selection_start) || !boundedInteger(action.selection_end)) fail();
  }
  for (const name of ["elapsed_ms", "checkpoint_credits", "offset"]) {
    if (name in action && !boundedInteger(action[name])) fail();
  }
  if (action.type === "key") {
    nonemptyString(action.key, 64);
    stringArray(action.modifiers, true);
    if (action.modifiers.some((item) => !["alt", "control", "meta", "shift"].includes(item))) fail();
  }
  if (action.type === "select-files") {
    stringArray(action.accept);
    if (!Array.isArray(action.files)) fail();
    for (const file of action.files) {
      exactKeys(file, ["name", "size", "type"]);
      nonemptyString(file.name, 255);
      if (!boundedInteger(file.size) || typeof file.type !== "string") fail();
    }
  }
}

function validateRunnerRequest(envelope) {
  exactKeys(envelope, ["protocol", "epoch", "sequence", "render_revision", "actions"]);
  if (envelope.protocol !== DEMO_PROTOCOL_VERSION) fail();
  for (const name of ["epoch", "sequence", "render_revision"]) {
    if (!boundedInteger(envelope[name])) fail();
  }
  if (!Array.isArray(envelope.actions)) fail();
  for (const action of envelope.actions) validateAction(action);
}

function validateErrorPayload(payload) {
  if (!isPlainObject(payload) || typeof payload.code !== "string") fail();
  const allowed = new Set(["code", "kind", "id"]);
  if (Object.keys(payload).some((key) => !allowed.has(key))) fail();
  if (![
    "unsupported-protocol", "unknown-node-kind", "missing-required-field",
    "unknown-enum-value", "unknown-widget-id", "hidden-target", "disabled-target",
    "invalid-option-id", "invalid-link-id", "stale-epoch", "stale-sequence",
    "stale-render-revision", "invalid-envelope", "invalid-action",
    "action-not-allowed", "scheduler-limit", "journal-limit",
  ].includes(payload.code)) fail();
  if ("kind" in payload && typeof payload.kind !== "string") fail();
  if ("id" in payload && typeof payload.id !== "string") fail();
}

function validateContractTree(tree) {
  exactKeys(tree, ["root_id", "nodes"]);
  nonemptyString(tree.root_id, 255);
  if (!Array.isArray(tree.nodes) || !tree.nodes.length) fail();
  const ids = new Set();
  for (const node of tree.nodes) {
    if (!isPlainObject(node) || !NODE_KIND_SET.has(node.kind)) fail();
    exactKeys(node, [...REQUIRED_FIELDS.node, ...REQUIRED_FIELDS.node_kinds[node.kind]]);
    nonemptyString(node.id, 255);
    if (ids.has(node.id)) fail();
    ids.add(node.id);
    if (node.parent_id !== null && typeof node.parent_id !== "string") fail();
    stringArray(node.children, true);
    stringArray(node.style_roles, true);
    stringArray(node.actions, true);
    if (node.actions.some((action) => !ACTION_KIND_SET.has(action))) fail();
    for (const name of ["visible", "effective_visible", "enabled", "effective_enabled", "readonly"]) {
      if (typeof node[name] !== "boolean") fail();
    }
    for (const name of ["accessible_name", "accessible_description", "tooltip", "focus_policy"]) {
      if (typeof node[name] !== "string") fail();
    }
    validateNodeKind(node);
  }
  if (!ids.has(tree.root_id)) fail();
}

function validatePending(pending) {
  if (!Array.isArray(pending)) fail();
  for (const item of pending) {
    exactKeys(item, ["task_id", "kind", "stage"]);
    if (!Array.isArray(item.task_id) || item.task_id.length !== 4
        || item.task_id.some((part) => !boundedInteger(part))) fail();
    nonemptyString(item.kind, 255);
    nonemptyString(item.stage, 1000);
  }
}

function validateRunnerResponse(envelope) {
  exactKeys(envelope, [
    "protocol", "epoch", "sequence", "render_revision", "status", "payload",
    "pending", "safe_status",
  ]);
  if (envelope.protocol !== DEMO_PROTOCOL_VERSION) fail();
  for (const name of ["epoch", "sequence", "render_revision"]) {
    if (!boundedInteger(envelope[name])) fail();
  }
  if (!["need", "done", "error", "stale", "contract-error"].includes(envelope.status)) {
    fail();
  }
  validatePending(envelope.pending);
  exactKeys(envelope.safe_status, ["code"]);
  nonemptyString(envelope.safe_status.code, 255);
  if (["error", "stale", "contract-error"].includes(envelope.status)) {
    validateErrorPayload(envelope.payload);
  } else if (!isPlainObject(envelope.payload)) {
    fail();
  } else if (envelope.payload.contract_tree) {
    validateContractTree(envelope.payload.contract_tree);
  }
}

function validatePayload(type, payload) {
  if (!isPlainObject(payload)) fail();
  if (type === "boot") {
    const keys = Object.keys(payload);
    if (keys.length > 1 || (keys.length === 1 && keys[0] !== "dark")) fail();
    if ("dark" in payload && typeof payload.dark !== "boolean") fail();
  } else if (type === "menu") {
    exactKeys(payload, []);
  } else if (type === "start") {
    exactKeys(payload, ["menu_id", "epoch"]);
    if (typeof payload.menu_id !== "string" || !payload.menu_id
        || !boundedInteger(payload.epoch)) fail();
  } else if (type === "feed") {
    exactKeys(payload, ["envelope"]);
    validateRunnerRequest(payload.envelope);
  } else if (type === "state") {
    if (typeof payload.action !== "string") fail();
    if (payload.action === "read" || payload.action === "auto-sync") {
      exactKeys(payload, ["action"]);
    } else if (payload.action === "set-note") {
      exactKeys(payload, ["action", "guid", "text"]);
      if (typeof payload.guid !== "string" || !payload.guid
          || typeof payload.text !== "string") fail();
    } else if (payload.action === "list-files") {
      exactKeys(payload, ["action", "folder"]);
      if (typeof payload.folder !== "string") fail();
    } else {
      fail();
    }
  } else if (type === "maintainer") {
    exactKeys(payload, ["operation"]);
    if (!["fix", "reword", "add", "restyle", "history", "bulk", "auto"]
      .includes(payload.operation)) fail();
  } else if (type === "set-theme") {
    exactKeys(payload, ["dark"]);
    if (typeof payload.dark !== "boolean") fail();
  } else if (type === "reset") {
    exactKeys(payload, ["recovery", "cancel"]);
    if (!Array.isArray(payload.recovery)) fail();
    for (const entry of payload.recovery) validateRecoveryEntry(entry);
    validatePayload("feed", payload.cancel);
  } else {
    fail();
  }
  validateJsonValue(payload);
}

function validateRecoveryEntry(entry) {
  exactKeys(entry, ["type", "payload", "result"]);
  if (!MESSAGE_TYPE_SET.has(entry.type) || ["boot", "reset"].includes(entry.type)) fail();
  validatePayload(entry.type, entry.payload);
  validateResponsePayload(entry.type, entry.result);
  return entry;
}

export function validatePageMessage(message) {
  exactKeys(message, ["protocol", "request_id", "type", "payload"]);
  if (message.protocol !== DEMO_PROTOCOL_VERSION || !boundedInteger(message.request_id, 1)
      || !MESSAGE_TYPE_SET.has(message.type)) fail();
  validatePayload(message.type, message.payload);
  return message;
}

function validateResponsePayload(type, payload) {
  if (!isPlainObject(payload)) fail();
  if (type === "boot") {
    exactKeys(payload, ["state", "config", "tooltips", "menu"]);
  } else if (type === "menu") {
    exactKeys(payload, ["menu"]);
  } else if (["start", "feed", "reset"].includes(type)) {
    exactKeys(payload, ["state", "config", "tooltips", "response"]);
    validateRunnerResponse(payload.response);
  } else if (type === "state") {
    const keys = Object.keys(payload).sort();
    const stateKeys = ["config", "state", "tooltips"];
    const fileKeys = ["files"];
    if (keys.join("\0") !== stateKeys.sort().join("\0")
        && keys.join("\0") !== fileKeys.join("\0")) fail();
  } else if (type === "maintainer") {
    exactKeys(payload, ["label", "deck"]);
  } else if (type === "set-theme") {
    exactKeys(payload, []);
  } else {
    fail();
  }
  if ("state" in payload) validateState(payload.state);
  if ("config" in payload) validateConfig(payload.config);
  if ("tooltips" in payload) stringArray(payload.tooltips);
  if ("menu" in payload) validateMenu(payload.menu);
  if ("files" in payload) stringArray(payload.files);
  if (type === "maintainer") {
    nonemptyString(payload.label);
    nonemptyString(payload.deck);
  }
  validateJsonValue(payload);
}

function validateMenu(menu) {
  if (!Array.isArray(menu)) fail();
  for (const item of menu) {
    if (!isPlainObject(item) || !["action", "item", "menu", "sep"].includes(item.t)) fail();
    if (item.t === "sep") exactKeys(item, ["t"]);
    else if (item.t === "action" || item.t === "item") {
      exactKeys(item, ["t", "id", "label"]);
      nonemptyString(item.id, 255);
      nonemptyString(item.label);
    } else {
      exactKeys(item, ["t", "label", "items"]);
      nonemptyString(item.label);
      validateMenu(item.items);
    }
  }
}

function validateConfig(config) {
  exactKeys(config, ["auto_sync", "interval"]);
  if (typeof config.auto_sync !== "boolean" || !boundedInteger(config.interval)) fail();
}

function validateState(state) {
  exactKeys(state, ["decks", "version"]);
  if (!Array.isArray(state.decks) || typeof state.version !== "string") fail();
  for (const deck of state.decks) {
    exactKeys(deck, ["name", "cards"]);
    if (typeof deck.name !== "string" || !Array.isArray(deck.cards)) fail();
    for (const card of deck.cards) {
      if (!isPlainObject(card)) fail();
      for (const name of ["guid", "front", "back", "notes"]) {
        if (typeof card[name] !== "string") fail();
      }
      if (typeof card.cloze !== "boolean"
          || (card.interval !== null && typeof card.interval !== "string")) fail();
    }
  }
}

export function validateWorkerMessage(message) {
  exactKeys(message, [
    "protocol", "request_id", "type", "ok", "payload", "error", "recovery",
  ]);
  if (message.protocol !== DEMO_PROTOCOL_VERSION || !boundedInteger(message.request_id, 1)
      || !MESSAGE_TYPE_SET.has(message.type) || typeof message.ok !== "boolean") fail();
  if (!Array.isArray(message.recovery)) fail();
  for (const entry of message.recovery) validateRecoveryEntry(entry);
  if (message.ok) {
    if (message.error !== null) fail();
    validateResponsePayload(message.type, message.payload);
  } else {
    exactKeys(message.error, ["code"]);
    if (!PUBLIC_ERROR_CODES.has(message.error.code)) fail();
    if (!isPlainObject(message.payload) || Object.keys(message.payload).length) fail();
  }
  validateJsonValue(message);
  return message;
}

export function jsonClone(value) {
  validateJsonValue(value);
  return JSON.parse(JSON.stringify(value));
}
