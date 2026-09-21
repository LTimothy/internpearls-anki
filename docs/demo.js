import { renderWidgetTree } from "./demo-renderer.js";
import {
  ADVANCE_DELAY_MS,
  BOOT_WATCHDOG_MS,
  CANCEL_WATCHDOG_MS,
  DEMO_PROTOCOL_VERSION,
  EDIT_DEBOUNCE_MS,
  REQUEST_WATCHDOG_MS,
  DemoProtocolError,
  jsonClone,
  validatePageMessage,
  validateWorkerMessage,
} from "./demo-protocol.js";

const $ = (id) => document.getElementById(id);

let worker = null;
let workerGeneration = 0;
let requestId = 0;
let inFlight = null;
let queue = [];
let ready = false;
let epoch = 0;
let sequence = 0;
let revision = 0;
let lastSafeResponse = null;
let currentNodes = new Map();
let automaticTimer = null;
let cancelTimer = null;
let editTimer = null;
let blurTimer = null;
let autoSyncTimer = null;
let recovering = false;
let stopped = false;
const pendingEdits = new Map();
const unfinishedEdits = new Set();
const composing = new Set();
const compositionQueue = [];

function toast(message, milliseconds = 6000) {
  const element = $("toast");
  element.textContent = message;
  element.classList.add("show");
  clearTimeout(element.demoTimer);
  element.demoTimer = setTimeout(() => element.classList.remove("show"), milliseconds);
}

function status(message) {
  const element = $("bootline");
  if (element) element.textContent = message;
}

function reportFailure() {
  toast("The demo protocol stopped. Reload this page to start a fresh session.");
}

function clearAutomaticAdvance() {
  if (automaticTimer !== null) clearTimeout(automaticTimer);
  automaticTimer = null;
  const retained = [];
  for (const entry of queue) {
    if (entry.priority === "automatic") entry.resolve(null);
    else retained.push(entry);
  }
  queue = retained;
}

function insertQueue(entry) {
  if (entry.priority === "automatic") {
    queue.push(entry);
    return;
  }
  const automaticIndex = queue.findIndex((item) => item.priority === "automatic");
  if (entry.priority === "cancel") queue.unshift(entry);
  else if (automaticIndex === -1) queue.push(entry);
  else queue.splice(automaticIndex, 0, entry);
}

function enqueueCommand(type, payload, options = {}) {
  return new Promise((resolve, reject) => {
    if (stopped) {
      reject(new DemoProtocolError("protocol-stopped"));
      return;
    }
    insertQueue({
      type,
      payload,
      priority: options.priority || "user",
      handle: options.handle || null,
      authority: options.authority || null,
      resolve,
      reject,
    });
    pumpQueue();
  });
}

function request(type, payload) {
  return enqueueCommand(type, jsonClone(payload));
}

function requestTimedOut(entry) {
  if (inFlight !== entry) return;
  inFlight = null;
  stopped = true;
  if (worker) worker.terminate();
  worker = null;
  for (const queued of queue) queued.reject(new DemoProtocolError("protocol-stopped"));
  queue = [];
  entry.reject(new DemoProtocolError("request-timeout"));
  reportFailure();
}

function pumpQueue() {
  if (!worker || inFlight || !queue.length) return;
  const entry = queue.shift();
  if (entry.authority && (entry.authority.epoch !== epoch
      || entry.authority.sequence !== sequence
      || entry.authority.revision !== revision)) {
    if (entry.priority === "cancel") clearCancelWatchdog();
    entry.resolve(null);
    pumpQueue();
    return;
  }
  let payload;
  try {
    payload = typeof entry.payload === "function" ? entry.payload() : entry.payload;
    const nextRequestId = requestId + 1;
    entry.message = validatePageMessage({
      protocol: DEMO_PROTOCOL_VERSION,
      request_id: nextRequestId,
      type: entry.type,
      payload: jsonClone(payload),
    });
    requestId = nextRequestId;
  } catch (error) {
    if (entry.priority === "cancel") clearCancelWatchdog();
    entry.reject(error);
    pumpQueue();
    return;
  }
  entry.generation = workerGeneration;
  const delay = entry.type === "boot" ? BOOT_WATCHDOG_MS : REQUEST_WATCHDOG_MS;
  entry.watchdog = setTimeout(() => requestTimedOut(entry), delay);
  inFlight = entry;
  worker.postMessage(jsonClone(entry.message));
}

function handleWorkerMessage(generation, raw) {
  if (generation !== workerGeneration) return;
  let message;
  try {
    message = validateWorkerMessage(raw);
  } catch (_error) {
    return;
  }
  const entry = inFlight;
  if (!entry || entry.generation !== generation
      || message.request_id !== entry.message.request_id
      || message.type !== entry.message.type) return;
  clearTimeout(entry.watchdog);
  inFlight = null;
  if (!message.ok) {
    entry.reject(new DemoProtocolError(message.error.code));
  } else {
    lastSafeResponse = jsonClone(message);
    try {
      if (entry.handle) entry.handle(message.payload, message);
      entry.resolve(jsonClone(message.payload));
    } catch (error) {
      entry.reject(error);
    }
  }
  pumpQueue();
}

function createWorker() {
  workerGeneration += 1;
  const generation = workerGeneration;
  worker = new Worker(new URL("./demo-worker.js", import.meta.url), { type: "module" });
  worker.addEventListener("message", (event) => handleWorkerMessage(generation, event.data));
  worker.addEventListener("error", reportFailure);
}

function makeEnvelope(actions) {
  return {
    protocol: DEMO_PROTOCOL_VERSION,
    epoch,
    sequence: sequence + 1,
    render_revision: revision,
    actions: jsonClone(actions),
  };
}

function isCancelAction(action) {
  if (!action || typeof action !== "object") return false;
  if (action.type === "close") return true;
  if (action.type === "key" && action.key === "Escape"
      && Array.isArray(action.modifiers) && !action.modifiers.length) return true;
  if (action.type !== "activate") return false;
  const node = currentNodes.get(action.id);
  return node?.kind === "button" && node.text.trim().toLowerCase() === "cancel";
}

function clearCancelWatchdog() {
  if (cancelTimer !== null) clearTimeout(cancelTimer);
  cancelTimer = null;
}

function startCancelWatchdog(actions) {
  clearCancelWatchdog();
  cancelTimer = setTimeout(() => recoverFromCancel(actions), CANCEL_WATCHDOG_MS);
}

function settleEntriesForRecovery() {
  if (inFlight) {
    clearTimeout(inFlight.watchdog);
    inFlight.resolve(null);
    inFlight = null;
  }
  for (const entry of queue) entry.resolve(null);
  queue = [];
}

async function recoverFromCancel(actions) {
  if (recovering) return;
  recovering = true;
  clearAutomaticAdvance();
  clearCancelWatchdog();
  const recovery = jsonClone(lastSafeResponse?.recovery || []);
  const cancel = { envelope: makeEnvelope(actions) };
  settleEntriesForRecovery();
  if (worker) worker.terminate();
  createWorker();
  try {
    await enqueueCommand("boot", { dark: darkScheme().matches }, {
      priority: "cancel",
      handle: applyBootPayload,
    });
    await enqueueCommand("reset", { recovery, cancel }, {
      priority: "cancel",
      handle: applyCommandPayload,
    });
  } catch (_error) {
    reportFailure();
  } finally {
    recovering = false;
  }
}

function queueActions(actions, options = {}) {
  clearAutomaticAdvance();
  if (composing.size && options.waitForComposition !== false) {
    return deferUntilCompositionEnds(() => queueActions(actions, {
      ...options,
      waitForComposition: false,
    }));
  }
  const flushed = options.flush === false ? [] : takePendingEdits(true);
  const ordered = [...flushed, ...actions];
  const authority = { epoch, sequence, revision };
  const envelope = makeEnvelope(ordered);
  const cancel = ordered.some(isCancelAction);
  if (cancel) startCancelWatchdog(ordered);
  return enqueueCommand("feed", { envelope }, {
    priority: cancel ? "cancel" : (options.priority || "user"),
    authority,
    handle(payload) {
      if (cancel) clearCancelWatchdog();
      applyCommandPayload(payload);
    },
  });
}

function dispatch(actions) {
  if (!Array.isArray(actions)) return Promise.reject(new DemoProtocolError("invalid-message"));
  return queueActions(actions);
}

function scheduleAutomaticAdvance() {
  clearAutomaticAdvance();
  if (composing.size) return;
  automaticTimer = setTimeout(() => {
    automaticTimer = null;
    queueActions([{
      type: "advance",
      elapsed_ms: ADVANCE_DELAY_MS,
      checkpoint_credits: 1,
    }], { flush: false, priority: "automatic" }).catch(reportFailure);
  }, ADVANCE_DELAY_MS);
}

function paintBusy(label) {
  $("dtitle").textContent = "Intern Pearls";
  const line = document.createElement("div");
  line.className = "busyline";
  const spinner = document.createElement("span");
  spinner.className = "spinner";
  const text = document.createElement("span");
  text.textContent = label;
  line.append(spinner, text);
  $("dbody").replaceChildren(line);
  $("dbtns").replaceChildren();
  $("overlay").classList.add("show");
}

function beginFlow(menuId) {
  clearAutomaticAdvance();
  epoch += 1;
  sequence = 0;
  revision = 0;
  currentNodes = new Map();
  pendingEdits.clear();
  unfinishedEdits.clear();
  paintBusy("Working...");
  return enqueueCommand("start", { menu_id: String(menuId), epoch }, {
    priority: "user",
    handle: applyCommandPayload,
  }).catch((error) => {
    reportFailure();
    throw error;
  });
}

function runFlow(menuId) {
  if (!ready) return Promise.resolve(null);
  clearAutomaticAdvance();
  if (composing.size) return deferUntilCompositionEnds(() => runFlow(menuId));
  const edits = takePendingEdits(true);
  if (edits.length && currentNodes.size) {
    return queueActions(edits, { flush: false }).then(() => beginFlow(menuId));
  }
  return beginFlow(menuId);
}

function applyCommandPayload(payload) {
  if (payload.state) renderCollection(payload.state, payload.config);
  if (payload.tooltips?.length) toast(payload.tooltips.join(" · "));
  if (payload.response) applyRunnerResponse(payload.response);
}

function applyRunnerResponse(response) {
  if (response.protocol !== DEMO_PROTOCOL_VERSION || response.epoch !== epoch) return;
  if (response.sequence < sequence || response.render_revision < revision) return;
  if (response.sequence === sequence && response.render_revision === revision) return;
  if (response.sequence > sequence + 1) return;
  revision = response.render_revision;
  if (response.status === "stale") return;
  if (response.status === "contract-error") {
    toast("That action no longer applies to the current dialog.");
    return;
  }
  sequence = response.sequence;
  if (response.status === "done") {
    clearAutomaticAdvance();
    $("overlay").classList.remove("show");
    currentNodes = new Map();
    pendingEdits.clear();
    unfinishedEdits.clear();
    return;
  }
  if (response.status === "error") {
    clearAutomaticAdvance();
    toast("The demo could not finish that action.");
    return;
  }
  showPayload(response.payload);
  if (response.pending.length) scheduleAutomaticAdvance();
}

function actionButton(label, primary, callback) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `btn${primary ? " primary" : ""}`;
  button.textContent = label;
  button.addEventListener("click", () => callback().catch(reportFailure));
  return button;
}

function showSimplePayload(payload) {
  $("dtitle").textContent = payload.title || "Intern Pearls";
  const body = document.createElement("div");
  body.textContent = payload.text || "";
  $("dbody").replaceChildren(body);
  $("dbtns").replaceChildren(actionButton("OK", true, () => queueActions([])));
  $("overlay").classList.add("show");
}

function showPayload(payload) {
  if (payload?.contract_tree) {
    renderDialogTree(payload);
    return;
  }
  if (payload?.kind === "info" || payload?.kind === "warn") {
    showSimplePayload(payload);
    return;
  }
  if (payload?.kind === "work") {
    if (!$("overlay").classList.contains("show")) paintBusy("Working...");
    return;
  }
  showSimplePayload({ text: "This action is not available in the browser demo." });
}

function scheduleEdit(id, element, action) {
  clearAutomaticAdvance();
  unfinishedEdits.add(id);
  pendingEdits.set(id, { element, action });
  if (editTimer !== null) clearTimeout(editTimer);
  editTimer = setTimeout(() => {
    editTimer = null;
    const actions = takePendingEdits(false);
    if (actions.length) queueActions(actions, { flush: false }).catch(reportFailure);
  }, EDIT_DEBOUNCE_MS);
}

function takePendingEdits(finish) {
  if (editTimer !== null) clearTimeout(editTimer);
  if (blurTimer !== null) clearTimeout(blurTimer);
  editTimer = null;
  blurTimer = null;
  const edits = [];
  const ids = [];
  for (const [id, pending] of pendingEdits) {
    if (composing.has(pending.element)) continue;
    edits.push(pending.action(pending.element));
    ids.push(id);
  }
  for (const id of ids) pendingEdits.delete(id);
  if (finish) {
    for (const id of unfinishedEdits) {
      if (currentNodes.get(id)?.actions.includes("finish-edit")) {
        edits.push({ type: "finish-edit", id });
      }
    }
    unfinishedEdits.clear();
  }
  return edits;
}

function deferUntilCompositionEnds(callback) {
  return new Promise((resolve, reject) => {
    compositionQueue.push({ callback, resolve, reject });
  });
}

function drainCompositionQueue() {
  if (composing.size) return;
  for (const entry of compositionQueue.splice(0)) {
    try {
      Promise.resolve(entry.callback()).then(entry.resolve, entry.reject);
    } catch (error) {
      entry.reject(error);
    }
  }
}

function registerInput(element, action) {
  const id = element.dataset.wid;
  element.demoAction = action;
  if (element.matches("select, input[type=checkbox], input[type=radio]")) {
    element.onchange = (event) => {
      clearAutomaticAdvance();
      queueActions([event.currentTarget.demoAction(event.currentTarget)]).catch(reportFailure);
    };
    return;
  }
  element.oncompositionstart = (event) => {
    event.currentTarget.demoComposing = true;
    composing.add(event.currentTarget);
    clearAutomaticAdvance();
    if (editTimer !== null) clearTimeout(editTimer);
    editTimer = null;
  };
  element.oncompositionend = (event) => {
    const control = event.currentTarget;
    control.demoComposing = false;
    composing.clear();
    scheduleEdit(id, control, control.demoAction);
    drainCompositionQueue();
  };
  element.oninput = (event) => {
    const control = event.currentTarget;
    clearAutomaticAdvance();
    if (event.isComposing) {
      control.demoComposing = true;
      composing.add(control);
    }
    unfinishedEdits.add(id);
    pendingEdits.set(id, { element: control, action: control.demoAction });
    if (!control.demoComposing && !composing.has(control)) {
      scheduleEdit(id, control, control.demoAction);
    }
  };
  element.onblur = (event) => {
    const control = event.currentTarget;
    if (!pendingEdits.has(id) || composing.has(control)) return;
    if (blurTimer !== null) clearTimeout(blurTimer);
    blurTimer = setTimeout(() => {
      blurTimer = null;
      const actions = takePendingEdits(true);
      if (actions.length) queueActions(actions, { flush: false }).catch(reportFailure);
    }, 0);
  };
}

function dialogContext() {
  const ctx = {
    inputs: [],
    scrollActions: [],
    registerInput(element, action) {
      ctx.inputs.push({ element, action });
      registerInput(element, action);
    },
    currentActions() {
      return [
        ...ctx.inputs.filter(({ element }) => !element.disabled && !element.readOnly)
          .map(({ element }) => element.demoAction(element)),
        ...ctx.scrollActions,
      ];
    },
    scroll(id, offset) {
      const existing = ctx.scrollActions.find((item) => item.id === id);
      if (existing) existing.offset = offset;
      else ctx.scrollActions.push({ type: "scroll", id, offset });
      clearAutomaticAdvance();
      setTimeout(() => {
        if (!ctx.scrollActions.length) return;
        const actions = ctx.scrollActions.splice(0);
        queueActions(actions).catch(reportFailure);
      }, 0);
    },
    activate(id) {
      queueActions([{ type: "activate", id }]).catch(reportFailure);
    },
    activateLink(id, actionId) {
      queueActions([{ type: "activate-link", id, action_id: actionId }])
        .catch(reportFailure);
    },
    key(id, key, modifiers) {
      if (key.length === 1 && !modifiers.length) return;
      queueActions([{ type: "key", id, key, modifiers: modifiers.map((value) =>
        value.toLowerCase()) }]).catch(reportFailure);
    },
  };
  return ctx;
}

function stateKey(element, selector) {
  const owner = element.closest("[data-wid]");
  if (!owner) return null;
  const index = Array.from(owner.querySelectorAll(selector)).indexOf(element);
  return `${owner.dataset.wid}:${index}`;
}

function captureDomState() {
  const active = document.activeElement;
  const focusOwner = active?.closest?.("[data-wid]") || null;
  return {
    focus: focusOwner ? {
      id: focusOwner.dataset.wid,
      part: active.dataset?.demoPart || "",
      start: typeof active.selectionStart === "number" ? active.selectionStart : null,
      end: typeof active.selectionEnd === "number" ? active.selectionEnd : null,
    } : null,
    expanded: new Map(Array.from($("dbody").querySelectorAll("details"), (element) => [
      stateKey(element, "details"), element.open,
    ])),
    scroll: new Map(Array.from($("dbody").querySelectorAll("[data-wid]"), (element) => [
      element.dataset.wid, [element.scrollLeft, element.scrollTop],
    ])),
  };
}

function syncAttributes(current, next) {
  for (const name of current.getAttributeNames()) {
    if (!next.hasAttribute(name)) current.removeAttribute(name);
  }
  for (const name of next.getAttributeNames()) {
    current.setAttribute(name, next.getAttribute(name));
  }
  const active = document.activeElement;
  const id = current.dataset?.wid;
  const preserveValue = current === active || pendingEdits.has(id) || composing.has(current);
  if (current instanceof HTMLInputElement && next instanceof HTMLInputElement) {
    if (!preserveValue) current.value = next.value;
    current.checked = next.checked;
  } else if (current instanceof HTMLTextAreaElement && next instanceof HTMLTextAreaElement) {
    if (!preserveValue) current.value = next.value;
  } else if (current instanceof HTMLSelectElement && next instanceof HTMLSelectElement) {
    if (!preserveValue) current.value = next.value;
  } else if (current instanceof HTMLDetailsElement && next instanceof HTMLDetailsElement) {
    current.open = next.open;
  }
  for (const name of [
    "onclick", "onchange", "oninput", "onblur", "oncompositionstart",
    "oncompositionend", "onkeydown", "onscroll", "demoAction",
  ]) current[name] = next[name] || null;
}

function patchNode(current, next, oldById, used) {
  if (!current || current.nodeType !== next.nodeType
      || (current.nodeType === Node.ELEMENT_NODE && current.tagName !== next.tagName)) {
    if (current) current.replaceWith(next);
    return next;
  }
  if (current.nodeType === Node.TEXT_NODE) {
    if (current.data !== next.data) current.data = next.data;
    return current;
  }
  syncAttributes(current, next);
  const desired = [];
  const currentChildren = Array.from(current.childNodes);
  for (const [index, nextChild] of Array.from(next.childNodes).entries()) {
    const hasKey = nextChild.nodeType === Node.ELEMENT_NODE && Boolean(nextChild.id);
    const keyed = hasKey ? oldById.get(nextChild.id) : null;
    let candidate = hasKey
      ? (keyed && !used.has(keyed) ? keyed : null)
      : currentChildren[index];
    if (candidate && used.has(candidate)) candidate = null;
    const patched = patchNode(candidate, nextChild, oldById, used);
    used.add(patched);
    desired.push(patched);
  }
  for (const child of desired) current.appendChild(child);
  for (const child of Array.from(current.childNodes)) {
    if (!desired.includes(child)) child.remove();
  }
  return current;
}

function restoreDomState(state) {
  for (const details of $("dbody").querySelectorAll("details")) {
    const key = stateKey(details, "details");
    if (state.expanded.has(key)) details.open = state.expanded.get(key);
  }
  for (const [id, [left, top]] of state.scroll) {
    const element = document.getElementById(id);
    if (element) {
      element.scrollLeft = left;
      element.scrollTop = top;
    }
  }
  if (state.focus) {
    const selector = `[data-wid="${CSS.escape(state.focus.id)}"]`
      + (state.focus.part
        ? `[data-demo-part="${CSS.escape(state.focus.part)}"]` : "");
    const target = document.querySelector(selector);
    if (target) {
      target.focus({ preventScroll: true });
      if (state.focus.start !== null && typeof target.setSelectionRange === "function") {
        target.setSelectionRange(state.focus.start, state.focus.end);
      }
      return;
    }
  }
  const fallback = $("dbody").querySelector("[data-default=true], input, textarea, select, button");
  if (fallback) fallback.focus({ preventScroll: true });
}

function stablePatch(host, nextRoot) {
  const state = captureDomState();
  const oldById = new Map(Array.from(host.querySelectorAll("[id]"), (element) => [
    element.id, element,
  ]));
  const current = host.firstChild;
  if (!current) host.appendChild(nextRoot);
  else patchNode(current, nextRoot, oldById, new Set());
  while (host.childNodes.length > 1) host.lastChild.remove();
  restoreDomState(state);
}

function renderDialogTree(payload) {
  const tree = payload.contract_tree;
  currentNodes = new Map(tree.nodes.map((node) => [node.id, node]));
  const rendered = renderWidgetTree(tree, dialogContext());
  $("dtitle").textContent = payload.title || "Intern Pearls";
  stablePatch($("dbody"), rendered);
  $("dbtns").replaceChildren();
  $("overlay").classList.add("show");
}

const clozeFront = (text) => text.replace(/\{\{c\d+::(.*?)(::.*?)?\}\}/g, "[...]");

function renderCollection(state, config) {
  const area = $("deckarea");
  area.replaceChildren();
  for (const deck of state.decks || []) {
    const heading = document.createElement("div");
    heading.className = "deckname";
    heading.textContent = deck.name.split("::").join(" ▸ ");
    area.appendChild(heading);
    const table = document.createElement("table");
    const header = document.createElement("thead");
    const headerRow = document.createElement("tr");
    for (const label of ["Card front", "Scheduling", "Your notes on the card"] ) {
      const cell = document.createElement("th");
      cell.textContent = label;
      headerRow.appendChild(cell);
    }
    header.appendChild(headerRow);
    const body = document.createElement("tbody");
    for (const card of deck.cards || []) {
      const row = document.createElement("tr");
      const front = document.createElement("td");
      front.className = "front";
      front.appendChild(document.createTextNode(clozeFront(card.front)));
      const back = document.createElement("div");
      back.style.color = "var(--muted)";
      back.style.fontSize = "12px";
      back.textContent = card.back;
      front.appendChild(back);
      const scheduling = document.createElement("td");
      const pill = document.createElement("span");
      pill.className = `pill ${card.interval ? "interval" : "new"}`;
      pill.textContent = card.interval ? `due in ${card.interval}` : "new card";
      scheduling.appendChild(pill);
      const note = document.createElement("td");
      note.className = "noteCell";
      note.contentEditable = "plaintext-only";
      note.textContent = card.notes || "";
      note.addEventListener("blur", () => request("state", {
        action: "set-note", guid: card.guid, text: note.textContent.trim(),
      }).then(applyCommandPayload).catch(reportFailure));
      row.append(front, scheduling, note);
      body.appendChild(row);
    }
    table.append(header, body);
    area.appendChild(table);
  }
  configureAutoSync(config);
}

function configureAutoSync(config) {
  if (autoSyncTimer !== null) clearInterval(autoSyncTimer);
  autoSyncTimer = null;
  if (!config?.auto_sync) return;
  autoSyncTimer = setInterval(() => {
    if (inFlight || $("overlay").classList.contains("show")) return;
    request("state", { action: "auto-sync" })
      .then(applyCommandPayload).catch(reportFailure);
  }, config.interval * 5000);
}

function closeMenu() {
  $("ipMenu").classList.remove("show");
  $("ipMenuBtn").classList.remove("open");
  document.querySelectorAll(".hasSub.open").forEach((element) =>
    element.classList.remove("open"));
}

function buildMenu(tree) {
  const menu = $("ipMenu");
  menu.replaceChildren();
  const addItems = (items, host) => {
    for (const node of items) {
      if (node.t === "sep") {
        host.appendChild(document.createElement("hr"));
      } else if (node.t === "menu") {
        const item = document.createElement("div");
        item.className = "item hasSub";
        item.appendChild(document.createTextNode(`${node.label} `));
        const marker = document.createElement("span");
        marker.style.color = "var(--muted)";
        marker.textContent = "▸";
        item.appendChild(marker);
        const submenu = document.createElement("div");
        submenu.className = "submenu";
        addItems(node.items, submenu);
        item.appendChild(submenu);
        item.addEventListener("click", (event) => {
          if (event.target.closest(".submenu")) return;
          item.classList.toggle("open");
          event.stopPropagation();
        });
        host.appendChild(item);
      } else {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "item";
        button.textContent = node.label;
        button.addEventListener("click", (event) => {
          closeMenu();
          event.stopPropagation();
          runFlow(node.id).catch(reportFailure);
        });
        host.appendChild(button);
      }
    }
  };
  addItems(tree, menu);
}

function applyBootPayload(payload) {
  buildMenu(payload.menu);
  renderCollection(payload.state, payload.config);
  document.querySelectorAll("[data-ship]").forEach((button) => {
    button.disabled = false;
  });
  ready = true;
}

function darkScheme() {
  return window.matchMedia("(prefers-color-scheme: dark)");
}

async function boot() {
  status("Loading the browser demo...");
  createWorker();
  try {
    await enqueueCommand("boot", { dark: darkScheme().matches }, {
      priority: "user",
      handle: applyBootPayload,
    });
  } catch (_error) {
    status("The live demo could not start. The add-on itself is unaffected.");
  }
}

$("ipMenuBtn").addEventListener("click", (event) => {
  if (event.target.closest(".item")) return;
  $("ipMenu").classList.toggle("show");
  $("ipMenuBtn").classList.toggle("open");
  event.stopPropagation();
});
document.addEventListener("click", closeMenu);
document.addEventListener("submit", (event) => event.preventDefault(), true);
document.addEventListener("compositionend", () => {
  composing.clear();
  drainCompositionQueue();
});

document.querySelectorAll("[data-ship]").forEach((button) => {
  button.addEventListener("click", () => request("maintainer", {
    operation: button.dataset.ship,
  }).then((payload) => {
    $("shipped").textContent = `Updated the sample source: ${payload.label}.`;
  }).catch(reportFailure));
});

const scheme = darkScheme();
scheme.addEventListener("change", (event) => {
  if (ready) request("set-theme", { dark: event.matches }).catch(reportFailure);
});

const demo = { request, runFlow, dispatch };
Object.defineProperty(demo, "ready", { enumerable: true, get: () => ready });
window.demo = demo;
boot();
