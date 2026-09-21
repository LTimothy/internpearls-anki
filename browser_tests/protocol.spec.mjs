import { expect, test } from "@playwright/test";

const COMMON = {
  parent_id: null,
  children: [],
  visible: true,
  effective_visible: true,
  enabled: true,
  effective_enabled: true,
  accessible_name: "",
  accessible_description: "",
  tooltip: "",
  focus_policy: "none",
  readonly: false,
  style_roles: [],
  actions: [],
};

function fixtureTree() {
  const field = {
    ...COMMON,
    id: "field",
    kind: "line",
    parent_id: "root",
    value: "Alpha",
    placeholder: "",
    selection_start: 0,
    selection_end: 0,
    password: false,
    max_length: 80,
    max_blocks: 0,
    actions: ["edit-text", "finish-edit", "key"],
  };
  const details = {
    ...COMMON,
    id: "details",
    kind: "label",
    parent_id: "root",
    format: "rich",
    text: "<details open><summary>Details</summary><span>Generic text</span></details>",
    wrap: true,
    alignment: "start",
    strike: false,
    selectable: false,
    link_actions: [],
  };
  const longRow = {
    ...COMMON,
    id: "long-row",
    kind: "label",
    parent_id: "scroll",
    format: "plain",
    text: "A generic row that is tall enough to scroll. ".repeat(120),
    wrap: true,
    alignment: "start",
    strike: false,
    selectable: false,
    link_actions: [],
  };
  const scroll = {
    ...COMMON,
    id: "scroll",
    kind: "scroll",
    parent_id: "root",
    children: ["long-row"],
    offset: 0,
    extent: 200,
    shown_count: 1,
    total_count: 1,
    row_ids: ["long-row"],
    actions: ["scroll"],
  };
  const proceed = {
    ...COMMON,
    id: "proceed",
    kind: "button",
    parent_id: "root",
    text: "Continue",
    checkable: false,
    checked: false,
    role: "accept",
    default: true,
    escape: false,
    actions: ["activate"],
  };
  const cancel = {
    ...COMMON,
    id: "cancel",
    kind: "button",
    parent_id: "root",
    text: "Cancel",
    checkable: false,
    checked: false,
    role: "reject",
    default: false,
    escape: true,
    actions: ["activate"],
  };
  const root = {
    ...COMMON,
    id: "root",
    kind: "col",
    children: ["field", "details", "scroll", "proceed", "cancel"],
    margins: { top: 0, right: 0, bottom: 0, left: 0 },
    gap: 8,
    stretches: [],
    alignment: "start",
    actions: ["key", "close"],
  };
  return { root_id: "root", nodes: [root, field, details, scroll, longRow, proceed, cancel] };
}

function runnerEnvelope(message, options = {}) {
  const request = message.payload.envelope || {};
  return {
    protocol: 1,
    epoch: request.epoch ?? message.payload.epoch ?? 1,
    sequence: request.sequence ?? 0,
    render_revision: (request.render_revision ?? 0) + 1,
    status: options.status || "need",
    payload: options.payload || {
      kind: "dialog",
      title: "Generic task",
      contract_tree: fixtureTree(),
    },
    pending: options.pending === undefined ? [{
      task_id: [request.epoch ?? message.payload.epoch ?? 1, 1, 1, 1],
      kind: "assistant",
      stage: "assistant:event:1",
    }] : options.pending,
    safe_status: { code: options.pending === null ? "ready" : "working" },
  };
}

async function installFixtureWorker(page, delayedTypes = []) {
  await page.addInitScript(({ delayedTypes }) => {
    const nativeSetTimeout = window.setTimeout.bind(window);
    window.__scheduledDelays = [];
    window.setTimeout = (callback, delay, ...args) => {
      window.__scheduledDelays.push(delay);
      return nativeSetTimeout(callback, delay, ...args);
    };

    class FixtureWorker extends EventTarget {
      constructor() {
        super();
        this.messages = [];
        this.recovery = [];
        this.terminated = false;
        this.delayedTypes = new Set(delayedTypes);
        window.__fixtureWorkers.push(this);
      }

      postMessage(message) {
        this.messages.push(structuredClone(message));
        if (!this.delayedTypes.has(message.type)) {
          queueMicrotask(() => this.respond(this.messages.length - 1));
        }
      }

      responsePayload(message) {
        const state = {
          state: { decks: [{ name: "Sample", cards: [] }], version: "1.0" },
          config: { auto_sync: false, interval: 1 },
          tooltips: [],
        };
        if (message.type === "boot") {
          return { ...state, menu: [{ t: "action", id: "sample", label: "Open" }] };
        }
        if (message.type === "menu") return { menu: [] };
        if (message.type === "state") return state;
        if (message.type === "start" || message.type === "feed") {
          return { ...state, response: window.__runnerEnvelope(message) };
        }
        if (message.type === "maintainer") {
          return { label: "Updated sample", deck: "Sample" };
        }
        if (message.type === "set-theme") return {};
        if (message.type === "reset") {
          this.recovery = structuredClone(message.payload.recovery);
          return { ...state, response: window.__runnerEnvelope({
            type: "feed",
            payload: { envelope: message.payload.cancel.envelope },
          }) };
        }
        throw new Error(`unsupported fixture message: ${message.type}`);
      }

      respond(index, overrides = {}) {
        const message = this.messages[index];
        if (!message || this.terminated) return;
        const payload = this.responsePayload(message);
        if (["start", "feed", "maintainer", "set-theme", "state"].includes(message.type)) {
          if (message.type !== "feed" || overrides.record !== false) {
            this.recovery.push({
              type: message.type,
              payload: structuredClone(message.payload),
              result: structuredClone(payload),
            });
          }
        }
        const response = {
          protocol: 1,
          request_id: message.request_id,
          type: message.type,
          ok: true,
          payload,
          error: null,
          recovery: structuredClone(this.recovery),
          ...overrides,
        };
        this.dispatchEvent(new MessageEvent("message", { data: structuredClone(response) }));
      }

      emit(response) {
        this.dispatchEvent(new MessageEvent("message", { data: structuredClone(response) }));
      }

      terminate() {
        this.terminated = true;
      }
    }

    window.__fixtureWorkers = [];
    window.__runnerEnvelope = () => ({
      protocol: 1,
      epoch: 1,
      sequence: 0,
      render_revision: 1,
      status: "need",
      payload: { kind: "dialog", title: "Generic task", contract_tree: null },
      pending: [],
      safe_status: { code: "ready" },
    });
    Object.defineProperty(window, "Worker", { value: FixtureWorker, configurable: true });
  }, { delayedTypes });
  await page.addInitScript((tree) => {
    window.__runnerEnvelope = (message) => {
      const request = message.payload.envelope || {};
      return {
        protocol: 1,
        epoch: request.epoch ?? message.payload.epoch ?? 1,
        sequence: request.sequence ?? 0,
        render_revision: (request.render_revision ?? 0) + 1,
        status: "need",
        payload: { kind: "dialog", title: "Generic task", contract_tree: tree },
        pending: [{
          task_id: [request.epoch ?? message.payload.epoch ?? 1, 1, 1, 1],
          kind: "assistant",
          stage: "assistant:event:1",
        }],
        safe_status: { code: "working" },
      };
    };
  }, fixtureTree());
}

async function openFixture(page, delayedTypes = []) {
  await installFixtureWorker(page, delayedTypes);
  await page.goto("/docs/index.html");
  await page.waitForFunction(() => window.demo && window.demo.ready === true, null, {
    timeout: 2000,
  });
}

test("validates typed JSON messages and fixed watchdogs", async ({ page }) => {
  await page.goto("/browser_tests/contract-page.html");
  const result = await page.evaluate(async () => {
    const protocol = await import("/docs/demo-protocol.js");
    const valid = protocol.validatePageMessage({
      protocol: 1,
      request_id: 1,
      type: "state",
      payload: { action: "read" },
    });
    const errors = [];
    for (const message of [
      { protocol: 1, request_id: 2, type: "unknown", payload: {} },
      { protocol: 1, request_id: 3, type: "state", payload: { value: Infinity } },
      { protocol: 1, request_id: 4, type: "state", payload: { value: undefined } },
    ]) {
      try {
        protocol.validatePageMessage(message);
      } catch (error) {
        errors.push(error.code);
      }
    }
    return {
      valid,
      errors,
      boot: protocol.BOOT_WATCHDOG_MS,
      request: protocol.REQUEST_WATCHDOG_MS,
      cancel: protocol.CANCEL_WATCHDOG_MS,
      advance: protocol.ADVANCE_DELAY_MS,
      edit: protocol.EDIT_DEBOUNCE_MS,
    };
  });
  expect(result.valid.type).toBe("state");
  expect(result.errors).toEqual(["invalid-message", "invalid-message", "invalid-message"]);
  expect(result).toMatchObject({
    boot: 120000,
    request: 10000,
    cancel: 5000,
    advance: 200,
    edit: 100,
  });
});

test("keeps Pyodide and Python proxies off the main page", async ({ page }) => {
  await openFixture(page);
  const exposed = await page.evaluate(() => ({
    keys: Object.keys(window.demo).sort(),
    hasPyodide: "loadPyodide" in window,
    constructors: Object.values(window.demo).map((value) => value?.constructor?.name),
  }));
  expect(exposed.keys).toEqual(["dispatch", "ready", "request", "runFlow"]);
  expect(exposed.hasPyodide).toBe(false);
  expect(exposed.constructors).not.toContain("PyProxy");
});

test("allows one request in flight with monotonic request ids", async ({ page }) => {
  await openFixture(page, ["menu"]);
  expect(await page.evaluate(() => window.__scheduledDelays)).toContain(120000);
  await page.evaluate(() => {
    window.__requests = [window.demo.request("menu", {}), window.demo.request("menu", {})];
  });
  expect(await page.evaluate(() => window.__scheduledDelays)).toContain(10000);
  let messages = await page.evaluate(() => window.__fixtureWorkers[0].messages);
  expect(messages.map((message) => [message.request_id, message.type])).toEqual([
    [1, "boot"],
    [2, "menu"],
  ]);

  await page.evaluate(() => window.__fixtureWorkers[0].respond(1));
  await page.evaluate(() => window.__requests[0]);
  messages = await page.evaluate(() => window.__fixtureWorkers[0].messages);
  expect(messages.map((message) => [message.request_id, message.type])).toEqual([
    [1, "boot"],
    [2, "menu"],
    [3, "menu"],
  ]);
  await page.evaluate(() => window.__fixtureWorkers[0].respond(2));
  await page.evaluate(() => Promise.all(window.__requests));
});

test("advances epoch sequence and revision monotonically", async ({ page }) => {
  await openFixture(page, ["feed"]);
  await page.evaluate(() => window.demo.runFlow("sample"));
  let start = await page.evaluate(() => window.__fixtureWorkers[0].messages[1]);
  expect(start.payload).toEqual({ menu_id: "sample", epoch: 1 });

  await page.evaluate(() => {
    window.__dispatch = window.demo.dispatch([]);
  });
  let feed = await page.evaluate(() => window.__fixtureWorkers[0].messages[2]);
  expect(feed.payload.envelope).toMatchObject({
    epoch: 1,
    sequence: 1,
    render_revision: 1,
  });
  await page.evaluate(() => window.__fixtureWorkers[0].respond(2));
  await page.evaluate(() => window.__dispatch);

  await page.evaluate(() => {
    window.__dispatch = window.demo.dispatch([]);
  });
  feed = await page.evaluate(() => window.__fixtureWorkers[0].messages[3]);
  expect(feed.payload.envelope).toMatchObject({
    epoch: 1,
    sequence: 2,
    render_revision: 2,
  });
  await page.evaluate(() => window.__fixtureWorkers[0].respond(3));
  await page.evaluate(() => window.__dispatch);

  await page.evaluate(() => window.demo.runFlow("sample"));
  start = await page.evaluate(() => window.__fixtureWorkers[0].messages[4]);
  expect(start.payload).toEqual({ menu_id: "sample", epoch: 2 });
});

test("ignores duplicate and stale Worker responses", async ({ page }) => {
  await openFixture(page, ["menu"]);
  await page.evaluate(() => {
    window.__menuRequest = window.demo.request("menu", {});
  });
  await page.evaluate(() => {
    const worker = window.__fixtureWorkers[0];
    worker.respond(1);
    const accepted = {
      protocol: 1,
      request_id: 2,
      type: "menu",
      ok: true,
      payload: { menu: [{ id: "stale" }] },
      error: null,
      recovery: [],
    };
    worker.emit(accepted);
    worker.emit({ ...accepted, request_id: 1 });
  });
  await expect(page.locator("#ipMenu")).not.toContainText("stale");
  const messages = await page.evaluate(() => window.__fixtureWorkers[0].messages.length);
  expect(messages).toBe(2);
});

test("paints pending state before one-credit automatic advances", async ({ page }) => {
  await page.clock.install();
  await openFixture(page, ["feed"]);
  await page.evaluate(() => window.demo.runFlow("sample"));
  await expect(page.getByRole("button", { name: "Cancel" })).toBeVisible();
  expect(await page.evaluate(() => window.__fixtureWorkers[0].messages.length)).toBe(2);

  await page.clock.runFor(199);
  expect(await page.evaluate(() => window.__fixtureWorkers[0].messages.length)).toBe(2);
  await page.clock.runFor(1);
  const feed = await page.evaluate(() => window.__fixtureWorkers[0].messages[2]);
  expect(feed.type).toBe("feed");
  expect(feed.payload.envelope.actions).toEqual([
    { type: "advance", elapsed_ms: 200, checkpoint_credits: 1 },
  ]);
});

test("keeps Cancel clickable and recovers after five seconds", async ({ page }) => {
  await page.clock.install();
  await openFixture(page, ["feed"]);
  await page.evaluate(() => window.demo.runFlow("sample"));
  await page.clock.runFor(200);
  const cancel = page.getByRole("button", { name: "Cancel" });
  await expect(cancel).toBeEnabled();
  await cancel.click();
  await page.clock.runFor(4999);
  expect(await page.evaluate(() => window.__fixtureWorkers.length)).toBe(1);
  await page.clock.runFor(1);
  await expect.poll(() => page.evaluate(() => window.__fixtureWorkers.length)).toBe(2);
  const recovery = await page.evaluate(() => ({
    oldTerminated: window.__fixtureWorkers[0].terminated,
    messages: window.__fixtureWorkers[1].messages,
  }));
  expect(recovery.oldTerminated).toBe(true);
  expect(recovery.messages.map((message) => message.type)).toEqual(["boot", "reset"]);
  expect(recovery.messages[1].payload.recovery.map((entry) => entry.type)).toEqual(["start"]);
  expect(recovery.messages[1].payload.cancel.envelope.actions).toEqual([
    { type: "activate", id: "cancel" },
  ]);
});

test("recovers a queued Cancel after an earlier response changes authority", async ({ page }) => {
  await openFixture(page, ["feed"]);
  await page.evaluate(() => window.demo.runFlow("sample"));
  await page.evaluate(() => {
    window.__priorRequest = window.demo.dispatch([{ type: "activate", id: "proceed" }]);
  });
  await page.getByRole("button", { name: "Cancel" }).click();
  expect(await page.evaluate(() => window.__fixtureWorkers[0].messages.length)).toBe(3);

  await page.evaluate(() => window.__fixtureWorkers[0].respond(2));
  await expect.poll(() => page.evaluate(() => window.__fixtureWorkers.length)).toBe(2);
  const recovery = await page.evaluate(() => ({
    terminated: window.__fixtureWorkers[0].terminated,
    types: window.__fixtureWorkers[1].messages.map((message) => message.type),
    cancel: window.__fixtureWorkers[1].messages.find((message) => message.type === "reset")
      ?.payload.cancel.envelope.actions,
  }));
  expect(recovery).toEqual({
    terminated: true,
    types: ["boot", "reset"],
    cancel: [{ type: "activate", id: "cancel" }],
  });
});

test("patches stable ids without losing focus selection expansion or scroll", async ({ page }) => {
  await openFixture(page);
  await page.evaluate(() => window.demo.runFlow("sample"));
  await page.evaluate(() => {
    const field = document.querySelector("#field");
    field.focus();
    field.setSelectionRange(1, 4);
    document.querySelector("#details details").open = false;
    const scroll = document.querySelector("#scroll");
    if (scroll.scrollHeight <= scroll.clientHeight) throw new Error("fixture is not scrollable");
    scroll.scrollTop = 40;
    window.__stableElements = { field, scroll };
  });
  await page.evaluate(() => window.demo.dispatch([]));
  const preserved = await page.evaluate(() => {
    const field = document.querySelector("#field");
    const scroll = document.querySelector("#scroll");
    return {
      sameField: field === window.__stableElements.field,
      sameScroll: scroll === window.__stableElements.scroll,
      focused: document.activeElement === field,
      selection: [field.selectionStart, field.selectionEnd],
      expanded: document.querySelector("#details details").open,
      scrollTop: scroll.scrollTop,
    };
  });
  expect(preserved).toEqual({
    sameField: true,
    sameScroll: true,
    focused: true,
    selection: [1, 4],
    expanded: false,
    scrollTop: 40,
  });
});

test("keeps surviving stable ids when a new sibling is inserted", async ({ page }) => {
  await openFixture(page);
  await page.evaluate(() => window.demo.runFlow("sample"));
  await page.evaluate(() => {
    window.__survivingField = document.querySelector("#field");
    const priorEnvelope = window.__runnerEnvelope;
    window.__runnerEnvelope = (message) => {
      const response = priorEnvelope(message);
      const tree = response.payload.contract_tree;
      const root = tree.nodes.find((node) => node.id === tree.root_id);
      root.children.unshift("inserted");
      tree.nodes.push({
        ...tree.nodes.find((node) => node.id === "field"),
        id: "inserted",
        value: "Inserted generic text",
      });
      return response;
    };
  });
  await page.evaluate(() => window.demo.dispatch([]));
  const result = await page.evaluate(() => ({
    sameField: document.querySelector("#field") === window.__survivingField,
    inserted: document.querySelector("#inserted")?.value,
  }));
  expect(result).toEqual({
    sameField: true,
    inserted: "Inserted generic text",
  });
});

test("flushes debounced edits before actions and waits for composition end", async ({ page }) => {
  await page.clock.install();
  await openFixture(page, ["feed"]);
  await page.evaluate(() => window.demo.runFlow("sample"));
  const field = page.locator("#field");
  await field.fill("Alphabet");
  await page.clock.runFor(99);
  expect(await page.evaluate(() => window.__fixtureWorkers[0].messages.length)).toBe(2);
  await page.locator("#proceed").dispatchEvent("click");
  let feed = await page.evaluate(() => window.__fixtureWorkers[0].messages[2]);
  expect(feed.payload.envelope.actions.map((action) => action.type)).toEqual([
    "edit-text", "finish-edit", "activate",
  ]);

  await page.evaluate(() => window.__fixtureWorkers[0].respond(2));
  await page.evaluate(() => {
    const input = document.querySelector("#field");
    input.dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true }));
    input.value = "Alphabet composed";
    input.dispatchEvent(new InputEvent("input", {
      bubbles: true,
      inputType: "insertCompositionText",
      data: " composed",
      isComposing: true,
    }));
  });
  await page.locator("#proceed").dispatchEvent("click");
  await page.clock.runFor(150);
  expect(await page.evaluate(() => window.__fixtureWorkers[0].messages.length)).toBe(3);
  await page.evaluate(() => {
    const input = document.querySelector("#field");
    input.dispatchEvent(new CompositionEvent("compositionend", {
      bubbles: true,
      data: " composed",
    }));
  });
  await page.clock.runFor(0);
  feed = await page.evaluate(() => window.__fixtureWorkers[0].messages[3]);
  expect(feed.payload.envelope.actions).toEqual([
    {
      type: "edit-text",
      id: "field",
      value: "Alphabet composed",
      selection_start: 17,
      selection_end: 17,
      composing: false,
    },
    { type: "finish-edit", id: "field" },
    { type: "activate", id: "proceed" },
  ]);

  const submitPrevented = await page.evaluate(() => {
    const form = document.createElement("form");
    document.body.appendChild(form);
    const event = new Event("submit", { bubbles: true, cancelable: true });
    form.dispatchEvent(event);
    return event.defaultPrevented;
  });
  expect(submitPrevented).toBe(true);
});

test("keeps the newest queued edit until it is acknowledged", async ({ page }) => {
  await page.clock.install();
  await openFixture(page, ["feed"]);
  await page.evaluate(() => {
    const original = window.__runnerEnvelope;
    window.__runnerEnvelope = (message) => {
      const response = original(message);
      const actions = message.payload.envelope?.actions || [];
      const edit = message.payload.envelope?.actions.find((action) =>
        action.type === "edit-text" && action.id === "field");
      if (edit) {
        response.payload.contract_tree.nodes.find((node) => node.id === "field").value = edit.value;
      }
      if (actions.some((action) => action.type === "activate" && action.id === "proceed")) {
        window.__savedValue = edit?.value || null;
      }
      return response;
    };
    return window.demo.runFlow("sample");
  });
  const field = page.locator("#field");
  await field.fill("First value");
  await page.clock.runFor(100);
  await field.fill("Final value");
  await page.clock.runFor(100);
  await page.getByRole("button", { name: "Continue" }).click();

  await page.evaluate(() => window.__fixtureWorkers[0].respond(2));
  await expect(field).toHaveValue("Final value");
  await expect.poll(() => page.evaluate(() => window.__fixtureWorkers[0].messages.length))
    .toBe(4);
  const retried = await page.evaluate(() =>
    window.__fixtureWorkers[0].messages[3].payload.envelope.actions);
  expect(retried).toContainEqual(expect.objectContaining({
    type: "edit-text", id: "field", value: "Final value",
  }));
  expect(retried).toContainEqual({ type: "finish-edit", id: "field" });
  expect(retried).toContainEqual({ type: "activate", id: "proceed" });
  await page.evaluate(() => window.__fixtureWorkers[0].respond(3));
  expect(await page.evaluate(() => window.__savedValue)).toBe("Final value");
  await expect(field).toHaveValue("Final value");
});

test("Enter finishes an editable control without forwarding a root key", async ({ page }) => {
  await page.clock.install();
  await openFixture(page, ["feed"]);
  await page.evaluate(() => window.demo.runFlow("sample"));
  await page.locator("#field").fill("Entered value");
  await page.locator("#field").press("Enter");
  const actions = await page.evaluate(() =>
    window.__fixtureWorkers[0].messages[2].payload.envelope.actions);
  expect(actions).toEqual([
    expect.objectContaining({ type: "edit-text", id: "field", value: "Entered value" }),
    { type: "finish-edit", id: "field" },
  ]);
});

test("Tab and Shift Tab retain native focus navigation", async ({ page }) => {
  await page.clock.install();
  await openFixture(page, ["feed"]);
  await page.evaluate(() => window.demo.runFlow("sample"));
  await page.locator("#field").focus();
  await page.locator("#field").press("Tab");
  expect(await page.locator("#field").evaluate((element) => document.activeElement !== element))
    .toBe(true);
  await page.keyboard.press("Shift+Tab");
  await expect(page.locator("#field")).toBeFocused();
  const actions = await page.evaluate(() => window.__fixtureWorkers[0].messages.slice(2)
    .flatMap((message) => message.payload.envelope.actions));
  expect(actions).toEqual([]);
});

test("blur finishes one acknowledged text edit exactly once", async ({ page }) => {
  await page.clock.install();
  await openFixture(page, ["feed"]);
  await page.evaluate(() => window.demo.runFlow("sample"));
  await page.locator("#field").fill("Finished value");
  await page.clock.runFor(100);
  await page.evaluate(() => window.__fixtureWorkers[0].respond(2));
  await page.locator("#proceed").focus();
  await page.clock.runFor(0);
  const actions = await page.evaluate(() =>
    window.__fixtureWorkers[0].messages[3].payload.envelope.actions);
  expect(actions).toEqual([{ type: "finish-edit", id: "field" }]);
  await page.evaluate(() => {
    document.querySelector("#field").dispatchEvent(new FocusEvent("blur"));
  });
  await page.clock.runFor(0);
  expect(await page.evaluate(() => window.__fixtureWorkers[0].messages.length)).toBe(4);
});

test("does not consume a request id for an invalid public request", async ({ page }) => {
  await openFixture(page);
  const result = await page.evaluate(async () => {
    let invalidCode = null;
    try {
      await window.demo.request("state", { action: "unsupported" });
    } catch (error) {
      invalidCode = error.code;
    }
    await window.demo.request("menu", {});
    return {
      invalidCode,
      requests: window.__fixtureWorkers[0].messages.map((message) => ({
        requestId: message.request_id,
        type: message.type,
      })),
    };
  });
  expect(result).toEqual({
    invalidCode: "invalid-message",
    requests: [
      { requestId: 1, type: "boot" },
      { requestId: 2, type: "menu" },
    ],
  });
});

test("discards a queued action when the acknowledged tree changes", async ({ page }) => {
  await openFixture(page, ["feed"]);
  await page.evaluate(() => window.demo.runFlow("sample"));
  await page.evaluate(() => {
    const original = window.__runnerEnvelope;
    window.__runnerEnvelope = (message) => {
      const response = original(message);
      const proceed = response.payload.contract_tree.nodes.find((node) => node.id === "proceed");
      proceed.text = "Changed action";
      return response;
    };
    window.__firstClick = window.demo.dispatch([{ type: "activate", id: "proceed" }]);
    window.__secondClick = window.demo.dispatch([{ type: "activate", id: "proceed" }]);
  });
  expect(await page.evaluate(() => window.__fixtureWorkers[0].messages.length)).toBe(3);
  await page.evaluate(() => window.__fixtureWorkers[0].respond(2));
  await page.evaluate(() => window.__firstClick);
  await expect.poll(() => page.evaluate(() => window.__fixtureWorkers[0].messages.length))
    .toBe(3);
  expect(await page.evaluate(() => window.__secondClick)).toBeNull();
});

test("does not rebind an edit-bound activation when its target meaning changes", async ({ page }) => {
  await openFixture(page, ["feed"]);
  await page.evaluate(() => window.demo.runFlow("sample"));
  await page.evaluate(() => {
    const original = window.__runnerEnvelope;
    window.__runnerEnvelope = (message) => {
      const response = original(message);
      const proceed = response.payload.contract_tree.nodes.find((node) => node.id === "proceed");
      proceed.text = "Changed action";
      return response;
    };
    window.__treeChange = window.demo.dispatch([]);
  });
  await page.locator("#field").fill("Pending value");
  await page.getByRole("button", { name: "Continue" }).click();

  expect(await page.evaluate(() => window.__fixtureWorkers[0].messages.length)).toBe(3);
  await page.evaluate(() => window.__fixtureWorkers[0].respond(2));
  await page.evaluate(() => window.__treeChange);
  await expect.poll(() => page.evaluate(() => window.__fixtureWorkers[0].messages.length))
    .toBe(4);
  const retried = await page.evaluate(() =>
    window.__fixtureWorkers[0].messages[3].payload.envelope.actions);
  expect(retried).toContainEqual(expect.objectContaining({
    type: "edit-text", id: "field", value: "Pending value",
  }));
  expect(retried).toContainEqual({ type: "finish-edit", id: "field" });
  expect(retried).not.toContainEqual({ type: "activate", id: "proceed" });
});

test("updates a stable rich link listener when its action changes", async ({ page }) => {
  await openFixture(page);
  await page.evaluate(() => window.demo.runFlow("sample"));
  await page.evaluate(async () => {
    const original = window.__runnerEnvelope;
    let actionId = "first-action";
    window.__runnerEnvelope = (message) => {
      const response = original(message);
      const details = response.payload.contract_tree.nodes.find((node) => node.id === "details");
      details.text = `<a href="${actionId}">Open</a>`;
      details.link_actions = [actionId];
      details.actions = ["activate-link"];
      return response;
    };
    await window.demo.dispatch([]);
    actionId = "second-action";
    await window.demo.dispatch([]);
  });
  await page.locator("#details a").dispatchEvent("click");
  const action = await page.evaluate(() => {
    const messages = window.__fixtureWorkers[0].messages;
    return messages[messages.length - 1].payload.envelope.actions[0];
  });
  expect(action).toEqual({
    type: "activate-link",
    id: "details",
    action_id: "second-action",
  });
});

test("preserves editable combo focus and selection across a stable patch", async ({ page }) => {
  await openFixture(page);
  await page.evaluate(() => window.demo.runFlow("sample"));
  await page.evaluate(async () => {
    const original = window.__runnerEnvelope;
    window.__runnerEnvelope = (message) => {
      const response = original(message);
      const field = response.payload.contract_tree.nodes.find((node) => node.id === "field");
      for (const key of [
        "value", "placeholder", "selection_start", "selection_end", "password",
        "max_length", "max_blocks",
      ]) delete field[key];
      Object.assign(field, {
        kind: "combo",
        options: [{ id: "one", label: "One" }],
        current_index: 0,
        current_text: "One",
        editable: true,
        editor_value: "Editable",
        actions: ["select-option", "edit-text"],
      });
      return response;
    };
    await window.demo.dispatch([]);
    const editor = document.querySelector("#field input");
    editor.focus();
    editor.setSelectionRange(2, 6);
    window.__comboEditor = editor;
    await window.demo.dispatch([]);
  });
  const state = await page.evaluate(() => {
    const editor = document.querySelector("#field input");
    return {
      same: editor === window.__comboEditor,
      focused: document.activeElement === editor,
      selection: [editor.selectionStart, editor.selectionEnd],
    };
  });
  expect(state).toEqual({ same: true, focused: true, selection: [2, 6] });
});

test("preserves spin input focus across acknowledgement and continued typing", async ({ page }) => {
  await page.clock.install();
  await openFixture(page, ["feed"]);
  await page.evaluate(() => {
    const original = window.__runnerEnvelope;
    window.__runnerEnvelope = (message) => {
      const response = original(message);
      const field = response.payload.contract_tree.nodes.find((node) => node.id === "field");
      for (const key of [
        "placeholder", "selection_start", "selection_end", "password",
        "max_length", "max_blocks",
      ]) delete field[key];
      Object.assign(field, {
        kind: "spin", value: 1, minimum: 0, maximum: 9, step: 1,
        suffix: "", special_value_text: "", actions: ["edit-text"],
      });
      return response;
    };
  });
  await page.evaluate(() => window.demo.runFlow("sample"));
  const spin = page.locator("#field input");
  await spin.fill("2");
  await page.clock.runFor(100);
  await page.evaluate(() => window.__fixtureWorkers[0].respond(2));
  await expect(spin).toBeFocused();
  expect(await spin.getAttribute("data-demo-part")).toBe("editor");

  await spin.fill("3");
  await page.clock.runFor(100);
  await expect(spin).toHaveValue("3");
  const continued = await page.evaluate(() =>
    window.__fixtureWorkers[0].messages[3].payload.envelope.actions[0]);
  expect(continued).toMatchObject({ type: "edit-text", id: "field", value: "3" });
});

test("installs current Escape handling when busy content becomes a dialog root", async ({ page }) => {
  await openFixture(page, ["feed"]);
  await page.evaluate(() => window.demo.runFlow("sample"));
  await page.locator("#root").dispatchEvent("keydown", { key: "Escape" });
  const last = await page.evaluate(() => window.__fixtureWorkers[0].messages.at(-1));
  expect(last.type).toBe("feed");
  expect(last.payload.envelope.actions).toEqual([{
    type: "key", id: "root", key: "Escape", modifiers: [],
  }]);
});

test("reorders inserts and deletes siblings without replacing survivors", async ({ page }) => {
  await openFixture(page);
  await page.evaluate(() => window.demo.runFlow("sample"));
  await page.evaluate(async () => {
    window.__survivors = {
      field: document.querySelector("#field"),
      cancel: document.querySelector("#cancel"),
    };
    const original = window.__runnerEnvelope;
    window.__runnerEnvelope = (message) => {
      const response = original(message);
      const tree = response.payload.contract_tree;
      const root = tree.nodes.find((node) => node.id === tree.root_id);
      root.children = ["cancel", "inserted", "field", "scroll", "proceed"];
      tree.nodes = tree.nodes.filter((node) => node.id !== "details");
      tree.nodes.push({
        ...tree.nodes.find((node) => node.id === "field"),
        id: "inserted",
        value: "Inserted",
      });
      return response;
    };
    await window.demo.dispatch([]);
  });
  const state = await page.evaluate(() => ({
    order: Array.from(document.querySelector("#root").children, (node) => node.id),
    sameField: document.querySelector("#field") === window.__survivors.field,
    sameCancel: document.querySelector("#cancel") === window.__survivors.cancel,
    deleted: document.querySelector("#details") === null,
  }));
  expect(state).toEqual({
    order: ["cancel", "inserted", "field", "scroll", "proceed"],
    sameField: true,
    sameCancel: true,
    deleted: true,
  });
});

test("finishes a previously delivered text edit before activation", async ({ page }) => {
  await page.clock.install();
  await openFixture(page, ["feed"]);
  await page.evaluate(() => window.demo.runFlow("sample"));
  await page.locator("#field").fill("Final value");
  await page.clock.runFor(100);
  let feed = await page.evaluate(() => window.__fixtureWorkers[0].messages[2]);
  expect(feed.payload.envelope.actions.map((action) => action.type)).toEqual(["edit-text"]);
  await page.evaluate(() => window.__fixtureWorkers[0].respond(2));
  await page.locator("#proceed").dispatchEvent("click");
  feed = await page.evaluate(() => window.__fixtureWorkers[0].messages[3]);
  expect(feed.payload.envelope.actions).toEqual([
    { type: "finish-edit", id: "field" },
    { type: "activate", id: "proceed" },
  ]);
});

test("does not finish a spin edit whose contract forbids finish-edit", async ({ page }) => {
  await page.clock.install();
  await openFixture(page, ["feed"]);
  await page.evaluate(() => {
    const original = window.__runnerEnvelope;
    window.__runnerEnvelope = (message) => {
      const response = original(message);
      const field = response.payload.contract_tree.nodes.find((node) => node.id === "field");
      for (const key of [
        "value", "placeholder", "selection_start", "selection_end", "password",
        "max_length", "max_blocks",
      ]) delete field[key];
      Object.assign(field, {
        kind: "spin", value: 1, minimum: 0, maximum: 5, step: 1,
        suffix: "", special_value_text: "", actions: ["edit-text"],
      });
      return response;
    };
  });
  await page.evaluate(() => window.demo.runFlow("sample"));
  await page.locator("#field input").fill("2");
  await page.clock.runFor(100);
  const actions = await page.evaluate(() =>
    window.__fixtureWorkers[0].messages[2].payload.envelope.actions);
  expect(actions.map((action) => action.type)).toEqual(["edit-text"]);
  await page.evaluate(() => window.__fixtureWorkers[0].respond(2));
  await page.locator("#proceed").focus();
  await page.clock.runFor(0);
  expect(await page.evaluate(() => window.__fixtureWorkers[0].messages.length)).toBe(3);
});

test("does not finish a combo control on blur", async ({ page }) => {
  await page.clock.install();
  await openFixture(page, ["feed"]);
  await page.evaluate(() => {
    const original = window.__runnerEnvelope;
    window.__runnerEnvelope = (message) => {
      const response = original(message);
      const field = response.payload.contract_tree.nodes.find((node) => node.id === "field");
      for (const key of [
        "value", "placeholder", "selection_start", "selection_end", "password",
        "max_length", "max_blocks",
      ]) delete field[key];
      Object.assign(field, {
        kind: "combo",
        options: [{ id: "one", label: "One" }],
        current_index: 0,
        current_text: "One",
        editable: true,
        editor_value: "Editable",
        actions: ["select-option", "edit-text"],
      });
      return response;
    };
  });

  await page.evaluate(() => window.demo.runFlow("sample"));
  await page.locator("#field input").fill("Changed");
  await page.clock.runFor(100);
  await page.evaluate(() => window.__fixtureWorkers[0].respond(2));
  await page.locator("#proceed").focus();
  await page.clock.runFor(0);
  expect(await page.evaluate(() => window.__fixtureWorkers[0].messages.length)).toBe(3);
});

test("does not finish a radio control on blur", async ({ page }) => {
  await openFixture(page, ["feed"]);
  await page.evaluate(() => {
    const original = window.__runnerEnvelope;
    window.__runnerEnvelope = (message) => {
      const response = original(message);
      const field = response.payload.contract_tree.nodes.find((node) => node.id === "field");
      for (const key of [
        "value", "placeholder", "selection_start", "selection_end", "password",
        "max_length", "max_blocks",
      ]) delete field[key];
      Object.assign(field, {
        kind: "radio", text: "Choice", checked: false,
        group_id: "group", exclusive: true, actions: ["toggle"],
      });
      return response;
    };
  });
  await page.evaluate(() => window.demo.runFlow("sample"));
  await page.locator("#field input").check();
  await page.evaluate(() => window.__fixtureWorkers[0].respond(2));
  await page.locator("#proceed").focus();
  await page.waitForTimeout(0);
  expect(await page.evaluate(() => window.__fixtureWorkers[0].messages.length)).toBe(3);
});

test("enters a terminal state after a protocol timeout", async ({ page }) => {
  await page.clock.install();
  await openFixture(page, ["menu"]);
  await page.evaluate(() => {
    window.__timedOut = window.demo.request("menu", {}).catch((error) => error.code);
  });
  await page.clock.runFor(10000);
  expect(await page.evaluate(() => window.__timedOut)).toBe("request-timeout");
  const nextCode = await page.evaluate(() =>
    window.demo.request("state", { action: "read" }).catch((error) => error.code));
  const state = await page.evaluate(() => ({
    terminated: window.__fixtureWorkers[0].terminated,
    messages: window.__fixtureWorkers[0].messages.map((message) => message.type),
  }));
  expect(nextCode).toBe("protocol-stopped");
  expect(state).toEqual({ terminated: true, messages: ["boot", "menu"] });
});

test("rejects malformed nested protocol shapes with fixed errors", async ({ page }) => {
  await page.goto("/browser_tests/contract-page.html");
  const result = await page.evaluate(async (tree) => {
    const protocol = await import("/docs/demo-protocol.js");
    const request = {
      protocol: 1,
      request_id: 1,
      type: "feed",
      payload: { envelope: {
        protocol: 1, epoch: 1, sequence: 1, render_revision: 1, actions: [],
      } },
    };
    const state = {
      state: { decks: [{ name: "Sample", cards: [] }], version: "1.0" },
      config: { auto_sync: false, interval: 1 },
      tooltips: [],
    };
    const worker = {
      protocol: 1,
      request_id: 1,
      type: "boot",
      ok: true,
      payload: { ...state, menu: [] },
      error: null,
      recovery: [],
    };
    const contractWorker = (contractTree) => ({
      ...worker,
      type: "start",
      payload: { ...state, response: {
        protocol: 1,
        epoch: 1,
        sequence: 0,
        render_revision: 1,
        status: "need",
        payload: { kind: "dialog", title: "Generic task", contract_tree: contractTree },
        pending: [],
        safe_status: { code: "ready" },
      } },
    });
    const malformedTree = (field, value) => {
      const copy = structuredClone(tree);
      const label = copy.nodes.find((node) => node.kind === "label");
      label[field] = value;
      return contractWorker(copy);
    };
    const malformed = [
      { ...request, protocol: true },
      { ...request, payload: { envelope: {
        ...request.payload.envelope,
        actions: [{ type: "unknown" }],
      } } },
      { ...request, payload: { envelope: {
        ...request.payload.envelope,
        actions: [{ type: "edit-text", id: "field", value: "x",
          selection_start: true, selection_end: 1, composing: false }],
      } } },
      { ...worker, payload: { ...worker.payload, menu: {} } },
      { ...worker, payload: { ...worker.payload, config: { auto_sync: "no", interval: 1 } } },
      { ...worker, payload: { ...worker.payload, tooltips: [4] } },
      { ...worker, payload: { ...worker.payload, state: { decks: "bad", version: "1.0" } } },
      { ...worker, ok: false, payload: {}, error: { code: "/runtime/private/path" } },
      malformedTree("text", null),
      malformedTree("format", null),
      malformedTree("link_actions", null),
      malformedTree("link_actions", [["nested"]]),
    ];
    return malformed.map((message, index) => {
      try {
        if (index < 3) protocol.validatePageMessage(message);
        else protocol.validateWorkerMessage(message);
        return "accepted";
      } catch (error) {
        return `${error.name}:${error.code}`;
      }
    });
  }, fixtureTree());
  expect(result).toEqual(Array(12).fill("DemoProtocolError:invalid-message"));
});
