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
const INTEGER_MAX = 2147483647;

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

function validateRunnerRequest(envelope) {
  exactKeys(envelope, ["protocol", "epoch", "sequence", "render_revision", "actions"]);
  if (envelope.protocol !== DEMO_PROTOCOL_VERSION) fail();
  for (const name of ["epoch", "sequence", "render_revision"]) {
    if (!boundedInteger(envelope[name])) fail();
  }
  if (!Array.isArray(envelope.actions)) fail();
  validateJsonValue(envelope.actions);
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
  if (!Array.isArray(envelope.pending)) fail();
  exactKeys(envelope.safe_status, ["code"]);
  if (typeof envelope.safe_status.code !== "string") fail();
  validateJsonValue(envelope.payload);
  validateJsonValue(envelope.pending);
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
    if (!["fix", "reword", "add", "restyle"].includes(payload.operation)) fail();
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
  exactKeys(entry, ["type", "payload"]);
  if (!MESSAGE_TYPE_SET.has(entry.type) || ["boot", "reset"].includes(entry.type)) fail();
  validatePayload(entry.type, entry.payload);
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
  validateJsonValue(payload);
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
    if (typeof message.error.code !== "string" || !message.error.code) fail();
    if (!isPlainObject(message.payload) || Object.keys(message.payload).length) fail();
  }
  validateJsonValue(message);
  return message;
}

export function jsonClone(value) {
  validateJsonValue(value);
  return JSON.parse(JSON.stringify(value));
}
