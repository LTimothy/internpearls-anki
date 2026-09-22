import { ACTION_KINDS, NODE_KINDS, REQUIRED_FIELDS } from "./demo-contract.js";
import {
  DEMO_PROTOCOL_VERSION,
  jsonClone,
  validatePageMessage,
  validateWorkerMessage,
} from "./demo-protocol.js";

const PYODIDE_ROOT = "https://cdn.jsdelivr.net/pyodide/v0.26.4/full/";
const SOURCE_REPOSITORY = "LTimothy/internpearls-example-deck";

let pyodide = null;
let harness = null;
let booted = false;
let recovery = [];
let lastRequestId = null;
let processing = Promise.resolve();
const responseCache = new Map();

function fetchOptions() {
  return {
    cache: "no-store",
    credentials: "omit",
    redirect: "error",
    referrerPolicy: "no-referrer",
  };
}

async function fetchBytes(url) {
  const response = await fetch(url, fetchOptions());
  if (!response.ok) throw new Error("asset-fetch-failed");
  return new Uint8Array(await response.arrayBuffer());
}

async function fetchJson(url) {
  const response = await fetch(url, fetchOptions());
  if (!response.ok) throw new Error("asset-fetch-failed");
  return response.json();
}

async function writeAsset(url, path) {
  pyodide.FS.writeFile(path, await fetchBytes(url));
}

function pythonContractSource() {
  const literal = (value) => JSON.stringify(JSON.stringify(value));
  return [
    "import json",
    `ACTION_KINDS = tuple(json.loads(${literal(ACTION_KINDS)}))`,
    `NODE_KINDS = tuple(json.loads(${literal(NODE_KINDS)}))`,
    `REQUIRED_FIELDS = json.loads(${literal(REQUIRED_FIELDS)})`,
  ].join("\n");
}

function callHarness(type, payload) {
  const raw = harness.handle_worker_message(JSON.stringify({ type, payload }));
  if (typeof raw !== "string") throw new Error("python-boundary-failed");
  return JSON.parse(raw);
}

async function boot(payload) {
  if (booted) {
    return { ...callHarness("state", { action: "read" }),
      menu: callHarness("menu", {}).menu };
  }
  const runtime = await import(`${PYODIDE_ROOT}pyodide.mjs`);
  pyodide = await runtime.loadPyodide({ indexURL: PYODIDE_ROOT });
  await pyodide.loadPackage("sqlite3");

  pyodide.FS.mkdirTree("/app/internpearls");
  pyodide.FS.mkdirTree("/app/demo");
  pyodide.FS.mkdirTree("/source/decks");
  pyodide.FS.writeFile(
    "/app/demo_contract_generated.py",
    new TextEncoder().encode(pythonContractSource()),
  );
  const files = await fetchJson("addon/files.json");
  await Promise.all(files.map((name) => writeAsset(
    `addon/${name}`,
    name === "mock_anki.py" ? `/app/${name}`
      : (name === "demo_replay.py" ? "/app/demo/replay.py"
        : `/app/internpearls/${name}`),
  )));
  await writeAsset("demo_harness.py", "/app/demo_harness.py");

  const rawRoot = `https://raw.githubusercontent.com/${SOURCE_REPOSITORY}/main/`;
  const manifest = await fetchJson(`${rawRoot}manifest.json`);
  pyodide.FS.writeFile(
    "/source/manifest.json",
    new TextEncoder().encode(JSON.stringify(manifest)),
  );
  await Promise.all(manifest.decks.map((deck) => writeAsset(
    `${rawRoot}${deck.apkg}`,
    `/source/${deck.apkg}`,
  )));

  pyodide.runPython("import sys; sys.path.insert(0, '/app')");
  harness = pyodide.pyimport("demo_harness");
  if ("dark" in payload) harness.set_dark(payload.dark);
  const state = JSON.parse(harness.boot());
  const menu = callHarness("menu", {}).menu;
  const config = JSON.parse(harness.get_config());
  booted = true;
  return { state, config, tooltips: [], menu };
}

function isAccepted(type, payload, result) {
  if (type === "start" || type === "maintainer" || type === "set-theme") return true;
  if (type === "state") return ["set-note", "auto-sync"].includes(payload.action);
  if (type === "feed") {
    // Recorded only on a status we actually saw. A missing one is not evidence of
    // acceptance, and this log's whole job is to replay faithfully.
    const status = result.response?.status;
    return typeof status === "string" && status !== "stale";
  }
  return false;
}

// Held back until the response it belongs to has validated. A result the protocol
// cannot represent must never reach the log: once one is in there, every later
// message that carries the log fails to serialize too, including the error ones.
let pendingRecord = null;

function runCommand(type, payload, record = true) {
  const result = callHarness(type, payload);
  if (record && isAccepted(type, payload, result)) {
    pendingRecord = jsonClone({ type, payload, result });
  }
  return result;
}

function replayAndCancel(payload) {
  // Built aside and only published once the whole prefix has verified: runCommand
  // applies to the harness before the check, so assigning as we go would leave the
  // log short of a command the harness has already taken.
  const replayed = [];
  for (const entry of payload.recovery) {
    const result = runCommand(entry.type, entry.payload, false);
    if (JSON.stringify(result) !== JSON.stringify(entry.result)
        || result.response?.status === "stale") {
      throw new Error("replay-failed");
    }
    replayed.push(jsonClone(entry));
  }
  recovery = replayed;
  return runCommand("feed", payload.cancel, true);
}

async function execute(message) {
  pendingRecord = null;
  let payload;
  if (message.type === "boot") {
    payload = await boot(message.payload);
  } else {
    if (!booted) throw new Error("worker-not-booted");
    payload = message.type === "reset"
      ? replayAndCancel(message.payload)
      : runCommand(message.type, message.payload, true);
  }
  const next = pendingRecord ? [...recovery, pendingRecord] : recovery;
  const validated = validateWorkerMessage({
    protocol: DEMO_PROTOCOL_VERSION,
    request_id: message.request_id,
    type: message.type,
    ok: true,
    payload: jsonClone(payload),
    error: null,
    recovery: jsonClone(next),
  });
  recovery = next;
  pendingRecord = null;
  return validated;
}

function errorResponse(message, code) {
  const requestId = Number.isInteger(message?.request_id) && message.request_id > 0
    ? Math.min(message.request_id, 2147483647) : 1;
  const type = [
    "boot", "menu", "start", "feed", "state", "maintainer", "set-theme", "reset",
  ].includes(message?.type) ? message.type : "state";
  const base = {
    protocol: DEMO_PROTOCOL_VERSION,
    request_id: requestId,
    type,
    ok: false,
    payload: {},
    error: { code },
  };
  try {
    return validateWorkerMessage({ ...base, recovery: jsonClone(recovery) });
  } catch (_error) {
    // The log itself will not serialize. Answering without it beats not
    // answering, which leaves the page waiting on its watchdog and then stopped.
    return validateWorkerMessage({ ...base, recovery: [] });
  }
}

async function processMessage(raw) {
  let message;
  try {
    message = jsonClone(validatePageMessage(raw));
  } catch (_error) {
    self.postMessage(jsonClone(errorResponse(raw, "invalid-message")));
    return;
  }
  if (responseCache.has(message.request_id)) {
    self.postMessage(jsonClone(responseCache.get(message.request_id)));
    return;
  }
  if (lastRequestId !== null && message.request_id !== lastRequestId + 1) {
    self.postMessage(jsonClone(errorResponse(message, "stale-request")));
    return;
  }
  let response;
  try {
    response = await execute(message);
  } catch (_error) {
    response = errorResponse(
      message,
      message.type === "boot" ? "boot-failed" : "protocol-failed",
    );
  }
  lastRequestId = message.request_id;
  responseCache.set(message.request_id, jsonClone(response));
  if (responseCache.size > 8) responseCache.delete(responseCache.keys().next().value);
  self.postMessage(jsonClone(response));
}

self.addEventListener("message", (event) => {
  // Settled both ways, so one rejection cannot leave the chain permanently
  // rejected and silently swallow every later message.
  const next = () => processMessage(event.data).catch(() => {
    try {
      self.postMessage(jsonClone(errorResponse(event.data, "protocol-failed")));
    } catch (_error) {
      // Nothing safe left to say; the page's request watchdog covers this.
    }
  });
  processing = processing.then(next, next);
});
