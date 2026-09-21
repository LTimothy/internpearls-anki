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
let currentRootId = null;
let editVersion = 0;
let retryEditScheduled = false;
let retryFinishes = [];
let retryActions = [];
let dialogReturnFocus = null;
const pendingEdits = new Map();
const unfinishedEdits = new Map();
const finishingEdits = new Map();
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

function emptyEditBatch() {
  return { actions: [], transaction: { edits: [], finishes: [] } };
}

function transactionHasEdits(transaction) {
  return Boolean(transaction?.edits.length || transaction?.finishes.length);
}

function nodeSemanticSignature(id) {
  const node = currentNodes.get(id);
  if (!node) return null;
  const semantic = jsonClone(node);
  for (const name of [
    "checked", "current_index", "current_text", "editor_value", "offset",
    "selection_end", "selection_start", "shown_count", "value",
  ]) delete semantic[name];
  return JSON.stringify(semantic);
}

function captureActionRecords(actions, authority) {
  return actions.map((action) => {
    const hasTarget = action !== null && typeof action === "object"
      && Object.prototype.hasOwnProperty.call(action, "id");
    return {
      action: jsonClone(action),
      authority: { ...authority },
      hasTarget,
      target: hasTarget ? nodeSemanticSignature(action.id) : null,
    };
  });
}

function actionRecordIsCurrent(record) {
  if (record.authority.epoch !== epoch || sequence < record.authority.sequence
      || revision < record.authority.revision) return false;
  if (!record.hasTarget) return true;
  return record.target !== null && record.target === nodeSemanticSignature(record.action.id);
}

function commitEditTransaction(transaction) {
  if (!transaction) return;
  for (const edit of transaction.edits) {
    if (pendingEdits.get(edit.id)?.version === edit.version) pendingEdits.delete(edit.id);
  }
  for (const finish of transaction.finishes) {
    if (finishingEdits.get(finish.id) === finish.version) finishingEdits.delete(finish.id);
    if (unfinishedEdits.get(finish.id)?.version === finish.version) {
      unfinishedEdits.delete(finish.id);
    }
  }
}

function rollbackEditTransaction(transaction) {
  if (!transaction) return;
  for (const edit of transaction.edits) {
    const pending = pendingEdits.get(edit.id);
    if (pending?.version === edit.version) pending.queued = false;
  }
  for (const finish of transaction.finishes) {
    if (finishingEdits.get(finish.id) === finish.version) finishingEdits.delete(finish.id);
  }
}

function schedulePendingEditRetry(finishes, actions) {
  retryFinishes.push(...finishes);
  retryActions.push(...actions);
  if (retryEditScheduled) return;
  retryEditScheduled = true;
  queueMicrotask(() => {
    retryEditScheduled = false;
    const finishesToRetry = retryFinishes;
    const actionsToRetry = retryActions;
    retryFinishes = [];
    retryActions = [];
    if (stopped || recovering) return;
    const batch = takePendingEdits(finishesToRetry);
    if (batch.actions.length || actionsToRetry.length) {
      queueActions(actionsToRetry.map((record) => record.action), {
        flush: false,
        batch,
        retryRecords: actionsToRetry,
      }).catch(reportFailure);
    }
  });
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
      transaction: options.transaction || null,
      cancelActions: options.cancelActions || null,
      retryActions: options.retryActions || [],
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
  rollbackEditTransaction(entry.transaction);
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
    rollbackEditTransaction(entry.transaction);
    if (entry.priority === "cancel" && entry.cancelActions?.length) {
      recoverFromCancel(entry.cancelActions).then(entry.resolve, entry.reject);
      return;
    }
    const replayableActions = entry.priority === "automatic"
      ? [] : entry.retryActions.filter(actionRecordIsCurrent);
    if (transactionHasEdits(entry.transaction) || replayableActions.length) {
      schedulePendingEditRetry(entry.transaction?.finishes || [], replayableActions);
    }
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
    rollbackEditTransaction(entry.transaction);
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
    rollbackEditTransaction(entry.transaction);
    entry.reject(new DemoProtocolError(message.error.code));
  } else {
    lastSafeResponse = jsonClone(message);
    try {
      commitEditTransaction(entry.transaction);
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
    rollbackEditTransaction(inFlight.transaction);
    inFlight.resolve(null);
    inFlight = null;
  }
  for (const entry of queue) {
    rollbackEditTransaction(entry.transaction);
    entry.resolve(null);
  }
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
  const batch = options.batch || (options.flush === false
    ? emptyEditBatch() : takePendingEdits(true));
  const ordered = [...batch.actions, ...actions];
  const authority = { epoch, sequence, revision };
  const envelope = makeEnvelope(ordered);
  const retryRecords = options.retryRecords || captureActionRecords(actions, authority);
  const cancelActions = ordered.filter(isCancelAction);
  const cancel = cancelActions.length > 0;
  if (cancel) startCancelWatchdog(cancelActions);
  return enqueueCommand("feed", { envelope }, {
    priority: cancel ? "cancel" : (options.priority || "user"),
    authority,
    transaction: batch.transaction,
    cancelActions,
    retryActions: retryRecords,
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
  line.setAttribute("role", "status");
  line.setAttribute("aria-live", "polite");
  line.setAttribute("aria-atomic", "true");
  const spinner = document.createElement("span");
  spinner.className = "spinner";
  const text = document.createElement("span");
  text.textContent = label;
  line.append(spinner, text);
  $("dbody").replaceChildren(line);
  $("dbtns").replaceChildren();
  $("overlay").classList.add("show");
  $("overlay").querySelector(".dialog").focus({ preventScroll: true });
}

function beginFlow(menuId) {
  const active = document.activeElement;
  if (active && !$("overlay").contains(active)) {
    dialogReturnFocus = active.closest?.("#ipMenu") ? $("ipMenuBtn") : active;
  }
  clearAutomaticAdvance();
  epoch += 1;
  sequence = 0;
  revision = 0;
  currentNodes = new Map();
  currentRootId = null;
  pendingEdits.clear();
  unfinishedEdits.clear();
  finishingEdits.clear();
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
  const batch = takePendingEdits(true);
  if (batch.actions.length && currentNodes.size) {
    return queueActions([], { flush: false, batch }).then(() => beginFlow(menuId));
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
    currentRootId = null;
    pendingEdits.clear();
    unfinishedEdits.clear();
    finishingEdits.clear();
    const target = dialogReturnFocus;
    dialogReturnFocus = null;
    if (target?.isConnected) target.focus({ preventScroll: true });
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
  $("dbtns").querySelector("button")?.focus({ preventScroll: true });
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

function rememberEdit(id, element, action) {
  clearAutomaticAdvance();
  const version = editVersion += 1;
  const edit = { id, version, signature: nodeSemanticSignature(id) };
  unfinishedEdits.set(id, edit);
  pendingEdits.set(id, {
    element,
    action,
    version,
    signature: edit.signature,
    queued: false,
  });
}

function scheduleEdit(id, element, action) {
  rememberEdit(id, element, action);
  if (editTimer !== null) clearTimeout(editTimer);
  editTimer = setTimeout(() => {
    editTimer = null;
    const batch = takePendingEdits(false);
    if (batch.actions.length) {
      queueActions([], { flush: false, batch }).catch(reportFailure);
    }
  }, EDIT_DEBOUNCE_MS);
}

function discardEditVersion(id, version) {
  if (pendingEdits.get(id)?.version === version) pendingEdits.delete(id);
  if (unfinishedEdits.get(id)?.version === version) unfinishedEdits.delete(id);
  if (finishingEdits.get(id) === version) finishingEdits.delete(id);
}

function takePendingEdits(finish) {
  if (editTimer !== null) clearTimeout(editTimer);
  if (blurTimer !== null) clearTimeout(blurTimer);
  editTimer = null;
  blurTimer = null;
  const batch = emptyEditBatch();
  for (const [id, pending] of pendingEdits) {
    if (pending.queued || composing.has(pending.element)) continue;
    if (pending.signature === null || pending.signature !== nodeSemanticSignature(id)) {
      discardEditVersion(id, pending.version);
      continue;
    }
    batch.actions.push(pending.action(pending.element));
    batch.transaction.edits.push({
      id,
      version: pending.version,
      signature: pending.signature,
    });
    pending.queued = true;
  }
  const finishes = finish === true ? [...unfinishedEdits.values()] : (finish || []);
  const seen = new Set();
  for (const requested of finishes) {
    const current = unfinishedEdits.get(requested.id);
    const key = `${requested.id}:${requested.version}`;
    if (seen.has(key) || current?.version !== requested.version) continue;
    seen.add(key);
    if (requested.signature === null
        || requested.signature !== nodeSemanticSignature(requested.id)) {
      discardEditVersion(requested.id, requested.version);
      continue;
    }
    if (finishingEdits.get(requested.id) !== requested.version
        && currentNodes.get(requested.id)?.actions.includes("finish-edit")) {
      batch.actions.push({ type: "finish-edit", id: requested.id });
      batch.transaction.finishes.push({ ...requested });
      finishingEdits.set(requested.id, requested.version);
    }
  }
  return batch;
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
    if (control.demoComposing || composing.has(control)) {
      rememberEdit(id, control, control.demoAction);
    } else scheduleEdit(id, control, control.demoAction);
  };
  element.onblur = (event) => {
    const control = event.currentTarget;
    if ((!pendingEdits.has(id) && !unfinishedEdits.has(id)) || composing.has(control)) return;
    if (blurTimer !== null) clearTimeout(blurTimer);
    blurTimer = setTimeout(() => {
      blurTimer = null;
      const batch = takePendingEdits(true);
      if (batch.actions.length) {
        queueActions([], { flush: false, batch }).catch(reportFailure);
      }
    }, 0);
  };
}

function isEditableControl(element) {
  if (element instanceof HTMLTextAreaElement) return !element.readOnly && !element.disabled;
  if (!(element instanceof HTMLInputElement) || element.readOnly || element.disabled) return false;
  return !["button", "checkbox", "file", "radio", "reset", "submit"].includes(element.type);
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
    key(id, key, modifiers, target) {
      clearAutomaticAdvance();
      if (["Alt", "Control", "Meta", "Shift"].includes(key)) return false;
      if (key === "Tab") return false;
      const editable = isEditableControl(target);
      if (key === "Enter" && editable) {
        queueActions([]).catch(reportFailure);
        return true;
      }
      if (key === "Enter") return false;
      if (key !== "Escape") return false;
      if (key === "Escape" && id !== currentRootId) return false;
      queueActions([{ type: "key", id, key, modifiers: modifiers.map((value) =>
        value.toLowerCase()) }]).catch(reportFailure);
      return true;
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
  currentRootId = tree.root_id;
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
  $("ipMenuBtn").setAttribute("aria-expanded", "false");
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
  $("ipMenuBtn").setAttribute("aria-expanded",
    String($("ipMenu").classList.contains("show")));
  event.stopPropagation();
});
$("ipMenuBtn").addEventListener("keydown", (event) => {
  if (!["Enter", " "].includes(event.key)) return;
  event.preventDefault();
  $("ipMenuBtn").click();
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
